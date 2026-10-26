use std::collections::VecDeque;
use std::sync::{LazyLock, OnceLock};

use crate::goertzel::*;
use crate::net_audio::{Sample, BLOCK_NUM_SAMPLES, SAMPLE_RATE};

#[derive(Debug, Copy, Clone, PartialEq)]
pub enum DtmfTone {
    Num(i32),
    Special(i32),
    Asterisk,
    Pound,
}

pub struct DtmfScanner {
    dtmf_queue: VecDeque<DtmfTone>,
    current_tone: Option<DtmfTone>,
}

// Match the library we were using and is an even divisor of our block size.
const DTMF_SCAN_SAMPLE_GRANULARITY: usize = 250;

impl DtmfScanner {
    pub fn new() -> Result<Self, std::io::Error> {
        Ok(Self {
            dtmf_queue: VecDeque::new(),
            current_tone: None,
        })
    }

    fn scan_chunk_for_dtmf(&mut self, samples: &[Sample]) {
        // Figure out the tone
        let next_tone = detect_dtmf(samples);

        // Unchanged?
        if self.current_tone == next_tone {
            return;
        }

        // Different state.
        self.current_tone = next_tone;

        // Is it a tone now?
        if let Some(tone) = self.current_tone {
            self.dtmf_queue.push_back(tone);
        }
    }

    pub fn scan_block_for_dtmf(&mut self, in_block: &[Sample; BLOCK_NUM_SAMPLES]) {
        // Scan in fixed granularity chunks
        let (chunks, remainder) = in_block.as_chunks::<DTMF_SCAN_SAMPLE_GRANULARITY>();
        assert!(remainder.is_empty());
        for chunk in chunks {
            self.scan_chunk_for_dtmf(chunk);
        }
    }

    pub fn has_tone(&self) -> bool {
        !self.dtmf_queue.is_empty()
    }

    pub fn pop_tone(&mut self) -> Option<DtmfTone> {
        self.dtmf_queue.pop_front()
    }
}

const DTMF_FREQS_LO: [f32; 4] = [697., 770., 852., 941.];
const DTMF_FREQS_HI: [f32; 4] = [1209., 1336., 1477., 1633.];

static DTMF_COEFFS_LO: LazyLock<[i32; 4]> = LazyLock::new(|| {
    [
        goertzel_coeff(DTMF_FREQS_LO[0]),
        goertzel_coeff(DTMF_FREQS_LO[1]),
        goertzel_coeff(DTMF_FREQS_LO[2]),
        goertzel_coeff(DTMF_FREQS_LO[3]),
    ]
});
static DTMF_COEFFS_HI: LazyLock<[i32; 4]> = LazyLock::new(|| {
    [
        goertzel_coeff(DTMF_FREQS_HI[0]),
        goertzel_coeff(DTMF_FREQS_HI[1]),
        goertzel_coeff(DTMF_FREQS_HI[2]),
        goertzel_coeff(DTMF_FREQS_HI[3]),
    ]
});
static DTMF_COEFFS_LO_HARM: LazyLock<[i32; 4]> = LazyLock::new(|| {
    [
        goertzel_coeff(2. * DTMF_FREQS_LO[0]),
        goertzel_coeff(2. * DTMF_FREQS_LO[1]),
        goertzel_coeff(2. * DTMF_FREQS_LO[2]),
        goertzel_coeff(2. * DTMF_FREQS_LO[3]),
    ]
});
static DTMF_COEFFS_HI_HARM: LazyLock<[i32; 4]> = LazyLock::new(|| {
    [
        goertzel_coeff(2. * DTMF_FREQS_HI[0]),
        goertzel_coeff(2. * DTMF_FREQS_HI[1]),
        goertzel_coeff(2. * DTMF_FREQS_HI[2]),
        goertzel_coeff(2. * DTMF_FREQS_HI[3]),
    ]
});

fn decide_main(mag_sqs: &[i64; 4]) -> Option<usize> {
    // Find max
    let mut best = (0usize, 0i64);
    for (i, val) in mag_sqs.iter().enumerate() {
        if *val > best.1 {
            best = (i, *val);
        }
    }

    // Find relative power
    let mut other_power = 0;
    for (i, val) in mag_sqs.iter().enumerate() {
        if i != best.0 {
            other_power += *val;
        }
    }

    // Check relative power
    const MIN_FREQ_RELATIVE_MAG_SQ: i64 = 8;
    if best.1 > MIN_FREQ_RELATIVE_MAG_SQ * other_power {
        Some(best.0)
    } else {
        None
    }
}

fn detect_freq(
    samples: &[Sample],
    freq_coeffs: &[i32; 4],
    harm_coeffs: &[i32; 4],
) -> Option<usize> {
    // Run the frequency filters, two per pass over the samples.
    let (m0, m1) = goertzel_mag_sq_2(samples, freq_coeffs[0], freq_coeffs[1]);
    let (m2, m3) = goertzel_mag_sq_2(samples, freq_coeffs[2], freq_coeffs[3]);
    let mag_sqs = [m0, m1, m2, m3];

    // Decide which is the strongest frequency
    let main = match decide_main(&mag_sqs) {
        Some(i) => i,
        None => {
            // Could not determine a clear frequency.
            return None;
        }
    };
    let main_mag_sq = mag_sqs[main];

    // Check harmonic
    let harm_mag_sq = goertzel_mag_sq(samples, harm_coeffs[main]);

    // Strong harmonic present? Then reject.
    const MIN_HARM_RELATIVE_MAG_SQ: i64 = 8;
    if main_mag_sq < MIN_HARM_RELATIVE_MAG_SQ * harm_mag_sq {
        return None;
    }

    // Detected a frequency
    Some(main)
}

