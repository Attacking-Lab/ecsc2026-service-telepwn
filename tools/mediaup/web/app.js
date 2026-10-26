const WIDTH = 480;
const HEIGHT = 240;
const SAMPLE_RATE = 8000;
const RINGTONE_BLOCK = 1000;
const RINGTONE_MAX_SAMPLES = 480 * RINGTONE_BLOCK;
const WALLPAPER_BYTES = WIDTH * HEIGHT * 2;
const MAX_NAME_LEN = 64;
const ACTIVE_WALLPAPER_NAME = "active_wallpaper.rgb565";
const ACTIVE_RINGTONE_NAME = "active_ringtone.pcm";

function storedName(file, sourceExt, storedExt, activeName) {
  const sanitized = file.name.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "");
  const lower = sanitized.toLowerCase();
  const ext = [sourceExt, storedExt].find((e) => lower.endsWith(e));
  const stem = ext ? sanitized.slice(0, -ext.length) : sanitized;
  const name = (stem || "upload") + storedExt;
  if (name.length > MAX_NAME_LEN) {
    throw new Error("file name too long: " + name.length + " > " + MAX_NAME_LEN + " chars");
  }
  if (name === activeName) {
    throw new Error(name + " is the active file and can't be replaced");
  }
  return name;
}

function checkWallpaper(bytes) {
  if (bytes.length !== WALLPAPER_BYTES) {
    throw new Error("expected " + WALLPAPER_BYTES + " bytes, got " + bytes.length);
  }
}

function checkRingtone(samples) {
  if (
    samples.length === 0 ||
    samples.length % RINGTONE_BLOCK !== 0 ||
    samples.length > RINGTONE_MAX_SAMPLES
  ) {
    throw new Error(
      "length must be a non-zero multiple of " + RINGTONE_BLOCK + " up to " +
        RINGTONE_MAX_SAMPLES + ", got " + samples.length,
    );
  }
}

function setStatus(el, message, cls) {
  el.textContent = message;
  el.className = "status" + (cls ? " " + cls : "");
}

function imageToWallpaper(bitmap) {
  const canvas = document.getElementById("wp-canvas");
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, WIDTH, HEIGHT);

  const scale = Math.max(WIDTH / bitmap.width, HEIGHT / bitmap.height);
  const dw = bitmap.width * scale;
  const dh = bitmap.height * scale;
  ctx.imageSmoothingEnabled = scale < 1;
  ctx.drawImage(bitmap, (WIDTH - dw) / 2, (HEIGHT - dh) / 2, dw, dh);

  const pixels = ctx.getImageData(0, 0, WIDTH, HEIGHT).data;
  const out = new Uint8Array(WIDTH * HEIGHT * 2);
  let p = 0;
  for (let i = 0; i < WIDTH * HEIGHT; i++) {
    const r = pixels[i * 4];
    const g = pixels[i * 4 + 1];
    const b = pixels[i * 4 + 2];
    const v = ((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3);
    out[p++] = (v >> 8) & 0xff;
    out[p++] = v & 0xff;
  }
  return out;
}

async function audioToRingtone(file) {
  const raw = await file.arrayBuffer();
  const decodeCtx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await decodeCtx.decodeAudioData(raw);
  decodeCtx.close();

  const frames = Math.ceil(decoded.duration * SAMPLE_RATE);
  const offline = new OfflineAudioContext(1, Math.max(frames, 1), SAMPLE_RATE);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start();
  const rendered = await offline.startRendering();

  const mono = rendered.getChannelData(0);
  let count = Math.min(mono.length, RINGTONE_MAX_SAMPLES);
  count = Math.max(Math.floor(count / RINGTONE_BLOCK) * RINGTONE_BLOCK, RINGTONE_BLOCK);

  const out = new Int8Array(count);
  for (let i = 0; i < count; i++) {
    const s = i < mono.length ? Math.max(-1, Math.min(1, mono[i])) : 0;
    out[i] = Math.round(s * 127);
  }
  return out;
}

