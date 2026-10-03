# Tapewyrm — QIC-80 tape recovery over Greaseweazle v4.1

> **Status:** design record, brought up to date with the as-built tree (October 2026). The host is four Python packages under `packages/` (§6A.1) and has dumped and fully recovered real QIC-80 backup tapes end to end (flux → TWRF → TWTI → TWVL → tar); the Greaseweazle firmware is **vendored complete in-tree** (`firmware/`, Unlicense) with the QIC layer grafted onto GW's control loop and flux recording. Where the build departed from a decision recorded here, the decision is kept and marked **superseded**, with what replaced it and why — the reasoning is part of the record.
> **License:** the whole project is **public domain under the Unlicense** (see `UNLICENSE`); the vendored Greaseweazle firmware is itself Unlicense.
> **Purpose:** a single, self-contained document to document the full reasoning, decisions, and open questions can be reconstructed without re-deriving them. Reads top-to-bottom; later sections assume the vocabulary of earlier ones.
> **Name:** *Tapewyrm* — the tape is a long serpent of a medium, written in a back-and-forth serpentine (§7.4); a wyrm rather than a weasel.

---

## 0. How to use this document

This captures *why* as much as *what*. Where a decision had a real alternative, the alternative and the reason for rejecting it are recorded inline. Anything not yet verified or designed is collected in **§9 Open questions** — treat those as the work queue, not as settled fact. Exact register-level constants (flux opcode bytes, QIC timing envelope) are deliberately left as "confirm from source" rather than asserted from memory.

> **Grounding.** §2.1/§5.3 (command channel) are grounded against **QIC-117 Rev J** and §2.2/§7 (recording format) against **QIC-80-MC Rev N**, both read in full; claims traceable to those specs are stated as fact. The file-set layer (§7.5) is grounded against **QIC-113 Rev G** and its compression against **QIC-122 Rev B**; the higher-density formats against **QIC-40-MC Rev M** and **QIC-3010-MC / QIC-3020-MC Rev H** (geometry, §3.4 rates, §5.4.1 segment formulas, format codes). What still rests on bench observation rather than a spec — Rev K's fixed formats, vendor volume-table layouts — is flagged where it is used and in §9–§10.
>
> **Companion documents.** The on-disk formats each have their own specification in `docs/spec/`: [`twrf.md`](spec/twrf.md) (TWS-1, flux captures), [`twti.md`](spec/twti.md) (TWS-2, tape images and TWTZ), [`twvl.md`](spec/twvl.md) (TWS-3, extracted volumes). Those own the byte layouts; this document keeps the *why* (§7.1, §7.6, §7.7). `STYLE.md` at the repo root owns code conventions and the package dependency rules.

---

## 1. Goal & scope

**Goal.** Read and recover data from QIC-80 *floppy-interface* tape cartridges (and close relatives: QIC-40, QIC-3010, QIC-3020/Travan) using a Greaseweazle v4.1 as the bus interface, capturing raw flux and decoding **offline**.

**The two hard constraints (everything else is new):**

| Borrowed (then owned) | Built new |
|---|---|
| Greaseweazle **v4.1 hardware** — no board respin | Firmware: QIC command verbs, bus arbiter, free-running flux capture + markers |
| The GW **native flux transfer encoding** (referred to in discussion as *"greasepack"* — see note below) | Host: device-link stubs, QIC-117 drive layer, tape dump, QIC-80 physical decode, QIC layout + backup-format library (`qiclib`) |
| GW flux engine, PLL (vendored into the host as `codec/gwpll.py`), bitcell recovery, **MFM framing** (the timing-critical primitives) | The file formats: **TWRF** flux captures, **TWTI/TWTZ** tape images, **TWVL** volumes (§7.1, §7.6, §7.7) + tape marker opcodes |
| `gw pin set` / `gw pin get` (bench bring-up only), the bootloader / update **protocol** (kept wire-compatible), read/stop control semantics | The entire QIC-117 / QIC-80 / QIC-113 "brain" (host-side), **and the CLIs: `tw` (hardware, flux → image, flashing via `tw flash` / `tw dfu`) and `qicsilver` (image → files, offline); the `gw` executable is never required** |

