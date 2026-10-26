#!/usr/bin/env python3
"""COMBINED driver: skb-copy leak fix (patch_emac_copy2.py: 256-ring + delivery-time
skb-copy cave + reloc surgery) + /proc sscanf("%s") BOF mitigation (patch_emac_procfix.py:
stub port_wproc_mode/_port_qos). Chains copy2, then stubs the two handlers (disjoint file
regions). Module -> emacfull. Usage: patch_emac_full.py <emac.ko> <out.ko>"""
import os, struct, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
ORIG = (0xE1A0C00D, 0xE92DD870)          # mov r12,sp ; push {...}
STUB = (0xE1A00002, 0xE1A0F00E)          # mov r0,r2 ; mov pc,lr
STUBS = {'port_wproc_mode': 0xA188, 'port_wproc_port_qos': 0x9EFC}
NAME_OFF = 0xF42C
def main():
    if len(sys.argv) != 3: sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, 'copy2.ko')
        subprocess.run([sys.executable, os.path.join(HERE, 'patch_emac_copy2.py'),
                        sys.argv[1], tmp], check=True)
        d = bytearray(open(tmp, 'rb').read())
    for name, fo in STUBS.items():
        a, b = struct.unpack_from('<II', d, fo)
        if (a, b) != ORIG: sys.exit(f"FAIL {name}@{fo:#x}: {a:#010x} {b:#010x} != prologue")
        struct.pack_into('<II', d, fo, *STUB)
        print(f"  stub {name} @file {fo:#x}: mov r0,r2 ; mov pc,lr")
    if bytes(d[NAME_OFF:NAME_OFF+8]) != b'emaccpy2': sys.exit(f"FAIL name {bytes(d[NAME_OFF:NAME_OFF+9])!r}")
    d[NAME_OFF:NAME_OFF+8] = b'emacfull'
    print("  module name: emaccpy2 -> emacfull")
    open(sys.argv[2], 'wb').write(d); print(f"wrote {sys.argv[2]} ({len(d)} bytes)")
main()
