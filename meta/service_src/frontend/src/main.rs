mod audio;
mod call_manager;
mod config;
mod display;
mod fax;
mod input;
mod live_call;
mod paths;
mod platform;
mod report_accepter;
mod ringtone_recorder;
mod ui;
mod util;

use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread::sleep;
use std::time::{Duration, Instant};

use shared::net_audio::Sample;
use tracing::info;
use tracing::level_filters::LevelFilter;
use tracing_subscriber::fmt::format::FmtSpan;
use tracing_subscriber::EnvFilter;

use anyhow::{Error, Result};

use crate::call_manager::*;
use crate::config::Config;
use crate::display::Display;
use crate::input::Input;
use crate::platform::led::LedManager;
use crate::ui::Ui;

fn init_logging() {
    let filter = EnvFilter::builder() //.
        .with_default_directive(LevelFilter::INFO.into())
        .from_env_lossy();
    tracing_subscriber::fmt() //.
        .with_env_filter(filter)
        .with_span_events(FmtSpan::ACTIVE)
        .init();
}

fn copy_default_if_not_exists(path: &Path, default_path: &Path) -> Result<(), std::io::Error> {
    if !std::fs::exists(path)? {
        info!(?path, ?default_path, "doesn't exist, creating from default");
        std::fs::copy(default_path, path)?;
    }
    Ok(())
}

fn main() -> Result<(), Error> {
    // Initialize logging
    init_logging();

    // Drivers
    cfg_select! {
        target_arch = "arm" => {
            let mut display = platform::FbdevDisplay::new()?;
            let mut input = platform::EvdevInput::new()?;
            let handset_audio_in = platform::AlsaAudioCaptureChannel::new("handset", 5)?;
            let handset_audio_out = platform::AlsaAudioPlayChannel::new("handset", 5)?;
            let speaker_audio_in = platform::AlsaAudioCaptureChannel::new("speakerphone", 8)?;
            let speaker_audio_out = platform::AlsaAudioPlayChannel::new("speakerphone", 6)?;
        },
        target_arch = "x86_64" => {
            let platform = platform::DesktopPlatform::new()?;
            let (mut display, mut input) = platform.systems();
        },
    }

    // Components
    let config = Config::load()?;

    // Init data
    shared::check_data_dir()?;

    // Report dirs
    std::fs::create_dir_all(shared::NEW_REPORTS_DIR)?;
    std::fs::create_dir_all(shared::OLD_REPORTS_DIR)?;

    // Init default wallpaper from flag
    std::fs::create_dir_all(paths::WALLPAPERS_DIR)?;
    copy_default_if_not_exists(
        Path::new(paths::ACTIVE_WALLPAPER_PATH),
        Path::new(&config.team_infos[config.own_team_index].default_wallpaper_path),
    )?;

    // Init default ringtone from anthem
    std::fs::create_dir_all(paths::RINGTONES_DIR)?;
    copy_default_if_not_exists(
        Path::new(paths::ACTIVE_RINGTONE_PATH),
        Path::new(&config.team_infos[config.own_team_index].default_ringtone_path),
    )?;

    // Only use anthem as hold music to avoid leaking flags or something
    let anthem_samples =
        util::load_audio(&config.team_infos[config.own_team_index].default_ringtone_path)?;

    // Start report accepter
    report_accepter::start();

    // Configure call manager
    let call_manager = Arc::new(Mutex::new(CallManager::new(
        AudioChannelDefinition {
            audio_in: Box::new(handset_audio_in),
            audio_out: Box::new(handset_audio_out),
        },
        AudioChannelDefinition {
            audio_in: Box::new(speaker_audio_in),
            audio_out: Box::new(speaker_audio_out),
        },
        anthem_samples,
    )?));

    // Load ringtone
    call_manager.lock().unwrap().reload_ringtone()?;

    // Start audio thread
    let mut call_manager_thread = CallManagerThread::new(call_manager.clone())?;
    call_manager_thread.start()?;

    let mut ui = Ui::new(&config)?;
    let mut led = LedManager::new()?;

    // Main loop
    const TARGET_UPDATE_INTERVAL: f32 = 1. / 15.;
    const MIN_REPAINT_INTERVAL: f32 = 1. / 2.;
    let mut first = true;
    let mut last_repaint_time = Instant::now();
    info!("ready");
    loop {
        let start = Instant::now();

        // Initial repainting
        if first {
            ui.update(&mut display, None, &call_manager, &mut led)?;
            display.flush()?;
        }
        first = false;

        // Update UI
        let input_keys = input.poll_events()?;
        let mut did_repaint = false;
        if input_keys.is_empty() {
            // No input. Time since last minimum repaint expired?
            if start - last_repaint_time > Duration::from_secs_f32(MIN_REPAINT_INTERVAL) {
                ui.update(&mut display, None, &call_manager, &mut led)?;
                did_repaint = true;
            }
        } else {
            for key in input_keys {
                ui.update(&mut display, Some(key), &call_manager, &mut led)?;
            }
            did_repaint = true;
        }

        if did_repaint {
            // Flush output
            display.flush()?;

            // Mark the time
            last_repaint_time = start;
        }

        // Wait until next tick
        let deadline = start + Duration::from_secs_f32(TARGET_UPDATE_INTERVAL);
        let remaining = deadline - Instant::now();
        sleep(remaining);
    }

    call_manager_thread.stop()?;

    Ok(())
}
