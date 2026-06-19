# Tapewyrm — QIC-80 tape recovery over Greaseweazle v4.1

> **Status:** design record. The host stack and codec are implemented; the Greaseweazle firmware is **vendored complete in-tree** (`firmware/`, Unlicense) with the QIC layer grafted onto GW's control loop and flux recording.
> **License:** the whole project is **public domain under the Unlicense** (see `UNLICENSE`); the vendored Greaseweazle firmware is itself Unlicense.
> **Purpose:** a single, self-contained document to seed a Claude Code session (or hand to an engineer) so the full reasoning, decisions, and open questions can be reconstructed without re-deriving them. Reads top-to-bottom; later sections assume the vocabulary of earlier ones.
> **Name:** *Tapewyrm* — the tape is a long serpent of a medium, written in a back-and-forth serpentine (§7.4); a wyrm rather than a weasel.

---

## 0. How to use this document

This captures *why* as much as *what*. Where a decision had a real alternative, the alternative and the reason for rejecting it are recorded inline. Anything not yet verified or designed is collected in **§9 Open questions** — treat those as the work queue, not as settled fact. Exact register-level constants (flux opcode bytes, QIC timing envelope) are deliberately left as "confirm from source" rather than asserted from memory.

> **Grounding.** §2.1/§5.3 (command channel) are grounded against **QIC-117 Rev J** and §2.2/§7 (recording format) against **QIC-80-MC Rev N**, both read in full; claims traceable to those specs are stated as fact, and what still rests on secondary sources or remains unread (QIC-113, QIC-3010/3020) is flagged in §9–§10.

---

## 1. Goal & scope

**Goal.** Read and recover data from QIC-80 *floppy-interface* tape cartridges (and close relatives: QIC-40, QIC-3010, QIC-3020/Travan) using a Greaseweazle v4.1 as the bus interface, capturing raw flux and decoding **offline**.

**The two hard constraints (everything else is new):**

| Borrowed (then owned) | Built new |
|---|---|
| Greaseweazle **v4.1 hardware** — no board respin | Firmware: QIC command verbs, bus arbiter, free-running flux capture + markers |
| The GW **native flux transfer encoding** (referred to in discussion as *"greasepack"* — see note below) | Host: device-link stubs, QIC-117 drive layer, tape transport, QIC-80 format codec |
| GW flux engine, PLL, bitcell recovery, **MFM framing** (the timing-critical primitives) | `RawFluxCapture` linear track-stack container + tape marker opcodes |
| `gw pin set` / `gw pin get` (bench bring-up only), the bootloader / update **protocol** (kept wire-compatible), read/stop control semantics | The entire QIC-117 / QIC-80 "brain" (host-side), **and the single `tw` CLI — which carries its own flashing client (`tw flash` / `tw dfu`); the `gw` executable is never required** |

