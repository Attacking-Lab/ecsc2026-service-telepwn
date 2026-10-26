use anyhow::Result;

pub use shared::net_audio::{Sample, BLOCK_NUM_SAMPLES, SAMPLE_RATE};

pub trait AudioPlayChannel {
    fn play_samples(&mut self, in_samples: &[Sample]) -> Result<bool>;
    fn set_volume(&mut self, _volume: f32) -> Result<()> {
        Ok(())
    }
}

pub trait AudioCaptureChannel {
    fn capture_samples(&mut self, out_samples: &mut [Sample]) -> Result<bool>;
    fn set_volume(&mut self, _volume: f32) -> Result<()> {
        Ok(())
    }
}
