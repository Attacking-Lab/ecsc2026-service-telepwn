use std::fs::File;
use std::io::ErrorKind::Other;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::Path;
use std::time::Duration;

use shared::dtmf::*;
use shared::net_audio::*;

use tracing::{debug, instrument, trace, warn};

// O_NOATIME is refused with EPERM for files we don't own.
pub fn open_read(path: &Path) -> Result<File, std::io::Error> {
    use std::os::unix::fs::OpenOptionsExt;
    match std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOATIME).open(path) {
        Err(e) if e.raw_os_error() == Some(libc::EPERM) => File::open(path),
        r => r,
    }
}

pub const BUFFER_NUM_SAMPLES: usize = 15 * SAMPLE_RATE;
pub struct AudioContext {
    stream: SyncAudioStream,
    dtmf: DtmfScanner,
    buffer: Box<[Sample; BUFFER_NUM_SAMPLES]>,
    buffer_used: usize,
}

impl AudioContext {
    pub fn new(stream: TcpStream) -> Result<AudioContext, std::io::Error> {
        Ok(AudioContext {
            stream: SyncAudioStream::new(stream, Some(Duration::from_secs(30)))?,
            dtmf: DtmfScanner::new()?,
            buffer: vec![0; BUFFER_NUM_SAMPLES].try_into().unwrap(),
            buffer_used: 0,
        })
    }

    pub fn close(self) -> TcpStream {
        self.stream.close()
    }

    //#[instrument(level = "debug", skip(self))]
    fn send_file_until_key(&mut self, path: &Path) -> Result<(), std::io::Error> {
        let mut in_block = [0 as Sample; BLOCK_NUM_SAMPLES];
        let mut p = 0;
        loop {
            trace!(p);

            // Is there a key input? Then we are done.
            if self.dtmf.has_tone() {
                break;
            }

            // Load buffer if necessary
            if p == 0 {
                self.load_buffer_from_path(path)?;
            }

            // Are we done?
            if p >= self.buffer_used {
                break;
            }

            // Transfer a block
            let out_block_ref: &[Sample; BLOCK_NUM_SAMPLES] =
                (&self.buffer[p..p + BLOCK_NUM_SAMPLES]).try_into().unwrap();
            self.stream.transfer_block(out_block_ref, &mut in_block)?;
            self.dtmf.scan_block_for_dtmf(&in_block);
            p += BLOCK_NUM_SAMPLES;
        }

        Ok(())
    }

    pub fn send_clip_until_key(&mut self, name: &str) -> Result<(), std::io::Error> {
        let mut path = String::new();
        path += "lines/";
        path += name;
        path += ".pcm";

        self.send_file_until_key(Path::new(&path))
    }

    //#[instrument(level = "debug", skip(self), ret)]
    pub fn recv_dtmf_key(&mut self) -> Result<DtmfTone, std::io::Error> {
        let out_block = [0 as Sample; BLOCK_NUM_SAMPLES];

        loop {
            // Do we have a key?
            if let Some(tone) = self.dtmf.pop_tone() {
                return Ok(tone);
            }

            // No keys, but we need one. Read a block and respond with silence
            let mut in_block = [0 as Sample; BLOCK_NUM_SAMPLES];
            self.stream.transfer_block(&out_block, &mut in_block)?;
            self.dtmf.scan_block_for_dtmf(&in_block);
        }
    }

    //#[instrument(level = "debug", skip(self))]
    pub fn send_dtmf_key(&mut self, tone: DtmfTone) -> Result<(), std::io::Error> {
        // Modulate
        let mut out_block = [0 as Sample; BLOCK_NUM_SAMPLES];
        add_dtmf_tone(&mut out_block, tone);

        // Transfer
        let mut in_block = [0 as Sample; BLOCK_NUM_SAMPLES];
        self.stream.transfer_block(&out_block, &mut in_block)?;
        self.dtmf.scan_block_for_dtmf(&in_block); // XXX kinda optional

        Ok(())
    }

    //#[instrument(level = "debug", skip(self))]
    pub fn recv_number_or_cancel(&mut self) -> Result<Option<u64>, std::io::Error> {
        let mut num = 0u64;
        loop {
            let digit = match self.recv_dtmf_key()? {
                DtmfTone::Num(n @ 0..=9) => n,
                DtmfTone::Asterisk => return Ok(None),
                DtmfTone::Pound => break,
                _ => 0, // Invalid digit
            };
            num = num * 10 + (digit as u64);
        }

        Ok(Some(num))
    }

