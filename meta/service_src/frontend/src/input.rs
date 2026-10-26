use anyhow::Result;

#[allow(unused)]
#[derive(Debug)]
pub enum KeyCode {
    Num(i32),
    Asterisk,
    Pound,

    Up,
    Down,
    Left,
    Right,

    Confirm,
    Cancel,

    VolumeDown,
    VolumeUp,

    Headset,
    Mute,
    Message,
    Hold,
    Redial,
    Transfer,

    Speaker,

    Line(i32),
    Menu(i32),

    OnHook(bool),
}

pub trait Input {
    fn poll_events(&mut self) -> Result<Vec<KeyCode>>;
}
