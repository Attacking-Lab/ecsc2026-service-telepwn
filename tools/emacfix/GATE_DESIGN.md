# Provably-zero RX leak: the descriptor-ownership GATE design

Deep-reverse of `emac.ko`'s RX DMA path to design a fix that makes the cross-connection
leak **impossible by construction** (not just rarer, as the 64→256 ring did). No phone
changes here — this is the design + risk assessment requested before patching.

## Why a bigger ring can never be provably 0

The RX path runs a **free-running circular** DW-AHB-DMAC descriptor ring with no
producer/consumer handshake:

- `net_open` builds a 64-entry circular ring (`desc[63].llp = ring_base`), every
  descriptor's `CTL.lo` has `LLP_SRC_EN|LLP_DST_EN` set, then starts the channel.
- The DW DMAC, in LLP multi-block mode paced by the ESW flow-controller, walks the LLP
  chain **forever**. It sets the per-descriptor DONE bit (`CTL.hi` bit 12) and writes the
  block byte-count to `status` (+0x14) as it fills each buffer, but it **never reads**
  any software-ownership bit before writing.
- `net_poll` (consumer) checks DONE, delivers the buffer, allocates a replacement,
  refills the descriptor (`dar`=new phys, `CTL.hi`=0x800 which clears DONE, `status`=0),
  advances its index — and **never stops or restarts the engine, never touches LLP**.
- The ISR (`netdev_ethIsrRx`) masks+clears the ch2 block/err interrupts and schedules
  NAPI. **No restart.**

So when RX backlog exceeds the ring depth, the engine **laps** the consumer and
overwrites buffers `net_poll` is mid-delivery → one connection's frame lands in
another's buffer (the leak). A deeper ring widens the window but a circular,
un-gated ring of *any* size can still be lapped. **Provably 0 requires the engine to
*halt* when it catches the consumer, and the consumer to *re-arm* it.**

## Primitives confirmed in the binary (all cheap, all already used by the driver)

Controller base `0xf3300000`; RX = **channel 2** (BSP `esw_rx` `dedicatedChannel 2`).
Per-channel regs at `base + 0x58*ch`; ch2 ⇒ `0xf33000b0`.

| op | exact writes | source in binary |
|----|--------------|------------------|
| **start / restart channel** | `*(u32*)0xf33000c0 = desc_phys` (LLP[ch2]); `*(u32*)0xf33003a0 = 0x404` (ChEnReg: wr-en ch2 + enable) | `net_open` d108–d130 — the driver's own start |
| **stop channel** | `*(u32*)0xf33003a0 = 0x400` (wr-en ch2, enable=0) | `netdev_dmaStop` / net_open error path |
| **engine idle?** | `(*(u32*)0xf33003a0 >> 2) & 1` == 0 | ChEnReg ch2 bit |
| **make desc a terminator** (engine halts after it) | `desc.CTL.lo (+0x0c) &= ~0x18000000` (clear LLP_SRC_EN\|LLP_DST_EN) | `CTL.lo` const `0x1a408037` |
| **re-link desc** (engine flows through it) | `desc.CTL.lo \|= 0x18000000` | — |

Restart needs **no** CFG/SAR/DAR reprogram: SAR (ESW FIFO `0x30488000`) and CTL are
auto-loaded from each descriptor via LLP; CFG (flow-control/handshake) persists across
disable/enable. The descriptor ring lives in `dma_alloc_coherent` (uncached) memory, so
descriptor writes are visible to the engine immediately (no cache flush, but ordering
still matters).

Descriptor (32 B, matches BSP `dmacHw_priv.h`): `sar`+0x00, `dar`+0x04, `llp`+0x08,
`CTL.lo`+0x0c, `CTL.hi`+0x10 (DONE=bit12, low 12b=block size), `status`+0x14,
`sstat`+0x18, `devCtl/bufp`+0x1c.

## Design A — moving terminator (full throughput, higher race risk)

Keep the ring circular; track the terminator at the consumer's refill frontier. In the
`net_poll` refill block (per drained desc `i`), after re-arming `desc[i]`:
1. re-link the previous terminator: `desc[term].CTL.lo |= 0x18000000`;
2. make `desc[i]` the new terminator: `desc[i].CTL.lo &= ~0x18000000`; `term = i`;
3. if engine idle (`ChEnReg` ch2 bit clear): restart at the first DONE-clear slot
   (`LLP[ch2]=&desc[resume]_phys; ChEnReg=0x404`).