function wallpaperToPngBlob(bytes) {
  const height = Math.floor(bytes.length / (WIDTH * 2));
  if (height === 0) throw new Error("file too small for a " + WIDTH + "px wide image");
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const image = ctx.createImageData(WIDTH, height);
  for (let i = 0; i < WIDTH * height; i++) {
    const v = (bytes[i * 2] << 8) | bytes[i * 2 + 1];
    const r5 = (v >> 11) & 0x1f;
    const g6 = (v >> 5) & 0x3f;
    const b5 = v & 0x1f;
    image.data[i * 4] = (r5 << 3) | (r5 >> 2);
    image.data[i * 4 + 1] = (g6 << 2) | (g6 >> 4);
    image.data[i * 4 + 2] = (b5 << 3) | (b5 >> 2);
    image.data[i * 4 + 3] = 255;
  }
  ctx.putImageData(image, 0, 0);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
}

function ringtoneToWavBlob(samples) {
  const dataLen = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataLen);
  const view = new DataView(buffer);
  let o = 0;
  const str = (s) => {
    for (let i = 0; i < s.length; i++) view.setUint8(o++, s.charCodeAt(i));
  };
  const u32 = (v) => {
    view.setUint32(o, v, true);
    o += 4;
  };
  const u16 = (v) => {
    view.setUint16(o, v, true);
    o += 2;
  };
  str("RIFF");
  u32(36 + dataLen);
  str("WAVE");
  str("fmt ");
  u32(16);
  u16(1);
  u16(1);
  u32(SAMPLE_RATE);
  u32(SAMPLE_RATE * 2);
  u16(2);
  u16(16);
  str("data");
  u32(dataLen);
  for (let i = 0; i < samples.length; i++) {
    view.setInt16(o, samples[i] * 256, true);
    o += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function fetchRaw(url) {
  const response = await fetch(url);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error("HTTP " + response.status);
  return new Uint8Array(await response.arrayBuffer());
}

async function refreshList(select, url, activeName) {
  const response = await fetch(url);
  const parsed = await response.json();
  if (!response.ok || !parsed.ok) {
    throw new Error(parsed.error || ("HTTP " + response.status));
  }
  const previous = select.value || activeName;
  select.replaceChildren(
    ...parsed.files.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name === activeName ? name + " (active)" : name;
      return option;
    }),
  );
  if (parsed.files.includes(previous)) select.value = previous;
}

async function upload(url, name, bytes) {
  const response = await fetch(url + "?name=" + encodeURIComponent(name), {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: bytes,
  });
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = { ok: response.ok, error: text };
  }
  if (!response.ok || !parsed.ok) {
    throw new Error(parsed.error || ("HTTP " + response.status));
  }
  return parsed;
}

function playSamples(samples) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const buffer = ctx.createBuffer(1, samples.length, SAMPLE_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) {
    channel[i] = samples[i] / 127;
  }
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  source.onended = () => ctx.close();
  source.start();
}

