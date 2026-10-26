use std::collections::VecDeque;
use std::io::ErrorKind::WouldBlock;
use std::net::SocketAddr;
use std::net::TcpListener;
use std::net::TcpStream;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::thread;
use std::thread::{sleep, JoinHandle};
use std::time::{Duration, Instant};

use anyhow::Result;

use anyhow::anyhow;
use shared::net_audio::AsyncAudioStream;
use tracing::error;
use tracing::{info, warn};

use shared::dtmf::DtmfTone;

use crate::audio::*;
use crate::fax::FaxModem;
use crate::live_call::*;
use crate::paths;
use crate::platform::led::*;
use crate::ringtone_recorder::RingtoneRecorder;
use crate::util;

#[derive(Debug, Copy, Clone, PartialEq)]
pub enum Channel {
    Handset,
    Speaker,
}

pub struct AudioChannelDefinition {
    pub audio_in: Box<dyn AudioCaptureChannel + std::marker::Send + 'static>,
    pub audio_out: Box<dyn AudioPlayChannel + std::marker::Send + 'static>,
}

const MAX_HOLD_CALLS: usize = 10; // for the line LEDs

struct HoldCall {
    stream: AsyncAudioStream,
    hold_music_offset: usize,
}

pub struct CallManager {
    handset_channel: AudioChannelDefinition,
    speaker_channel: AudioChannelDefinition,

    handset_volume: i32,
    speaker_volume: i32,

    active_channel: Channel,
    live_call: Option<LiveCall>,
    hold_call_queue: VecDeque<HoldCall>,

    ringtone_data: Vec<Sample>,
    ringtone_offset: usize,

    hold_music_data: Vec<Sample>,

    last_error: Option<String>,

    mute: bool,
}

impl CallManager {
    pub fn new(
        handset_channel: AudioChannelDefinition,
        speaker_channel: AudioChannelDefinition,
        hold_music_data: Vec<Sample>,
    ) -> Result<Self> {
        Ok(Self {
            handset_channel,
            speaker_channel,

            handset_volume: 5,
            speaker_volume: 5,

            active_channel: Channel::Handset,
            live_call: None,
            hold_call_queue: VecDeque::new(),

            ringtone_data: vec![0; 3 * SAMPLE_RATE],
            ringtone_offset: 0,

            hold_music_data,

            last_error: None,

            mute: false,
        })
    }

    fn start_live_call(&mut self, stream: AsyncAudioStream) -> Result<()> {
        // Dialing out with an existing call ends the old call
        self.live_call = Some(LiveCall::new(stream, self.mute)?);

        // XXX: Do we want this LED here or in the UI?
        // Lower risk of desync here, but ugly...
        led_set(LedId::Status, true)?;

        Ok(())
    }

    fn end_live_call(&mut self) -> Result<Option<AsyncAudioStream>> {
        let live_call = self.live_call.take();
        let stream = live_call.map(|lc| lc.close());

        // XXX see start_live_call
        led_set(LedId::Status, false)?;

        Ok(stream)
    }

    pub fn dial_out(&mut self, addr: &str) -> Result<()> {
        info!(addr, "dial");

        let outbound_addr = addr.parse::<SocketAddr>()?;
        let outbound = AsyncAudioStream::new(
            TcpStream::connect_timeout(&outbound_addr, Duration::from_secs(15))?,
            Some(Duration::from_secs(15)),
        )?;
        self.start_live_call(outbound)?;

        Ok(())
    }

    pub fn accept_from_hold(&mut self) -> Result<()> {
        info!("accept from hold");

        if let Some(hold_call) = self.hold_call_queue.pop_front() {
            self.start_live_call(hold_call.stream)?;
        } else {
            warn!("no call on hold to accept");
        }

        Ok(())
    }

    pub fn reject_hold(&mut self) -> Result<()> {
        info!("reject on hold");

        if self.hold_call_queue.pop_front().is_none() {
            warn!("no call on hold to reject");
        }

        Ok(())
    }

    pub fn hang_up(&mut self) -> Result<()> {
        info!("hang up");
        self.end_live_call()?;

        Ok(())
    }

    pub fn put_back_on_hold(&mut self) -> Result<()> {
        info!("put back on hold");

        if let Some(stream) = self.end_live_call()? {
            self.hold_call_queue.push_back(HoldCall { stream, hold_music_offset: 0 });
        } else {
            warn!("no live call to put back on hold");
        }

        Ok(())
    }