- Pro: preserves NAPI batching / pipelining → no throughput loss.
- Con: the terminator hand-off races the still-running engine. If the engine is loading
  `desc[i].CTL.lo` exactly as we flip bit 27/28 → torn fetch; if we mis-order the
  re-link vs. terminator → either a 1-slot lap (leak survives) or a permanent stall (RX
  hang). This is the classic lock-free DMA producer/consumer hard part. **Risk: high**;
  needs on-hardware iteration to flush races.

## Design B — batch stop-and-go (RECOMMENDED: simplest provably-correct)

Let the engine fill the whole refilled window, **halt at the terminator**, and have NAPI
drain+refill+re-arm **while the engine is provably stopped**:

- `net_open`: ring stays as built, but set `desc[last].CTL.lo &= ~0x18000000` (terminator
  at the end of the initial 64-slot window) instead of circular-linking. Engine fills
  0..63, halts.
- `net_poll`: drain all DONE descriptors as today (deliver + refill). The engine is
  **stopped** (it hit the terminator) for the entire drain — so there is **no concurrent
  writer** and the refill cannot be lapped. After draining, move the terminator to the
  last refilled slot and restart: `LLP[ch2]=&desc[first_refilled]_phys; ChEnReg=0x404`.
- The ISR stays as-is (it already schedules NAPI on block-complete; "block complete" now
  coincides with "batch done / engine halted").

Correctness argument (provably 0): the engine only ever writes descriptors that NAPI
refilled in the *previous* batch, and NAPI only touches descriptors while the engine is
halted. The two never write the same buffer concurrently, and the engine can never
advance past the terminator into an undrained slot. Under overload, incoming frames back
up in the ESW RX FIFO and are **dropped** on FIFO overflow (TCP retransmits) — never
leaked. Drop-under-overload is the correct failure mode.

- Pro: no inter-actor race (engine quiescent during refill); restart is the driver's own
  proven 2-write sequence; small, local patch.
- Con: one stop→drain→restart round per batch instead of continuous DMA — a throughput
  cost. Mitigated by batching the full window (≈ ring depth) per round; the phone's load
  is modest (≤~150 conns) so this is very likely fine. If a batch's window is too small
  the engine idles between batches (latency), so keep the ring at 128–256.

## Risk & effort

