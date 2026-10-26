use anyhow::Result;
use shared::dtmf::DtmfTone;

use std::{
    fs::{self, File},
    io::Read,
    path::{Path, PathBuf},
    sync::Mutex,
    time::{Duration, Instant},
};

use tracing::{error, info};

use embedded_graphics::{
    geometry::AnchorPoint,
    image::{Image, ImageRaw},
    mono_font::{ascii::FONT_10X20, MonoTextStyle},
    prelude::*,
    primitives::{PrimitiveStyle, Rectangle},
    text::{Alignment, Text},
};

use crate::{
    call_manager::*,
    config::*,
    paths::{ACTIVE_RINGTONE_PATH, RINGTONES_DIR},
};
use crate::{display::*, fax::FaxModemActivity};
use crate::{input::KeyCode, paths};
use crate::{platform::led::*, ringtone_recorder::RingtoneRecorderActivity};

const LOGO_WIDTH: usize = 48;
const LOGO_HEIGHT: usize = 32;

const MARGIN: usize = 10;

// SYNC: This needs to be matched to the baked in background color of the flag images.
const COL_BACKGROUND: FbColor = FbColor::BLACK; // fb_from_rgb888(0x10, 0x10, 0x10);
const COL_CURSOR: FbColor = FbColor::CSS_YELLOW;
const COL_TEXT: FbColor = FbColor::WHITE;

const COL_OUTLINE: FbColor = FbColor::CSS_GRAY;
const COL_ERROR: FbColor = FbColor::RED;
const COL_WARNING: FbColor = FbColor::YELLOW;

#[derive(Debug, Copy, Clone, PartialEq)]
enum IdlePage {
    MainMenu,
    TeamSelect,
    Reports(ReportPageKind),
}

#[derive(Debug, Copy, Clone, PartialEq)]
enum ReportPageKind {
    Old,
    New,
}

#[derive(Debug, Copy, Clone, PartialEq)]
enum CallPage {
    CallOverview,
    FaxOverview,
    RingtoneRecorder,
}

pub struct Ui<'a> {
    config: &'a Config,

    text_style: MonoTextStyle<'a, FbColor>,

    idle_page: IdlePage,

    team_cols: usize,
    team_rows: usize,

    team_cx: usize,
    team_cy: usize,

    call_page: CallPage,

    file_select_open: bool,
    file_select_position: usize,

    file_select_options: Vec<(String, PathBuf)>,
    file_select_result: Option<PathBuf>,

    wallpaper_data: Option<Vec<u8>>,

    modal: Option<(Instant, String, FbColor)>,

    report_page_dir: PathBuf,
    report_call_started: bool,
}

fn draw_cursor(display: &mut impl Display, bbox: &Rectangle) -> Result<()> {
    draw_outline(display, bbox, COL_CURSOR, 2)?;
    Ok(())
}

fn draw_outline(
    display: &mut impl Display,
    bbox: &Rectangle,
    color: FbColor,
    spacing: usize,
) -> Result<()> {
    let cursor_stroke = 3;
    let cursor_style = PrimitiveStyle::with_stroke(color, cursor_stroke);
    let expanded_bbox = bbox.resized(
        bbox.size + Size::new((spacing as u32) * cursor_stroke, (spacing as u32) * cursor_stroke),
        AnchorPoint::Center,
    );
    expanded_bbox.into_styled(cursor_style).draw(display)?;

    Ok(())
}

impl<'a> Ui<'a> {
    pub fn new(config: &'a Config) -> Result<Self> {
        Ok(Self {
            config,

            text_style: MonoTextStyle::new(&FONT_10X20, COL_TEXT),

            idle_page: IdlePage::MainMenu,

            team_cx: 0,
            team_cy: 0,
            team_cols: 8,
            team_rows: 6,

            call_page: CallPage::CallOverview,

            file_select_open: false,
            file_select_position: 0,
            file_select_options: vec![],
            file_select_result: None,

            wallpaper_data: None,

            modal: None,

            report_page_dir: PathBuf::new(),
            report_call_started: false,
        })
    }