function initWallpaper() {
  const fileInput = document.getElementById("wp-file");
  const applyButton = document.getElementById("wp-apply");
  const downloadButton = document.getElementById("wp-download");
  const select = document.getElementById("wp-select");
  const status = document.getElementById("wp-status");
  let payload = null;
  let name = null;

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    setStatus(status, "decoding…");
    applyButton.disabled = true;
    try {
      const bitmap = await createImageBitmap(file);
      try {
        payload = imageToWallpaper(bitmap);
      } finally {
        bitmap.close();
      }
      checkWallpaper(payload);
      name = storedName(file, ".png", ".rgb565", ACTIVE_WALLPAPER_NAME);
      applyButton.disabled = false;
      setStatus(status, "ready: " + payload.length + " bytes, will be saved as " + name, "ok");
    } catch (e) {
      payload = null;
      setStatus(status, "error: " + e.message, "err");
    }
  });

  applyButton.addEventListener("click", async () => {
    if (!payload) return;
    applyButton.disabled = true;
    setStatus(status, "uploading…");
    try {
      const result = await upload("/api/wallpaper", name, payload);
      setStatus(status, "wallpaper saved to phone as " + result.name, "ok");
      await refreshList(select, "/api/wallpapers", ACTIVE_WALLPAPER_NAME);
    } catch (e) {
      setStatus(status, "error: " + e.message, "err");
    } finally {
      applyButton.disabled = false;
    }
  });

  downloadButton.addEventListener("click", async () => {
    downloadButton.disabled = true;
    setStatus(status, "fetching…");
    try {
      const selected = select.value;
      if (!selected) {
        setStatus(status, "no file selected", "err");
        return;
      }
      const bytes = await fetchRaw("/api/wallpaper?name=" + encodeURIComponent(selected));
      if (!bytes) {
        setStatus(status, "file no longer exists", "err");
        await refreshList(select, "/api/wallpapers", ACTIVE_WALLPAPER_NAME);
        return;
      }
      const blob = await wallpaperToPngBlob(bytes);
      const filename = selected.replace(/\.[^.]*$/, "") + ".png";
      triggerDownload(blob, filename);
      setStatus(status, "downloaded " + filename, "ok");
    } catch (e) {
      setStatus(status, "error: " + e.message, "err");
    } finally {
      downloadButton.disabled = false;
    }
  });

  refreshList(select, "/api/wallpapers", ACTIVE_WALLPAPER_NAME).catch((e) =>
    setStatus(status, "error listing files: " + e.message, "err"),
  );
}

function initRingtone() {
  const fileInput = document.getElementById("rt-file");
  const previewButton = document.getElementById("rt-preview");
  const applyButton = document.getElementById("rt-apply");
  const downloadButton = document.getElementById("rt-download");
  const select = document.getElementById("rt-select");
  const status = document.getElementById("rt-status");
  let samples = null;
  let name = null;

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    setStatus(status, "decoding & resampling…");
    previewButton.disabled = true;
    applyButton.disabled = true;
    try {
      samples = await audioToRingtone(file);
      checkRingtone(samples);
      name = storedName(file, ".wav", ".pcm", ACTIVE_RINGTONE_NAME);
      previewButton.disabled = false;
      applyButton.disabled = false;
      const seconds = (samples.length / SAMPLE_RATE).toFixed(1);
      setStatus(
        status,
        "ready: " + samples.length + " samples (" + seconds + "s), will be saved as " + name,
        "ok",
      );
    } catch (e) {
      samples = null;
      setStatus(status, "error: " + e.message, "err");
    }
  });

  previewButton.addEventListener("click", () => {
    if (samples) playSamples(samples);
  });

  applyButton.addEventListener("click", async () => {
    if (!samples) return;
    applyButton.disabled = true;
    setStatus(status, "uploading…");
    try {
      const result = await upload("/api/ringtone", name, new Uint8Array(samples.buffer));
      setStatus(status, "ringtone saved to phone as " + result.name, "ok");
      await refreshList(select, "/api/ringtones", ACTIVE_RINGTONE_NAME);
    } catch (e) {
      setStatus(status, "error: " + e.message, "err");
    } finally {
      applyButton.disabled = false;
    }
  });

  downloadButton.addEventListener("click", async () => {
    downloadButton.disabled = true;
    setStatus(status, "fetching…");
    try {
      const selected = select.value;
      if (!selected) {
        setStatus(status, "no file selected", "err");
        return;
      }
      const bytes = await fetchRaw("/api/ringtone?name=" + encodeURIComponent(selected));
      if (!bytes) {
        setStatus(status, "file no longer exists", "err");
        await refreshList(select, "/api/ringtones", ACTIVE_RINGTONE_NAME);
        return;
      }
      const blob = ringtoneToWavBlob(new Int8Array(bytes.buffer));
      const filename = selected.replace(/\.[^.]*$/, "") + ".wav";
      triggerDownload(blob, filename);
      setStatus(status, "downloaded " + filename, "ok");
    } catch (e) {
      setStatus(status, "error: " + e.message, "err");
    } finally {
      downloadButton.disabled = false;
    }
  });

  refreshList(select, "/api/ringtones", ACTIVE_RINGTONE_NAME).catch((e) =>
    setStatus(status, "error listing files: " + e.message, "err"),
  );
}

initWallpaper();
initRingtone();
