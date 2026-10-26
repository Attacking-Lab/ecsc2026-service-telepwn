#!/usr/bin/env python3
"""SECURITY FIX: neuter the two unbounded sscanf("%s") stack overflows in the emac /proc
write handlers (port_wproc_mode @0x1a130, port_wproc_port_qos @0x19ea4 in binja vaddrs;
file = vaddr-0xffa8). Both do sscanf(user_buf,"%s",&stackbuf[~12]) on a world-writable
(0777) /proc/emac/<if>_mode and _port_qos -> saved-LR overwrite -> ring0. A bounded "%Ns"
needs string relocation (no in-place room); the robust binary fix is to turn each handler
into a success-returning no-op: `mov r0, r2 ; mov pc, lr` (return count, never call sscanf).
This disables those two debug proc-writes (mode/port_qos) but eliminates the overflow.
Module renamed emac -> emacpfix. Usage: patch_emac_procfix.py <emac.ko> <out.ko>"""
import struct, sys
NAME_OFF=0xF42C; NEW_NAME=b"emacpfix"
ORIG=(0xE1A0C00D,0xE92DD870)             # mov r12,sp ; push {r4,r5,r6,r11,r12,lr,pc}
STUB=(0xE1A00002,0xE1A0F00E)             # mov r0,r2 ; mov pc,lr
SITES={'port_wproc_mode':0xA188,'port_wproc_port_qos':0x9EFC}
def main():
    if len(sys.argv)!=3: sys.exit(__doc__)
    d=bytearray(open(sys.argv[1],'rb').read())
    for name,fo in SITES.items():
        a,b=struct.unpack_from('<II',d,fo)
        if (a,b)!=ORIG: sys.exit(f"FAIL {name}@{fo:#x}: {a:#010x} {b:#010x} != prologue")
        struct.pack_into('<II',d,fo,*STUB)
        print(f"  {name} @file {fo:#x}: stubbed -> mov r0,r2 ; mov pc,lr")
    if bytes(d[NAME_OFF:NAME_OFF+5])!=b"emac\x00": sys.exit("FAIL: module name")
    d[NAME_OFF:NAME_OFF+len(NEW_NAME)+1]=NEW_NAME+b"\x00"
    print(f"  module name: emac -> {NEW_NAME.decode()}")
    open(sys.argv[2],'wb').write(d); print(f"wrote {sys.argv[2]} ({len(d)} bytes)")
main()
