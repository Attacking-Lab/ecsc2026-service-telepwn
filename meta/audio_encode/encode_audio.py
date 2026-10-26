import argparse
import math
import os
import subprocess
import tempfile

import json5
import numpy as np
import scipy as sp

TARGET_SAMP_RATE = 8000


class EspeakError(Exception):
    pass


def synth_wav(text, wav_path, voice, wpm):
    # "--" so a line starting with "-" is not parsed as an espeak-ng option.
    cmd = ["espeak-ng", "-v", voice, "-s", str(wpm), "-w", wav_path, "--", text]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        raise EspeakError("espeak-ng not found on PATH") from None
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise EspeakError(f"espeak-ng failed for {text!r}: {err}")


def synth_text(voice, text, wpm):
    with tempfile.TemporaryDirectory() as dir_path:
        wav_path = os.path.join(dir_path, "temp.wav")
        synth_wav(text, wav_path, voice, wpm)
        wav_rate, wav_samples = sp.io.wavfile.read(wav_path)
    g = math.gcd(TARGET_SAMP_RATE, wav_rate)
    up, down = TARGET_SAMP_RATE // g, wav_rate // g
    return sp.signal.resample_poly(wav_samples.astype(np.float64), up, down)


def normalize(x, target_rms, peak=126.0):
    if x.max() == 0:
        return x
    lo, hi = 1e-6, 1.0
    for _ in range(80):
        g = (lo + hi) / 2
        rms = np.sqrt((np.tanh(x * g) ** 2).mean()) * peak
        if rms < target_rms:
            lo = g
        else:
            hi = g
    return np.tanh(x * g) * peak


def trim_edges(x, threshold=8.0, tail_samples=80):
    nz = np.nonzero(np.abs(x) > threshold)[0]
    if len(nz) == 0:
        return np.zeros(tail_samples, dtype=x.dtype)
    return np.concatenate([x[nz[0]: nz[-1] + 1], np.zeros(tail_samples)])


def make_line(voice, text, slot, tail_samples, target_rms, min_wpm=175, max_wpm=1000):
    for wpm in range(min_wpm, max_wpm + 1, 25):
        x = normalize(synth_text(voice, text, wpm), target_rms)
        x = trim_edges(x, tail_samples=tail_samples)
        if len(x) <= slot:
            break

    body_target = slot - tail_samples
    current_body_len = len(x) - tail_samples
    if current_body_len < body_target:
        # Audio is shorter than the slot. Pad with silence instead of resampling.
        pad_len = body_target - current_body_len
        comp = np.concatenate([x[:-tail_samples], np.zeros(pad_len)])
    else:
        # Audio is still longer than the slot (even at max WPM). 
        # Compress it slightly to fit. (Note: this will raise the pitch)
        print("can't make it fit, resampling!")
        g = math.gcd(current_body_len, body_target)
        comp = sp.signal.resample_poly(x[:-tail_samples], body_target // g, current_body_len // g)

    return np.concatenate([comp, np.zeros(tail_samples)]), max_wpm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("outdir")
    ap.add_argument("--wpm", type=int, default=175)
    ap.add_argument("--min-wpm", type=int, default=175)
    ap.add_argument("--tail-samples", type=int, default=80)
    ap.add_argument("--target-rms", type=float, default=30.0)
    ap.add_argument("--voice", default="en")
    args = ap.parse_args()

    with open(args.input) as f:
        lines = json5.load(f)

    os.makedirs(args.outdir, exist_ok=True)
    for line in lines:
        if "slot" in line:
            pcm, wpm = make_line(args.voice, line["text"], line["slot"], args.tail_samples, args.target_rms, min_wpm=args.min_wpm)
        else:
            x = normalize(synth_text(args.voice, line["text"], args.wpm), args.target_rms)
            #pcm = trim_edges(x, tail_samples=args.tail_samples)
            pcm = x
            wpm = args.wpm
        pcm = np.clip(np.round(pcm), -127, 127).astype(np.int8)
        with open(os.path.join(args.outdir, f"{line['name']}.pcm"), "wb") as f:
            f.write(pcm.tobytes())
        print(f"{line['name']:>16} {len(pcm) / TARGET_SAMP_RATE * 1000:6.0f}ms wpm={wpm}")


if __name__ == "__main__":
    main()
