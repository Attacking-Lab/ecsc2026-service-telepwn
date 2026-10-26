use std::{
    fs::File,
    io::{Read, Write},
    path::Path,
};

use anyhow::{anyhow, Result};

use shared::{dtmf::*, net_audio::*};

use embedded_graphics::{
    image::{Image, ImageRaw},
    pixelcolor::{raw::RawU16, Rgb555},
    prelude::*,
    primitives::Rectangle,
};
use tracing::debug;

use crate::{
    display::{fb_from_rgb888, Display, FbColor, DISP_WIDTH},
    util,
};
const RINGTONE_CHUNK_SIZE: usize = 250;
const RINGTONE_MAX_NUM_SAMPLES: usize = 15 * SAMPLE_RATE;
const RINGTONE_MAX_CHUNKS: usize = RINGTONE_MAX_NUM_SAMPLES / RINGTONE_CHUNK_SIZE;
const WAVEFORM_COLOR: FbColor = fb_from_rgb888(0x80, 0x10, 0xa0);

#[derive(Debug, Copy, Clone, PartialEq)]
pub enum RingtoneRecorderActivity {
    Idle,
    Transmitting,
    Receiving,
}

#[derive(Debug, Copy, Clone, PartialEq)]
enum RingtoneStage {
    StartTone,
    Chunk(usize),
    EndTone,
    Done,
}

pub struct RingtoneRecorder {
    dtmf_scanner: DtmfScanner,

    activity: RingtoneRecorderActivity,
    stage: RingtoneStage,

    chunks_used: usize,
    data: Box<[Sample; RINGTONE_MAX_NUM_SAMPLES]>,
    chunk_mins: Box<[Sample; RINGTONE_MAX_CHUNKS]>,
    chunk_maxs: Box<[Sample; RINGTONE_MAX_CHUNKS]>,
}

impl RingtoneRecorder {
    pub fn new() -> Result<Self> {
        Ok(Self {
            dtmf_scanner: DtmfScanner::new()?,

            activity: RingtoneRecorderActivity::Idle,
            stage: RingtoneStage::Done,

            chunks_used: 0,
            data: vec![0; RINGTONE_MAX_NUM_SAMPLES].try_into().unwrap(),
            chunk_mins: vec![0; RINGTONE_MAX_CHUNKS].try_into().unwrap(),
            chunk_maxs: vec![0; RINGTONE_MAX_CHUNKS].try_into().unwrap(),
        })
    }

    pub fn recv_start(&mut self) -> Result<()> {
        // Reset buffer
        self.activity = RingtoneRecorderActivity::Receiving;
        self.stage = RingtoneStage::Chunk(0);
        self.data.fill(0);
        self.chunks_used = 0;
        self.chunk_mins.fill(0); // optional ig
        self.chunk_maxs.fill(0);

        Ok(())
    }

    pub fn recv_stop(&mut self) -> Result<()> {
        // Reset buffer
        self.activity = RingtoneRecorderActivity::Idle;
        self.stage = RingtoneStage::Done;

        Ok(())
    }

    fn generate_waveform_for_chunk(&mut self, n: usize) -> Result<()> {
        let start = n * RINGTONE_CHUNK_SIZE;
        let end = start + RINGTONE_CHUNK_SIZE;
        let chunk_samples = &self.data[start..end];
        let mut min = chunk_samples[0];
        let mut max = chunk_samples[0];
        for sample in chunk_samples {
            if *sample < min {
                min = *sample;
            }
            if *sample > max {
                max = *sample;
            }
        }
        self.chunk_mins[n] = min;
        self.chunk_maxs[n] = max;

        Ok(())
    }

    pub fn recv_block(&mut self, in_block: &[Sample; BLOCK_NUM_SAMPLES]) -> Result<()> {
        // Trigger phrase
        if self.activity == RingtoneRecorderActivity::Idle {
            // Pass it through the DTMF scanner
            self.dtmf_scanner.scan_block_for_dtmf(in_block);

            // Fax start signal?
            while let Some(dtmf_key) = self.dtmf_scanner.pop_tone() {
                debug!(?dtmf_key, "ringtone start scan");
                match dtmf_key {
                    DtmfTone::Special(1) => self.recv_start()?,
                    _ => {},
                }
            }

            // Fax starts in next block!
            return Ok(());
        }

        // Not receiving?
        if self.activity != RingtoneRecorderActivity::Receiving {
            // We are done
            return Ok(());
        }

        // Pass it through the DTMF scanner to look for stop signal
        self.dtmf_scanner.scan_block_for_dtmf(in_block);
        while let Some(dtmf_key) = self.dtmf_scanner.pop_tone() {
            debug!(?dtmf_key, "ringtone stop scan");
            match dtmf_key {
                DtmfTone::Pound => self.recv_stop()?,
                _ => {},
            }
        }

        let (chunks, remainder) = in_block.as_chunks::<RINGTONE_CHUNK_SIZE>();
        assert!(remainder.is_empty());
        for chunk_samples in chunks {
            match self.stage {
                RingtoneStage::StartTone => { /* should never happen */ },
                RingtoneStage::Chunk(n) => {
                    let start = n * RINGTONE_CHUNK_SIZE;
                    let end = start + RINGTONE_CHUNK_SIZE;
                    self.data[start..end].copy_from_slice(chunk_samples);
                    self.chunks_used = n + 1;

                    // Generate min and max
                    self.generate_waveform_for_chunk(n)?;

                    if n + 1 < RINGTONE_MAX_CHUNKS {
                        self.stage = RingtoneStage::Chunk(n + 1);
                    } else {
                        // We are done
                        self.stage = RingtoneStage::Done;
                        self.activity = RingtoneRecorderActivity::Idle;
                    }
                },
                RingtoneStage::EndTone => { /* ignore */ },
                RingtoneStage::Done => {},
            }
        }

        Ok(())
    }