    pub fn load_buffer_from_path(&mut self, path: &Path) -> Result<(), std::io::Error> {
        // Read
        let mut file = open_read(path)?;
        let end = file.read(samples_as_raw_mut(&mut *self.buffer))?;

        // Pad
        let padded_end = end.div_ceil(BLOCK_NUM_SAMPLES) * BLOCK_NUM_SAMPLES;
        self.buffer[end..padded_end].fill(0);
        self.buffer_used = padded_end;

        debug!(?path, buffer_used = self.buffer_used, "load_buffer_from_path");

        Ok(())
    }

    pub fn save_buffer_to_path(&mut self, path: &Path) -> Result<(), std::io::Error> {
        let mut file = File::create_new(path)?;
        let written = file.write(samples_as_raw(&self.buffer[0..self.buffer_used]))?;
        if written != self.buffer_used {
            return Err(std::io::Error::new(Other, "not all data written from buffer"));
        }

        Ok(())
    }

    //#[instrument(level = "debug", skip(self))]
    pub fn recv_buffer_until_key(&mut self) -> Result<(), std::io::Error> {
        let out_block = [0 as Sample; BLOCK_NUM_SAMPLES];

        let mut p = 0;
        loop {
            trace!(p);

            // Is there a key input? Then we are done.
            // FIXME: we consume queued keys here, but only one per block... kinda weird...
            match self.dtmf.pop_tone() {
                Some(DtmfTone::Pound) => break,
                _ => (),
            }

            // No input. Commit the previous block if it exists
            if p > 0 {
                self.buffer_used = p;
            }

            // Are we full?
            if p == BUFFER_NUM_SAMPLES {
                break;
            }

            // Read a block and respond with silence
            // Note that this reads into the buffer misaligned from buffer_used.
            // It will be fixed at the start of the next iteration of the loop.
            let in_block_ref: &mut [Sample; BLOCK_NUM_SAMPLES] =
                (&mut self.buffer[p..p + BLOCK_NUM_SAMPLES]).try_into().unwrap();
            self.stream.transfer_block(&out_block, in_block_ref)?;
            self.dtmf.scan_block_for_dtmf(in_block_ref);
            p += BLOCK_NUM_SAMPLES;
        }

        Ok(())
    }

    pub fn send_buffer(&mut self) -> Result<(), std::io::Error> {
        self.send_buffer_with_limit(BUFFER_NUM_SAMPLES)
    }

    //#[instrument(level = "debug", skip(self))]
    pub fn send_buffer_with_limit(&mut self, limit: usize) -> Result<(), std::io::Error> {
        let count = std::cmp::min(self.buffer_used, limit);

        let mut in_block = [0 as Sample; BLOCK_NUM_SAMPLES];
        let mut p = 0;
        loop {
            // Are we done?
            if p >= count {
                break;
            }

            // Send a block
            let out_block_ref: &[Sample; BLOCK_NUM_SAMPLES] =
                &self.buffer[p..p + BLOCK_NUM_SAMPLES].try_into().unwrap();
            self.stream.transfer_block(out_block_ref, &mut in_block)?;
            self.dtmf.scan_block_for_dtmf(&in_block);
            p += BLOCK_NUM_SAMPLES;
        }

        Ok(())
    }

    pub fn downsample_fax_buffer(&mut self) -> Result<(), std::io::Error> {
        // SYNC with fax.rs
        const PIXEL_NUM_SAMPLES: usize = 8;
        let input_width = 120;
        let input_height = 60;
        let downsampling = 10;

        let mut p = 0;
        'outer: for y in (0..input_height).step_by(downsampling) {
            for x in (0..input_width).step_by(downsampling) {
                let src_offset = (y * input_width + x) * PIXEL_NUM_SAMPLES;
                if src_offset + PIXEL_NUM_SAMPLES >= self.buffer_used {
                    warn!("fax downsampling terminated early, buffer too short");
                    break 'outer;
                }
                self.buffer.copy_within(src_offset..src_offset + PIXEL_NUM_SAMPLES, p);
                p += PIXEL_NUM_SAMPLES;
            }
        }
        self.buffer_used = p;

        Ok(())
    }

    pub fn buffer_used(&self) -> usize {
        self.buffer_used
    }
}
