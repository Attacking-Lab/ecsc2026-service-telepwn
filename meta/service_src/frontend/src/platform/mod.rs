cfg_select! {
    target_arch = "arm" => {
        mod alsa_audio;
        mod evdev_input;
        mod fbdev_display;

        pub use alsa_audio::*;
        pub use evdev_input::EvdevInput;
        pub use fbdev_display::FbdevDisplay;
    },
    target_arch = "x86_64" => {
        mod desktop;
        pub use desktop::*;
    },
}

// TODO: Kinda ugly and not how the other modules are doing this but simpler...
pub mod led;
