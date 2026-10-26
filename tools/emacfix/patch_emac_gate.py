#!/usr/bin/env python3
"""PROVABLY-ZERO fix: add the missing descriptor-ownership GATE to emac.ko's RX path.

Root cause (see GATE_DESIGN.md): the RX DMA engine runs a free-running circular
descriptor ring with no producer/consumer handshake, so under backlog it laps the NAPI
poll and overwrites buffers being delivered -> one connection's frame leaks into
another's. A bigger ring only widens the window. This patch makes the engine *halt* at a
moving terminator and lets net_poll re-arm it, so the producer can never overtake the
consumer -> 0 leaks by construction (overload degrades to frame-drop, not leak).

Two code caves in the dead `use_debug>3` hexdump block (.text 0xbd78..0xbe8c):
  CAVE_OPEN  (hooked from net_open cc44): set desc[63] as initial terminator, old_T=63.
  CAVE_GATE  (hooked from net_poll c1c8): at each NAPI-complete, advance the terminator
             to (C-1) and restart the channel if it halted.
Cave machine code is produced by the ARM assembler/linker (not hand-encoded), spliced in
with no section resize; module renamed emac->emacfix so a reboot reloads the pristine
driver from the RO rootfs.

Usage: patch_emac_gate.py <emac.ko> <out.ko>
Needs: arm-linux-gnueabi-{as,ld,objcopy,nm} on PATH.
"""
import struct
import subprocess
import sys
import tempfile
import os

TEXT = 0x58  # .text file offset
CAVE_VA = 0xBD78  # dead use_debug hexdump block start
HOOK_OPEN = 0xCC44  # net_open: add r2,r9,#0x1d4   -> b CAVE_OPEN
HOOK_GATE = 0xC1C8  # net_poll: mrs r0,CPSR        -> b CAVE_GATE
SAFETY = 0xBD20  # net_poll: b 0xbd78 (use_debug>3) -> b 0xbe8c (skip the cave)
RET_OPEN = 0xCC48  # CAVE_OPEN returns here
RET_GATE = 0xC1CC  # CAVE_GATE returns here
SKIP_TARGET = 0xBE8C  # normal deliver resume (also the SAFETY retarget)
NAME_OFF = 0xF42C
NEW_NAME = b"emacfix"

ORIG_OPEN = 0xE2892F75  # add r2, r9, #0x1d4
ORIG_GATE = 0xE10F0000  # mrs r0, CPSR
ORIG_SAFETY = 0xEA000014  # b 0xbd78

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
	ldr   ip, [r10, #0x1b4]
	cmp   ip, #0
	beq   .Ldone
	ldr   r1, [r10, #0x1bc]
	sub   r1, r1, #1
	and   r1, r1, #0x3f
	ldr   r2, [r10, #0x1c0]
	and   r2, r2, #0x3f
	cmp   r1, r2
	beq   .Lrestart
	add   r3, ip, r1, lsl #5
	ldr   lr, [r3, #0x0c]
	bic   lr, lr, #0x18000000
	str   lr, [r3, #0x0c]
	add   r3, ip, r2, lsl #5
	ldr   lr, [r3, #0x0c]
	orr   lr, lr, #0x18000000
	str   lr, [r3, #0x0c]
	str   r1, [r10, #0x1c0]
.Lrestart:
	mov   r3, #0xf3000000
	orr   r3, r3, #0x300000
	ldr   lr, [r3, #0x3a0]
	tst   lr, #4
	bne   .Ldone
	add   r2, r2, #1
	and   r2, r2, #0x3f
	ldr   ip, [r10, #0x1b4]
	add   ip, ip, r2, lsl #5
	ldr   r1, [ip, #0x00]
	str   r1, [r3, #0xb0]
	ldr   r1, [ip, #0x04]
	str   r1, [r3, #0xb8]
	ldr   r1, [ip, #0x0c]
	str   r1, [r3, #0xc8]
	ldr   r1, [ip, #0x10]
	str   r1, [r3, #0xcc]
	ldr   ip, [r10, #0x1b8]
	add   ip, ip, r2, lsl #5
	str   ip, [r3, #0xc0]
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

    # splice the cave
    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    print(f"  cave {CAVE_VA:#x}..{end:#x} ({len(code)} B): CAVE_OPEN={cave_open:#x} CAVE_GATE={cave_gate:#x}")

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
    print("  module name: emac -> emacfix")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
