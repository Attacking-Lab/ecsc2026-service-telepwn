use std::io::{Read, Write};
use std::path::Path;
use std::sync::LazyLock;
use std::{f32::consts::PI, fs::File};

use anyhow::Result;

use embedded_graphics::{
    image::{Image, ImageRaw},
    pixelcolor::{raw::RawU16, Rgb555},
    prelude::*,
    primitives::Rectangle,
};
use shared::dtmf::{add_dtmf_tone, DtmfScanner, DtmfTone};
use shared::{goertzel::*, net_audio::*};
use tracing::debug;

use crate::display::{Display, FbColor, BYTES_PER_PIXEL};

const PIXEL_NUM_SAMPLES: usize = 8;
// These should be chosen to be pure DFT bins
// Since we use 8000 Hz sampling rate, and are using 8 samples per symbol
// that makes N*8000/8 = 0, 1000, 2000, 3000 our possible tones.
// Using DC is unrealistic, so this leaves us with three tones for three channels.
// 2000 and 3000 Hz are of course harmonics of 1000 Hz, which is not ideal in
// case of hypothetic distortion, but for us it will be fine.
// A training preamble could be implemented later to construct a calibration
// matrix at the start of receiving.
const CHANNEL_NUM: usize = 3;
const CHANNEL_FREQS_HZ: [f32; CHANNEL_NUM] = [1000., 2000., 3000.];

type FaxColor = Rgb555;
const CHANNEL_MAX: u8 = FaxColor::MAX_R;

// Some assumptions for this module which, if changed, require adjustments here...
//const {
//	assert!(BLOCK_NUM_SAMPLES % PIXEL_NUM_SAMPLES == 0);
//	assert!(SAMPLE_RATE == 8000);
//}

static TONE_SAMPLE_TABLES: LazyLock<[[i16; PIXEL_NUM_SAMPLES]; CHANNEL_NUM]> =
    LazyLock::new(|| {
        [
            make_tone_samples(CHANNEL_FREQS_HZ[0]),
            make_tone_samples(CHANNEL_FREQS_HZ[1]),
            make_tone_samples(CHANNEL_FREQS_HZ[2]),
        ]
    });
static TONE_COEFF_TABLES: LazyLock<[i32; CHANNEL_NUM]> = LazyLock::new(|| {
    [
        goertzel_coeff(CHANNEL_FREQS_HZ[0]),
        goertzel_coeff(CHANNEL_FREQS_HZ[1]),
        goertzel_coeff(CHANNEL_FREQS_HZ[2]),
    ]
});

pub fn make_tone_samples(freq_hz: f32) -> [i16; PIXEL_NUM_SAMPLES] {
    let mut result = [0; PIXEL_NUM_SAMPLES];

    let omega = 2. * PI * freq_hz;
    for (i, out_sample) in result.iter_mut().enumerate() {
        let t = i as f32 / SAMPLE_RATE as f32;
        let soft = f32::sin(t * omega);
        let hard = (soft * (1 << 8) as f32).round() as i16;
        *out_sample = hard;
    }

    result
}

pub fn encode_color(in_color: &FaxColor, out_samples: &mut [Sample; PIXEL_NUM_SAMPLES]) {
    // Max for RGB555 is 2^5 = 32 in each channel
    // 32 * 3 = 96 is in range for i8, so no clipping should occur
    let chan_values = [in_color.r(), in_color.g(), in_color.b()];

    for (p, sample) in out_samples.iter_mut().enumerate() {
        // Accumulate in i16 space
        let mut sample_wide = 0i16;
        for (chan_index, chan_value) in chan_values.iter().enumerate() {
            sample_wide += TONE_SAMPLE_TABLES[chan_index][p] * *chan_value as i16;
        }

        // Truncate to i8
        *sample = (sample_wide >> (i16::BITS - Sample::BITS)) as i8;
    }
}

pub fn encode_bit(bit: bool, out_samples: &mut [Sample; PIXEL_NUM_SAMPLES]) {
    let color = match bit {
        false => FaxColor::new(0, CHANNEL_MAX, 0),
        true => FaxColor::new(0, 0, CHANNEL_MAX),
    };
    encode_color(&color, out_samples)
}

pub fn decode_color_soft(in_samples: &[Sample; PIXEL_NUM_SAMPLES]) -> [i32; CHANNEL_NUM] {
    let mut chan_values = [0; CHANNEL_NUM];
    for (i, tone_coeff) in TONE_COEFF_TABLES.iter().enumerate() {
        let soft = goertzel_mag(in_samples, *tone_coeff);
        chan_values[i] = soft;
    }
    chan_values
}

