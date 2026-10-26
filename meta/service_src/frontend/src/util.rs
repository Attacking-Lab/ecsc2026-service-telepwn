use std::{
    fs::File,
    io::{Read, Write},
    path::Path,
};

use anyhow::{anyhow, Result};

use shared::net_audio::*;

pub fn load_audio(path: &Path) -> Result<Vec<Sample>> {
    let mut file = File::open(path)?;
    let mut data = vec![];
    file.read_to_end(&mut data)?;
    // XXX: should probably remove this dependency
    let samples = bytemuck::allocation::cast_vec(data);

    if samples.len() % BLOCK_NUM_SAMPLES != 0 {
        return Err(anyhow!("audio is misaligned size"));
    }

    Ok(samples)
}
