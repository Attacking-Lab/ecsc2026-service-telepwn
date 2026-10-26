import sys
import os
import io
import json
import base64
import struct

from PIL import Image

LOGO_WIDTH = 48
LOGO_HEIGHT = 32
WALLPAPER_WIDTH = 480
WALLPAPER_HEIGHT = 240

def img_to_rgb565_data(img):
	width = img.size[0]
	height = img.size[1]
	bytes_per_pixel = 2
	buffer = bytearray(width * height * bytes_per_pixel)

	pixels = img.load()
	for y in range(height):
		for x in range(width):
			r, g, b = pixels[x, y]

			rgb565 = 0
			rgb565 |= ((r >> 3) & 0x1f) << (5+6)
			rgb565 |= ((g >> 2) & 0x3f) << 5
			rgb565 |= ((b >> 3) & 0x1f) << 0

			p = ((y * width) + x) * bytes_per_pixel
			struct.pack_into(">H", buffer, p, rgb565)

	return buffer

def pad_img(img, pad):
	w, h = img.size
	wp, hp = w + 2*pad, h + 2*pad

	res = Image.new(img.mode, (wp, hp))
	res.paste(img, (pad, pad))

	return res

def make_rgb565_img(img, width, height):
	# Make sure it fits...
	for pad in range(512):
		if pad > 0:
			padded_img = pad_img(img, pad)
		else:
			padded_img = img

		# Resize to fit
		resized_logo_img = padded_img.resize((width, width))
		crop_y = (width - height) // 2

		# Check that the crop fits...
		bad_crop = False
		pixels = resized_logo_img.load()
		for y in [crop_y - 1, height + crop_y - 1]:
			for x in range(resized_logo_img.size[0]):
				px = pixels[x,y]
				#print(x, y, px)
				if pixels[x,y][-1] == 255:
					bad_crop = True
					break
			if bad_crop:
				break

		# Does it fit?
		if not bad_crop:
			break

		# Otherwise, try to add some padding and retry.
		print("trying more padding", pad + 1)
	else:
		assert False, "can't make logo fit"

	cropped_logo_img = resized_logo_img.crop((0, crop_y, width, crop_y + height))

	# Convert to RGB (background here if we want it)
	cropped_logo_img = cropped_logo_img.convert("RGB")

	logo_rgb565_data = img_to_rgb565_data(cropped_logo_img)
	return logo_rgb565_data

def main():
	with open(sys.argv[1], encoding="utf-8") as f:
		input_json = json.load(f)
	output_root = os.path.abspath(sys.argv[2])

	input_teams = input_json["teams"]

	os.makedirs(output_root, exist_ok=True)
	os.makedirs(output_root + "/logos", exist_ok=True)
	os.makedirs(output_root + "/wallpapers", exist_ok=True)

	output_json = []
	for team_index, team in enumerate(input_teams):
		team_id = team["team_id"]
		print(team_id, team["name"])
		#assert team["logo_mime"] == "image/png"
		logo_png_data = base64.b64decode(team["logo"].encode("utf-8"))
		logo_img = Image.open(io.BytesIO(logo_png_data)).convert("RGBA")
		#logo_img.save(f"logos_png/{team_id}.png")

		print("logo")
		logo_rgb565_data = make_rgb565_img(logo_img, LOGO_WIDTH, LOGO_HEIGHT)
		logo_subpath = f"logos/{team_id}.rgb565"
		logo_path = os.path.join(output_root, logo_subpath)
		with open(logo_path, "wb") as f:
			f.write(logo_rgb565_data)

		print("wallpaper")
		wallpaper_rgb565_data = make_rgb565_img(logo_img, WALLPAPER_WIDTH, WALLPAPER_HEIGHT)
		wallpaper_subpath = f"wallpapers/{team_id}.rgb565"
		wallpaper_path = os.path.join(output_root, wallpaper_subpath)
		with open(wallpaper_path, "wb") as f:
			f.write(wallpaper_rgb565_data)

		# Default ringtone path
		team_name_tokens = team["name"].split(" ")
		if len(team_name_tokens) > 1:
			ringtone_name = "_".join(team_name_tokens[1:])
		else:
			ringtone_name = team_name_tokens[0] # NOP

		default_ringtone_path = f"anthems/{ringtone_name.lower()}.pcm"

		ip = f"10.60.{team_id}.2"
		output_json.append({
			"name": team["name"],
			"id": team_id,
			"ip": ip,
			"logo_path": logo_subpath,
			"default_ringtone_path": default_ringtone_path,
			"default_wallpaper_path": wallpaper_subpath,
		})

	output_json.sort(key=lambda t: t["id"])

	with open(os.path.join(output_root, "teams.json"), "w", encoding="utf-8") as f:
		json.dump(output_json, f, indent=2)

if __name__ == "__main__":
	main()