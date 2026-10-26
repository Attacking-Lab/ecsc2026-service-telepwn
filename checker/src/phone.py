from enochecker3 import AsyncSocket

import datetime
import asyncio
import logging
import math
import os
import socket
import collections

SAMPLE_RATE = 8000
BLOCK_NUM_SAMPLES = 1000
SILENCE = bytes(BLOCK_NUM_SAMPLES)

SKIP_NUM_SAMPLES = BLOCK_NUM_SAMPLES # skip after one block

DTMF_MIN_TONE_SAMPLES = 250 # minimum
DTMF_MIN_SILENCE_SAMPLES = 250
TONE_AMPLITUDE = 32 # of one freq
DTMF_LO = (697, 770, 852, 941)
DTMF_HI = (1209, 1336, 1477, 1633)
DTMF = {
    key: (DTMF_LO[i // 4], DTMF_HI[i % 4]) for i, key in enumerate("123A456B789C*0#D")
}

LINES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "lines"
)

log = logging.getLogger(__name__)

SILENCE_LONG = bytes(5000 * BLOCK_NUM_SAMPLES)
def get_silence(num_samples):
    assert num_samples <= len(SILENCE_LONG)
    return memoryview(SILENCE_LONG)[:num_samples]
    
def ignore_future(future):
    def cb(f):
        if not f.cancelled() and f.exception():
            log.error(f"ignored future exception: {f.exception()}")
    future.add_done_callback(cb)

class TelepwnError(Exception):
    pass

class ConnectionClosed(TelepwnError):
    pass

class UnexpectedResponseError(TelepwnError):
    pass


def pad_samples(samples):
    return bytes(samples) + bytes(-len(samples) % BLOCK_NUM_SAMPLES)

_lines = {}
def line_data(name):
    if name not in _lines:
        with open(os.path.join(LINES_DIR, f"{name}.pcm"), "rb") as f:
            _lines[name] = pad_samples(f.read())
    return memoryview(_lines[name])


_tones = {}
def tone_samples(key, on_samples):
    cache_key = (key, on_samples)
    if cache_key not in _tones:
        freq_lo, freq_hi = DTMF[key]
        samples = bytearray(on_samples)
        for i in range(on_samples):
            t = i / SAMPLE_RATE
            value = TONE_AMPLITUDE * (
                math.sin(2 * math.pi * freq_lo * t) + math.sin(2 * math.pi * freq_hi * t)
            )
            samples[i] = max(-128, min(127, round(value))) & 0xFF
        _tones[cache_key] = bytes(samples)
    return _tones[cache_key]

class AsyncCheck:
    def __init__(self, rx_future, check):
        self.rx_future = rx_future
        self.check = check

    async def resolve(self):
        return await self.check(self.rx_future)

class Phone:
    def __init__(self, log: logging.LoggerAdapter, async_socket: AsyncSocket, purpose: str):
        self.log = log
        self.purpose = purpose

        # configure socket
        self._reader, self._writer = async_socket

        # set nodelay
        #sock = self._writer.transport.get_extra_info("socket")
        #assert sock is not None
        #sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        self._rx = bytearray()
        self._rx_task_handle = None
        self._rx_pending_segments = asyncio.Queue()

        self._rx_total_segments = 0
        self._rx_total_blocks = 0

        self._num_rtts = 0

        self._just_skipped = False
        self._blocks_per_class = collections.defaultdict(int)

    def log_operation(self, text):
        self.log.info(text)

    def log_dump(self, text):
        self.log.info(text)

    async def __aenter__(self):
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def init(self):
        self.init_time = datetime.datetime.now()
        self.active_future = None
        handle = asyncio.create_task(self._rx_task())
        assert handle is not None
        self._rx_task_handle = handle

    async def close(self):
        self._rx_task_handle.cancel()
        self._writer.close()
        await asyncio.gather(
            self._rx_task_handle,
            self._writer.wait_closed(),
            return_exceptions=True,
        )

        close_time = datetime.datetime.now()
        duration_secs = (close_time - self.init_time).total_seconds()

        stats_text = f"stats {self.purpose}: total {self._rx_total_blocks} blocks in {duration_secs} secs, {self._num_rtts} RTTs (outdated stat), {self._rx_total_segments} segments. "
        stats_text += f"by class: {", ".join([f"{c}={n}" for c, n in self._blocks_per_class.items()])}"
        self.log_operation(stats_text)

    async def _rx_task(self):
        try:
            while True:
                num_blocks, self.active_future = await self._rx_pending_segments.get()

                start_dt = datetime.datetime.now()
                data = await self._reader.readexactly(num_blocks * BLOCK_NUM_SAMPLES)
                end_dt = datetime.datetime.now()
                duration_secs = (end_dt - start_dt).total_seconds()
                self.log_operation(f"recv {num_blocks} took {duration_secs:.3f}s")

                self._rx_total_blocks += num_blocks
                self._rx_total_segments += 1

                if self.active_future.done():
                    self.log.error("rx result future was cancelled before it could be answered")
                else:
                    self.active_future.set_result(data)

                # Mark that it doesn't need to be set in case of an exception
                self.active_future = None

                # We are done with this segment.
                self._rx_pending_segments.task_done()
        except Exception as exc:
            self._rx_task_abort(exc)

            # Raise hell
            raise
        finally:
            self._rx_task_abort(ConnectionClosed())


    def _rx_task_abort(self, exc):
        # Propagate to what is in progress
        if self.active_future is not None:
            # Propagate the exception
            if not self.active_future.done():
                self.active_future.set_exception(exc)
            self.active_future = None
            self._rx_pending_segments.task_done()

        # Propagate the exception to everything pending
        self.log.info(f"_rx_task_abort({exc})")
        while not self._rx_pending_segments.empty():
            _num_blocks, result_future = self._rx_pending_segments.get_nowait()
            if not result_future.done():
                result_future.set_exception(exc)
            self._rx_pending_segments.task_done()

    async def _queue_recv(self, num_blocks):
        future = asyncio.get_running_loop().create_future()
        segment = (num_blocks, future)
        await self._rx_pending_segments.put(segment)
        return future

    async def _transfer_blocks_async(self, samples, purpose):
        # check if RX task is dead
        if self._rx_task_handle.done():
            raise self._rx_task_handle.exception() or ConnectionClosed("reader died")

        # queue RX
        assert len(samples) % BLOCK_NUM_SAMPLES == 0
        num_blocks = len(samples) // BLOCK_NUM_SAMPLES

        rx_result = await self._queue_recv(num_blocks)
        self._blocks_per_class[purpose] += num_blocks

        # perform TX in blocks
        #mv = memoryview(samples)
        #for p in range(0, len(mv), BLOCK_NUM_SAMPLES):
        #    block = mv[p:p + BLOCK_NUM_SAMPLES]
        start_dt = datetime.datetime.now()
        self._writer.write(samples)
        await self._writer.drain()
        end_dt = datetime.datetime.now()
        duration_secs = (end_dt - start_dt).total_seconds()
        self.log_operation(f"send {num_blocks} took {duration_secs:.3f}s")

        # give back result (unawaited!)
        return rx_result

    async def press(self, keys):
        self.log_operation(f"press({keys=})")
        samples = bytearray()
        for i, key in enumerate(keys):
            samples += tone_samples(key, DTMF_MIN_TONE_SAMPLES)
            # NB: If we put the silence after, we must make sure the last block is not only silence,
            # as this will trigger the next clip to run earlier than expected!
            # We align to block boundaries right now so it's fine.
            samples += SILENCE_LONG[:DTMF_MIN_SILENCE_SAMPLES]
        samples = pad_samples(samples)
        num_blocks = len(samples) // BLOCK_NUM_SAMPLES
        rx_future = await self._transfer_blocks_async(samples, "press")
        # XXX: Ignored response
        ignore_future(rx_future)

        self._just_skipped = False

        return num_blocks # for ringtone exploit

    async def enter_number(self, value):
        self.log_operation(f"enter_number({value=})")
        await self.press(str(value) + "#")

    async def cancel(self):
        await self.press("*")

    async def send_audio(self, samples):
        self.log_operation(f"send_audio({len(samples)=})")
        assert len(samples) % BLOCK_NUM_SAMPLES == 0
        rx_future = await self._transfer_blocks_async(samples, "send_audio")
        # XXX: Ignored response
        ignore_future(rx_future)

        self._just_skipped = False

    async def recv_audio(self, num_samples, defer=False):
        self.log_operation(f"recv_audio({num_samples=})")

        assert num_samples % BLOCK_NUM_SAMPLES == 0
        rx_future = await self._transfer_blocks_async(get_silence(num_samples), "recv_audio")
        self._num_rtts += 1

        if not defer:
            return await rx_future
        else:
            return rx_future

    async def recv_expect(self, name, defer=False):
        no_skip=False
        self.log_operation(f"recv_expect({name=}{", no_skip" if no_skip else ""})")

        # Did we just skip? Then the following line will also be skipped...
        if self._just_skipped:
            self.log_operation(f"recv_expect after skip -> skip next fully")
            return []

        # Check what we expect
        expected_samples = line_data(name)

        # Skip
        if not no_skip:
            expected_samples = expected_samples[:SKIP_NUM_SAMPLES]
            self._just_skipped = True

        num_samples = len(expected_samples)
        assert num_samples % BLOCK_NUM_SAMPLES == 0

        # Transmit silence
        rx_future = await self._transfer_blocks_async(get_silence(num_samples), "recv_expect" + ("_noskip" if no_skip else ""))
        self._num_rtts += 1

        async def check(rx_future):
            got_samples = await rx_future
            if got_samples != expected_samples:
                dump_len = 100
                self.log_dump(f"unexpected samples: got={got_samples[:dump_len].hex()}..., expected={expected_samples[:dump_len].hex()}...")
                raise UnexpectedResponseError(f"expected {name}, got something else")

        if not defer:
            await check(rx_future)
            return []
        else:
            return [AsyncCheck(rx_future, check)]

    async def recv_until_expect(self, name, max_until, defer=False):
        """
        This function is a bit inefficient, so do not use ideally. We need it for exploit because of unknown length.
        """
        self.log_operation(f"recv_until_expect({name=}, {max_until=})")

        # Check what we expect
        expected_samples = line_data(name)
        assert len(expected_samples) % BLOCK_NUM_SAMPLES == 0
        assert max_until % BLOCK_NUM_SAMPLES == 0

        no_skip = False
        if not no_skip:
            expected_samples = expected_samples[:SKIP_NUM_SAMPLES]
            self._just_skipped = True

        num_samples = max_until + len(expected_samples)
        rx_future = await self._transfer_blocks_async(get_silence(num_samples), "recv_until_expect")
        self._num_rtts += 1

        async def check(rx_future):
            got_samples = await rx_future

            # Find our clip
            expect_index = got_samples.find(expected_samples)
            if expect_index == -1:
                self.log_dump(f"unexpected samples: end of got=...{got_samples[-BLOCK_NUM_SAMPLES:].hex()}, expected={expected_samples.hex()}")
                raise UnexpectedResponseError(f"expected {name}, got something else")
            if expect_index % BLOCK_NUM_SAMPLES != 0:
                self.log_dump(f"misaligned, {expect_index=}")
                raise UnexpectedResponseError(f"misaligned response")

            # Cut out our data
            data = got_samples[:expect_index]

            return data

        if not defer:
            return await check(rx_future)
        else:
            ac = AsyncCheck(rx_future, check)
            return ac.resolve() # unawaited
