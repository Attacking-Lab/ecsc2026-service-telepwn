use std::fs::OpenOptions;

use memmap2::{MmapMut, MmapOptions};

use embedded_graphics::{
    pixelcolor::{raw::RawU16, Rgb565},
    prelude::*,
};

use anyhow::Result;

use crate::display::*;

pub struct FbdevDisplay {
    fb: MmapMut,
    buf: Vec<u8>,
}

impl FbdevDisplay {
    pub fn new() -> Result<FbdevDisplay, std::io::Error> {
        let fb_file = OpenOptions::new() //.
            .read(true)
            .write(true)
            .open("/dev/fb0")?;
        let fb = unsafe {
            MmapOptions::new() //.
                .len(DISP_HEIGHT * DISP_WIDTH * BYTES_PER_PIXEL)
                .map_mut(&fb_file)
        }?;

        let buf = vec![0; DISP_WIDTH * DISP_HEIGHT * BYTES_PER_PIXEL];
        Ok(FbdevDisplay { fb, buf })
    }
}

impl DrawTarget for FbdevDisplay {
    type Color = Rgb565;
    type Error = core::convert::Infallible;

    fn draw_iter<I>(&mut self, pixels: I) -> Result<(), Self::Error>
    where
        I: IntoIterator<Item = Pixel<Self::Color>>,
    {
        for Pixel(coord, color) in pixels.into_iter() {
            const WIDTH_I32: i32 = DISP_WIDTH as i32;
            const HEIGHT_I32: i32 = DISP_HEIGHT as i32;
            if let Ok((x @ 0..WIDTH_I32, y @ 0..HEIGHT_I32)) = coord.try_into() {
                // Rotate 180 degrees
                let flipped_y = HEIGHT_I32 - y - 1;
                let flipped_x = WIDTH_I32 - x - 1;

                let offset: usize =
                    ((flipped_y as usize) * DISP_WIDTH + (flipped_x as usize)) * BYTES_PER_PIXEL;

                let color_raw = RawU16::from(color).into_inner();
                let hi = ((color_raw >> 8) & 0xff) as u8;
                let lo = ((color_raw >> 0) & 0xff) as u8;
                self.buf[offset + 0] = lo;
                self.buf[offset + 1] = hi;
            }
        }
        Ok(())
    }
}

impl OriginDimensions for FbdevDisplay {
    fn size(&self) -> Size {
        Size::new(DISP_WIDTH as u32, DISP_HEIGHT as u32)
    }
}

impl Display for FbdevDisplay {
    fn flush(&mut self) -> Result<()> {
        self.fb.copy_from_slice(&self.buf);
        Ok(())
    }
}