    pub fn has_live_call(&self) -> Result<bool> {
        Ok(self.live_call.is_some())
    }

    pub fn live_call_send_dtmf(&mut self, tone: DtmfTone) -> Result<()> {
        if let Some(c) = self.live_call.as_mut() {
            c.queue_dtmf_tone(tone)?;
        } else {
            warn!("no live call to send DTMF");
        }

        Ok(())
    }

    pub fn get_active_channel(&self) -> Result<Channel> {
        Ok(self.active_channel)
    }

    pub fn set_active_channel(&mut self, channel: Channel) -> Result<()> {
        self.active_channel = channel;

        Ok(())
    }

    pub fn get_volume(&self, channel: Channel) -> i32 {
        match channel {
            Channel::Speaker => self.speaker_volume,
            Channel::Handset => self.handset_volume,
        }
    }

    pub fn set_volume(&mut self, channel: Channel, volume: i32) -> Result<()> {
        let (volume_ref, channel_ref) = match channel {
            Channel::Speaker => (&mut self.speaker_volume, &mut self.speaker_channel.audio_out),
            Channel::Handset => (&mut self.handset_volume, &mut self.handset_channel.audio_out),
        };

        // TODO: Clamp here or outside?
        let clamped_volume = volume.clamp(0, 10);
        *volume_ref = clamped_volume;
        channel_ref.set_volume(clamped_volume as f32 / 10.)?;

        Ok(())
    }

    pub fn num_hold_calls(&self) -> Result<usize> {
        Ok(self.hold_call_queue.len())
    }

    pub fn reload_ringtone(&mut self) -> Result<()> {
        let new_ringtone_audio = util::load_audio(Path::new(paths::ACTIVE_RINGTONE_PATH))?;
        if new_ringtone_audio.len() < BLOCK_NUM_SAMPLES {
            return Err(anyhow!("ringtone too short"));
        }
        self.ringtone_offset = 0;
        self.ringtone_data = new_ringtone_audio;

        info!(size = self.ringtone_data.len(), "reloaded ringtone");

        Ok(())
    }

    pub fn fax_modem(&mut self) -> Option<&mut FaxModem> {
        if let Some(c) = self.live_call.as_mut() {
            Some(c.fax_modem())
        } else {
            None
        }
    }

    pub fn ringtone_recorder(&mut self) -> Option<&mut RingtoneRecorder> {
        if let Some(c) = self.live_call.as_mut() {
            Some(c.ringtone_recorder())
        } else {
            None
        }
    }

    pub fn take_last_error(&mut self) -> Option<String> {
        self.last_error.take()
    }

    pub fn get_mute(&self) -> bool {
        self.mute
    }

    pub fn set_mute(&mut self, mute: bool) {
        self.mute = mute;

        if let Some(live_call) = self.live_call.as_mut() {
            live_call.set_mute(self.mute);
        }
    }
}

pub struct CallManagerThread {
    state: Arc<Mutex<CallManager>>,

    task_handle: Option<JoinHandle<()>>,
    task_running: Arc<AtomicBool>,
}

impl CallManagerThread {
    pub fn new(state: Arc<Mutex<CallManager>>) -> Result<Self> {
        Ok(Self { state, task_handle: None, task_running: Arc::new(AtomicBool::new(false)) })
    }

