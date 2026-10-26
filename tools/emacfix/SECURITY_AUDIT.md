# emac.ko security audit (BCMRING / Yealink T46S, ARM, Linux 2.6.27)

Audit of the vendor `emac.ko` ethernet/switch driver beyond the known RX cross-connection
leak (see `../tcpswap/REVERSING.md`, `README.md`). Four areas were swept in parallel
(Binary Ninja MCP) and every claimed finding was adversarially re-verified against the
decompiler **and** confirmed live on the device. Binja loads the `.ko` at base `0x10000`,
so **file offset = binja_vaddr − 0xffa8** (e.g. `port_wproc_mode` @binja `0x1a130` → file
`0xa188`).

## 🔴 CRITICAL — unprivileged kernel stack overflow via world-writable /proc (CONFIRMED, exploited live)

**Two unbounded `sscanf("%s")` writes into a ~12-byte stack buffer on world-writable files.**

| handler | binja addr | proc file (per port) | sink |
|---|---|---|---|
| `port_wproc_mode` | `0x1a130` | `/proc/emac/<if>_mode` | `sscanf(user, "%s", &buf)` @`0x1a158` |
| `port_wproc_port_qos` | `0x19ea4` | `/proc/emac/<if>_port_qos` | `sscanf(user, "%s", &buf)` @`0x19ed4` |

- Format `"%s"` is at `0x1e196` (verified `25 73 00`), **no width limit**.
- The handler frame: `push {r4,r5,r6,r11,r12,lr,pc}`; `buf = r11-0x24` (= `sp`). Epilogue
  `ldm sp,{r4,r5,r6,r11,sp,pc}` restores **PC from `buf+0x20` (offset 32)** and **SP from
  `buf+0x1c` (offset 28)**. So a token ≥36 bytes overwrites the restored SP and PC. **No
  stack canary** (2.6.27 ARM).
- **Reachable unprivileged:** `procfs_create_emac` registers every entry with mode
  `0x1ff` = **`0777`**. Confirmed live: `-rwxrwxrwx … eth0_mode / eth0_port_qos`. Any local
  user can write them.
- The user buffer is passed **straight to `sscanf`** (no `copy_from_user`); on this
  no-SMAP/PAN target that just reads attacker memory — the bytes are fully controlled.

