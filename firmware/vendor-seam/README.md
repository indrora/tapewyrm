# Vendor seam — the Greaseweazle primitives kept pristine

Tapewyrm is a **hard fork** of Greaseweazle (GW) firmware: GW v1.6 is **vendored
complete in-tree** (it builds here — see `../README.md`), not tracked against
upstream, and the QIC verbs / capture changes are structural (DESIGN.md §1,
§12.5). Mergeability with upstream is **not** a goal. The one discipline that
*replaces* upstream-tracking is this seam: a set of GW primitives is kept
**pristine** so that

1. future upstream fixes to those primitives can still be cherry-picked **by
   hand**, and
2. the bootloader / update protocol stays **wire-compatible** so Tapewyrm's own
   flashing client (`tw flash`) can drive it — and stock `gw update` keeps
   working on the same board too. (Tapewyrm ships only `tw`; it never requires
   the `gw` executable — DESIGN.md §1.)

## How the reuse actually works (no `gw_stubs.h`)

Tapewyrm **reuses GW's control loop and flux-recording engine in place**, rather
than re-implementing them behind an abstraction. The QIC code in
`src/qic/qic.c` is `#include`d directly into `src/floppy.c` and binds to GW's
real `static` primitives — `write_pin(step,…)`, `get_trk0()`/`get_index()`,
`delay_us`, `time_now()`/`time_since()`, `watchdog_kick()`, the `tim_rdata`/
`dma_rdata` flux front-end, `rdata_encode_flux()`, `floppy_read_prep()`, the
`u_buf[]` ring and `ST_read_flux` state machine.

There is intentionally **no `gw_stubs.h` `extern` seam** in the build. (An
earlier, pre-vendor skeleton declared every GW primitive as a `gw_*` `extern`
stub so the QIC modules could syntax-check standalone; that abstraction fought
the in-place-reuse model and has been retired to `attic/qic_skeleton/`. Its logic
was carried forward and rebound to the real primitives in `src/qic/qic.c`.) The
seam is now conceptual rather than a header full of stubs: it is the *set of GW
functions the QIC graft is allowed to call*, plus the *boot/update layer it must
not touch*.

## What is kept pristine

| GW primitive | Why it's reused, not rewritten | Where the graft binds to it |
|---|---|---|
| **Flux-capture engine** (input-capture timer + DMA front-end, the GW flux byte encoder, the read state machine) | The timing-critical primitive Tapewyrm depends on; re-deriving it would throw away the hardest-to-get-right, battle-tested code (§1, §4.1 decision 3). Tapewyrm only drives it **free-running** (no index limit / no tick deadline) instead of revolution-gated (§5.4), and brackets the stream with typed markers. | `src/floppy.c`: `floppy_read_prep`, `rdata_encode_flux`, `tim_rdata`/`dma_rdata`, `ST_read_flux`, `u_buf[]`, the `FLUXOP_*`/`0xFF` opcode escape |
| **USB CDC-ACM stack** (enumeration, bulk/CDC transport, the EP0 control / `BAUD_CLEAR_COMMS` clear-comms abort) | The real-time boundary (§4). Reused unchanged; the QIC command set is layered on GW's existing command framing, and the host stops a capture via GW's existing out-of-band clear-comms path. | `src/usb/`, `usb_write`/`ep_tx_ready` via `floppy_read`, `floppy_configure` |
| **Bootloader + `gw update` protocol** | The neutral substrate that keeps the board a **dual citizen** (see below). | NOT touched by any QIC code — it lives below the application entry point |
| **AT32 clock / GPIO init, board pin map, linker map, startup, watchdog** | Board bring-up; reused untouched (§13.4). | `src/mcu/at32f4/`, `src/board.c`, `write_pin`/`get_*` pin macros, `delay_us`, `time_now`, `watchdog_kick` |

The QIC graft **never** touches a hardware register directly — every hardware
dependency goes through GW's existing macros/functions above. That is the seam in
practice; this README is its rationale.

## The protecting invariant (DESIGN.md §12.5)

> **Do not touch the bootloader flash region, the application entry vector, the
> DFU strap, or the USB update-mode descriptors / commands.** Keep all of that
> wire-compatible with `gw update`.

Diverge as hard as you like *above* the application entry point — the QIC verbs
and capture are added freely into `process_command()` and the flux read path —
but the boot/update layer is the one place the fork stays faithful. Concretely,
for any change:

- **Never** relocate or resize the bootloader flash region.
- **Never** change the application entry vector the bootloader jumps to.
- **Never** alter the hardware DFU strap behavior or the AT32 ROM-bootloader path
  (the un-brick route, §12.3).
- **Never** change the **USB VID/PID** or the update-mode commands/protocol that
  `gw update` speaks to — detection and flashing key off the VID/PID, so they must
  stay. (The human-readable USB *product string* IS rebranded to `Tapewyrm`; that
  is cosmetic and does not affect wire-compatibility. Tapewyrm's *application-mode*
  command numbers — the `CMD_QIC_*` verbs at `0x80+`, i.e. the generated `TW_TXN_*`
  — are likewise separate and may differ freely.)

## Why protect it — three reasons (DESIGN.md §12.5)

Leaving GW's bootloader and update protocol untouched means the **same board**
flashes a stock Greaseweazle image **or** a Tapewyrm image with one flash each
(`tw flash` for Tapewyrm; stock `gw update` still works too); the application
that lands decides what the board is that session. Worth protecting for three
reasons beyond tidiness:

1. **Instant revert to stock for bench debugging.** Flash stock GW to rule out
   your own firmware: confirm the drive / cabling / hardware behaves, then flash
   back. No DFU jumper, no ArteryISP, no un-brick ritual.
2. **A/B against a known-good flux engine.** Stock GW reading a *real floppy* is
   the reference for whether a capture fault is yours or the silicon's — same
   board, same USB, same flux primitives, firmware known-good. If GW reads a
   floppy clean and Tapewyrm's capture is garbage, the bug is yours. (And because
   the capture *is* GW's read engine driven free-running, the two share almost
   all of the same code, making the comparison especially tight.)
3. **The hardware keeps its resale / reuse value.** It is still a Greaseweazle
   when you are done, not a single-purpose brick.

## Cherry-picking upstream flux fixes

Because the reused flux primitives are GW's own functions called in place (not
copies behind a stub), an upstream GW fix to the flux engine is applied by:

1. updating the vendored GW source in place (`src/floppy.c` /
   `src/mcu/at32f4/floppy.c` — the flux read engine), then
2. re-checking that the QIC graft hooks in `src/floppy.c`
   (`qic_capture_on_index`, `qic_capture_account_range`, the budget/finish/
   session-start hooks) still line up with the patched read path — the hooks are
   small and clearly commented (`/* Tapewyrm: … */`), so an upstream rebase of
   the flux engine only needs those few call sites re-checked.

If a reused primitive needs *Tapewyrm-specific* patching beyond the hooks, make
the change in the vendored GW source and note the divergence here, rather than
carrying a fork of a wider slice of the tree.
