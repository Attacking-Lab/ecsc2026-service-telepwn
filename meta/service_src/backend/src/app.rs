use std::{
    fs::{self, File},
    io::{
        ErrorKind::{AlreadyExists, NotFound, Other},
        Write,
    },
    net::{Shutdown, SocketAddr, TcpStream},
    path::PathBuf,
    time::Duration,
};

use byteorder::{BigEndian, LittleEndian, ReadBytesExt, WriteBytesExt};
use const_format::concatcp;
use tracing::{info, warn};

use shared::dtmf::DtmfTone::*;
use shared::net_audio::*;

use crate::audio::*;

const NUM_PREVIEW_SAMPLES: usize = 3 * SAMPLE_RATE; // three second preview
const NUM_MIN_RINGTONE_SAMPLES: usize = 3 * SAMPLE_RATE;

enum ItemKind {
    Ringtone,
    Wallpaper,
}

pub fn init_data_dir() -> Result<(), std::io::Error> {
    shared::check_data_dir()?;

    // create items dir
    fs::create_dir_all(ITEMS_PATH)?;

    // new report dir
    fs::create_dir_all(shared::NEW_REPORTS_DIR)?;

    Ok(())
}

pub const BACKEND_DATA_DIR: &str = concatcp!(shared::DATA_DIR, "backend/");
pub const ITEMS_PATH: &str = concatcp!(BACKEND_DATA_DIR, "items/");

#[must_use]
fn item_dir_by_id(id: u64) -> PathBuf {
    const GROUP_SIZE: usize = 2;
    const MAX_GROUP_COUNT: usize = 8;

    let id_string = format!("{:016x}", id);
    let group_count = std::cmp::min((id_string.len() - 1) / GROUP_SIZE, MAX_GROUP_COUNT);
    let chunked_len = group_count * GROUP_SIZE;

    let mut container_path = PathBuf::from(ITEMS_PATH);
    for group in 0..group_count {
        let start = group * GROUP_SIZE;
        let end = start + GROUP_SIZE;
        container_path.push(&id_string[start..end]);
    }

    let mut remainder = String::from("item_");
    remainder += &id_string[chunked_len..];
    container_path.push(remainder);

    container_path
}

#[must_use]
fn item_pin_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("pin")
}
#[must_use]
fn item_tradeable_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("tradeable")
}
#[must_use]
fn item_requested_trade_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("requested_trade")
}
#[must_use]
fn item_ringtone_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("ringtone.pcm")
}
#[must_use]
fn item_wallpaper_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("wallpaper.pcm")
}
#[must_use]
fn item_wallpaper_preview_path_by_id(id: u64) -> PathBuf {
    item_dir_by_id(id).join("wallpaper_preview.pcm")
}

#[must_use]
fn item_accepted_trade_path_by_id(offered_item_id: u64, requested_item_id: u64) -> PathBuf {
    item_dir_by_id(offered_item_id).join(format!("accepted_trade_{}", requested_item_id))
}

#[must_use]
fn item_new_report_path_by_id(id: u64) -> PathBuf {
    PathBuf::from(shared::NEW_REPORTS_DIR).join(id.to_string())
}

fn get_item_kind(item_id: u64) -> Result<ItemKind, std::io::Error> {
    if fs::exists(item_ringtone_path_by_id(item_id))? {
        return Ok(ItemKind::Ringtone);
    }
    if fs::exists(item_wallpaper_path_by_id(item_id))? {
        return Ok(ItemKind::Wallpaper);
    }
    Err(std::io::Error::new(Other, "unable to determine item type"))
}

fn item_exists(item_id: u64) -> Result<bool, std::io::Error> {
    fs::exists(item_dir_by_id(item_id))
}

fn is_item_tradeable(item_id: u64) -> Result<bool, std::io::Error> {
    Ok(fs::exists(item_tradeable_path_by_id(item_id))?)
}

