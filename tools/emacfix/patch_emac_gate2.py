#!/usr/bin/env python3
"""PROVABLY-ZERO fix v3: descriptor-ownership GATE for emac.ko's RX path.

Same gate as patch_emac_gate.py (moving LLP terminator + restart, see GATE_DESIGN.md)
but fixes the bug that made v2's module REJECTED at insmod ("invalid module format"):

  The code cave lives in the dead `use_debug>3` hexdump block (.text 0xbd78..0xbe8c),
  which contains two `bl printk` calls carrying **R_ARM_CALL relocations** (.rel.text
  offsets 0xbd88 and 0xbe88). At insmod the module loader applies those relocations ON
  TOP OF a cave placed at 0xbd78, rewriting the cave instruction at 0xbd88 into a garbage
  `bl printk`. (v2 put the cave at 0xbd78; the on-device symptom was the driver failing to
  load, the network dropping, and the phone looking hung — confirmed via a persistent
  on-USB swap log: `emacgate: unknown relocation: 0` when we first tried neutralizing the
  reloc to R_ARM_NONE -- which this 2.6.27 ARM loader has no case for and rejects.)

  Fix: place the cave at 0xbd8c, in the GAP between the two relocs (0xbd88 < cave,
  0xbe88 > cave end), so the cave is relocation-free and the relocs land harmlessly on the
  dead skipped bytes around it. No relocation edits -> valid module format. A build-time
  assertion enforces that no .rel.text reloc falls inside the cave.

Usage: patch_emac_gate2.py <emac.ko> <out.ko>
Needs: arm-linux-gnueabi-{as,ld,objcopy,nm} on PATH.
"""
import os
import struct
import subprocess
import sys
import tempfile

TEXT = 0x58  # .text file offset
# Dead use_debug>3 hexdump block spans 0xbd78..0xbe8c and carries two R_ARM_CALL(printk)
# relocations at 0xbd88 and 0xbe88. This 2.6.27 ARM module loader has NO R_ARM_NONE case
# (it rejects type 0 as "unknown relocation" -> "invalid module format"), so the cave must
# simply AVOID those reloc offsets: place it at 0xbd8c, in the gap between the two relocs
# (0xbd8c..0xbe60 for a 212B cave, both relocs land on dead skipped bytes outside it).
CAVE_VA = 0xBD8C
HOOK_OPEN = 0xCC44  # net_open: add r2,r9,#0x1d4   -> b CAVE_OPEN
HOOK_GATE = 0xC1C8  # net_poll: mrs r0,CPSR        -> b CAVE_GATE
SAFETY = 0xBD20  # net_poll: b 0xbd78 (use_debug>3) -> b 0xbe8c (skip the cave)
RET_OPEN = 0xCC48  # CAVE_OPEN returns here
RET_GATE = 0xC1CC  # CAVE_GATE returns here
SKIP_TARGET = 0xBE8C  # normal deliver resume (also the SAFETY retarget)
NAME_OFF = 0xF42C
NEW_NAME = b"emacgate"  # distinct from the 256-ring's "emacfix" so /proc/modules disambiguates

ORIG_OPEN = 0xE2892F75  # add r2, r9, #0x1d4
ORIG_GATE = 0xE10F0000  # mrs r0, CPSR
ORIG_SAFETY = 0xEA000014  # b 0xbd78

SHT_REL = 9