    fn reload_wallpaper(&mut self) -> Result<()> {
        let mut file = File::open(paths::ACTIVE_WALLPAPER_PATH)?;
        let mut data = vec![];
        file.read_to_end(&mut data)?;
        info!(size = data.len(), "reloaded wallpaper");
        self.wallpaper_data = Some(data);
        Ok(())
    }

    fn get_selected_team(&self) -> Option<&TeamInfo> {
        let selected_team_index = self.team_cy * self.team_cols + self.team_cx;
        if selected_team_index >= self.config.team_infos.len() {
            return None;
        }

        Some(&self.config.team_infos[selected_team_index])
    }

    fn draw_menu_controls(&self, display: &mut impl Display, control_names: &[&str]) -> Result<()> {
        let button_width = DISP_WIDTH / 4;
        let button_y = DISP_HEIGHT - 8;
        for (i, control_name) in control_names.iter().enumerate() {
            let button_x = i * button_width + button_width / 2;
            let text = Text::with_alignment(
                control_name,
                Point::new(button_x as i32, button_y as i32),
                self.text_style,
                Alignment::Center,
            );
            text.draw(display)?;
        }
        Ok(())
    }

    fn update_main_menu(
        &mut self,
        display: &mut impl Display,
        key: Option<KeyCode>,
        call_manager: &Mutex<CallManager>,
    ) -> Result<()> {
        match key {
            Some(KeyCode::Menu(0)) => self.idle_page = IdlePage::TeamSelect,
            Some(KeyCode::Menu(1)) => self.load_report_page(ReportPageKind::New)?,
            Some(KeyCode::Menu(2)) => self.load_report_page(ReportPageKind::Old)?,
            Some(KeyCode::Menu(3)) => {
                // Reset default ringtone and wallpaper
                self.set_modal(
                    String::from("ringtone and wallpaper\nrestored to defaults"),
                    COL_OUTLINE,
                )?;
                if let Err(e) = std::fs::copy(
                    Path::new(
                        &self.config.team_infos[self.config.own_team_index].default_wallpaper_path,
                    ),
                    Path::new(paths::ACTIVE_WALLPAPER_PATH),
                ) {
                    self.set_modal(format!("error resetting wallpaper:\n{:?}", e), COL_ERROR)?;
                }
                if let Err(e) = std::fs::copy(
                    Path::new(
                        &self.config.team_infos[self.config.own_team_index].default_ringtone_path,
                    ),
                    Path::new(paths::ACTIVE_RINGTONE_PATH),
                ) {
                    self.set_modal(format!("error resetting ringtone:\n{:?}", e), COL_ERROR)?;
                }

                let _ = self.reload_wallpaper();
                let _ = call_manager.lock().unwrap().reload_ringtone();
            },
            _ => {},
        }

        // First time load
        if self.wallpaper_data.is_none() {
            if let Err(e) = self.reload_wallpaper() {
                error!(?e, "failed to load wallpaper")
            }
        }

        if let Some(wallpaper_data) = &self.wallpaper_data {
            let img_raw = ImageRaw::<FbColor>::new(wallpaper_data, DISP_WIDTH as u32);
            let img = Image::new(&img_raw, Point::zero());
            img.draw(display)?;
        }

        self.draw_menu_controls(display, &["dial", "new", "old", "reset"])?;

        Ok(())
    }

