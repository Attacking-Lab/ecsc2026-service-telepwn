use std::cell::RefCell;

use embedded_graphics::{pixelcolor::Rgb565, prelude::*};
use embedded_graphics_simulator::{
    sdl2, OutputSettingsBuilder, SimulatorDisplay, SimulatorEvent, Window,
};

use anyhow::Result;

use crate::{display::*, input::*};

pub struct DesktopPlatform {
    window: RefCell<Window>,
    display: RefCell<SimulatorDisplay<Rgb565>>,
}

pub struct DesktopInput<'a> {
    platform: &'a DesktopPlatform,
}

pub struct DesktopDisplay<'a> {
    platform: &'a DesktopPlatform,
}

impl DesktopPlatform {
    pub fn new() -> Result<Self> {
        let display: SimulatorDisplay<Rgb565> =
            SimulatorDisplay::new(Size::new(DISP_WIDTH as u32, DISP_HEIGHT as u32));

        let output_settings = OutputSettingsBuilder::new().scale(2).build();
        let window = Window::new("Telepwn Frontend", &output_settings); // .show_static(&display)

        Ok(DesktopPlatform { display: RefCell::new(display), window: RefCell::new(window) })
    }

    pub fn systems(&self) -> (DesktopDisplay<'_>, DesktopInput<'_>) {
        (DesktopDisplay { platform: self }, DesktopInput { platform: self })
    }
}

impl DrawTarget for DesktopDisplay<'_> {
    type Color = Rgb565;
    type Error = core::convert::Infallible;

    // TODO: consider declarative macro
    fn draw_iter<I>(&mut self, pixels: I) -> Result<(), Self::Error>
    where
        I: IntoIterator<Item = Pixel<Self::Color>>,
    {
        let mut display = self.platform.display.borrow_mut();
        display.draw_iter(pixels)?;

        Ok(())
    }

    fn fill_contiguous<I>(
        &mut self,
        area: &embedded_graphics::primitives::Rectangle,
        colors: I,
    ) -> Result<(), Self::Error>
    where
        I: IntoIterator<Item = Self::Color>,
    {
        let mut display = self.platform.display.borrow_mut();
        display.fill_contiguous(area, colors)?;

        Ok(())
    }

    fn fill_solid(
        &mut self,
        area: &embedded_graphics::primitives::Rectangle,
        color: Self::Color,
    ) -> Result<(), Self::Error> {
        let mut display = self.platform.display.borrow_mut();
        display.fill_solid(area, color)?;

        Ok(())
    }
}

impl OriginDimensions for DesktopDisplay<'_> {
    fn size(&self) -> Size {
        self.platform.display.borrow().size()
    }
}

impl Display for DesktopDisplay<'_> {
    fn flush(&mut self) -> Result<()> {
        let mut window = self.platform.window.borrow_mut();
        let mut display = self.platform.display.borrow_mut();
        window.update(&display);

        Ok(())
    }
}

impl DesktopInput<'_> {
    fn map_key(c: sdl2::Keycode) -> Option<KeyCode> {
        match c {
            sdl2::Keycode::NUM_0 => Some(KeyCode::Num(0)),
            sdl2::Keycode::NUM_1 => Some(KeyCode::Num(1)),
            sdl2::Keycode::NUM_2 => Some(KeyCode::Num(2)),
            sdl2::Keycode::NUM_3 => Some(KeyCode::Num(3)),
            sdl2::Keycode::NUM_4 => Some(KeyCode::Num(4)),
            sdl2::Keycode::NUM_5 => Some(KeyCode::Num(5)),
            sdl2::Keycode::NUM_6 => Some(KeyCode::Num(6)),
            sdl2::Keycode::NUM_7 => Some(KeyCode::Num(7)),
            sdl2::Keycode::NUM_8 => Some(KeyCode::Num(8)),
            sdl2::Keycode::NUM_9 => Some(KeyCode::Num(9)),
            sdl2::Keycode::ASTERISK => Some(KeyCode::Asterisk),
            sdl2::Keycode::HASH => Some(KeyCode::Pound),
            sdl2::Keycode::UP => Some(KeyCode::Up),
            sdl2::Keycode::DOWN => Some(KeyCode::Down),
            sdl2::Keycode::LEFT => Some(KeyCode::Left),
            sdl2::Keycode::RIGHT => Some(KeyCode::Right),
            sdl2::Keycode::RETURN => Some(KeyCode::Confirm),
            _ => None,
        }
    }
}

impl Input for DesktopInput<'_> {
    fn poll_events(&mut self) -> Result<Vec<KeyCode>> {
        let mut results = vec![];
        let window = self.platform.window.borrow();
        for event in window.events() {
            match event {
                SimulatorEvent::KeyDown { keycode, .. } => {
                    if let Some(code) = Self::map_key(keycode) {
                        results.push(code);
                    }
                },
                _ => {},
            }
        }
        Ok(results)
    }
}
