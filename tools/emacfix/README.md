# emacfix — binary-patched `emac.ko` to root-cause & fix the RX cross-connection leak

Causal verification for the cross-connection TCP receive leak (see
`../tcpswap/REVERSING.md`): patch the Broadcom `emac` driver and measure the leak on the
phone. The history below walks three hypotheses — delivery-time cache invalidate
(refuted), buffer-poison test (ruled out stale buffers), and the **64→256 RX-ring
resize** (RESULT 3: confirmed the missing DMAC LLP gate, ~6× fewer leaks).

## Reproducing the modules (binaries are **gitignored** — never pushed)

Every `*.ko` here is produced deterministically by a Python byte-patcher from the
pristine driver `../../filesystem/usr/driver/emac.ko`; the `.ko` outputs are gitignored.
Rebuild any of them with:

```
# 256-ring fix (RESULT 3 — the one to load)
python3 patch_emac_bigring.py ../../filesystem/usr/driver/emac.ko emacfix_bigring.ko
# delivery-time cache invalidate (RESULT 1 — refuted)
python3 patch_emac.py          ../../filesystem/usr/driver/emac.ko emacfix.ko
# payload-poison diagnostic (RESULT 2)
python3 patch_emac_zerotest.py ../../filesystem/usr/driver/emac.ko emacfix_zerotest.ko
```

All three rename the module `emac`→`emacfix` (file offset `0xf42c`) so a reboot reloads
the pristine driver from the read-only rootfs. `swap.sh` insmods `/tmp/emacfix.ko`, so
copy whichever module you are testing to that name when staging.

## The fix

The driver invalidates RX DMA buffers only at *install* and never re-invalidates the
*received* buffer before the CPU reads it, so the socket copy can serve a recycled
buffer's stale (foreign) payload. `patch_emac.py` injects
`dma_cache_maint(received_buf, 0x800, DMA_FROM_DEVICE)` into `netdev_net_poll` right
before the first payload read.

Technique — **no section resize, no new relocation, vermagic untouched** so `insmod`
accepts it:
- The `use_debug>3` hexdump block is dead code → code cave at `.text+0xbd78`.
- A `bl printk` there (`.text+0xbd88`, `ebfffffe`) already has an `R_ARM_CALL`
  relocation; keep the bytes and repoint that reloc's symbol printk(767)→
  dma_cache_maint(805).
- Overwrite the first payload read (`.text+0xbe8c`, `ldrb r3,[r6,#0xf]`) with a
  branch into the cave; the cave sets `r0=r6` (the received buffer), `r1=0x800`,
  `r2=2`, calls `dma_cache_maint`, replays the `ldrb`, and branches back.

Verified patched flow:
```
be8c: b bd78
bd78: nop ; mov r0,r6 ; mov r1,#0x800 ; mov r2,#2
bd88: bl dma_cache_maint        ; R_ARM_CALL -> dma_cache_maint
bd8c: ldrb r3,[r6,#15]          ; replayed, now reads post-DMA data
bd90: b be90
```
Integrity: file size unchanged (98462), `.rel.text` count unchanged (1541),
vermagic `2.6.27.47 preempt mod_unload ARMv6`.

## Build

```
python3 patch_emac.py ../../filesystem/usr/driver/emac.ko emac_fixed.ko
arm-linux-gnueabi-objdump -dr -j .text emac_fixed.ko | sed -n '/bd74:/,/bd98:/p'   # verify
cmp -l ../../filesystem/usr/driver/emac.ko emac_fixed.ko                            # only patched bytes differ
```

## Deploy (⚠ physical access required — see swap.sh)

Staged but **not executed** (remote-only recovery is unsafe for a live `rmmod`).
`emac_fixed.ko` and `swap.sh` are relayed to the phone's `/tmp` (root fs is RO, so
`/usr/driver/emac.ko` stays pristine and any reboot recovers).

When physically at the phone:
```
( sh /tmp/swap.sh </dev/null >/tmp/swap.log 2>&1 & )   # swaps driver, restores eth0
# reconnect, then:
touch /tmp/ok                                          # cancel the watchdog reboot
```
`swap.sh` baked-in net config: IP 192.168.69.200/24, MAC 80:5E:C0:55:DD:8F, GW
192.168.69.1. If `/tmp/ok` isn't created within ~120 s (net never returned), it
reboots into the original driver. A kernel panic is **not** caught — power-cycle.

## Verify the fix (with mserver running on the phone, `tools/leakmin`)