/// Get trade, if exists
fn get_item_requested_trade(item_id: u64) -> Result<Option<u64>, std::io::Error> {
    let mut trade_file = match open_read(&item_requested_trade_path_by_id(item_id)) {
        Err(e) if e.kind() == NotFound => {
            return Ok(None);
        },
        r => r?,
    };
    let requested_id = trade_file.read_u64::<LittleEndian>()?;

    Ok(Some(requested_id))
}

/// Set trade or overwrite
fn set_item_requested_trade(item_id: u64, requested_id: Option<u64>) -> Result<(), std::io::Error> {
    if let Some(requested_id) = requested_id {
        let mut trade_file = File::create(item_requested_trade_path_by_id(item_id))?;
        trade_file.write_u64::<LittleEndian>(requested_id)?;
    } else {
        match fs::remove_file(item_requested_trade_path_by_id(item_id)) {
            Err(e) if e.kind() == NotFound => {
                return Ok(());
            },
            r => r?,
        };
    }

    Ok(())
}

fn add_accepted_trade(offered_item_id: u64, requested_item_id: u64) -> Result<(), std::io::Error> {
    let _ = match File::create_new(item_accepted_trade_path_by_id(
        offered_item_id,
        requested_item_id,
    )) {
        Err(e) if e.kind() == AlreadyExists => {
            return Ok(());
        },
        r => r?,
    };

    Ok(())
}

fn has_accepted_trade(
    offered_item_id: u64,
    requested_item_id: u64,
) -> Result<bool, std::io::Error> {
    Ok(fs::exists(item_accepted_trade_path_by_id(offered_item_id, requested_item_id))?)
}

fn check_item_pin(ctx: &mut AudioContext, item_id: u64) -> Result<bool, std::io::Error> {
    // PIN to confirm
    ctx.send_clip_until_key("enter_item_pin_or_cancel")?;
    let Some(got_pin) = ctx.recv_number_or_cancel()? else { return Ok(false) };

    // Read expected PIN
    let mut pin_file = match open_read(&item_pin_path_by_id(item_id)) {
        Err(e) if e.kind() == NotFound => {
            ctx.send_clip_until_key("item_doesnt_exist")?;
            return Ok(false);
        },
        r => r?,
    };
    let expected_pin = pin_file.read_u64::<LittleEndian>()?;

    // Check PIN
    if got_pin != expected_pin {
        // Bad PIN
        ctx.send_clip_until_key("incorrect_pin")?;
        return Ok(false);
    }

    Ok(true)
}

fn authenticate_item(ctx: &mut AudioContext) -> Result<Option<u64>, std::io::Error> {
    // Get ID
    ctx.send_clip_until_key("enter_item_id_or_cancel")?;
    let Some(id) = ctx.recv_number_or_cancel()? else { return Ok(None) };

    // Check PIN
    if !check_item_pin(ctx, id)? {
        return Ok(None);
    }

    Ok(Some(id))
}

fn send_item_preview(ctx: &mut AudioContext, item_id: u64) -> Result<(), std::io::Error> {
    match get_item_kind(item_id)? {
        ItemKind::Ringtone => {
            // Preview it for three seconds
            ctx.load_buffer_from_path(&item_ringtone_path_by_id(item_id))?;
            ctx.send_buffer_with_limit(NUM_PREVIEW_SAMPLES)?;
        },
        ItemKind::Wallpaper => {
            // Load and display wallpaper preview
            ctx.send_dtmf_key(Special(3))?; // start preview fax
            ctx.load_buffer_from_path(&item_wallpaper_preview_path_by_id(item_id))?;
            ctx.send_buffer()?;
            ctx.send_dtmf_key(Pound)?;
        },
    }

    Ok(())
}

fn send_item_full(ctx: &mut AudioContext, item_id: u64) -> Result<(), std::io::Error> {
    // Give full sound
    match get_item_kind(item_id)? {
        ItemKind::Ringtone => {
            // TODO: more fluff around this, make this "set as ringtone" maybe?
            ctx.send_dtmf_key(Special(1))?;
            ctx.load_buffer_from_path(&item_ringtone_path_by_id(item_id))?;
            ctx.send_buffer()?;
            ctx.send_dtmf_key(Pound)?;
        },
        ItemKind::Wallpaper => {
            // Fax start marker
            ctx.send_dtmf_key(Special(2))?;
            ctx.load_buffer_from_path(&item_wallpaper_path_by_id(item_id))?;
            ctx.send_buffer()?;
            ctx.send_dtmf_key(Pound)?;
        },
    }

    Ok(())
}

