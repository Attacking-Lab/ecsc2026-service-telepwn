#!/usr/bin/env python3
"""Binary-patch the phone's `emac.ko` to add the missing RX delivery-time cache
invalidate (the root cause of the cross-connection TCP receive leak).

The driver invalidates RX buffers only at *install* (dma_cache_maint(newbuf,...))
and never re-invalidates the *received* buffer before the CPU reads it, so the
socket copy can serve a recycled buffer's stale (foreign) payload. This inserts
`dma_cache_maint(i_1, 0x800, DMA_FROM_DEVICE)` on the received buffer (held in r6)
right before its first payload read in netdev_net_poll.

Technique (no section resize, no new relocation):
  * The `use_debug>3` hexdump block is dead code -> code cave.
  * A `bl printk` in that cave (.text+0xbd88) already has an R_ARM_CALL relocation;
    we keep the instruction bytes (ebfffffe) and just repoint that relocation's
    symbol from printk (idx 767) to dma_cache_maint (idx 805).
  * One instruction at the first payload read (.text+0xbe8c, `ldrb r3,[r6,#0xf]`)
    is overwritten with a branch into the cave; the cave does the invalidate,
    replays the displaced ldrb, and branches back.

Addressing: `.text` is at file offset 0x58 (relocatable ELF, addr 0). Binary Ninja
shows VAs rebased by +0x10000, so file_off = (BN_VA - 0x10000) + 0x58 = BN_VA - 0xffa8.

Usage: patch_emac.py <emac.ko> <emac_fixed.ko>
"""
import struct
import sys

TEXT_FILE_OFF = 0x58  # .text section file offset (readelf -S)
RELTEXT_OFF = 0xFB94  # .rel.text file offset
RELTEXT_SIZE = 0x2EF0
SYM_PRINTK = 767
SYM_DMA_CACHE_MAINT = 805
R_ARM_CALL = 28

# struct module.name lives at .gnu.linkonce.this_module + 0x0c (state[4]+list[8]).
# Section file offset 0xf420 -> name at 0xf42c. Renamed so a reboot reloads the
# pristine /usr/driver/emac.ko (the running module shows as the new name instead).
NAME_FILE_OFF = 0xF42C
NEW_NAME = b"emacfix"


def toff(text_off):
    return TEXT_FILE_OFF + text_off


def arm_b(p, target):
    """Encode `b target` (cond=AL) at .text offset p."""
    imm24 = ((target - (p + 8)) >> 2) & 0xFFFFFF
    return 0xEA000000 | imm24


# (.text offset, expected original word, new word) -- little-endian 32-bit
PATCHES = [
    (0xBD78, 0xE59F14FC, 0xE1A00000),  # nop (mov r0,r0); was ldr r1,[pc,#1276]
    (0xBD7C, 0xE1A02005, 0xE1A00006),  # mov r0, r6            ; was mov r2,r5
    (0xBD80, 0xE1A03006, 0xE3A01B02),  # mov r1, #0x800        ; was mov r3,r6
    (0xBD84, 0xE59F04F4, 0xE3A02002),  # mov r2, #2 (FROM_DEV) ; was ldr r0,[pc,#1268]
    # 0xBD88 stays ebfffffe (bl) -- only its relocation symbol changes
    (0xBD8C, 0xE5D60003, 0xE5D6300F),  # ldrb r3,[r6,#0xf] (replay); was ldrb r0,[r6,#3]
    (0xBD90, 0xE5D61000, arm_b(0xBD90, 0xBE90)),  # b 0xbe90 (return); was ldrb r1,[r6]
    (0xBE8C, 0xE5D6300F, arm_b(0xBE8C, 0xBD78)),  # b 0xbd78 (detour); was ldrb r3,[r6,#0xf]
]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    data = bytearray(open(sys.argv[1], "rb").read())

    # 1) instruction patches
    for text_off, expect, new in PATCHES:
        fo = toff(text_off)
        cur = struct.unpack_from("<I", data, fo)[0]
        if cur != expect:
            sys.exit(
                f"FAIL @.text+{text_off:#x} (file {fo:#x}): "
                f"expected {expect:#010x}, found {cur:#010x} -- wrong file?"
            )
        struct.pack_into("<I", data, fo, new)
        print(f"  patched .text+{text_off:#06x}: {expect:#010x} -> {new:#010x}")

    # 2) repoint the bl@0xbd88 relocation from printk -> dma_cache_maint
    old_info = (SYM_PRINTK << 8) | R_ARM_CALL
    new_info = (SYM_DMA_CACHE_MAINT << 8) | R_ARM_CALL
    target = struct.pack("<II", 0xBD88, old_info)  # r_offset, r_info
    idx = data.find(target, RELTEXT_OFF, RELTEXT_OFF + RELTEXT_SIZE)
    if idx < 0:
        sys.exit("FAIL: R_ARM_CALL@0xbd88->printk relocation not found")
    struct.pack_into("<I", data, idx + 4, new_info)
    print(
        f"  reloc @file {idx:#x}: r_info {old_info:#x} (printk) -> "
        f"{new_info:#x} (dma_cache_maint)"
    )

    # 3) rename the module (struct module.name) so a reboot reloads the pristine
    #    /usr/driver/emac.ko; the running module shows as NEW_NAME instead.
    old_name = bytes(data[NAME_FILE_OFF : NAME_FILE_OFF + 5])
    if old_name != b"emac\x00":
        sys.exit(f"FAIL: module name @ {NAME_FILE_OFF:#x} is {old_name!r}, not 'emac\\0'")
    data[NAME_FILE_OFF : NAME_FILE_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print(f"  module name: 'emac' -> {NEW_NAME.decode()!r}")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
