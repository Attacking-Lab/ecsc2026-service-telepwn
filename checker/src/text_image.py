import bdfparser
import numpy as np

import checker_util
import phone
import fax

# Load glyphs

FONT_WIDTH = 8
FONT_HEIGHT = 13

BLACK = np.array((0,0,0), dtype=np.uint8)
WHITE = np.array((31,31,31), dtype=np.uint8)

def load_bdf_glyphs(bdf_path):
	glyphs_pixels = {}
	font = bdfparser.Font(bdf_path)
	assert font.headers["fbbx"] == FONT_WIDTH
	assert font.headers["fbby"] == FONT_HEIGHT

	for char in checker_util.FLAG_ALPHABET:
		glyph = font.glyph(char)
		bitmap = glyph.draw()
		binary_data = np.array(bitmap.todata(2))
		assert 2 not in binary_data # ???

		# map into bitmap: 0 -> white, 1 -> black
		pixels = (1 - binary_data[..., np.newaxis]) * WHITE
		#print("MODMODMOD", char, pixels)
		glyphs_pixels[char] = pixels
	return glyphs_pixels

FONT_PIXELS_BY_CHAR = load_bdf_glyphs("8x13.bdf")

def round_up(v, m):
	return v + (-v % m)

# full res
FAX_FULL_WIDTH = fax.FAX_DISP_WIDTH // fax.FAX_FULL_SCALE
FAX_FULL_HEIGHT = fax.FAX_DISP_HEIGHT // fax.FAX_FULL_SCALE

FAX_FULL_NUM_PIXELS = FAX_FULL_WIDTH * FAX_FULL_HEIGHT
FAX_FULL_NUM_SAMPLES = 2 * phone.BLOCK_NUM_SAMPLES + round_up(FAX_FULL_NUM_PIXELS * fax.PIXEL_NUM_SAMPLES, phone.BLOCK_NUM_SAMPLES)

# preview res
FAX_PREVIEW_WIDTH = fax.FAX_DISP_WIDTH // fax.FAX_PREVIEW_SCALE
FAX_PREVIEW_HEIGHT = fax.FAX_DISP_HEIGHT // fax.FAX_PREVIEW_SCALE

FAX_PREVIEW_NUM_PIXELS = FAX_PREVIEW_WIDTH * FAX_PREVIEW_HEIGHT
FAX_PREVIEW_NUM_SAMPLES = 2 * phone.BLOCK_NUM_SAMPLES + round_up(FAX_PREVIEW_NUM_PIXELS * fax.PIXEL_NUM_SAMPLES, phone.BLOCK_NUM_SAMPLES)

# NB these must be multiples of the preview/full downsampling to ensure
# no data leaks!
WALLPAPER_STRIDE_Y = 15
WALLPAPER_STRIDE_X = 10
# further, aligned pixels (so (0,0), (0,10), (10,0), (10,10), ...)
# must always be white to avoid leaks!
WALLPAPER_INSET_X = 1
WALLPAPER_INSET_Y = 1

EMPTY_CHAR_PIXELS = np.full((FONT_HEIGHT, FONT_WIDTH, fax.CHANNEL_NUM), WHITE)

def text_to_wallpaper(text):
	wallpaper = np.full((FAX_FULL_HEIGHT, FAX_FULL_WIDTH, fax.CHANNEL_NUM), WHITE)

	# at least one pixel margin on top and left side
	assert WALLPAPER_STRIDE_Y >= FONT_HEIGHT + 1
	assert WALLPAPER_STRIDE_X >= FONT_WIDTH + 1

	# let's go
	y = 0
	x = 0
	for char in text:
		char_pixels = FONT_PIXELS_BY_CHAR[char]

		start_x = x + WALLPAPER_INSET_X
		start_y = y + WALLPAPER_INSET_Y
		# this is the _character_, not the striding!
		end_x = start_x + FONT_WIDTH
		end_y = start_y + FONT_HEIGHT

		# insert the char
		wallpaper[start_y:end_y,start_x:end_x,:] = char_pixels

		# advance x/y
		x += WALLPAPER_STRIDE_X
		# no more characters fit on this line?
		if x + WALLPAPER_STRIDE_X > wallpaper.shape[1]:
			# next line
			y += WALLPAPER_STRIDE_Y
			x = 0

	return wallpaper

def text_from_wallpaper(wallpaper):
	text = ""

	x = 0
	y = 0
	while True:
		# extract char
		start_x = x + WALLPAPER_INSET_X
		start_y = y + WALLPAPER_INSET_Y
		end_x = start_x + FONT_WIDTH
		end_y = start_y + FONT_HEIGHT
		got_char_pixels = wallpaper[start_y:end_y,start_x:end_x,:]

		# compare with pure white
		if np.array_equal(got_char_pixels, EMPTY_CHAR_PIXELS):
			break

		# compare with our catalog
		for candidate_char, candidate_char_pixels in FONT_PIXELS_BY_CHAR.items():
			if np.array_equal(got_char_pixels, candidate_char_pixels):
				got_char = candidate_char
				break
		else:
			# unable to decode
			return None

		# add char
		text += got_char

		# advance x/y
		x += WALLPAPER_STRIDE_X
		# no more characters fit on this line?
		if x + WALLPAPER_STRIDE_X > wallpaper.shape[1]:
			# next line
			y += WALLPAPER_STRIDE_Y
			x = 0

	return text

async def text_to_fax(text, preview=False):
	img = text_to_wallpaper(text)
	assert img.shape == (FAX_FULL_HEIGHT, FAX_FULL_WIDTH, fax.CHANNEL_NUM), f"bad fax shape {img.shape}"
	kind = "Full" if not preview else "Preview"
	return await fax.encode_fax(img, kind, prescale=fax.FAX_FULL_SCALE)

async def text_from_fax(samples):
	audio = np.frombuffer(samples, dtype=np.int8)
	img = await fax.decode_fax(audio)
	if img is None: # uncached color for example...
		return None

	# preview can't be decoded into text
	if img.shape != (fax.FAX_DISP_HEIGHT, fax.FAX_DISP_WIDTH, fax.CHANNEL_NUM):
		print("bad fax shape", img.shape)
		return None

	downsampled_img = img[::fax.FAX_FULL_SCALE,::fax.FAX_FULL_SCALE,:]
	text = text_from_wallpaper(downsampled_img)
	return text