pub fn decode_color_hard(
    in_samples: &[Sample; PIXEL_NUM_SAMPLES],
    black: &[i32; CHANNEL_NUM],
    white: &[i32; CHANNEL_NUM],
) -> FaxColor {
    let chan_values_soft = decode_color_soft(in_samples);
    let mut chan_values_hard = [0; CHANNEL_NUM];
    for (i, hard) in chan_values_hard.iter_mut().enumerate() {
        let soft_remapped =
            ((chan_values_soft[i] - black[i]) * CHANNEL_MAX as i32) / (white[i] - black[i]);
        *hard = (soft_remapped as u8).clamp(0, CHANNEL_MAX);
    }

    FaxColor::new(chan_values_hard[0], chan_values_hard[1], chan_values_hard[2])
}

pub fn decode_bit(in_samples: &[Sample; PIXEL_NUM_SAMPLES]) -> bool {
    let soft = decode_color_soft(in_samples);

    soft[2] > soft[1]
}

// Statically computed white level
static FAX_WHITE_LEVEL: LazyLock<[i32; CHANNEL_NUM]> = LazyLock::new(|| {
    let mut samples = [0; PIXEL_NUM_SAMPLES];
    encode_color(&FaxColor::WHITE, &mut samples);
    decode_color_soft(&samples)
});

const FAX_DISP_WIDTH: usize = 480;
const FAX_DISP_HEIGHT: usize = 240;
const FAX_DISP_NUM_PIXELS: usize = FAX_DISP_HEIGHT * FAX_DISP_WIDTH;

// SYNC with backend downsample logic in audio.rs
const FAX_FULL_SCALE: usize = 4;
const FAX_PREVIEW_DOWNSAMPLING: usize = 10;
const FAX_PREVIEW_SCALE: usize = FAX_FULL_SCALE * FAX_PREVIEW_DOWNSAMPLING;

#[derive(Debug, Copy, Clone, PartialEq)]
pub enum FaxKind {
    Full,
    Preview,
}

#[derive(Debug, Copy, Clone, PartialEq)]
pub enum FaxModemActivity {
    Idle,
    Transmitting,
    Receiving,
}

#[derive(Debug, Copy, Clone, PartialEq)]
enum FaxStage {
    StartTone,
    Pixel(usize, usize), // TODO: white balance detection and stuff
    EndTone,
    Done,
}

pub struct FaxModem {
    dtmf_scanner: DtmfScanner,

    data: Box<[u8; BYTES_PER_PIXEL * FAX_DISP_NUM_PIXELS]>,

    activity: FaxModemActivity,
    stage: FaxStage,
    scale: usize,
    rx_black_level: [i32; CHANNEL_NUM],
    rx_white_level: [i32; CHANNEL_NUM],
}

impl FaxModem {
    pub fn new() -> Result<Self> {
        Ok(Self {
            dtmf_scanner: DtmfScanner::new()?,
            data: vec![0u8; BYTES_PER_PIXEL * FAX_DISP_NUM_PIXELS].try_into().unwrap(),
            activity: FaxModemActivity::Idle,
            stage: FaxStage::Done,
            scale: FAX_FULL_SCALE,
            rx_black_level: [0; CHANNEL_NUM],
            rx_white_level: *FAX_WHITE_LEVEL,
        })
    }

    pub fn recv_start(&mut self, kind: FaxKind) -> Result<()> {
        // Reset buffer
        self.activity = FaxModemActivity::Receiving;
        self.stage = FaxStage::Pixel(0, 0);
        self.scale = match kind {
            FaxKind::Full => FAX_FULL_SCALE,
            FaxKind::Preview => FAX_PREVIEW_SCALE,
        };
        self.data.fill(0);

        Ok(())
    }

    fn set_pixel(&mut self, pixel_offset: usize, color: FaxColor) {
        let start = pixel_offset * BYTES_PER_PIXEL;
        self.data[start..start + BYTES_PER_PIXEL]
            .copy_from_slice(&FbColor::from(color).to_be_bytes());
    }

    fn get_pixel(&mut self, pixel_offset: usize) -> FaxColor {
        let start = pixel_offset * BYTES_PER_PIXEL;
        let data = &self.data[start..start + BYTES_PER_PIXEL];
        FaxColor::from(FbColor::from(RawU16::new(u16::from_be_bytes(data.try_into().unwrap()))))
    }

