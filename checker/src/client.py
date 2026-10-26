from text_image import FAX_FULL_NUM_SAMPLES, FAX_PREVIEW_NUM_SAMPLES
from phone import BLOCK_NUM_SAMPLES, SAMPLE_RATE, pad_samples

MAX_RECORD_SECONDS = 15
MAX_RECORD_NUM_SAMPLES = MAX_RECORD_SECONDS * SAMPLE_RATE

MIN_RINGTONE_SECONDS = 3
MIN_RINGTONE_NUM_SAMPLES = MIN_RINGTONE_SECONDS * SAMPLE_RATE
RINGTONE_PREVIEW_NUM_SAMPLES = 3 * SAMPLE_RATE

DEFERRED = False

async def resolve_deferred_checks(deferred_checks):
    for c in deferred_checks:
        await c.resolve()

async def start(phone):
    await phone.recv_expect("welcome")

async def hangup(phone):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.cancel()
    deferred_checks += await phone.recv_expect("goodbye", defer=DEFERRED)

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)

async def authenticate(phone, item_id, pin, defer=False):
    deferred_checks = []

    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=defer)
    await phone.enter_number(item_id)
    deferred_checks += await phone.recv_expect("enter_item_pin_or_cancel", defer=defer)
    await phone.enter_number(pin)

    return deferred_checks

async def record_audio(phone, samples, press_pound_if_early=True):
    # Pad to block boundary
    samples = pad_samples(samples)

    # Check that it fits
    if len(samples) > MAX_RECORD_NUM_SAMPLES:
        raise ValueError(f"the phone records at most {MAX_RECORD_NUM_SAMPLES} blocks")

    # Record
    await phone.send_audio(samples)

    if press_pound_if_early and len(samples) < MAX_RECORD_NUM_SAMPLES:
        # Press # if necessary
        await phone.press("#")

async def create_ringtone(phone, item_id, pin, samples):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("2")
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(item_id)
    deferred_checks += await phone.recv_expect("enter_item_pin_or_cancel", defer=DEFERRED)
    await phone.enter_number(pin)

    await phone.press("1") # ringtone
    deferred_checks += await phone.recv_expect("record_ringtone", defer=DEFERRED)
    deferred_checks += await phone.recv_expect("start_stop_recording", defer=DEFERRED)
    await phone.press("#") # start recording
    await record_audio(phone, samples)

    deferred_checks += await phone.recv_expect("thanks", defer=DEFERRED)

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)

async def create_wallpaper(phone, item_id, pin, samples):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("2")
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(item_id)
    deferred_checks += await phone.recv_expect("enter_item_pin_or_cancel", defer=DEFERRED)
    await phone.enter_number(pin)

    await phone.press("2") # wallpaper

    deferred_checks += await phone.recv_expect("send_wallpaper_or_cancel", defer=DEFERRED)
    await record_audio(phone, samples, press_pound_if_early=False) # pound included in fax

    deferred_checks += await phone.recv_expect("thanks", defer=DEFERRED)

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)

async def get_ringtone_preview(phone, ringtone_id):
    return await get_item_preview(phone, ringtone_id, RINGTONE_PREVIEW_NUM_SAMPLES)

async def get_ringtone_full(phone, ringtone_id, pin, full_num_samples):
    if full_num_samples is not None:
        full_num_samples += 2 * BLOCK_NUM_SAMPLES # account for the start/end tones
    preview, full = await get_item_full(phone, ringtone_id, pin, RINGTONE_PREVIEW_NUM_SAMPLES, full_num_samples)
    full_trimmed = full[BLOCK_NUM_SAMPLES:-BLOCK_NUM_SAMPLES] # remove helper tones
    return preview, full_trimmed

async def get_wallpaper_preview(phone, wallpaper_id):
    return await get_item_preview(phone, wallpaper_id, FAX_PREVIEW_NUM_SAMPLES)

