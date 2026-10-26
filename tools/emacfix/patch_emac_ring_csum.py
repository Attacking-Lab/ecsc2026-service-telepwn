#!/usr/bin/env python3
"""COMBINED fix: 256-ring (reduce DMA lapping) + force CHECKSUM_NONE (deterministic L4
backstop for any residual leak). The ring deepening cuts how often the engine laps the
NAPI consumer (~31x fewer corruptions, full throughput); the software-checksum backstop
drops any corrupted segment that still slips through (victim header + foreign payload ->
checksum mismatch -> drop -> retransmit). Goal: 0 leaks at near-full throughput (the ring
keeps corruption rare so the checksum rarely has to throttle).

= patch_emac_bigring.py (ring 64->256 + wrap masks) + patch_emac_csumnone.py (delivery-
time ip_summed=CHECKSUM_NONE cave). The two edit disjoint bytes (ring/wraps at
0xcb4c/0xcc18/0xbcec/0xbd28/0xbd50 vs cave 0xbd8c / hook 0xc194 / safety 0xbd20).
Module renamed emac -> emacrcsm.

Usage: patch_emac_ring_csum.py <emac.ko> <out.ko>
Needs: arm-linux-gnueabi-{as,ld,objcopy,nm} on PATH.
"""
import os
import struct
import subprocess
import sys
import tempfile

TEXT = 0x58

# --- ring + wrap word-patches (from patch_emac_bigring.py) ---
RING_PATCHES = [
    (0xCB4C, 0xE3A01B02, 0xE3A01A02),  # ring alloc size 0x800 -> 0x2000
    (0xCC18, 0xE3550B02, 0xE3550A02),  # fill-loop bound 0x800 -> 0x2000
    (0xBCEC, 0xE353003F, 0xE35300FF),  # advance wrap 0x3f -> 0xff
    (0xBD28, 0xE352003F, 0xE35200FF),  # fwd-scan wrap 0x3f -> 0xff
    (0xBD50, 0xE352003F, 0xE35200FF),  # fwd-scan wrap 0x3f -> 0xff
]

# --- checksum cave (from patch_emac_csumnone.py) ---
CAVE_VA = 0xBD8C
HOOK_CSUM = 0xC194
SAFETY = 0xBD20
RET_CSUM = 0xC198
SKIP_TARGET = 0xBE8C
ORIG_CSUM = 0xE1A00008  # mov r0, r8
ORIG_SAFETY = 0xEA000014  # b 0xbd78

NAME_OFF = 0xF42C
NEW_NAME = b"emacrcsm"

SHT_REL = 9

CAVE_ASM = """
	.syntax unified
	.arch armv6
	.text
CAVE_CSUM:
	ldrb  r3, [r8, #0x6a]
	bic   r3, r3, #0xc
	strb  r3, [r8, #0x6a]
	ldrb  r3, [r8, #0x6b]
	bic   r3, r3, #0xc
	strb  r3, [r8, #0x6b]
	mov   r0, r8
	b     ret_csum
"""

PFX = "arm-linux-gnueabi-"


def arm_b(src, dst):
    return 0xEA000000 | (((dst - (src + 8)) >> 2) & 0xFFFFFF)


def assemble_cave():
    with tempfile.TemporaryDirectory() as d:
        s, o, e, b = (os.path.join(d, f) for f in ("c.s", "c.o", "c.elf", "c.bin"))
        open(s, "w").write(CAVE_ASM)
        subprocess.run([PFX + "as", "-march=armv6", "-o", o, s], check=True)
        subprocess.run(
            [PFX + "ld", "-Ttext=%#x" % CAVE_VA, "--defsym", "ret_csum=%#x" % RET_CSUM, "-o", e, o],
            check=True,
        )
        subprocess.run([PFX + "objcopy", "-O", "binary", "--only-section=.text", e, b], check=True)
        code = open(b, "rb").read()
        nm = subprocess.run([PFX + "nm", e], check=True, capture_output=True, text=True).stdout
        syms = {}
        for line in nm.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in ("t", "T"):
                syms[parts[2]] = int(parts[0], 16)
        return code, syms


def section_headers(data):
    e_shoff = struct.unpack_from("<I", data, 0x20)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x2E)[0]
    e_shnum = struct.unpack_from("<H", data, 0x30)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x32)[0]
    secs = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        vals = struct.unpack_from("<10I", data, off)
        secs.append(dict(name=vals[0], type=vals[1], offset=vals[4], size=vals[5], info=vals[7]))
    return secs, e_shstrndx


def relocs_in_range(data, lo, hi):
    secs, shstrndx = section_headers(data)
    strtab = secs[shstrndx]["offset"]

    def secname(n):
        end = data.index(b"\x00", strtab + n)
        return data[strtab + n:end].decode()

    text_idx = next(i for i, s in enumerate(secs) if secname(s["name"]) == ".text")
    hits = []
    for s in secs:
        if s["type"] == SHT_REL and s["info"] == text_idx:
            for j in range(s["size"] // 8):
                r_offset, _ = struct.unpack_from("<II", data, s["offset"] + j * 8)
                if lo <= r_offset < hi:
                    hits.append(r_offset)
    return hits


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(sys.argv[1], "rb").read())

    def fo(va):
        return TEXT + va

    def expect(va, val):
        cur = struct.unpack_from("<I", data, fo(va))[0]
        if cur != val:
            sys.exit(f"FAIL @.text+{va:#x}: expected {val:#010x}, found {cur:#010x}")

    # ring + wraps
    for t, exp, new in RING_PATCHES:
        cur = struct.unpack_from("<I", data, fo(t))[0]
        if cur != exp:
            sys.exit(f"FAIL @.text+{t:#x}: expected {exp:#010x}, found {cur:#010x}")
        struct.pack_into("<I", data, fo(t), new)
        print(f"  ring .text+{t:#06x}: {exp:#010x} -> {new:#010x}")

    # checksum cave
    expect(HOOK_CSUM, ORIG_CSUM)
    expect(SAFETY, ORIG_SAFETY)
    code, syms = assemble_cave()
    cave_csum = syms["CAVE_CSUM"]
    assert cave_csum == CAVE_VA
    end = CAVE_VA + len(code)
    if end > SKIP_TARGET:
        sys.exit("FAIL: cave overruns live code")
    hits = relocs_in_range(data, CAVE_VA, end)
    if hits:
        sys.exit("FAIL: cave overlaps relocs at %s" % ", ".join("%#x" % h for h in hits))
    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    print(f"  csum cave {CAVE_VA:#x}..{end:#x} ({len(code)} B, reloc-free)")
    for va, target, what in (
        (HOOK_CSUM, cave_csum, "net_poll mov r0,r8 -> CAVE_CSUM"),
        (SAFETY, SKIP_TARGET, "use_debug cave skip"),
    ):
        struct.pack_into("<I", data, fo(va), arm_b(va, target))
        print(f"  .text+{va:#06x}: b {target:#x}   ({what})")

    if bytes(data[NAME_OFF:NAME_OFF + 5]) != b"emac\x00":
        sys.exit("FAIL: module name not 'emac'")
    data[NAME_OFF:NAME_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print(f"  module name: emac -> {NEW_NAME.decode()}")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