    pub fn start(&mut self) -> Result<()> {
        // TODO: Overkill ordering
        assert!(!self.task_running.load(Ordering::SeqCst));

        self.task_running.store(true, Ordering::SeqCst);
        let task_running_clone = self.task_running.clone();
        let state_clone = self.state.clone();
        self.task_handle = Some(thread::spawn(move || {
            // Listen
            // TODO: Do we want this to be on 0.0.0.0 for direct inbound?
            let listener = TcpListener::bind(format!("0.0.0.0:{}", shared::FRONTEND_PORT))
                .expect("failed to bind");
            listener.set_nonblocking(true).expect("failed to set_nonblocking");

            // Update 4x as fast as the buffers should be flying
            const TIME_PER_BUFFER: f32 = BLOCK_NUM_SAMPLES as f32 / SAMPLE_RATE as f32;
            const TARGET_UPDATE_INTERVAL: f32 = TIME_PER_BUFFER / 4.;

            while task_running_clone.load(Ordering::SeqCst) {
                let start = Instant::now();

                {
                    let mut state_guard = state_clone.lock().unwrap();
                    let state = &mut *state_guard;

                    // Check for new connections
                    match listener.accept() {
                        Ok((stream, _addr)) => {
                            // Is there room?
                            if state.hold_call_queue.len() >= MAX_HOLD_CALLS {
                                // Reject it.
                                warn!("new caller on hold but hold call queue has reached max length, rejecting!");
                            }
                            // Put it on hold
                            else if let Ok(async_stream) =
                                AsyncAudioStream::new(stream, Some(Duration::from_secs(15)))
                            {
                                state.hold_call_queue.push_back(HoldCall {
                                    stream: async_stream,
                                    hold_music_offset: 0,
                                });
                            } else {
                                error!("error while trying to turn stream into async audio stream");
                            }
                        },
                        Err(ref e) if e.kind() == WouldBlock => {
                            // No new connection
                        },
                        Err(e) => {
                            error!(err = ?e, "error while trying to accept connection");
                        },
                    }

                    // Kick out all dead calls
                    state.hold_call_queue.retain(|s| !s.stream.is_stream_dead());

                    // Play some hold music
                    let mut in_block = [0; BLOCK_NUM_SAMPLES];
                    for hold_call in &mut state.hold_call_queue {
                        // Wrap if necessary
                        // Do it at the start so we can tolerate a resize if we ever support swapping
                        // hold music out
                        if hold_call.hold_music_offset + BLOCK_NUM_SAMPLES
                            > state.hold_music_data.len()
                        {
                            hold_call.hold_music_offset = 0;
                        }

                        let p = hold_call.hold_music_offset;
                        let hold_block_ref: &[Sample; BLOCK_NUM_SAMPLES] =
                            &state.hold_music_data[p..p + BLOCK_NUM_SAMPLES].try_into().unwrap();

                        if let Ok(true) = hold_call.stream.recv_block_async(&mut in_block) {
                            // ignore result
                            let _ = hold_call.stream.send_block_async(hold_block_ref);
                            hold_call.hold_music_offset += BLOCK_NUM_SAMPLES;
                        }
                    }

                    // Is there a call in progress?
                    let mut is_ringing = false;
                    if let Some(live_call) = state.live_call.as_mut() {
                        // Get the channel we are going to use
                        let channel = match state.active_channel {
                            Channel::Speaker => &mut state.speaker_channel,
                            Channel::Handset => &mut state.handset_channel,
                        };

                        // Update the live call
                        let r =
                            live_call.update(channel.audio_in.as_mut(), channel.audio_out.as_mut());
                        if let Err(e) = r {
                            // TODO: Distinguish: EOF is fine, everything else is unexpected.
                            warn!(?e, "error while updating live call, hanging up");
                            state.last_error = Some(format!("live call error\n{:?}", e));
                            state.hang_up().unwrap();
                        }
                    } else {
                        // No call in progress. Should we be ringing the phone?
                        if !state.hold_call_queue.is_empty() {
                            // Get the speaker channel
                            let speaker_out = &mut state.speaker_channel.audio_out;

                            // Ring it
                            let ringtone_chunk = &state.ringtone_data
                                [state.ringtone_offset..state.ringtone_offset + BLOCK_NUM_SAMPLES];
                            let r = speaker_out.play_samples(ringtone_chunk);
                            let got = match r {
                                Err(e) => {
                                    warn!(?e, "error while trying to play ringtone");
                                    0
                                },
                                Ok(false) => 0,
                                Ok(true) => BLOCK_NUM_SAMPLES,
                            };
                            state.ringtone_offset += got;
                            is_ringing = true;

                            // One full ring cycle?
                            if state.ringtone_offset >= state.ringtone_data.len() {
                                // Reset the cycle
                                state.ringtone_offset = 0;

                                // Reject the first caller
                                state.reject_hold().unwrap();
                            }
                        }
                    }

                    // If not ringing, reset the ringtone offset
                    if !is_ringing {
                        state.ringtone_offset = 0;
                    }
                }

                // Wait until next tick
                let deadline = start + Duration::from_secs_f32(TARGET_UPDATE_INTERVAL);
                let remaining = deadline - Instant::now();
                sleep(remaining);
            }
        }));

        Ok(())
    }

    pub fn stop(&mut self) -> Result<()> {
        assert!(self.task_running.load(Ordering::SeqCst));

        self.task_running.store(false, Ordering::SeqCst);
        self.task_handle.take().expect("no join handle").join().expect("failed to join thread");

        Ok(())
    }
}
