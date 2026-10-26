import asyncio

# Generated with Gemini 3.1 Pro from service/frontend/src/fax.rs

import math
import numpy as np

# --- Constants ---
SAMPLE_RATE = 8000
PIXEL_NUM_SAMPLES = 8
CHANNEL_NUM = 3
CHANNEL_FREQS_HZ = [1000.0, 2000.0, 3000.0]

CHANNEL_MAX = 31  # RGB555 max value

FAX_DISP_WIDTH = 480
FAX_DISP_HEIGHT = 240
FAX_DISP_NUM_PIXELS = FAX_DISP_HEIGHT * FAX_DISP_WIDTH

FAX_FULL_SCALE = 4
FAX_PREVIEW_SCALE = FAX_FULL_SCALE * 10

SAMPLE_QN = 15
COEFF_QN = 14
DTMF_SCAN_SAMPLE_GRANULARITY = 250

# --- Emulating Rust's precise mathematical semantics ---
def rust_round(x: float) -> float:
    """Rust's f32::round() rounds half-way cases away from 0.0"""
    return math.copysign(math.floor(abs(x) + 0.5), x)

def wrap_i32(v: int) -> int:
    """Simulate i32 overflow/underflow"""
    return (v + 2147483648) % 4294967296 - 2147483648

def as_i8(v: int) -> int:
    """Simulate casting to i8 (truncating higher bits)"""
    return (v + 128) % 256 - 128

def trunc_div(a: int, b: int) -> int:
    """Rust's integer division truncates towards zero."""
    ans = abs(a) // abs(b)
    return -ans if (a < 0) ^ (b < 0) else ans

# --- Goertzel Algorithm ---
def goertzel_coeff(freq_hz: float) -> int:
    omega = np.float32(2.0) * np.float32(math.pi) * np.float32(freq_hz) / np.float32(SAMPLE_RATE)
    coeff_float = np.float32(2.0) * np.float32(math.cos(omega))
    val = coeff_float * np.float32(1 << COEFF_QN)
    return int(rust_round(float(val)))

def goertzel_mag_sq(samples: np.ndarray, coeff: int) -> int:
    s1, s2 = 0, 0
    for sample in samples:
        x = int(sample) << SAMPLE_QN
        # Python natively handles arbitrary precision, but we must enforce 32-bit wrap
        s1c = wrap_i32((s1 * coeff) >> COEFF_QN)
        s0 = wrap_i32(x + s1c - s2)
        s2 = s1
        s1 = s0

    # Computed strictly in simulated 64-bit bounds
    term1 = (s1 * s1) >> SAMPLE_QN
    term2 = (s2 * s2) >> SAMPLE_QN
    term3 = (((s1 * s2) >> SAMPLE_QN) * coeff) >> COEFF_QN
    return term1 + term2 - term3

def goertzel_mag(samples: np.ndarray, coeff: int) -> int:
    mag_squared = goertzel_mag_sq(samples, coeff)
    return math.isqrt(abs(mag_squared))

# --- Core Modem Tone Encoding ---
def make_tone_samples(freq_hz: float) -> np.ndarray:
    result = np.zeros(PIXEL_NUM_SAMPLES, dtype=np.int16)
    omega = np.float32(2.0 * math.pi * freq_hz)
    for i in range(PIXEL_NUM_SAMPLES):
        t = np.float32(i) / np.float32(SAMPLE_RATE)
        soft = np.float32(math.sin(t * omega))
        val = soft * np.float32(256.0)
        result[i] = int(rust_round(float(val)))
    return result

TONE_SAMPLE_TABLES = [make_tone_samples(f) for f in CHANNEL_FREQS_HZ]
TONE_COEFF_TABLES = [goertzel_coeff(f) for f in CHANNEL_FREQS_HZ]

