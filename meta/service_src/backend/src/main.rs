mod app;
mod audio;

use std::io::ErrorKind::Other;
use std::net::SocketAddr;
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

use shared::net_audio::*;
use tracing::level_filters::LevelFilter;
use tracing::{error, info, info_span};
use tracing_subscriber::fmt::format::FmtSpan;
use tracing_subscriber::EnvFilter;

use crate::app::InteractResult;

fn init_logging() {
    let filter = EnvFilter::builder() //.
        .with_default_directive(LevelFilter::INFO.into())
        .from_env_lossy();
    tracing_subscriber::fmt() //.
        .with_env_filter(filter)
        .with_span_events(FmtSpan::ACTIVE)
        .init();
}

fn handle_connection(stream: TcpStream) -> Result<(InteractResult, TcpStream), std::io::Error> {
    let mut ctx = audio::AudioContext::new(stream)?;
    let r = app::interact(&mut ctx)?;
    let stream = ctx.close();

    Ok((r, stream))
}

fn forward_connection(stream: TcpStream) -> Result<(), std::io::Error> {
    let timeout = Duration::from_secs(15);
    let mut stream = AsyncAudioStream::new(stream, Some(timeout))?;
    let own_team_id =
        std::env::var("TEAM_ID").ok().and_then(|s| s.parse::<i32>().ok()).unwrap_or(0);
    let forward_addr =
        match format!("10.60.{}.4:{}", own_team_id, shared::FRONTEND_PORT).parse::<SocketAddr>() {
            Ok(a) => a,
            Err(_) => return Err(std::io::Error::new(Other, "unable to create forward address")),
        };

    let mut forward_stream = AsyncAudioStream::new(
        TcpStream::connect_timeout(&forward_addr, Duration::from_secs(15))?,
        Some(timeout),
    )?;

    info!("forward connection established");

    // TODO: This is kind of duplicated with frontend call_manager and main
    // Also not clear if this is the way, maybe we should be slower here...
    // Update 4x as fast as the buffers should be flying
    const TIME_PER_BUFFER: f32 = BLOCK_NUM_SAMPLES as f32 / SAMPLE_RATE as f32;
    const TARGET_UPDATE_INTERVAL: f32 = TIME_PER_BUFFER / 4.;

    let mut block = [0 as Sample; BLOCK_NUM_SAMPLES];
    loop {
        let start = Instant::now();

        if forward_stream.recv_block_async(&mut block)? {
            stream.send_block_async(&block)?;
        }
        if stream.recv_block_async(&mut block)? {
            forward_stream.send_block_async(&block)?;
        }

        // Wait until next tick
        let deadline = start + Duration::from_secs_f32(TARGET_UPDATE_INTERVAL);
        let remaining = deadline - Instant::now();
        thread::sleep(remaining);
    }
}

fn main() -> Result<(), std::io::Error> {
    // Initialize logging
    init_logging();

    // Check data path
    app::init_data_dir()?;

    // Start listening
    let listener =
        TcpListener::bind(format!("0.0.0.0:{}", shared::BACKEND_PORT)).expect("failed to bind");
    info!("listening");

    loop {
        let (stream, addr) = match listener.accept() {
            Ok(r) => r,
            Err(e) => {
                error!(?e, "failed to accept conn");
                thread::sleep(Duration::from_millis(100));
                continue;
            },
        };

        thread::spawn(move || {
            let _span = info_span!("connection", %addr).entered();

            let (r, stream) = match handle_connection(stream) {
                Err(e) => {
                    info!(err = ?e, "app connection ended");
                    return;
                },
                Ok(r) => r,
            };

            match r {
                InteractResult::Done => {},
                InteractResult::RequestSupport => match forward_connection(stream) {
                    Err(e) => {
                        info!(err = ?e, "forward connection ended");
                        return;
                    },
                    Ok(_) => {},
                },
            };
        });
    }
}
