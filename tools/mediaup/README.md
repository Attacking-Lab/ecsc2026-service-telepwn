# mediaup

Tiny std-only HTTP server that runs **on the phone** and serves a one-page web app
for replacing the phone's **wallpaper** and **ringtone**. Upload a **.png** / **.wav**,
or download the current one back as **.png** / **.wav**. All the heavy lifting —
image decode/scale/quantize (and back), audio decode/resample/requantize (and back) —
happens **in the browser**, as do the file naming and the format checks; the phone
only reads/writes the raw file.

## Why

The phone is a single-core ARMv6 with ~12 MB RAM. Decoding a JPEG or resampling an
MP3 there is painful. Instead the browser produces the exact on-disk format and
uploads raw bytes; the server is a dumb, fixed-format sink.

## Wire formats (produced by the browser, written verbatim by the server)

- **Wallpaper** — 480×240 (the browser scales the PNG to fill this size, centred,
  cropping any overflow; nearest-neighbour when upscaling, smoothed when
  downscaling), RGB565, **big-endian**, row-major, top-left origin. Exactly
  `480*240*2 = 230400` bytes. This is what the frontend's `ImageRaw::<Rgb565>`
  consumes.
- **Ringtone** — mono, **8000 Hz**, signed **8-bit** PCM
  (`i8`). Length must be a non-zero multiple of `1000` samples (the frontend's
  `BLOCK_NUM_SAMPLES`), capped at 480000 samples (60 s).

## Endpoints

- `GET /` / `GET /app.js` — the embedded web app.
- `GET /api/info` — JSON of the expected dimensions/sizes.
- `POST /api/wallpaper?name=<name>` — raw RGB565 body, stored verbatim as `<name>`.
- `POST /api/ringtone?name=<name>` — raw i8 PCM body, stored verbatim as `<name>`.
- `GET /api/wallpapers` / `GET /api/ringtones` — JSON list of the files in the
  respective directory: `{"ok":true,"files":[...]}`.
- `GET /api/wallpaper?name=<name>` — raw RGB565 bytes of that wallpaper (404 if missing;
  `name` defaults to `active_wallpaper.rgb565`). The browser expands them to RGB888 and
  saves a PNG, taking the height from the file size so older 480×272 files still convert.
- `GET /api/ringtone?name=<name>` — raw i8 PCM bytes of that ringtone (404 if missing;
  `name` defaults to `active_ringtone.pcm`). The browser wraps them in a 16-bit mono
  8 kHz WAV container and saves a WAV.

Writes are atomic (temp file + rename) and parent dirs are created on demand.

## Config (env)

- `PORT` (default `80`), `BIND` (default `0.0.0.0`).

The browser picks the stored name: it replaces characters outside `[A-Za-z0-9._-]`
with `_`, strips a trailing `.png` / `.wav` (case-insensitive), and appends
`.rgb565` / `.pcm`:

- wallpapers → `/tmp/telepwn_data/frontend/wallpapers/<name>.rgb565`
- ringtones → `/tmp/telepwn_data/frontend/ringtones/<name>.pcm`

The browser also enforces the byte lengths above, a 64-char name limit, and that
uploads never replace `active_wallpaper.rgb565` / `active_ringtone.pcm`.

The server does no format checks and stores bodies as-is (up to 4 MiB). It keeps only
two guards so hand-made requests can't misplace files: names must be a single path
component not starting with `.`, and uploads can't overwrite the active files. Only
regular files are listed or served (no symlinks or subdirs).

## Build

Cross-compiles for the phone via the pinned `.cargo/config.toml`
(`arm-unknown-linux-musleabi`, static musl, no C deps):

```
cargo build --release   # -> target/arm-unknown-linux-musleabi/release/mediaup
```

Host build for local testing: `cargo build --release --target x86_64-unknown-linux-gnu`.

## Deploy on the phone

```
binup target/arm-unknown-linux-musleabi/release/mediaup <my-ip> 9000 <phone-ip> 23
python3 ../phonesh/hold_run.py "cd /tmp/udisk1 && ./mediaup"
```

Then browse to `http://<phone-ip>/`, pick an image / audio file, and hit
"upload to phone". Stop it with `pkill -f hold_run.py`.
