telepwn
============

Authors:
* Liikt
* PistonMiner

Categories:
* rev
* dsp

Overview
--------

telepwn is a service which exposes and automated phone systems for storing and retrieving ringtones and wallpapers. The service is split in two: the backend runs on the vulnbox and hosts the automated phone system. The frontend runs on a (physical) phone and represents the reference client as well as exposing "customer support" functionality which lets teams call each other and exchange ringtones and wallpapers. Both components are written in Rust. Communication happens via TCP, with signed 8-bit PCM samples at 8000 Hz.

### Flag Store 1

Ringtones can be stored and retrieved in the service. They are protected by a PIN. It is possible to browse and preview arbitrary ringtones, however only the first three seconds of the ringtone are part of the preview. The flag is stored as text-to-speech characters, at a speed where this preview exposes only 10 characters, which is not sufficient to be able to guess the rest of the flag given the flag format.

Attack info is the ringtone ID.

### Flag Store 2

Wallpapers of 120x60 pixels can be stored and retrieved in the service. They also are protected by PIN and also have a preview available. The preview is a 10x subsampled version of the same image with 12x6 pixels. The flag is stored as written characters, such that the preview pixel for each 10x10 block is always white, thus not leaking anything about the flag. Wallpapers further expose a trading feature, where wallpapers can be offered in trade to each other, previewed, and if accepted, be made available in full to both trading partners.

Attack info is the wallpaper ID.

Vulnerabilities
---------------

### Flag Store 1, Vuln 1

Whenever audio is played back, the whole audio is loaded into a 15-second memory buffer allocated for each connection for this purpose, after which no disk access is necessary for that clip. While the preview is only three seconds long, the whole ringtone is loaded into the buffer regardless. It is possible to skip the playback of menu voicelines by pressing a key early. If a keypress is already queued when a clip attempts to start playing, loading the new clip is skipped. This allows preserving the previous buffer contents by navigating through menus quickly, however a method of leaking the buffer contents is required.

The `recv_buffer_until_key` function is what is used to record new ringtones and is intended to receive samples until a keypress. However, it has a bug: if a keypress is already queued when the function starts, it will not set the buffer to size zero, but instead preserve what was in the buffer before. This can be used to create a ringtone out of the current buffer contents.

Putting it all together: by previewing a ringtone, then while the preview is playing queueing the key presses to navigate from the preview menu into the "create ringtone" menu and immediately start and stop recording, a new ringtone is created with the full audio of the one previewed. This ringtone can then simply by listened to in full using the PIN just entered.

* Category: rev
* Difficulty: hard
* Discoverability: hard
* Patchability: easy

### Flag Store 2, Vuln 1

Wallpapers permit trading. Every wallpaper can only have one outgoing trade request at a time. When previewing a trade offer, only the offered item ID is kept in memory, to display the item preview. When accepting the trade offer, the requested item is then reloaded by checking the currently requested trade of the offered item, and the requested item receives the offered item as an accepted trade and vice versa. This is susceptible to a time-of-check/time-of-use (TOCTOU) issue: by creating a trade offer to a controlled account, previewing that trade on a second connection, then changing the trade offer to point at the flag instead, and finally accepting the still-open previewed trade, a trade for the flag can be forced to be accepted instead.

* Category: rev
* Difficulty: hard
* Discoverability: hard
* Patchability: medium

Patches
-------

### Flag Store 1, Vuln 1

The issue can be patched by ensuring that the `self.buffer_used` variable is always set to zero at the start of `recv_buffer_until_key`.

### Flag Store 2, Vuln 1

The issue can be patched by keeping the requested item ID in memory and using it when resolving the trade instead of reloading it from disk.

License
-------

This project is released under the MIT License. See [LICENSE](LICENSE).