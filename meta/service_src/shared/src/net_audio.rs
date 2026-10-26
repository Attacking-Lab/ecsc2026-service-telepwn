use std::io::ErrorKind::{Interrupted, TimedOut, UnexpectedEof, WouldBlock};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::os::fd::AsRawFd;
use std::slice;
use std::time::{Duration, Instant};

pub const BLOCK_NUM_SAMPLES: usize = 1000;
pub const SAMPLE_RATE: usize = 8000;
pub type Sample = i8;

pub fn samples_as_raw(buffer: &[Sample]) -> &[u8] {
    unsafe {
        slice::from_raw_parts(
            buffer.as_ptr() as *const u8, //.
            std::mem::size_of_val(buffer),
        )
    }
}
pub fn samples_as_raw_mut(buffer: &mut [Sample]) -> &mut [u8] {
    unsafe {
        slice::from_raw_parts_mut(
            buffer.as_mut_ptr() as *mut u8, //.
            std::mem::size_of_val(buffer),
        )
    }
}

pub const IO_BATCH_BLOCKS_DEFAULT: usize = 4;
pub const IO_BATCH_BLOCKS_MAX: usize = 16;

fn io_batch_blocks() -> usize {
    let default = IO_BATCH_BLOCKS_DEFAULT;
    match std::env::var("TELEPWN_IO_BLOCKS") {
        Ok(value) => value
            .parse::<usize>()
            .unwrap_or(default)
            .clamp(1, IO_BATCH_BLOCKS_MAX),
        Err(_) => default,
    }
}

pub struct SyncAudioStream {
    stream: TcpStream,

    inbuf: Box<[u8]>,
    in_start: usize,
    in_end: usize,

    outbuf: Box<[u8]>,
    out_len: usize,
}

impl SyncAudioStream {
    pub fn new(stream: TcpStream, timeout: Option<Duration>) -> Result<Self, std::io::Error> {
        // Set TCP_NODELAY
        stream.set_nodelay(true)?;

        // Timeouts
        stream.set_read_timeout(timeout)?;
        stream.set_write_timeout(timeout)?;

        let batch_bytes = io_batch_blocks() * BLOCK_NUM_SAMPLES;

        Ok(Self {
            stream,
            inbuf: vec![0u8; batch_bytes].into_boxed_slice(),
            in_start: 0,
            in_end: 0,
            outbuf: vec![0u8; batch_bytes].into_boxed_slice(),
            out_len: 0,
        })
    }

    pub fn close(mut self) -> TcpStream {
        let _ = self.flush();
        self.stream
    }

    fn buffered_input(&self) -> usize {
        self.in_end - self.in_start
    }

    pub fn flush(&mut self) -> Result<(), std::io::Error> {
        let mut p = 0;
        while p < self.out_len {
            match self.stream.write(&self.outbuf[p..self.out_len]) {
                Ok(0) => {
                    return Err(std::io::Error::new(
                        UnexpectedEof,
                        "stream closed gracefully",
                    ));
                }
                Ok(written) => p += written,
                Err(e) if e.kind() == Interrupted => continue,
                Err(e) => return Err(e),
            }
        }

        self.out_len = 0;
        Ok(())
    }

    fn fill_input(&mut self) -> Result<(), std::io::Error> {
        while self.buffered_input() < BLOCK_NUM_SAMPLES {
            // Flush before blocking: a lock-step peer may be waiting for it.
            self.flush()?;

            if self.in_start == self.in_end {
                self.in_start = 0;
                self.in_end = 0;
            } else if self.inbuf.len() - self.in_end < BLOCK_NUM_SAMPLES {
                self.inbuf.copy_within(self.in_start..self.in_end, 0);
                self.in_end -= self.in_start;
                self.in_start = 0;
            }

            let got = self.stream.read(&mut self.inbuf[self.in_end..])?;
            if got == 0 {
                return Err(std::io::Error::new(
                    UnexpectedEof,
                    "stream closed gracefully",
                ));
            }
            self.in_end += got;
        }

        Ok(())
    }

    /// Receive one block and reply with one block, in sync. Use this for the
    /// server side where we require determinism.
    pub fn transfer_block(
        &mut self,
        out_block: &[Sample; BLOCK_NUM_SAMPLES],
        in_block: &mut [Sample; BLOCK_NUM_SAMPLES],
    ) -> Result<(), std::io::Error> {
        self.fill_input()?;
        samples_as_raw_mut(in_block)
            .copy_from_slice(&self.inbuf[self.in_start..self.in_start + BLOCK_NUM_SAMPLES]);
        self.in_start += BLOCK_NUM_SAMPLES;

        if self.out_len == self.outbuf.len() {
            self.flush()?;
        }
        self.outbuf[self.out_len..self.out_len + BLOCK_NUM_SAMPLES]
            .copy_from_slice(samples_as_raw(out_block));
        self.out_len += BLOCK_NUM_SAMPLES;
        if self.out_len == self.outbuf.len() {
            self.flush()?;
        }

        Ok(())
    }
}