    fn update_team_selection(
        &mut self,
        display: &mut impl Display,
        key: Option<KeyCode>,
        call_manager: &Mutex<CallManager>,
    ) -> Result<()> {
        // Handle selection input
        if key.is_some() {
            info!(?key, "input");
        }
        let last_row_width = self.config.team_infos.len() % self.team_cols;
        match key {
            Some(KeyCode::Up) => {
                self.team_cy = if self.team_cy > 0 {
                    self.team_cy - 1
                } else {
                    if self.team_cx < last_row_width {
                        self.team_rows - 1
                    } else {
                        self.team_rows - 2
                    }
                }
            },
            Some(KeyCode::Down) => {
                self.team_cy = if self.team_cy < self.team_rows - 1
                    && !(self.team_cy == self.team_rows - 2 && self.team_cx >= last_row_width)
                {
                    self.team_cy + 1
                } else {
                    0
                }
            },
            Some(KeyCode::Left) => {
                self.team_cx = if self.team_cx > 0 {
                    self.team_cx - 1
                } else {
                    if self.team_cy != self.team_rows - 1 {
                        self.team_cols - 1
                    } else {
                        last_row_width - 1
                    }
                }
            },
            Some(KeyCode::Right) => {
                self.team_cx = if self.team_cx
                    < (if self.team_cy == self.team_rows - 1 {
                        last_row_width - 1
                    } else {
                        self.team_cols - 1
                    }) {
                    self.team_cx + 1
                } else {
                    0
                }
            },
            Some(KeyCode::Cancel) => {
                self.idle_page = IdlePage::MainMenu;
            },
            _ => {},
        }

        // TODO: do the table layout outside and separately ahead of time so that cursor management becomes
        // independent of drawing?
        let vertical_spacing = LOGO_HEIGHT + MARGIN / 2;
        let horizontal_spacing =
            (DISP_WIDTH - 2 * MARGIN - LOGO_WIDTH) as f32 / (self.team_cols - 1) as f32;
        for (i, team) in self.config.team_infos.iter().enumerate() {
            // Table layout
            let yi = i / self.team_cols;
            let xi = i % self.team_cols;

            let y = MARGIN + yi * vertical_spacing;
            let x = MARGIN + (xi as f32 * horizontal_spacing) as usize;

            //debug!(yi, xi, team.name, "draw team");

            // Draw logo
            // TODO: Endianness?
            let img_raw = ImageRaw::<FbColor>::new(&team.logo_data, LOGO_WIDTH as u32);
            let img = Image::new(&img_raw, Point { x: x as i32, y: y as i32 });
            img.draw(display)?;

            // Draw cursor
            if self.team_cx == xi && self.team_cy == yi {
                // Draw cursor
                draw_cursor(display, &img.bounding_box())?;
            }
        }

        // Draw team info
        // Figure out selected team...
        if let Some(team_info) = self.get_selected_team() {
            let x = MARGIN;
            let y = DISP_HEIGHT - 2 * MARGIN;

            // TODO: Better font, maybe profont
            let text_style = MonoTextStyle::new(&FONT_10X20, COL_TEXT);
            let team_ip = &team_info.ip;
            let text = format!("{} - {}", team_info.name, team_ip);
            Text::new(&text, Point::new(x as i32, y as i32), text_style).draw(display);

            if let Some(KeyCode::Confirm) = key {
                // Dial this team
                let addr = format!("{}:{}", team_ip, shared::BACKEND_PORT);
                if let Err(e) = call_manager.lock().unwrap().dial_out(&addr) {
                    error!(?e, "failed to dial team");
                    self.set_modal(format!("failed to dial\n{:?}", e), COL_ERROR)?;
                }
            }
        }

        Ok(())
    }

    fn load_report_page(&mut self, kind: ReportPageKind) -> Result<()> {
        self.idle_page = IdlePage::Reports(kind);

        let dir = match kind {
            ReportPageKind::New => PathBuf::from(&shared::NEW_REPORTS_DIR),
            ReportPageKind::Old => PathBuf::from(&shared::OLD_REPORTS_DIR),
        };

        self.file_select_start(&dir, false)?;
        self.report_page_dir = dir;
        self.report_call_started = false;

        Ok(())
    }