def encode_color(color: tuple[int, int, int]) -> np.ndarray:
    out_samples = np.zeros(PIXEL_NUM_SAMPLES, dtype=np.int8)
    for p in range(PIXEL_NUM_SAMPLES):
        sample_wide = 0
        for chan_index in range(CHANNEL_NUM):
            sample_wide += int(TONE_SAMPLE_TABLES[chan_index][p]) * int(color[chan_index])
        
        # Simulate i16 arithmetic wrap/shift -> i8
        sample_wide = (sample_wide + 32768) % 65536 - 32768
        out_samples[p] = as_i8(sample_wide >> 8)
    return out_samples

PRECACHE_COLORS = [(CHANNEL_MAX, CHANNEL_MAX, CHANNEL_MAX), (0, 0, 0)]
COLOR_SAMPLE_PRECACHE = {
    color: encode_color(color) for color in PRECACHE_COLORS
}

def encode_color_cached(color: tuple[int, int, int]) -> np.ndarray:
    return COLOR_SAMPLE_PRECACHE[color]

def decode_color_soft(in_samples: np.ndarray) -> tuple[int, int, int]:
    return tuple(goertzel_mag(in_samples, TONE_COEFF_TABLES[i]) for i in range(CHANNEL_NUM))

# Statically pre-computed white level 
WHITE_SAMPLES = encode_color((CHANNEL_MAX, CHANNEL_MAX, CHANNEL_MAX))
FAX_WHITE_LEVEL = decode_color_soft(WHITE_SAMPLES)

SAMPLE_COLOR_PRECACHE = {
    tuple(encode_color(color)): color for color in PRECACHE_COLORS
}

def decode_color_hard_cached(in_samples: np.ndarray, black: tuple[int, int, int], white: tuple[int, int, int]) -> tuple[int, int, int]:
    key = tuple(in_samples)
    if key in SAMPLE_COLOR_PRECACHE:
        return SAMPLE_COLOR_PRECACHE[key]
    return None

def decode_color_hard(in_samples: np.ndarray, black: tuple[int, int, int], white: tuple[int, int, int]) -> tuple[int, int, int]:
    chan_values_soft = decode_color_soft(in_samples)
    hard_vals = [0, 0, 0]
    for i in range(CHANNEL_NUM):
        num = (chan_values_soft[i] - black[i]) * CHANNEL_MAX
        den = white[i] - black[i]
        soft_remapped = trunc_div(num, den) if den != 0 else 0
        hard_vals[i] = max(0, min(CHANNEL_MAX, soft_remapped))
    return tuple(hard_vals)

# --- DTMF ---
DTMF_FREQS_LO = [697.0, 770.0, 852.0, 941.0]
DTMF_FREQS_HI = [1209.0, 1336.0, 1477.0, 1633.0]

DTMF_COEFFS_LO = [goertzel_coeff(f) for f in DTMF_FREQS_LO]
DTMF_COEFFS_HI = [goertzel_coeff(f) for f in DTMF_FREQS_HI]
DTMF_COEFFS_LO_HARM = [goertzel_coeff(2.0 * f) for f in DTMF_FREQS_LO]
DTMF_COEFFS_HI_HARM = [goertzel_coeff(2.0 * f) for f in DTMF_FREQS_HI]

def get_dtmf_freqs_hz(tone_str: str) -> tuple[float, float]:
    tone_map = {
        'Num(1)': (0, 0), 'Num(2)': (0, 1), 'Num(3)': (0, 2), 'Special(0)': (0, 3),
        'Num(4)': (1, 0), 'Num(5)': (1, 1), 'Num(6)': (1, 2), 'Special(1)': (1, 3),
        'Num(7)': (2, 0), 'Num(8)': (2, 1), 'Num(9)': (2, 2), 'Special(2)': (2, 3),
        'Asterisk': (3, 0), 'Num(0)': (3, 1), 'Pound': (3, 2), 'Special(3)': (3, 3),
    }
    idx_lo, idx_hi = tone_map[tone_str]
    return DTMF_FREQS_LO[idx_lo], DTMF_FREQS_HI[idx_hi]

