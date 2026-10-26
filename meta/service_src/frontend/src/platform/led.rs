use anyhow::Result;
use std::collections::hash_map::Entry;

use std::{collections::HashMap, fs::OpenOptions, io::Write};

#[allow(unused)]
#[derive(Debug, Copy, Clone, PartialEq, Eq, Hash)]
pub enum LedId {
    Status,
    Headset,
    Mute,
    Speaker,

    LineGreen(usize),
    LineRed(usize),
}

fn led_to_index(id: LedId) -> usize {
    match id {
        LedId::Status => 0,
        LedId::LineGreen(n @ 0..=4) => 1 + n,
        LedId::Headset => 6,
        LedId::LineRed(n @ 0..=4) => 7 + n,
        LedId::Mute => 12,
        LedId::LineGreen(n @ 5..=9) => 13 + n,
        LedId::Speaker => 18,
        LedId::LineRed(n @ 5..=9) => 19 + n,
        _ => panic!(),
    }
}

#[cfg(target_arch = "arm")]
pub fn led_set(id: LedId, on: bool) -> Result<()> {
    let mut led_file = OpenOptions::new() //.
        .write(true)
        .open("/proc/keypad/led")?;

    // Ignore written amount...
    // XXX: do we want write_all?
    let _ = led_file
        .write(format!("0 {} 0x{}\n", led_to_index(id), if on { 1 } else { 0 }).as_bytes())?;

    Ok(())
}

#[cfg(target_arch = "x86_64")]
pub fn led_set(id: LedId, on: bool) -> Result<()> {
    // Dummy
    Ok(())
}

pub struct LedManager {
    states: HashMap<LedId, bool>,
}

impl LedManager {
    pub fn new() -> Result<Self> {
        Ok(Self { states: HashMap::new() })
    }

    pub fn set(&mut self, id: LedId, state: bool) -> Result<()> {
        let mut dirty = false;
        match self.states.entry(id) {
            Entry::Occupied(mut entry) => {
                let v = entry.get_mut();
                if *v != state {
                    *v = state;
                    dirty = true;
                }
            },
            Entry::Vacant(entry) => {
                entry.insert(state);
                dirty = true;
            },
        }

        if dirty {
            led_set(id, state)?;
        }

        Ok(())
    }
}