Leak rate from teru is low (~0.2–0.3%), so use large N:
```
# baseline (BEFORE swap):  ~10k conns -> expect ~20-40 LEAK lines
ssh teru "bash -c 'for i in \$(seq 1 200); do /tmp/mclient 192.168.69.200:22000 --sweep 48 --segs 64; done'"
# after swap + touch /tmp/ok + restart mserver: identical hammer -> expect 0 LEAK lines
```
Restore: reboot (RO root → pristine `emac`), or `( sh -c 'ifconfig eth0 down; rmmod emacfix; insmod /usr/driver/emac.ko; ifconfig eth0 192.168.69.200 netmask 255.255.255.0 up; route add default gw 192.168.69.1' </dev/null >/tmp/restore.log 2>&1 & )`.

## RESULT (2026-06-12): the cache-invalidate fix does NOT fix the leak — hypothesis refuted

Ran the causal test on-device (`emacfix` loaded under its renamed module, the
original `emac` rmmod'd; mserver from `tools/leakmin`, load from teru):

| driver | conns | LEAK lines |
|--------|------:|-----------:|
| `emac` (baseline) | 2880 | **9** |
| `emacfix` (delivery-time invalidate) | 2880 | **5** |

5/2880 (0.17%) vs 9/2880 (0.31%) is **within the run-to-run variance** we've seen
(1–9 leaks per ~3k conns); the leaks under `emacfix` are still genuine (verbatim,
cross-connection, `same_off=false`). **Adding `dma_cache_maint(received_buf,…,
DMA_FROM_DEVICE)` before the payload read did not eliminate the leak.** The patch
*does* execute (the network functions, which requires the detour's replayed BRCM
tag-check `ldrb` to run).

**Conclusion:** the missing delivery-time invalidate (real anti-pattern in the
static analysis) is **not the cause**, or not the whole cause. Once the NIC DMA has
completed, DRAM holds the correct bytes, so any invalidate-then-read yields fresh
data — therefore the foreign payload must be **physically in DRAM**, not stale in
cache. That points at a **DMA / descriptor / buffer-management** defect (e.g. a
recycled buffer delivered without a fresh/complete DMA, or a descriptor↔buffer
aliasing/refill race) rather than a cache-coherency bug. The empirical leak
signature (verbatim cross-connection MSS payload, `same_off=false`) still stands —
only the *mechanism* in `REVERSING.md §3/§5` is wrong and must be revised.

This is exactly why the patch test was worth running: the gold-standard causal
experiment refuted a confidently-argued static hypothesis.

Note: `swap.sh`'s watchdog used `$((...))` (busybox ash here doesn't support it) and
`ifconfig hw ether` (different busybox syntax) — both failed harmlessly because the
network came back on its own; fix before relying on the watchdog. Also: hammering
the buggy `emac` hard (~12k conns in a tight loop) **crashed/rebooted the phone**
once (watchdog reset) — keep buggy-driver load moderate (≤~80 sweeps).

## RESULT 2 (2026-06-12): poison test — ACTIVE foreign-payload DMA, not a stale buffer

`patch_emac_zerotest.py` poisons each RX buffer's **payload** region
(`memset(newbuf+0x20, 0xAA, 0x600)`) and uses `dma_cache_maint(...,0=clean+invalidate)`
so the poison reaches DRAM before the engine fills the buffer. Hot-swapped and hammered:

- **8 leaks, 0 poison** — every leak showed a *real* `src_conn` with `verbatim=true`
  (a real foreign payload), never `0xAAAAAAAA`.

So the DMA engine **actively writes another connection's payload into the victim's
buffer**, overwriting the poison — it is **not** an un-written/stale recycled buffer
(that would show poison). (The clean reaches DRAM for sure: an earlier full-2 KB
`memzero`+clean crashed by corrupting `skb_shared_info` *in DRAM*.)

Because the foreign `conn_id` appears exactly at the app-payload start, the buffer ends
up = **victim's eth/ip/tcp header** (so it routes to the victim at the right seq) **+ a
foreign connection's TCP payload**, split at the 54-byte header boundary. That is a
**header/payload frame-segmentation desync** in the BCMRING MAC-RX-FIFO / DMA path under
load — the victim's header gets paired with another frame's actively-DMA'd payload.
Cache invalidation cannot fix it (the wrong data is actively written to DRAM). The exact
register-level cause needs the Broadcom DMAC/MAC datasheet.