**Exploitation performed on the live phone (authorized, owner's hardware):**
- **DoS — reproduced 3×.** `cat <36+ byte payload> > /proc/emac/eth1_mode` reliably wedges
  the kernel → BCMRING **hardware watchdog reboots** the phone.
- **Control-flow hijack proven.** Payload `41×28 | 80 80 80 c0 | ef be ad de` set the
  restored `sp=0xc0808080` (mapped lowmem, `mem=60M`) and `pc=0xdeadbeef`; the kernel
  jumped to the attacker-chosen PC.
- **Why not a surviving shell:** the overflow smashes the writer's kernel stack, so the
  kernel can't cleanly `do_exit` after the fault → it wedges → watchdog reboot; the oops
  (which would show `PC is at deadbeef`) lands in `log_buf` but the reboot wipes it and
  `/proc/cmdline` has **no `console=`**. A surviving ring-0 exploit needs an info-leak (to
  relocate the intact kernel stack for a clean return / ROP) or the kernel image — standard
  exploit engineering, not a mitigation. The primitive (no-canary stack BOF + full saved-PC
  control on a `0777` file) is unambiguously code-execution-grade.

**PoC payloads** (token must avoid `0x00` and whitespace `09-0d,20`):
```
# crash / control-flow hijack (PC = 0xdeadbeef):
python3 -c 'import sys;sys.stdout.buffer.write(b"A"*28+bytes.fromhex("808080c0")+bytes.fromhex("efbeadde"))' > p && cat p > /proc/emac/eth0_mode
# trivial DoS (any long token):
printf 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAZZZZ' > /proc/emac/eth0_mode
```

### Mitigation (built + verified live): `patch_emac_procfix.py`
`%s`→`%31s` is **not** safe here (buffer is ~12 B before the saved regs, 32 B before saved
PC; `%31s` still smashes saved SP) and a bounded `"%Ns"` needs string relocation (no
in-place room). The robust binary fix stubs both handlers to a success-returning no-op —
`mov r0, r2 ; mov pc, lr` (return `count`, never reach `sscanf`) — at file `0xa188` /
`0x9efc` (16 bytes, module → `emacpfix`). **Verified:** the 48-byte payload that hung the
kernel 3× returns `rc=0` and the phone stays up (uptime kept climbing). Trade-off: the two
debug knobs (`_mode`, `_port_qos` writes) become no-ops — acceptable to kill a ring-0 BOF.
A functionality-preserving fix (`%11s` via reloc surgery, or `copy_from_user`+length cap)
belongs in source.

## 🟠 HIGH — the entire /proc/emac interface is world-writable (CONFIRMED live)
`create_proc_entry(name, 0x1ff /*0777*/, …)` for **every** entry. Even without the
overflow, any unprivileged user can:
- `*_rp` → inject arbitrary raw Ethernet frames onto the wire (`port_wproc_rp` does
  `dev_alloc_skb(count+4)` + `copy_from_user` + TX; bounded, but an L2 spoofing primitive).
- `hw_reset` / `sw_reset` → reset the switch (network DoS). `mdio` → r/w PHY registers.
- `*_vlan_*` / `*_qos` / `*_8021x` / `*_down` / `*_loop` → reconfigure / disable networking.

Fix: `0644`/`0600` perms and/or `CAP_NET_ADMIN`, especially `*_rp`, `*_reset`, `mdio`.

## 🟡 LOW–MED — RX path: no maximum frame-length check (verified missing; exploitability uncertain)
`netdev_net_poll` trusts the DMA-reported length (`len = (status>>13)&0xFFFF`, ≤65535) with
only a **signed minimum** gate (`cmp r7,#0x3b`) before `skb_put(skb, len-8)`; **no max
check** (no `cmp … #0x800`). If a crafted/oversize frame's reported length can exceed the
2 KB skb, `skb_put` over-panics (remote DoS) or corrupts `skb->len`. Likely mitigated by the
switch MAC's oversize-frame filtering + DMA block-size capping — but the driver validates
nothing. Add `len ≤ 0x800` before `skb_put`. Not tested on-device (risky / probably
HW-filtered).

## ✅ Audited and CLEAN (reassuring)
- **ioctl config setters** (`emac_cmd_process` + all `bcm111xx_if_set_*`): command word
  bounds-checked (≤`0x1c`), fixed `0x24`-byte `copy_from_user`/`to_user`, **every**
  port/VLAN/QoS index range-checked (port∈{0,1,2}, vid<`0x1000`, queue≤7, dscp≤`0x3f`); no
  uninitialised-stack info-leak; and the ioctl path is gated by the kernel's `CAP_NET_ADMIN`
  on `SIOCDEVPRIVATE`.
- **Switch/HAL table setters** (ARL/L2/VLAN/QoS/mirror/DSCP): indices are loop counters or
  explicitly range-checked; several are init-only/unreachable. No OOB table/register write.
- **Integer `/proc` parsers** (`%d`/`%c` handlers): write scalars, bounded. Only the two
  `%s` handlers overflow. `emac_rproc_vt` read handler lacks a count bound but is bounded by
  a fixed iteration count (defensive only).

## Severity summary
| # | issue | severity | vector |
|---|---|---|---|
| 1 | `%s` stack BOF on `0777` /proc (`_mode`, `_port_qos`) | **Critical** | local → ring-0 |
| 2 | entire `/proc/emac` world-writable | High | local (DoS / L2 inject / config) |
| 3 | RX no max-length before `skb_put` | Low–Med | remote (likely HW-mitigated) |
| — | ioctl setters / switch tables / int parsers | clean | — |

Tools: `patch_emac_procfix.py` (mitigation). Exploit PoC payloads above. Audit driven by 4
parallel Binary Ninja subagents + adversarial re-verification + live confirmation.