> **Fork posture — this is a hard fork, not a tracked branch.** Tapewyrm starts from Greaseweazle and diverges; mergeability with upstream is **not** a goal, and on the host side little if any GW *tooling* survives recognizably (GW's host code is built around `.scp`-style disk images and disk verbs — almost none of which maps onto a QIC-117 drive model + tape transport + linear-track-stack codec). What actually carries over is narrow: the flux-capture timing skeleton and USB/bootloader on the firmware, and the flux-codec / PLL / MFM primitives on the host. The discipline that *replaces* upstream-tracking is a **clean internal seam** isolating exactly those primitives, so (a) future upstream fixes to the flux engine can still be cherry-picked by hand, and (b) the bootloader/update protocol stays wire-compatible so it can be driven by `tw`'s own flashing client (and, incidentally, by stock `gw update` too — but `tw` never *requires* `gw`). Everything else is owned outright. (Mechanics in §12.5.)

> **Terminology note.** "Greasepack" is not an official Greaseweazle term and could not be found in GW docs or firmware. What is meant is unambiguous: the GW on-wire flux byte stream (variable-length inter-transition intervals at a known sample clock, plus an opcode/escape channel). This document calls it the **GW flux encoding**.

**Tooling posture — one tool, `tw`.** All host functionality lives in a single executable, **`tw`**: `probe` / `capture` / `decode` / `recover` / `replay` **and firmware flashing** (`tw flash`, `tw dfu`). Tapewyrm deliberately does **not** reuse or require Greaseweazle's `gw` executable. The bootloader/update protocol is kept wire-compatible (§12.5) so the board stays a dual citizen — flashable by `tw`'s own client *and*, incidentally, by stock `gw update` — but a Tapewyrm install needs nothing from GW. (`gw pin` remains a handy bench aid during bring-up before firmware exists; that is not a runtime dependency.)

**Non-goals (for now):**
- Writing or formatting tapes (needs reference-burst/servo handling — out of scope; read-only is far simpler and is all recovery requires). *(But see Sometime goals — the Linux linear-device aim eventually pulls write into scope.)*
- QIC-3020 @ 2 Mbit/s tuning (feasible on this hardware; not the first target).
- Recovering **non-QIC-113 proprietary** archive containers (e.g. CP Backup's CPB, some Norton/Central Point layouts that predate or ignore QIC-113). Standards-compliant **QIC-113 file sets are in scope** (§7.5) — decoding QIC-80 + QIC-113 yields the directory tree and files directly; only genuinely non-standard app containers need a separate, app-specific parser on top.
- **STAC/DCLZ decompression** of compressed volumes is a thin separate stage (§7.5) — the framing is parsed here; the LZS/DCLZ codec itself (QIC-122 / QIC-130 / QIC-154) is a drop-in not yet specified.

**Sometime goals (aspirational, not scheduled):**
- **Linux linear-tape device → `tar`.** Eventually present the drive to Linux as a *linear device* with streaming/sequential semantics (in the spirit of a SCSI tape `/dev/st0`), so standard tools — notably **`tar`** — can read and *write* QIC cartridges directly (`tar tf`, `tar xf`, `tar cf`). This pulls two things into scope that are non-goals today: (1) the **write/format path** (reference-burst/servo handling — the hard half, and it gates writing), and (2) a **device shim** that maps the serpentine segment-stack to a flat byte stream with on-the-fly QIC-80/QIC-113 (de)framing — realizable either as a kernel character device or, more cheaply first, in userspace via FUSE/NBD over the `tw` host. Read-as-linear-device sits naturally on top of the existing offline decode stack (§6.4); write is the bigger lift. Sequenced after the read-recovery target is solid.

---

## 2. Background: the crux is that this is two channels on one bus

A QIC-80 floppy-tape drive is **not** "a weird floppy." It multiplexes two protocols over the same 34-pin Shugart cable, and they are **time-multiplexed** — only one is live on the wire at a time.

### 2.1 Out-of-band command/status channel — **QIC-117**

The FDC control lines are repurposed (grounded against **QIC-117 Rev J**):
- **STEP** = a numeric command. *N* step pulses = command number *N*, verbatim, with 47 codes defined. **Arguments** (e.g. seek-head-to-track) follow as a second pulse train in **N+2 form** — value plus two pulses — so an argument can never collide with the single-pulse Soft Reset (command 1) and zero is sendable. (Soft Select is the exception: a literal 20 pulses.)
- **TRK0** = the level-sensitive serial return. After a report command the drive clocks bits out LSB-first; **the first bit is an Acknowledge — always TRUE; FALSE ⇒ hardware failure or reset — and the final bit is normally TRUE; FALSE ⇒ an error occurred mid-report.** Reports are up to 16 bits (Report Error Code is two bytes: error code, then associated command). The value is **latched** at command receipt, so host clocking jitter is harmless.
- **INDEX** = a general cue, *not* just "ready." It cues three idle states (ready-after-command, a report bit is now on TRK0, waiting-for-argument) **and marks each data segment during Logical Forward** (see §2.2).
- A status byte is read by sending `REPORT_DRIVE_STATUS`, reading the ACK, clocking each of 8 bits with `REPORT_NEXT_BIT` (command 2 = 2 pulses), then reading the Final bit.
- **Timing (Table 1):** STEP interval ~2.0 ms nominal (0.9–2.1 ms); the *command time-out* ending a pulse train is ~2.5 ms (2.2–2.9 ms); a report bit appears within 900 µs of the 2nd pulse. **Hazard:** pulses must stay grouped under the time-out — an isolated slow pulse (≥ ~2.9 ms) reads as Soft Reset — and the drive keeps TRK0 inactive except during a report so a host floppy-detect can't mistake it for a diskette.

The standard groups commands as **report / mode-switching / motion-control**, with separate **non-interruptible** and **high-speed** flags, and gates each by a **restriction table** (illegal modes × required status bits) richer than a single tag. The clean mode/motion/report dispatch (from ftape's `qic117.h`) is a convenience over that table. Dispatch:
- *report* → ACK, clock bits, check Final.
- *motion* (logical forward, seek, skip, pause, stop) → these run **seconds to minutes** (Seek Head to Track 15 s, Stop 8 s, a full Logical Forward pass bounded by tape-length/speed). Wait-ready on a generous timeout — **except** you do **not** wait-ready after Logical Forward (it streams to EOT; you arm capture and stop).
- *mode* (enter format/verify/primary, soft/phantom select, rate select) → state change, no motion, no report.

Useful reports: drive status (6), error code (7, read to clear — errors latch, never overwritten), drive configuration (8 → rate, where `10`=500 kbit/s and `00` is ambiguously 4 Mbps-or-250 kbit/s by drive type), tape status (33 → QIC-40/80/3010/3020 + tape type/length), format segments (37 → segments/track). Commands 8/33/36/37 are CCS-level-dependent; a basic drive may lack 36/37, in which case geometry falls back to fixed values (§7.3).

### 2.2 In-band data channel — **QIC-80 recording format**

Once the drive is in motion (Logical Forward, in Primary or Verify mode), data flows as ordinary **MFM at 500 kbit/s** (QIC-80; 1 Mbit/s QIC-3010; 2 Mbit/s QIC-3020) on RDATA/WDATA — the same regime as HD floppy. Grounded against **QIC-80-MC Rev N**:

- **Each segment is a standard IBM/MFM "track" image.** It opens with an **index address mark** (`C2 C2 C2 FC`, missing-clock C2) — the very mark a floppy carries once per revolution, here once per **segment** — then 32 repetitions of a **sector ID** (`A1 A1 A1 FE` + `FTK, FSD, FSC, 03` + CRC) and a **data block** (`A1 A1 A1 FB` + 1024 bytes + CRC; `F8` in place of `FB` = **deleted-data** = bad block). GW's existing IBM-MFM decoder recovers these directly; the only QIC twist is that the ID's cylinder/head/record/size = **(FTK, FSD, FSC, 03)**. CRC is CCITT **x¹⁶+x¹²+x⁵+1**, register preset all-ones.
- **The hardware INDEX pulse corresponds to that mark** — the drive asserts INDEX at each segment start, within 8 flux transitions after ≥50% of the erased inter-segment gap. So segment boundaries are signalled **twice, independently**: the in-stream `C2/FC` mark and the hardware INDEX edge.
- **Sector identity → tape coordinate (exact).** Each sector's `(FSD, FTK, FSC)` maps deterministically to a logical sector number, logical segment, tape track, and segment-relative-to-track:
  - `LSN = 32640·FSD + 128·FTK + (FSC−1)`
  - `SEG = 1020·FSD + 4·FTK + ⌊(FSC−1)/32⌋`   (inverse: `FSD = SEG/1020`, `FTK = (SEG mod 1020)/4`, `FSC = (SEG mod 4)·32 + 1`)
  - `TPT = ⌊SEG / segments_per_track⌋`,  `TPS = SEG mod segments_per_track`
  - First sector of tape track 0 is `(0,0,1)`. Ranges: `1≤FSC≤128`, `0≤FTK≤254`, `FSD` length-dependent. One "floppy track" (FTK) = 128 sectors = **4 segments**; one "floppy side" (FSD) = 1020 segments. This is the bridge between the FDC's circular-disk addressing and the tape's linear reality — see §7.4.
- **Serpentine geometry:** 28 tracks (0.250 in tape; 36 for 0.315 in/Travan). **Even tracks recorded forward, odd tracks reverse**, referenced to forward/reverse reference bursts at BOT. All tracks hold the same segment count. Example (425 ft): 207 segments/track → 5,796 segments, 185,472 sectors.
- The **first defect-free segment is the header segment** (duplicated in the second); it holds the **format parameter record** (signature `55 AA 55 AA`, format code `04` = variable-length, segments/track, track count, dates, ASCII tape name) and the **bad-sector map**. Segments before/between the two header copies are written as deleted-data. Some sectors are bad *by design* (e.g. QIC-3020 pre-maps zones near BOT/EOT hole imprints) — the BSM distinguishes those.

### 2.3 Why erasure decoding is the whole recovery game

Every sector carries its own CRC, so the decoder knows **which** sectors are bad → it decodes the segment in **erasure mode**. The ECC is a per-**column** Reed–Solomon code of redundancy 3 over GF(256) (rows 0–28 data, 29–31 parity, written row-by-row). It corrects **up to 3 sectors with CRC errors per segment** (or 1 CRC-error + 1 CRC-*failure* — a "failure" being an error the CRC missed, i.e. unknown-location); with erasure positions known from CRC you get the full 3-sector budget, far stronger than blind error correction. Decoder subtlety: **BSM-excluded sectors are physically skipped on tape**, and the 3 parity sectors occupy the **last 3 non-excluded** sectors, so a segment's codeword length is `N = 31 − bad-blocks-in-segment`, not always 32. Sectors are **self-locating** (their ID is their coordinate), so capture order is irrelevant — partial, retried, or out-of-order captures still reassemble.

---

## 3. Hardware substrate: Greaseweazle v4.1 (fixed, no respin)

- MCU: **AT32F403 (Cortex-M4)**, ~144 MHz (some boards report AT32F403A @ higher clock). SRAM extended to **224 kB** on current firmware.
- USB: **High-Speed (480 Mbps)**, large USB buffer (64 kB+). Enormous headroom over QIC-80's ~500 kbit/s flux.
- **Buffered 40 mA outputs** (drives strong drive-side pull-ups cleanly). `gw pin set` / `gw pin get` already exist (use for bring-up/bit-bang before committing firmware). 3 user-definable outputs (pins 2/4/6); pin 34 readable input; external-LED header.
- Power: **5V-only** on-board header; 12V drives use a **separate PSU**; **USB 5V isolation jumper** lets the board run off external power safely.
- Connectivity/protection: USB-C, ESD protection on USB data, over-current protection on USB power.
- **Hardware DFU header:** straps the AT32's built-in ROM bootloader, giving a probe-less, application-independent flash path that survives a broken/half-flashed firmware (the un-brick route). Flashing detail in §12.3.

**Why it suffices for QIC (no board change):** QIC-117 needs a *subset* of the Shugart lines in the *same directions* GW already drives (STEP/DIR/WGATE/MOTOR/DSEL out) and senses (INDEX/TRK0/RDATA in). STEP is the command line; TRK0 is the return; INDEX is ready; RDATA is flux. The data channel is literally what GW does. Net work is firmware verbs + host software + a power/termination cabling setup. 12V via separate PSU + isolation jumper.

> **Verify (cheap):** from the v4.1 design files, confirm TRK0/INDEX land on pollable GPIO/EXTI (not a peripheral-locked pin) and that the STEP output buffer swings the bus at the chosen cadence. Near-certain for a floppy interface; take it from the schematic, not from assumption.

---

## 4. Architecture overview

A layered stack. **The USB line is the real-time boundary.**

```
HOST  (leisurely · semantic · NO bus access)
  Format codec            decodes flux into files (offline, pure)
  ───────────────────────── IO layer boundary  (artifact crossing: RawFluxCapture)
  Tape transport          positioning · serpentine · emits RawFluxCapture
  QIC-117 drive           command table · status/error · DriveProfile
  Device link             transaction stubs · typed RPC · no bus access
  ═════════════════════════ USB — REAL-TIME BOUNDARY
DEVICE / FIRMWARE  (hard real-time · bus-owning · semantically BLIND)
  Transaction interface   atomic command & capture txns
  Bus arbiter             single lease · owns drive-select · serializes channels
    QIC verbs engine      pulse · report · wait-ready        (command/status channel)
    Flux engine           GW flux · free-running capture     (data channel)
  ───────────────────────── physical bus
  QIC-80 drive            34-pin Shugart bus
    Command/status        STEP · TRK0 · INDEX                (out-of-band)
    Flux data             RDATA · WDATA                      (in-band)
```

Two things move through the stack in opposite directions: **control descends** as verbatim command numbers; **flux ascends** as the GW byte stream. Both become bus signals only *below* the real-time line.

### 4.1 Load-bearing decisions (and why)

1. **The USB device is the bus arbiter; firmware is a clean interface.**
   - *Why:* makes "never stream flux while pulsing STEP" **structural**, not a host convention — the host has no verb that can express the collision. Keeps timing-critical work (report-bit clock, motion→arm handoff) off the USB round-trip. Mirrors GW's existing contract (firmware owns the bus; host issues commands). Enables a safety-critical auto-stop on USB loss (§5.2).
2. **Verbatim QIC-117 command passthrough.**
   - *Why:* firmware stays dumb and rarely-reflashed; the whole 47-command table, status/error model, per-drive quirks, and recovery logic iterate in host Python. Future/vendor-unique commands need no firmware change. **Arbitration and verbatim are orthogonal:** verbatim = command *content*; arbitration = bus *ownership*. The device can own the bus while staying semantically blind.
3. **Reuse the GW flux encoding as the transfer + storage representation — NOT the `.scp` container.**
   - *Why:* `.scp` (the gw tool's default output) is structured per-revolution around index pulses; tape has neither revolutions nor index. Reusing the *encoding* (not the disk container) lets us inherit GW's PLL / bitcell recovery / MFM framing and existing flux tooling; only the QIC-80-specific codec is new.
4. **Capture-then-decode-offline beats a period-correct FDC for recovery.**
   - *Why:* a real 486 + FDC gets one hardware-timed, DMA-underrun-bound shot per pass. Recording raw flux lets us do multi-pass union, erasure-mode RS, and arbitrarily aggressive PLL/retry offline — and there is **no shoe-shining**, because we record rather than feed an FDC in real time.

---

## 5. Firmware design (below the real-time line)

### 5.1 Device contract — the transaction interface

Small, atomic, transaction-oriented. The host never touches a pin.

- `command_txn(n, report_bits=k)` — take lease, emit *n* step pulses (verbatim), optionally clock *k* report bits off TRK0, release, return bits.
- `wait_ready(timeout)` — poll the ready/INDEX line.
- `capture_session(motion_cmd, stop)` — take lease, issue motion via verbs engine, arm free-running flux, stream GW flux until `stop`, issue stop/pause, release.
- `set_timing(...)`, `select(...)` — configuration (no bus needed; serviceable while idle).
- **abort/stop** — an out-of-band control valid *during* a capture session (see §5.2).

### 5.2 Bus arbiter — lease state machine

**Lease** = the exclusive right to drive the bus (toggle a pin or stream flux). One holder at a time; the arbiter is the sole owner of drive-select and the sole grantor.

States: **Idle → Grant → {Command held | Capture held} → Quiesce → Idle.**

- **Idle:** no lease, bus quiescent; config serviced here.
- **Grant:** validate txn, assert select, hand lease to one engine, branch on type.
- **Command held:** verbs engine owns bus for a bounded op (pulses / report clock / wait-ready). Guard timeout backstops a wedged drive.
- **Capture held:** verbs engine issues motion, then the *same lease* is retargeted to the flux engine, held for the whole host-paced stream (this is why no command can interleave). Responsive to exactly one out-of-band input: abort/stop.
- **Quiesce — the release funnel:** every exit from a held state (normal, error, abort, watchdog, fault, USB-loss) routes here for the *same* safe teardown before releasing. **Invariant:** one release path → no lease leaks, tape never left running.

**Clean mid-capture abort sequence:**
1. Abort arrives **out-of-band** (not a queued txn — a capture holds the lease indefinitely, so a queued command would deadlock). Same trigger is fed by the watchdog (max duration / byte budget) and a **USB suspend/disconnect dead-man**.
2. Within the still-held lease, hand the bus flux→verbs (stop arming/sampling).
3. **Issue STOP_TAPE/PAUSE before releasing** — dropping the lease with tape rolling risks spooling into EOT/BOT and the broken-tape hazard. Stop first, always.
4. Cleanly terminate the flux stream: flush buffer, emit `END` marker with counts (§7.2).
5. Restore drive-select, release lease, → Idle.

Properties: **idempotent** (a second abort while quiescing is a no-op); broken-tape (error 10), EOT, and wait-ready timeout all take the same funnel. **USB loss is the safety-critical case** and the strongest reason the *device*, not the host, owns the stop. Optional: **sticky select** across back-to-back command txns to dodge re-wake latency (lease still cycles per op).

### 5.3 QIC verbs engine (command/status channel)

- **Pulse emit:** *n* STEP pulses at the configured cadence (verbatim — no knowledge of meaning), then hold the terminating gap. Defaults from `set_timing`: ~2.0 ms step interval, gap > 2.9 ms to end the command. **Must keep pulses grouped under the command time-out** (an isolated slow pulse = Soft Reset) and **must not leave TRK0 asserted** between commands.
- **Arguments:** for argument-bearing commands, emit the operand as a following pulse train in **N+2 form** (value+2 pulses); Soft Select is the exception (a literal 20 pulses).
- **Report clock:** issue the report command, read the **ACK** bit (within TACK ≈ 2.5 ms; if false → flag reset/hardware failure), then for each data bit issue `REPORT_NEXT_BIT` (2 pulses) and sample TRK0 within TBIT (900 µs of the 2nd pulse edge), LSB-first; finally read the **Final** bit (false ⇒ report error). Up to 16 bits (Report Error Code = 2 bytes). The whole loop runs device-side → immune to USB jitter; the drive latched the value at command receipt. Prefer waiting on the ready/INDEX edge over a blind delay; support both, pick per drive on the bench.
- **wait-ready:** poll the ready line until ready or a generous, motion-scaled timeout (seeks 15 s, stop 8 s).

### 5.4 Flux engine (data channel) — capture pipeline

Pipeline: **RDATA → capture front-end → encoder → ring buffer → USB streamer → host**, with a **marker injector** merging into the stream.

- **Capture front-end:** timer input-capture measuring inter-transition intervals in sample-clock ticks (reused from GW). **Free-running** — armed/stopped on command, *not* gated to one index-to-index span. Tape *does* assert INDEX, but **once per segment, not per revolution**, so the engine **records** each INDEX edge as a marker rather than gating on it — segment boundaries arrive for free, and the in-stream `C2/FC` index address mark gives a second, independent boundary signal for the decoder to cross-check.
- **Encoder:** GW flux byte encoding — short intervals as direct bytes, a **long-flux continuation escape** (important on tape: dropouts and inter-record gaps produce long intervals that must not be lost or saturated), and **opcode escapes** for out-of-band events (reused).
- **Marker injector (new):** rides the opcode escape channel (same mechanism GW uses for index), so markers never get misread as flux. **Decision:** reuse the existing Index opcode (bring-up; parseable by stock GW tooling) *or* define typed tape opcodes in a header shared by firmware + custom host decoder (real design). Recommend typed, under the same "one shared table, both ends" discipline as the command set. Marker set in §7.2.
- **Ring buffer (SRAM) + HS USB streamer:** 224 kB SRAM + 480 Mbps give ample headroom; the buffer absorbs host stalls, it doesn't keep pace with the drive.
- **Backpressure policy — never silently drop flux:** transient lag absorbed by the buffer; sustained near-overflow → emit `EVENT{overflow}` and trigger a **clean abort** through the arbiter funnel (for tape a gap is unrecoverable in place; a clean re-do beats a corrupt splice). If you must continue instead, emit an explicit **gap marker** recording how much flux was lost so the codec treats it as a discontinuity. Overflow is one of the arbiter's fault triggers.
- **Termination / accounting:** on disarm, halt front-end, flush buffer, emit a single `END` opcode carrying reason, total flux-transition count, total byte count, and a checksum. Makes `RawFluxCapture` **self-terminating and self-verifying**. A capture with no valid `END` is flagged truncated (USB loss can't flush) — but it still decodes, because sectors self-locate; you lose the tail, not the file.

---

## 6. Host design (above the real-time line)

### 6.1 Device link — transaction stubs
Typed RPC wrappers over the USB transaction protocol. **No arbitration, no bus access.** Serializes `command_txn` / `wait_ready` / `capture_session` / `set_timing` / `abort`.

### 6.2 QIC-117 drive — the semantic layer
- The **command table** (the ~47 commands, each tagged mode/motion/report + non-interruptible).
- **Dispatch:** report → follow with report clock; motion → wait-ready + status check; mode → state change.
- **Status/error model:** after motion, `REPORT_DRIVE_STATUS` (6); if ERROR bit set, `REPORT_ERROR_CODE` (7) to read+clear; handle CARTRIDGE_PRESENT / NEW_CARTRIDGE / REFERENCED / WRITE_PROTECT / AT_BOT / AT_EOT.
- **DriveProfile** injected here (data, not code): per-drive wake/select quirk + timing envelope. A new drive is a profile, not a code change.

### 6.3 Tape transport — logical motion + capture orchestration
- Positioning: seek load point (14), seek-head-to-track (13, operand as **N+2 pulses** — §2.1). Skip-N/Pause (25/26/3) for targeted re-reads, reading sector IDs afterward to confirm the landed segment.
- **Serpentine walk:** issue **logical forward** per track; the drive presents data in logical order regardless of physical direction, so no software reversal is needed (only a physical-reverse salvage pass would be time-reversed offline). **Do not** wait-ready/status between the forward and arming capture — a status report can swallow the first segment (§2.1, §5.2).
- **Capture orchestration:** compose a motion command + `capture_session`; emit a `RawFluxCapture` per pass. Hazard rule: broken-tape → abort immediately.

### 6.4 Format codec — offline, pure, above the IO boundary
Consumes `RawFluxCapture`; touches no hardware.
1. Reuse GW **PLL + MFM framing** → recover sectors (ID + data fields with CRC).
2. Read each sector's **(FSD,FTK,FSC)** → place it in (tape track, logical segment, sector-in-segment) via the §7.3 algebra. Deleted-data marks (`F8`) = format-time bad blocks.
3. Bin into 32-sector segments; CRC pass/fail → **erasure mask**.
4. **RS erasure decode** (redundancy 3 over GF(256), column-wise; `N = 31 − bad-blocks`) → rebuild up to 3 sectors/segment (spec + algorithm in §13.2).
5. Parse the **header segment + BSM**; subtract pre-mapped bad sectors; parse the **volume table** → file-set segment ranges.
6. Reassemble data sectors in logical-segment order → each file set's **Volume Data Area** byte stream.
7. **QIC-113 parse** (§7.5) → directory tree + files (decompress per §7.5 if flagged). This is the output users actually want.
- **Multi-pass:** capture the same track again → another `RawFluxCapture` → **union good sectors before RS**. No drive/bus/firmware in this loop.
- *(Pipeline **structure** is detailed in §6A.5; the RS solver, ID algebra, header/BSM, and QIC-113 layout are now **grounded** — §7.3, §7.5, §13.2 — the one remaining drop-in is the STAC/DCLZ decompressor, §9.)*

---

## 6A. Host software — detailed design

> Expands §6 into implementable detail. The interface sketches below are **signatures, not implementations** — enough to scaffold modules and write tests against, not the finished code. Names are suggestions; the structure is the point.

### 6A.1 Language, runtime, project layout

**Language: Python 3.11+.** This is a deliberate choice driven by the reuse boundary, not preference. The single largest reuse win is the Greaseweazle host library's flux pipeline — PLL / bitcell recovery / MFM (IBM) framing — which is Python. Re-implementing that in another language to keep the host in C#/Go would throw away exactly the part that is hardest to get right and is already battle-tested. The control/transport layers (`link`, `qic117`, `tape`) are simple enough to live in any language, but the codec pins us to Python, so the whole host is Python for cohesion.

> **Escape hatch (recorded, not recommended for v1):** if a different host language is wanted later, the clean seam is the `codec` — keep it as a Python "decode service" consuming `RawFluxCapture` files and emitting a `LogicalVolume` + report, and reimplement only `link`/`qic117`/`tape` elsewhere. Because capture and decode are fully decoupled (the `RawFluxCapture` file is the entire contract), this split is cheap. Do not do it for v1.

**Dependencies.** `greaseweazle` (host package) for the flux primitives and USB device handling; `pyserial` only if not using GW's USB layer; `numpy` for the RS/GF(256) math; **`click`** for the CLI; `tomllib` (stdlib) for drive profiles. No async framework required (see §6A.8).

**Package layout:**

```
tapewyrm/
  __init__.py
  types.py            # shared dataclasses crossing boundaries (§6A.11)
  link/
    transport.py      # USB serial / GW device handling, framing
    client.py         # DeviceLink: the transaction stub API
    protocol.py       # USB opcode constants, packet (de)serialization
  qic117/
    commands.py       # the command table (code, kind, non_intr, name)
    drive.py          # Qic117Drive: dispatch, status/error, reports
    status.py         # DriveStatus / ErrorCode / DriveConfig / TapeStatus decoders
    profile.py        # DriveProfile loader (data, not code)
  tape/
    geometry.py       # Geometry model, serpentine direction
    transport.py      # TapeTransport: positioning, capture orchestration, walk
  codec/
    pipeline.py       # the offline decode stages, wired together
    flux.py           # RawFluxCapture -> FluxStream (reuses GW)
    mfm.py            # bitstream -> RawSector (reuses GW IBM codec)
    place.py          # (FSD,FTK,FSC) -> (track, segment, index)
    rs.py             # GF(256) (32,29) erasure decoder
    volume.py         # header/BSM parse, logical reassembly
    merge.py          # multi-pass union
  rawflux/
    container.py      # RawFluxCapture read/write, marker parsing, verify()
  profiles/
    colorado.toml     # per-drive wake/timing data (one file per drive family)
    iomega.toml
    conner.toml
  cli.py              # probe / capture / decode / recover / replay
tests/
  fixtures/           # golden RawFluxCaptures, recorded transaction logs
  ...
```

### 6A.2 `link` — transport & transaction client

The host's only door to the device. **No QIC or QIC-80 semantics live here** — it speaks the USB transaction protocol (§5.1) and nothing more.

- **Transport.** The GW v4.1 enumerates as a USB CDC-ACM serial device. Reuse GW's port autodetect and CDC framing where possible; otherwise `pyserial`. One process owns the port.
- **Capability gate.** On open, query device `info()` and verify a firmware capability flag advertising the QIC verbs + free-running capture. Refuse to proceed against stock GW firmware.
- **Two interaction shapes over one link:** synchronous request/response for control, and a streaming pull for capture. Both are framed by `protocol.py`.

```python
@dataclass(frozen=True)
class DeviceInfo:
    model: str; mcu: str; firmware: str; serial: str
    usb_high_speed: bool; sram_bytes: int
    qic_caps: frozenset[str]          # e.g. {"verbs","capture","markers"}

class DeviceLink:
    def open(self, port: str | None = None) -> DeviceInfo: ...
    def close(self) -> None: ...

    # --- control (synchronous) ---
    def set_timing(self, t: TimingParams) -> None: ...
    def select(self, hint: SelectHint) -> None: ...
    def command_txn(self, n: int, report_bits: int = 0) -> bytes:
        """Emit n STEP pulses (verbatim); optionally clock `report_bits`
        off TRK0. Returns the (possibly empty) report bytes."""
    def wait_ready(self, timeout_ms: int) -> bool: ...

    # --- capture (streaming) ---
    def capture(self, motion_cmd: int, stop: StopCond) -> "CaptureStream":
        """Open a capture session: device takes the lease, issues motion,
        streams GW flux. Returns a stream handle (context manager)."""

class CaptureStream(AbstractContextManager):
    def chunks(self) -> Iterator[bytes]:   # raw GW flux bytes incl. markers
        ...
    def abort(self) -> None:               # out-of-band stop, valid mid-stream
        ...
    # __exit__ guarantees the device session is torn down (stop/abort)
```

`command_txn` is the verbatim seam: the host passes a command *number*, the firmware never learns its meaning. `capture()` returns a handle whose `chunks()` is drained by the reader (see §6A.8); `abort()` writes the out-of-band stop control. Reconnection and timeouts raise typed `LinkError` subclasses; the device's in-stream `EVENT{overflow}`/`END` markers surface through the byte stream (parsed in `rawflux`).

### 6A.3 `qic117` — drive & protocol layer

Where semantics begin. Wraps a `DeviceLink`; turns "seek to load point" into the right transaction sequence and decodes what comes back.

- **Command table as data.** All ~47 commands as a frozen table, sourced from the QIC-117 spec / ftape `qic117.h`.

```python
class Kind(Enum): MODE = auto(); MOTION = auto(); REPORT = auto()

@dataclass(frozen=True)
class Cmd:
    code: int; kind: Kind; non_intr: bool; name: str
    takes_arg: bool = False

TABLE: dict[str, Cmd] = {
    "SOFT_RESET":            Cmd(1,  Kind.MODE,   True,  "soft reset"),
    "REPORT_NEXT_BIT":       Cmd(2,  Kind.REPORT, True,  "report next bit"),
    "PAUSE":                 Cmd(3,  Kind.MOTION, False, "pause"),
    "REPORT_DRIVE_STATUS":   Cmd(6,  Kind.REPORT, True,  "report drive status"),
    "REPORT_ERROR_CODE":     Cmd(7,  Kind.REPORT, True,  "report error code"),
    "REPORT_CONFIGURATION":  Cmd(8,  Kind.REPORT, True,  "report configuration"),
    "LOGICAL_FORWARD":       Cmd(10, Kind.MOTION, False, "logical forward"),
    "SEEK_HEAD_TO_TRACK":    Cmd(13, Kind.MOTION, True,  "seek head to track", takes_arg=True),
    "SEEK_LOAD_POINT":       Cmd(14, Kind.MOTION, False, "seek load point"),
    "STOP_TAPE":             Cmd(18, Kind.MOTION, False, "stop tape"),
    "REPORT_TAPE_STATUS":    Cmd(33, Kind.REPORT, True,  "report tape status"),
    # ... fill out the full set, incl. vendor-unique 31, 40-45 ...
}
```

- **Dispatch by kind** (this is the whole point of tagging):

```python
class Qic117Drive:
    def __init__(self, link: DeviceLink, profile: DriveProfile): ...

    def command(self, cmd: Cmd, arg: int | None = None) -> DriveStatus | None:
        if cmd.takes_arg:
            self._send_arg(cmd, arg)            # cmd, then operand as N+2 pulses
        else:
            self.link.command_txn(cmd.code)
        # Logical Forward streams to EOT and stays NOT-Ready for the whole pass —
        # never wait-ready/status after it (a status report would swallow segment 0).
        if cmd.kind is Kind.MOTION and not cmd.is_streaming:
            self.link.wait_ready(self.profile.timing.motion_timeout_s)
            return self.status()
        return None

    def report(self, cmd: Cmd, nbits: int) -> int:
        # device clocks ACK (must be true), nbits LSB-first, then Final (false=error);
        # link returns the nbits data payload, raising on a bad ACK/Final.
        raw = self.link.command_txn(cmd.code, report_bits=nbits)
        return bits_to_int(raw, self.profile.bit_order)

    def status(self) -> DriveStatus:
        b = self.report(TABLE["REPORT_DRIVE_STATUS"], 8)
        st = DriveStatus.decode(b)
        if st.new_cartridge or st.error:          # both cleared via Report Error Code
            self._last_error = ErrorCode.decode(self.report(TABLE["REPORT_ERROR_CODE"], 16))
        return st

    def config(self) -> DriveConfig: ...          # data rate
    def tape_status(self) -> TapeStatus: ...       # QIC-40/80/3010/3020 + length
    def wake(self) -> None: ...                    # runs profile.wake_sequence
    def reset(self) -> None: ...
```

- **`DriveProfile` — the injection seam (data, not code).** Per-drive wake/select quirk + timing envelope, loaded from `profiles/*.toml`. This is the analogue of ftape `vendors.h`; a new drive is a new TOML file, never a code change.

```python
@dataclass(frozen=True)
class TimingParams:
    pulse_us: int; inter_pulse_us: int; terminate_gap_us: int
    report_settle_us: int; motion_timeout_s: int    # motion runs seconds–minutes

@dataclass(frozen=True)
class DriveProfile:
    name: str
    wake_sequence: tuple[tuple[str, int | None, int], ...]  # (cmd, arg, delay_ms)
    timing: TimingParams
    bit_order: str                # "msb" | "lsb"
    report_strategy: str          # "index_edge" | "fixed_settle"
    quirks: frozenset[str]
```

- **Error classification.** A table mapping error codes → fatal/benign, consulted by `tape` to decide whether to abort the sweep (broken-tape = fatal hard stop; reset-occurred = benign).

### 6A.4 `tape` — transport & capture orchestration

Logical motion + geometry; turns a track number into a `RawFluxCapture`.

```python
@dataclass(frozen=True)
class Geometry:
    tracks: int; segments_per_track: int; sectors_per_segment: int = 32
    def direction(self, track: int) -> Direction:     # even=fwd, odd=reverse (logical)
        ...
    def byte_budget(self, rate_kbps: int) -> int:      # for the capture stop condition
        ...

class TapeTransport:
    def __init__(self, drive: Qic117Drive): ...

    def identify(self) -> tuple[DriveConfig, TapeStatus, Geometry]:
        self.drive.wake(); ...                          # config + tape_status -> geometry

    def load_point(self) -> None:
        self.drive.command(TABLE["SEEK_LOAD_POINT"])

    def seek_track(self, t: int) -> None:
        self.drive.command(TABLE["SEEK_HEAD_TO_TRACK"], arg=t)

    def capture_pass(self, track: int, pass_id: int) -> RawFluxCapture:
        self.seek_track(track)
        hdr = CaptureHeader(rate=self.cfg.rate, sample_clock=self.dev.clock,
                            track=track, direction=self.geom.direction(track),
                            pass_id=pass_id, utc=now())
        stop = StopCond(byte_budget=self.geom.byte_budget(self.cfg.rate))
        with self.drive.link.capture(TABLE["LOGICAL_FORWARD"].code, stop) as cap:
            return RawFluxCapture.from_stream(hdr, cap.chunks())   # drains to file

    def walk_all(self, passes: int = 1) -> Iterator[RawFluxCapture]:
        for t in range(self.geom.tracks):
            for p in range(passes):
                self._guard_hazards()                  # broken-tape -> abort + raise
                yield self.capture_pass(t, p)
```

Serpentine is handled by the drive (logical-forward presents data in order regardless of physical direction), so `tape` never reverses flux — it only records `direction` in the header for the codec.

### 6A.5 `codec` — offline decode pipeline

Pure functions over `RawFluxCapture`; **no hardware, fully testable** (§6A.10). Wired as discrete stages so each is independently testable and replaceable.

```python
# flux.py  — reuse GW byte->intervals + split markers
def load(cap: RawFluxCapture) -> tuple[FluxStream, list[Marker]]: ...

# mfm.py   — reuse GW PLL + IBM/MFM framing
def recover_sectors(flux: FluxStream, rate_kbps: int) -> Iterator[RawSector]: ...

# place.py — (FSD,FTK,FSC) -> (SEG,TPT,TPS,sec) via §7.3 algebra
def place(sectors: Iterable[RawSector]) -> dict[tuple[int,int], Segment]: ...

# rs.py    — GF(256) erasure decode, redundancy 3, N=31-bad (algorithm §13.2)
def correct(seg: Segment) -> SegmentResult:
    erasures = [i for i, s in enumerate(seg.sectors) if not s.data_crc_ok]
    # column-wise RS over GF(256); recover up to 3 erased sectors (§13.2)
    ...

# volume.py — header/BSM + volume table -> per-file-set byte stream
def parse_header(seg0: Segment) -> tuple[VolumeInfo, BadSectorMap]: ...
def volume_streams(segs: dict, vol: VolumeInfo, bsm: BadSectorMap) -> list[tuple[VtblEntry, bytes]]: ...

# qic113.py — file-set byte stream -> directory tree + files (§7.5)
def extract(stream: bytes, vtbl: VtblEntry) -> FileSet: ...

# pipeline.py — top-level wiring + recovery report
def decode(caps: list[RawFluxCapture]) -> tuple[list[FileSet], RecoveryReport]:
    sectors = merge.union(load_and_recover(c) for c in caps)  # multi-pass first
    segs = place(sectors)
    results = {k: correct(s) for k, s in segs.items()}
    vol, bsm = parse_header(segs[first_good_key(results)])
    filesets = [extract(s, v) for v, s in volume_streams(segs, vol, bsm)]
    return filesets, RecoveryReport(results, bsm)
```

Stage notes:
- `recover_sectors` is mostly a thin adapter over GW's existing IBM/MFM decoder; the only QIC-specific part is *not* discarding the abused C/H/S — we keep them as `(fsd, ftk, fsc)`.
- `merge.union` runs **before** RS: across multiple passes, take any sector whose `data_crc_ok` in *any* pass; only then hand erasure positions to RS. This is the multi-pass recovery win.
- `correct` uses CRC-derived erasure positions, so it gets full redundancy-3 erasure correction (up to 3 sectors/segment), far stronger than blind error decoding.

### 6A.6 `rawflux` — capture container

Read/write + integrity. Keep the on-disk format dead simple and lossless: a length-prefixed JSON header, then the verbatim GW flux byte stream (markers inside as opcodes). No re-encoding.

```python
@dataclass
class RawFluxCapture:
    header: CaptureHeader
    flux: bytes                       # verbatim on-wire GW flux (with marker opcodes)

    @classmethod
    def from_stream(cls, hdr: CaptureHeader, chunks: Iterator[bytes]) -> "RawFluxCapture": ...
    def markers(self) -> Iterator[Marker]: ...      # walk opcode escapes
    def verify(self) -> bool:                        # check END counts + checksum
        ...
    @property
    def is_truncated(self) -> bool:                  # no valid END (e.g. USB loss)
        ...
    def save(self, path: Path) -> None: ...
    @classmethod
    def load(cls, path: Path) -> "RawFluxCapture": ...
```

A truncated capture (no `END`) is *not* an error — it still decodes, because sectors self-locate; `is_truncated` just flags reduced confidence in the tail.

### 6A.7 CLI, configuration, logging

Built on **Click**: a single `@click.group()` exposed as the **`tw`** executable (long alias `tapewyrm`), one `@cli.command()` per verb, with shared options (`--port`, `--profile`, `--config`) hung off the group via a `click.Context` object so subcommands inherit them. Seven verbs, each mapping to a layer (flashing included, so `tw` is self-sufficient — no `gw`):

| Command | Does | Layers |
|---|---|---|
| `tw probe` | open device, wake, identify; print config / tape status / geometry | link, qic117, tape |
| `tw capture` | sweep tracks → write `RawFluxCapture` files | + rawflux |
| `tw decode` | `RawFluxCapture`(s) → `LogicalVolume` + recovery report (no hardware) | rawflux, codec |
| `tw recover` | capture + decode + multi-pass retries on weak segments | all |
| `tw replay` | re-decode saved flux with different PLL/RS options | rawflux, codec |
| `tw flash` | update firmware via the GW-compatible application bootloader over USB | link/update |
| `tw dfu` | recovery / first flash via the AT32 ROM bootloader (wraps `dfu-util`) | link/update |

```python
import click

@click.group()
@click.option("--port", default=None, help="GW serial port (autodetect if unset)")
@click.option("--profile", default=None, help="drive profile name")
@click.option("--config", type=click.Path(), default=None)
@click.pass_context
def cli(ctx, port, profile, config):
    ctx.obj = AppContext.load(port, profile, config)   # resolves precedence below

@cli.command()
@click.pass_obj
def probe(app): ...

@cli.command()
@click.option("--passes", default=1)
@click.option("-o", "--out", type=click.Path(), required=True)
@click.pass_obj
def capture(app, passes, out): ...
# decode / recover / replay similarly
```

Config precedence: CLI flags → config file → profile defaults, resolved once in `AppContext.load` and carried on `ctx.obj`. Key settings: device port, drive profile, passes-per-track, output dir, PLL/decode options. Logging is structured and per-stage; long captures emit progress (track N/M, MB streamed) via a `click.progressbar`; decode emits per-segment results.

### 6A.8 Concurrency & data-flow model

- **Control path:** synchronous, blocking request/response. No concurrency needed.
- **Capture path:** streaming, and the one place concurrency matters. Model it as **reader → bounded queue → writer**:
  - a **reader thread** drains `CaptureStream.chunks()` off the serial port as fast as the USB delivers;
  - a bounded `queue.Queue` decouples it from disk;
  - a **writer** drains the queue to the `RawFluxCapture` file.
  This keeps a slow disk from stalling USB reads (which would back-pressure the device and trip its overflow → abort). The host-side queue + the device's SRAM ring buffer together absorb stalls.
- **Abort** is a control write (`CaptureStream.abort()`) issued from the main thread; the reader loop terminates when it sees the `END` marker in the stream.
- **Threads, not asyncio.** pyserial is blocking; a single reader thread + queue is simpler and entirely sufficient here. (asyncio with a thread executor is possible but buys nothing.)

### 6A.9 Error handling, partial captures, observability

- **Typed errors:** `LinkError` (transport/timeout/version), `DriveError` (carrying the QIC error code + fatal/benign classification), `CaptureError` (overflow/aborted), `DecodeError`.
- **Overflow:** an in-stream `EVENT{overflow}` means the device aborted the session cleanly; the host records it and **retries the pass** rather than trusting a gapped capture.
- **Truncation:** missing `END` → flagged, decoded anyway.
- **Recovery report** is the user-facing quality signal: per-segment status (`clean` / `corrected(k)` / `uncorrectable`), per-track coverage %, BSM accounting (expected-bad vs unexpected-bad), and a list of segments worth re-capturing. `recover` uses this to decide which tracks to re-run.

### 6A.10 Testing strategy (hardware-free first)

The capture/decode decoupling makes most of the system testable with no drive attached:

- **Golden fixtures:** record a few real `RawFluxCapture` files (or synthesize them) and commit as fixtures; the entire `codec` is tested against them offline, deterministically.
- **Mock `DeviceLink`:** replays a recorded transaction log (for control) and a recorded flux byte stream (for capture). Lets `qic117` and `tape` be tested end-to-end without hardware.
- **Unit tests:** RS erasure decode against known vectors (cross-check with ftape `ecc`); MFM framing against GW's own decoder tests; `(FSD,FTK,FSC)` → geometry with hand-built IDs; status/error/config bit-decoding tables.
- **Property tests:** `RawFluxCapture` save/load round-trip is lossless; any segment with ≤3 injected erasures always recovers; capture order permutations yield identical placement (sectors self-locate).
- **Integration (with hardware):** `probe` smoke test (wake + identify) on the target drive; then a full `recover` of a known tape, diffed against DOS/ftape output if available.

### 6A.11 Core data types (reference)

The dataclasses that cross module boundaries (all in `types.py` unless noted):

| Type | Carries | Produced by | Consumed by |
|---|---|---|---|
| `DeviceInfo` | model, mcu, fw, caps, sram | `link` | CLI, capability gate |
| `TimingParams`, `SelectHint`, `StopCond` | config for the device | `qic117`/`tape` | `link` |
| `DriveStatus` | ready/error/cartridge/wp/new/referenced/bot/eot | `qic117` | `tape`, CLI |
| `ErrorCode` | QIC error + fatal flag | `qic117` | `tape` |
| `DriveConfig` | data rate | `qic117` | `tape`, `codec` (rate) |
| `TapeStatus` | format (QIC-40/80/3010/3020), length | `qic117` | `tape` (geometry) |
| `DriveProfile` | wake seq, timing, quirks | `profiles/*.toml` | `qic117` |
| `Geometry` | tracks, segs/track, direction() | `tape` | `tape`, `codec` |
| `CaptureHeader` | rate, clock, track, direction, pass-id, utc | `tape` | `rawflux`, `codec` |
| `Marker` | session-start / heartbeat / event / end | `rawflux` (parse) | `codec`, reports |
| `RawFluxCapture` | header + verbatim flux bytes | `tape`/`rawflux` | `codec` |
| `FluxStream` | decoded intervals | `codec.flux` | `codec.mfm` |
| `RawSector` | (fsd,ftk,fsc), data[1024], crc flags, deleted | `codec.mfm` | `codec.place` |
| `Segment` | 32 `RawSector` slots | `codec.place` | `codec.rs` |
| `VolumeInfo`, `BadSectorMap` | header-segment contents | `codec.volume` | `codec.reassemble` |
| `LogicalVolume` | recovered logical-sector stream / files | `codec` | CLI, user |
| `RecoveryReport` | per-segment/track recovery quality | `codec` | CLI, `recover` |

---

## 7. Data formats

### 7.1 `RawFluxCapture` — a linear track-stack, not a disk image

The container reuses GW's flux **byte encoding** but **not** its disk framing. A `.scp` image is organized around *revolutions*: each index-to-index span is one full circular track, and sectors live around the revolution. Tape has no revolutions — it is a linear medium written as a **serpentine stack of tracks**, each track a linear run of index-delimited segments. So the container frames flux that way (full model in §7.4):

- **Capture header (the linearization key).** Recording-format (QIC-40/80/3010/3020), declared bitcell rate (from `REPORT_DRIVE_CONFIGURATION`, stamped before arming), sample clock/tick rate, **geometry** `{segments_per_track, tracks, sectors_per_segment=32}`, tape type/length if known, device serial, start UTC. Geometry is what lets the decoder turn a disk-coordinate sector ID into a linear `(TPT, TPS)` position.
- **Track runs (the stack).** One per captured tape-track pass: `{TPT, direction (even=forward / odd=reverse), pass-id, flux-run}`. The flux-run is the verbatim GW byte stream for that pass, with its in-stream segment-index markers. A salvage pass taken in *physical* reverse is flagged so its flux can be time-reversed offline; a normal Logical-Forward pass is already in logical order and needs no reversal.
- **Segment marks (within a run).** Each hardware INDEX edge is recorded as a `SEGMENT` marker, and the in-stream `C2/FC` index address mark is the second, independent boundary — together they delimit the linear segment sequence the FDC sees.
- **Footer:** implicit, in the in-stream `END` opcode (counts + checksum). A run with no valid `END` is flagged truncated but still decodes (sectors self-locate).

Multiple passes of the same `TPT` are independent runs in (or across) captures; the codec unions their good sectors before RS (§6.4). One capture file may hold one run, one track's passes, or a whole sweep — the header + per-run `TPT`/direction make it self-describing either way.

### 7.2 Tape marker opcodes (shared firmware/host header)
Markers ride the GW opcode-escape channel, so they can never be misread as flux.

| Marker | Payload | When |
|---|---|---|
| `SESSION_START` | rate, sample clock, **TPT, direction**, pass-id, UTC | at arm — anchors t=0 and tags the run's place in the stack |
| `SEGMENT` | tick count since previous (and running index) | each hardware INDEX edge — the per-segment boundary |
| `EVENT` | code (motion-started, hole/EOT edge, overflow, gap…) | on observed bus/drive event |
| `END` | reason, flux-count, byte-count, checksum | at disarm — seals the run |

> `HEARTBEAT` from the earlier draft is **demoted**: its job was to let the host re-align a stream that had no intrinsic landmarks, but the per-segment `SEGMENT`/INDEX marker now supplies real boundaries. Keep a coarse keepalive only if a long erased stretch (inter-segment gap, end-of-data) would otherwise starve the stream of markers.

### 7.3 QIC-80-MC format reference (decode target — grounded against Rev N)

**Encoding.** MFM, MSB-first, nominal 14,700 BPI / 68 µin bit cell, tape 34 ips. Rate: 500 kbit/s (QIC-80); 1 Mbit/s (QIC-3010, 22,125 BPI); 2 Mbit/s (QIC-3020, 42,000 BPI).

**Segment = 32 sectors** (29 data + 3 ECC), each sector a 1024-byte block. On-tape byte layout per segment:

```
SEGMENT HEADER   12×00 sync · 3×C2 + FC (index addr mark, missing-clock C2) · 4E gaps
  ×32 sectors:
    SECTOR ID    12×00 sync · 3×A1 + FE (id addr mark, missing-clock A1)
                 FTK · FSD · FSC · 03(=1024B) · 2×CRC · 4E gap
    DATA BLOCK   12×00 sync · 3×A1 + FB (data addr mark; F8 = deleted/bad)
                 1024 data · 2×CRC · 4E gaps + dropout guard
  4E gap until next index
```
- **CRC:** CCITT `x¹⁶+x¹²+x⁵+1`, register preset all-ones, over the 8 ID bytes / 1028 data bytes.
- **Sector ID = abused C/H/R/N:** cylinder=`FTK`, head=`FSD`, record=`FSC`, size=`03`.

**Coordinate algebra** (`segments_per_track` = `spt`, e.g. 207 @ 425 ft):
```
LSN = 32640·FSD + 128·FTK + (FSC−1)
SEG = 1020·FSD + 4·FTK + ⌊(FSC−1)/32⌋     FSD=SEG/1020  FTK=(SEG mod 1020)/4  FSC=(SEG mod 4)·32+1
TPT = ⌊SEG/spt⌋     TPS = SEG mod spt
ranges: 1≤FSC≤128, 0≤FTK≤254, FSD length-dependent; (FSD,FTK,FSC)=(0,0,1) ⇒ tape track 0, segment 0
128 sectors = 1 floppy track = 4 segments;  1020 segments = 1 floppy side
```

**ECC (Reed–Solomon, per column).** Bytes of a segment form a 32×1024 matrix; each **column** is an independent RS codeword of redundancy 3 over GF(256), rows 0–28 data, 29–31 parity, written row-by-row. Field `f(x)=x⁸+x⁷+x²+x+1`; generator `g(x)=x³+r¹⁰⁵·x²+r¹⁰⁵·x+1` with `r¹⁰⁵ = 0xC0`. Corrects ≤3 CRC-error sectors per segment (or 1 error + 1 CRC-failure). **Excluded sectors are physically skipped**; parity occupies the last 3 non-excluded sectors; codeword length `N = 31 − bad-blocks`. (Test codewords are tabulated in Rev N §6.2.4 — use them as RS unit-test vectors.)

**Geometry.** 28 tracks (0.250 in) / 36 (0.315 in/Travan); **even tracks forward, odd reverse** (serpentine), referenced to forward/reverse BOT bursts; equal segment count per track. `spt` is variable; if the drive lacks Calibrate/Report-Format-Segments (CCS-2), fall back to the QIC-117 override: 100 (≤153 calibrated) or 207 (154–228).

**Header segment.** First defect-free segment (duplicated in the second); preceding/intervening segments are deleted-data. Sector 0 = **format parameter record** (offset 0–3 sig `55 AA 55 AA`; 4 = format code `04` variable-length; 24–25 segments/track; 26 tracks; 27 max FSD; 28 max FTK=254; 29 max FSC=128; 30–73 ASCII tape name; dates as packed `SC+60·(MN+60·(HR+24·(DY+31·MO)))`). Sectors 0–28 = **bad-sector map** (ascending 3-byte LSN entries, 1-based; `0`=end; **high bit of MSB set ⇒ that whole 32-sector segment is bad**). Sectors 29–31 = ECC.

**Volume table segment.** First segment of the logical area; 128-byte `VTBL` entries (start/end SEG, description, flags, OS type `1`=DOS required, compression per QIC-123), with extensions `XTBL` (unicode), `UTID` (unicode tape name), `EXVT` (overflow to another segment). The file-set/file layout *inside* the volume is **QIC-113**, specified in §7.5 (the top of the decode stack).

### 7.4 Circular-track → linear-track-stack framing (the model)

This is the conceptual shift the container encodes. **What the FDC believes:** it is addressing a diskette by `(side=FSD, cylinder=FTK, record=FSC)`, finding sectors around an index-delimited *revolution*. **What is physically there:** a linear tape, written as a serpentine stack of tracks; the FDC's disk coordinates are an *overlay* on linear position via the §7.3 algebra. Reconciling the two:

| FDC / disk view | Tape reality | Bridge |
|---|---|---|
| one revolution = one circular track, one index/rev | one **segment** = one index-delimited run of 32 sectors; **many segments per tape track** | INDEX fires per segment, not per track; `SEGMENT` markers record each |
| `(FSD, FTK, FSC)` C/H/S address | linear `(TPT, TPS, sector-in-segment)` | `SEG = 1020·FSD+4·FTK+⌊(FSC−1)/32⌋`; `TPT=⌊SEG/spt⌋` |
| heads stacked, all tracks same direction | tracks stacked **serpentine** — even forward, odd reverse | per-run `direction`; physical-reverse salvage is time-reversed offline |
| `.scp` stores per-revolution flux | capture stores **per-tape-track runs of segments** | `RawFluxCapture` header geometry + track runs (§7.1) |

Practical consequence: the decoder treats a track run as a **linear sequence of index-delimited segments**, assigns each recovered sector by its self-locating ID to `(TPT, TPS, sector-in-segment)`, cross-checks that against the `SEGMENT`/`C2-FC` boundary sequence, and stacks tracks `0…N−1` into the logical volume. Because sectors self-locate, the linear framing is a **robustness + honest-representation** layer (boundaries corroborate IDs, and the file represents tape as tape) rather than a correctness dependency — a capture with damaged segment marks still reassembles from IDs alone.

### 7.5 QIC-113 — file-set extraction (the top of the stack, grounded against Rev G)

After RS decode (§6.4 step 4), the **data sectors** of a file set's segment range — taken from its `VTBL` entry (§7.3), concatenated in logical-segment order with ECC sectors dropped — form a contiguous **Volume Data Area** byte stream. QIC-113 defines that stream's structure: the directory tree and the file bytes. Note the data and directory sections **observe no sector/segment boundaries** (densely packed), *except* the directory section is **segment-aligned**; the 4-byte signatures below are resync anchors that make partial recovery from a damaged volume tractable.

**Which level (QIC-40/80, Rev G §6).** Detect from the `VTBL` entry: if byte 56 bit 0 (vendor-specific) is set **and** vendor-extension words at offsets 58/60 read `113`/`7`, it is an **Extended-OS** volume (Rev G §8); otherwise, or if byte 125 (Format & OS Type) `= 1`, it is **Basic DOS** (§7). Basic DOS is the dominant case for QIC-80 DOS-era backups and the primary target.

**Volume layout.** Directory-Last flag = `VTBL` byte 56 bit 5:
- clear (Directory-First): `Directory Section + Data Section`, directory wholly on the first cartridge.
- set (Directory-Last; always set for Extended-OS): `Data Section + Segment Gap + Directory Section`, the directory segment-aligned and located by subtracting `Directory Section Size` (offsets 92–95, rounded up to whole segments) from the Ending Segment.

**Basic DOS (§7).**
- *Directory Section* = concatenated variable-length **Directory Entries**, each: Fixed Portion `{1B fixed+vendor size · 1B attrs [b0 read, b1 write, b2 exec, b3 hidden, b4 system, b5 subdir, b6 last-in-dir, b7 last-in-table] · 4B modify short-date · 4B data-entry-size · 1B extra-info [bits0–5: 2 = unreadable-at-backup]}` + `[vendor portion if size>10]` + Name Portion `{1B name size · ASCII name}`. **Ordering is breadth-first preorder**: root entries first, each directory's entries terminated by the `last-in-dir` bit, then descend left→right; `last-in-table` ends it. The tree reconstructs from that ordering + the dir/last bits.
- *Data Section* = concatenated **Data Entries**, same order: `{4B signature 0x33CC33CC (on tape little-endian → CC 33 CC 33) · copy of the Directory Entry · Path Entry [1B size · null-separated ASCII path] · file bytes}`. Non-empty directories have no data entry; empty directories carry a header-only entry (so the tree survives even if the directory section is lost).
- *Extraction*: parse the directory section into the tree (names/attrs/dates/sizes), then walk the data section — each `0x33CC33CC`-anchored entry yields path + bytes → write the file. The signature is the resync point when a volume is partially corrupt.

**Extended-OS (§8, summary).** Directory-Last always set. Names are **Unicode**. Data Entry = `0x33CC33CC + Directory Entry + Path Entry + 0..n Data Areas`; each **Data Area** = `{4B 0x66996699 · 2B Data-Area-ID · data}` where data is Null / Blob (`ID 7` = primary file bytes; `ID 6` = AFP resource fork) / Multirecord (Extended-OS records). The extended **Directory Entry** = Fixed Portion `{2B dir-entry-size · 8B data-entry-size · 2B path-size · 2B native-FS · 1B traversal [b0 dir, b1 empty-dir, b2 file-error, b3 last-in-dir, b4 last-in-media, b5 last-in-set, b6 root-entry]}` + Data Description Portion (≥1 Data Description Entries: `{2B ID · 8B data-area-size · 2B struct-size · struct · 2B name-size · Unicode name}`). **Data Description IDs** (priority-ordered): 0 vendor, 1 UNIX, 2 DOS, 3 Novell-3, 4 OS/2, 5 NT, 6 AFP, 7 Data, 8 Novell-2, 9 Novell-4, 10 Win95 — each with its own attribute structure (DOS: `1B attrs + 8B modify`; NT/Win95/OS2: `4B attrs + create/access/modify`; UNIX: `3B perms + 3 dates + inode/uid/gid + major/minor`; Novell/AFP richer). A root entry's name is the source device (`C:`, `\\SERVER\SYS`, `/usr/mounted`).

**Dates.** Basic uses Short Date/Time (`bits31–25 year−1970`, `bits24–0` packed `sc+60·(mn+60·(hr+24·(dy+31·mo)))` — same encoding as QIC-80-MC). Extended uses Full Date/Time (`4B seconds since 1970 GMT` + `4B tz/µs`); `0xFFFFFFFF`/all-`FF` = undefined.

**Compression (§9, if `VTBL` byte 124 bit 7 set).** Volume Data Area = Compression Extents → Compression Frames. Frame = `{2B Frame Size (hi bit set ⇒ data is uncompressed raw) · data bytes}`. Non-segment-spanning: one extent per segment at byte 0. Segment-spanning (`VTBL` byte 56 bit 4): first 2B of each segment = `Next Extent Offset`; each extent opens with an `8B Uncompressed Volume Byte Offset` for seek-without-decompress. The frame codec itself is **STAC LZS** (compression code 1) or DCLZ/ALDC (QIC-122/130/154) — a drop-in decompressor, not specified here.

**Multi-cartridge.** A `Link Sub-Section` (`'LTLT'` signature + media-sequence count + per-media ending offsets) is appended to the directory section; out of scope for a single-cartridge first target but the signature should be recognized and skipped.

This layer is implemented as a pure `codec/qic113.py` consuming `(volume_byte_stream, vtbl_entry)` and yielding a directory tree + file blobs; like the rest of the codec it touches no hardware and is fixture-testable.

---

## 8. End-to-end data flow

**Trace A — command + status transaction (e.g., seek load point, then read status):**
`TapeTransport` → `Qic117Drive` looks up command, knows follow-up → `DeviceLink.command_txn(14, report_bits=0)` then `wait_ready` (bytes over USB, no bus) → crosses real-time line → firmware decodes, arbiter grants lease to verbs engine → 14 STEP pulses (verbatim) → release. Then `command_txn(6, report_bits=8)`: emit 6 pulses, clock 8 report bits off TRK0 device-side, return byte → host parses `DriveStatus`; on ERROR, `command_txn(7,…)` to read+clear.
*Representation:* `"seek load point"` → `(14, report=0)` → STEP edges (down); TRK0 samples → 8 bits → `DriveStatus` (up). Semantics exist only at the very top and the timing only at the very bottom.

**Trace B — capture session (one track pass):**
`capture_pass()` → `DeviceLink.capture_session(LOGICAL_FORWARD, stop)` → arbiter grants the lease and **holds it for the whole session** → verbs engine issues motion → firmware waits lead-in, arms flux engine free-running → flux engine encodes RDATA to GW flux + injects markers, streams over USB → host appends to `RawFluxCapture` with **no deadline** (recording, not feeding an FDC → no underrun/shoe-shine) → on stop, verbs engine issues STOP, flux engine emits `END`, lease released → offline codec runs PLL/MFM → sectors → segments → RS erasure → volume.
*Invariant:* one lease, device-held, granted to one engine at a time → control and flux can never collide, because the host has no verb to express it.

---

## 9. Open questions / work queue

Several earlier unknowns are now **resolved** against the standards (recorded so they aren't re-litigated):

- *QIC-117 timing* — STEP interval ~2.0 ms, command time-out 2.2–2.9 ms, report bit < 900 µs, motion timeouts seconds–minutes; the single-pulse=reset and TRK0-inactive hazards (§2.1, §5.3).
- *Argument framing* — **N+2 pulse form** (Soft Select = literal 20) (§2.1, §5.3).
- *Report framing* — ACK-first / Final-bit / LSB-first / value latched at command receipt, up to 16 bits (§2.1, §5.3).
- *INDEX on tape* — **per-segment**, not absent; recorded as a marker and cross-checked by the in-stream `C2/FC` mark (§2.2, §5.4, §7).
- *On-tape format* — exact segment byte layout, CCITT CRC, the `(FSD,FTK,FSC)↔(SEG,TPT,TPS)` algebra, RS field/generator (`f`, `g`, `r¹⁰⁵=C0`), header/BSM/volume-table structures (§7.3).
- *Geometry source* — Calibrate / Report Format Segments (CCS-2) with the fixed 100/207 fallback (§2.1, §7.3).
- *DFU recovery path* — jumper DFU↔3V3 + **ArteryISP**, not stock `dfu-util` (§12.3).

**Still open:**

1. **GW flux encoding byte-level specifics** — exact opcode values and the long-flux continuation scheme; read from `greaseweazle-firmware`. Then assign the `SEGMENT` / `SESSION_START` / `EVENT` / `END` opcodes in the shared header.
2. **Firmware extension points** — adding the verbs to GW's command set; whether free-running capture is a new command or a flag on the existing flux read; mapping `stop`/`abort` onto GW's read-stop control.
3. **Target-drive specifics (narrowed, not eliminated).** Init is *specified* (reset → up to ~1 s diagnostics → clear New-Cartridge/Error via Report Error Code → auto seek-load-point), so this is no longer a mystery handshake. The real unknowns: the drive's **CCS level** (does it support 36/37 geometry, Skip-Extended, Report Tape Status?), its exact timings, and vendor quirks. Characterize on the bench via `gw pin` + scope; encode as a `DriveProfile` (ftape `vendors.h` for hints).
4. **34-pin open-collector assumption** — confirm the target drive is the Subsection-I 34-pin open-collector interface GW matches, **not** the 40-pin tri-state variant (§2, §3).
5. **v4.1 schematic** — TRK0/INDEX on pollable GPIO/EXTI; STEP buffer swings the bus at cadence.
6. **RS decoder excluded-sector handling** — implement the `N = 31 − bad-blocks` codeword length and the skip-and-repack of BSM-excluded sectors (§7.3; Rev N Fig 6.4); validate against the Rev N test codewords and ftape `ecc.c`.
7. **QIC-113 (Host Interchange Format)** — **read; specified in §7.5.** Basic-DOS file extraction is fully grounded; what remains open is the **STAC LZS / DCLZ / ALDC decompressor** for compressed volumes (QIC-122/130/154, not read) and full Extended-OS attribute round-tripping (the framing is specified; per-OS structures are summarized).
8. **QIC-3010/3020 deltas** — same framing, higher density/rate (1 / 2 Mbit/s, 22,125 / 42,000 BPI, 40/50 tracks) and 3020's larger pre-mapped BOT/EOT bad zones; fold into geometry/profile when targeting them.
9. **Drive power/termination** — separate 5V/12V PSU; USB 5V isolation jumper set; terminate the bus at the drive end as for a floppy.
10. **App-container parse (above QIC-80/113)** — CP Backup CPB and similar are a separate, app-specific parse on the recovered logical data.

---

## 10. References (sources leaned on)

**Provenance:** the command channel (§2.1, §5.3) is grounded against **QIC-117 Rev J**, the recording format (§2.2, §7.3) against **QIC-80-MC Rev N**, and the file-set logical format (§7.5) against **QIC-113 Rev G** — all three read in full. QIC-3010/3020-MC and QIC-40-MC are *not yet read* (deltas noted in §9); the STAC/DCLZ/ALDC compression codecs (QIC-122/130/154) are *not read* (decompressor is a drop-in).

- **QIC-117 Rev J** — Command Set Interface (the command channel): STEP command/argument pulses (N+2), TRK0 report framing (ACK/Final), INDEX cueing + per-segment marking, Table 1 timing, restriction/argument/response/timeout tables, error codes, 34-pin open-collector electrical (Subsection I). `https://www.qic.org/html/standards/11x.x/qic117j.pdf`
- **QIC-80-MC Rev N** — Recording Format: tracks/serpentine, segment MFM byte layout, sector-ID coordinate algebra, RS ECC (field/generator/test codewords), header segment + format parameter record + BSM, volume table. `https://www.qic.org/html/standards/8x.x/qic80n.pdf`
- **QIC-113 Rev G** — Host Interchange Format: volume data area, Basic-DOS + Extended-OS directory/data sections, directory entry layouts, signatures (`0x33CC33CC`, `0x66996699`, `VTBL`, `LTLT`), compression framing. `https://www.qic.org/html/standards/11x.x/qic113g.pdf`
- **QIC-3010-MC Rev H / QIC-3020-MC Rev H** — higher-density MFM floppy-tape formats (1 / 2 Mbit/s). `https://www.qic.org/html/standards/301x.x/qic3010h.pdf`, `https://www.qic.org/html/standards/302x.x/qic3020h.pdf`
- **QIC-40-MC Rev M** — the 20-track predecessor QIC-80 extends. `https://www.qic.org/html/standards/4x.x/qic40m.pdf`
- **ftape** (GPL, Bas Laarhoven) — reference implementation. Mine: `qic117.h` (command set/types/status/errors), `ecc.c`/`ecc.h` (RS), `vendors.h` (`wake_up_*` quirks). HOWTO/FAQ for behavioral notes.
- **Greaseweazle** — `keirf/greaseweazle` (host tools, flux encoding, `gw pin set/get`) and `keirf/greaseweazle-firmware` (bare-metal firmware, `at32f4` target, SRAM/USB). The flux encoding is the GW USB flux protocol; `.scp` is a *disk* container we deliberately do not reuse for tape (§7.1, §7.4).

---

## 11. Glossary

- **(FSD, FTK, FSC)** — flexible-disk side / track / sector; the abused MFM-ID fields (C/H/R) that encode a sector's tape coordinate.
- **LSN / SEG / TPT / TPS** — logical sector number / logical segment / tape track / segment-relative-to-track; the linear coordinates the disk-style IDs resolve to (§7.3 algebra).
- **Serpentine** — the track layout: even tracks written forward, odd tracks reverse, so the head sweeps back and forth across the medium.
- **Segment** — 32 sectors (29 data + 3 ECC); the RS unit, opened on tape by a `C2/FC` index address mark.
- **BSM** — bad-sector map, in the header segment.
- **Lease** — exclusive right to drive the bus; held by one engine, granted by the arbiter.
- **RawFluxCapture** — the IO layer's output artifact: GW flux bytes + tape metadata header + in-stream `END`.
- **Quiesce funnel** — the single arbiter release path guaranteeing safe teardown.
- **Real-time boundary** — the USB line; below it is hard-real-time and bus-owning, above it is leisurely and semantic.
- **Verbatim passthrough** — firmware emits command numbers as pulse counts without knowing their meaning.
- **Erasure decoding** — RS correction using known bad-sector positions (from CRC), recovering up to 3 sectors/segment.

---

## 12. Toolchain & build

The project is **two artifacts plus a generated contract between them** — firmware (C), host (Python), and a `protocol` definition that generates code for both ends. Each half uses the toolchain native to it; the firmware half deliberately follows Greaseweazle's so the board stays a first-class GW target rather than a fork to maintain in isolation.

### 12.1 Firmware — extend GW's build, don't reinvent it

GW firmware builds with the **ARM GNU toolchain via Make**: `gcc-arm-none-eabi`, plus `srecord`, `stm32flash`, `zip`, and a small set of Python packages (`bitarray crcmod pyserial requests`). `make dist` produces `out/<mcu>/<level>/tapewyrm/target.hex` for `<mcu> ∈ {at32f4, stm32f1, stm32f7}` and `<level> ∈ {debug, prod}`.

**The v4.1's AT32F403 is the `at32f4` target.** Our QIC sources (verbs engine, arbiter, free-running flux mode, marker injector) are added into GW's tree and built as the `at32f4` target. Build `prod` for releases; build `debug` during bring-up — it enables **3 Mbaud serial logging**, which is what you want while debugging the report-bit loop and the arbiter lease machine.

- **Pin the toolchain version.** The `gcc-arm-none-eabi` version affects code generation and therefore instruction timing — and this firmware is timing-sensitive (QIC pulse cadence, report-bit windows, flux capture). Pin it; reproduce via container or Nix (§12.6).

### 12.2 Host — modern Python, `uv`-centric

PEP 621 `pyproject.toml`, with **uv** for environment, lockfile, and task running (fast, single tool, reproducible lock). Gate: **ruff** (lint + format in one), **mypy** (the design is heavily typed — this pays off), **pytest** + pytest-cov against the hardware-free fixtures (§6A.10). CLI entry via `[project.scripts] tw = "tapewyrm.cli:cli"` (plus a `tapewyrm` long alias). Core deps: `click`, `pyserial`. (`greaseweazle` for flux-primitive reuse and `numpy` for an optional accel path are *not* hard deps — the codec is pure stdlib so it stays installable and fixture-testable without them; install `greaseweazle` manually when wiring hardware. Poetry/PDM are acceptable alternatives; uv is the recommendation.)

### 12.3 Flashing & recovery — three tiers

The hardware DFU header (§3) makes this comfortably robust: there is always a probe-less way back, even from a bad flash.

All three are driven by **`tw`** (or, for the debug tier, a probe) — never by the `gw` tool.

| Tier | Mechanism | Driven by | Use it for | Needs |
|---|---|---|---|---|
| Routine | GW-compatible application bootloader, over USB | **`tw flash`** | normal firmware updates | nothing extra |
| **Recovery / un-brick** | **Hardware DFU header → AT32 built-in ROM bootloader** | **`tw dfu`** (wraps `dfu-util`) | flashing when the app bootloader is broken/half-flashed; first-flash | `dfu-util` (or Artery ISP/AT-Link if the AT32 ROM-DFU descriptor doesn't enumerate cleanly under stock `dfu-util`) |
| Debug | SWD via ST-Link / Black Magic Probe + OpenOCD (`flash`/`ocd` make targets) | a debug probe | live debugging, breakpoints, single-stepping | a debug probe |

The middle tier is the important one for this project: iterating on the arbiter and timing code risks bricking the application, and the DFU header guarantees an application-independent recovery path that needs no debug probe. Treat `tw dfu` (→ `dfu-util` / Artery's tool) as the always-works fallback and `tw flash` (the GW-compatible app-bootloader protocol, spoken by `tw` itself) as the convenience path. `tw` carries its own flashing client so it never shells out to `gw update`; the protocol staying wire-compatible means stock `gw update` *also* works on the same board, but Tapewyrm doesn't depend on it.

> **Open item:** confirm whether the AT32F403 ROM DFU enumerates under stock `dfu-util` or requires Artery's ISP/AT-Link tooling — settle this early, since it's the recovery path everything else leans on.

### 12.4 Shared protocol contract — codegen, not discipline

The design depends on "one opcode/command table, both ends, never drifts" (the USB transaction opcodes **and** the flux marker opcodes). Make that a build artifact:

- **One source of truth** in `protocol/` (a small YAML/TOML, or a single annotated Python module) defining every opcode/marker, its fields, and the protocol version.
- A **generator** emits both `firmware/.../protocol.h` (C) and `host/tapewyrm/link/protocol.py` (Python) from it.
- A **CI job** regenerates and fails on `git diff` — this *mechanically* enforces the no-drift invariant from §5.4 / §6A.2 rather than relying on a human to keep two files aligned.

The protocol version generated here feeds the device capability gate in §6A.2.

### 12.5 Repo layout (monorepo)

```
tapewyrm/
  firmware/        # hard fork of GW firmware (vendored) + Tapewyrm QIC sources
    vendor-seam/   # the few GW primitives kept pristine for cherry-picking upstream
  host/            # the uv Python project (tapewyrm)
  protocol/        # source-of-truth opcode/marker defs + generator
  docs/            # this document and design notes
  justfile         # task runner (below)
  .github/workflows/
```

**Fork posture, stated plainly (see §1).** The firmware is a **hard fork** — GW's tree is **vendored**, not submoduled-to-upstream, because the arbiter / verbs / capture changes are structural and the hardware is fixed at v4.1, so tracking upstream buys nothing. The one discipline retained: keep the genuinely-reused GW primitives (flux-capture timing, USB, **bootloader/update protocol**) behind a clean seam so upstream fixes to them can still be cherry-picked by hand, and so `tw flash` (and, still, stock `gw update`) stays a valid flash path. On the host there is effectively **nothing to fork** — GW's tooling is disk-image-centric and does not survive; depend on (or vendor) only the flux-codec / PLL / MFM primitives and write the rest as Tapewyrm. If those primitives need patching, vendor that slice rather than carrying a fork of the whole GW host package.

**The bootloader is the neutral substrate — keep the board a dual citizen.** Leaving GW's bootloader and update protocol untouched means the *same board* flashes a stock Greaseweazle image **or** a Tapewyrm image with one flash each (`tw flash` for Tapewyrm; stock `gw update` still works too, since the protocol is wire-compatible); the application that lands decides what the board is that session. Worth protecting for three reasons beyond tidiness:
1. **Instant revert to stock** for bench debugging — rule out your own firmware by flashing GW, confirm the drive/cabling/hardware behaves, flash back. No DFU jumper, no ArteryISP, no un-brick ritual.
2. **A/B against a known-good flux engine** — stock GW reading a *real floppy* is the reference for whether a capture fault is yours or the silicon's: same board, same USB, same flux primitives, firmware known-good. If GW reads a floppy clean and Tapewyrm's capture is garbage, the bug is yours.
3. **The hardware keeps its resale/reuse value** — it is still a Greaseweazle when you are done, not a single-purpose brick.

The protecting invariant is narrow: **do not touch the bootloader flash region, the application entry vector, the DFU strap, or the USB VID/PID + update-mode commands/protocol** — keep all of that wire-compatible with the GW bootloader (so both `tw flash` and stock `gw update` drive it; detection keys off the VID/PID). The human-readable USB *product string* is rebranded to `Tapewyrm` and the boot banner says Tapewyrm (both cosmetic — they don't affect wire-compat); the update-mode bootloader itself stays the GW substrate. Diverge as hard as you like *above* the application entry point; the boot/update layer is the one place the fork stays faithful.

### 12.6 Task runner — `justfile` skeleton

A `just` recipe set coordinates the heterogeneous build. The Python helper scripts
carry **PEP 723 inline metadata** (`# /// script … # ///`) declaring their deps, and
are run with **`uv run`**, so e.g. `crcmod` (needed for the `.upd` CRCs) is fetched
automatically — no manual venv, no system `srecord`/`crcmod`/`zip`.

```just
# regenerate protocol.h + protocol.py from the single source of truth
gen:
    uv run protocol/generate.py

# build the WHOLE project as one package (host wheel + at32f4 firmware) -> dist/
package:
    uv run tools/package.py

# build just the firmware images -> dist/ (portable: pure-Python HEX merge, no srecord)
fw mcus="at32f4":
    uv run tools/package.py --skip-host --mcus {{mcus}}

# full firmware release: all MCUs + a combined .upd update file -> dist/ (no host wheel)
fw-dist:
    uv run tools/package.py --dist --skip-host

# convenience flash via the GW-compatible application bootloader (tw owns this, not gw)
flash image="firmware/out/at32f4/prod/tapewyrm/target.bin":
    cd host && uv run tw flash ../{{image}}

# recovery flash via the hardware DFU header + ROM bootloader (tw -> dfu-util)
dfu bin="firmware/out/at32f4/prod/tapewyrm/target.bin":
    cd host && uv run tw dfu ../{{bin}}

# remove build, package, and cache artifacts (keeps the uv venv)
clean:
    uv run tools/clean.py

# host: sync, lint, typecheck, test (no hardware)
host:
    cd host && uv sync --extra dev
    cd host && uv run ruff check . && uv run ruff format --check .
    cd host && uv run mypy tapewyrm && uv run pytest

# everything CI runs
ci: gen host
    git diff --exit-code   # protocol drift check
```

`tools/package.py --dist` reimplements GW's `make dist` portably: it builds every
MCU, merges bootloader+app with the pure-Python `tools/ihex.py`, and writes a
combined `.upd` via a faithful port of `firmware/scripts/mk_update.py` (validated
byte-for-byte by GW's own `mk_update.py verify`).

### 12.7 CI matrix (GitHub Actions)

Mirror GW's own workflow shape:

- **firmware** — pinned toolchain container; build `at32f4` `prod` + `debug`; upload `target.hex` artifacts.
- **host** — `uv sync` → ruff → mypy → pytest against fixtures. No hardware.
- **contract** — run `protocol/generate.py`; `git diff --exit-code` to fail on drift.
- **hardware-in-the-loop** — manual / self-hosted-runner only (probe + a known tape); never gates PRs.

### 12.8 Reproducibility tiers

1. **Minimum:** pin `gcc-arm-none-eabi`; commit the uv lockfile.
2. **Better:** a Docker image pinning the firmware toolchain, used locally and in CI.
3. **Gold:** a single **Nix flake** so `nix develop` provides the firmware toolchain *and* the Python env identically on every machine. Worth the learning-curve cost here precisely because the firmware is timing-sensitive and reproducible codegen matters.

---

## 13. Implementation reference (build-from-this)

Concrete data and algorithms so the modules in §5/§6/§6A can be generated directly. Where a value is grounded in a standard it is stated as fact; the few genuine unknowns are called out in §13.6.

### 13.1 QIC-117 command table (complete, from Rev J Tables 2a/2b/2c/2d)

`Arg` is in **N+2 pulse form** unless noted. `Kind`: RPT report · MOD mode · MOT motion · STREAM streaming-motion · SEL select · CFG config · RST reset · INT internal. Flags: **n** non-interruptible · **h** high-speed. Timeouts in seconds unless stated; motion timeouts are maxima over tape length/speed.

| Code | Command | Kind | Arg(s) | Ready req'd | Flags | Timeout |
|---|---|---|---|---|---|---|
| 1 | Soft Reset | RST | — | — | — | 1 ack / 460 ready |
| 2 | Report Next Bit | INT | — | — | — | 900 µs |
| 3 | Pause | MOT | — | yes | n | 16 |
| 4 | Micro Step Pause | MOT | — | yes | n | 16 |
| 5 | Alternate Command Time-out | CFG | — | — | — | 0 |
| 6 | Report Drive Status | RPT | — | — | — | 2.5 ms ack |
| 7 | Report Error Code | RPT | — | — | — | 2.5 ms ack |
| 8 | Report Drive Configuration | RPT | — | — | — | 2.5 ms ack |
| 9 | Report ROM Version | RPT | — | — | — | 2.5 ms ack |
| 10 | **Logical Forward** | STREAM | — | yes | — | tape-len/speed (≤~650) |
| 11 | Physical Reverse | MOT | — | yes | h | tape-len/speed |
| 12 | Physical Forward | MOT | — | yes | h | tape-len/speed |
| 13 | Seek Head to Track | MOT | `Track+2` | yes | — | 15 |
| 14 | Seek Load Point | MOT | — | cartridge | n | ~670 |
| 15 | Enter Format Mode | MOD | — | — | — | 0 |
| 16 | Write Reference Burst | MOT | — | format | n | 940 |
| 17 | Enter Verify Mode | MOD | — | yes | — | 0 |
| 18 | Stop Tape | MOT | — | — | n | 8 |
| 21 | Micro Step Head Up | MOT | — | — | n | 200 ms |
| 22 | Micro Step Head Down | MOT | — | — | n | 200 ms |
| 23 | Soft Select | SEL | **20 literal pulses** | — | — | 0 |
| 24 | Soft Deselect | SEL | — | — | — | 0 |
| 25 | Skip N Segs Reverse | MOT | `(N&15)+2, (N≫4)+2` | yes | n | tape-len/speed |
| 26 | Skip N Segs Forward | MOT | `(N&15)+2, (N≫4)+2` | yes | n | tape-len/speed |
| 27 | Select Rate or Format | CFG | `N+2` (rate or format) | — | — | 0 |
| 28 | Enter Diag Mode 1 | MOD | `28` (sent twice) | — | — | — |
| 29 | Enter Diag Mode 2 | MOD | `29` (sent twice) | — | — | — |
| 30 | Enter Primary Mode | MOD | — | — | — | 0 |
| 32 | Report Vendor ID | RPT | — | — | — | 2.5 ms ack |
| 33 | Report Tape Status | RPT | — | — | — | 2.5 ms ack |
| 34 | Skip N Ext Reverse | MOT | 3 nibbles, each `+2` | yes | n | tape-len/speed |
| 35 | Skip N Ext Forward | MOT | 3 nibbles, each `+2` | yes | n | tape-len/speed |
| 36 | Calibrate Tape Length | MOT | — | yes | n | ~1300 |
| 37 | Report Format Segments | RPT | — | — | — | 2.5 ms ack |
| 38 | Set N Format Segments | CFG | 3 nibbles, each `+2` | — | — | 0 |
| 46 | Phantom Select | SEL | `Unit+2` | — | — | 0 |
| 47 | Phantom Deselect | SEL | — | — | — | 0 |

(19–20, 39 reserved; 31, 40–45 vendor-unique.) Codes >32 unsupported by a drive are ignored; codes <32 that are undefined raise "undefined command." A command pulse-train >32 pulses is ignored.

**Argument encoding.** `Rate` (cmd 27): 0=4 Mbps-or-250 kbps, 1=2 Mbps, 2=500 kbps, 3=1 Mbps. `Format` (cmd 27): `(tape_format×4)+increment`, tape_format 1=QIC-40 2=QIC-80 3=QIC-3020 4=QIC-3010, increment 1=standard 3=wide(8 mm).

**Report payloads (data bits between ACK and Final, LSB-first).**
- `6` Drive Status (8b): 0 ready · 1 error · 2 cartridge-present · 3 write-protect · 4 new-cartridge · 5 referenced · 6 at-BOT · 7 at-EOT. (bits 1,5,6,7 valid only when ready.)
- `7` Error Code (16b): bits 0–7 error code, 8–15 associated command.
- `8` Drive Config (8b): bits 3–4 rate (00=4M/250k, 01=2M, 10=500k, 11=1M) · 6 extra-length · 7 QIC-80-mode.
- `9` ROM Version (8b): 0–6 version, 7 beta.
- `32` Vendor ID (16b): 0–5 model, 6–15 make.
- `33` Tape Status (8b): 0–3 format (0 unknown,1 QIC-40,2 QIC-80,3 QIC-3020,4 QIC-3010) · 4–6 type · 7 wide.
- `37` Format Segments (16b): segments per tape track.

### 13.2 Reed–Solomon erasure decode over GF(256) (from QIC-80-MC Rev N §6.2)

The only nontrivial algorithm in the codec; everything else is parsing. Erasure-only (positions known from CRC), redundancy 3.

**Field.** Primitive polynomial `f(x)=x⁸+x⁷+x²+x+1` → reduction modulus `0x187`. Primitive element `α=0x02`. Build `exp[0..510]`, `log[0..255]`:
```
v=1; for i in 0..254: exp[i]=v; log[v]=i; v<<=1; if v&0x100: v^=0x187
exp[i+255]=exp[i] for i in 0..255          # doubled table avoids mod in mul
assert exp[105]==0xC0                       # r¹⁰⁵; fails ⇒ wrong field/primitive
gmul(a,b)= 0 if a==0 or b==0 else exp[log[a]+log[b]]
gdiv(a,b)=       exp[log[a]-log[b]+255]     # b≠0
```
**Generator** `g(x)=x³+0xC0·x²+0xC0·x+1`. Its 3 roots are the syndrome evaluation points; find them once by search: `roots=[e for e in 1..255 if g_eval(e)==0]` (degree 3 ⇒ exactly 3).

**Decode a segment** (1024 columns share one erasure set `E` = bad-sector positions, `|E|≤3`; with excluded sectors, positions index the **non-excluded** codeword of length `N+1 = 32−bad_blocks`, parity in the last 3):
```
if len(E) > 3: segment uncorrectable          # flag, keep partial
for each column c of 1024:
    R = received symbols (erased positions set to 0)
    S[j] = Σ_i R[i]·exp[(roots_log[j])·pos[i]]   for j in 0..2   # syndromes = R(root_j)
    # solve  Σ_{e∈E} (root_j)^{pos(e)} · X_e = S[j]   for the |E| unknowns X_e
    solve small linear system over GF(256) (Gaussian elim or Cramer)
    write X_e back into the erased positions
```
With ≤3 syndromes and ≤3 unknowns the system is square/over-determined and exact. (For non-erasure CRC-failures, treat them as additional unknowns only within the redundancy budget — see Rev N §6.2 correction table.)

**Test vector** (Rev N Fig 6.3, last column): data symbols rows 0–28 = `01 02 03 … 1D` (value = row+1); parity rows 29,30,31 = `5D FF A3`. Unit test: erase any ≤3 of the 32 positions, decode, expect exact restoration. Five more columns in Fig 6.3 give additional vectors; cross-check against ftape `ecc.c`.

### 13.3 USB transaction wire protocol (Tapewyrm device protocol)

Layered on GW's USB CDC-ACM transport. Host→device **request frames** `{opcode:u8, len:u16, payload}`; device→host **response frames** likewise; the capture path is a continuous stream. Opcodes/marker codes live in the generated `protocol.h`/`protocol.py` (§12.4); byte-level framing aligns with GW's existing command framing once §13.6/§9.1 is settled.

| Transaction | Request payload | Response | Notes |
|---|---|---|---|
| `INFO` | — | `DeviceInfo{model, mcu, fw, sram, caps, proto_ver}` | host capability gate (QIC bit + version) |
| `SET_TIMING` | `TimingParams` | ok | idle-only |
| `SELECT` | `SelectHint` | ok | idle-only; sticky-select optional |
| `COMMAND_TXN` | `{cmd_n:u8, report_bits:u8}` | `{ack:bit, bits:u16, final:bit}` | verbs engine; ACK/Final checked device-side, raised on failure |
| `WAIT_READY` | `{timeout_s:u16}` | `{status:u8}` | poll ready line |
| `CAPTURE` | `{motion_n:u8, stop:StopCond}` | **stream** | arbiter holds lease whole session; stream = GW flux bytes + markers |
| `ABORT`/`STOP` | — (**out-of-band control**, not queued) | — | valid during CAPTURE; routes through Quiesce |

**Capture stream** = verbatim GW flux bytes interleaved with opcode-escape **markers** (§7.2): `SESSION_START{rate, clock, TPT, direction, pass_id, utc}` · `SEGMENT{ticks, index}` (per hardware INDEX edge) · `EVENT{code}` · `END{reason, flux_count, byte_count, checksum}`. Backpressure: sustained overflow → `EVENT{overflow}` + clean abort (never silently drop). USB suspend/disconnect → device-side dead-man → Quiesce stop.

### 13.4 Module manifest (generate these)

**Firmware** — added *above the application entry point* in the vendored GW tree; flux timer/DMA, USB CDC, bootloader, AT32 clock/GPIO, and linker map are **reused untouched** (§12.5).

| File | Responsibility | Key entry points |
|---|---|---|
| `qic/arbiter.{c,h}` | lease state machine (§5.2); owns drive-select; Quiesce funnel; watchdog + USB-loss dead-man | `arb_grant`, `arb_quiesce`, `arb_on_usb_loss` |
| `qic/verbs.{c,h}` | pulse emit (cadence), N+2 arg emit, report clock (ACK/bits/Final), wait-ready (§5.3) | `qic_pulses`, `qic_arg`, `qic_report`, `qic_wait_ready` |
| `qic/flux_capture.{c,h}` | free-running capture (extends GW flux read): arm/disarm, RDATA→encode→ring→USB, SEGMENT on INDEX, overflow→EVENT+abort (§5.4) | `cap_arm`, `cap_disarm`, `cap_on_index` |
| `qic/markers.{c,h}` | marker injection on opcode channel; END accounting (counts+checksum) | `mk_session_start`, `mk_segment`, `mk_event`, `mk_end` |
| `qic/transactions.{c,h}` | USB transaction dispatch (§5.1/§13.3) hooked into GW's handler; `INFO` capability flag | `txn_dispatch` |
| `protocol.h` | **generated** (§12.4): opcodes, marker codes, version | — |

**Host** (`tapewyrm/`) — all pure above the USB line; the `codec/*` tree is hardware-free and fixture-tested.

| File | Responsibility |
|---|---|
| `link/transport.py` | pyserial wrapper + frame codec |
| `link/protocol.py` | **generated**: opcode/marker enums, version |
| `link/device.py` | `DeviceLink` typed RPC (§13.3) + capability gate |
| `qic117/commands.py` | command table (§13.1) as `Cmd` records; N+2 arg encoder |
| `qic117/drive.py` | `Qic117Drive` (§6A.3): command/report/status/config/tape_status/wake/reset |
| `qic117/status.py` | `DriveStatus`/`ErrorCode`/`DriveConfig`/`TapeStatus` bit-decoders (§13.1 payloads) |
| `qic117/profile.py` | `DriveProfile` + TOML loader |
| `tape/geometry.py` | `Geometry`, the §7.3 coordinate algebra, CCS-level + 100/207 fallback |
| `tape/transport.py` | `TapeTransport`: identify/load_point/seek/capture_pass/walk_all; serpentine; no-status-after-LF; broken-tape guard |
| `rawflux/container.py` | `RawFluxCapture` linear track-stack (§7.1): header, track runs, markers, save/load, END verify |
| `codec/flux.py` | GW flux bytes → intervals (reuse GW primitive) |
| `codec/mfm.py` | intervals → IBM MFM sectors (reuse GW PLL/MFM); parse `C2/FC`, `A1A1A1 FE` IDs, `A1A1A1 FB/F8` data, CCITT CRC |
| `codec/place.py` | sector `(FSD,FTK,FSC)` → `(SEG,TPT,TPS,sec)` (§7.3); bin into `Segment`s |
| `codec/rs.py` | GF(256) RS erasure decode (§13.2) |
| `codec/segment.py` | `Segment` assembly; excluded-sector repack (`N=31−bad`); CRC→erasure mask |
| `codec/volume.py` | header segment (format params + BSM), volume table (`VTBL`/`XTBL`/`UTID`/`EXVT`); per-file-set logical-segment-ordered byte stream |
| `codec/qic113.py` | QIC-113 file-set parse (§7.5): Basic-DOS + Extended-OS; tree + files; signature resync; decompress hook |
| `codec/merge.py` | multi-pass union of good sectors before RS |
| `report.py` | `RecoveryReport` (per-segment/track quality) |
| `cli.py` | Click CLI: `probe` / `capture` / `decode` / `recover` / `replay` |
| `profiles/*.toml` | per-drive `DriveProfile` data |

### 13.5 Decode stack (one ladder, with the module at each rung)

```
GW flux bytes        rawflux.container → codec.flux       (reuse GW interval decode)
  → intervals        codec.flux
  → MFM sectors      codec.mfm          C2/FC index · A1A1A1 FE id (FTK,FSD,FSC,03) · A1A1A1 FB/F8 data · CCITT-CRC
  → placed sectors   codec.place        (FSD,FTK,FSC) → (SEG,TPT,TPS,sec) via §7.3
  → segments         codec.segment      32-sector bins; CRC → erasure mask; excluded-sector repack
  → corrected data   codec.rs           GF(256) erasure decode (§13.2)
  → logical volume   codec.volume       header/BSM + volume table → per-file-set byte stream
  → files            codec.qic113       directory tree + file bytes (§7.5)  [+ decompress]
```
Multi-pass union (`codec.merge`) sits between *placed sectors* and *segments*; the whole ladder is offline and pure.

### 13.6 Oneshot scope — what generates cleanly vs what needs the bench

**Generates directly from this document (no hardware):**
- The **entire `codec/*` tree** — §7.3/§7.5/§13.2/§13.5 are complete; test against the §13.2 RS vector, synthesized `RawFluxCapture` fixtures, and ftape cross-checks.
- **Host control layers** (`link/`, `qic117/`, `tape/`, `rawflux/`, `report.py`, `cli.py`) — §6A + §13.1 + §13.3 + §13.4.
- **Firmware** — Greaseweazle v1.6 is **vendored complete in-tree** (`firmware/`, builds with the ARM GNU toolchain) and the QIC layer is grafted onto GW's control loop: `src/qic/qic.c` is `#include`d into `src/floppy.c`, adding `CMD_QIC_*` (= the generated `TW_TXN_*`) cases to `process_command` and reusing GW's flux-read engine for free-running capture (§5, §13.3).
- The **protocol codegen** and `justfile`/CI (§12).

**Needs a bench / a real drive before it runs (the genuine seams, §9):**
1. **Host-side GW flux opcode reconciliation** — the firmware now reuses GW's real flux encoder + `0xFF` opcode escape (tape markers ride codes `0xF0–0xF4`); what remains is teaching the host `codec.flux`/`rawflux` to skip GW's *own* `FLUXOP_*` opcodes when decoding a real capture. The firmware side is done.
2. ~~GW firmware integration points~~ — **resolved by vendoring** (firmware now builds in-tree): QIC verbs are grafted into GW's `process_command`, capture reuses `floppy_read`/`rdata_encode_flux` free-running, and host-stop maps onto GW's `BAUD_CLEAR_COMMS` out-of-band path. What remains is hardware validation of the pulse/flux timing on a real drive.
3. **The target `DriveProfile`** — wake timing, CCS level, quirks; characterize via `gw pin` + scope.
4. **v4.1 schematic confirm** + **34-pin open-collector** assumption.
5. **STAC/DCLZ decompressor** for compressed volumes.

The host codec + control stack are implemented and the Greaseweazle firmware is **vendored complete in-tree** with the QIC graft building; the items above are where progress now needs a real drive (timing / profile / schematic validation) or the one host-side decode tweak. They are marked `TODO(bench)` at their sites.
