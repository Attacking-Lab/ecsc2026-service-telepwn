pub mod dtmf;
pub mod goertzel;
pub mod net_audio;

use const_format::concatcp;

pub const BACKEND_PORT: u16 = 9400; // Public!
pub const FRONTEND_PORT: u16 = 10401; // Not public!
pub const FRONTEND_REPORT_PORT: u16 = 10402; // Not public!

pub const DATA_DIR: &str = "/tmp/telepwn_data/";

pub fn check_data_dir() -> Result<(), std::io::Error> {
    if !std::path::Path::new(DATA_DIR).is_dir() {
        return Err(std::io::Error::other(format!(
            "data path {} does not exist",
            DATA_DIR
        )));
    }

    Ok(())
}

pub const NEW_REPORTS_DIR: &str = concatcp!(DATA_DIR, "reports/new/");
pub const OLD_REPORTS_DIR: &str = concatcp!(DATA_DIR, "reports/old/");