    pub fn send_start(&mut self) -> Result<()> {
        self.activity = RingtoneRecorderActivity::Transmitting;
        self.stage = RingtoneStage::StartTone;
        Ok(())
    }

    pub fn send_block(&mut self, out_block: &mut [Sample; BLOCK_NUM_SAMPLES]) -> Result<()> {
        if self.activity != RingtoneRecorderActivity::Transmitting {
            return Ok(());
        }

        if self.stage == RingtoneStage::StartTone {
            out_block.fill(0);
            add_dtmf_tone(out_block, DtmfTone::Pound);
            self.stage = RingtoneStage::Chunk(0);
            return Ok(());
        } else if self.stage == RingtoneStage::EndTone {
            out_block.fill(0);
            add_dtmf_tone(out_block, DtmfTone::Pound);
            self.stage = RingtoneStage::Done;
            self.activity = RingtoneRecorderActivity::Idle;
            return Ok(());
        }

        let (chunks, remainder) = out_block.as_chunks_mut::<RINGTONE_CHUNK_SIZE>();
        assert!(remainder.is_empty());
        for chunk_samples in chunks {
            match self.stage {
                RingtoneStage::StartTone => { /* should never happen */ },
                RingtoneStage::Chunk(n) => {
                    let start = n * RINGTONE_CHUNK_SIZE;
                    let end = start + RINGTONE_CHUNK_SIZE;
                    chunk_samples.copy_from_slice(&self.data[start..end]);

                    if n + 1 < self.chunks_used {
                        self.stage = RingtoneStage::Chunk(n + 1);
                    } else {
                        // We are done
                        self.stage = if n + 1 < RINGTONE_MAX_CHUNKS {
                            // Not a full ringtone, send
                            RingtoneStage::EndTone
                        } else {
                            RingtoneStage::Done
                        };
                    }
                },
                RingtoneStage::EndTone => { /* wait for next block */ },
                RingtoneStage::Done => {},
            }
        }

        Ok(())
    }

    pub fn draw(&self, display: &mut impl Display) -> Result<()> {
        const {
            assert!(RINGTONE_MAX_CHUNKS == DISP_WIDTH);
        }
        for chunk_index in 0..self.chunks_used {
            let x = chunk_index as i32;
            let zero_y = 128;
            let scale = 1;
            let top_y = zero_y - self.chunk_maxs[chunk_index] as i32 / scale;
            let bottom_y = zero_y - self.chunk_mins[chunk_index] as i32 / scale;
            let rect = Rectangle::with_corners(Point { x, y: top_y }, Point { x, y: bottom_y });

            display.fill_solid(&rect, WAVEFORM_COLOR)?;
        }

        Ok(())
    }

    pub fn activity(&self) -> RingtoneRecorderActivity {
        self.activity
    }

    pub fn progress_pct(&self) -> usize {
        // TODO this should really be option
        if let RingtoneStage::Chunk(n) = self.stage {
            let pct = (100 * n) / RINGTONE_MAX_CHUNKS;
            pct
        } else {
            100
        }
    }

    pub fn load_ringtone(&mut self, path: &Path) -> Result<()> {
        let samples = util::load_audio(path)?;
        if samples.len() > self.data.len() {
            return Err(anyhow!("ringtone is too big"));
        }
        if samples.len() % BLOCK_NUM_SAMPLES != 0 {
            return Err(anyhow!("ringtone is misaligned size"));
        }
        self.data[..samples.len()].copy_from_slice(&samples);
        self.chunks_used = samples.len() / RINGTONE_CHUNK_SIZE;

        for n in 0..self.chunks_used {
            self.generate_waveform_for_chunk(n)?;
        }

        Ok(())
    }

    pub fn save_ringtone(&mut self, path: &Path) -> Result<()> {
        // Refuse if it is too short
        if self.chunks_used * RINGTONE_CHUNK_SIZE < 3 * SAMPLE_RATE {
            return Err(anyhow!("ringtone too short"));
        }

        File::create(path)?
            .write_all(samples_as_raw(&self.data[0..self.chunks_used * RINGTONE_CHUNK_SIZE]))?;

        Ok(())
    }
}
