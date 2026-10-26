#!/usr/bin/env python3
"""Force RX skb->ip_summed = CHECKSUM_NONE so the kernel software-verifies the TCP/IP
checksum instead of trusting the EMAC's HW offload.

Hypothesis (confirmed on-device: 98 cross-conn leaks / 960 conns with Tcp InErrs==0 ->
the kernel never re-checks): the leaked segment carries the *victim's* TCP header (so it
routes to the victim at the right seq) but a *foreign* payload. If the stack recomputes
the L4 checksum it will mismatch -> segment dropped -> TCP retransmits -> the app gets
correct bytes and the cross-connection leak is gone, regardless of the DMA race / ring
depth. This is the "let the kernel do the TCP work" fix (there is no TCP offload engine on
this chip, only checksum offload, so approaches 1+2 collapse to this one lever).

net_poll delivers each frame at .text 0xc198 (`bl netif_receive_skb`). The driver's own
`skb[0x68] &= ~0xc` at 0xc180 is NOT ip_summed (clearing it on-device did not trigger
verification). In the 2.6.27 sk_buff, ip_summed lives in the `flags1` byte just before
`protocol` (written at skb+0x6c via `strh` at 0xc190) -> byte 0x6a, bits[2:3]. We clear
bits 2,3 of both flags1 bytes 0x6a and 0x6b (0x6b = pkt_type/fclone, which are 0 for our
unicast RX skbs, so clearing is a no-op) to land CHECKSUM_NONE wherever it sits.

Cave reuses patch_emac_gate2.py's reloc-free slot 0xbd8c (proven to insmod cleanly).
Hook: net_poll `mov r0, r8` @0xc194 -> b CAVE_CSUM; cave clears the bits, replays the mov,
returns to the `bl netif_receive_skb` at 0xc198. Module renamed emac -> emaccsum.

Usage: patch_emac_csumnone.py <emac.ko> <out.ko>
Needs: arm-linux-gnueabi-{as,ld,objcopy,nm} on PATH.
"""
import os
import struct
import subprocess
import sys
import tempfile

TEXT = 0x58
CAVE_VA = 0xBD8C
HOOK_CSUM = 0xC194  # net_poll: mov r0, r8 (before netif_receive_skb) -> b CAVE_CSUM
SAFETY = 0xBD20  # net_poll: b 0xbd78 (use_debug>3) -> b 0xbe8c (skip the cave)
RET_CSUM = 0xC198  # bl netif_receive_skb
SKIP_TARGET = 0xBE8C
NAME_OFF = 0xF42C
NEW_NAME = b"emaccsum"

ORIG_CSUM = 0xE1A00008  # mov r0, r8
ORIG_SAFETY = 0xEA000014  # b 0xbd78

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
            [PFX + "ld", "-Ttext=%#x" % CAVE_VA,
             "--defsym", "ret_csum=%#x" % RET_CSUM,
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

    expect(HOOK_CSUM, ORIG_CSUM)
    expect(SAFETY, ORIG_SAFETY)

    code, syms = assemble_cave()
    cave_csum = syms["CAVE_CSUM"]
    assert cave_csum == CAVE_VA, f"CAVE_CSUM at {cave_csum:#x}, expected {CAVE_VA:#x}"
    end = CAVE_VA + len(code)
    if end > SKIP_TARGET:
        sys.exit(f"FAIL: cave ({len(code)} B) overruns into live code")

    hits = relocs_in_range(data, CAVE_VA, end)
    if hits:
        sys.exit("FAIL: cave overlaps relocs at %s" % ", ".join("%#x" % h for h in hits))

    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    print(f"  cave {CAVE_VA:#x}..{end:#x} ({len(code)} B, reloc-free): CAVE_CSUM={cave_csum:#x}")

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
