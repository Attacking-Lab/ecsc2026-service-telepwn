use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

use anyhow::Result;

use tracing::{debug, info, warn};

use serde::Deserialize;

pub struct TeamInfo {
    pub id: i32,
    pub name: String,
    pub ip: String,
    pub logo_data: Vec<u8>,
    pub default_ringtone_path: PathBuf,
    pub default_wallpaper_path: PathBuf,
}

#[derive(Deserialize, Debug)]
struct ConfigTeam {
    id: i32,
    name: String,
    ip: String,
    logo_path: String,
    default_ringtone_path: String,
    default_wallpaper_path: String,
}

fn load_team_infos() -> Result<Vec<TeamInfo>> {
    // Load config
    let config_root = Path::new("config/");
    let config_file = File::open(config_root.join("teams.json"))?;
    let config_reader = BufReader::new(config_file);

    let mut result = vec![];
    let config_teams: Vec<ConfigTeam> = serde_json::from_reader(config_reader)?;
    for config_team in config_teams {
        let logo_path = config_root.join(&config_team.logo_path);
        debug!(?logo_path, "loading logo");
        let mut logo_file = File::open(logo_path)?;
        let mut logo_data = vec![];
        logo_file.read_to_end(&mut logo_data)?;

        let team = TeamInfo {
            id: config_team.id,
            ip: config_team.ip.clone(),
            name: config_team.name.clone(),
            logo_data,
            default_ringtone_path: config_root.join(config_team.default_ringtone_path.clone()),
            default_wallpaper_path: config_root.join(config_team.default_wallpaper_path.clone()),
        };
        result.push(team);
    }

    Ok(result)
}

pub struct Config {
    pub own_team_index: usize,
    pub team_infos: Vec<TeamInfo>,
}

impl Config {
    pub fn load() -> Result<Config> {
        let own_team_id =
            std::env::var("TEAM_ID").ok().and_then(|s| s.parse::<i32>().ok()).unwrap_or(0);

        let mut team_infos = load_team_infos()?;
        let own_team_index = match team_infos.iter().position(|ti| ti.id == own_team_id) {
            Some(index) => index,
            None => {
                warn!(own_team_id, "unable to find own team ID in config, falling back to NOP");
                0
            },
        };

        info!(
            own_team_index,
            own_team_id,
            team_name = team_infos[own_team_index].name,
            "configured as team"
        );

        Ok(Config { own_team_index, team_infos })
    }
}