    fn update_reports(
        &mut self,
        _display: &mut impl Display,
        _key: Option<KeyCode>,
        call_manager: &Mutex<CallManager>,
    ) -> Result<()> {
        // Wait for file select menu to close
        if self.file_select_open {
            return Ok(());
        }

        // File select menu closed. Did the user pick an item?
        if let Some(path) = self.file_select_result.take() {
            // Get the ID to open in a second
            let id = path.file_name().expect("no file name").to_str().expect("cant put as string");

            if self.idle_page == IdlePage::Reports(ReportPageKind::New) {
                // Move to the old dir
                let old_path = PathBuf::from(shared::OLD_REPORTS_DIR).join(id);
                if let Err(e) = std::fs::rename(&path, old_path) {
                    error!(?e, "failed to move report to old");
                    self.set_modal(format!("failed to move report to old\n{:?}", e), COL_ERROR)?;
                }
            }

            let own_team_ip = &self.config.team_infos[self.config.own_team_index].ip;
            let addr = format!("{}:{}", own_team_ip, shared::BACKEND_PORT);
            {
                let mut cm = call_manager.lock().unwrap();

                if let Err(e) = cm.dial_out(&addr) {
                    error!(?e, "failed to dial team");
                    self.set_modal(format!("failed to dial\n{:?}", e), COL_ERROR)?;
                }

                // browse to the item
                cm.live_call_send_dtmf(DtmfTone::Num(1))?; // browse
                for digit in id.chars().filter_map(|c| c.to_digit(10)) {
                    cm.live_call_send_dtmf(DtmfTone::Num(digit as i32))?;
                }
                cm.live_call_send_dtmf(DtmfTone::Pound)?; // confirm
                cm.live_call_send_dtmf(DtmfTone::Num(2))?; // try to view full
                cm.live_call_send_dtmf(DtmfTone::Pound)?; // confirm empty pin
            }

            self.report_call_started = true;
        } else if self.report_call_started {
            // return from call
            self.file_select_start(&self.report_page_dir.clone(), false)?;
            self.report_call_started = false;
        } else {
            // User cancelled. Return to main menu
            self.idle_page = IdlePage::MainMenu;
        }

        Ok(())
    }