DTMF_SAMPLES_CACHE = {}
def add_dtmf_tone(samples: np.ndarray, tone_str: str) -> np.ndarray:
    freq_lo, freq_hi = get_dtmf_freqs_hz(tone_str)
    freq_amplitude = np.float32(0.5 / 2.0)
    
    for freq_hz in [freq_lo, freq_hi]:
        omega = np.float32(2.0 * math.pi * freq_hz)
        for i in range(len(samples)):
            t = np.float32(i) / np.float32(SAMPLE_RATE)
            float_val = np.sin(t * omega, dtype=np.float32)
            fixed_float = float_val * freq_amplitude * np.float32(127.0)
            fixed = int(fixed_float)
            samples[i] = as_i8(int(samples[i]) + fixed)
    return samples

def make_dtmf_block(tone_str: str, block_size: int = 1000) -> np.ndarray:
    if tone_str in DTMF_SAMPLES_CACHE:
        return DTMF_SAMPLES_CACHE[tone_str]

    block = np.zeros(block_size, dtype=np.int8)
    add_dtmf_tone(block, tone_str)
    DTMF_SAMPLES_CACHE[tone_str] = block
    return block

def detect_freq(samples: np.ndarray, freq_coeffs: list[int], harm_coeffs: list[int]) -> int | None:
    mag_sqs = [goertzel_mag_sq(samples, c) for c in freq_coeffs]
    
    best_idx, best_val = max(enumerate(mag_sqs), key=lambda x: x[1])
    other_power = sum(mag_sqs) - best_val
    
    if best_val <= 8 * other_power:
        return None
        
    harm_mag_sq = goertzel_mag_sq(samples, harm_coeffs[best_idx])
    if best_val < 8 * harm_mag_sq:
        return None
        
    return best_idx

def detect_dtmf(samples: np.ndarray) -> str | None:
    if not np.any(np.abs(samples) >= 2):
        return None

    main_lo = detect_freq(samples, DTMF_COEFFS_LO, DTMF_COEFFS_LO_HARM)
    if main_lo is None: return None
    
    main_hi = detect_freq(samples, DTMF_COEFFS_HI, DTMF_COEFFS_HI_HARM)
    if main_hi is None: return None
    
    tone_map = {
        (0, 0): 'Num(1)', (0, 1): 'Num(2)', (0, 2): 'Num(3)', (0, 3): 'Special(0)',
        (1, 0): 'Num(4)', (1, 1): 'Num(5)', (1, 2): 'Num(6)', (1, 3): 'Special(1)',
        (2, 0): 'Num(7)', (2, 1): 'Num(8)', (2, 2): 'Num(9)', (2, 3): 'Special(2)',
        (3, 0): 'Asterisk', (3, 1): 'Num(0)', (3, 2): 'Pound', (3, 3): 'Special(3)',
    }
    return tone_map[(main_lo, main_hi)]

class DtmfScanner:
    def __init__(self):
        self.dtmf_queue = []
        self.current_tone = None

    def scan_chunk_for_dtmf(self, samples: np.ndarray):
        next_tone = detect_dtmf(samples)
        if self.current_tone == next_tone:
            return
        self.current_tone = next_tone
        if self.current_tone is not None:
            self.dtmf_queue.append(self.current_tone)

    def scan_block_for_dtmf(self, in_block: np.ndarray):
        for i in range(0, len(in_block), DTMF_SCAN_SAMPLE_GRANULARITY):
            self.scan_chunk_for_dtmf(in_block[i:i+DTMF_SCAN_SAMPLE_GRANULARITY])

    def has_tone(self) -> bool:
        return len(self.dtmf_queue) > 0

    def pop_tone(self) -> str | None:
        return self.dtmf_queue.pop(0) if self.has_tone() else None

# --- Big Outer Interfaces ---

