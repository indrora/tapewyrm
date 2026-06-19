# Vendor seam — the Greaseweazle primitives kept pristine

Tapewyrm is a **hard fork** of Greaseweazle (GW) firmware: GW's tree is
*vendored*, not tracked against upstream, and the arbiter / verbs / capture
changes are structural (DESIGN.md §1, §12.5). Mergeability with upstream is **not**
a goal. The one discipline that *replaces* upstream-tracking is this seam: a
small set of GW primitives is kept **pristine and isolated** so that

1. future upstream fixes to those primitives can still be cherry-picked **by
   hand**, and
2. the bootloader / update protocol stays **wire-compatible** so Tapewyrm's own
   flashing client (`tw flash`) can drive it — and stock `gw update` keeps
   working on the same board too. (Tapewyrm ships only `tw`; it never requires
   the `gw` executable — DESIGN.md §1.)

Everything *above* the application entry point is owned outright and may diverge
as hard as needed. This directory documents what stays faithful and why.

## What is kept pristine

| GW primitive | Why it's reused, not rewritten | Where it's bound |
|---|---|---|
| **Flux-capture timing skeleton** (input-capture timer + DMA front-end, flux byte encoding) | The timing-critical primitive Tapewyrm depends on; re-deriving it would throw away the hardest-to-get-right, battle-tested code (§1, §4.1 decision 3). Tapewyrm only changes it to run **free-running** instead of revolution-gated (§5.4). | `qic/gw_stubs.h`: `gw_flux_*`, `gw_flux_encode_interval`, `gw_flux_opcode_escape` |
| **USB CDC-ACM stack** (enumeration, bulk/CDC transport) | The real-time boundary (§4). Reused unchanged; Tapewyrm layers its transaction opcodes on top of GW's framing (§13.3). | `qic/gw_stubs.h`: `gw_usb_*` |
| **Bootloader + `gw update` protocol** | The neutral substrate that keeps the board a **dual citizen** (see below). | NOT touched by any QIC source — it lives below the application entry point. |
| **AT32 clock / GPIO init, linker map, startup** | Board bring-up; reused untouched (§13.4). | `qic/gw_stubs.h`: `gw_pin_*`, `gw_delay_us`, `gw_now_us`, `gw_watchdog_kick` |

The QIC sources **never** touch a hardware register directly — every hardware and
GW dependency goes through the `gw_*` seam declared in `qic/gw_stubs.h`. At
vendoring time those `extern` stubs are bound to the real GW functions named in
each stub's `TODO(bench)` comment. That single header *is* the seam in code form;
this README is its rationale.

## The protecting invariant (DESIGN.md §12.5)

> **Do not touch the bootloader flash region, the application entry vector, the
> DFU strap, or the USB update-mode descriptors / commands.** Keep all of that
> wire-compatible with `gw update`.

Diverge as hard as you like *above* the application entry point; the boot/update
layer is the one place the fork stays faithful. Concretely, for any change:

- **Never** relocate or resize the bootloader flash region.
- **Never** change the application entry vector the bootloader jumps to.
- **Never** alter the hardware DFU strap behavior or the AT32 ROM-bootloader path
  (the un-brick route, §12.3).
- **Never** change the USB descriptors, VID/PID, or update-mode commands that
  `gw update` speaks to. (Tapewyrm's *application-mode* transaction opcodes are
  separate and may differ freely — §13.3.)

## Why protect it — three reasons (DESIGN.md §12.5)

Leaving GW's bootloader and update protocol untouched means the **same board**
flashes a stock Greaseweazle image **or** a Tapewyrm image with one flash each
(`tw flash` for Tapewyrm; stock `gw update` still works too); the application that
lands decides what the board is that session. Worth protecting for three reasons
beyond tidiness:

1. **Instant revert to stock for bench debugging.** Flash stock GW to rule out
   your own firmware: confirm the drive / cabling / hardware behaves, then flash
   back. No DFU jumper, no ArteryISP, no un-brick ritual.
2. **A/B against a known-good flux engine.** Stock GW reading a *real floppy* is
   the reference for whether a capture fault is yours or the silicon's — same
   board, same USB, same flux primitives, firmware known-good. If GW reads a
   floppy clean and Tapewyrm's capture is garbage, the bug is yours.
3. **The hardware keeps its resale / reuse value.** It is still a Greaseweazle
   when you are done, not a single-purpose brick.

## Cherry-picking upstream flux fixes

Because the reused flux primitives are reached only through `gw_stubs.h`, an
upstream GW fix to the flux engine is applied by:

1. updating the vendored GW flux source in place (it lives in the GW tree, not
   here), then
2. re-checking that the `gw_flux_*` seam signatures in `gw_stubs.h` still hold
   (adjust the thin adapter if a signature changed).

If a reused primitive needs *Tapewyrm-specific* patching, vendor **that slice**
and note the divergence here, rather than carrying a fork of the whole GW tree.
