use std::f32::consts::PI;

use crate::net_audio::{Sample, SAMPLE_RATE};

const SAMPLE_QN: usize = 15;
const COEFF_QN: usize = 14;

pub fn goertzel_coeff(freq_hz: f32) -> i32 {
    let omega = 2. * PI * freq_hz / SAMPLE_RATE as f32;
    let coeff_float = 2. * omega.cos();
    let coeff_fixed = (coeff_float * (1 << COEFF_QN) as f32).round() as i32;

    coeff_fixed
}

pub fn goertzel_mag_sq(samples: &[Sample], coeff: i32) -> i64 {
    // s in Q14
    let mut s1: i32 = 0; // s[i - 1]
    let mut s2: i32 = 0; // s[i - 2]
    for sample in samples.iter() {
        let x = (*sample as i32) << SAMPLE_QN;

        // Q2.14 * Q8.15
        let s1c = ((s1 as i64 * coeff as i64) >> COEFF_QN) as i32;
        let s0 = x + s1c - s2;
        s2 = s1;
        s1 = s0;
    }

    ((s1 as i64 * s1 as i64) >> SAMPLE_QN) + ((s2 as i64 * s2 as i64) >> SAMPLE_QN)
        - ((((s1 as i64 * s2 as i64) >> SAMPLE_QN) * coeff as i64) >> COEFF_QN)
}

pub fn goertzel_mag_sq_2(samples: &[Sample], coeff_a: i32, coeff_b: i32) -> (i64, i64) {
    let mut s1a: i32 = 0;
    let mut s2a: i32 = 0;
    let mut s1b: i32 = 0;
    let mut s2b: i32 = 0;
    for sample in samples.iter() {
        let x = (*sample as i32) << SAMPLE_QN;

        let s1ca = ((s1a as i64 * coeff_a as i64) >> COEFF_QN) as i32;
        let s0a = x + s1ca - s2a;
        s2a = s1a;
        s1a = s0a;

        let s1cb = ((s1b as i64 * coeff_b as i64) >> COEFF_QN) as i32;
        let s0b = x + s1cb - s2b;
        s2b = s1b;
        s1b = s0b;
    }

    let mag_a = ((s1a as i64 * s1a as i64) >> SAMPLE_QN) + ((s2a as i64 * s2a as i64) >> SAMPLE_QN)
        - ((((s1a as i64 * s2a as i64) >> SAMPLE_QN) * coeff_a as i64) >> COEFF_QN);
    let mag_b = ((s1b as i64 * s1b as i64) >> SAMPLE_QN) + ((s2b as i64 * s2b as i64) >> SAMPLE_QN)
        - ((((s1b as i64 * s2b as i64) >> SAMPLE_QN) * coeff_b as i64) >> COEFF_QN);
    (mag_a, mag_b)
}

pub fn goertzel_mag(samples: &[Sample], coeff: i32) -> i32 {
    let mag_squared = goertzel_mag_sq(samples, coeff);
    let mag = mag_squared.saturating_abs().isqrt() as i32;

    mag
}
