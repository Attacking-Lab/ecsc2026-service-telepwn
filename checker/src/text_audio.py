import os

import secrets
import enochecker3

from phone import BLOCK_NUM_SAMPLES, SAMPLE_RATE, TelepwnError
from client import MIN_RINGTONE_NUM_SAMPLES, MAX_RECORD_NUM_SAMPLES, MAX_RECORD_SECONDS

FLAG_LINES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "lines"
)

class SpeechError(ValueError):
    pass

NATO_WORDS = "alfa bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey x-ray yankee zulu".split()

CHAR_TO_PCM = {}
for _digit in "0123456789":
    CHAR_TO_PCM[_digit] = (_digit,)
for _letter, _word in zip("abcdefghijklmnopqrstuvwxyz", NATO_WORDS):
    CHAR_TO_PCM[_letter] = ("small", _word)
    CHAR_TO_PCM[_letter.upper()] = ("big", _word)
CHAR_TO_PCM.update(
    {
        "{": ("open_brace",),
        "}": ("close_brace",),
        "+": ("plus",),
        "/": ("slash",),
        "=": ("equal",),
        "-": ("minus",),
        "_": ("under",),
    }
)
CHAR_SAMPLES = 2400

_line_cache = {}
def _line_data(name):
    if name not in _line_cache:
        with open(os.path.join(FLAG_LINES_DIR, f"{name}.pcm"), "rb") as f:
            _line_cache[name] = f.read()
    return _line_cache[name]

_char_cache = {}
def _char_samples(ch):
    if ch not in _char_cache:
        names = CHAR_TO_PCM.get(ch)
        if names is None:
            raise SpeechError(f"no spoken sample for character {ch!r}")
        data = b"".join(_line_data(name) for name in names)
        data += bytes(CHAR_SAMPLES - len(data))
        _char_cache[ch] = data
    return _char_cache[ch]

FLAG_LEN = 38
FLAG_NUM_SAMPLES = (((FLAG_LEN * CHAR_SAMPLES) + BLOCK_NUM_SAMPLES - 1) // BLOCK_NUM_SAMPLES) * BLOCK_NUM_SAMPLES

def text_from_audio(samples):
    for start in range(0, len(samples), BLOCK_NUM_SAMPLES):
        text = []
        offset = start
        while offset + CHAR_SAMPLES <= len(samples):
            chunk = samples[offset : offset + CHAR_SAMPLES]
            if not any(chunk):
                break
            for ch in CHAR_TO_PCM:
                if chunk == _char_samples(ch):
                    text.append(ch)
                    break
            else:
                break
            offset += CHAR_SAMPLES
        if text and not any(samples[offset:]):
            return "".join(text)
    raise SpeechError("no spoken text found")


def text_to_audio(data):
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        return ringtone(text)
    except SpeechError as e:
        raise enochecker3.InternalErrorException(f"could not encode as speech: {e}")


def encode(text):
    out = bytearray()
    for ch in text:
        out += _char_samples(ch)
    if len(out) > MAX_RECORD_NUM_SAMPLES:
        raise SpeechError(
            f"speech is {len(out) / SAMPLE_RATE:.1f}s long, but the phone "
            f"records at most {MAX_RECORD_SECONDS}s"
        )
    return bytes(out)


def ringtone(text):
    samples = encode(text)
    n = len(samples)
    padded = (-n) % BLOCK_NUM_SAMPLES + n
    if padded < MIN_RINGTONE_NUM_SAMPLES:
        padded = MIN_RINGTONE_NUM_SAMPLES
    return samples + bytes(padded - n)