#!/usr/bin/env python3
"""DIAGNOSTIC patch: zero every RX buffer's DRAM before the NIC DMAs into it.

Tests whether the cross-connection leak is a buffer delivered *un-written* by the
DMA engine (it still holds a recycled previous frame) vs. the engine writing a
*wrong* frame into it:

  * If un-written: the delivered frame becomes all-zeros -> the stack drops it
    (bad eth/ip headers) -> TCP retransmits -> the foreign leak DISAPPEARS.
  * If wrong-frame: the engine overwrites the zeros with the (wrong) frame ->
    the foreign leak PERSISTS.

How: in netdev_net_poll's buffer-install path, insert `memset(newbuf, 0x800)`
before the cache op, and change that cache op from dma_cache_maint(...,DMA_FROM_DEVICE
=invalidate) to (...,DMA_BIDIRECTIONAL=clean+invalidate) so the zeros are written
back to DRAM before the buffer is handed to the engine.

Technique (same as patch_emac.py): code cave in the dead use_debug hexdump block,
repurpose its bl-printk relocation -> memset, no section resize. Also renames the
module emac -> emacfix so a reboot reloads the pristine driver.

Usage: patch_emac_zerotest.py <emac.ko> <out.ko>
"""
import struct
import sys

TEXT = 0x58
RELTEXT_OFF = 0xFB94
RELTEXT_SIZE = 0x2EF0
SYM_PRINTK = 767
SYM_MEMSET = 779
R_ARM_CALL = 28
POISON = 0xAA  # buffer payload poison byte (conn ids are 0..~150, so 0xAAAAAAAA is unmistakable)
NAME_OFF = 0xF42C
NEW_NAME = b"emacfix"


def fo(text_off):
    return TEXT + text_off


def arm_b(p, target):
    return 0xEA000000 | (((target - (p + 8)) >> 2) & 0xFFFFFF)


# (.text offset, expected original word, new word)
PATCHES = [
    # install path: r0=r5=newbuf here. divert into the cave, change cache dir 2->0.
    (0xBCB4, 0xE3A01B02, arm_b(0xBCB4, 0xBD78)),  # b cave ; was mov r1,#0x800
    (0xBCB8, 0xE3A02002, 0xE3A02000),             # mov r2,#0 (BIDIRECTIONAL) ; was #2
    # cave @ 0xbd78 (dead debug block): memset(newbuf+0x20, 0xAA, 0x600) -- poison the
    # PAYLOAD region (the header 0..0x20 is left for the engine to write so the frame
    # still routes to its connection; 0x20..0x620 < 2048 so no skb_shared_info overrun).
    (0xBD78, 0xE59F14FC, 0xE2850020),  # add r0, r5, #0x20  (newbuf + 0x20)
    (0xBD7C, 0xE1A02005, 0xE3A010AA),  # mov r1, #0xaa      (poison byte)
    (0xBD80, 0xE1A03006, 0xE3A02E60),  # mov r2, #0x600     (1536 bytes)
    (0xBD84, 0xE59F04F4, 0xE1A00000),  # nop
    # 0xBD88 stays ebfffffe (bl) -- reloc repurposed printk -> memset
    (0xBD8C, 0xE5D60003, 0xE1A00005),  # mov r0, r5  (restore newbuf)
    (0xBD90, 0xE5D61000, 0xE3A01B02),  # mov r1, #0x800 (restore size for dma_cache_maint)
    (0xBD94, 0xE5D62001, arm_b(0xBD94, 0xBCB8)),  # b back to mov r2,#0 ; bl dma_cache_maint
]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(sys.argv[1], "rb").read())

    for t, expect, new in PATCHES:
        cur = struct.unpack_from("<I", data, fo(t))[0]
        if cur != expect:
            sys.exit(f"FAIL @.text+{t:#x}: expected {expect:#010x}, found {cur:#010x}")
        struct.pack_into("<I", data, fo(t), new)
        print(f"  .text+{t:#06x}: {expect:#010x} -> {new:#010x}")

    old = (SYM_PRINTK << 8) | R_ARM_CALL
    new = (SYM_MEMSET << 8) | R_ARM_CALL
    idx = data.find(struct.pack("<II", 0xBD88, old), RELTEXT_OFF, RELTEXT_OFF + RELTEXT_SIZE)
    if idx < 0:
        sys.exit("FAIL: bl@0xbd88 printk relocation not found")
    struct.pack_into("<I", data, idx + 4, new)
    print(f"  reloc @{idx:#x}: printk -> memset")

    if bytes(data[NAME_OFF : NAME_OFF + 5]) != b"emac\x00":
        sys.exit("FAIL: module name not 'emac'")
    data[NAME_OFF : NAME_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print("  module name: emac -> emacfix")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
