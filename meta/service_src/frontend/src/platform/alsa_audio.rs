use alsa::ctl::{ElemId, ElemValue};
use anyhow::{anyhow, Result};
use std::ffi::CString;

use tracing::{debug, info, warn};

use alsa::pcm::{Access, Format, HwParams, State, PCM};
use alsa::{Direction, HCtl, ValueOr};

use crate::audio::*;

fn open_pcm(name: &str, dir: Direction) -> Result<PCM> {
    let pcm = PCM::new(name, dir, false)?;

    let target_latency_samples = SAMPLE_RATE / 4;
    {
        let hwp = HwParams::any(&pcm)?;
        hwp.set_channels(1)?;
        hwp.set_rate(SAMPLE_RATE as u32, ValueOr::Nearest)?;
        hwp.set_format(Format::s16())?;
        hwp.set_access(Access::RWInterleaved)?;
        hwp.set_buffer_size_near(target_latency_samples as i32 * 2)?;
        pcm.hw_params(&hwp)?;

        // Start/stop thresholds for hiding jitters
        let swp = pcm.sw_params_current()?;
        match dir {
            Direction::Capture => {
                // Start on any request
                // XXX: This won't do anything right now because we check
                // avail before...
                swp.set_start_threshold(1)?;
            },
            Direction::Playback => {
                // Start after this many samples
                swp.set_start_threshold(std::cmp::min(
                    target_latency_samples as i32,
                    hwp.get_buffer_size()?,
                ))?;
            },
        }

        pcm.sw_params(&swp)?;
    }

    Ok(pcm)
}

fn set_hctl_elem(hctl_elem_name: &str, value: i32) -> Result<()> {
    let h = HCtl::new("main", false)?;
    h.load()?;

    let mut elem_id = ElemId::new(alsa::ctl::ElemIface::Mixer);
    elem_id.set_name(&CString::new(hctl_elem_name)?);
    let elem = match h.find_elem(&elem_id) {
        None => {
            return Err(anyhow!("control not found"));
        },
        Some(e) => e,
    };

    // DEBUG
    //for elem in h.elem_iter() {
    //    info!(n = ?elem.get_id()?, v = ?elem.read()?, "elem");
    //}

    let mut elem_value = ElemValue::new(alsa::ctl::ElemType::Integer)?;
    elem_value.set_integer(0, value);

    //info!(e = ?elem.read()?, "before");
    let value_before = elem.read()?;
    elem.write(&elem_value)?;
    //info!(e = ?elem.read()?, "after");
    debug!(name = hctl_elem_name, before = ?value_before, after = ?elem.read()?, "set_hctl_elem");

    Ok(())
}

fn convert_digital_volume(volume: f32) -> i32 {
    (volume * 69.).round() as i32
}

fn set_volume(channel_name: &str, direction: Direction, analog: i32, digital: i32) -> Result<()> {
    let dir_str = match direction {
        Direction::Playback => "Playback",
        Direction::Capture => "Capture",
    };

    set_hctl_elem(format!("{} {} Volume", channel_name, dir_str).as_str(), analog)?;
    set_hctl_elem(format!("{}dig {} Volume", channel_name, dir_str).as_str(), digital)?;

    Ok(())
}

pub struct AlsaAudioPlayChannel {
    channel_name: String,
    analog_volume: i32,
    pcm_out: PCM,
}

impl AlsaAudioPlayChannel {
    pub fn new(channel_name: &str, analog_volume: i32) -> Result<Self> {
        let mut res = Self {
            channel_name: channel_name.to_owned(),
            analog_volume,
            pcm_out: open_pcm(channel_name, Direction::Playback)?,
        };

        // Set initial volume
        res.set_volume(0.5)?;

        Ok(res)
    }
}

// TODO: Potentially remove dir param
fn try_recover_xrun(pcm: &mut PCM, _dir: Direction) -> Result<bool> {
    if pcm.state() == State::XRun {
        pcm.prepare()?;
        Ok(true)
    } else {
        Ok(false)
    }
}