# NB this takes full size, not preview size
async def encode_fax(image: np.ndarray, kind: str = "Full", prescale=1, block_size: int = 1000) -> np.ndarray:
    """
    Encodes an image to raw int8 audio samples representing the fax datastream.
    `image` must be a numpy array of shape (240, 480, 3) containing 0-31 range RGB555 colors.
    `block_size` should be a multiple of 8 and 200 to align cleanly with sample constraints.
    """
    scale = FAX_FULL_SCALE//prescale if kind == "Full" else FAX_PREVIEW_SCALE//prescale
    out_audio_blocks = []
    
    # 1. Start Tone Block
    tone = 'Special(2)' if kind == "Full" else 'Special(3)'
    out_audio_blocks.append(make_dtmf_block(tone, block_size))
    
    # 2. Pixel Encoding
    pixel_samples = []
    # Collect correctly scaled pixels
    for y in range(0, FAX_DISP_HEIGHT // (prescale * scale)):
        for x in range(0, FAX_DISP_WIDTH // (prescale * scale)):
            yb = y * scale
            xb = x * scale
            color = tuple(image[yb, xb])
            pixel_samples.append(encode_color_cached(color))
        await asyncio.sleep(0)
            
    flat_pixel_samples = np.concatenate(pixel_samples)
    
    # Pack pixel symbols chunk by chunk, zero-padding the final partial block.
    pad_len = block_size - (len(flat_pixel_samples) % block_size)
    if pad_len != block_size:
        flat_pixel_samples = np.concatenate([flat_pixel_samples, np.zeros(pad_len, dtype=np.int8)])
        
    for i in range(0, len(flat_pixel_samples), block_size):
        out_audio_blocks.append(flat_pixel_samples[i:i+block_size])
        
    # 3. End Tone Block
    out_audio_blocks.append(make_dtmf_block('Pound', block_size))
    
    return np.concatenate(out_audio_blocks)


async def decode_fax(audio: np.ndarray, block_size: int = 1000) -> np.ndarray:
    """
    Decodes 1D int8 audio samples back into a (240, 480, 3) uint8 image.
    Outputs RGB colors restricted between 0-31 representing RGB555 channels.
    """
    output_image = np.zeros((FAX_DISP_HEIGHT, FAX_DISP_WIDTH, 3), dtype=np.uint8)
    scanner = DtmfScanner()
    
    stage = 'idle'
    scale = FAX_FULL_SCALE
    rx_black = (0, 0, 0)
    rx_white = tuple(int(x) for x in FAX_WHITE_LEVEL)
    
    y, x = 0, 0
    num_blocks = len(audio) // block_size
    
    for b_idx in range(num_blocks):
        block = audio[b_idx*block_size : (b_idx+1)*block_size]
        
        if stage == 'idle':
            scanner.scan_block_for_dtmf(block)
            while scanner.has_tone():
                dtmf_key = scanner.pop_tone()
                if dtmf_key == 'Special(2)':
                    scale = FAX_FULL_SCALE
                    stage = 'receiving'
                    y, x = 0, 0
                    output_image.fill(0)
                elif dtmf_key == 'Special(3)':
                    scale = FAX_PREVIEW_SCALE
                    stage = 'receiving'
                    y, x = 0, 0
                    output_image.fill(0)
            continue
            
        if stage == 'receiving':
            num_chunks = len(block) // PIXEL_NUM_SAMPLES
            for i in range(num_chunks):
                if stage != 'receiving':
                    break
                    
                symbol_samples = block[i*PIXEL_NUM_SAMPLES : (i+1)*PIXEL_NUM_SAMPLES]
                color = decode_color_hard_cached(symbol_samples, rx_black, rx_white)
                if color is None:
                    return None
                
                yb = y * scale
                xb = x * scale
                
                # Write decoded color into the FB buffer 
                for yp in range(yb, min(yb + scale, FAX_DISP_HEIGHT)):
                    for xp in range(xb, min(xb + scale, FAX_DISP_WIDTH)):
                        output_image[yp, xp] = color
                        
                # Advance pixel coordinates
                if xb + scale < FAX_DISP_WIDTH:
                    x += 1
                elif yb + scale < FAX_DISP_HEIGHT:
                    y += 1
                    x = 0
                else:
                    stage = 'idle'
                    
    return output_image