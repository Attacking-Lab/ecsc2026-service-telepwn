#!/usr/bin/env python3
"""DIAGNOSTIC probe: printk skb struct words 0x64/0x68/0x6c at net_poll delivery, to find
the kernel sk_buff `ip_summed` offset/value for THIS vendor build and decide whether the
RX-checksum avenue is alive. NO ip_summed modification — pristine behaviour + a dmesg line
per delivered frame.

Reuses the dead `use_debug>3` hexdump block and its EXISTING `bl printk` R_ARM_CALL reloc at
.text+0xbd88 (so no new relocation — loader-safe). Layout (cave entered only via the hook):

  0xbd78  add  r0, pc, #0x14     ; r0 = &fmt (@0xbd94)
  0xbd7c  ldr  r1, [r8, #0x64]   ; word 0x64 (bytes 64..67)
  0xbd80  ldr  r2, [r8, #0x68]   ; word 0x68 (bytes 68..6b -> ip_summed lives here)
  0xbd84  ldr  r3, [r8, #0x6c]   ; word 0x6c (low half = skb->protocol = 0x0008 -> validates)
  0xbd88  bl   printk            ; ORIGINAL bytes kept; reloc fixes target to printk
  0xbd8c  mov  r0, r8            ; replay the displaced net_poll insn
  0xbd90  b    0xc198            ; -> bl netif_receive_skb
  0xbd94  "EMACPRB %08x %08x %08x\n"

r8 = skb at the hook (net_poll 0xc194 `mov r0,r8`). printk is AAPCS (r0=fmt,r1..r3=args),
preserves r4-r11 (r8/skb survives), clobbers only r0-r3/ip/lr (all scratch here; the bl sets
lr=0xbd8c so printk returns into our continuation). Hook 0xc194 -> b 0xbd78; safety 0xbd20 ->
b 0xbe8c so use_debug>3 can't fall into the cave. Module renamed emac -> emacprob.

Read it on-phone: load, send a few RX frames, `dmesg | grep EMACPRB`. On PRISTINE the 0x68
word reveals ip_summed: UNNECESSARY(1) => kernel trusts HW (forcing NONE *might* enable
verify); NONE(0) already => kernel doesn't SW-verify NONE => checksum avenue is dead.

Usage: patch_emac_probe.py <emac.ko> <out.ko>
"""
import struct
import sys

TEXT = 0x58
NAME_OFF = 0xF42C
NEW_NAME = b"emacprob"
FMT_VA = 0xBD94
FMT = b"EMACPRB %08x %08x %08x\n\x00"

SHT_REL = 9


def arm_b(src, dst):
    return 0xEA000000 | (((dst - (src + 8)) >> 2) & 0xFFFFFF)


# (va, expected_original, new_word)
WORDS = [
    (0xBD78, 0xE59F14FC, 0xE28F0014),  # ldr r1,[pc,#0x4fc]  -> add r0, pc, #0x14
    (0xBD7C, 0xE1A02005, 0xE5981064),  # mov r2, r5          -> ldr r1, [r8, #0x64]
    (0xBD80, 0xE1A03006, 0xE5982068),  # mov r3, r6          -> ldr r2, [r8, #0x68]
    (0xBD84, 0xE59F04F4, 0xE598306C),  # ldr r0,[pc,#0x4f4]  -> ldr r3, [r8, #0x6c]
    # 0xBD88 = bl printk (0xEBFFFFFE) KEPT — its R_ARM_CALL reloc fixes the target
    (0xBD8C, 0xE5D60003, 0xE1A00008),  # ldrb r0,[r6,#3]     -> mov r0, r8
    (0xBD90, 0xE5D61000, arm_b(0xBD90, 0xC198)),  # ldrb r1,[r6] -> b 0xc198
]
HOOK = (0xC194, 0xE1A00008, arm_b(0xC194, 0xBD78))  # mov r0,r8 -> b 0xbd78
SAFETY = (0xBD20, 0xEA000014, arm_b(0xBD20, 0xBE8C))  # b 0xbd78 -> b 0xbe8c
KEEP_BL = (0xBD88, 0xEBFFFFFE)


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

    def ck(va, exp):
        cur = struct.unpack_from("<I", data, fo(va))[0]
        if cur != exp:
            sys.exit(f"FAIL @.text+{va:#x}: expected {exp:#010x}, found {cur:#010x}")

    # keep the printk bl + its reloc untouched
    ck(*KEEP_BL)
    # every byte we OVERWRITE must be reloc-free (0xbd88 stays; string region 0xbd94..)
    bad = [h for h in relocs_in_range(data, 0xBD78, FMT_VA + len(FMT)) if h != 0xBD88]
    if bad:
        sys.exit("FAIL: would clobber relocs at %s" % ", ".join("%#x" % h for h in bad))

    for va, exp, new in WORDS + [HOOK, SAFETY]:
        ck(va, exp)
        struct.pack_into("<I", data, fo(va), new)
        print(f"  .text+{va:#06x}: {exp:#010x} -> {new:#010x}")

    data[fo(FMT_VA):fo(FMT_VA) + len(FMT)] = FMT
    print(f"  .text+{FMT_VA:#06x}: fmt {FMT!r} ({len(FMT)} B)")

    if bytes(data[NAME_OFF:NAME_OFF + 5]) != b"emac\x00":
        sys.exit("FAIL: module name not 'emac'")
    data[NAME_OFF:NAME_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print(f"  module name: emac -> {NEW_NAME.decode()}")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
