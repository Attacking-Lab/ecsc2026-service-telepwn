use std::{collections::VecDeque, net::TcpStream};

use anyhow::Result;

use shared::dtmf::*;
use shared::net_audio::*;

use crate::audio::*;
use crate::fax::FaxModem;
use crate::ringtone_recorder::RingtoneRecorder;

pub struct LiveCall {
    //current_latency: usize, // TODO: Do we need this?
    stream: AsyncAudioStream,
    samples_out_mix_queue: VecDeque<Sample>,

    fax_modem: FaxModem,
    ringtone_recorder: RingtoneRecorder,

    mute: bool,
}

impl LiveCall {
    pub fn new(stream: AsyncAudioStream, initial_mute: bool) -> Result<Self> {
        Ok(Self {
            stream,
            samples_out_mix_queue: VecDeque::new(),
            fax_modem: FaxModem::new()?,
            ringtone_recorder: RingtoneRecorder::new()?,
            mute: initial_mute,
        })
    }

    pub fn set_mute(&mut self, mute: bool) {
        self.mute = mute;
    }

    pub fn close(self) -> AsyncAudioStream {
        self.stream
    }

    pub fn queue_dtmf_tone(&mut self, tone: DtmfTone) -> Result<()> {
        let mut samples = [0; BLOCK_NUM_SAMPLES];
        add_dtmf_tone(&mut samples[0..BLOCK_NUM_SAMPLES / 2], tone);
        self.queue_mix_samples(&samples)?;

        Ok(())
    }

    pub fn queue_mix_samples(&mut self, samples: &[Sample]) -> Result<()> {
        self.samples_out_mix_queue.extend(samples);

        Ok(())
    }

    pub fn fax_modem(&mut self) -> &mut FaxModem {
        &mut self.fax_modem
    }

    pub fn ringtone_recorder(&mut self) -> &mut RingtoneRecorder {
        &mut self.ringtone_recorder
    }

    pub fn update(
        &mut self,
        audio_in: &mut (impl AudioCaptureChannel + ?Sized),
        audio_out: &mut (impl AudioPlayChannel + ?Sized),
    ) -> Result<()> {
        //debug!("capture_samples");

        // Try to read a block from the microphone
        let mut out_block = [0; BLOCK_NUM_SAMPLES];
        let got_tx = audio_in.capture_samples(&mut out_block)?;

        //debug!(got_tx, "capture_samples done");

        if got_tx {
            // Are we muted?
            if self.mute {
                out_block.fill(0);
            }

            // Do we have some samples to mix in?
            //debug!("mix");
            let num_samples_mix = std::cmp::min(out_block.len(), self.samples_out_mix_queue.len());
            for i in 0..num_samples_mix {
                // XXX: Add or overwrite? Overwrite seems safer...
                out_block[i] = self.samples_out_mix_queue[i];
            }
            if num_samples_mix > 0 {
                //debug!(num_samples_mix, "mix samples");
                //debug!(?out_block);
            }
            self.samples_out_mix_queue
                .retain_back(self.samples_out_mix_queue.len() - num_samples_mix);

            // Pass it through the ringtone recorder
            self.ringtone_recorder.send_block(&mut out_block)?;

            // Pass it through the fax modem
            self.fax_modem.send_block(&mut out_block)?;

            // Send it
            // If this fails, we just ignore it
            let _send_success = self.stream.send_block_async(&out_block)?;
        }

        // Try to read a block from the network
        let mut in_block = [0; BLOCK_NUM_SAMPLES];
        let got_rx = self.stream.recv_block_async(&mut in_block)?;

        // Did we get something?
        if got_rx {
            // Play it
            // If this returns false, we just ignore it.
            let _play_success = audio_out.play_samples(&in_block)?;

            // Pass it through the fax modem
            self.ringtone_recorder.recv_block(&in_block)?;

            // Pass it through the fax modem
            self.fax_modem.recv_block(&in_block)?;
        }

        Ok(())
    }
}