    pub fn recv_block(&mut self, in_block: &[Sample; BLOCK_NUM_SAMPLES]) -> Result<()> {
        // Trigger phrase
        if self.activity == FaxModemActivity::Idle {
            // Pass it through the DTMF scanner
            self.dtmf_scanner.scan_block_for_dtmf(in_block);

            // Fax start signal?
            while let Some(dtmf_key) = self.dtmf_scanner.pop_tone() {
                debug!(?dtmf_key, "fax start");
                match dtmf_key {
                    DtmfTone::Special(2) => self.recv_start(FaxKind::Full)?,
                    DtmfTone::Special(3) => self.recv_start(FaxKind::Preview)?,
                    _ => {},
                }
            }

            // Fax starts in next block!
            return Ok(());
        }

        // Not receiving?
        if self.activity != FaxModemActivity::Receiving {
            // We are done
            return Ok(());
        }

        let (chunks, remainder) = in_block.as_chunks::<PIXEL_NUM_SAMPLES>();
        assert!(remainder.is_empty());
        for symbol_samples in chunks {
            match self.stage {
                FaxStage::StartTone => { /* should never happen */ },
                FaxStage::Pixel(y, x) => {
                    // Decode color
                    let color = decode_color_hard(
                        symbol_samples,
                        &self.rx_black_level,
                        &self.rx_white_level,
                    );

                    // Save to FB
                    let yb = y * self.scale;
                    let xb = x * self.scale;
                    for yp in yb..yb + self.scale {
                        let row_offset = yp * FAX_DISP_WIDTH;
                        for xp in xb..xb + self.scale {
                            let pixel_offset = row_offset + xp;
                            self.set_pixel(pixel_offset, color);
                        }
                    }

                    // Advance pixel
                    if xb + self.scale < FAX_DISP_WIDTH {
                        // Next pixel in same row
                        self.stage = FaxStage::Pixel(y, x + 1);
                    } else if yb + self.scale < FAX_DISP_HEIGHT {
                        // First pixel of next row
                        self.stage = FaxStage::Pixel(y + 1, 0);
                    } else {
                        // We are done
                        self.stage = FaxStage::Done;
                        self.activity = FaxModemActivity::Idle;
                    }
                },
                FaxStage::EndTone => { /* ignore */ },
                FaxStage::Done => {},
            }
        }

        Ok(())
    }

    pub fn send_start(&mut self) -> Result<()> {
        self.activity = FaxModemActivity::Transmitting;
        self.stage = FaxStage::StartTone;
        self.scale = FAX_FULL_SCALE;
        Ok(())
    }

    pub fn send_block(&mut self, out_block: &mut [Sample; BLOCK_NUM_SAMPLES]) -> Result<()> {
        if self.activity != FaxModemActivity::Transmitting {
            return Ok(());
        }

        if self.stage == FaxStage::StartTone {
            out_block.fill(0);
            add_dtmf_tone(out_block, DtmfTone::Special(2));
            self.stage = FaxStage::Pixel(0, 0);
            return Ok(());
        } else if self.stage == FaxStage::EndTone {
            out_block.fill(0);
            add_dtmf_tone(out_block, DtmfTone::Pound);
            self.stage = FaxStage::Done;
            self.activity = FaxModemActivity::Idle;
            return Ok(());
        }

        let (chunks, remainder) = out_block.as_chunks_mut::<PIXEL_NUM_SAMPLES>();
        assert!(remainder.is_empty());
        for symbol_samples in chunks {
            match self.stage {
                FaxStage::StartTone => { /* should never happen */ },
                FaxStage::Pixel(y, x) => {
                    // Fetch color
                    let yb = y * self.scale;
                    let xb = x * self.scale;
                    let p = yb * FAX_DISP_WIDTH + xb;
                    let color = self.get_pixel(p);

                    // Encode
                    encode_color(&color, symbol_samples);

                    // Advance pixel
                    if xb + self.scale < FAX_DISP_WIDTH {
                        // Next pixel in same row
                        self.stage = FaxStage::Pixel(y, x + 1);
                    } else if yb + self.scale < FAX_DISP_HEIGHT {
                        // First pixel of next row
                        self.stage = FaxStage::Pixel(y + 1, 0);
                    } else {
                        // We are done
                        self.stage = FaxStage::EndTone;
                    }
                },
                FaxStage::EndTone => { /* wait for next block */ },
                FaxStage::Done => {},
            }
        }

        Ok(())
    }

    pub fn draw(&self, display: &mut impl Display) -> Result<()> {
        let img_raw = ImageRaw::<FbColor>::new(&*self.data, FAX_DISP_WIDTH as u32);
        let img = Image::new(&img_raw, Point { x: 0 as i32, y: 0 as i32 });
        img.draw(display)?;

        Ok(())
    }

    pub fn activity(&self) -> FaxModemActivity {
        self.activity
    }

    pub fn progress_pct(&self) -> usize {
        // TODO this should really be option
        if let FaxStage::Pixel(y, x) = self.stage {
            // TODO dedup
            let yb = y * self.scale;
            let xb = x * self.scale;
            let p = yb * FAX_DISP_WIDTH + xb;
            let pct = (100 * p) / FAX_DISP_NUM_PIXELS;
            pct
        } else {
            100
        }
    }

    pub fn load_image(&mut self, path: &Path) -> Result<()> {
        File::open(path)?.read_exact(&mut *self.data)?;

        Ok(())
    }

    pub fn save_image(&mut self, path: &Path) -> Result<()> {
        File::create(path)?.write_all(&*self.data)?;

        Ok(())
    }
}