fn unlock_item(ctx: &mut AudioContext, item_id: u64) -> Result<(), std::io::Error> {
    // Check PIN
    if !check_item_pin(ctx, item_id)? {
        return Ok(());
    }

    send_item_full(ctx, item_id)?;

    Ok(())
}

fn report_item(ctx: &mut AudioContext, item_id: u64) -> Result<(), std::io::Error> {
    // create connection to the frontend
    let own_team_id =
        std::env::var("TEAM_ID").ok().and_then(|s| s.parse::<i32>().ok()).unwrap_or(0);
    let forward_addr = match format!("10.60.{}.4:{}", own_team_id, shared::FRONTEND_REPORT_PORT)
        .parse::<SocketAddr>()
    {
        Ok(a) => a,
        Err(_) => return Err(std::io::Error::new(Other, "unable to create forward address")),
    };

    let timeout = Duration::from_secs(5);
    let mut report_stream = TcpStream::connect_timeout(&forward_addr, timeout)?;

    report_stream.set_read_timeout(Some(timeout))?;
    report_stream.set_write_timeout(Some(timeout))?;

    report_stream.write_u64::<BigEndian>(item_id)?;

    report_stream.flush()?;
    report_stream.shutdown(Shutdown::Write)?;

    ctx.send_clip_until_key("report_succeeded")?;

    Ok(())
}

fn view_item(ctx: &mut AudioContext, item_id: u64) -> Result<(), std::io::Error> {
    if !item_exists(item_id)? {
        ctx.send_clip_until_key("item_doesnt_exist")?;
        return Ok(());
    }

    'preview: loop {
        // Preview ringtone
        send_item_preview(ctx, item_id)?;

        // Get key
        loop {
            ctx.send_clip_until_key("repeat_preview_or_unlock_full_or_offer_trade_or_cancel")?;
            match ctx.recv_dtmf_key()? {
                Num(1) => continue 'preview,
                Num(2) => unlock_item(ctx, item_id)?,
                Num(3) => break 'preview,
                Num(4) => report_item(ctx, item_id)?,
                Asterisk => return Ok(()),
                _ => {
                    ctx.send_clip_until_key("invalid_input")?;
                },
            }
        }
    }

    // Offer trade

    // Check requested item is tradeable
    if !is_item_tradeable(item_id)? {
        ctx.send_clip_until_key("item_is_not_tradeable")?;
        return Ok(());
    }

    // Get an item to trade
    ctx.send_clip_until_key("select_trade_item")?;
    let Some(offered_item_id) = authenticate_item(ctx)? else { return Ok(()) };

    // Check offered item is tradeable
    if !is_item_tradeable(offered_item_id)? {
        ctx.send_clip_until_key("item_is_not_tradeable")?;
        return Ok(());
    }

    // Check it's not our own item
    if offered_item_id == item_id {
        // TODO special msg maybe
        ctx.send_clip_until_key("item_is_not_tradeable")?;
        return Ok(());
    }

    // Set trade
    set_item_requested_trade(offered_item_id, Some(item_id))?;
    info!(offered_item_id, requested_item_id = item_id, "offered trade");

    // Confirm
    ctx.send_clip_until_key("trade_offered")?;

    Ok(())
}

pub fn browse_items(ctx: &mut AudioContext) -> Result<(), std::io::Error> {
    // Please select item
    // TODO: Searching by name/category?
    loop {
        ctx.send_clip_until_key("enter_item_id_or_cancel")?;
        let Some(item_id) = ctx.recv_number_or_cancel()? else { break };
        view_item(ctx, item_id)?;
    }

    Ok(())
}