impl AudioPlayChannel for AlsaAudioPlayChannel {
    fn play_samples(&mut self, samples: &[Sample]) -> Result<bool> {
        // Resolve any xruns
        try_recover_xrun(&mut self.pcm_out, Direction::Playback)?;

        // Space to write?
        let avail = match self.pcm_out.avail() {
            Ok(n) => n as usize,
            Err(e) => {
                if try_recover_xrun(&mut self.pcm_out, Direction::Playback)? {
                    return Ok(false);
                }
                return Err(e.into());
            },
        };
        debug!(avail, need = samples.len(), "playback");
        if avail < samples.len() {
            warn!(avail, need = samples.len(), "playback insufficient avail");
            return Ok(false);
        }

        // Convert to native
        let native_samples: Vec<_> = samples.iter().map(|s| (*s as i16) << 8).collect();

        // Push to device
        let r = {
            let io = self.pcm_out.io_i16()?;
            io.writei(&native_samples)
        };
        let _written = match r {
            Ok(n) => n,
            Err(e) => {
                if try_recover_xrun(&mut self.pcm_out, Direction::Playback)? {
                    return Ok(false);
                }
                return Err(e.into());
            },
        };

        Ok(true)
    }

    fn set_volume(&mut self, volume: f32) -> Result<()> {
        set_volume(
            &self.channel_name,
            Direction::Playback,
            self.analog_volume,
            convert_digital_volume(volume),
        )?;

        Ok(())
    }
}

unsafe impl Send for AlsaAudioPlayChannel {}

pub struct AlsaAudioCaptureChannel {
    channel_name: String,
    analog_volume: i32,
    pcm_in: PCM,
}

impl AlsaAudioCaptureChannel {
    pub fn new(channel_name: &str, analog_volume: i32) -> Result<Self> {
        let mut res = Self {
            channel_name: channel_name.to_owned(),
            analog_volume,
            pcm_in: open_pcm(channel_name, Direction::Capture)?,
        };

        // Set initial volume
        // Since we probably won't expose adjusting these, might as well put
        // them on full and use less analog gain instead.
        res.set_volume(1.0)?;

        Ok(res)
    }
}

impl AudioCaptureChannel for AlsaAudioCaptureChannel {
    fn capture_samples(&mut self, samples: &mut [Sample]) -> Result<bool> {
        // Resolve any xruns
        try_recover_xrun(&mut self.pcm_in, Direction::Playback)?;

        // Not running but could be?
        // XXX: Because of the way we are doing non-blocking handling,
        // read auto-start cannot work for us since we check avail
        // before calling read. This is a work-around for now.
        if self.pcm_in.state() == State::Prepared {
            self.pcm_in.start()?;
        }

        // Anything to read?
        let avail = match self.pcm_in.avail() {
            Ok(n) => n as usize,
            Err(e) => {
                if try_recover_xrun(&mut self.pcm_in, Direction::Capture)? {
                    return Ok(false);
                }
                return Err(e.into());
            },
        };
        //debug!(avail, need = samples.len(), "capture");
        if avail < samples.len() {
            //warn!(avail, need = samples.len(), "capture insufficient avail");
            return Ok(false);
        }

        // Pull from device
        let mut native_samples = vec![0i16; samples.len()];
        let r = {
            let io = self.pcm_in.io_i16()?;
            io.readi(&mut native_samples)
        };
        let _read = match r {
            Ok(n) => n,
            Err(e) => {
                if try_recover_xrun(&mut self.pcm_in, Direction::Capture)? {
                    return Ok(false);
                }
                return Err(e.into());
            },
        };

        // Convert to native
        for (out, native) in samples.iter_mut().zip(native_samples) {
            *out = (native >> 8) as i8;
        }

        Ok(true)
    }

    fn set_volume(&mut self, volume: f32) -> Result<()> {
        set_volume(
            &self.channel_name,
            Direction::Capture,
            self.analog_volume,
            convert_digital_volume(volume),
        )?;

        Ok(())
    }
}

unsafe impl Send for AlsaAudioCaptureChannel {}