async def get_wallpaper_full(phone, wallpaper_id, pin):
    return await get_item_full(phone, wallpaper_id, pin, FAX_PREVIEW_NUM_SAMPLES, FAX_FULL_NUM_SAMPLES)

async def get_item_preview(phone, item_id, preview_num_samples):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("1")
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(item_id)

    preview = await phone.recv_audio(preview_num_samples, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("repeat_preview_or_unlock_full_or_offer_trade_or_cancel", defer=DEFERRED)

    await phone.cancel()
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.cancel()

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)
        preview = await preview

    return preview

async def get_item_full(phone, item_id, pin, preview_num_samples, full_num_samples):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("1")
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(item_id)

    preview = await phone.recv_audio(preview_num_samples, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("repeat_preview_or_unlock_full_or_offer_trade_or_cancel", defer=DEFERRED)

    await phone.press("2")
    deferred_checks += await phone.recv_expect("enter_item_pin_or_cancel", defer=DEFERRED)
    await phone.enter_number(pin)

    if full_num_samples is None:
        # We don't know how long to go, so go to max. This should only be used in exploit.
        full = await phone.recv_until_expect(
            "repeat_preview_or_unlock_full_or_offer_trade_or_cancel",
            2 * BLOCK_NUM_SAMPLES + MAX_RECORD_NUM_SAMPLES, # include start/stop tone
            defer=DEFERRED
        )
    else:
        full = await phone.recv_audio(full_num_samples, defer=DEFERRED)
        deferred_checks += await phone.recv_expect("repeat_preview_or_unlock_full_or_offer_trade_or_cancel", defer=DEFERRED)

    await phone.cancel()
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.cancel()

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)
        preview = await preview
        full = await full

    return preview, full


async def offer_trade(phone, requested_id, offered_id, offered_pin, preview_num_samples=FAX_PREVIEW_NUM_SAMPLES):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("1")
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(requested_id)

    preview = await phone.recv_audio(preview_num_samples, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("repeat_preview_or_unlock_full_or_offer_trade_or_cancel", defer=DEFERRED)

    await phone.press("3")
    deferred_checks += await phone.recv_expect("select_trade_item", defer=DEFERRED)
    deferred_checks += await authenticate(phone, offered_id, offered_pin, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("trade_offered", defer=DEFERRED)
    deferred_checks += await phone.recv_expect("enter_item_id_or_cancel", defer=DEFERRED)
    await phone.cancel()

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)
        preview = await preview

    return preview


async def view_offered_trade(phone, requested_id, pin, offered_id, preview_num_samples=FAX_PREVIEW_NUM_SAMPLES, accept=True):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("3")
    deferred_checks += await authenticate(phone, requested_id, pin, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("enter_offered_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(offered_id)

    preview = await phone.recv_audio(preview_num_samples, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("repeat_preview_or_accept_trade_or_cancel", defer=DEFERRED)

    if accept:
        await phone.press("2")
        deferred_checks += await phone.recv_expect("trade_accepted", defer=DEFERRED)
    else:
        await phone.cancel()
    deferred_checks += await phone.recv_expect("enter_offered_item_id_or_cancel", defer=DEFERRED)
    await phone.cancel()

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)
        preview = await preview

    return preview


async def view_accepted_trade(phone, offered_id, pin, requested_id, full_num_samples=FAX_FULL_NUM_SAMPLES):
    deferred_checks = []

    deferred_checks += await phone.recv_expect(
        "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        defer=DEFERRED
    )
    await phone.press("4")
    deferred_checks += await authenticate(phone, offered_id, pin, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("enter_requested_item_id_or_cancel", defer=DEFERRED)
    await phone.enter_number(requested_id)

    full = await phone.recv_audio(full_num_samples, defer=DEFERRED)
    deferred_checks += await phone.recv_expect("enter_requested_item_id_or_cancel", defer=DEFERRED)

    await phone.cancel()

    if DEFERRED:
        await resolve_deferred_checks(deferred_checks)
        full = await full

    return full
