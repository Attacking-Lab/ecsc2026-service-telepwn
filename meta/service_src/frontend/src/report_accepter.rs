use std::{
    fs::File,
    net::{TcpListener, TcpStream},
    path::PathBuf,
    thread,
};

use anyhow::Result;

use byteorder::{BigEndian, ReadBytesExt};
use tracing::{info, warn};

#[must_use]
fn item_new_report_path_by_id(id: u64) -> PathBuf {
    PathBuf::from(shared::NEW_REPORTS_DIR).join(id.to_string())
}

fn handle_report(mut stream: TcpStream) -> Result<()> {
    let item_id = stream.read_u64::<BigEndian>()?;

    if let Err(e) = File::create_new(item_new_report_path_by_id(item_id)) {
        warn!(?e, item_id, "tried to create report for item but failed");
        return Ok(());
    }
    info!(item_id, "item report handled");

    Ok(())
}

pub fn start() {
    thread::spawn(|| {
        // Start listening
        let listener = TcpListener::bind(format!("0.0.0.0:{}", shared::FRONTEND_REPORT_PORT))
            .expect("failed to bind");
        info!("report accepter listening");

        loop {
            let (stream, _addr) = listener.accept().expect("failed to accept");
            thread::spawn(move || {
                let res = handle_report(stream);
                match res {
                    Ok(_) => {},
                    Err(e) => {
                        println!("connection ended with {e:?}");
                    },
                }
            });
        }
    });
}