CAVE_ASM = """
	.syntax unified
	.arch armv6
	.text
CAVE_OPEN:
	ldr   r3, [r4, #0x0c]
	bic   r3, r3, #0x18000000
	str   r3, [r4, #0x0c]
	mov   r3, #0x3f
	str   r3, [r9, #0x1c0]
	add   r2, r9, #0x1d4
	b     ret_open
CAVE_GATE:
	push  {r1, r2, r3, ip, lr}
	mov   r3, #0xf3000000
	orr   r3, r3, #0x300000
	ldr   lr, [r3, #0x3a0]
	tst   lr, #4
	bne   .Ldone
	ldr   ip, [r10, #0x1b4]
	cmp   ip, #0
	beq   .Ldone
	ldr   r1, [ip, #0x00]
	str   r1, [r3, #0xb0]
	ldr   r1, [ip, #0x04]
	str   r1, [r3, #0xb8]
	ldr   r1, [ip, #0x0c]
	str   r1, [r3, #0xc8]
	ldr   r1, [ip, #0x10]
	str   r1, [r3, #0xcc]
	ldr   r1, [r10, #0x1b8]
	str   r1, [r3, #0xc0]
	mov   lr, #4
	orr   lr, lr, #0x400
	str   lr, [r3, #0xa0]
.Ldone:
	pop   {r1, r2, r3, ip, lr}
	mrs   r0, CPSR
	b     ret_gate
"""

PFX = "arm-linux-gnueabi-"


def arm_b(src, dst):
    return 0xEA000000 | (((dst - (src + 8)) >> 2) & 0xFFFFFF)


def assemble_cave():
    """Assemble+link the cave at CAVE_VA; return (bytes, {sym: va})."""
    with tempfile.TemporaryDirectory() as d:
        s, o, e, b = (os.path.join(d, f) for f in ("c.s", "c.o", "c.elf", "c.bin"))
        open(s, "w").write(CAVE_ASM)
        subprocess.run([PFX + "as", "-march=armv6", "-o", o, s], check=True)
        subprocess.run(
            [PFX + "ld", "-Ttext=%#x" % CAVE_VA,
             "--defsym", "ret_open=%#x" % RET_OPEN,
             "--defsym", "ret_gate=%#x" % RET_GATE,
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
        name, typ, flags, addr, offset, size, link, info, align, entsize = struct.unpack_from("<10I", data, off)
        secs.append(dict(name=name, type=typ, offset=offset, size=size, link=link, info=info, entsize=entsize))
    return secs, e_shstrndx


def relocs_in_range(data, lo, hi):
    """Return [r_offset, ...] for .rel.text relocations with lo <= r_offset < hi."""
    secs, shstrndx = section_headers(data)
    strtab = secs[shstrndx]["offset"]

    def secname(nameoff):
        end = data.index(b"\x00", strtab + nameoff)
        return data[strtab + nameoff:end].decode()

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

    expect(HOOK_OPEN, ORIG_OPEN)
    expect(HOOK_GATE, ORIG_GATE)
    expect(SAFETY, ORIG_SAFETY)

    code, syms = assemble_cave()
    cave_gate = syms["CAVE_GATE"]
    cave_open = syms["CAVE_OPEN"]
    assert cave_open == CAVE_VA, f"CAVE_OPEN at {cave_open:#x}, expected {CAVE_VA:#x}"
    end = CAVE_VA + len(code)
    if end > SKIP_TARGET:
        sys.exit(f"FAIL: cave ({len(code)} B) overruns into live code at {SKIP_TARGET:#x}")

    # THE FIX: the cave must be relocation-free (this loader rejects R_ARM_NONE, so we
    # can't neutralize — we place the cave to avoid the printk relocs instead).
    hits = relocs_in_range(data, CAVE_VA, end)
    if hits:
        sys.exit("FAIL: cave [%#x,%#x) overlaps .rel.text relocs at %s — move CAVE_VA"
                 % (CAVE_VA, end, ", ".join("%#x" % h for h in hits)))

    # splice the cave
    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    print(f"  cave {CAVE_VA:#x}..{end:#x} ({len(code)} B, reloc-free): "
          f"CAVE_OPEN={cave_open:#x} CAVE_GATE={cave_gate:#x}")

    # hooks + safety retarget
    for va, target, what in (
        (HOOK_OPEN, cave_open, "net_open -> CAVE_OPEN"),
        (HOOK_GATE, cave_gate, "net_poll -> CAVE_GATE"),
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
