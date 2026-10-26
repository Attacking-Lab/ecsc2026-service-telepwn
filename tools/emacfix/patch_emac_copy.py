#!/usr/bin/env python3
"""skb-COPY fix: at net_poll delivery, copy large RX frames out of the live DMA ring buffer
into a fresh skb and deliver the copy, so the app never reads a buffer the engine can still
overwrite (the post-verification race that defeats the checksum). Small frames (<=128 B:
ACKs/ARP/SYN, not leak carriers) pass through untouched.

Registers confirmed live at the hook (probe2): r5 = frame start (eth header, = r6+4),
r7 = full ethernet frame length, r8 = old skb. The original delivers data=IP (eth pulled
by eth_type_trans), len=r7-14, so the copy takes src=r5+14, len=r7-14 and sets
new->dev/protocol from the old skb, new->ip_summed=NONE (kernel re-sums the *stable* copy,
catching any pre-copy corruption while delivering good post-copy data).

The cave calls dev_alloc_skb/skb_put/memcpy/dev_kfree_skb_any — none reusable from the lone
spare printk reloc — so this patch performs **relocation-table surgery**: it appends 4
R_ARM_CALL entries to .rel.text and shifts every following section + the SHT. Cave at the
reloc-free 0xbd8c; hook net_poll mov r0,r8 @0xc194 -> b cave; safety 0xbd20 -> b 0xbe8c.
Module renamed emac -> emaccopy.

Usage: patch_emac_copy.py <emac.ko> <out.ko>   (also applies the 256-ring + works standalone)
"""
import os
import struct
import subprocess
import sys
import tempfile

TEXT = 0x58
CAVE_VA = 0xBD8C
HOOK = 0xC194
SAFETY = 0xBD20
RET = 0xC198
SKIP = 0xBE8C
NAME_OFF = 0xF42C
NEW_NAME = b"emaccopy"
ORIG_HOOK = 0xE1A00008
ORIG_SAFETY = 0xEA000014
PLACEHOLDER = 0xEBFFFFFE
R_ARM_CALL = 28
SHT_REL = 9

# bl targets in cave order -> symbol table index
CALL_SYMS = [772, 616, 650, 723]  # dev_alloc_skb, skb_put, memcpy, dev_kfree_skb_any

# 256-ring (disjoint from the cave); keep it so the copy runs at full throughput
RING_PATCHES = [
    (0xCB4C, 0xE3A01B02, 0xE3A01A02),
    (0xCC18, 0xE3550B02, 0xE3550A02),
    (0xBCEC, 0xE353003F, 0xE35300FF),
    (0xBD28, 0xE352003F, 0xE35200FF),
    (0xBD50, 0xE352003F, 0xE35200FF),
]

CAVE_ASM = """
	.syntax unified
	.arch armv6
	.text
CAVE_COPY:
	cmp   r7, #0x80
	bls   .Lorig
	mov   r0, #0x600
	.word 0xebfffffe        @ bl dev_alloc_skb
	cmp   r0, #0
	beq   .Lorig
	str   r0, [sp, #-8]!
	sub   r1, r7, #14
	.word 0xebfffffe        @ bl skb_put   (r0=new -> returns dest)
	sub   r2, r7, #14
	add   r1, r5, #14
	.word 0xebfffffe        @ bl memcpy    (r0=dest,r1=src,r2=len)
	ldr   r0, [sp]
	ldr   r1, [r8, #0x18]
	str   r1, [r0, #0x18]
	ldrh  r1, [r8, #0x6c]
	strh  r1, [r0, #0x6c]
	mov   r1, #0
	strb  r1, [r0, #0x69]
	mov   r0, r8
	.word 0xebfffffe        @ bl dev_kfree_skb_any
	ldr   r0, [sp], #8
	b     ret_copy
.Lorig:
	mov   r0, r8
	b     ret_copy
"""

PFX = "arm-linux-gnueabi-"


def arm_b(src, dst):
    return 0xEA000000 | (((dst - (src + 8)) >> 2) & 0xFFFFFF)


def assemble():
    with tempfile.TemporaryDirectory() as d:
        s, o, e, b = (os.path.join(d, f) for f in ("c.s", "c.o", "c.elf", "c.bin"))
        open(s, "w").write(CAVE_ASM)
        subprocess.run([PFX + "as", "-march=armv6", "-o", o, s], check=True)
        subprocess.run([PFX + "ld", "-Ttext=%#x" % CAVE_VA, "--defsym", "ret_copy=%#x" % RET, "-o", e, o], check=True)
        subprocess.run([PFX + "objcopy", "-O", "binary", "--only-section=.text", e, b], check=True)
        code = bytearray(open(b, "rb").read())
        bls = [i for i in range(0, len(code), 4) if struct.unpack_from("<I", code, i)[0] == PLACEHOLDER]
        return code, bls