- Restart mechanics: **low risk** (verbatim copy of net_open's start).
- Design B race surface: **low-medium** (engine halted during refill is the key
  invariant; the one subtlety is the terminator-move + restart ordering and reading the
  engine's stop point correctly — but with the engine halted this is sequential).
- Design A race surface: **high** (concurrent engine).
- Code size: needs a code cave bigger than the cache-fix patches used. The dead
  `use_debug>3` hexdump block (~0x110 bytes at `.text+0xbd78`) plus the spare
  printk-arg blocks should fit Design B's ~15–25 instructions; otherwise append to a
  `.text` tail.
- Recovery on a bad patch: RX hangs → userspace watchdog reboots → RO-rootfs reloads
  pristine `emac`. A kernel oops → power-cycle. Both recover the phone.
- Expect **2–4 swap/test/reboot iterations** to validate (leak count must hit a hard 0
  over ≥20k conns, and throughput/latency must stay sane).

## Recommendation

Implement **Design B** (batch stop-and-go). It is the smallest change that is
*provably* leak-free (engine quiescent during refill), reuses the driver's own restart,
and degrades to frame-drop (not leak) under overload. Verify with a long leakmin run
(target: 0 leaks over ≥20k conns across multiple sender rates) plus a `loadtest`
capacity check to confirm throughput is acceptable. If Design B's per-batch stall costs
too much throughput, evolve toward Design A's pipelining only after B is proven 0.

## Final implementation (patch_emac_gate.py)

Insight that makes B both safe *and* fast: do the terminator advance only at
`net_poll`'s **exit** (the NAPI-complete path `c1c8`). During the whole drain, the
**old** terminator fences the engine, so the engine can never reach the slots the
consumer is refilling — yet while the consumer keeps up, the engine runs continuously
(no stop/go); it only actually halts when it catches the terminator (overload/idle),
and the next poll restarts it. Producer never overtakes consumer ⇒ provably 0.

Bases confirmed equal: `net_open` `r9` == `net_poll` `sl` == `dev+0x360`. Fields
(all off that base): `0x1b4` ring_virt, `0x1b8` ring_phys, `0x1bc` consumer index `C`,
`0x1c0` **free** word reused for `old_T` (terminator index). Descriptor `CTL.lo` at
+0x0c; `LLP_SRC_EN|LLP_DST_EN` = `0x18000000`.

Two code caves in the dead `use_debug>3` hexdump block (`.text 0xbd78..0xbe8c`, 276 B):

**CAVE_OPEN** (hook: replace `net_open` `cc44 add r2,r9,#0x1d4` with `b CAVE_OPEN`) —
runs once at open, after the ring is built (`r4`=&desc[63]):
```
desc[63].CTL.lo &= ~0x18000000   ; make desc[63] the initial terminator
*(u32*)(r9+0x1c0) = 63            ; old_T = 63
r2 = r9 + 0x1d4                   ; replay displaced insn
b 0xcc48
```

**CAVE_GATE** (hook: replace `net_poll` `c1c8 mrs r0,CPSR` with `b CAVE_GATE`) —
runs at every NAPI-complete exit:
```
new_T = (C-1)&0x3f ; old_T = *(r10+0x1c0) ; vbase = *(r10+0x1b4)
if new_T != old_T:
    desc[new_T].CTL.lo &= ~0x18000000     ; fence ahead FIRST
    desc[old_T].CTL.lo |=  0x18000000     ; then re-link old fence
    *(r10+0x1c0) = new_T
if (*(u32*)0xf33003a0 & 4) == 0:          ; ChEnReg: engine halted?
    resume = (old_T+1)&0x3f
    *(u32*)0xf33000c0 = *(r10+0x1b8) + resume*32   ; LLP[ch2]
    *(u32*)0xf33003a0 = 0x404                       ; ChEnReg: restart
mrs r0,CPSR ; b 0xc1cc                     ; replay displaced insn
```
(`pop`/`push {r1,r2,r3,ip,lr}` around the body; `r0` set by the replayed `mrs`; `sl`/`r9`
untouched.) Also retarget the dead `bd20 b 0xbd78` → `b 0xbe8c` so even `use_debug>3`
can't fall into the cave. Cave bytes are produced by `arm-linux-gnueabi-as`/`ld`
(linked at 0xbd78) — not hand-encoded — then spliced in; module renamed emac→emacfix.

Init/empty-ring correctness checked: at open the engine fills 0..63, halts at the
terminator 63; first poll drains, advances the terminator, and restarts the engine to
refill only the *consumed* slots, never the unconsumed ones (verified for budget<64 and
full-ring cases). On exit via the budget-exhausted path (`c260`, not `c1c8`) the
terminator simply isn't advanced that poll — the engine stays fenced and the next
no-work poll advances it; no deadlock, no leak.

## On-device test log

**v1 (2026-06-13) — HUNG THE KERNEL.** First swap: RX never came back. Post-mortem:
the restart wrote only `LLP[ch2]` + `ChEnReg=0x404`. After a halt the channel's
`CTL.lo` register holds the *terminator's* value (LLP_EN cleared), so enabling with a
stale/wrong CTL made the engine mis-DMA (wrong block/addr) → memory corruption → a
**kernel-level hang, not just an RX stall**. CRITICAL lesson: the `swap.sh` watchdog
runs in *userspace* and can only recover an RX stall; a kernel hang is NOT caught and
needs a **physical power-cycle** (RO rootfs then reloads pristine `emac`). So the gate
debug loop requires physical access to the phone — it is not the cheap auto-recover
originally assumed.

**v2 — restart fixed (matches BSP `dmacHw_initiateTransfer`).** Reload the full channel
state from the resume descriptor before enabling:
```
SAR[ch2](0xf33000b0)=desc.sar ; DAR(0xb8)=desc.dar
CTL.lo(0xc8)=desc.ctl.lo ; CTL.hi(0xcc)=desc.ctl.hi
LLP(0xc0)=ring_phys+resume*32 ; ChEnReg(0x3a0)=0x404
```
net_open's own start relies on `CTL.lo` (set at `c968`) holding LLP_EN for the
first-descriptor auto-fetch; after a halt that bit is gone, which is exactly what v1
missed.

**v2 hardening (reduce hard-hang risk, since recovery needs a power-cycle):**
- bail out of the gate if `ring_virt` (sl+0x1b4) is 0 (ring not set up yet);
- mask `old_T` (sl+0x1c0) with `&0x3f` after reading, so a corrupt/uninit value can
  never produce a wild descriptor address (`new_T`/`resume` already masked);
- restart only when the channel is disabled (`ChEnReg & 4 == 0`).
Descriptor `CTL.lo` writes are single aligned-word writes to uncached coherent memory
(atomic, no torn read vs. the engine); the ISR never touches the channel SAR/DAR/CTL/
LLP/ChEnReg regs, so no IRQ guard is needed.

v2 is built (`emacfix_gate.ko`) and disassembly-verified.

## v2 ON-DEVICE RESULT (2026-06-14): STILL HARD-HANGS

v2 was tested on-device. **It hard-hangs the kernel at swap — with only a ping, no
load.** swap.sh's 120s userspace watchdog never fired (scheduler frozen), so recovery
needed a physical power-cycle.

Static re-audit after the hang **cleared** every register/address assumption:
- `r9`(net_open) == `sl`(net_poll) == `netdev+0x360` — **confirmed**. Anchor: the RX
  descriptor ring is the **2048-byte** `dma_alloc_coherent` whose phys handle is stored
  at `netdev+0x518` (`add r2,r9,#0x1b8` @cb64), read by net_poll at `[sl+0x1b8]`. (A
  *second*, small 0x44-byte dma_alloc with handle at `netdev+0x3e4` is unrelated — it
  was a red-herring anchor that briefly suggested a base mismatch.)
- `old_T` scratch word `netdev+0x520` (=base+0x1c0) is genuinely free (zeroed once at
  cb58, read by nothing in net_poll, which uses 0x1bc and 0x1c4 only).
- DMAC ch2 offsets (SAR 0xb0 / DAR 0xb8 / LLP 0xc0 / CTL 0xc8,0xcc / ChEnReg 0x3a0) ✓.
- Code-cave splice is isolated: `bd74 = b c1b4`, `bd1c = ble be8c`, so nothing falls
  through into the cave at `bd78`.

### Root gap (the real reason it can't be a small patch)
**net_poll has no incrementing consumer index. It SCANS the ring for the next
DONE-bit descriptor** (`bd24`–`bd70`): from `C+1`, wrapping at 63, `tst ctl.hi,#0x1000`;
on a hit it does `str r2,[sl,#0x1bc]` (jump `C` to that index) and delivers. So `C` is
the **last-delivered** index, there is **no clean producer/consumer tail**, and the
gate's terminator model `new_T = (C-1) & 0x3f` (treating `C` as next-to-fill) doesn't
correspond to the engine's write frontier. A correct gate would have to track the
engine's frontier independently of this scan — a deeper change than the cave allows.

### Conclusion / recommendation
A provably-0 descriptor-gate by **blind** binary patching — no kernel debugger, no
captured serial console, each failed iteration costing a manual power-cycle, against a
scan-based RX model — is high-risk and may not converge. Recommend either:
1. **Get kernel-debug visibility first** — a UART serial console to capture the
   oops/panic, turning blind iteration into informed debugging; or
2. **Ship the 256-ring mitigation** (`patch_emac_bigring.py`, ~6× leak reduction,
   stable) as the practical result and close out the gate as designed-but-blocked.

### Why the hardware watchdog does NOT rescue this loop
`/dev/watchdog` exists (Broadcom WDT, `WDIOC_SETTIMEOUT` honored up to 60s) and
`tools/wdtpet` pets it, but it is **unusable as a hang-recovery net under load**: the
leak test is an RX packet flood, softirq/NAPI monopolizes the single ~500MHz core and
**starves userspace** (even at `nice -20`; `SCHED_FIFO` is `ENOSYS` on this 2.6.27
build), so the petter misses its deadline and the WDT fires a **spurious** reset during
a *healthy* run. It cannot distinguish "kernel hung" from "userspace starved by the
test." The only reliable hang-recovery here is the physical power-cycle.

### (Superseded) workflow-hardening idea
The big cost is the manual power-cycle on a kernel hang. The
BCMRING **hardware watchdog** (`/dev/watchdog`) was tried as auto-recovery, but see
above — it spuriously resets under the test load. That would make the
gate-validation loop self-healing even against kernel hangs.

---

# 2026-06-20 — the gate DOES load (the "hard-hang" was a loader bug), and why no binary gate still beats the 256-ring

The 2026-06-14 conclusion above ("v2 hard-hangs the kernel at swap, with only a ping")
was **a misdiagnosis**. This session corrected the load failure, got the gate running,
and then measured — at the register level, with a `/dev/mem` DMAC peeker — exactly why a
*provably-0* gate cannot be landed as a binary patch on this driver. Three new tools made
this possible: a smart-plug power-cycle for clean swaps (HA-controlled), a self-contained
capture script (`runcap.sh`), and **`tools/dmacpeek`** — a Rust `/dev/mem` reader/writer
for the live DMAC channel-2 registers.

## Correction 1 — the swap "hang" was `insmod: invalid module format`, not a kernel hang

The gate never loaded, so nothing it did could have hung the kernel. `swap.sh` runs
`ifconfig eth0 down; rmmod emac; insmod emacfix_gate.ko` — the `insmod` failed, leaving
eth0 down with **no driver bound**, which *looks* identical to a hang from the laptop
(unreachable until watchdog/power-cycle). Two independent loader faults, both rooted in
the **cave placement at `.text+0xbd78`**:

1. **The cave overlapped live relocations.** The 2.6.27 ARM module loader applies the
   `.rel.text` entries over the module image *after* copying it. Two `R_ARM_CALL` printk
   relocations land inside the old cave window — `.rel.text` `r_offset` `0xbd88` and
   `0xbe88` — so the loader overwrote the spliced cave bytes with relocated `bl printk`
   words, corrupting the hand-placed code.
2. **`R_ARM_NONE` (type 0) is rejected.** This loader's `apply_relocate` treats an
   unknown/zero reloc type as fatal ("unknown relocation"), so any attempt to *neutralize*
   the overlapping relocs by zeroing their type also fails the load.

**Fix:** place the cave at **`.text+0xbd8c`** — the reloc-free gap *between* the two printk
relocs (`0xbd88 < 0xbd8c < 0xbe88`) — and **edit no relocations at all**. `relocs_in_range()`
in every patch script now asserts the chosen cave window contains zero `.rel.text` entries.
With this, `emacfix_gate2.ko` (terminator gate) **`insmod`s cleanly**, eth0 comes back, RX/TX
healthy on a quiet link. The "needs kernel-debug visibility / each iteration costs a
power-cycle" framing was therefore too pessimistic: most failures were load-time and
diagnosable on the host with `readelf`/`objdump`. (The smart-plug still power-cycles for a
*clean-boot* swap, because `rmmod emac` blocks on lingering TIME_WAIT sockets after a heavy
run — a convenience, not a hang-recovery necessity.)

## Correction 2 — the terminator gate loads but WEDGES under load (and dmacpeek says why)

`emacfix_gate2.ko` runs fine idle, but under the full-MSS load it **stops RX** (recoverable
RX stall → userspace watchdog reboots; *not* a kernel hang). Every restart variant tried
wedged the same way: `old_T+1`, a hardware-LLP reload (which additionally hard-froze from a
garbage LLP value), `desc[0]` (ring base), and each of those `+clear-status`.

`tools/dmacpeek` settled the cause by reading ch2's registers live during a wedge and by a
controlled **`bounce`** experiment (disable the channel, apply a candidate recovery, read
back). The decisive distinction:

| how the channel stopped | `ChEnReg` re-enable (`=0x404`) | resumable? |
|---|---|---|
| **software ABORT** (`ChEnReg=0x400` written *while running*) | takes effect, `ChEn` bit 2 returns | **YES — regardless of FIFO state** |
| **LLP-terminator completion** (engine finished a block whose `CTL.lo` had `LLP_SRC_EN\|LLP_DST_EN` cleared) | write executes but `ChEn` bit 2 **stays 0** | **NO — non-resumable "transfer-complete"** |

This is the root reason **the entire terminator design (Designs A/B above) cannot work by
restart**: the gate's whole premise is "let the engine *complete into* a terminator, then
re-arm it" — but completing into a terminator is exactly the one stop-state the DW-AHB-DMAC
will not resume from. The driver's own `net_open` start works only because it starts from a
channel that was *never* run to a terminator (fresh program). No sequence of channel-register
writes (`bounce` methods 0–5: re-enable, disable-toggle, CFG `CH_SUSPEND` toggle, global
`DmaCfgReg` bounce, full SAR/DAR/CTL/LLP reprogram, LLP-advance) re-animates a
terminator-completed channel — only a full teardown/rebuild would, which a per-poll cave
can't do.

(FIFO state was *cleared* as the suspect along the way: `CFGlo` `CH_FIFO_EMPTY=0x200` /
`CH_SUSPEND=0x100` do not gate the abort-resume; abort resumes with a non-empty FIFO. So the
non-resumability is specifically the transfer-complete latch, not back-pressure.)

## The pivot — abort (resumable) instead of terminator (non-resumable)

Since abort-disable *is* resumable, gate the engine with an **abort during the consumer's
critical section** instead of fencing it with a terminator. `tools/dmacpeek bounce 0` proved
`ChEnReg: 0x400 → (drain) → 0x404` resumes RX cleanly. Two abort gates were built and
measured against the 256-ring's **0.49 %** laptop-full-MSS baseline (RESULT 5 in README.md).

### Abort gate A — abort on *every* `net_poll` (`patch_emac_abort.py`, module `emacabrt`)
Hook `net_poll` entry → `ChEnReg=0x400`; hook `net_poll` exit → `ChEnReg=0x404`. The engine
is frozen for the whole drain, so it provably cannot overwrite a descriptor mid-delivery — no
leak during a poll. **But it OVER-THROTTLES catastrophically:** `48 / 1440` connections
completed in 440 s (`waves=30 conns_sent_ok=48/1440`). Aborting on *every* poll — including
the vast majority where the consumer is keeping up and no gating is needed — starves the
engine so hard that connections time out. Resumable (no wedge), but a self-inflicted DoS.

### Abort gate B — *selective* abort by frontier gap (`patch_emac_sgate.py`, module `emacsgat`)
Abort **only when the engine has run too far ahead** of the consumer. At `net_poll` entry,
compute the in-flight gap from the live LLP register and the consumer index:
```
gap = ((LLP[ch2] - ring_phys) / 32  -  C) & 0x3f      ; descriptors engine is ahead
if gap >= THRESH:  ChEnReg = 0x400                    ; only then freeze
```
(`LLP[ch2]` read from `0xf33000c0`; `ring_phys` `[sl+0x1b8]`; `C` `[sl+0x1bc]`. Exit re-enables
only if the channel is disabled.) This restores **full throughput** — it gates rarely — and
cuts the leak, but **cannot reach the 256-ring**:

| threshold | leak rate | throughput |
|---|---|---|
| `gap ≥ 48` | **9.0 %** | full |
| `gap ≥ 16` | **3.8 %** | full |
| 256-ring (target) | **0.49 %** | full |

Tightening the threshold helps monotonically but never crosses the 256-ring, and pushing it
lower collapses back toward gate A's over-throttle. **Why selective abort can't be provably-0:**
the gap is sampled at poll *entry*, but the engine runs free during the **NAPI-masked window
between polls** (RX IRQ masked, softirq not yet re-scheduled). A burst can lap the consumer
*inside* that window — after the last gap-check passed — and the next check only fires after
the damage. The abort is structurally too coarse to catch sub-poll lapping, which is precisely
the regime the leak lives in.

## Verdict — a binary-patch gate cannot beat the 256-ring on this driver

| approach | provably-0? | wedges? | throughput | measured leak |
|---|---|---|---|---|
| terminator gate (Design A/B) | yes *by design* | **YES** (completion non-resumable) | — | unrunnable |
| abort-every-poll | yes *during poll* | no | **DoS (48/1440)** | unmeasurable |
| selective abort, `gap≥16` | no | no | full | 3.8 % |
| selective abort, `gap≥48` | no | no | full | 9.0 % |
| **256-ring (shipped)** | **no** | **no** | **full** | **0.49 %** |

The provably-0 design (terminator) is blocked at the **hardware**: the one stop-state it
relies on is the one the DMAC won't resume. The resumable alternative (abort) is either a DoS
(every-poll) or too coarse to be provably-0 (selective), because the driver's RX model gives
no clean producer/consumer handshake to gate on — it's a **free-running, scan-based ring with
no consumer tail index** and an inter-poll NAPI-masked window. Closing the leak *to zero at
any load* requires restructuring that producer/consumer handshake (a real descriptor-ownership
protocol with a halt the engine *can* resume — e.g. a gated ring where the engine blocks on a
software-owned descriptor and NAPI hands ownership back), which is a **source-level change**,
not something a per-poll code cave can express. **Ship the 256-ring** (31× mitigation, full
throughput, never wedges) as the best achievable binary fix; reserve provably-0 for the
source.

## Register reference nailed down this session (DW-AHB-DMAC, RX = channel 2)

Controller **phys `0x30300000`** (`MM_ADDR_IO_DMA0=0x30200000` + 0x100000), **virt
`0xf3300000`** (`MM_IO_VIRT_TO_PHYS`). Per-channel regs at `base + 0x58*ch`; ch2 = `+0xb0`.

| reg | ch2 offset | notes |
|---|---|---|
| SAR / DAR / LLP | `0xb0 / 0xb8 / 0xc0` | LLP low bits select next desc; `CTL` auto-loads from it |
| CTLlo / CTLhi | `0xc8 / 0xcc` | `CTL.lo` LLP-enable bits `LLP_SRC_EN\|LLP_DST_EN = 0x18000000` |
| CFGlo / CFGhi | `0xf0 / 0xf4` | `CFGlo` `CH_SUSPEND=0x100`, `CH_FIFO_EMPTY=0x200` |
| **ChEnReg** | **`0x3a0`** | write `0x404`=wr-en ch2 + **enable**; `0x400`=wr-en ch2 + **disable/abort**; **busy = bit 2** (`& 4`) |
| DmaCfgReg | `0x398` | global DMAC enable (`bounce` method 3 toggles it) |
| Raw/St Tfr·Blk·Err | `0x2c0 0x2c8 0x2e0 / 0x2e8 0x2f0 0x308` | interrupt raw + status |
| Clear Tfr·Blk·Err | `0x338 0x340 0x358` | write-1-to-clear |

**Key semantic (the whole finding):** `ChEnReg=0x400` *while running* = **resumable abort**;
running a block to LLP-terminator completion = **non-resumable** transfer-complete latch
(`ChEn` bit 2 stuck 0). `tools/dmacpeek` (one-shot dump / monitor-to-logfile / `bounce
<method> <log>`) is the live instrument for all of the above.

## The source the provably-0 fix needs is not public (and Yealink published the wrong one)

A correct gated-ring rewrite needs the `bcm111xx`/BCMRING `emac`+`esw` driver source and the
matching 2.6.27.47 ARM kernel. **None is public:** the driver was never upstreamed, Broadcom's
BCMRING BSP is private, and **Yealink's own GPL "Linux Kernel" download is the wrong product** —
verified as **2.6.10
`dev-malta-mips2`, `CONFIG_MIPS=y`, `CONFIG_MIPS_AVALANCHE_SOC=y`**, shipping
`drivers/net/avalanche_eswitch` + `ibm_emac` (a **TI Avalanche MIPS** MontaVista kernel), not
the **ARM BCMRING 2.6.27.47** the T46S runs. Even though Yealink's GPL table lists *T4XS* next
to that download, the file does not contain the corresponding source for this device — a GPL
§3/§6 gap. **Route to provably-0:** a formal GPL source demand to Yealink (citing the T46S
firmware version and the wrong-architecture kernel they serve), escalating to the SFC /
gpl-violations.org if stonewalled.