    fn update_call(
        &mut self,
        display: &mut impl Display,
        key: Option<KeyCode>,
        call_manager: &Mutex<CallManager>,
    ) -> Result<()> {
        let text_style = MonoTextStyle::new(&FONT_10X20, COL_TEXT);
        match self.call_page {
            CallPage::CallOverview => {
                // WIP graphics
                let in_progress_text = String::from("call in progress");
                /*{
                    if call_manager.lock().unwrap().get_mute() {
                        in_progress_text += "\nmuted";
                    }
                }*/
                Text::with_alignment(
                    &in_progress_text,
                    Point::new(DISP_WIDTH as i32 / 2, DISP_HEIGHT as i32 / 2),
                    text_style,
                    Alignment::Center,
                )
                .draw(display);

                match key {
                    Some(KeyCode::Message) => {
                        self.call_page = CallPage::FaxOverview;
                    },
                    Some(KeyCode::Redial) => {
                        self.call_page = CallPage::RingtoneRecorder;
                    },
                    Some(KeyCode::Cancel) => {
                        call_manager.lock().unwrap().hang_up()?;
                    },
                    _ => {},
                };
            },
            CallPage::FaxOverview => {
                // Draw fax controls
                let (state_string, state_send_available) = {
                    if let Some(modem) = call_manager.lock().unwrap().fax_modem() {
                        match modem.activity() {
                            FaxModemActivity::Idle => (String::from("send"), true),
                            FaxModemActivity::Transmitting => {
                                (format!("tx {}%", modem.progress_pct()), false)
                            },
                            FaxModemActivity::Receiving => {
                                (format!("rx {}%", modem.progress_pct()), false)
                            },
                        }
                    } else {
                        (String::from("invalid"), false)
                    }
                };
                let control_names = ["select", "load", "save", &state_string];
                self.draw_menu_controls(display, &control_names)?;

                // Fax input
                match key {
                    Some(KeyCode::Menu(0)) => {
                        self.file_select_start(Path::new(paths::WALLPAPERS_DIR), true)?;
                    },
                    Some(KeyCode::Menu(1)) => {
                        if let Some(path) = self.file_select_result.take() {
                            if let Some(Err(e)) = call_manager
                                .lock()
                                .unwrap()
                                .fax_modem()
                                .map(|fm| fm.load_image(&path))
                            {
                                self.set_modal(format!("failed to load\n{:?}", e), COL_ERROR)?;
                            }
                        } else {
                            self.set_modal("no file selected".into(), COL_WARNING)?;
                        }
                    },
                    Some(KeyCode::Menu(2)) => {
                        if let Some(path) = self.file_select_result.take() {
                            if let Some(Err(e)) = call_manager
                                .lock()
                                .unwrap()
                                .fax_modem()
                                .map(|fm| fm.save_image(&path))
                            {
                                self.set_modal(format!("failed to save\n{:?}", e), COL_ERROR)?;
                            } else {
                                self.set_modal(String::from("wallpaper saved"), COL_OUTLINE)?;

                                // in case we overwrote our wallpaper
                                if let Err(e) = self.reload_wallpaper() {
                                    error!(?e, "failed to reload wallpaper")
                                }
                            }
                        } else {
                            self.set_modal("no file selected".into(), COL_WARNING)?;
                        }
                    },
                    Some(KeyCode::Menu(3)) => {
                        // TODO: error handling
                        if state_send_available {
                            call_manager.lock().unwrap().fax_modem().map(|fm| fm.send_start());
                        }
                    },
                    Some(KeyCode::Message) => {
                        self.call_page = CallPage::CallOverview;
                    },
                    Some(KeyCode::Redial) => {
                        self.call_page = CallPage::RingtoneRecorder;
                    },
                    Some(KeyCode::Cancel) => {
                        call_manager.lock().unwrap().hang_up()?;
                    },
                    _ => {},
                }

                // Draw fax data
                {
                    call_manager.lock().unwrap().fax_modem().map(|fm| fm.draw(display));
                }
            },
            CallPage::RingtoneRecorder => {
                // Draw fax controls
                let (state_string, state_send_available) = {
                    if let Some(modem) = call_manager.lock().unwrap().ringtone_recorder() {
                        match modem.activity() {
                            RingtoneRecorderActivity::Idle => (String::from("play"), true),
                            RingtoneRecorderActivity::Transmitting => {
                                (format!("tx {}%", modem.progress_pct()), false)
                            },
                            RingtoneRecorderActivity::Receiving => {
                                (format!("rx {}%", modem.progress_pct()), false)
                            },
                        }
                    } else {
                        (String::from("invalid"), false)
                    }
                };
                let control_names = ["select", "load", "save", &state_string];
                self.draw_menu_controls(display, &control_names)?;

                // Fax input
                match key {
                    Some(KeyCode::Menu(0)) => {
                        self.file_select_start(Path::new(paths::RINGTONES_DIR), true)?;
                    },
                    Some(KeyCode::Menu(1)) => {
                        if let Some(path) = self.file_select_result.take() {
                            if let Some(Err(e)) = call_manager
                                .lock()
                                .unwrap()
                                .ringtone_recorder()
                                .map(|fm| fm.load_ringtone(&path))
                            {
                                self.set_modal(format!("failed to load\n{:?}", e), COL_ERROR)?;
                            }
                        } else {
                            self.set_modal("no file selected".into(), COL_WARNING)?;
                        }
                    },
                    Some(KeyCode::Menu(2)) => {
                        if let Some(path) = self.file_select_result.take() {
                            if let Some(Err(e)) = call_manager
                                .lock()
                                .unwrap()
                                .ringtone_recorder()
                                .map(|fm| fm.save_ringtone(&path))
                            {
                                self.set_modal(format!("failed to save\n{:?}", e), COL_ERROR)?;
                            } else {
                                self.set_modal(String::from("ringtone saved"), COL_OUTLINE)?;
                                if path == Path::new(ACTIVE_RINGTONE_PATH) {
                                    // Try to reload the ringtone
                                    if let Err(e) = call_manager.lock().unwrap().reload_ringtone() {
                                        self.set_modal(
                                            format!("failed to reload ringtone\n{:?}", e),
                                            COL_ERROR,
                                        )?;
                                    }
                                }
                            }
                        } else {
                            self.set_modal("no file selected".into(), COL_WARNING)?;
                        }
                    },
                    Some(KeyCode::Menu(3)) => {
                        // TODO: error handling
                        if state_send_available {
                            call_manager
                                .lock()
                                .unwrap()
                                .ringtone_recorder()
                                .map(|fm| fm.send_start());
                        }
                    },
                    Some(KeyCode::Message) => {
                        self.call_page = CallPage::FaxOverview;
                    },
                    Some(KeyCode::Redial) => {
                        self.call_page = CallPage::CallOverview;
                    },
                    Some(KeyCode::Cancel) => {
                        call_manager.lock().unwrap().hang_up()?;
                    },
                    _ => {},
                }

                // Draw fax data
                {
                    call_manager.lock().unwrap().ringtone_recorder().map(|fm| fm.draw(display));
                }
            },
        }

        Ok(())
    }