def sht(data):
    e_shoff = struct.unpack_from("<I", data, 0x20)[0]
    entsize = struct.unpack_from("<H", data, 0x2E)[0]
    num = struct.unpack_from("<H", data, 0x30)[0]
    strndx = struct.unpack_from("<H", data, 0x32)[0]
    secs = []
    for i in range(num):
        off = e_shoff + i * entsize
        v = list(struct.unpack_from("<10I", data, off))
        secs.append({"hdr_off": off, "name": v[0], "type": v[1], "off": v[4], "size": v[5], "info": v[7]})
    return secs, strndx, e_shoff, entsize


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(sys.argv[1], "rb").read())

    def fo(va):
        return TEXT + va

    def ck(va, exp):
        cur = struct.unpack_from("<I", data, fo(va))[0]
        if cur != exp:
            sys.exit(f"FAIL @.text+{va:#x}: {cur:#010x} != {exp:#010x}")

    # ring
    for t, exp, new in RING_PATCHES:
        ck(t, exp)
        struct.pack_into("<I", data, fo(t), new)
    # hook + safety expected
    ck(HOOK, ORIG_HOOK)
    ck(SAFETY, ORIG_SAFETY)

    code, bls = assemble()
    if len(bls) != 4:
        sys.exit(f"FAIL: expected 4 placeholder bls, found {len(bls)} at {bls}")
    end = CAVE_VA + len(code)
    if end > SKIP:
        sys.exit(f"FAIL: cave overruns ({len(code)} B)")

    # reloc-free check on the cave bytes (the printk reloc at 0xbd88 is before the cave)
    secs, strndx, e_shoff, entsize = sht(data)
    strtab = secs[strndx]["off"]

    def secname(n):
        return data[strtab + n:data.index(b"\x00", strtab + n)].decode()

    text_idx = next(i for i, s in enumerate(secs) if secname(s["name"]) == ".text")
    reltext = next(s for s in secs if s["type"] == SHT_REL and s["info"] == text_idx)
    for j in range(reltext["size"] // 8):
        ro, _ = struct.unpack_from("<II", data, reltext["off"] + j * 8)
        if CAVE_VA <= ro < end:
            sys.exit(f"FAIL: existing reloc {ro:#x} inside cave")

    # splice cave + hooks
    data[fo(CAVE_VA):fo(CAVE_VA) + len(code)] = code
    struct.pack_into("<I", data, fo(HOOK), arm_b(HOOK, CAVE_VA))
    struct.pack_into("<I", data, fo(SAFETY), arm_b(SAFETY, SKIP))
    print(f"  cave {CAVE_VA:#x}..{end:#x} ({len(code)} B); bls at {[hex(CAVE_VA+b) for b in bls]}")

    # --- relocation-table surgery: append 4 R_ARM_CALL entries to .rel.text ---
    new_rels = b"".join(
        struct.pack("<II", CAVE_VA + bls[i], (CALL_SYMS[i] << 8) | R_ARM_CALL) for i in range(4)
    )
    insert_at = reltext["off"] + reltext["size"]
    data[insert_at:insert_at] = new_rels  # shift everything after by 32
    grow = len(new_rels)
    # fix section headers (table itself may have shifted)
    new_e_shoff = e_shoff + (grow if e_shoff >= insert_at else 0)
    for i in range(len(secs)):
        hoff = new_e_shoff + i * entsize
        s_off = struct.unpack_from("<I", data, hoff + 0x10)[0]
        s_size = struct.unpack_from("<I", data, hoff + 0x14)[0]
        if i == secs.index(reltext):
            struct.pack_into("<I", data, hoff + 0x14, s_size + grow)  # .rel.text grew
        elif s_off >= insert_at:
            struct.pack_into("<I", data, hoff + 0x10, s_off + grow)
    if e_shoff >= insert_at:
        struct.pack_into("<I", data, 0x20, new_e_shoff)
    print(f"  +4 relocs at .rel.text (syms {CALL_SYMS}); file +{grow} B")

    ck2 = bytes(data[NAME_OFF:NAME_OFF + 5])
    if ck2 != b"emac\x00":
        sys.exit(f"FAIL: module name {ck2!r}")
    data[NAME_OFF:NAME_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print(f"  module name: emac -> {NEW_NAME.decode()}")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