/// Use this for streams connected to actual audio where there is not a perfect
/// lockstep synchronization between send and receive side.
pub struct AsyncAudioStream {
    stream: TcpStream,
    rx_block: [Sample; BLOCK_NUM_SAMPLES],
    rx_len_left: usize,
    tx_block: [Sample; BLOCK_NUM_SAMPLES],
    tx_len_left: usize,

    timeout: Option<Duration>,
    last_rx_time: Instant,
}

impl AsyncAudioStream {
    pub fn new(stream: TcpStream, timeout: Option<Duration>) -> Result<Self, std::io::Error> {
        // Set TCP_NODELAY
        stream.set_nodelay(true)?;

        // Set non-blocking
        stream.set_nonblocking(true)?;

        Ok(Self {
            stream,
            rx_block: [0; BLOCK_NUM_SAMPLES],
            rx_len_left: BLOCK_NUM_SAMPLES,
            tx_block: [0; BLOCK_NUM_SAMPLES],
            tx_len_left: 0,

            timeout,
            last_rx_time: Instant::now(),
        })
    }

    pub fn close(self) -> TcpStream {
        self.stream
    }

    pub fn is_stream_dead(&self) -> bool {
        is_stream_dead(&self.stream)
    }

    fn try_flush_block(&mut self) -> Result<(), std::io::Error> {
        if self.tx_len_left == 0 {
            return Ok(());
        }

        let offset = BLOCK_NUM_SAMPLES - self.tx_len_left;
        let got = match self.stream.write(samples_as_raw(&self.tx_block[offset..])) {
            Err(ref e) if e.kind() == WouldBlock => 0,
            Ok(0) => {
                return Err(std::io::Error::new(
                    UnexpectedEof,
                    "stream closed gracefully",
                ));
            }
            r => r?,
        };
        assert!(got <= self.tx_len_left);
        self.tx_len_left -= got;

        Ok(())
    }

    /// Only use this in non-deterministic outbound context!
    pub fn send_block_async(
        &mut self,
        out_block: &[Sample; BLOCK_NUM_SAMPLES],
    ) -> Result<bool, std::io::Error> {
        // Try to flush anything old left in there
        // XXX: Do we want to be doing this or should
        // we only do this in case of a partial send?
        self.try_flush_block()?;

        // Space for a new block?
        if self.tx_len_left == 0 {
            self.tx_block = *out_block;
            self.tx_len_left = BLOCK_NUM_SAMPLES;

            // Try to flush immediately
            self.try_flush_block()?;

            // Block was accepted
            Ok(true)
        } else {
            // Block was not accepted
            Ok(false)
        }
    }

    /// Only use this in non-deterministic outbound context!
    pub fn recv_block_async(
        &mut self,
        in_block: &mut [Sample; BLOCK_NUM_SAMPLES],
    ) -> Result<bool, std::io::Error> {
        // Try to get data
        let offset = BLOCK_NUM_SAMPLES - self.rx_len_left;
        let got = match self
            .stream
            .read(samples_as_raw_mut(&mut self.rx_block[offset..]))
        {
            Err(ref e) if e.kind() == WouldBlock => 0,
            Ok(0) => {
                return Err(std::io::Error::new(
                    UnexpectedEof,
                    "stream closed gracefully",
                ));
            }
            r => r?,
        };
        assert!(got <= self.rx_len_left);
        self.rx_len_left -= got;

        // Did we get nothing and has it been a while since we did?
        if let Some(timeout) = self.timeout {
            let now = Instant::now();
            if got == 0 {
                if now - self.last_rx_time > timeout {
                    return Err(std::io::Error::new(TimedOut, "rx timeout expired"));
                }
            } else {
                self.last_rx_time = now;
            }
        }

        // Do we have a full block?
        if self.rx_len_left == 0 {
            *in_block = self.rx_block;
            self.rx_len_left = BLOCK_NUM_SAMPLES;
            Ok(true)
        } else {
            Ok(false)
        }
    }
}

/// Check if a TCP stream is alive or if the other side has terminated the connection already
/// NB: **Only use this with non-blocking streams!**
pub fn is_stream_dead(stream: &TcpStream) -> bool {
    cfg_select! {
        target_os = "linux" => {
            // TODO: Consider using nix crate
            let fd = stream.as_raw_fd();
            let mut pollfd = libc::pollfd {
                fd,
                events: libc::POLLRDHUP,
                revents: 0,
            };

            let r = unsafe { libc::poll(&mut pollfd, 1, 0) };

            r > 0 && (pollfd.revents & libc::POLLRDHUP) != 0
        }
        not(target_os = "linux") => {
            // Peek a byte and see if that says the stream is closed
            let mut buf = [0u8; 1];
            match stream.peek(&mut buf) {
                Err(ref e) if e.kind() == WouldBlock => false,
                Err(_) => true,

                Ok(0) => true,
                Ok(_) => false,
            }
        }
    }
}