Pitfall: zeroing the full `0x800` from `newbuf` (= skb data+8) overruns into
`skb_shared_info` and crashes — poison only the payload (`newbuf+0x20 .. +0x620`, < 2048).

## RESULT 3 (2026-06-13): 64→256 RX-ring resize — leak cut ~6×, NOT eliminated

`patch_emac_bigring.py` enlarges the RX descriptor ring 64→256 to address the
confirmed root cause: the emac RX path runs a permanently-chained **circular** DW
DMAC descriptor ring with **no descriptor-ownership / LLP end-of-list gate** (unlike
the Broadcom BSP's `dmacHw_setVariableDataDescriptor`). Under load the DMA engine
**laps** the slower NAPI poll and overwrites descriptors the driver is delivering,
mixing one connection's frame into another's buffer. A correct gate needs the DW
DMAC channel-restart state machine (too risky to graft blind); a deeper ring instead
lets the engine tolerate longer poll backlogs before it can lap.

5 in-place word patches, no section resize, vermagic + file size unchanged, module
renamed `emac`→`emacfix` (reboot reloads pristine, RO rootfs):
- `.text+0xcb4c` ring `dma_alloc_coherent` size `0x800`→`0x2000`
- `.text+0xcc18` fill-loop bound (descriptor count) `0x800`→`0x2000`
- `.text+0xbcec/0xbd28/0xbd50` `netdev_net_poll` index wraps `0x3f`→`0xff`

The fill loop builds `desc[0..254]` with sequential phys LLPs (`r5+r7+0x20`), buffer
phys in `dar`, block-size in `ctl.hi`, then overwrites `desc[last].llp` with the ring
base (field `0x518`) for the circular wrap; the DW DMAC just follows the longer LLP
chain, so 256 is transparent to it. Alloc size, fill bound, and poll wraps all agree
on 256. Verified clean: only 8 bytes differ vs pristine (5 instruction bytes + 3 name
bytes). Cost ≈ +0x2000 ring + 256×~2.3 KB skbs ≈ +0.6 MB of 12.6 MB free.

Live test (renamed module, self-healing `swap.sh`, leakmin `mserver`, load from teru),
reboot-safe 80-wave (3840-conn) sessions with idle between (the phone has a cumulative
**load watchdog** that resets it after sustained load):

| driver | conns | LEAK lines | rate |
|--------|------:|-----------:|------|
| pristine `emac` (baseline) | 11520 | 19 | **0.165 %** |
| `emacfix` (256-ring) | 7680 | 2 | **0.026 %** |

**~6× fewer leaks**, statistically overwhelming (at the baseline rate ~12.7 leaks were
expected over the patched 7680 conns; **2** observed, P ≪ 0.001). The driver loads and
runs cleanly (net_open builds the 256-ring, no panic; eth0 RX/TX healthy).

**Conclusion: the missing LLP/descriptor-ownership gate is confirmed as the dominant
mechanism** — deepening the ring eliminates ~6 in 7 cross-connection mixes, exactly as
the lapping model predicts. But a **residual ~0.026 %** persists (steady ~1 leak per
3840 conns across two independent runs), so 256 descriptors is a **mitigation, not a
cure**: either a few backlogs still lap a 256-deep ring, or a second, rarer mechanism
(e.g. the header/payload frame-segmentation desync of RESULT 2) also contributes. The
*correct* fix remains the BSP gate-and-restart state machine (fill only FREE
descriptors, clear LLP on the new last = terminator, re-link + `dmacHw_initiateTransfer`
on refill) — see RESULT 4 for the attempt to land it as a binary patch.

Stability note: the watchdog reboot under sustained load is **load-general, not a patch
regression** — a control run of the *pristine* `emac` under the identical 7680-conn
tight load **also rebooted**. Both drivers survive ≤3840-conn bursts and reboot once
cumulative load runs long; the 256-ring's extra ~0.6 MB may nudge the threshold slightly
earlier but is not the cause. Recovery is automatic (RO rootfs reloads pristine `emac`).
Helper: `accum_test.sh <label> <sessions>` runs the reboot-safe accumulation.

## RESULT 4 (2026-06-14): provably-0 gate attempted, hard-hangs — ship the 256-ring fix

> **Superseded by RESULT 6 (2026-06-20).** The "hard-hangs the kernel at swap" below was a
> **misdiagnosis** — the gate `insmod`-failed (`invalid module format`: the cave overlapped
> `.rel.text` printk relocs and the loader rejects `R_ARM_NONE`), leaving eth0 down with no
> driver, which only *looked* like a hang. With the cave moved to the reloc-free gap the gate
> **loads and runs**; the real blocker is a hardware non-resumability, not a hang. Kept below
> as the historical record.

Goal: drive the residual 0.026 % to **0** with the real descriptor-ownership gate, as a
binary patch (`patch_emac_gate.py`, full design in `GATE_DESIGN.md`). The GPL-source
route is a **dead end** — Yealink's published "Linux Kernel" download is a 2.6.10 MIPS
MontaVista tree, not the 2.6.27.47 ARM BCMRING with `emac` source — so a binary patch is
the only path.

The gate (moving LLP-terminator advanced at `net_poll`'s NAPI-complete, restart-if-halted)
was fully reversed and built, but **v1 and v2 both hard-hang the kernel at swap** (even
with no load — just a ping). swap.sh's userspace watchdog can't recover a frozen
scheduler, so each attempt costs a physical power-cycle. A post-hang static re-audit
*cleared* every address/register assumption (bases `r9`==`sl`==`netdev+0x360`, the free
scratch word, DMAC ch2 offsets, code-cave isolation) but surfaced the real blocker:
**`net_poll` keeps no consumer index — it scans the ring for DONE-bit descriptors and
jumps `C` to each hit** (`bd24`–`bd70`). So `C` is the *last-delivered* slot, there is no
clean producer/consumer tail, and the gate's `new_T = C-1` model doesn't track the DMA
write frontier. A correct gate needs frontier tracking — a deeper change than a code-cave
allows. The HW watchdog (`/dev/watchdog`, `tools/wdtpet`) can't backstop this either: the
RX flood starves the userspace petter (softirq monopolizes the single core; `SCHED_FIFO`
is ENOSYS on 2.6.27), so it spuriously resets a *healthy* run. Details in `GATE_DESIGN.md`.

**Decision: ship the 256-ring mitigation** (RESULT 3) as the practical fix and close the
gate as designed-but-blocked (needs a UART serial console to debug informed, not blind).

Re-confirmation this session, two load profiles:

**(a) Laptop cadence — the realistic threat model** (`laptop_accum.sh`, driven from the
dev laptop which is on the LAN; 480-conn bursts of `--sweep 48` with 6 s idle between,
12000 conns each). Bursting + idle avoids the phone's load-reboot (it resets under
*sustained* continuous load, not cumulative — recovery between bursts keeps it up), so a
full 12k sample fits in one boot:

| driver | conns | LEAK lines | rate |
|--------|------:|-----------:|------|
| pristine `emac` (baseline) | 12000 | 5 | **0.042 %** |
| `emacfix` (256-ring)       | 12000 | **0** | **0 %** |

The baseline leaks (5, clustered in a single backlog event); the 256-ring driver shows
**zero leaks across 12000 conns**. (0/12000 is "below detection at this load," not
literally proven-zero — the gentler idle lowers the rate vs continuous load.)

**(b) teru continuous — heavier, exposes the residual** (`accum_test.sh`, 3840-conn
continuous sessions; one session per boot, phone reboots near ~7680 cumulative):

| driver | conns | LEAK lines | rate |
|--------|------:|-----------:|------|
| pristine `emac` (baseline) | 3840 | 10 | **0.26 %** |
| `emacfix` (256-ring)       | 3840 | 1  | **0.026 %** |

~10× this run; patched **0.026 %** is identical to RESULT 3's independent run. Combined
teru sessions: patched **3 / 11520 = 0.026 %** vs baseline **29 / 15360 ≈ 0.19 %**. The
256-ring reduces leaks ~6–10× under continuous load and drops them below detection under
the laptop-realistic burst load. Stable and reproducible.

(Harness note: `accum_test.sh`/`laptop_accum.sh` had two latent bugs fixed here —
`grep -c` exits 1 on a zero count, and phonesh echoes the command, so reboot-detection
must test the parsed post-`SEP` value, not `grep NOLOG` over the echoed command.)

## RESULT 5 (2026-06-20): live continuous full-MSS LAN A/B — proves "window, not fix"

Re-determined from scratch this session: re-reversed the RX path (`netdev_net_open`,
`netdev_net_poll`, `netdev_ethIsrRx`, `netdev_dmaStop` in Binary Ninja) **and** ran a fresh
on-phone A/B driven the realistic way — the **dev laptop on the direct LAN at full 1500
MTU** (not teru, not the Tailscale 1280-MTU path), **continuous** 48-wide full-MSS waves
(no idle), identical load on both drivers, only the module swapped:

| driver | conns | LEAK lines | rate | |
|--------|------:|-----------:|------|---|
| pristine `emac` (64-ring)  | 1440 | **218** | **15.1 %** | |
| `emacfix` (256-ring)       | 1440 | **7**   | **0.49 %** | 31× fewer — **not zero** |

Both leak the *same* way (`same_off=false verbatim=true` + occasional torn `verbatim=false`).
**Verdict: the 256-ring is a window-widener, not a cure** — a strong ~31× mitigation that
still leaks the same cross-connection segments under deep-enough backlog.

Two things this run corrects vs RESULT 4:
- **The realistic threat is severe.** Full-MSS on the direct LAN with *no idle* leaks
  **15 %**, not the 0.04–0.26 % seen before. The earlier low figures were sender-throttle
  artifacts: teru's slow Pi NIC, and the laptop's Tailscale 1280-MTU path. RESULT 4(a)'s
  `0/12000` on the 256-ring was an **idle-recovery artifact** (6 s gaps let the ring drain),
  *not* "fixed" — continuous full-MSS leaks 0.49 % on the same driver.
- **The residual scales with attacker strength** — 0.026 % (teru) → 0.49 % (laptop full-MSS),
  ~19× worse. A real fix is 0 % at *any* load; a bigger buffer leaks more the harder you push,
  because the free-running ring is still ungated (no LLP terminator / owner bit). This is the
  defining signature of can-kicking.

Static confirmation (this session, independent of the prior notes): the engine is a
**free-running circular** DW-DMAC ring — `netdev_net_open` wires `desc[last].LLP = ring_base`
(`0x1cc40`) with LLP-enable bits set on every descriptor (`CTL.lo = 0x30488000 & 0x18000000`),
armed once (`0xf33000c0 = ring_base`, `0xf33003a0 = 0x404`). The RX ISR (`netdev_ethIsrRx`)
only acks + `__napi_schedule` (no stop/re-arm); `netdev_net_poll` only refills + advances its
read index `C` (no gate vs the DMA write frontier). `patch_emac_bigring.py` changes only the
ring size (`0x800→0x2000`) + 3 wrap masks (`0x3f→0xff`) + the name = **8 bytes**, zero new
sync. (Bonus tell of a hasty bump: `netdev_dmaStop`'s free loop `cmp r5, #0x800` at `0x1c680`
is unpatched, so unload frees only 64 of 256 buffers.)

The correct fix remains the BSP gate-and-restart (clear LLP-enable on the last *refilled*
descriptor = moving end-of-list terminator the consumer advances; `dmacHw_initiateTransfer`
over reclaimed descriptors only) → serializes producer behind consumer → provably 0.

Live-swap method notes (cost three reboots to nail down; all auto-recovered via the RO
rootfs reloading pristine `emac`): (1) **`rmmod emac` hangs** if the swap follows heavy load —
TIME_WAIT sockets pin the netdev refcount (`unregister_netdevice: waiting for eth0`), so swap
from a *freshly-booted, no-load* state; (2) this busybox **rejects `head -1`** (needs
`head -n 1`) — a swap-confirm poll that parsed with `head -1` silently failed and let the
watchdog false-fire; (3) the laptop's **ARP entry for .200 goes stale** on every eth0 flap —
`ping` before each telnet to force re-resolution; (4) keep the swap watchdog lenient (≥600 s)
so it can't false-fire during the test, and touch `/tmp/ok` unconditionally once telnet is back.

## RESULT 6 (2026-06-20): the gate loads — and no binary gate beats the 256-ring

Followed RESULT 5's "correct fix = the gate" all the way to a built, loading, measured gate.
Three findings, full register-level detail + tables in **`GATE_DESIGN.md`** (§2026-06-20):

- **The gate loads** (corrects RESULT 4). The swap "hang" was `insmod: invalid module format`,
  not a kernel hang: the code cave at `.text+0xbd78` overlapped two `R_ARM_CALL` printk relocs
  (`.rel.text` `0xbd88`, `0xbe88`) that the 2.6.27 ARM loader applies *over* the cave, and the
  loader also rejects `R_ARM_NONE` (so zeroing the reloc type fails too). Moving the cave to
  **`.text+0xbd8c`** (the reloc-free gap between the two relocs) and editing **no** relocs →
  `emacfix_gate2.ko` `insmod`s cleanly, RX/TX healthy. (`patch_emac_gate2.py`.)

- **The terminator gate wedges — by hardware, not by bug.** `tools/dmacpeek` (a `/dev/mem`
  DMAC ch2 reader/writer, with a `bounce` recovery-probe mode) proved the DW-AHB-DMAC
  distinction: a channel disabled by **software abort** (`ChEnReg=0x400` *while running*) is
  **resumable** (`=0x404` restarts RX, any FIFO state); a channel that **completes an
  LLP-terminator block** enters a **non-resumable** transfer-complete latch (`ChEn` bit 2 stuck
  0). The terminator design's whole premise — complete *into* a terminator, then re-arm — hits
  exactly the one stop-state the engine won't resume. No register sequence (`bounce` 0–5)
  revives it. So every terminator-restart variant stalls RX under load.

- **Abort gates: resumable, but can't reach 0.49 %.** Pivot to gating with the *resumable*
  abort (`patch_emac_abort.py` / `patch_emac_sgate.py`):

  | gate | module | leak | throughput |
  |------|--------|-----:|------------|
  | abort every `net_poll` | `emacabrt` | n/a | **DoS — 48/1440 conns in 440 s** |
  | selective abort, `gap ≥ 16` | `emacsgat` | **3.8 %** | full |
  | selective abort, `gap ≥ 48` | `emacsgat` | **9.0 %** | full |
  | **256-ring (shipped)** | `emacfix` | **0.49 %** | full |

  Abort-every-poll freezes the engine even when the consumer keeps up → over-throttle DoS.
  Selective abort (freeze only when the engine's frontier gap exceeds a threshold) keeps full
  throughput but samples the gap at poll *entry*, so a burst laps the consumer inside the
  **NAPI-masked inter-poll window** before the next check — structurally too coarse to be
  provably-0, and it never crosses the 256-ring.

**Verdict:** the provably-0 design is blocked at the hardware (terminator-completion
non-resumable); the resumable alternative is either a DoS or too coarse. A real fix needs to
restructure the producer/consumer handshake (a gated ring with a halt the engine *can* resume)
— a **source-level** change a per-poll code cave can't express. **The 256-ring is the best
binary fix** (31×, full throughput, never wedges). Provably-0 awaits the BCMRING source, which
is not public and which Yealink serves as the **wrong product** (their GPL "Linux Kernel" is a
2.6.10 MIPS Avalanche tree, verified on-disk — not the T46S's ARM BCMRING 2.6.27.47); route is
a GPL source demand to Yealink. Phone left on pristine `emac` (RO-rootfs boot driver).

## RESULT 7 (2026-06-25): RX-checksum approach — eliminate the leak by making the kernel re-verify? NO (throttle artifact)

Hypothesis: the leaked segment carries the *victim's* TCP header (routes to the victim) +
*foreign* payload, so if the kernel software-verifies the L4 checksum it must mismatch ->
drop -> retransmit -> correct data, leak gone regardless of ring depth. On this chip there is
no TCP offload engine, only **checksum offload**, so "don't set CHECKSUM_UNNECESSARY" and "let
the kernel do the TCP work" are the same lever: force `skb->ip_summed = CHECKSUM_NONE` at
delivery so TCP re-checksums. Patch = a cave hooking `net_poll`'s `mov r0,r8` @0xc194 (just
before `netif_receive_skb` @0xc198) that clears the `ip_summed` bits in the `flags1` bytes
(0x6a/0x6b, just before `skb->protocol` written at 0x6c). `patch_emac_csumnone.py` (module
`emaccsum`); combined with the 256-ring as `patch_emac_ring_csum.py` (module `emacrcsm`).

On-phone A/B (leakmin mserver + laptop full-MSS mclient, 960 conns/run; loadtest = dtmf_epoll
capacity; nping = bad-checksum SYN probe):

| driver | ring | csum cave | leaks/960 | leak-burst time | DTMF conn-errors (30/60/90/120) |
|--------|------|-----------|----------:|-----------------|----------------------------------|
| pristine `emac` | 64 | — | 98 | 16 s | 5 / 6 / 9 / 13 |
| `emaccsum` | 64 | yes | **0** | **28 s (slow)** | 4 / 8 / 7 / 13 |
| `emacrcsm` | 256 | yes | **6 (0.6 %)** | 16 s | **1 / 1 / 3 / 2** |
| 256-ring alone (`emacfix`) | 256 | — | ~5 (0.49 %) | 16 s | (low) |

**Verdict: the checksum approach does NOT fix the leak — `emaccsum`'s clean 0 was a throttle
artifact, not a backstop.** The tell is `emacrcsm`: adding the 256-ring (which restores full
speed, 16 s) makes the cave's "protection" **vanish** — 6 leaks, `Tcp InErrs` did not move,
identical to the 256-ring *with no cave*. So `emaccsum`'s 0 came from its 28 s **slowness**
flow-controlling the senders enough to stop the ring overrunning; the instant the ring removes
that throttle the leak returns. (This is the RESULT-4 idle-recovery trap again.) The throttle
in `emaccsum` is *not* the checksum's CPU cost (negligible) but **retransmits of corruption
caught under the 64-ring** — yet `InErrs` barely moved, so the cave is almost certainly **not
actually flipping `ip_summed`** (0x6a/0x6b is the wrong byte for this vendor sk_buff; the
driver's own pre-existing `bic` on 0x68 isn't it either — clearing it never triggered
verification). The kernel sk_buff `ip_summed` offset for this exact build is unknown and blind
guessing has now failed twice.

**nping verification probe is inconclusive *by design*:** a bad-TCP-checksum SYN is dropped
(`Rcvd: 0`) on **both** pristine and `emaccsum`/`emacrcsm` — the **EMAC hardware discards
bad-wire-checksum frames** before the kernel sees them, so the test can't reach kernel
software-verification. Useful corollary: the leak's frames have **valid wire checksums** (the
corruption is in DRAM, *after* HW validation), and `net_poll` refills the descriptor with a
fresh buffer *before* delivering the old one (0xc bcc8 vs 0xc198) and the kernel's checksum
runs synchronously inside `net_poll` — so the corruption is **pre-delivery** (engine laps and
overwrites the buffer before `net_poll` reads it), which a delivery-time skb *copy* would NOT
escape (it would copy already-corrupt data). The only thing that *could* work is making the
kernel actually re-verify (correct `ip_summed` offset) — pre-delivery corruption would then be
caught — which needs an **instrumented build to read `ip_summed` at delivery** (dmesg/kmem),
not another blind patch.

**Bonus (a):** the cave imposes **no real DTMF capacity cost** (latency identical to pristine,
~90 conns within the 15 s budget; an earlier "8× hit" was a measurement against the wrong —
leakmin — server). And the loadtest confirms the **256-ring genuinely cuts corruption** on the
real workload (DTMF conn-errors 5/6/9/13 -> 1/1/3/2) at zero throughput cost — re-validating it
as the best mitigation. Net: **256-ring stays the best fix (~0.5 %, full throughput); the
checksum approach is retracted as a non-fix.**

## RESULT 8 (2026-06-25): found the real ip_summed offset, characterized the bug as a *post-verification race*, and PROVED the skb-copy fix (0 leaks, full speed) — but it needs source to be load-robust

Instrumented the kernel sk_buff to settle the RX-checksum question definitively. A printk
probe (`patch_emac_probe.py`, reusing the dead hexdump block's `bl printk` reloc to dump skb
struct words at delivery; `patch_emac_probe2.py` dumps the cave registers) showed:

- **`ip_summed` is byte `0x69`** (not the mainline `0x6a` my first cave cleared — which is why
  it did nothing). The EMAC offloads RX checksum as **`CHECKSUM_COMPLETE`(2)** or
  **`UNNECESSARY`(1)** (`word@0x68` = `0x..0200`/`0x..0100`; `word@0x6c` low half `0x0008` =
  `protocol`=IP, validating alignment). So the kernel folds the HW-precomputed `skb->csum` and
  never re-sums the payload → DRAM corruption is invisible.
- Cave registers at the hook: **`r5` = frame start (`= r6+4`), `r7` = frame length, `r8` = skb.**

**Corrected checksum cave** (`patch_emac_ring_csum2.py`, clear `ip_summed@0x69`→NONE, +256-ring):
forcing `NONE` *does* engage kernel re-verification — `Tcp InErrs` climbed **+3** (flat with the
wrong offset) — **but 7 still leaked at full speed.** The +3 caught = corruption already present
at checksum time; the 7 = payload that was *good* when the kernel re-summed and got corrupted
**after**. So the bug is a **post-verification race**: the DW-DMAC, writing via its
register-latched DAR, overwrites the delivered buffer *after* `net_poll`'s synchronous checksum
but *before* the socket copy. **No checksum can catch it** — the checksum avenue is dead, proven
by engaging it and watching it lose the race.

**The skb-copy fix (`patch_emac_copy.py` / `..._copy2.py`, module `emaccpy2`)** — the only thing
that decouples the app's read from the live ring buffer. It needs to call
`dev_alloc_skb`/`skb_put`/`memcpy`/`eth_type_trans`/`dev_kfree_skb_any`, none reusable from the
lone spare printk reloc, so the patcher performs **relocation-table surgery** (appends 5
`R_ARM_CALL` entries to `.rel.text`, shifts every following section + the SHT). At delivery, for
frames `0x80 < len <= 0x5f0`, it allocates a fresh skb, `memcpy`s the frame in, replays
`eth_type_trans`, sets `ip_summed=NONE`, delivers the **copy**, and frees the original; small
frames pass through. Result:

| driver | conns | leaks | speed |
|--------|------:|------:|-------|
| pristine | 960 (sweep48) | 98 | 16 s |
| 256-ring + checksum@0x69 | 960 | 7 | 15 s |
| **256-ring + skb-copy (`emaccpy2`)** | **960 (sweep48)** | **0** | **13 s (full speed)** |

**0 leaks at full throughput — the post-verification-race diagnosis confirmed and the fix
validated.** (v1 skipped `eth_type_trans` and panicked on the first large frame; v2 replicates
the driver's delivery faithfully and runs clean.) **Caveat:** under heavier concurrency
(sweep 64+) the cave **panics** — it doubles transient RX skb memory per large frame, and with
`mserver` buffering whole streams that OOMs the ~12 MB phone (sweep64 limped 831/1024 in 85 s
then rebooted). A copy-break threshold / proper memory accounting would fix that — but that is a
**source-level** change (a clean `skb = skb_copy(skb, GFP_ATOMIC)` with the kernel's own
budgeting), not a blunt per-frame cave.

**Final verdict — binary patching, exhausted with proof:** the bug is a post-verification DMA
overwrite race; the **256-ring is the best *shippable* binary fix** (~0.5 %, full throughput,
robust); the **skb-copy is the validated deterministic fix (0 leaks, full speed)** but is only
load-robust in **source**. Probe tooling: `patch_emac_probe.py`, `patch_emac_probe2.py`,
`patch_emac_copy.py`, `patch_emac_copy2.py`, `swap_test.sh`.

## RESULT 9 (2026-06-25): verification battery — the skb-copy fix is correct to high confidence (two independent methods), with a binary-only memory ceiling

To rule out a false zero, the copy driver (`emaccpy2`) was cross-checked two independent ways
(a leak-count cannot detect truncation; a full-response check cannot be fooled by a foreign
conn-id slipping the modal test — both passing means the bytes are *complete and correct*):

| method | copy driver | pristine |
|--------|-------------|----------|
| leakmin LEAK lines (960 conns sweep48, ×2–3 runs) | **0** | 98 |
| DTMF full-response errors (10/30/48 conns) | 0 / 0 / 0 | 5 / 6 / — |
| DTMF errors, 5× sweep48 rounds | 0 every round | — |
| DTMF errors, 60 / 80 / 100 conns | **0 / 0 / 0** | — |

~670 DTMF connections to 100-concurrent with **zero data errors** (the lone "FAIL" at 100 is
*latency* — 76/100 within the 15 s budget, the same compute ceiling as pristine ~85 — not
corruption). Full speed throughout (13 s for 960 leakmin conns; ~6–13 s DTMF), so the 0 is not a
throttle artifact, and pristine demonstrably corrupts at the identical load. Mechanism is sound:
the copy snapshots the frame at delivery, decoupling the socket read from the buffer the engine
overwrites post-checksum.

**The one caveat (binary-only):** the cave allocates a fresh skb per large frame; under the
*pathological* leakmin server (which buffers whole streams in RAM) at sustained high concurrency
(~1500 conns) the extra churn tips the 12 MB phone into a **kernel OOM → reboot**. The real DTMF
workload (incremental, no whole-stream buffering) is stable to capacity. That ceiling is an
artifact of doing the copy in a blind code cave — a **source-level** `skb_copy(skb, GFP_ATOMIC)`
with the kernel's own memory budgeting (or a copy-break threshold) removes it.

**Conclusion:** the skb-copy is the **validated, correct, deterministic fix** (0 leaks + 0
full-response errors, two methods, reproducible, at capacity, full speed); the **256-ring stays
the best *shippable* binary mitigation** (robust, ~0.5%); and a production-grade deterministic
fix belongs in **source** (copy or descriptor gate, both trivial there).
