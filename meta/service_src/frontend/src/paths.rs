use const_format::concatcp;

pub const FRONTEND_DATA_DIR: &str = concatcp!(shared::DATA_DIR, "frontend/");

pub const WALLPAPERS_DIR: &str = concatcp!(FRONTEND_DATA_DIR, "wallpapers/");
pub const ACTIVE_WALLPAPER_PATH: &str = concatcp!(WALLPAPERS_DIR, "active_wallpaper.rgb565");

pub const RINGTONES_DIR: &str = concatcp!(FRONTEND_DATA_DIR, "ringtones/");
pub const ACTIVE_RINGTONE_PATH: &str = concatcp!(RINGTONES_DIR, "active_ringtone.pcm");