> **Fork posture — this is a hard fork, not a tracked branch.** Tapewyrm starts from Greaseweazle and diverges; mergeability with upstream is **not** a goal, and on the host side little if any GW *tooling* survives recognizably (GW's host code is built around `.scp`-style disk images and disk verbs — almost none of which maps onto a QIC-117 drive model + tape transport + linear-track-stack codec). What actually carries over is narrow: the flux-capture timing skeleton and USB/bootloader on the firmware, and the flux-codec / PLL / MFM primitives on the host. The discipline that *replaces* upstream-tracking is a **clean internal seam** isolating exactly those primitives, so (a) future upstream fixes to the flux engine can still be cherry-picked by hand, and (b) the bootloader/update protocol stays wire-compatible so it can be driven by `tw`'s own flashing client (and, incidentally, by stock `gw update` too — but `tw` never *requires* `gw`). Everything else is owned outright. (Mechanics in §12.5.)

> **Terminology note.** "Greasepack" is not an official Greaseweazle term and could not be found in GW docs or firmware. What is meant is unambiguous: the GW on-wire flux byte stream (variable-length inter-transition intervals at a known sample clock, plus an opcode/escape channel). This document calls it the **GW flux encoding**.

**Tooling posture — `tw` owns the hardware, `qicsilver` owns the files.** *Original decision (superseded in part):* all host functionality in a single executable, **`tw`**, with verbs `probe` / `capture` / `decode` / `recover` / `replay` and firmware flashing. *As built:* everything that touches the board — drive control, dumping, firmware flashing — and the flux → image step are still one executable, **`tw`** (`info`, `drive …`, `dump`, `convert`, `flash`, `dfu`; §6A.7). Everything after the tape image is the separate, hardware-free **`qicsilver`** (`identify`, `extract`, `tar`). The split follows the file formats: a TWTI image is the complete hand-off, so a user recovering files from someone else's image needs no USB stack, no drive profiles and no firmware tooling, and the backup-format code can change without touching the hardware package (§6A.1). The rest of the original posture stands: Tapewyrm deliberately does **not** reuse or require Greaseweazle's `gw` executable. The bootloader/update protocol is kept wire-compatible (§12.5) so the board stays a dual citizen — flashable by `tw`'s own client *and*, incidentally, by stock `gw update` — but a Tapewyrm install needs nothing from GW. (`gw pin` remains a handy bench aid during bring-up before firmware exists; that is not a runtime dependency.)

**Non-goals (for now):**
- Writing or formatting tapes (needs reference-burst/servo handling — out of scope; read-only is far simpler and is all recovery requires). *(But see Sometime goals — the Linux linear-device aim eventually pulls write into scope.)*
- Drive modes faster than the standards' rates (e.g. 2 Mbit/s) — not targeted; whether the USB link could even carry one is open (§3.1). *(Corrected: an earlier draft listed "QIC-3020 @ 2 Mbit/s"; the QIC-3020 standard rate is 1 Mbit/s, §2.2, and QIC-3010/3020 headers are now read — §7.3, §7.8.)*
- Recovering **non-QIC-113 proprietary** archive containers (e.g. CP Backup's CPB, some Norton/Central Point layouts that predate or ignore QIC-113). Standards-compliant **QIC-113 file sets are in scope** (§7.5) — decoding QIC-80 + QIC-113 yields the directory tree and files directly; only genuinely non-standard app containers need a separate, app-specific parser on top.
- **DCLZ / ALDC decompression** (QIC-130 / QIC-154). *Done since:* the **QIC-122 (Stac LZS)** codec that QIC-80 backups actually use is implemented (`qiclib.qic122`) and decompresses real volumes (§7.5); the other two codecs remain a drop-in nobody has needed yet.

**Sometime goals (aspirational, not scheduled):**
- **Linux linear-tape device → `tar`.** Eventually present the drive to Linux as a *linear device* with streaming/sequential semantics (in the spirit of a SCSI tape `/dev/st0`), so standard tools — notably **`tar`** — can read and *write* QIC cartridges directly (`tar tf`, `tar xf`, `tar cf`). This pulls two things into scope that are non-goals today: (1) the **write/format path** (reference-burst/servo handling — the hard half, and it gates writing), and (2) a **device shim** that maps the serpentine segment-stack to a flat byte stream with on-the-fly QIC-80/QIC-113 (de)framing — realizable either as a kernel character device or, more cheaply first, in userspace via FUSE/NBD over the `tw` host. Read-as-linear-device sits naturally on top of the existing offline decode stack (§6.4) — offline, `qicsilver tar` already turns a recovered volume into a POSIX tar; write is the bigger lift. Sequenced after the read-recovery target is solid.

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

Once the drive is in motion (Logical Forward, in Primary or Verify mode), data flows as ordinary **MFM at 500 kbit/s** (QIC-80) on RDATA/WDATA — the same regime as HD floppy. The standards' intended rates (each spec's §3.4) are 250 kbit/s for QIC-40, 500 kbit/s for QIC-80 and QIC-3010, and 1 Mbit/s for QIC-3020; every one adds that "other speeds and compatible transfer rates are possible", and drives use that (on the bench a Colorado 1400 reported 1 Mbit/s in its configuration while the QIC-80 tape in it was recorded at 500 kbit/s — see `profiles/drive/colorado.1400.toml` and `tw drive rate`). So a capture never assumes a rate: it records the one Report Drive Configuration gives (§7.1), and `TapeFormat.rate_kbps` (the per-standard table) is only a fallback. *(An earlier draft gave 1 / 2 Mbit/s for QIC-3010 / 3020; those are drive modes, not the standards' rates.)* Grounded against **QIC-80-MC Rev N**:

- **Each segment is a standard IBM/MFM "track" image.** It opens with an **index address mark** (`C2 C2 C2 FC`, missing-clock C2) — the very mark a floppy carries once per revolution, here once per **segment** — then 32 repetitions of a **sector ID** (`A1 A1 A1 FE` + `FTK, FSD, FSC, 03` + CRC) and a **data block** (`A1 A1 A1 FB` + 1024 bytes + CRC; `F8` in place of `FB` = **deleted-data** = bad block). GW's existing IBM-MFM decoder recovers these directly; the only QIC twist is that the ID's cylinder/head/record/size = **(FTK, FSD, FSC, 03)**. CRC is CCITT **x¹⁶+x¹²+x⁵+1**, register preset all-ones.
- **The hardware INDEX pulse corresponds to that mark** — the drive asserts INDEX at each segment start, within 8 flux transitions after ≥50% of the erased inter-segment gap. So segment boundaries are signalled **twice, independently**: the in-stream `C2/FC` mark and the hardware INDEX edge.
- **Sector identity → tape coordinate (exact).** Each sector's `(FSD, FTK, FSC)` maps deterministically to a logical sector number, logical segment, tape track, and segment-relative-to-track:
  - `LSN = 32640·FSD + 128·FTK + (FSC−1)`
  - `SEG = 1020·FSD + 4·FTK + ⌊(FSC−1)/32⌋`   (inverse: `FSD = SEG/1020`, `FTK = (SEG mod 1020)/4`, `FSC = (SEG mod 4)·32 + 1`)
  - `TPT = ⌊SEG / segments_per_track⌋`,  `TPS = SEG mod segments_per_track`
  - First sector of tape track 0 is `(0,0,1)`. Ranges: `1≤FSC≤128`, `0≤FTK≤254`, `FSD` length-dependent. One "floppy track" (FTK) = 128 sectors = **4 segments**; one "floppy side" (FSD) = 1020 segments. This is the bridge between the FDC's circular-disk addressing and the tape's linear reality — see §7.4.
- **Serpentine geometry:** 28 tracks for QIC-80 on 0.250 in tape (36 on 0.315 in / Travan); QIC-40 has 20; QIC-3010 and QIC-3020 both have 40 (50 on 0.315 in). **Even tracks recorded forward, odd tracks reverse**, referenced to forward/reverse reference bursts at BOT. All tracks hold the same segment count. Example (425 ft): 207 segments/track → 5,796 segments, 185,472 sectors.
- The **first defect-free segment is the header segment** (duplicated in the second); it holds the **format parameter record** (signature `55 AA 55 AA`, format code `04` = variable-length — real tapes also carry Rev K's fixed codes 2/3/5, §7.3 — segments/track, track count, dates, ASCII tape name) and the **bad-sector map**. Segments before/between the two header copies are written as deleted-data. Some sectors are bad *by design* (e.g. QIC-3020 pre-maps zones near BOT/EOT hole imprints) — the BSM distinguishes those.

### 2.3 Why erasure decoding is the whole recovery game

Every sector carries its own CRC, so the decoder knows **which** sectors are bad → it decodes the segment in **erasure mode**. The ECC is a per-**column** Reed–Solomon code of redundancy 3 over GF(256) (rows 0–28 data, 29–31 parity, written row-by-row). It corrects **up to 3 sectors with CRC errors per segment** (or 1 CRC-error + 1 CRC-*failure* — a "failure" being an error the CRC missed, i.e. unknown-location); with erasure positions known from CRC you get the full 3-sector budget, far stronger than blind error correction. Decoder subtlety: **BSM-excluded sectors are not part of the codeword** (Rev N says they are physically skipped on tape; on the bench's fixed-format tape the excluded slot was still recorded with a valid ID, so the decoder drops excluded slots by the map, never by absence), and the 3 parity sectors occupy the **last 3 non-excluded** sectors, so a segment's codeword length is `N = 31 − bad-blocks-in-segment`, not always 32. Sectors are **self-locating** (their ID is their coordinate), so capture order is irrelevant — partial, retried, or out-of-order captures still reassemble.

---

## 3. Hardware substrate: Greaseweazle v4.1 (fixed, no respin)

- MCU: **AT32F403 (Cortex-M4)**, ~144 MHz (some boards report AT32F403A @ higher clock). SRAM extended to **224 kB** on current firmware.
- USB: **link speed is under discussion — see §3.1.** As built, the firmware enumerates **Full-Speed (12 Mbit/s)**, which carries every standard-rate capture so far; the large buffer (64 kB+) is the SRAM flux ring, not a USB feature.
- **Buffered 40 mA outputs** (drives strong drive-side pull-ups cleanly). `gw pin set` / `gw pin get` already exist (use for bring-up/bit-bang before committing firmware). 3 user-definable outputs (pins 2/4/6); pin 34 readable input; external-LED header.
- Power: **5V-only** on-board header; 12V drives use a **separate PSU**; **USB 5V isolation jumper** lets the board run off external power safely.
- Connectivity/protection: USB-C, ESD protection on USB data, over-current protection on USB power.
- **Hardware DFU header:** straps the AT32's built-in ROM bootloader, giving a probe-less, application-independent flash path that survives a broken/half-flashed firmware (the un-brick route). Flashing detail in §12.3.

**Why it suffices for QIC (no board change):** QIC-117 needs a *subset* of the Shugart lines in the *same directions* GW already drives (STEP/DIR/WGATE/MOTOR/DSEL out) and senses (INDEX/TRK0/RDATA in). STEP is the command line; TRK0 is the return; INDEX is the cue line (ready/idle, report bit presented, waiting for argument -- Rev J §1.3) and the segment mark during Logical Forward; RDATA is flux. The data channel is literally what GW does. Net work is firmware verbs + host software + a power/termination cabling setup. 12V via separate PSU + isolation jumper.

> **Verify (cheap):** from the v4.1 design files, confirm TRK0/INDEX land on pollable GPIO/EXTI (not a peripheral-locked pin) and that the STEP output buffer swings the bus at the chosen cadence. Near-certain for a floppy interface; take it from the schematic, not from assumption.

### 3.1 Discussion: USB link speed (open)

> **Status: discussion, not a decision.** Earlier drafts stated the v4.1 link was **High-Speed (480 Mbit/s)** with "enormous headroom". Nothing checked supports that; everything checked says **Full-Speed (12 Mbit/s)**. This subsection replaces the flat claim.

**What the hardware can do.** The board definition (`firmware/boards/greaseweazle_v4_at32f403a.json`) names the MCU as `at32f403acgu7`. The AT32F403A's USB peripheral is a **full-speed device controller (USBFS) with an embedded PHY** and a small dedicated packet-buffer SRAM; it has no high-speed controller and no ULPI interface for an external HS PHY. (From Artery's AT32F403A datasheet as recalled, not re-read for this note; confirm against the datasheet and the v4.1 schematic.) Greaseweazle's own descriptors also report the board's speed at runtime, so a connected board settles it (below).

**What the firmware configures.** `firmware/src/usb/hw_at32f4.c` binds AT32F403/AT32F403A to the `usbd` driver (the DWC-OTG driver is used only for AT32F415). In `firmware/src/usb/hw_usbd_at32f4.c`, `usbd_has_highspeed()` and `usbd_is_highspeed()` both return `FALSE`, and endpoint buffers are fixed at 64 bytes (`USB_FS_MPS`). `firmware/src/usb/core.c` initialises `usb_bulk_mps = USB_FS_MPS` (64), and only the DWC-OTG path ever raises it to `USB_HS_MPS` (512). `firmware/src/usb/cdc_acm.c` reports `gw_info.usb_speed = usb_is_highspeed()`, which the host reads as `DeviceInfo.usb_high_speed` (`link/device.py`) — on this board it is always false. Consistent with this: a command response is one 64-byte packet, and the bench saw a 253-byte response truncated at 64, so every verb response must fit in 64 bytes (including its 2-byte header) or be streamed like READ_FLUX.

**What throughput is needed.** Flux bytes per wall-clock second, from the `bytes` and `seconds` fields of the captures' `dump.jsonl` (Logical Forward, standard rates):

| Rate | Capture sets | Average per set | Highest single track |
|---|---|---|---|
| 500 kbit/s | 4 sets (30 tracks) | 392–396 kB/s | 412 kB/s |
| 1 Mbit/s | 2 sets (35 tracks) | 610–637 kB/s | 676 kB/s |

**Is Full-Speed enough?** For the standard rates, yes, so far. Full-speed bulk tops out at 19 × 64-byte packets per 1 ms frame, about **1.2 MB/s** in theory and less in practice (it depends on the host controller and on other devices sharing the bus). The worst 1 Mbit/s track used about 56% of the theoretical ceiling; no standard-rate capture has ended on overflow. The margin is real but not "enormous". Two things do not fit: a 2 Mbit/s mode would need roughly 1.3 MB/s by scaling, which is over the ceiling, and Physical (high-speed) motion produces flux fast enough to overflow the link (the bench notes in `tape/fluxprobe.py`; the firmware ends that capture on overflow by design, §5.4).

**Open questions.**
1. Confirm Full-Speed on a live board: `tw info` / `DeviceInfo.usb_high_speed`, or the host's USB device tree.
2. Measure the real sustained Full-Speed ceiling on the host controllers we use, rather than relying on the 1.2 MB/s theoretical figure.
3. How much of the margin at 1 Mbit/s goes when the bus is shared (hubs, other devices), and does overflow → clean abort (§5.4) stay the right answer there?
4. Whether anything beyond the standard rates (2 Mbit/s, Physical-motion capture) is worth pursuing given the ceiling — for example by compressing the flux stream on the device. Today the answer is "not targeted" (§1).

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

**As built, the "Format codec" box is three stages with a file between each**, so every stage can be re-run, shared and inspected on its own:

```
tw dump        drive → TWRF captures (one per track pass)               packages/tapewyrm-cli
tw convert     TWRF → MFM sectors (tapewyrm-cli) → TWTI/TWTZ image       + packages/qiclib (layout)
qicsilver      TWTI → identify / extract → TWVL volumes → tar           packages/qiclib + qicsilver
```

The physical layer (flux → PLL → MFM sectors) lives with the hardware in `tapewyrm-cli`; everything from placed sectors upward — geometry, Reed–Solomon, header/BSM/VTBL, QIC-113, QIC-122, cartridge and volume profiles — is the stdlib-only library `qiclib`; the file formats are `tapewyrm-archive`. The dependency rules are in §6A.1 and `STYLE.md` §2.

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

*As built*, these are Greaseweazle command packets grafted onto GW's `process_command()` (§13.3): `COMMAND_TXN`, `WAIT_READY`, `CAPTURE`, `SET_TIMING`, plus `INFO`, `SCOPE` (a bench edge-logger) and `BUILD_INFO`. Select is GW's own `SET_BUS_TYPE` + `SELECT`, and abort is GW's out-of-band clear-comms path rather than a verb of its own.

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

**As built** there is no separate arbiter module (the standalone `qic/arbiter.c` skeleton is retired to `firmware/attic/qic_skeleton/`, §13.4). GW's own floppy state machine *is* the lease: a verb only runs from `ST_command_wait`, a capture holds `ST_read_flux` until it ends, so nothing can interleave. The Quiesce funnel is two functions in `src/qic/qic.c`: `qic_capture_finish()` (every in-band end — normal, byte budget, overflow, no-start — issues **Stop Tape first**, then writes `END`) and `qic_capture_abort_silent()` (the host stopped the stream or the link dropped: still Stop Tape, no `END`, so the host sees a truncated run). The invariant — the device, not the host, stops the tape — holds.

### 5.3 QIC verbs engine (command/status channel)

- **Pulse emit:** *n* STEP pulses at the configured cadence (verbatim — no knowledge of meaning), then hold the terminating gap. Defaults from `set_timing`: ~2.0 ms step interval, gap > 2.9 ms to end the command. **Must keep pulses grouped under the command time-out** (an isolated slow pulse = Soft Reset) and **must not leave TRK0 asserted** between commands: after a report's Final bit the drive *holds* TRK0 and keeps emitting cue INDEX "until another command is received" (Rev J §1.4.2), so the firmware sends one trailing `REPORT_NEXT_BIT` (ignored outside the report subcontext) to clear both. The spec calls this out as important for reports during Logical Forward, where stray cues would look like segment marks.
- **Arguments:** for argument-bearing commands, emit the operand as a following pulse train in **N+2 form** (value+2 pulses); Soft Select is the exception (a literal 20 pulses), and Enter Diag Mode 1/2 repeat the command code. An argument is at most a **6-bit value** (0-63, Rev J §1.4.3); out-of-range arguments are silently *ignored* by the drive, so the host refuses them.
- **Report clock:** issue the report command, read the **ACK** bit (within TACK ≈ 2.5 ms *after the command time-out*, i.e. up to ~5.4 ms after the last STEP -- Rev J §1.4.2 says wait TACK + nominal TTIMEOUT; if false → flag reset/hardware failure), then for each data bit issue `REPORT_NEXT_BIT` (2 pulses) and sample TRK0 within TBIT (900 µs of the 2nd pulse edge), LSB-first; finally read the **Final** bit (false ⇒ report error). Up to 16 bits (Report Error Code = 2 bytes). The whole loop runs device-side → immune to USB jitter; the drive latched the value at command receipt. Two strategies: fixed settle (sample after TBIT; bench-proven on the Colorado Jumbo 350, the default) or `index_edge` (wait for the cue INDEX the drive emits while a report bit is presented; first cue within TINXON 2.5 ms, then every TINX ≤ 12 ms -- so the wait is bounded by TINXON + TINX, not TBIT). Profiles pick via `report_strategy`; the drive layer pushes it with the timings in `wake()`.
- **wait-ready:** QIC-117 has **no ready line**. The host polls the Ready bit of Report Drive Status (ftape-style) with Rev J Table 2d's per-command time-out (Seek Load Point 670 s, Stop 8 s, Seek Head to Track 15 s, ...); the firmware `WAIT_READY` verb waits on cue INDEX instead, which also fires while a report bit is up or an argument is awaited, so it is only meaningful when neither is pending. Error Detected / Referenced / BOT / EOT are **valid only while Ready**, and Report Error Code clears the latch only once Ready.

### 5.4 Flux engine (data channel) — capture pipeline

Pipeline: **RDATA → capture front-end → encoder → ring buffer → USB streamer → host**, with a **marker injector** merging into the stream.

- **Capture front-end:** timer input-capture measuring inter-transition intervals in sample-clock ticks (reused from GW). **Free-running** — armed/stopped on command, *not* gated to one index-to-index span. Tape *does* assert INDEX, but **once per segment, not per revolution**, so the engine **records** each INDEX edge as a marker rather than gating on it — segment boundaries arrive for free, and the in-stream `C2/FC` index address mark gives a second, independent boundary signal for the decoder to cross-check.
- **Encoder:** GW flux byte encoding — short intervals as direct bytes, a **long-flux continuation escape** (important on tape: dropouts and inter-record gaps produce long intervals that must not be lost or saturated), and **opcode escapes** for out-of-band events (reused).
- **Marker injector (new):** rides the opcode escape channel (same mechanism GW uses for index), so markers never get misread as flux. **Decision:** reuse the existing Index opcode (bring-up; parseable by stock GW tooling) *or* define typed tape opcodes in a header shared by firmware + custom host decoder (real design). Recommend typed, under the same "one shared table, both ends" discipline as the command set. Marker set in §7.2.
- **Ring buffer (SRAM) + USB streamer:** the SRAM ring absorbs host stalls, it doesn't keep pace with the drive; the link itself is Full-Speed with modest, not ample, headroom at standard rates (§3.1).
- **Backpressure policy — never silently drop flux:** transient lag absorbed by the buffer; sustained near-overflow → emit `EVENT{overflow}` and trigger a **clean abort** through the arbiter funnel (for tape a gap is unrecoverable in place; a clean re-do beats a corrupt splice). If you must continue instead, emit an explicit **gap marker** recording how much flux was lost so the codec treats it as a discontinuity. Overflow is one of the arbiter's fault triggers.
- **Termination / accounting:** on disarm, halt front-end, flush buffer, emit a single `END` opcode carrying reason, total flux-transition count, total byte count, and a checksum. Makes `RawFluxCapture` **self-terminating and self-verifying**. A capture with no valid `END` is flagged truncated (USB loss can't flush) — but it still decodes, because sectors self-locate; you lose the tail, not the file.
- **When a pass ends (bench findings).** A pass ends when the *tape* stops, never on a timer: Logical Forward runs to logical EOT and halts by itself, and the bench drive ignored Stop Tape during Logical Forward. Once the first segment's INDEX has been seen, **1 s with no flux transition** ends the run (the longest gap inside real data on the bench was 72 ms). The idle rule is not armed by flux alone — a pass starting at the physical end crosses ~1 s of blank leader, and stray head-settling transitions there ended early passes before any data — so until the first segment only a **15 s no-start** limit applies (Logical Forward refused, e.g. QIC-117 error 19 on an unreferenced tape). An INDEX within **50 ms** of arming is a ready-cue pulse, not a segment, and does not count. During Logical Forward the drive pulses INDEX once per segment and nothing else, so INDEX counts segments: `tw dump` uses that as a free health check (§6.3).

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
- **As built — `tw dump` (`tape/dump.py`).** One Logical Forward pass per track, streamed straight to `track-NN.twrf` with the drive's identity in the header (§7.1). Two bench lessons shaped it. (1) Logical Forward starts reading *wherever the tape is*, and where a pass stops is already past the next track's first few segments: the first bench dump lost ~5 segments at the start of every track after track 0. So each pass first **winds to the end where its track starts** (`wind_to_track_start`); in a sequential dump that is only the last few feet. (2) Old tape is fragile, so after every pass a **cheap, decode-free health check** (`check_pass`) asks: did the pass end at logical EOT, does the stream match the firmware's `END` accounting, did the drive find as many segments (INDEX pulses) as on earlier passes? The dump stops rather than spend passes on a tape that may be shedding. `--check` additionally decodes each pass and requires ≥ 80 % CRC-clean sectors. The `TapeTransport` class sketched in §6A.4 was never wired to a command and has been deleted; `dump` is the capture path.

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
- *(Pipeline **structure** is detailed in §6A.5; the RS solver, ID algebra, header/BSM, QIC-113 layout and QIC-122 decompression are **grounded and implemented** — §7.3, §7.5, §13.2.)*
- **As built, the steps split at two files.** Steps 1–4 are `tw convert` (step 1 in `tapewyrm-cli`'s `codec.gwstream` → `codec.gwpll` → `codec.mfm`; multi-pass union, placement, BSM and RS in `qiclib.build`), which writes a **TWTI** image (§7.6). Steps 5–6 are `qicsilver extract` (`qiclib.extract`, decompressing QIC-122 extents on the way), which writes one **TWVL** per volume (§7.7). Step 7 is `qicsilver tar`. The ladder with modules is §13.5.

---

## 6A. Host software — detailed design

> Expands §6 into implementable detail. The interface sketches below are **signatures, not implementations** — enough to scaffold modules and write tests against, not the finished code. Names are suggestions; the structure is the point.
>
> **Reading this section after the build.** The sketches were written before the code and are kept for their reasoning. Where the code took a different shape, an **As built** note says what exists and where; the file map is §6A.1 and §13.4, and the code is the authority.

### 6A.1 Language, runtime, project layout

**Language: Python 3.11+.** This is a deliberate choice driven by the reuse boundary, not preference. The single largest reuse win is the Greaseweazle host library's flux pipeline — PLL / bitcell recovery / MFM (IBM) framing — which is Python. Re-implementing that in another language to keep the host in C#/Go would throw away exactly the part that is hardest to get right and is already battle-tested. The control/transport layers (`link`, `qic117`, `tape`) are simple enough to live in any language, but the codec pins us to Python, so the whole host is Python for cohesion.

> **Escape hatch (recorded, not recommended for v1):** if a different host language is wanted later, the clean seam is the `codec` — keep it as a Python "decode service" consuming `RawFluxCapture` files and emitting a `LogicalVolume` + report, and reimplement only `link`/`qic117`/`tape` elsewhere. Because capture and decode are fully decoupled (the `RawFluxCapture` file is the entire contract), this split is cheap. Do not do it for v1. *(As built the seam is even cleaner: the TWRF and TWTI files are documented formats, `docs/spec/`, so any stage can be reimplemented against a file rather than an API.)*

**Dependencies.** *Original plan (superseded):* `greaseweazle` (host package) for the flux primitives and USB device handling; `pyserial` only if not using GW's USB layer; `numpy` for the RS/GF(256) math; **`click`** for the CLI; `tomllib` (stdlib) for drive profiles. *As built:* the decode stack is **pure stdlib**. GW's PLL is **vendored** (`codec/gwpll.py`, credited, Unlicense) rather than depended on — GW's version needs `bitarray` and a compiled extension, its wheels lag new CPython releases, and the reuse turned out to be one function. The RS math is a few hundred lines of table lookups and needs no `numpy`, so there is no `accel` extra. Runtime third-party deps are `pyserial` (the link), `rich` + `rich-click` (the CLIs' presentation only) and `backports.zstd` below Python 3.14 (TWTZ; the stdlib `compression.zstd` from 3.14). `tomllib` reads drive, cartridge and volume profiles. No async framework (see §6A.8).

**Package layout (as built).** *Superseded:* a single `tapewyrm/` package holding link, drive, tape, codec, rawflux and profiles together. That one package was split along the file formats into **four packages** under `packages/`, each with its own `pyproject.toml`, tests and `uv.lock`, joined by editable path sources. The arrows mean "depends on"; nothing may point the other way (`STYLE.md` §2):

```
tapewyrm-cli  ──► tapewyrm-archive ◄── qiclib ◄── qicsilver
      └──────────────────────────────► qiclib   (tw convert only)
```

Why: the libraries (`tapewyrm-archive`, `qiclib`) are **stdlib-only** — no rich/click, no hardware code — so a TWTI image can be read, identified and extracted on any machine with no USB stack, and the backup-format code (the part that grows with every new tape vendor) cannot accidentally reach for the drive. Presentation lives only in the two CLIs, each with its own `console.py` (kept identical by hand).

```
packages/
  tapewyrm-archive/tapewyrm_archive/   # the file formats; base of the graph; stdlib (+ backports.zstd < 3.14)
    twrf.py            # TWRF flux capture (RawFluxCapture), marker framing     §7.1, spec/twrf.md
    twti.py            # TWTI tape image: sparse save, TWTZ (zstd), sniff()    §7.6, spec/twti.md
    twvl.py            # TWVL extracted volume (+ SparseVolume, holes)        §7.7, spec/twvl.md
    types.py           # CaptureHeader, Marker, MarkerKind, Direction, TapeFormat (rate_kbps)
    qic117.py          # report-byte decoders DriveStatus / DriveConfig / TapeStatus (§13.1)
    progress.py        # Progress protocol + NULL_PROGRESS (library -> CLI progress hook)
    _zstd.py           # the one place zstd is imported from
  qiclib/qiclib/                       # QIC layout + backup formats; stdlib + tapewyrm-archive
    types.py           # RawSector, Segment, SegmentResult, SegmentStatus, FileEntry, FileSet
    geometry.py        # Geometry, the §7.3 coordinate algebra, ftk_per_side, 100/207 fallback
    place.py           # sectors -> (tpt,tps) segment bins; drops out-of-range IDs
    merge.py           # multi-pass union of CRC-good sectors (before RS)
    rs.py              # GF(256) erasure decoder (§13.2)
    segment.py         # erasure mask, excluded-sector repack, correct, classify
    volume.py          # header segment, BSM (code 4 list / fixed-format masks), VTBL, locate_header
    build.py           # sectors -> TWTI: merge, locate header, place, BSM, RS, write (tw convert)
    cartridge.py       # cartridge guess from geometry; per-standard §5.4.1 formulas
    volume_profile.py  # volume-table layouts per backup program; guess + scoring
    identify.py        # `qicsilver identify`: header + VTBL only
    extract.py         # `qicsilver extract`: TWTI -> TWVL, QIC-122 extents, holes
    qic113.py          # QIC-113 Basic-DOS file sets (§7.5)
    qic113ext.py       # QIC-113 Extended-OS file sets (§7.5)
    qic122.py          # QIC-122 (Stac LZS) decoder + QIC-113 compression extents
    profiles/cartridge/*.toml   # one physical cartridge per file (§7.8)
    profiles/volume/*.toml      # one volume-table layout per file (§7.8)
    testing/           # synthetic builders + real bench header bytes, for any package's tests
  tapewyrm-cli/tapewyrm/               # `tw`: hardware + physical decode; pyserial, rich, rich-click
    cli/, console.py   # rich-click front end (one module per command group); rich logging + progress on stderr
    types.py           # DeviceInfo, TimingParams, SelectHint, StopCond, ErrorCode, DriveProfile, ...
    link/              # transport.py (serial + GW framing), device.py (DeviceLink),
                       #   protocol.py (GENERATED), update.py (tw flash / tw dfu)
    qic117/            # commands.py (Rev J table), drive.py (Qic117Drive), status.py, profile.py
    tape/              # dump.py (tw dump), fluxprobe.py (tw drive flux)
    codec/             # gwstream.py (device stream), gwpll.py (vendored GW PLL), mfm.py (framing+CRC),
                       #   flux.py + pipeline.py (the original synthetic-fixture pipeline, tests only)
    image/convert.py   # tw convert: TWRF -> sectors -> qiclib.build -> TWTI/TWTZ
    buildinfo.py       # git commit for tw info (+ hatch_build.py stamps wheels)
    profiles/drive/*.toml   # colorado, colorado.1400, conner, iomega
  qicsilver/qicsilver/                 # `qicsilver`: offline image -> files; rich, rich-click
    cli.py, console.py # identify / extract / tar; console.py is a copy of tw's
    tar.py             # TWVL -> pax tar + damage report (was contrib/qic2tar.py)
```

Tests live in each package's `tests/` (one `test_<module>.py` per module); synthetic tape structures come from `qiclib.testing.builders`.

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

*As built* (`link/transport.py`, `link/device.py`): the transport is Tapewyrm's own pyserial `SerialTransport` speaking GW's command-packet framing (plus a scriptable `FakeTransport` for tests), with port autodetect via pyserial's `list_ports`; no GW host code is imported. `open()` runs GW `GET_INFO` then the Tapewyrm `INFO` capability gate. The capture stream's markers are parsed by `codec/gwstream.py` (the real device encoding, §13.5), not by a `rawflux` module. Payload layouts are hand-mirrored from `firmware/src/qic/qic.c` — a recorded TODO is to move them into `protocol/protocol.toml` so `generate.py` emits both sides, since exactly that drift broke the first bench bring-up.

### 6A.3 `qic117` — drive & protocol layer

Where semantics begin. Wraps a `DeviceLink`; turns "seek to load point" into the right transaction sequence and decodes what comes back.

- **Command table as data.** All ~47 commands as a frozen table, sourced from the QIC-117 spec / ftape `qic117.h`.

```python
class Kind(Enum): MODE = auto(); MOTION = auto(); REPORT = auto()

@dataclass(frozen=True)
class Cmd:
    code: int; kind: Kind; non_intr: bool; name: str
    takes_arg: bool = False

# Sketch only -- the authoritative table is packages/tapewyrm-cli/tapewyrm/qic117/commands.py,
# audited against Rev J Tables 2a-2d. non_intr is Rev J's "(n)" flag: exactly
# 3, 4, 14, 16, 18, 25, 26, 34, 35, 36.
TABLE: dict[str, Cmd] = {
    "SOFT_RESET":            Cmd(1,  Kind.RESET,  False, "soft reset", timeout_s=460),
    "REPORT_DRIVE_STATUS":   Cmd(6,  Kind.REPORT, False, "report drive status"),
    "SEEK_HEAD_TO_TRACK":    Cmd(13, Kind.MOTION, False, "seek head to track", takes_arg=True, timeout_s=15),
    "SEEK_LOAD_POINT":       Cmd(14, Kind.MOTION, True,  "seek load point", timeout_s=670),
    "STOP_TAPE":             Cmd(18, Kind.MOTION, True,  "stop tape", timeout_s=8),
    # ... full set incl. vendor-unique 31, 40-45 ...
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

- **`DriveProfile` — the injection seam (data, not code).** Per-drive wake/select quirk + timing envelope, loaded from `profiles/drive/*.toml`. This is the analogue of ftape `vendors.h`; a new drive is a new TOML file, never a code change. A wake step is a QIC-117 command name or a *line step*: `delay` (only its delay) or `motor on` (GW IBM PC bus SELECT + MOTOR for unit `arg`, ftape's Motor-on wake).

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

*As built:* `qic117/drive.py` dispatches on eight kinds (REPORT / MODE / MOTION / STREAM / SELECT / CONFIG / RESET / INTERNAL, §13.1), waits for Ready by polling Report Drive Status with Rev J Table 2d's per-command time-outs, and **refuses the write path** (Enter Format Mode, Write Reference Burst) unless built with `allow_writes=True` — this project recovers tapes, and nothing should arm the write head by accident. The report-byte decoders (`DriveStatus`, `DriveConfig`, `TapeStatus`) moved to `tapewyrm_archive.qic117`, because TWRF and TWTI headers store raw report bytes and the offline tools must decode them without the hardware package; `ErrorCode` and the classification stay in `tapewyrm-cli`. Drive profiles are `tapewyrm/profiles/drive/*.toml`; the bench-proven one is `colorado` (Jumbo 350: a jumperless *phantom* drive selected by Phantom Select 46 plus its N+2 unit argument), with `colorado.1400` for the QIC-3010 Colorado 1400.

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

*As built:* `Geometry` moved to `qiclib.geometry` (the offline side needs it, the hardware side only reads it). `TapeTransport` was built as sketched but only tests ever drove it, so it is gone; the capture path is `tw dump` (`tape/dump.py`, §6.3): one function per pass that winds to the track's start, arms `CAPTURE` with Logical Forward, streams to a TWRF file and checks the pass. `tw drive flux` (`tape/fluxprobe.py`) is a diagnostic sibling — a timed Physical Forward/Reverse (or Logical Forward when referenced) recorded to TWRF, for a drive that will not reference a tape.

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

*As built*, the sketch above is `codec/pipeline.py` and still runs — against synthetic fixtures in the tests whose "flux" is the decoded MFM byte stream itself (`codec/flux.py`). Real captures take a different road, because the real device stream is GW's encoding (§7.1):

```python
# tapewyrm-cli: tapewyrm/image/convert.py  (tw convert)
ps      = gwstream.parse(flux)                 # GW flux encoding + Tapewyrm markers; verified vs END
cells   = gwpll.flux_to_bitcells(ps.intervals, ps.sample_clock_hz, mfm.bitcell_seconds(rate))
decoded, _ = mfm.frame_bitcells(cells)         # bitcells -> bytes at the A1/C2 sync marks
sectors = mfm.recover_sectors_from_bytes(decoded)   # ID + data fields, CCITT CRC -> RawSector
# qiclib: qiclib/build.py
build_image(passes, out, sources=...)  # merge.union -> volume.locate_header -> geometry from the
                                       # header -> place -> apply_bsm -> RS -> twti.TapeImage.save
```

`rate_kbps` is the capture's own recorded rate (§7.1). The header segment is found *before* placement, because the geometry used to place every other sector — segments per track, and `ftk_per_side` (§7.3) — comes from the header itself. `convert` logs one INFO line per stage and pass (what the capture is, flux and PLL counts, sector tally, merge, header, geometry, BSM, cartridge guess, segment tally, write) and, with `--progress`, shows an outer "capture k/N" bar plus one bar per stage; decode cost with progress off is within ~2 %.

### 6A.6 `rawflux` (as built: `tapewyrm_archive.twrf`) — capture container

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

*As built:* the container is `tapewyrm_archive.twrf` (there is no `rawflux` package), the format is called **TWRF**, and its byte layout is owned by [`docs/spec/twrf.md`](spec/twrf.md); §7.1 keeps the rationale. Two departures from the sketch: `tw dump` streams the device's bytes straight to disk behind a preamble (`write_preamble`) rather than buffering a `RawFluxCapture` in memory, and the stored flux is the **verbatim device stream** — GW's encoding with Tapewyrm markers — which `tapewyrm_archive.twrf.parse_body` parses and checks against the `END` marker's counts and checksum. That one tokenizer serves both `tw` (as `codec/gwstream.parse`) and `RawFluxCapture`'s helpers (`iter_markers`, `flux_data_only`, `verify`); until 2026-10-03 those helpers scanned for `0xFF` with an invented `0xFF 0xFF` stuffing rule and failed on real captures (TWS-1 §5.5).

### 6A.7 CLI, configuration, logging

*Original plan (superseded):* one Click group, the **`tw`** executable, with seven verbs each mapping to a layer:

| Command | Does | Layers |
|---|---|---|
| `tw probe` | open device, wake, identify; print config / tape status / geometry | link, qic117, tape |
| `tw capture` | sweep tracks → write `RawFluxCapture` files | + rawflux |
| `tw decode` | `RawFluxCapture`(s) → `LogicalVolume` + recovery report (no hardware) | rawflux, codec |
| `tw recover` | capture + decode + multi-pass retries on weak segments | all |
| `tw replay` | re-decode saved flux with different PLL/RS options | rawflux, codec |
| `tw flash` | update firmware via the GW-compatible application bootloader over USB | link/update |
| `tw dfu` | recovery / first flash via the AT32 ROM bootloader (wraps `dfu-util`) | link/update |

*As built*, the verbs follow the files instead of the layers (§4): each command reads one format and writes the next, so a step can be re-run without redoing the ones before it (a re-read of three bad tracks is a second `dump` directory handed to the same `convert`). Bring-up also needed hand control of the drive, which became `tw drive`. Two executables, both on **rich-click** (imported as `click`):

**`tw`** (`packages/tapewyrm-cli`, long alias `tapewyrm`) — the hardware and the flux → image step:

| Command | Does |
|---|---|
| `tw info` | host and firmware build identity (git commit, dirty flag), board, port, USB serial; warns when they differ |
| `tw drive select [--unit N]` / `deselect` | select the drive (profile's wake sequence; phantom unit override) / release it |
| `tw drive status` / `report NAME` | decoded Report Drive Status / any report command (error, config, rom, vendor, tape, …) |
| `tw drive load-point` / `fwd` / `rev` / `stop` / `track N` | motion by hand (`fwd`/`rev` for `--seconds`); `track` is Seek Head to Track (0–63) |
| `tw drive rate KBPS` / `format FORMAT` | Select Rate or Format (command 27) — e.g. 500 kbit/s for a QIC-80 tape in a QIC-3010 drive |
| `tw drive micro up\|down` | micro-step the head |
| `tw drive flux` | run the tape for `--seconds` under a chosen motion and record a TWRF (diagnostic, §6A.4) |
| `tw drive scope` | edge-log TRK0 / INDEX / WRPROT / pin 34 after optional STEP pulses (`SCOPE` verb) |
| `tw dump DIR [TRACKS] [--check]` | one Logical Forward pass per track (TRACKS such as `0-27`; default: every track of the format the drive reports) → `DIR/track-NN.twrf` (+ `dump.jsonl`); health-checked (§6.3) |
| `tw convert SOURCE… IMAGE` | TWRF dumps → TWTI image (`.twtz` suffix: zstd); several dumps of one tape merge (§6A.5) |
| `tw inspect FILE [--json]` | header of any TWRF / TWTI / TWTZ / TWVL file, identified by magic: decoded summary, or the stored JSON verbatim (`--json`); reads only the header and an image's segment table (`tapewyrm_archive.inspect`) |
| `tw flash IMAGE [--dfu]` | firmware via the GW-compatible application bootloader (or DFU) |
| `tw dfu IMAGE` | recovery / first flash via the AT32 ROM bootloader (wraps `dfu-util`) |

**`qicsilver`** (`packages/qicsilver`) — offline, no hardware, no flux; reads TWTI or TWTZ (chosen by magic, not suffix):

| Command | Does |
|---|---|
| `qicsilver identify IMAGE [--json] [--volume-profile P] [--raw]` | cartridge guess, header fields, dates, bad sectors, volume table — from the header and VTBL segments only |
| `qicsilver extract IMAGE [OUTDIR] [--volumes LIST] [--prefix P] [-o FILE] [--volume-profile P]` | `OUTDIR/PREFIXNN.twvl` (default `./vol-NN.twvl`), one per selected volume, or `-o FILE` for exactly one: QIC-122 decoded, holes recorded (§7.7). LIST is `0,2-4` style; a volume the table lacks is refused before anything is written |
| `qicsilver inspect VOLUME [PATH...] [--json] [--damaged]` | `tar tv`-style listing of a TWVL: summary (tape, label, date, directory format, counts, missing bytes, lost segments), then mode, size, mtime, damage (`lost N` / `error`), path per entry; PATH globs filter. Shares tar's directory walk (`qicsilver.entries`) |
| `qicsilver tar VOLUME OUT.tar [--report R] [--skip-damaged]` | TWVL → pax tar + damage report (default `OUT.tar.damaged.txt`; was `contrib/qic2tar.py`) |

**Shared front end (`STYLE.md` §2.5).** Both CLIs take the same global flags — `--progress` (rich progress bars), `-v` (debug) and `-q` / `-qq` (warnings / errors only) — the log level moves around INFO — and subcommands may not reuse `-v`/`-q`. Command *results* (status lines, summaries, `--json`) go to **stdout** via `click.echo`, so they pipe; the library's narrative goes through `logging`, and both it and the progress bars go to **stderr** through **one shared rich console**, because rich's live display tears if anything else writes to the terminal while bars are up. Each CLI has its own `console.py` (the two are kept identical by hand) rather than a shared presentation package, so neither CLI depends on the other. The libraries never import rich or click: they log through `logging` (log before acting; every guard logs at debug, with lazy `%` arguments; hot paths log one summary, not per item) and report progress through the `tapewyrm_archive.progress.Progress` protocol, whose default `NULL_PROGRESS` costs nothing. `ValueError` from a library becomes a `click.ClickException` at the CLI edge. Heavy imports happen inside the command functions so `--help` stays fast.

Config precedence (`tw`): CLI flags → config file (`--config`, TOML; without it the per-user `~/.config/tapewyrm/config.toml`, or `$XDG_CONFIG_HOME` / `%APPDATA%`, if present) → profile defaults, resolved once in `AppContext.load` and carried on `ctx.obj`; the file's keys are `port` and `profile` (`cli.app.CONFIG_KEYS`), and any other key is a WARNING naming it, so a typo is not silently ignored; logging is set up *before* that load so `-v` shows which config and drive profile were picked up. *Superseded:* with no profile named the default was `default` (no wake sequence), which a phantom drive never answers. *Superseded:* `auto` tried only `colorado` and sent Phantom Deselect (47) after a try that got no answer. *As built:* the default is `auto`, which follows ftape's drive detection (Linux 2.6.19 `drivers/char/ftape/lowlevel/ftape-ctl.c` `ftape_activate_drive()`, `ftape-io.c` `ftape_wakeup_drive()` / `ftape_report_raw_drive_status()`, `include/linux/ftape-vendors.h` `WAKEUP_METHODS`); ftape's behaviour is the safety precedent. Each drive session tries the profiles in `qic117.profile.AUTO_ORDER` — `default` (ftape "None": no wake), `colorado` ("Colorado": Phantom Select 46 + unit 0; tw adds Enter Primary Mode), `mountain` ("Mountain": Soft Select 23 + 20 pulses), `insight` ("Motor-on": 100 ms, then IBM PC bus unit 0 select + motor-enable via GW SELECT/MOTOR) — in that order (`qic117.drive.auto_wake`), and keeps the first the drive answers by ftape's test (Report Drive Status within 4 tries, not 0xff). As in ftape, nothing is undone between tries except a Motor-on motor (motor off + deselect), which also goes off at session end; a try the link cannot do is skipped with an INFO line. The Soft-Reset placeholder wakes (`conner`, `iomega`) stay opt-in: ftape never wakes with a Soft Reset. Only the Colorado method is bench-verified; None, Mountain, Motor-on, the order and the motor undo have run only against fake boards.

### 6A.8 Concurrency & data-flow model

- **Control path:** synchronous, blocking request/response. No concurrency needed.
- **Capture path:** streaming, and the one place concurrency matters. Model it as **reader → bounded queue → writer**:
  - a **reader thread** drains `CaptureStream.chunks()` off the serial port as fast as the USB delivers;
  - a bounded `queue.Queue` decouples it from disk;
  - a **writer** drains the queue to the `RawFluxCapture` file.
  This keeps a slow disk from stalling USB reads (which would back-pressure the device and trip its overflow → abort). The host-side queue + the device's SRAM ring buffer together absorb stalls.
- **Abort** is a control write (`CaptureStream.abort()`) issued from the main thread; the reader loop terminates when it sees the `END` marker in the stream.
- **Threads, not asyncio.** pyserial is blocking; a single reader thread + queue is simpler and entirely sufficient here. (asyncio with a thread executor is possible but buys nothing.)
- *As built*, even the reader thread proved unnecessary at QIC-80 rates: `tw dump` reads the stream and writes it to the TWRF file in one loop on the main thread. The thread + queue design stays recorded for faster formats or slower disks.

### 6A.9 Error handling, partial captures, observability

- **Typed errors:** `LinkError` (transport/timeout/version), `DriveError` (carrying the QIC error code + fatal/benign classification), `CaptureError` (overflow/aborted), `DecodeError`.
- **Overflow:** an in-stream `EVENT{overflow}` means the device aborted the session cleanly; the host records it and **retries the pass** rather than trusting a gapped capture.
- **Truncation:** missing `END` → flagged, decoded anyway.
- **Recovery report** is the user-facing quality signal: per-segment status (`clean` / `corrected(k)` / `uncorrectable`), per-track coverage %, BSM accounting (expected-bad vs unexpected-bad), and a list of segments worth re-capturing. `recover` uses this to decide which tracks to re-run.

*As built*, quality travels **inside the files** rather than in a separate report: every TWTI segment carries its state (missing / clean / corrected / uncorrectable / bad) and erasure count (§7.6), every TWVL records the byte ranges it could not fill (§7.7), and `qicsilver tar` writes a damage report naming each file that touches a hole, separately from files the original backup software itself could not read. There is no `recover` verb; re-reading is "dump the weak tracks again, `convert` both dumps together" (the merge keeps any CRC-good copy). `RecoveryReport` (`tapewyrm.types`) survives as the synthetic pipeline's return value only; its formatter `report.py` is gone. Overflow and truncation behave as above: the firmware ends the run cleanly, `gwstream` reports whether the parse matched the `END` marker, and `convert` says so per capture.

### 6A.10 Testing strategy (hardware-free first)

The capture/decode decoupling makes most of the system testable with no drive attached:

- **Golden fixtures:** record a few real `RawFluxCapture` files (or synthesize them) and commit as fixtures; the entire `codec` is tested against them offline, deterministically.
- **Mock `DeviceLink`:** replays a recorded transaction log (for control) and a recorded flux byte stream (for capture). Lets `qic117` and `tape` be tested end-to-end without hardware.
- **Unit tests:** RS erasure decode against known vectors (cross-check with ftape `ecc`); MFM framing against GW's own decoder tests; `(FSD,FTK,FSC)` → geometry with hand-built IDs; status/error/config bit-decoding tables.
- **Property tests:** `RawFluxCapture` save/load round-trip is lossless; any segment with ≤3 injected erasures always recovers; capture order permutations yield identical placement (sectors self-locate).
- **Integration (with hardware):** `probe` smoke test (wake + identify) on the target drive; then a full `recover` of a known tape, diffed against DOS/ftape output if available.

*As built:* each package has its own `tests/` (one `test_<module>.py` per module, `STYLE.md` §2.6) and `just host` runs ruff, mypy and pytest over all four in dependency order (CI does the same as a matrix). Synthetic header segments, volume tables and QIC-113 volumes come from `qiclib.testing.builders`; a `FakeTransport` scripts the device for the link, drive and CLI tests. In place of committed golden flux captures (large, and cut from real people's tapes), `qiclib.testing` holds a few **real header and volume-table bytes** from bench tapes, and `gwstream`'s decoder was validated once against a real capture's `END` accounting (§13.6 item 1). The hardware integration step happened on the bench: a whole QIC-80 backup tape dumped, converted, extracted and turned into a tar with nothing lost.

### 6A.11 Core data types (reference)

The dataclasses that cross module boundaries. *As built* they live in three places, by who must be able to read them without the others: `tapewyrm_archive.types` / `.qic117` (anything stored in a file), `qiclib.types` (sectors, segments, files) and `tapewyrm.types` (the hardware stack).

| Type | Carries | Defined in | Produced by | Consumed by |
|---|---|---|---|---|
| `DeviceInfo` | model, mcu, fw, caps, sram | `tapewyrm.types` | `link` | `tw info`, capability gate |
| `TimingParams`, `SelectHint`, `StopCond` | config for the device | `tapewyrm.types` | `qic117`/`tape` | `link` |
| `DriveStatus` | ready/error/cartridge/wp/new/referenced/bot/eot | `tapewyrm_archive.qic117` | `qic117` | `tape`, CLIs |
| `ErrorCode` | QIC error + fatal flag | `tapewyrm.types` | `qic117` | `tape` |
| `DriveConfig` | data rate, extra-length, QIC-80 mode | `tapewyrm_archive.qic117` | `qic117` | `dump` (rate), `identify` |
| `TapeStatus` | format (QIC-40/80/3010/3020), tape type, wide | `tapewyrm_archive.qic117` | `qic117` | `dump`, `cartridge`, `identify` |
| `DriveProfile` | wake seq, timing, report strategy, quirks | `tapewyrm.types` | `profiles/drive/*.toml` | `qic117` |
| `Direction`, `TapeFormat` | serpentine direction; standard + `rate_kbps` (§2.2) | `tapewyrm_archive.types` | — | everywhere |
| `CaptureHeader` | rate, clock, track, direction, pass-id, utc, raw drive reports, commits | `tapewyrm_archive.types` | `dump` | TWRF, `convert` |
| `Marker`, `MarkerKind` | session-start / segment / event / end / heartbeat | `tapewyrm_archive.types` | `twrf` (parse) | tests, synthetic pipeline |
| `RawFluxCapture` | header + verbatim flux bytes | `tapewyrm_archive.twrf` | `twrf` (parse), test fixtures | synthetic pipeline |
| `FluxStream` | decoded intervals | `tapewyrm.types` | `codec.flux` | `codec.mfm` |
| `RawSector` | (fsd,ftk,fsc), data[1024], crc flags, deleted | `qiclib.types` | `codec.mfm` | `qiclib.merge`, `.place` |
| `Segment`, `SegmentResult`, `SegmentStatus` | 32 `RawSector` slots; RS outcome | `qiclib.types` | `qiclib.place` / `.rs` | `qiclib.build` |
| `Geometry` | tracks, spt, `ftk_per_side`, direction() | `qiclib.geometry` | header (`volume.locate_header`) | `place`, `build` |
| `VolumeInfo`, `BadSectorMap`, `VtblEntry` | header segment, BSM, volume table | `qiclib.volume` | `qiclib.volume` | `build`, `identify`, `extract` |
| `TapeImage`, `SegmentState` | an open TWTI/TWTZ image | `tapewyrm_archive.twti` | `qiclib.build` | `identify`, `extract` |
| `Volume`, `SparseVolume` | one TWVL volume; bytes + holes | `tapewyrm_archive.twvl` | `qiclib.extract` | `qicsilver tar` |
| `FileEntry`, `FileSet` | a recovered Basic-DOS tree + file bytes | `qiclib.types` | `qic113.extract` | `qicsilver tar` |
| `qic113ext.DirEntry`, `EntryLayout` | an Extended-OS entry and where its data lies | `qiclib.qic113ext` | `qic113ext.layout` | `qicsilver tar` |
| `RecoveryReport` | the sketch's output | `tapewyrm.types` | `codec.pipeline` | tests only |

---

## 7. Data formats

### 7.1 `RawFluxCapture` (TWRF) — a linear track-stack, not a disk image

The container reuses GW's flux **byte encoding** but **not** its disk framing. A `.scp` image is organized around *revolutions*: each index-to-index span is one full circular track, and sectors live around the revolution. Tape has no revolutions — it is a linear medium written as a **serpentine stack of tracks**, each track a linear run of index-delimited segments. So the container frames flux that way (full model in §7.4):

- **Capture header (the linearization key).** Recording-format (QIC-40/80/3010/3020), declared bitcell rate (from `REPORT_DRIVE_CONFIGURATION`, stamped before arming), sample clock/tick rate, **geometry** `{segments_per_track, tracks, sectors_per_segment=32}`, tape type/length if known, device serial, start UTC. Geometry is what lets the decoder turn a disk-coordinate sector ID into a linear `(TPT, TPS)` position.
- **Track runs (the stack).** One per captured tape-track pass: `{TPT, direction (even=forward / odd=reverse), pass-id, flux-run}`. The flux-run is the verbatim GW byte stream for that pass, with its in-stream segment-index markers. A salvage pass taken in *physical* reverse is flagged so its flux can be time-reversed offline; a normal Logical-Forward pass is already in logical order and needs no reversal.
- **Segment marks (within a run).** Each hardware INDEX edge is recorded as a `SEGMENT` marker, and the in-stream `C2/FC` index address mark is the second, independent boundary — together they delimit the linear segment sequence the FDC sees.
- **Footer:** implicit, in the in-stream `END` opcode (counts + checksum). A run with no valid `END` is flagged truncated but still decodes (sectors self-locate).

Multiple passes of the same `TPT` are independent runs in (or across) captures; the codec unions their good sectors before RS (§6.4). One capture file may hold one run, one track's passes, or a whole sweep — the header + per-run `TPT`/direction make it self-describing either way.

**As built — TWRF.** The format is **TWRF** (`.twrf`, `tapewyrm_archive.twrf`); the byte layout is specified in [`docs/spec/twrf.md`](spec/twrf.md) (TWS-1). In summary: a magic, a version, a length-prefixed **JSON header** (the `CaptureHeader`), then the **verbatim device stream** — GW's flux encoding with the Tapewyrm markers inside (§7.2, §13.5) — exactly as it came off USB. What changed from the plan above:

- **One file per pass.** *Superseded:* the multi-run container. Each file holds exactly one run (`track-NN.twrf`, one Logical Forward pass); a dump directory is the "stack", and `dump.jsonl` logs every pass. Simpler to stream, to re-take one track, and to hand `convert` any mix of dumps.
- **Geometry is not in the capture.** The drive cannot be trusted to know it (a basic drive lacks Report Format Segments) and the tape already says it: `convert` reads geometry from the **header segment** (§7.3). The `segments_per_track`/`tracks` header fields stay for synthetic captures and are 0 in real ones.
- **TWRF v2 — self-describing captures.** The header gained the drive's **raw QIC-117 report bytes** at capture time (Report Drive Status, Drive Configuration, ROM Version, Vendor ID, Tape Status) and the `tw` and firmware **git commits**. The bit rate stored in the header comes from Drive Configuration, so decode never assumes a rate (§2.2), and the drive's tape-type opinion travels with the flux for `identify` to compare against the header's (§7.8). Every member is required (the drive reports may be null); v1 captures and headerless pre-TWRF `.raw` streams are not read.

### 7.2 Tape marker opcodes (shared firmware/host header)
Markers ride the GW opcode-escape channel, so they can never be misread as flux.

| Marker | Payload | When |
|---|---|---|
| `SESSION_START` | rate, sample clock, **TPT, direction**, pass-id, UTC | at arm — anchors t=0 and tags the run's place in the stack |
| `SEGMENT` | tick count since previous (and running index) | each hardware INDEX edge — the per-segment boundary |
| `EVENT` | code (motion-started, hole/EOT edge, overflow, gap…) | on observed bus/drive event |
| `END` | reason, flux-count, byte-count, checksum | at disarm — seals the run |

> `HEARTBEAT` from the earlier draft is **demoted**: its job was to let the host re-align a stream that had no intrinsic landmarks, but the per-segment `SEGMENT`/INDEX marker now supplies real boundaries. Keep a coarse keepalive only if a long erased stretch (inter-segment gap, end-of-data) would otherwise starve the stream of markers.

*As built*, the codes are `0xF0`–`0xF4` (`SESSION_START`, `SEGMENT`, `EVENT`, `END`, `HEARTBEAT`), framed `FF code len payload` inside GW's own opcode-escape channel, generated from `protocol/protocol.toml` (§12.4). The firmware emits `SESSION_START` and `EVENT{motion-started}` at arm, a `SEGMENT` next to each of GW's own `FLUXOP_INDEX` opcodes, and `END` on every in-band exit. Because the codes are now part of a *file* format, `tapewyrm_archive.twrf` keeps its own copy, and a `tapewyrm-cli` test asserts it matches the generated table.

### 7.3 QIC-80-MC format reference (decode target — grounded against Rev N)

**Encoding.** MFM, MSB-first, nominal 14,700 BPI / 68 µin bit cell, tape 34 ips. Standard rates (each spec's §3.4): 250 kbit/s (QIC-40), 500 kbit/s (QIC-80); 500 kbit/s (QIC-3010, 22,125 BPI) and 1 Mbit/s (QIC-3020, 44,250 BPI), both at 22.6 ips. *(Corrected from 1 / 2 Mbit/s and 42,000 BPI in an earlier draft; see §2.2 for why captures record the drive's actual rate.)*

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

**Geometry.** 28 tracks (0.250 in) / 36 (0.315 in/Travan) for QIC-80; 20 for QIC-40; 40 (0.250 in) / 50 (0.315 in) for both QIC-3010 and QIC-3020; **even tracks forward, odd reverse** (serpentine), referenced to forward/reverse BOT bursts; equal segment count per track. `spt` is variable; if the drive lacks Calibrate/Report-Format-Segments (CCS-2), fall back to the QIC-117 override: 100 (≤153 calibrated) or 207 (154–228). In practice `convert` takes tracks and `spt` from the **header segment** (below), not from the drive.

**Floppy tracks per side (bench correction).** The `1020` / `32640` constants above are Rev N's, which only covers format code 4, where the header records max FTK = 254. The general rule is **floppy tracks per side = header max FTK + 1**: the bench's fixed-format tape (code 5) records max FTK = 149, i.e. 150 floppy tracks = 600 segments per side, and with 1020 every sector on side ≥ 1 landed in the wrong segment. So the algebra takes `ftk_per_side` (`qiclib.geometry`, default 255), and the header must be located — with the default — *before* anything else is placed (`volume.locate_header`). Sectors whose IDs fall outside the coordinate space (FSC outside 1–128, FTK past the side, negative FSD) are dropped at placement rather than computing a nonsense segment.

**Header segment.** First defect-free segment (duplicated in the second); preceding/intervening segments are deleted-data. Sector 0 = **format parameter record** (offset 0–3 sig `55 AA 55 AA`; 4 = format code `04` variable-length; 24–25 segments/track; 26 tracks; 27 max FSD; 28 max FTK=254; 29 max FSC=128; 30–73 ASCII tape name; dates as packed `SC+60·(MN+60·(HR+24·(DY+31·MO)))`). Sectors 0–28 = **bad-sector map** (ascending 3-byte LSN entries, 1-based; `0`=end; **high bit of MSB set ⇒ that whole 32-sector segment is bad**). Sectors 29–31 = ECC.

- *Where the map starts.* Rev N §7.1 makes the parameter record bytes 0–255 of sector 0, so the 3-byte list starts at **offset 256**. (Starting at 128 read the lifetime-segments counter and the initial format date as two "bad sectors" on a bench tape.)
- *Fixed formats.* Real QIC-80 tapes also carry Rev K's **fixed format codes 2, 3 and 5**, which Rev N does not describe. On those, the map is a **32-bit little-endian mask per segment from offset 2048**, bit *k* = sector *k* excluded — taken from the bench, not a spec: it is the only reading under which the excluded segments decode and the QIC-113 byte offsets on both sides agree exactly. Their segment counts likewise come from the bench (150 for a 3M DC2120 at code 2, 207 for a Colorado-formatted tape at code 5), and the §5.4.1 equation inverts to the right lengths for both (§7.8).
- *QIC-3020 format code 6* (more than 65,535 segments) exists only in QIC-3020-MC Rev H §7.1, so it identifies the standard on its own.

**Volume table segment.** First segment of the logical area; 128-byte `VTBL` entries (start/end SEG, description, flags, OS type `1`=DOS required, compression per QIC-123), with extensions `XTBL` (unicode), `UTID` (unicode tape name), `EXVT` (overflow to another segment). Only **bytes 0–56** of an entry (signature, segment range, description, date, flags) are fixed by Rev N in a way every writer honours; everything after — section sizes, source label, compression, OS type — moves with the software that wrote the tape, which is what **volume profiles** describe (§7.8). The file-set/file layout *inside* the volume is **QIC-113**, specified in §7.5 (the top of the decode stack).

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

**Which level (QIC-40/80, Rev G §6).** Detect from the `VTBL` entry: if byte 56 bit 0 (vendor-specific) is set **and** vendor-extension words at offsets 58/60 read `113`/`7`, it is an **Extended-OS** volume (Rev G §8); otherwise, or if byte 125 (Format & OS Type) `= 1`, it is **Basic DOS** (§7). Basic DOS is the dominant case for QIC-80 DOS-era backups and the primary target. *(Refined on the bench: the word at 60 is the QIC-113 **revision**, and any revision counts — Rev F writes 6, and demanding 7 misread a real Rev F Extended-OS backup as Basic DOS. Byte 125 is deliberately **not** consulted either way: the vendor bit + `113` is the only mark Rev G §6 gives on QIC-40/80, and the bench's Extended volume has its OS type only behind that mark.)* As it happened, the first tape fully recovered was an **Extended-OS** backup (Colorado's CMS software), and the first Basic-DOS volumes came later, from tapes whose VTBL says `MTN` (§7.8).

**Volume layout.** Directory-Last flag = `VTBL` byte 56 bit 5:
- clear (Directory-First): `Directory Section + Data Section`, directory wholly on the first cartridge.
- set (Directory-Last; always set for Extended-OS): `Data Section + Segment Gap + Directory Section`, the directory segment-aligned and located by subtracting `Directory Section Size` (offsets 92–95, rounded up to whole segments) from the Ending Segment.

**Basic DOS (§7).**
- *Directory Section* = concatenated variable-length **Directory Entries**, each: Fixed Portion `{1B fixed+vendor size · 1B attrs [b0 read, b1 write, b2 exec, b3 hidden, b4 system, b5 subdir, b6 last-in-dir, b7 last-in-table] · 4B modify short-date · 4B data-entry-size · 1B extra-info [bits0–5: 2 = unreadable-at-backup]}` + `[vendor portion if size>10]` + Name Portion `{1B name size · ASCII name}`. **Ordering is breadth-first preorder**: root entries first, each directory's entries terminated by the `last-in-dir` bit, then descend left→right; `last-in-table` ends it. The tree reconstructs from that ordering + the dir/last bits.
- *Data Section* = concatenated **Data Entries**, same order: `{4B signature 0x33CC33CC (on tape little-endian → CC 33 CC 33) · copy of the Directory Entry · Path Entry [1B size · null-separated ASCII path] · file bytes}`. Non-empty directories have no data entry; empty directories carry a header-only entry (so the tree survives even if the directory section is lost).
- *Extraction*: parse the directory section into the tree (names/attrs/dates/sizes), then walk the data section — each `0x33CC33CC`-anchored entry yields path + bytes → write the file. The signature is the resync point when a volume is partially corrupt.
- **Corrections from real Basic-DOS volumes** (`qiclib.qic113`; the bullets above are kept as first read, these override them):
  - *Tree order.* Each directory **level** (all its entries, ending in `last-in-dir`) is followed by its subdirectories' levels, **each fully expanded, left to right, before the next sibling** — depth-first over levels. Rev G calls it "breadth-first, preorder", but its own worked example is depth-first, and so are real tapes; expanding levels with a queue mis-parented everything below the second level.
  - *Empty directories have no level.* A non-empty directory's Data Entry size is 0; an empty one's is non-zero (the size of its header-only data entry) and consumes no run. Missing that shifts every later level by one.
  - *Data Entry size counts the whole data header* (signature + directory-entry copy + path entry) plus the file bytes; `0xFFFFFFFF` means unknown, and the data then runs to the next signature.
  - *The Path Entry names the directory the item is in* (empty for the root); the item's own name comes from the directory-entry copy.
  - *Nine-byte fixed portions.* The `MTN` tapes write a fixed portion of size 9 — no extra-info byte — so the size byte, not a constant, places the Name Portion. They also end the directory with zero padding instead of `last-in-table`.
  - *False signatures.* `33 CC 33 CC` occurs inside file data (a bitmap on a bench tape); an entry is believed only if its size byte is 9–64, its name is printable and its length is not negative, otherwise the search resumes one byte later.
  - *`LTLT` counts only at the volume's end* (the four letters also occur inside files).
  - Files the directory lists whose data was lost are still reported, so the damage report can name them.
- **Directory-Last, located.** *Rev G locates the directory in segment space, back from the Ending Segment.* As built it is found **forward** from the data section: for an uncompressed volume the segments are laid end to end, so the Segment Gap is really in the stream and the directory is the **first segment boundary at or after `data_section_size`**; for a compressed volume the stream is rebuilt from the extents' uncompressed offsets, where the gap takes no room, so it is at exactly `data_section_size`. Both are tried, likelier first, and the first that parses as a Directory Entry wins. `qicsilver extract` records the exact offset as TWVL's `directory_offset` when it is known (§7.7). *(Superseded: `len(stream) − directory_section_size`, which is only right when the stream ends exactly at the directory's end, and sizing uncompressed volumes from the VTBL's section sizes, which ignores the gap and cut the end off the directory.)*

**Extended-OS (§8, summary).** Directory-Last always set. Names are **Unicode**. Data Entry = `0x33CC33CC + Directory Entry + Path Entry + 0..n Data Areas`; each **Data Area** = `{4B 0x66996699 · 2B Data-Area-ID · data}` where data is Null / Blob (`ID 7` = primary file bytes; `ID 6` = AFP resource fork) / Multirecord (Extended-OS records). The extended **Directory Entry** = Fixed Portion `{2B dir-entry-size · 8B data-entry-size · 2B path-size · 2B native-FS · 1B traversal [b0 dir, b1 empty-dir, b2 file-error, b3 last-in-dir, b4 last-in-media, b5 last-in-set, b6 root-entry]}` + Data Description Portion (≥1 Data Description Entries: `{2B ID · 8B data-area-size · 2B struct-size · struct · 2B name-size · Unicode name}`). **Data Description IDs** (priority-ordered): 0 vendor, 1 UNIX, 2 DOS, 3 Novell-3, 4 OS/2, 5 NT, 6 AFP, 7 Data, 8 Novell-2, 9 Novell-4, 10 Win95 — each with its own attribute structure (DOS: `1B attrs + 8B modify`; NT/Win95/OS2: `4B attrs + create/access/modify`; UNIX: `3B perms + 3 dates + inode/uid/gid + major/minor`; Novell/AFP richer). A root entry's name is the source device (`C:`, `\\SERVER\SYS`, `/usr/mounted`). *As built* in `qiclib.qic113ext`, checked against a real CMS Extended-OS backup: the data-section copies of directory entries may carry all-ones (unknown) sizes, which the spec allows, so the **directory section is the authority** — entry *k*'s data starts at the sum of the Data Entry sizes before it, and on the bench tape those sums total the VTBL's data-section size exactly. Paths are UTF-16LE components, each tagged with a file-system ID.

**Dates.** Basic uses Short Date/Time (`bits31–25 year−1970`, `bits24–0` packed `sc+60·(mn+60·(hr+24·(dy+31·mo)))` — same encoding as QIC-80-MC). Extended uses Full Date/Time (`4B seconds since 1970 GMT` + `4B tz/µs`); `0xFFFFFFFF`/all-`FF` = undefined.

**Compression (§9, if `VTBL` byte 124 bit 7 set).** Volume Data Area = Compression Extents → Compression Frames. Frame = `{2B Frame Size (hi bit set ⇒ data is uncompressed raw) · data bytes}`. Non-segment-spanning: one extent per segment at byte 0. Segment-spanning (`VTBL` byte 56 bit 4): first 2B of each segment = `Next Extent Offset`; each extent opens with an `8B Uncompressed Volume Byte Offset` for seek-without-decompress. The frame codec itself is **STAC LZS** (compression code 1) or DCLZ/ALDC (QIC-122/130/154) — a drop-in decompressor, not specified here.

*As built:* the **QIC-122 (Stac LZS)** decoder is `qiclib.qic122`, written from QIC-122 Rev B and QIC-113 Rev G §9 (no mature, widely used decoder existed to depend on): a bit-level stream of literals and back-references into a 2,048-byte history, each Compression Frame an independent stream; a frame ending with fewer than 18 bytes left in the segment is null fill and ends the extent. Decompression happens in `qicsilver extract` (`qiclib.extract`), segment by segment, placing each extent's output at its **Uncompressed Volume Byte Offset** — so a lost segment leaves a hole of the right size instead of shifting everything after it. Two findings: (1) the offset field is **8 bytes** per Rev G, but the `MTN` volumes use a **4-byte** offset (with 8, almost every segment fails; with 4, the offsets chain exactly, and the data section starts at an offset equal to the VTBL's directory-section size), so the width is a volume-profile setting (`[extent] offset_bytes`, §7.8); (2) only non-segment-spanning volumes have been seen, which is all the decoder handles. DCLZ/ALDC remain unimplemented.

**Multi-cartridge.** A `Link Sub-Section` (`'LTLT'` signature + media-sequence count + per-media ending offsets) is appended to the directory section; out of scope for a single-cartridge first target but the signature should be recognized and skipped.

This layer is implemented as pure library code consuming `(volume_byte_stream, vtbl_entry)` and yielding a directory tree + file blobs; like the rest of the codec it touches no hardware and is fixture-testable. *As built:* `qiclib.qic113` (Basic DOS, plus the Extended-OS framing) and `qiclib.qic113ext` (Extended-OS directory and data layout); the volume byte stream they consume is a TWVL file (§7.7), and `qicsilver tar` is what calls them.

### 7.6 TWTI / TWTZ — the logical tape image

**What it is.** The tape after the QIC-80 layer is done with it and before any backup format is touched: every sector placed by its own address under the **header segment's** geometry, the bad-sector map applied, every segment Reed–Solomon corrected, in segment order — plus how sure we are of each one. `tw convert` writes it (`qiclib.build` → `tapewyrm_archive.twti`); `qicsilver` reads it. The byte layout is specified in [`docs/spec/twti.md`](spec/twti.md) (TWS-2). In summary: magic, version, a JSON header (drive identity and **every source TWRF header**, so provenance survives each step), a **segment table** (per segment: state — missing / clean / corrected / uncorrectable / bad —, erasure count, bytes of real data, the BSM-excluded-sector mask), then a **fixed 29 KB slot per segment**.

**Why this cut.** The image is the point where the physical problem (flux, PLL, CRC, RS, which drive, which rate) ends and the logical problem (which backup program, which volume layout) begins. Everything physical is expensive and needs the drive's quirks; everything logical is cheap and needs a library of vendor layouts that keeps growing. Splitting there means a tape is decoded *once*, re-extracted any number of times as volume profiles improve, and an image can be shared and recovered by someone with no hardware at all. A missing segment is all zeros with state `missing`, never shortened, so later segments keep their positions and their uncompressed offsets stay meaningful.

**Fixed stride, random access.** A fixed slot per segment makes `segment(n)` a seek, which `identify` relies on (it reads two segments of a multi-gigabyte image). The price is size: a QIC-3020 tape has ~59,000 segments, so a single captured track made a 1.75 GB file that was nearly all zeros. Two answers, both transparent to readers:

- **Sparse TWTI.** `TapeImage.save` *seeks over* every empty or all-zero slot and sets the final length with `truncate`. On filesystems with holes (APFS, ext4, XFS, btrfs, ZFS) the zeros cost no disk and read back as zeros, so the bytes are identical to a dense file. (NTFS only makes holes for files flagged sparse, which Python cannot do portably; there the file is simply dense. APFS also densifies small files with interior holes — measured, not documented.) On the bench, one QIC-80 image was 124.8 MB long and occupied 9.2 MB (8.7 MB as TWTZ).
- **TWTZ** (`.twtz`): a TWTI byte stream through **one Zstandard compressor** — exactly what `.tar.zst` is to `.tar`, so `zstd -d x.twtz` gives a valid `x.twti`. It is for moving and archiving images, where holes do not survive. Writing picks TWTZ by **suffix** and streams through the compressor; reading picks by **magic** (`twti.sniff`), decompressing to a temporary *sparse* TWTI that is then memory-mapped like any other and deleted on close or exit. *Rejected:* a seekable-zstd frame index for true random access — one sequential pass to a sparse temp file buys the same `segment(n)` API with no index format to define. zstd comes from the stdlib `compression.zstd` (Python 3.14+, PEP 784) or its official backport below 3.14 — the one third-party exception in the libraries (`STYLE.md` §2).

### 7.7 TWVL — one extracted volume

**What it is.** One backup volume, laid out as the backup program wrote it: `qicsilver extract` reads the volume table through a volume profile (§7.8), decompresses each segment's QIC-122 extents (§7.5) and places the bytes at their uncompressed offsets — or, for an uncompressed volume, concatenates each segment's usable data. The byte layout is specified in [`docs/spec/twvl.md`](spec/twvl.md) (TWS-3). In summary: magic, version, a JSON header (the VTBL entry decoded and raw, the tape's identity, the source image, the **holes**, and `directory_offset`), then the volume bytes: the File Set Data Section followed by the Directory Section, which is what `qicsilver tar` reads.

**Holes, not shifts.** Segments that could not be read leave **holes**, recorded as byte ranges in the header and zero-filled in the data. Nothing after a hole moves, so every later file still sits where the directory says it does, and `tar` can name exactly which files touch damage. To size holes exactly, a lost segment's length is computed from its BSM-excluded sectors (which the TWTI records even for missing segments) rather than assumed to be a full 29 KB.

**`directory_offset`.** For an uncompressed Directory-Last volume the exact start of the directory is known at extraction time (the first segment boundary at or after the data section, §7.5), so it is stored in the header; `qic113.extract` and `qicsilver tar` use it instead of searching.

### 7.8 Cartridge profiles and volume profiles

Two different questions, answered by two kinds of TOML profile in `qiclib/profiles/`, both data rather than code (like drive profiles, §6A.3). *Superseded naming:* volume profiles were first called "tape profiles", which suggested they described the cartridge; they describe the **software** that wrote it, and the flag is now `--volume-profile`.

**Cartridge profiles — "what physical tape is this?"** (`profiles/cartridge/*.toml`, `qiclib.cartridge`). One file per catalogued cartridge (standard, tracks, width, length, cover capacity, coercivity, model, vendor, label), each with its own provenance comment. The header records tracks and segments per track; both are fixed by the recording standard and the tape's length, so together they name the cartridge. Exact counts come from the standards' covers (QIC-40's 68 / 102 / 365); variable-length formats use each standard's **§5.4.1 minimum-segments formula**, inverted to turn a segment count back into a length:

```
QIC-80:   int((length_in * 0.97 - 1.36 + 0.68 ) / 23.88 )    207 for 425 ft
QIC-3010: int((length_in * 0.97 - 1.36 + 0.452) / 15.936)    219 for 300 ft
QIC-3020: int((length_in * 0.97 - 1.36 + 0.226) / 8.131 )    429 for 300 ft
```

The formulas stay in code, keyed by standard — they are spec arithmetic every cartridge of that standard shares; the catalogue entries are data. QIC-3010 and QIC-3020 share track counts (40 / 50), so geometry alone can be ambiguous; `guess` takes the standard from the drive's **Report Tape Status** (recorded in TWRF v2) or from format code 6 (QIC-3020 only), and with neither and two fits it lists both rather than pick one. The bench's Verbatim **MC3020EX "QIC-Extra"** — an extended-length QIC-3020 cartridge whose shell sticks out past a standard minicartridge — reads 40 × 1,475 segments, which the QIC-3020 formula puts at ~1,030 ft: the 1,000 ft class (`qic-extra.toml`).

**Volume profiles — "how did this software lay out its volume table?"** (`profiles/volume/*.toml`, `qiclib.volume_profile`). Each maps VTBL fields to `[offset, length]` (section sizes, source label, compression, OS type, multi-cartridge sequence) and may set the QIC-113 extent offset width (`[extent] offset_bytes`). Shipped: `qic80-rev-n` (Rev N's own offsets), `cms-qic113` (Colorado CMS: vendor bit + `113` at byte 58, Rev N offsets), `mtn` (`MTN` at byte 58, vendor bit clear, label 4 bytes earlier, 4-byte extent offsets — every offset a reading of real bytes, not a spec) and `vendor-unknown` (only bytes 0–56 trusted). **Guessing** decodes every VTBL record with every profile and scores each reading with plausibility checks — segment range inside the data area, sane date, printable label, known compression code, claimed size fits the segments it occupies — plus `[match]` hints (vendor bit, signature bytes, format codes) worth ±2; the best total wins. A wrong layout reads neighbouring fields as sizes and labels and fails loudly (Rev N's layout on the `MTN` tape claims a volume of billions of gigabytes), so the margin is usually wide. `identify` and `extract` both read volume tables through this; the fixed parser in `qiclib.volume` remains the reference the `qic80-rev-n` and `cms-qic113` profiles are tested against.

---

## 8. End-to-end data flow

**Trace A — command + status transaction (e.g., seek load point, then read status):**
`tape.dump` → `Qic117Drive` looks up command, knows follow-up → `DeviceLink.command_txn(14, report_bits=0)` then `wait_ready` (bytes over USB, no bus) → crosses real-time line → firmware decodes, arbiter grants lease to verbs engine → 14 STEP pulses (verbatim) → release. Then `command_txn(6, report_bits=8)`: emit 6 pulses, clock 8 report bits off TRK0 device-side, return byte → host parses `DriveStatus`; on ERROR, `command_txn(7,…)` to read+clear.
*Representation:* `"seek load point"` → `(14, report=0)` → STEP edges (down); TRK0 samples → 8 bits → `DriveStatus` (up). Semantics exist only at the very top and the timing only at the very bottom.

**Trace B — capture session (one track pass):**
`capture_pass()` → `DeviceLink.capture_session(LOGICAL_FORWARD, stop)` → arbiter grants the lease and **holds it for the whole session** → verbs engine issues motion → firmware waits lead-in, arms flux engine free-running → flux engine encodes RDATA to GW flux + injects markers, streams over USB → host appends to `RawFluxCapture` with **no deadline** (recording, not feeding an FDC → no underrun/shoe-shine) → on stop, verbs engine issues STOP, flux engine emits `END`, lease released → offline codec runs PLL/MFM → sectors → segments → RS erasure → volume.
*Invariant:* one lease, device-held, granted to one engine at a time → control and flux can never collide, because the host has no verb to express it.

**Trace C — a whole recovery, as built (each arrow is a file):**
`tw drive select` / `tw drive status` (is it there, is the tape referenced?) → `tw dump dump1/ 0-27` (per track: wind to the track's start, Trace B, health check; §6.3) → `track-00.twrf … track-27.twrf` → *(optional: `tw dump` the weak tracks again into `dump2/`)* → `tw convert dump1/ dump2/ tape.twti` (gwstream → PLL → MFM per capture; merge passes; locate the header; place; BSM; RS; §6A.5) → `tape.twti` (or `.twtz`) → `qicsilver identify tape.twti` (cartridge, header, volume table; §7.8) → `qicsilver extract tape.twti vols/` (volume profile, QIC-122, holes; §7.7) → `vols/vol-00.twvl …` → `qicsilver tar vols/vol-00.twvl backup.tar` (QIC-113 tree → pax tar + damage report; §7.5).
*Representation:* flux exists only in TWRF; sectors and RS only between TWRF and TWTI; backup formats only after TWTI. Each stage can be re-run alone, and only the first needs the drive.

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

**Still open** (numbering is stable — code cites these items; resolved ones are struck through with what resolved them):

1. ~~**GW flux encoding byte-level specifics**~~ — **resolved.** The firmware reuses GW's real encoder and `0xFF` opcode escape; Tapewyrm markers are `0xF0`–`0xF4` (§7.2), and `codec/gwstream.py` decodes the whole stream (1–249 direct, 250–254 two-byte, `FF 01` INDEX, `FF 02` long interval / dead time, `00` end). Validated on the first real capture: transition count, flux byte count and checksum matched the firmware's `END` marker exactly.
2. ~~**Firmware extension points**~~ — **resolved by vendoring** (§13.6 item 2): verbs grafted into GW's `process_command`, capture reuses GW's flux read free-running, host stop maps onto GW's clear-comms path.
3. **Target-drive specifics (narrowed further).** Init is *specified* (reset → up to ~1 s diagnostics → clear New-Cartridge/Error via Report Error Code → auto seek-load-point), so this is no longer a mystery handshake. *Bench results:* the Colorado Jumbo 350 is a jumperless **phantom** drive (Phantom Select 46 + N+2 unit 0; a bare 46 is ignored), reports with the fixed-settle strategy, answers Report Tape Status, and ignores Stop Tape during Logical Forward; the Colorado 1400 (QIC-3010) addresses the same way and needs `tw drive rate 500` for QIC-80 tapes (`profiles/drive/colorado*.toml`). Still open: the `conner` and `iomega` profiles are unverified guesses, and exact timings / CCS levels of drives not yet on the bench. Characterize via `tw drive scope` (or `gw pin`) + scope; encode as a `DriveProfile` (ftape `vendors.h` for hints).
4. **34-pin open-collector assumption** — confirm the target drive is the Subsection-I 34-pin open-collector interface GW matches, **not** the 40-pin tri-state variant (§2, §3). *Functionally confirmed* — both Colorado drives take commands, report and stream through the stock v4.1 — but the scope-level polarity check is still a `TODO(bench)` in `qic.c`.
5. **v4.1 schematic** — TRK0/INDEX on pollable GPIO/EXTI; STEP buffer swings the bus at cadence. *Functionally confirmed* by the same bench work; not checked against the schematic.
6. ~~**RS decoder excluded-sector handling**~~ — **resolved.** `qiclib.rs` / `qiclib.segment` implement the `N = 31 − bad-blocks` codeword with parity in the last three non-excluded slots, verified exhaustively against the Rev N test vector and on real tapes, including fixed-format maps (§7.3).
7. **QIC-113 (Host Interchange Format)** — **read; specified in §7.5; implemented.** Basic-DOS and Extended-OS volumes both extract from real tapes, and the **QIC-122 (Stac LZS)** decompressor is done (`qiclib.qic122`). Still open: **DCLZ / ALDC** (QIC-130/154), segment-spanning compressed volumes (none seen), multi-cartridge sets, and full Extended-OS per-OS attribute round-tripping (DOS attributes are kept as a pax header today).
8. **QIC-3010/3020 deltas** — **folded in.** Same framing; 40 tracks on 0.250 in and 50 on 0.315 in for both; standard rates 500 kbit/s and 1 Mbit/s (§2.2); per-standard §5.4.1 formulas and cartridge profiles (§7.8); format code 6. A QIC-3020 tape (the MC3020EX "QIC-Extra") has been captured, converted and identified on the bench. Still open: no QIC-3010/3020 tape with a *backup on it* has been read yet, and `Geometry.for_format`'s 3010/3020 track counts are only a fallback the header overrides.
9. **Drive power/termination** — separate 5V/12V PSU; USB 5V isolation jumper set; terminate the bus at the drive end as for a floppy.
10. **App-container parse (above QIC-80/113)** — CP Backup CPB and similar are a separate, app-specific parse on the recovered logical data.
11. **Rev K fixed formats (codes 2, 3, 5)** — their bad-sector-map layout and segment counts come from bench tapes, not a spec (Rev K is not in hand; §7.3). Confirm against Rev K, or against more fixed-format tapes, before trusting them for a new vendor.
12. **Vendor volume-table layouts** — volume profiles (§7.8) other than Rev N's are readings of real bytes; each new backup program is a new profile, and `vendor-unknown` only trusts bytes 0–56.

---

## 10. References (sources leaned on)

**Provenance:** the command channel (§2.1, §5.3) is grounded against **QIC-117 Rev J**, the recording format (§2.2, §7.3) against **QIC-80-MC Rev N**, and the file-set logical format (§7.5) against **QIC-113 Rev G** — all three read in full. Since then QIC-3010/3020-MC and QIC-40-MC have been read for geometry, rates, segment formulas and format codes (§2.2, §7.3, §7.8), and **QIC-122 Rev B** in full (the Stac LZS decoder, §7.5); DCLZ/ALDC (QIC-130/154) are in `docs/qic-standards/` but not implemented. Rev K of QIC-80 (the fixed formats) is not in hand (§9 item 11). All the standards cited live in `docs/qic-standards/`.

- **QIC-117 Rev J** — Command Set Interface (the command channel): STEP command/argument pulses (N+2), TRK0 report framing (ACK/Final), INDEX cueing + per-segment marking, Table 1 timing, restriction/argument/response/timeout tables, error codes, 34-pin open-collector electrical (Subsection I). `https://www.qic.org/html/standards/11x.x/qic117j.pdf`
- **QIC-80-MC Rev N** — Recording Format: tracks/serpentine, segment MFM byte layout, sector-ID coordinate algebra, RS ECC (field/generator/test codewords), header segment + format parameter record + BSM, volume table. `https://www.qic.org/html/standards/8x.x/qic80n.pdf`
- **QIC-113 Rev G** — Host Interchange Format: volume data area, Basic-DOS + Extended-OS directory/data sections, directory entry layouts, signatures (`0x33CC33CC`, `0x66996699`, `VTBL`, `LTLT`), compression framing. `https://www.qic.org/html/standards/11x.x/qic113g.pdf`
- **QIC-3010-MC Rev H / QIC-3020-MC Rev H** — higher-density MFM floppy-tape formats (1 / 2 Mbit/s). `https://www.qic.org/html/standards/301x.x/qic3010h.pdf`, `https://www.qic.org/html/standards/302x.x/qic3020h.pdf`
- **QIC-40-MC Rev M** — the 20-track predecessor QIC-80 extends. `https://www.qic.org/html/standards/4x.x/qic40m.pdf`
- **QIC-122 Rev B** — the Stac LZS compressed-stream format QIC-113 §9 frames (`docs/qic-standards/qic122b.pdf`). **QIC-123** — the VTBL compression codes.
- **ftape** (GPL, Bas Laarhoven) — reference implementation. Mine: `qic117.h` (command set/types/status/errors), `ecc.c`/`ecc.h` (RS), `vendors.h` (`wake_up_*` quirks). HOWTO/FAQ for behavioral notes.
- **Greaseweazle** — `keirf/greaseweazle` (host tools, flux encoding, `gw pin set/get`; its PLL is vendored as `codec/gwpll.py` from v1.23) and `keirf/greaseweazle-firmware` (bare-metal firmware, `at32f4` target, SRAM/USB; v1.6 vendored as `firmware/`). The flux encoding is the GW USB flux protocol; `.scp` is a *disk* container we deliberately do not reuse for tape (§7.1, §7.4).

---

## 11. Glossary

- **(FSD, FTK, FSC)** — flexible-disk side / track / sector; the abused MFM-ID fields (C/H/R) that encode a sector's tape coordinate.
- **LSN / SEG / TPT / TPS** — logical sector number / logical segment / tape track / segment-relative-to-track; the linear coordinates the disk-style IDs resolve to (§7.3 algebra).
- **Serpentine** — the track layout: even tracks written forward, odd tracks reverse, so the head sweeps back and forth across the medium.
- **Segment** — 32 sectors (29 data + 3 ECC); the RS unit, opened on tape by a `C2/FC` index address mark.
- **BSM** — bad-sector map, in the header segment.
- **Lease** — exclusive right to drive the bus; held by one engine, granted by the arbiter.
- **RawFluxCapture / TWRF** — the IO layer's output artifact: GW flux bytes + tape metadata header + in-stream `END`; on disk a `.twrf` file, one per track pass (§7.1, `docs/spec/twrf.md`).
- **TWTI / TWTZ** — the logical tape image: placed, BSM-applied, RS-corrected segments with per-segment state; TWTZ is the same stream through zstd (§7.6, `docs/spec/twti.md`).
- **TWVL** — one extracted backup volume with its holes recorded (§7.7, `docs/spec/twvl.md`).
- **Drive / cartridge / volume profile** — TOML data describing, respectively, how a *drive* must be woken and timed (§6A.3), what physical *cartridge* a header's geometry names, and how the *backup software* laid out its volume table (§7.8).
- **Fixed format** — QIC-80 Rev K format codes 2, 3, 5: fixed segment counts and a per-segment-mask bad-sector map (§7.3).
- **Quiesce funnel** — the single arbiter release path guaranteeing safe teardown.
- **Real-time boundary** — the USB line; below it is hard-real-time and bus-owning, above it is leisurely and semantic.
- **Verbatim passthrough** — firmware emits command numbers as pulse counts without knowing their meaning.
- **Erasure decoding** — RS correction using known bad-sector positions (from CRC), recovering up to 3 sectors/segment.

---

## 12. Toolchain & build

The project is **two artifacts plus a generated contract between them** — firmware (C), host (Python; as built, four packages, §6A.1), and a `protocol` definition that generates code for both ends. Each half uses the toolchain native to it; the firmware half deliberately follows Greaseweazle's so the board stays a first-class GW target rather than a fork to maintain in isolation.

### 12.1 Firmware — extend GW's build, don't reinvent it

*Original plan (superseded below by PlatformIO):* GW firmware builds with the **ARM GNU toolchain via Make**: `gcc-arm-none-eabi`, plus `srecord`, `stm32flash`, `zip`, and a small set of Python packages (`bitarray crcmod pyserial requests`). `make dist` produces `out/<mcu>/<level>/tapewyrm/target.hex` for `<mcu> ∈ {at32f4, stm32f1, stm32f7}` and `<level> ∈ {debug, prod}`.

**The v4.1's AT32F403 is the `at32f4` target.** Our QIC sources (verbs engine, arbiter, free-running flux mode, marker injector) are added into GW's tree and built as the `at32f4` target. Build `prod` for releases; build `debug` during bring-up — it enables **3 Mbaud serial logging**, which is what you want while debugging the report-bit loop and the arbiter lease machine.

- **Pin the toolchain version.** The `gcc-arm-none-eabi` version affects code generation and therefore instruction timing — and this firmware is timing-sensitive (QIC pulse cadence, report-bit windows, flux capture). Pin it; reproduce via container or Nix (§12.6).

**As built — PlatformIO** (`firmware/platformio.ini`; *supersedes* GW's Make build, whose `Makefile`/`Rules.mk` remain in the tree for reference). PlatformIO's `ststm32` platform is used purely as a toolchain and SCons host — **no framework**: `scripts/pio_post.py` throws away the platform's default bare-metal flags and installs exactly the flag set the old `Rules.mk` used, and `scripts/pio_pre.py` adds per-file flags and generates `build_info.c` (the git commit for `BUILD_INFO`). Why: PlatformIO fetches its own **pinned** compiler (`toolchain-gccarmnoneeabi@~1.120301.0`, gcc 12.3 — the platform default would be gcc 7.2.1 from 2017), which settles the pinning bullet above without a container, on every OS. Two envs: `bootloader` (16 KB @ `0x08000000`, the GW-compatible application bootloader) and `tapewyrm` (48 KB @ `0x08004000`, the application); output lands in `firmware/.pio/build/<env>/firmware.{elf,bin,hex}`. Only the AT32F4 (v4.x) target is wired up; the STM32F1/F7 sources are still in-tree but unbuilt. A separate `debug` level with 3 Mbaud serial logging is not wired into PlatformIO.

### 12.2 Host — modern Python, `uv`-centric

PEP 621 `pyproject.toml`, with **uv** for environment, lockfile, and task running (fast, single tool, reproducible lock). Gate: **ruff** (lint + format in one), **mypy** (the design is heavily typed — this pays off), **pytest** + pytest-cov against the hardware-free fixtures (§6A.10). CLI entry via `[project.scripts] tw = "tapewyrm.cli:cli"` (plus a `tapewyrm` long alias). Core deps: `click`, `pyserial`. (Neither `greaseweazle` nor `numpy` is a dependency, hard or optional: the codec is pure stdlib, with GW's PLL vendored (§6A.1). Poetry/PDM are acceptable alternatives; uv is the recommendation.)

*As built:* **one `pyproject.toml` per package** (`packages/*`, hatchling), Python ≥ 3.11, ruff line length 100 with rules `E F I UP B W`, each package with its own `uv.lock` (kept LF by `.gitattributes`, since uv rewrites it). Siblings are editable path sources (`[tool.uv.sources] qiclib = { path = "../qiclib", editable = true }`). Entry points: `tw` / `tapewyrm` = `tapewyrm.cli:cli` (tapewyrm-cli) and `qicsilver` = `qicsilver.cli:cli`. The CLIs depend on `rich` + `rich-click` (and `tw` on `pyserial`); the libraries are stdlib-only apart from `backports.zstd` below 3.14 (§7.6). `greaseweazle` is not needed at all: its PLL is vendored (§6A.1). `hatch_build.py` stamps the git commit into `tw` wheels for `tw info`; editable installs ask git directly.

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
- A **generator** emits both `firmware/.../protocol.h` (C) and `packages/tapewyrm-cli/tapewyrm/link/protocol.py` (Python) from it.
- A **CI job** regenerates and fails on `git diff` — this *mechanically* enforces the no-drift invariant from §5.4 / §6A.2 rather than relying on a human to keep two files aligned.

The protocol version generated here feeds the device capability gate in §6A.2.

*As built:* `protocol/protocol.toml` (version, capability names, transaction opcodes `0x80`–`0x88`, marker codes `0xF0`–`0xF4`, event and end-reason codes) → `protocol/generate.py` → `firmware/inc/protocol.h` and `packages/tapewyrm-cli/tapewyrm/link/protocol.py`; `just gen` regenerates, `just gen-check` (`generate.py --check`) fails if either is stale, and the `contract` workflow runs it. Two gaps remain: the transaction *payload layouts* are still hand-mirrored between `qic.c` and `link/device.py` (§6A.2), and the marker codes are deliberately copied once more into `tapewyrm_archive.twrf` because they are part of a file format (§7.2), with a test pinning the copies together.

### 12.5 Repo layout (monorepo)

```
tapewyrm/
  firmware/        # hard fork of GW firmware (vendored) + Tapewyrm QIC sources
    vendor-seam/   # README: which GW primitives stay pristine (for hand cherry-picks) and why
    attic/         # retired pre-vendor QIC skeleton (arbiter/verbs/markers/...), kept for reference
    src/qic/qic.c  # the QIC graft, #included into src/floppy.c (§13.4)
    platformio.ini # the firmware build (§12.1)
  packages/
    tapewyrm-archive/   # TWRF / TWTI (+TWTZ) / TWVL formats, report decoders, progress hook; stdlib-only
    qiclib/             # QIC layout + backup formats, cartridge & volume profiles; stdlib-only
    tapewyrm-cli/       # `tw`: link, drive, dump, physical decode, convert, flashing
    qicsilver/          # `qicsilver`: TWTI/TWTZ -> identify / extract / tar
  protocol/        # protocol.toml (source of truth) + generate.py
  tools/           # package.py (release builder), ihex.py, clean.py
  docs/            # this document; spec/ (TWS-1..3 format specs); qic-standards/ (the QIC PDFs)
  justfile         # task runner (below)
  STYLE.md         # code conventions + package dependency rules
  .github/workflows/
```

**Fork posture, stated plainly (see §1).** The firmware is a **hard fork** — GW's tree is **vendored**, not submoduled-to-upstream, because the arbiter / verbs / capture changes are structural and the hardware is fixed at v4.1, so tracking upstream buys nothing. The one discipline retained: keep the genuinely-reused GW primitives (flux-capture timing, USB, **bootloader/update protocol**) behind a clean seam so upstream fixes to them can still be cherry-picked by hand, and so `tw flash` (and, still, stock `gw update`) stays a valid flash path. On the host there is effectively **nothing to fork** — GW's tooling is disk-image-centric and does not survive; depend on (or vendor) only the flux-codec / PLL / MFM primitives and write the rest as Tapewyrm. If those primitives need patching, vendor that slice rather than carrying a fork of the whole GW host package.

**The bootloader is the neutral substrate — keep the board a dual citizen.** Leaving GW's bootloader and update protocol untouched means the *same board* flashes a stock Greaseweazle image **or** a Tapewyrm image with one flash each (`tw flash` for Tapewyrm; stock `gw update` still works too, since the protocol is wire-compatible); the application that lands decides what the board is that session. Worth protecting for three reasons beyond tidiness:
1. **Instant revert to stock** for bench debugging — rule out your own firmware by flashing GW, confirm the drive/cabling/hardware behaves, flash back. No DFU jumper, no ArteryISP, no un-brick ritual.
2. **A/B against a known-good flux engine** — stock GW reading a *real floppy* is the reference for whether a capture fault is yours or the silicon's: same board, same USB, same flux primitives, firmware known-good. If GW reads a floppy clean and Tapewyrm's capture is garbage, the bug is yours.
3. **The hardware keeps its resale/reuse value** — it is still a Greaseweazle when you are done, not a single-purpose brick.

The protecting invariant is narrow: **do not touch the bootloader flash region, the application entry vector, the DFU strap, or the USB VID/PID + update-mode commands/protocol** — keep all of that wire-compatible with the GW bootloader (so both `tw flash` and stock `gw update` drive it; detection keys off the VID/PID). The human-readable USB *product string* is rebranded to `Tapewyrm` and the boot banner says Tapewyrm (both cosmetic — they don't affect wire-compat); the update-mode bootloader itself stays the GW substrate. Diverge as hard as you like *above* the application entry point; the boot/update layer is the one place the fork stays faithful.

### 12.6 Task runner — `justfile`

A `just` recipe set coordinates the heterogeneous build. The Python helper scripts
carry **PEP 723 inline metadata** (`# /// script … # ///`) declaring their deps, and
are run with **`uv run`**, so e.g. `crcmod` (needed for the `.upd` CRCs) is fetched
automatically — no manual venv, no system `srecord`/`crcmod`/`zip`. The recipes as built:

| Recipe | Does |
|---|---|
| `just gen` / `just gen-check` | regenerate `protocol.h` + `protocol.py` / fail if they are stale (§12.4) |
| `just package` | the whole project as one package: host wheels + at32f4 firmware → `dist/` |
| `just fw [mcus]` | firmware images only, via PlatformIO, pure-Python HEX merge |
| `just fw-dist` | firmware release: every PlatformIO-wired MCU + a combined `.upd` (no host wheel) |
| `just flash [image]` | `tw flash` via the GW-compatible application bootloader (default `firmware/.pio/build/tapewyrm/firmware.bin`) |
| `just dfu [bin]` | `tw dfu`: recovery flash via the DFU header + AT32 ROM bootloader |
| `just check PKG MODULE` | one package: `uv sync --extra dev`, ruff check, ruff format --check, mypy, pytest |
| `just host` | `check` for all four packages, base first (archive → qiclib → cli → qicsilver) |
| `just test` / `just lint` | tests only / lint + typecheck only, every package |
| `just clean` | remove build, package and cache artifacts (keeps the uv venvs) |
| `just ci` | `gen` + `host`, then `git diff --exit-code` (protocol drift) |
| `just tapewyrm ARGS` / `just qicsilver ARGS` | run `tw` / `qicsilver` from the checkout |

`tools/package.py --dist` reimplements GW's `make dist` portably: it builds the
firmware with PlatformIO (`pio run -e bootloader -e tapewyrm`), merges
bootloader+app with the pure-Python `tools/ihex.py`, and writes a combined `.upd`
via a faithful port of `firmware/scripts/mk_update.py` (validated byte-for-byte by
GW's own `mk_update.py verify`).

### 12.7 CI matrix (GitHub Actions)

Mirror GW's own workflow shape:

- **firmware** — pinned toolchain container; build `at32f4` `prod` + `debug`; upload `target.hex` artifacts.
- **host** — `uv sync` → ruff → mypy → pytest against fixtures. No hardware.
- **contract** — run `protocol/generate.py`; `git diff --exit-code` to fail on drift.
- **hardware-in-the-loop** — manual / self-hosted-runner only (probe + a known tape); never gates PRs.

*As built* (`.github/workflows/`): **`packages.yml`** is the host job as a **matrix over the four packages** (sync, ruff check, ruff format --check, mypy, pytest with coverage), triggered by any change under `packages/` since the siblings are editable dependencies of each other; **`contract.yml`** regenerates the protocol and fails on drift; **`firmware.yml`** builds the at32f4 image with `tools/package.py --skip-host`; **`package.yml`** builds the full release (`--dist`). Hardware-in-the-loop remains manual. *(Known gap: the two firmware workflows still install the apt ARM toolchain for the old Make build, but `tools/package.py` now needs PlatformIO's `pio`.)*

### 12.8 Reproducibility tiers

1. **Minimum:** pin `gcc-arm-none-eabi`; commit the uv lockfile. *(As built: PlatformIO pins the compiler package in `platformio.ini`, and every Python package commits its own `uv.lock`.)*
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
| 3 | Pause | MOT | — | referenced (not Ready) | n | 16 |
| 4 | Micro Step Pause | MOT | — | referenced (not Ready) | n | 16 |
| 5 | Alternate Command Time-out | CFG | — | — | — | 0 |
| 6 | Report Drive Status | RPT | — | — | — | 2.5 ms ack |
| 7 | Report Error Code | RPT | — | Ready | — | 2.5 ms ack |
| 8 | Report Drive Configuration | RPT | — | — | — | 2.5 ms ack |
| 9 | Report ROM Version | RPT | — | — | — | 2.5 ms ack |
| 10 | **Logical Forward** | STREAM | — | yes | — | tape-len/speed (≤~650) |
| 11 | Physical Reverse | MOT | — | Ready + cartridge (not referenced) | h | ≤650 |
| 12 | Physical Forward | MOT | — | Ready + cartridge (not referenced) | h | ≤650 |
| 13 | Seek Head to Track | MOT | `Track+2` | yes | — | 15 |
| 14 | Seek Load Point | MOT | — | Ready + cartridge | n | ≤670 (~30 s on the bench, even from BOT) |
| 15 | Enter Format Mode | MOD | — | Ready + cartridge, not write-protected | — (host refuses) | 0 |
| 16 | Write Reference Burst | MOT | — | format | n | 940 |
| 17 | Enter Verify Mode | MOD | — | yes | — | 0 |
| 18 | Stop Tape | MOT | — | — | n | 8 |
| 21 | Micro Step Head Up | MOT | — | — (illegal in F/N/H modes) | — | 200 ms |
| 22 | Micro Step Head Down | MOT | — | — (illegal in F/N/H modes) | — | 200 ms |
| 23 | Soft Select | SEL | **20 literal pulses** | — | — | 0 |
| 24 | Soft Deselect | SEL | — | — | — | 0 |
| 25 | Skip N Segs Reverse | MOT | `(N&15)+2, (N≫4)+2`, N ≤ 255 | referenced (not Ready) | n | ≤650 |
| 26 | Skip N Segs Forward | MOT | `(N&15)+2, (N≫4)+2`, N ≤ 255 | referenced (not Ready) | n | ≤650 |
| 27 | Select Rate or Format | CFG | `N+2` (rate or format) | — | — | 0 |
| 28 | Enter Diag Mode 1 | MOD | `28` (sent twice) | — | manufacturer-dependent; host refuses | — |
| 29 | Enter Diag Mode 2 | MOD | `29` (sent twice) | — | manufacturer-dependent; host refuses | — |
| 30 | Enter Primary Mode | MOD | — | — | — | 0 |
| 32 | Report Vendor ID | RPT | — | — | — | 2.5 ms ack |
| 33 | Report Tape Status | RPT | — | cartridge | — | 2.5 ms ack |
| 34 | Skip N Ext Reverse | MOT | 3 nibbles, each `+2` | yes | n | tape-len/speed |
| 35 | Skip N Ext Forward | MOT | 3 nibbles, each `+2` | yes | n | tape-len/speed |
| 36 | Calibrate Tape Length | MOT | — | yes | n | ~1300 |
| 37 | Report Format Segments | RPT | — | — | — | 2.5 ms ack |
| 38 | Set N Format Segments | CFG | 3 nibbles, each `+2` | — | — | 0 |
| 46 | Phantom Select | SEL | `Unit+2` (**required**: a bare 46 is ignored; Colorado Jumbo 350 = unit 0) | — | — | 0 |
| 47 | Phantom Deselect | SEL | — | — | — | 0 |

(19–20, 39 reserved; 31, 40–45 vendor-unique.) Codes >32 unsupported by a drive are ignored; codes <32 that are undefined raise "undefined command." A command pulse-train >32 pulses is ignored.

**Argument encoding.** `Rate` (cmd 27): 0=4 Mbps-or-250 kbps, 1=2 Mbps, 2=500 kbps, 3=1 Mbps. `Format` (cmd 27): `(tape_format×4)+increment`, tape_format 1=QIC-40 2=QIC-80 3=QIC-3020 4=QIC-3010, increment 1=standard 3=wide(8 mm).

**Report payloads (data bits between ACK and Final, LSB-first).**
- `6` Drive Status (8b): 0 ready · 1 error · 2 cartridge-present · 3 write-protect · 4 new-cartridge · 5 referenced · 6 at-BOT · 7 at-EOT. (bits 1,5,6,7 valid only when ready.)
- `7` Error Code (16b): bits 0–7 error code, 8–15 associated command (0 = process error, 1 = initialization error). Undefined unless Error Detected + Ready; otherwise the drive repeats the last code. Resets are 26 (power-on), 27 (soft), 41 (wakeup) -- not 1.
- `8` Drive Config (8b): bits 3–4 rate (00=4M/250k, 01=2M, 10=500k, 11=1M) · 6 extra-length · 7 QIC-80-mode.
- `9` ROM Version (8b): 0–6 version, 7 beta.
- `32` Vendor ID (16b): 0–5 model, 6–15 make. Exception: Colorado's legacy whole-word ID 71 (`0x0047`, seen on the Jumbo 350) -- Rev J lists Colorado as "4 & 71".
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

Layered on GW's USB CDC-ACM transport and **GW's own command packets** (the verbs are grafted onto GW's `process_command()`): request `{cmd:u8, total_len:u8, payload}` (total_len includes the 2-byte header), response `{cmd_echo:u8, ack:u8, payload}` with the payload sent **only when ack == OKAY** and **no length field** -- the host knows each command's response size, as GW's own tools do. A response is one 64-byte USB packet, so every verb's response must fit in 64 bytes. (An earlier draft specified u16-length frames with JSON payloads; the firmware never implemented that, and the host now matches the firmware.) Opcodes/marker codes live in the generated `protocol.h`/`protocol.py` (§12.4); payload layouts are mirrored by hand between `qic/qic.c` and `link/device.py` for now.

| Transaction | Request payload | Response | Notes |
|---|---|---|---|
| `INFO` | — | `{proto_ver:u8, caps:u32 bitmask, sram:u32, sample_hz:u32}` | capability gate; stock GW answers BAD_COMMAND. Board identity comes from GW `GET_INFO` |
| `SET_TIMING` | `{pulse_us, inter_pulse_us, terminate_gap_us, tack_us, tbit_us:u16; report_on_index:u8}` | — | idle-only; pushed by `Qic117Drive.wake()` |
| select | GW-native `SET_BUS_TYPE` + `SELECT` (+ `MOTOR`) | — | no Tapewyrm verb (0x85 is unimplemented); phantom drives want every DS idle |
| `COMMAND_TXN` | `{cmd_n:u8, report_bits:u8}` | `{flags:u8 (b0 ack, b1 final, b2 timed-out), bits:u16, nbits:u8}` | verbs engine; host raises on missing ACK/Final |
| `WAIT_READY` | `{timeout_s:u16}` | `{status:u8}` -- **0 = ready**, 1 = timed out | waits for cue INDEX (see §5.3) |
| `CAPTURE` | `{motion_n:u8, rate:u16, tpt:u16, direction:u8, pass_id:u16, byte_budget:u32}` | `{echo, ack}` then **stream** | stream = GW flux bytes + markers; byte_budget 0 = free-run |
| `SCOPE` | `{cmd_n:u8, duration_ms:u16}` | `{initial:u8, n_edges:u8, overflow:u8, counts:4×u16, edges:n×{t_us:u32, state:u8}}` (≤10 edges) | bench probe: edge-log TRK0/INDEX/WRPROT/pin34 after optional pulses |
| `BUILD_INFO` | — | `{commit:40 bytes ASCII hex (zero-filled if unknown), dirty:u8}` | build identity for `tw info`; `firmware/scripts/pio_pre.py` generates it; older images answer BAD_COMMAND |
| `ABORT`/`STOP` | — (**out-of-band control**, not queued) | — | valid during CAPTURE; routes through Quiesce |

**Capture stream** = verbatim GW flux bytes interleaved with opcode-escape **markers** (§7.2): `SESSION_START{rate, clock, TPT, direction, pass_id, utc}` · `SEGMENT{ticks, index}` (per hardware INDEX edge) · `EVENT{code}` · `END{reason, flux_count, byte_count, checksum}`. Backpressure: sustained overflow → `EVENT{overflow}` + clean abort (never silently drop). USB suspend/disconnect → device-side dead-man → Quiesce stop.

### 13.4 Module manifest (generate these)

**Firmware** — added *above the application entry point* in the vendored GW tree; flux timer/DMA, USB CDC, bootloader, AT32 clock/GPIO, and linker map are **reused untouched** (§12.5).

*Original plan (superseded — this skeleton now lives in `firmware/attic/qic_skeleton/`):*

| File | Responsibility | Key entry points |
|---|---|---|
| `qic/arbiter.{c,h}` | lease state machine (§5.2); owns drive-select; Quiesce funnel; watchdog + USB-loss dead-man | `arb_grant`, `arb_quiesce`, `arb_on_usb_loss` |
| `qic/verbs.{c,h}` | pulse emit (cadence), N+2 arg emit, report clock (ACK/bits/Final), wait-ready (§5.3) | `qic_pulses`, `qic_arg`, `qic_report`, `qic_wait_ready` |
| `qic/flux_capture.{c,h}` | free-running capture (extends GW flux read): arm/disarm, RDATA→encode→ring→USB, SEGMENT on INDEX, overflow→EVENT+abort (§5.4) | `cap_arm`, `cap_disarm`, `cap_on_index` |
| `qic/markers.{c,h}` | marker injection on opcode channel; END accounting (counts+checksum) | `mk_session_start`, `mk_segment`, `mk_event`, `mk_end` |
| `qic/transactions.{c,h}` | USB transaction dispatch (§5.1/§13.3) hooked into GW's handler; `INFO` capability flag | `txn_dispatch` |
| `protocol.h` | **generated** (§12.4): opcodes, marker codes, version | — |

*As built:* one file, **`firmware/src/qic/qic.c`**, `#include`d into GW's `src/floppy.c` so it can bind to GW's `static` primitives directly (`write_pin(step,…)`, `get_trk0()`/`get_index()`, `delay_us`, the `tim_rdata`/`dma_rdata` front end, `rdata_encode_flux()`, `floppy_read_prep()`, the `u_buf[]` ring, `ST_read_flux`). The skeleton's separate modules fought that in-place reuse — every GW primitive had to be re-declared as an `extern` stub — so they were retired. The roles survive as sections of `qic.c`:

| Role (skeleton file) | As built in `src/qic/qic.c` |
|---|---|
| verbs (`verbs.c`) | `qic_pulses`, the N+2 argument trains, the report clock (fixed settle or INDEX cue), wait-ready on cue INDEX (§5.3) |
| capture (`flux_capture.c`) | `qic_cmd_capture` → `qic_capture_arm` → GW's flux read with no index or tick limit; hooks in `floppy.c` call `qic_capture_on_index`, the byte-budget and end-of-motion checks (§5.4) |
| markers (`markers.c`) | `qic_mark_session_start` / `_segment` / `_event` / `_end`, written into `u_buf[]` beside GW's own opcodes; `END` carries flux count, byte count, checksum |
| arbiter (`arbiter.c`) | GW's state machine as the lease; `qic_capture_finish` / `qic_capture_abort_silent` as the Quiesce funnel (§5.2) |
| transactions (`transactions.c`) | `CMD_QIC_*` cases in GW's `process_command` (`INFO`, `SET_TIMING`, `COMMAND_TXN`, `WAIT_READY`, `CAPTURE`, `SCOPE`, `BUILD_INFO`) |
| `protocol.h` | **generated** into `firmware/inc/` (§12.4) |

**Host** — all pure above the USB line except `link`; regenerated from the tree (`packages/*`, §6A.1). Hardware package **`tapewyrm-cli`** (`tapewyrm/`):

| File | Responsibility |
|---|---|
| `cli/` | rich-click `tw`, one module per command group: `app.py` (`AppContext`, config, global flags, §6A.7), `session.py` (drive session, report decoders), `drive.py` (`drive …`), `dump.py`, `convert.py`, `info.py`, `firmware.py` (`flash`, `dfu`) |
| `console.py` | rich stderr console shared by logging and progress bars (copy kept in step with qicsilver's) |
| `types.py` | `DeviceInfo`, `TimingParams`, `SelectHint`, `StopCond`, `ErrorCode`, `DriveProfile`, `FluxStream`, `RecoveryReport` |
| `buildinfo.py` (+ `hatch_build.py`) | which commit this `tw` was built from |
| `link/transport.py` | `SerialTransport` (pyserial) + `FakeTransport`; GW command-packet framing |
| `link/protocol.py` | **generated**: opcode/marker enums, version |
| `link/device.py` | `DeviceLink` typed RPC (§13.3), capability gate, `LinkError` tree |
| `link/update.py` | `tw flash` (app bootloader) / `tw dfu` (`dfu-util`) |
| `qic117/commands.py` | command table (§13.1) as `Cmd` records; N+2 arg encoder |
| `qic117/drive.py` | `Qic117Drive` (§6A.3): dispatch by kind, Ready polling, reports, wake, write-path refusal |
| `qic117/status.py` | error classification (fatal/benign); re-exports the archive's report decoders |
| `qic117/profile.py` | `DriveProfile` TOML loader |
| `tape/dump.py` | `tw dump`: drive identity, wind to track start, pass capture, health check (§6.3) |
| `tape/fluxprobe.py` | `tw drive flux` diagnostic |
| `codec/gwstream.py` | real device stream → intervals, INDEX times, markers; `END` verification |
| `codec/gwpll.py` | vendored Greaseweazle PLL: intervals → bitcells |
| `codec/mfm.py` | bitcells → bytes at sync marks; ID/data fields, CCITT CRC → `RawSector`; encoders for fixtures |
| `codec/flux.py`, `codec/pipeline.py` | the original synthetic-fixture pipeline (tests) |
| `image/convert.py` | `tw convert`: per-capture decode with stage logging, then `qiclib.build` |
| `profiles/drive/*.toml` | per-drive `DriveProfile` data |

Library **`qiclib`**, library **`tapewyrm-archive`** and CLI **`qicsilver`**: see the annotated tree in §6A.1 — one line per module there.

### 13.5 Decode stack (one ladder, with the module at each rung)

*Original ladder (superseded module names; the rungs are unchanged):*

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

*As built*, with the files between the rungs:

```
device stream        tape.dump → TWRF (tapewyrm_archive.twrf)                    tw dump
  → intervals        codec.gwstream     GW encoding + markers; checked against END   ┐
  → bitcells         codec.gwpll        vendored GW PLL at the capture's own rate      │ tw convert
  → MFM sectors      codec.mfm          C2/FC · A1A1A1 FE id · A1A1A1 FB/F8 data · CRC  │ (tapewyrm-cli)
  → merged sectors   qiclib.merge       multi-pass union, CRC-good copy wins          ┐│
  → header           qiclib.volume      locate_header → geometry (tracks, spt, ftk/side)││
  → placed sectors   qiclib.place       (FSD,FTK,FSC) → (TPT,TPS,sec); bad IDs dropped ││ (qiclib.build)
  → segments         qiclib.segment     BSM exclusions, erasure mask, repack           ││
  → corrected data   qiclib.rs          GF(256) erasure decode (§13.2)                 ┘┘
  → TWTI / TWTZ      tapewyrm_archive.twti   per-segment state + 29 KB slots (§7.6)
  → volume table     qiclib.volume_profile   which software's VTBL layout (§7.8)      ┐ qicsilver extract
  → volume bytes     qiclib.extract + qiclib.qic122   QIC-122 extents, holes          ┘ (qiclib)
  → TWVL             tapewyrm_archive.twvl   bytes + holes + directory_offset (§7.7)
  → files            qiclib.qic113 / qic113ext → qicsilver.tar   tree → pax tar + damage report
```
The multi-pass union still sits before RS; the header is now located *before* placement, because placement needs the header's geometry (§7.3). Every rung below the first is offline and pure.

### 13.6 Oneshot scope — what generates cleanly vs what needs the bench

**Generates directly from this document (no hardware):**
- The **entire `codec/*` tree** — §7.3/§7.5/§13.2/§13.5 are complete; test against the §13.2 RS vector, synthesized `RawFluxCapture` fixtures, and ftape cross-checks. *(As built: split into `tapewyrm-cli`'s `codec/` and `qiclib`, §6A.1.)*
- **Host control layers** (`link/`, `qic117/`, `tape/`, `cli/`; `rawflux/` became `tapewyrm_archive.twrf`) — §6A + §13.1 + §13.3 + §13.4.
- **Firmware** — Greaseweazle v1.6 is **vendored complete in-tree** (`firmware/`, built with PlatformIO's pinned ARM GNU toolchain, §12.1) and the QIC layer is grafted onto GW's control loop: `src/qic/qic.c` is `#include`d into `src/floppy.c`, adding `CMD_QIC_*` (= the generated `TW_TXN_*`) cases to `process_command` and reusing GW's flux-read engine for free-running capture (§5, §13.3).
- The **protocol codegen** and `justfile`/CI (§12).

**Needed a bench / a real drive before it ran (the genuine seams, §9)** — numbering is stable, code cites these items:
1. ~~**Host-side GW flux opcode reconciliation**~~ — **resolved.** The firmware reuses GW's real flux encoder + `0xFF` opcode escape (tape markers ride codes `0xF0–0xF4`); the host side is `tapewyrm_archive.twrf.parse_body` (re-exported as `codec/gwstream.parse`), a full decoder of that stream including GW's own `FLUXOP_*` opcodes, validated against the first real capture's `END` counts and checksum (§9 item 1). The older escape-and-stuff framing in `twrf` is gone (2026-10-03); `codec.flux` is now only the decoded-bytes fixture path.
2. ~~GW firmware integration points~~ — **resolved by vendoring** (firmware now builds in-tree): QIC verbs are grafted into GW's `process_command`, capture reuses `floppy_read`/`rdata_encode_flux` free-running, and host-stop maps onto GW's `BAUD_CLEAR_COMMS` out-of-band path. Hardware validation happened on the bench: the Colorado drives command, report and stream (§9 items 3–5).
3. **The target `DriveProfile`** — wake timing, CCS level, quirks; characterize via `gw pin` + scope. *Done for the Colorado Jumbo 350 and 1400* (§9 item 3); other drives open.
4. **v4.1 schematic confirm** + **34-pin open-collector** assumption. *Functionally confirmed*; the scope-level check is still open (§9 items 4–5).
5. ~~**STAC**~~/DCLZ **decompressor** for compressed volumes — **QIC-122 (Stac LZS) done** (`qiclib.qic122`, §7.5); DCLZ/ALDC open.

The host codec + control stack are implemented and the Greaseweazle firmware is **vendored complete in-tree** with the QIC graft building; the items above are where progress needed a real drive (timing / profile / schematic validation) or the one host-side decode tweak. They are marked `TODO(bench)` at their sites. *As of this revision* the whole read path has run on real hardware and recovered real tapes; what remains open is collected in §9.