    fn file_select_start(&mut self, dir: &Path, allow_create: bool) -> Result<()> {
        self.file_select_open = true;
        self.file_select_options = fs::read_dir(dir)?
            .map(|res| {
                let path = res?.path();
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("").to_string();
                Ok((name, path))
            })
            .collect::<Result<Vec<_>, std::io::Error>>()?;
        self.file_select_options.sort();

        // Create new option
        if allow_create {
            let ts = chrono::Utc::now().format("%Y%m%d_%H%M%S").to_string();
            self.file_select_options.push((format!("<create> {}", ts), Path::new(dir).join(&ts)));
        }

        self.file_select_position = 0;
        Ok(())
    }

    fn update_file_select(
        &mut self,
        display: &mut impl Display,
        key: Option<KeyCode>,
    ) -> Result<()> {
        // input
        match key {
            Some(KeyCode::Up) => {
                if self.file_select_position > 0 {
                    self.file_select_position -= 1;
                }
            },
            Some(KeyCode::Down) => {
                if self.file_select_position < self.file_select_options.len() - 1 {
                    self.file_select_position += 1;
                }
            },
            Some(KeyCode::Cancel) => {
                self.file_select_open = false;
                self.file_select_result = None;
            },
            Some(KeyCode::Confirm) => {
                self.file_select_result =
                    if self.file_select_position < self.file_select_options.len() {
                        Some(self.file_select_options[self.file_select_position].1.clone())
                    } else {
                        None
                    };
                self.file_select_open = false;
            },
            _ => {},
        }

        // figure out page
        let entry_height = 22;
        let entries_per_page = (DISP_HEIGHT - 2 * MARGIN) / entry_height;
        let page_start = entries_per_page * (self.file_select_position / entries_per_page);

        for i in 0..entries_per_page {
            let option_idx = page_start + i;
            if option_idx >= self.file_select_options.len() {
                break;
            }

            let option_x = MARGIN;
            let option_y = MARGIN + entry_height * i + entry_height;
            let option = &self.file_select_options[option_idx];
            let text = Text::with_alignment(
                &option.0,
                Point::new(option_x as i32, option_y as i32),
                self.text_style,
                Alignment::Left,
            );
            text.draw(display)?;

            if option_idx == self.file_select_position {
                draw_cursor(display, &text.bounding_box())?;
            }
        }

        Ok(())
    }

    fn set_modal(&mut self, text: String, color: FbColor) -> Result<()> {
        // XXX we call now() manually here but not below
        self.modal = Some((Instant::now(), text, color));

        Ok(())
    }

    fn update_modal(&mut self, display: &mut impl Display) -> Result<()> {
        // XXX I'm sure there's some cleaner way to do this
        let mut remove = false;
        if let Some((set_time, modal_text, color)) = &self.modal {
            let elapsed = Instant::now() - *set_time;
            if elapsed > Duration::from_secs(3) {
                remove = true;
            } else {
                let text = Text::with_alignment(
                    modal_text,
                    Point::new(DISP_WIDTH as i32 / 2, DISP_HEIGHT as i32 / 2),
                    self.text_style,
                    Alignment::Center,
                );
                let bbox = text.bounding_box();
                let expanded_bbox =
                    bbox.resized(bbox.size + Size::new(10, 10), AnchorPoint::Center);
                display.fill_solid(&expanded_bbox, COL_BACKGROUND)?;
                draw_outline(display, &expanded_bbox, *color, 1)?;
                text.draw(display);
            }
        }
        if remove {
            self.modal = None;
        }
        Ok(())
    }