pub fn create_item(ctx: &mut AudioContext) -> Result<(), std::io::Error> {
    // Enter ID
    ctx.send_clip_until_key("enter_item_id_or_cancel")?;
    let Some(id) = ctx.recv_number_or_cancel()? else { return Ok(()) };

    // Enter PIN
    ctx.send_clip_until_key("enter_item_pin_or_cancel")?;
    let Some(pin) = ctx.recv_number_or_cancel()? else { return Ok(()) };

    // Which type to create?
    ctx.send_clip_until_key("ringtone_or_wallpaper_or_cancel")?;
    let kind = loop {
        match ctx.recv_dtmf_key()? {
            Num(1) => break ItemKind::Ringtone,
            Num(2) => break ItemKind::Wallpaper,
            Asterisk => return Ok(()),
            _ => {
                ctx.send_clip_until_key("invalid_input")?;
            },
        }
    };

    match kind {
        ItemKind::Ringtone => {
            // Enter audio
            ctx.send_clip_until_key("record_ringtone")?;
            ctx.send_clip_until_key("start_stop_recording")?;
            // Wait for start key
            loop {
                match ctx.recv_dtmf_key()? {
                    Pound => break,            // fax start
                    Asterisk => return Ok(()), // cancel
                    _ => {},
                }
            }

            ctx.recv_buffer_until_key()?;

            // Is it long enough?
            if ctx.buffer_used() < NUM_MIN_RINGTONE_SAMPLES {
                ctx.send_clip_until_key("ringtone_too_short")?;
                return Ok(());
            }
        },
        ItemKind::Wallpaper => {
            // Send wallpaper now
            ctx.send_clip_until_key("send_wallpaper_or_cancel")?;

            // Wait for start key
            loop {
                match ctx.recv_dtmf_key()? {
                    Special(2) => break,       // fax start
                    Asterisk => return Ok(()), // cancel
                    _ => {},
                }
            }
            ctx.recv_buffer_until_key()?;
        },
    };

    // Create parent folder. For this part it's okay if it already exists.
    let item_path = item_dir_by_id(id);
    if let Some(parent) = item_path.parent() {
        fs::create_dir_all(parent)?;
    } else {
        return Err(std::io::Error::new(Other, "item has no parent dir"));
    }

    // Create folder. This "allocates" the item and guards against races.
    match fs::create_dir(item_path) {
        Err(e) if e.kind() == AlreadyExists => {
            ctx.send_clip_until_key("item_already_exists")?;
            return Ok(());
        },
        r => r?,
    }

    match kind {
        ItemKind::Ringtone => {
            // Now save the ringtone itself.
            ctx.save_buffer_to_path(&item_ringtone_path_by_id(id))?;
        },
        ItemKind::Wallpaper => {
            // Save the wallpaper itself
            ctx.save_buffer_to_path(&item_wallpaper_path_by_id(id))?;

            // Generate preview
            ctx.downsample_fax_buffer()?;
            ctx.save_buffer_to_path(&item_wallpaper_preview_path_by_id(id))?;

            // Mark as tradeable
            // XXX: make this a separate question?
            // but this is at least helpful so its not checker dependent
            File::create_new(item_tradeable_path_by_id(id))?;
        },
    }

    // Lastly, write PIN
    let mut pin_file = File::create_new(item_pin_path_by_id(id))?;
    pin_file.write_u64::<LittleEndian>(pin)?;

    info!(id, "created item");
    ctx.send_clip_until_key("thanks")?;

    Ok(())
}

