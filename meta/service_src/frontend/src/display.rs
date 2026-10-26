use anyhow::Result;

use embedded_graphics::{pixelcolor::Rgb565, prelude::*};

pub const DISP_WIDTH: usize = 480;
pub const DISP_HEIGHT: usize = 272;
pub const BYTES_PER_PIXEL: usize = 2;

pub type FbColor = Rgb565;

pub const fn fb_from_rgb888(r: u8, g: u8, b: u8) -> FbColor {
    Rgb565::new(r >> 3, g >> 2, b >> 3)
}

pub trait Display:
    DrawTarget<Color = FbColor, Error = core::convert::Infallible> + OriginDimensions
{
    fn flush(&mut self) -> Result<()> {
        Ok(())
    }
}
//impl<T> Display for T where T: DrawTarget<Color = Rgb565, Error = core::convert::Infallible> {}