    pub fn update(
        &mut self,
        display: &mut impl Display,
        key: Option<KeyCode>,
        call_manager: &Mutex<CallManager>,
        led_manager: &mut LedManager,
    ) -> Result<()> {
        //debug!(key,"ui update");
        display.fill_solid(&display.bounding_box(), COL_BACKGROUND);

        // Global key handling
        match key {
            Some(KeyCode::Speaker) => {
                let mut cm = call_manager.lock().unwrap();
                let new_channel = match cm.get_active_channel()? {
                    Channel::Speaker => Channel::Handset,
                    Channel::Handset => Channel::Speaker,
                };
                cm.set_active_channel(new_channel)?;
            },
            Some(KeyCode::Headset) => {
                call_manager.lock().unwrap().accept_from_hold()?;
            },
            Some(KeyCode::Mute) => {
                call_manager.lock().unwrap().reject_hold()?;
            },
            Some(KeyCode::Hold) => {
                call_manager.lock().unwrap().put_back_on_hold()?;
            },
            Some(KeyCode::VolumeUp | KeyCode::VolumeDown) => {
                let delta = match key {
                    Some(KeyCode::VolumeUp) => 1,
                    Some(KeyCode::VolumeDown) => -1,
                    _ => panic!(),
                };

                let mut cm = call_manager.lock().unwrap();
                let chan = if !cm.has_live_call()? {
                    Channel::Speaker // ringtone
                } else {
                    cm.get_active_channel()?
                };
                let requested_volume = cm.get_volume(chan) + delta;
                cm.set_volume(chan, requested_volume)?;

                let chan_name = match chan {
                    Channel::Handset => "handset",
                    Channel::Speaker => "speaker",
                };
                self.set_modal(
                    format!("{} volume {}/10", chan_name, cm.get_volume(chan)),
                    COL_OUTLINE,
                )?;
            },
            _ => {},
        }

        // Global controls
        match key {
            Some(KeyCode::Transfer) => {
                let mut cm = call_manager.lock().unwrap();
                let old_mute = cm.get_mute();
                cm.set_mute(!old_mute);
                let new_mute = cm.get_mute();

                self.set_modal(format!("mute: {:?}", new_mute), COL_OUTLINE)?;
            },
            _ => {},
        }

        // Global call controls
        if call_manager.lock().unwrap().has_live_call()? {
            // Handle call-global inputs
            // NB: The call may be over by now or we might be on a different call...
            match key {
                Some(KeyCode::Num(n)) => {
                    call_manager.lock().unwrap().live_call_send_dtmf(DtmfTone::Num(n))?
                },
                Some(KeyCode::Asterisk) => {
                    call_manager.lock().unwrap().live_call_send_dtmf(DtmfTone::Asterisk)?
                },
                Some(KeyCode::Pound) => {
                    call_manager.lock().unwrap().live_call_send_dtmf(DtmfTone::Pound)?
                },
                Some(KeyCode::OnHook(true)) => {
                    let mut cm = call_manager.lock().unwrap();
                    if cm.get_active_channel()? == Channel::Handset {
                        cm.hang_up()?;
                    }
                },
                _ => {},
            }
        }

        if self.file_select_open {
            // File manager?
            self.update_file_select(display, key)?;
        } else if call_manager.lock().unwrap().has_live_call()? {
            // Active call?
            self.update_call(display, key, call_manager)?;
        } else {
            // Idle page
            match self.idle_page {
                IdlePage::MainMenu => self.update_main_menu(display, key, call_manager)?,
                IdlePage::Reports(_) => self.update_reports(display, key, call_manager)?,
                IdlePage::TeamSelect => self.update_team_selection(display, key, call_manager)?,
            };

            // reset call page for next call
            //self.call_page = CallPage::CallOverview;
        }

        {
            let mut cm = call_manager.lock().unwrap();

            if let Some(error_text) = cm.take_last_error() {
                self.set_modal(error_text, COL_ERROR)?;
            }
        }

        // Draw modal on top of everything
        self.update_modal(display)?;

        // Global status indicators
        // Do this at the end for the best latency
        {
            let cm = call_manager.lock().unwrap();
            let num_hold_calls = cm.num_hold_calls()?;

            led_manager.set(LedId::Headset, num_hold_calls > 0)?;
            led_manager.set(LedId::Mute, num_hold_calls > 0)?;
            led_manager.set(LedId::Speaker, cm.get_active_channel()? == Channel::Speaker)?;

            // Set line LEDs to indicate queue length
            for i in 0..10 {
                led_manager.set(LedId::LineGreen(i), i < num_hold_calls)?;
            }
        }

        Ok(())
    }
}