fn view_offered_trade(ctx: &mut AudioContext, offered_item_id: u64) -> Result<(), std::io::Error> {
    // View the trade
    'preview: loop {
        // Preview it
        send_item_preview(ctx, offered_item_id)?;

        // Get selection
        loop {
            ctx.send_clip_until_key("repeat_preview_or_accept_trade_or_cancel")?;
            match ctx.recv_dtmf_key()? {
                Num(1) => continue 'preview,
                Num(2) => break 'preview,
                Asterisk => return Ok(()),
                _ => {
                    ctx.send_clip_until_key("invalid_input")?;
                },
            }
        }
    }

    // Accept the trade.
    // Get offered ID
    let Some(requested_item_id) = get_item_requested_trade(offered_item_id)? else {
        ctx.send_clip_until_key("offered_trade_doesnt_exist")?;
        return Ok(());
    };

    // Resolve in both directions
    add_accepted_trade(offered_item_id, requested_item_id)?;
    add_accepted_trade(requested_item_id, offered_item_id)?;

    // Remove trade offer
    set_item_requested_trade(offered_item_id, None)?;

    info!(offered_item_id, requested_item_id, "accepted trade");
    ctx.send_clip_until_key("trade_accepted")?;

    Ok(())
}

fn view_offered_trades(ctx: &mut AudioContext) -> Result<(), std::io::Error> {
    let Some(requested_item_id) = authenticate_item(ctx)? else { return Ok(()) };

    // Is this tradeable?
    if !is_item_tradeable(requested_item_id)? {
        ctx.send_clip_until_key("item_is_not_tradeable")?;
        return Ok(());
    }

    loop {
        // Get ID of offered thing
        // TODO: browse all
        ctx.send_clip_until_key("enter_offered_item_id_or_cancel")?;
        let Some(offered_item_id) = ctx.recv_number_or_cancel()? else { return Ok(()) };

        // XXX: Could consider checking existence here...

        // Is this tradeable?
        if !is_item_tradeable(offered_item_id)? {
            ctx.send_clip_until_key("item_is_not_tradeable")?;
            continue;
        }

        // Check if there is a trade offer
        let actual_requested_trade = get_item_requested_trade(offered_item_id)?;
        if actual_requested_trade != Some(requested_item_id) {
            // Not trading for our item
            ctx.send_clip_until_key("offered_trade_doesnt_exist")?;
            continue;
        }

        // Preview the trade offer
        view_offered_trade(ctx, offered_item_id)?;
    }
}

fn view_accepted_trades(ctx: &mut AudioContext) -> Result<(), std::io::Error> {
    let Some(offered_item_id) = authenticate_item(ctx)? else { return Ok(()) };

    // Is this tradeable?
    if !is_item_tradeable(offered_item_id)? {
        ctx.send_clip_until_key("item_is_not_tradeable")?;
        return Ok(());
    }

    loop {
        // Get ID of requested thing
        // TODO: browse all
        ctx.send_clip_until_key("enter_requested_item_id_or_cancel")?;
        let Some(requested_item_id) = ctx.recv_number_or_cancel()? else { return Ok(()) };

        // Is this tradeable?
        if !is_item_tradeable(requested_item_id)? {
            ctx.send_clip_until_key("item_is_not_tradeable")?;
            continue;
        }

        // Check if there is an accepted trade
        if !has_accepted_trade(offered_item_id, requested_item_id)? {
            ctx.send_clip_until_key("accepted_trade_doesnt_exist")?;
            continue;
        }

        // View the full item
        send_item_full(ctx, requested_item_id)?;
    }
}

pub enum InteractResult {
    Done,
    RequestSupport,
}

pub fn interact(ctx: &mut AudioContext) -> Result<InteractResult, std::io::Error> {
    ctx.send_clip_until_key("welcome")?;

    loop {
        ctx.send_clip_until_key(
            "browse_items_or_create_item_or_view_offered_trades_or_view_accepted_trades_or_request_support",
        )?;
        match ctx.recv_dtmf_key()? {
            Num(1) => browse_items(ctx)?,
            Num(2) => create_item(ctx)?,
            Num(3) => view_offered_trades(ctx)?,
            Num(4) => view_accepted_trades(ctx)?,
            Num(5) => {
                // Customer support
                ctx.send_clip_until_key("please_hold")?;
                info!("request support");
                return Ok(InteractResult::RequestSupport);
            },
            Asterisk => {
                // Quit
                break;
            },
            _ => {
                ctx.send_clip_until_key("invalid_input")?;
            },
        }
    }

    // Goodbye
    ctx.send_clip_until_key("goodbye")?;
    Ok(InteractResult::Done)
}
