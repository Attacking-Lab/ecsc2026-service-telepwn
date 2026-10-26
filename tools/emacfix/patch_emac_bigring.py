#!/usr/bin/env python3
"""FIX ATTEMPT: enlarge the emac RX descriptor ring 64 -> 256.

Root cause (confirmed): the emac RX path runs a permanently-chained *circular* DW
DMAC descriptor ring with no descriptor-ownership / LLP end-of-list gate (unlike the
Broadcom BSP's dmacHw_setVariableDataDescriptor). Under load the DMA engine laps the
slower NAPI poll and races/overwrites descriptors the driver is delivering, mixing one
connection's frame data into another's buffer. A correct gate needs the DW DMAC
channel-restart state machine (too risky to graft via binary patch), so this instead
deepens the ring so the engine cannot lap the driver during the transient backlog
spikes that trigger the leak.

64 -> 256 descriptors. The RX ring size (0x800 = 64*32) is the same numeric value as the
2 KB RX *buffer* size, so only the two true ring-size sites are touched:
  - .text+0xcb4c  ring dma_alloc_coherent size      0x800 -> 0x2000
  - .text+0xcc18  fill-loop bound (descriptor count) 0x800 -> 0x2000
  - .text+0xbcec/0xbd28/0xbd50  net_poll index wraps 0x3f -> 0xff
(The 0x558 count field and the bic #0x3f sites are TX/other and left alone; the DW DMAC
just follows the longer circular LLP chain.)

Also renames the module emac -> emacfix so a reboot reloads the pristine driver.

Usage: patch_emac_bigring.py <emac.ko> <out.ko>
"""
import struct
import sys

TEXT = 0x58
NAME_OFF = 0xF42C
NEW_NAME = b"emacfix"


def fo(t):
    return TEXT + t


# (.text offset, expected original word, new word)
PATCHES = [
    (0xCB4C, 0xE3A01B02, 0xE3A01A02),  # mov r1, #0x2000   (ring alloc size; was #0x800)
    (0xCC18, 0xE3550B02, 0xE3550A02),  # cmp r5, #0x2000   (fill-loop bound; was #0x800)
    (0xBCEC, 0xE353003F, 0xE35300FF),  # cmp r3, #0xff     (advance wrap; was #0x3f)
    (0xBD28, 0xE352003F, 0xE35200FF),  # cmp r2, #0xff     (fwd-scan wrap; was #0x3f)
    (0xBD50, 0xE352003F, 0xE35200FF),  # cmp r2, #0xff     (fwd-scan wrap; was #0x3f)
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

    if bytes(data[NAME_OFF : NAME_OFF + 5]) != b"emac\x00":
        sys.exit("FAIL: module name not 'emac'")
    data[NAME_OFF : NAME_OFF + len(NEW_NAME) + 1] = NEW_NAME + b"\x00"
    print("  module name: emac -> emacfix")

    open(sys.argv[2], "wb").write(data)
    print(f"wrote {sys.argv[2]} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
