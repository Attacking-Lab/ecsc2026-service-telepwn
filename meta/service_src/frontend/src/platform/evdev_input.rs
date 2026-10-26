use std::fs::{File, OpenOptions};
use std::io::{Cursor, ErrorKind::WouldBlock, Read};
use std::os::unix::fs::OpenOptionsExt;

use anyhow::Result;

use tracing::{debug, info, info_span};

use byteorder::{NativeEndian, ReadBytesExt};

use crate::input::KeyCode::*;
use crate::input::*;

// evdev API spec
// https://docs.kernel.org/input/input.html
const O_NONBLOCK: i32 = 0o4000;
const EV_KEY: u16 = 0x01;

#[cfg(target_pointer_width = "64")]
#[repr(C)]
struct CTimeval {
    tv_sec: u64,
    tv_usec: u64,
}

#[cfg(target_pointer_width = "32")]
#[repr(C)]
struct CTimeval {
    tv_sec: u32,
    tv_usec: u32,
}

#[repr(C)]
struct EvdevEvent {
    time: CTimeval,
    type_: u16,
    code: u16,
    value: i32,
}

pub struct EvdevInput {
    file: File,
}

impl EvdevInput {
    pub fn new() -> Result<Self> {
        let file = OpenOptions::new() //.
            .read(true)
            .custom_flags(O_NONBLOCK)
            .open("/dev/keypad0")?;

        Ok(EvdevInput { file })
    }

    fn map_key(evdev_code: u16, evdev_value: i32) -> Option<KeyCode> {
        // HACK: For most keys we only care about press, however for the hook
        // state, we want both press and release. Therefore we pass the value
        // along and then map press and release to different keycodes here.

        // value 0 -> release
        // value 1 -> press
        // value 2 -> repeat
        match (evdev_code, evdev_value) {
            (0x39, 1) => Some(Num(1)),
            (0x31, 1) => Some(Num(2)),
            (0x29, 1) => Some(Num(3)),
            (0x3a, 1) => Some(Num(4)),
            (0x32, 1) => Some(Num(5)),
            (0x2a, 1) => Some(Num(6)),
            (0x3b, 1) => Some(Num(7)),
            (0x33, 1) => Some(Num(8)),
            (0x2b, 1) => Some(Num(9)),
            (0x3d, 1) => Some(Num(0)),
            (0x3c, 1) => Some(Asterisk),
            (0x34, 1) => Some(Pound),

            (0x21, 1) => Some(Up),
            (0x22, 1) => Some(Left),
            (0x23, 1) => Some(Confirm),
            (0x24, 1) => Some(Right),
            (0x25, 1) => Some(Down),

            (0x2c, 1) => Some(Cancel),

            (0x35, 1) => Some(VolumeDown),
            (0x2d, 1) => Some(VolumeUp),

            (0x46, 1) => Some(Headset),
            (0x45, 1) => Some(Mute),
            (0x19, 1) => Some(Message),
            (0x1a, 1) => Some(Hold),
            (0x1b, 1) => Some(Redial),
            (0x1c, 1) => Some(Transfer),

            (0x1d, 1) => Some(Speaker),

            // Left line keys
            (0x3e, 1) => Some(Line(0)),
            (0x36, 1) => Some(Line(1)),
            (0x2e, 1) => Some(Line(2)),
            (0x26, 1) => Some(Line(3)),
            (0x1e, 1) => Some(Line(4)),

            // Right line keys
            (0x3f, 1) => Some(Line(5)),
            (0x37, 1) => Some(Line(6)),
            (0x2f, 1) => Some(Line(7)),
            (0x27, 1) => Some(Line(8)),
            (0x1f, 1) => Some(Line(9)),

            // Menu keys
            (0x44, 1) => Some(Menu(0)),
            (0x43, 1) => Some(Menu(1)),
            (0x42, 1) => Some(Menu(2)),
            (0x41, 1) => Some(Menu(3)),

            // Hook state
            (0x59, 1) => Some(OnHook(true)),
            (0x59, 0) => Some(OnHook(false)),

            _ => None,
        }
    }

    fn try_read_event(&mut self) -> Result<Option<EvdevEvent>> {
        let mut buffer = [0u8; std::mem::size_of::<EvdevEvent>()];
        match self.file.read_exact(&mut buffer) {
            Err(e) if e.kind() == WouldBlock => {
                return Ok(None);
            },
            r => r?,
        }

        let mut c = Cursor::new(&buffer);

        #[cfg(target_pointer_width = "64")]
        let (tv_sec, tv_usec) = (c.read_u64::<NativeEndian>()?, c.read_u64::<NativeEndian>()?);
        #[cfg(target_pointer_width = "32")]
        let (tv_sec, tv_usec) = (c.read_u32::<NativeEndian>()?, c.read_u32::<NativeEndian>()?);

        let type_ = c.read_u16::<NativeEndian>()?;
        let code = c.read_u16::<NativeEndian>()?;
        let value = c.read_i32::<NativeEndian>()?;

        Ok(Some(EvdevEvent { time: CTimeval { tv_sec, tv_usec }, type_, code, value }))
    }
}

impl Input for EvdevInput {
    fn poll_events(&mut self) -> Result<Vec<KeyCode>> {
        let mut result = vec![];
        while let Some(event) = self.try_read_event()? {
            // Is it a key event?
            if event.type_ != EV_KEY {
                continue;
            }

            // Do we know the code/value combo?
            if let Some(c) = Self::map_key(event.code, event.value) {
                result.push(c);
            }
        }
        Ok(result)
    }
}
