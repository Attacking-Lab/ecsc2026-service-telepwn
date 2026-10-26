#!/usr/bin/env python3
"""ABORT-based RX gate for emac.ko (alternative to the terminator gate).

Experiments (tools/dmacpeek 'bounce') proved: a DW-AHB-DMAC channel disabled by
software ABORT (ChEnReg=0x400 while running) is RE-ENABLABLE (ChEnReg=0x404 resumes
RX, regardless of FIFO state). Only a channel that *completed* an LLP-terminator block
enters a non-resumable "transfer-complete" state — which is why the terminator gate
(patch_emac_gate2.py) wedges.

So this gate never uses a terminator. It freezes the engine with an ABORT for the
duration of each net_poll drain, so the engine cannot overwrite descriptors while the
consumer is delivering them (no leak during the poll), then resumes on exit:

  net_poll entry (.text 0xbc48, the `b c1b4` into the loop) -> CAVE_ABORT: ChEnReg=0x400
  net_poll exit  (.text 0xc260, common `ldr r0,[fp,#-52]`)  -> CAVE_RESUME: ChEnReg=0x404

Resumable stop => no kernel wedge. Not necessarily provably-0 (the engine still runs
free between polls, so a deep enough inter-poll burst can still lap), but it should
strongly cut the leak without the DoS-wedge. Measured against the 256-ring.

Cave placed at 0xbd8c (the reloc-free gap between the two printk relocs, see
patch_emac_gate2.py). Module renamed emac->emacabrt. Usage: patch_emac_abort.py <in> <out>.
"""
import os
import struct
import subprocess
import sys
import tempfile

TEXT = 0x58
CAVE_VA = 0xBD8C
HOOK_ABORT = 0xBC48  # net_poll: b c1b4 (enter loop)        -> b CAVE_ABORT
HOOK_RESUME = 0xC260  # net_poll: ldr r0,[fp,#-52] (exit)    -> b CAVE_RESUME
SAFETY = 0xBD20  # net_poll: b 0xbd78 (use_debug>3)     -> b 0xbe8c
RET_ABORT = 0xC1B4
RET_RESUME = 0xC264
SKIP_TARGET = 0xBE8C
NAME_OFF = 0xF42C
NEW_NAME = b"emacabrt"

ORIG_ABORT = 0xEA000159  # b c1b4
ORIG_RESUME = 0xE51B0034  # ldr r0, [fp, #-52]
ORIG_SAFETY = 0xEA000014  # b 0xbd78

SHT_REL = 9

CAVE_ASM = """
	.syntax unified
	.arch armv6
	.text
CAVE_ABORT:
	mov   r3, #0xf3000000
	orr   r3, r3, #0x300000
	mov   r2, #0x400
	str   r2, [r3, #0x3a0]
	b     ret_abort
CAVE_RESUME:
	mov   r3, #0xf3000000
	orr   r3, r3, #0x300000
	mov   r2, #4
	orr   r2, r2, #0x400
	str   r2, [r3, #0x3a0]
	ldr   r0, [r11, #-52]
	b     ret_resume
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
            [PFX + "ld", "-Ttext=%#x" % CAVE_VA,
             "--defsym", "ret_abort=%#x" % RET_ABORT,
             "--defsym", "ret_resume=%#x" % RET_RESUME,
             "-o", e, o],
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

    expect(HOOK_ABORT, ORIG_ABORT)
    expect(HOOK_RESUME, ORIG_RESUME)
    expect(SAFETY, ORIG_SAFETY)

    code, syms = assemble_cave()
    cave_abort = syms["CAVE_ABORT"]
    cave_resume = syms["CAVE_RESUME"]
    assert cave_abort == CAVE_VA, f"CAVE_ABORT at {cave_abort:#x}, expected {CAVE_VA:#x}"
    end = CAVE_VA + len(code)
    if end > SKIP_TARGET:
        sys.exit(f"FAIL: cave ({len(code)} B) overruns into live code")

    hits = relocs_in_range(data, CAVE_VA, end)
    if hits:
        sys.exit("FAIL: cave overlaps relocs at %s" % ", ".join("%#x" % h for h in hits))

    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    print(f"  cave {CAVE_VA:#x}..{end:#x} ({len(code)} B): CAVE_ABORT={cave_abort:#x} CAVE_RESUME={cave_resume:#x}")

    for va, target, what in (
        (HOOK_ABORT, cave_abort, "net_poll entry -> CAVE_ABORT"),
        (HOOK_RESUME, cave_resume, "net_poll exit -> CAVE_RESUME"),
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