fn squelch_open(samples: &[Sample]) -> bool {
    let (words, tail) = samples.as_chunks::<4>();
    let any_set = words
        .iter()
        .fold(0u32, |acc, w| acc | u32::from_ne_bytes(w.map(|s| s as u8)))
        != 0
        || tail.iter().any(|&s| s != 0);
    if !any_set {
        return false;
    }

    // Dirt cheap linear squelch
    const SQUELCH_MAG: Sample = 2;
    for sample in samples {
        if sample.unsigned_abs() >= SQUELCH_MAG as u8 {
            // avoid MIN issues
            return true;
        }
    }
    false
}

fn detect_dtmf(samples: &[Sample]) -> Option<DtmfTone> {
    if !squelch_open(samples) {
        return None;
    }

    // Find the main frequency
    let main_lo = detect_freq(samples, &DTMF_COEFFS_LO, &DTMF_COEFFS_LO_HARM)?;
    let main_hi = detect_freq(samples, &DTMF_COEFFS_HI, &DTMF_COEFFS_HI_HARM)?;

    // Map to tone ID
    let tone = match (main_lo, main_hi) {
        (0, 0) => DtmfTone::Num(1),
        (0, 1) => DtmfTone::Num(2),
        (0, 2) => DtmfTone::Num(3),
        (0, 3) => DtmfTone::Special(0),
        (1, 0) => DtmfTone::Num(4),
        (1, 1) => DtmfTone::Num(5),
        (1, 2) => DtmfTone::Num(6),
        (1, 3) => DtmfTone::Special(1),
        (2, 0) => DtmfTone::Num(7),
        (2, 1) => DtmfTone::Num(8),
        (2, 2) => DtmfTone::Num(9),
        (2, 3) => DtmfTone::Special(2),
        (3, 0) => DtmfTone::Asterisk,
        (3, 1) => DtmfTone::Num(0),
        (3, 2) => DtmfTone::Pound,
        (3, 3) => DtmfTone::Special(3),
        _ => panic!(),
    };

    Some(tone)
}

fn get_dtmf_tone_indices(tone: DtmfTone) -> (usize, usize) {
    // Get freqs for tone
    let (idx_lo, idx_hi) = match tone {
        DtmfTone::Num(1) => (0, 0),
        DtmfTone::Num(2) => (0, 1),
        DtmfTone::Num(3) => (0, 2),
        DtmfTone::Special(0) => (0, 3),
        DtmfTone::Num(4) => (1, 0),
        DtmfTone::Num(5) => (1, 1),
        DtmfTone::Num(6) => (1, 2),
        DtmfTone::Special(1) => (1, 3),
        DtmfTone::Num(7) => (2, 0),
        DtmfTone::Num(8) => (2, 1),
        DtmfTone::Num(9) => (2, 2),
        DtmfTone::Special(2) => (2, 3),
        DtmfTone::Asterisk => (3, 0),
        DtmfTone::Num(0) => (3, 1),
        DtmfTone::Pound => (3, 2),
        DtmfTone::Special(3) => (3, 3),
        _ => panic!(),
    };

    (idx_lo, idx_hi)
}

static DTMF_TONE_BLOCKS: [OnceLock<[Sample; BLOCK_NUM_SAMPLES]>; 16] =
    [const { OnceLock::new() }; 16];

fn synthesize_tone_block(idx_lo: usize, idx_hi: usize) -> [Sample; BLOCK_NUM_SAMPLES] {
    let tone_amplitude = 0.5;
    let freq_amplitude = tone_amplitude / 2.;
    let mut block = [0 as Sample; BLOCK_NUM_SAMPLES];
    for freq_hz in [DTMF_FREQS_LO[idx_lo], DTMF_FREQS_HI[idx_hi]] {
        for (i, sample) in block.iter_mut().enumerate() {
            let t = i as f32 / SAMPLE_RATE as f32;
            let omega = 2. * std::f32::consts::PI * freq_hz;
            let float = f32::sin(t * omega);
            let fixed = (float * freq_amplitude * Sample::MAX as f32) as Sample;
            *sample += fixed;
        }
    }
    block
}

fn dtmf_tone_block(tone: DtmfTone) -> &'static [Sample; BLOCK_NUM_SAMPLES] {
    let (idx_lo, idx_hi) = get_dtmf_tone_indices(tone);
    DTMF_TONE_BLOCKS[idx_lo * 4 + idx_hi].get_or_init(|| synthesize_tone_block(idx_lo, idx_hi))
}

/// Mix in a DTMF tone onto an existing buffer
pub fn add_dtmf_tone(samples: &mut [Sample], tone: DtmfTone) {
    let (idx_lo, idx_hi) = get_dtmf_tone_indices(tone);
    let block = dtmf_tone_block(tone);

    let (head, tail) = samples.split_at_mut(block.len().min(samples.len()));
    for (out, &mixed) in head.iter_mut().zip(block.iter()) {
        *out += mixed;
    }

    // Buffers longer than one block continue the phase, as before.
    if !tail.is_empty() {
        let tone_amplitude = 0.5;
        let freq_amplitude = tone_amplitude / 2.;
        for freq_hz in [DTMF_FREQS_LO[idx_lo], DTMF_FREQS_HI[idx_hi]] {
            for (i, sample) in tail.iter_mut().enumerate() {
                let t = (i + BLOCK_NUM_SAMPLES) as f32 / SAMPLE_RATE as f32;
                let omega = 2. * std::f32::consts::PI * freq_hz;
                let float = f32::sin(t * omega);
                let fixed = (float * freq_amplitude * Sample::MAX as f32) as Sample;
                *sample += fixed;
            }
        }
    }
}
