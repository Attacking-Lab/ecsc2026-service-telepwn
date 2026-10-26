# Telepwn

Welcome to Telepwn! This challenge runs in part on a Yealink T46S phone. The phone exposes telnet on port 23 for your benefit. For your convenience, a wallpaper and ringtone management service is also available on port 80. The backend service is hosted on the vulnbox on port 9400. The network protocol is TCP blocks of length 1000 bytes containing signed 8-bit samples running at an overall sampling rate of 8000 Hz (meaning 8 blocks per second for real-time calls). Fax start must be aligned to a block boundary.

Key map:
| Key         | Action |
|-------------|--------|
| ←↑→↓        | navigate |
| OK          | confirm / dial |
| X           | cancel / hang up |
| Message     | fax |
| Redial      | ringtone recorder |
| Speaker     | switch handset/speaker |
| Headset     | accept incoming call |
| Mute        | reject incoming call |
| Hold        | put call back on hold |
| Transfer    | toggle mute |
| Volume      | increase/decrease volume |

The headset/mute LED indicates whether there is an incoming call. The green line LEDs on the sides of screen indicate how many callers are currently on hold. The red status LED indicates if there is a live call.

## Main menu
* dial: dial a team
* new: new customer service reports
* old: old customer service reports
* reset: reset your wallpaper and ringtone to the defaults

## How to send a fax
* start a call
* press the Message key to open the fax menu
* press the select button to select a wallpaper
* press load to load it from disk
* press send and wait for progress to reach 100%

## How to receive a fax
* press the Message key to open the fax menu
* wait for a fax
* fax will be automatically detected and decoded

## How to set your wallpaper
* receive a fax
* select active_wallpaper.rgb565
* press save

## How to set your ringtone
* press the Redial key to open the ringtone menu
* unlock a ringtone
* ringtone will automatically be recorded
* select active_ringtone.pcm
* press save