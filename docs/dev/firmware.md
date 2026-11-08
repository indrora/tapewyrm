# Firmware

The Tapewyrm firmware is Greaseweazle (GW) v1.6 firmware, vendored complete in
`firmware/`, with a QIC-117 graft added. It runs on the Greaseweazle v4.1
(AT32F403A, Cortex-M4). This page covers how the graft is wired into GW, the
layout of `firmware/`, building with PlatformIO, the vendor seam and what is
still `TODO(bench)`. To flash a board, see
[Flash the Greaseweazle](../guide/flash-greaseweazle.md).

## How the graft works

Tapewyrm does not ship a parallel application. It reuses GW's control loop and
flux-recording engine in place and adds the QIC functions to them.

- **GW's command loop is reused.** The QIC verbs are new `case` entries in GW's
  `process_command()` switch in `src/floppy.c`, dispatched by the same
  `floppy_process()` loop in `src/main.c`.
- **GW's flux read is the capture engine.** A free-running tape capture is a GW
  flux read with no index limit and no tick deadline. It uses the same
  `tim_rdata`/`dma_rdata` input-capture front end, the same
  `rdata_encode_flux()` encoder, the same `ST_read_flux` state machine and
  the same `u_buf[]` USB pump. The INDEX edge GW records once per revolution
  on a floppy is, on tape, the start of a segment.
- **GW's primitives are called directly.** STEP is `write_pin(step, ...)`;
  TRK0 and INDEX are `get_trk0()` and `get_index()`; timing is `delay_us`,
  `delay_ms`, `time_now()` and `time_since()`; plus `act_led()` and the
  watchdog. The graft never touches a hardware register itself.

GW's flux engine and pin macros are `static` inside `src/floppy.c`. So the QIC
code, `src/qic/qic.c`, is `#include`d into `src/floppy.c` immediately before
`process_command()`, where it can see those statics. It is not a separate
translation unit and does not build on its own. SCons, under PlatformIO,
picks it up as a prerequisite of `floppy.o`, so an edit to it rebuilds.

```text
src/floppy.c
  ├─ #include "protocol.h"              generated opcodes and marker codes
  ├─ forward declarations of the QIC hooks
  ├─ rdata_encode_flux()                ─ hook ─► qic_capture_on_index()          SEGMENT marker
  │                                     ─ hook ─► qic_capture_account_range()     END accounting
  ├─ floppy_read()                      ─ hook ─► qic_capture_emit_session_start_if_needed()
  │                                     ─ hook ─► qic_capture_budget_reached() / finish()
  ├─ floppy_configure() / floppy_reset() ─ hook ─► qic_capture_abort_silent()     safe stop
  ├─ #include "qic/qic.c"               the verbs, markers and capture
  └─ process_command()                  ─ new cases ─► CMD_QIC_*
```

Every hook site in GW code is marked with a `/* Tapewyrm: ... */` comment.

### QIC commands

| Command | Code | Does |
|---|---|---|
| `CMD_QIC_INFO` | `0x80` | Device info and the capability gate: protocol version, capability bits, SRAM size, sample clock. |
| `CMD_QIC_SET_TIMING` | `0x81` | Set the QIC-117 pulse and report cadence. Idle only. |
| `CMD_QIC_PULSES` | `0x82` | Emit N STEP pulses verbatim; optionally clock K report bits off TRK0. The host calls it `COMMAND_TXN`. |
| `CMD_QIC_WAIT_READY` | `0x83` | Poll the INDEX/ready line until ready or timeout. |
| `CMD_QIC_CAPTURE` | `0x84` | Issue a motion command verbatim, then arm a free-running flux capture through GW's read path. |
| `CMD_QIC_SCOPE` | `0x87` | Bench logic probe: optional N STEP pulses, then an edge log of the drive's input lines. |
| `CMD_QIC_BUILD_INFO` | `0x88` | The git commit the image was built from and a dirty flag (`tw info`). |

The codes are the generated `TW_TXN_*` values from `inc/protocol.h`. They sit
above GW's own range (0 to 22). Running `gw` against Tapewyrm firmware is not
supported, and nothing in the application layer is kept for its sake.
`0x85` (`SELECT`) and `0x86` (`ABORT`) are reserved but are not cases. Drive
select uses GW's own `SELECT`, `DESELECT` and `MOTOR` commands, and a capture
is stopped out of band through GW's clear-comms baud change. The request and
response layouts are on [Device protocol](protocol.md#verb-payloads).

### Capture markers

The capture stream is GW's flux bytes with Tapewyrm markers on GW's `0xFF`
opcode escape, framed `0xFF code len payload`. GW's own flux opcodes are 1 to
3 and the markers are `0xF0` to `0xF4`, so they never collide. There is no byte
stuffing: `0xFF` appears as data (the second byte of a two-byte interval, the
N28 argument bytes, marker payloads), so a reader must walk the stream token by
token. The host reader is `tapewyrm_archive.twrf.parse_body`; the format is
[TWS-1](../spec/twrf.md).

A `SEGMENT` marker's `ticks` equals the preceding `FLUXOP_INDEX` N28: ticks
from GW's sample cursor (the last transition plus any dead-time SPACE) to the
INDEX edge. It is not the time since the previous segment.

## Layout

```text
firmware/
  platformio.ini        the build: envs `tapewyrm` (app) and `bootloader`, at32f4 only
  boards/               PlatformIO board definition for the v4.1
  scripts/pio_pre.py    per-file flags; generates tw_git.h (commit + dirty flag)
  scripts/pio_post.py   replaces the platform's flags with GW's own
  scripts/mk_update.py  GW's .upd builder (tools/package.py carries a port of it)
  Makefile, Rules.mk    GW's original make build; superseded, unused
  inc/
    protocol.h          GENERATED: opcodes, marker codes, version. Do not edit.
    cdc_acm_protocol.h  GW command set and flux encoding (FLUXOP_*, the opcode escape)
    ...                 other GW headers
  src/
    floppy.c            GW flux engine, the graft hooks and the CMD_QIC_* cases
    main.c              GW init and the floppy_process() main loop
    build_info.c        firmware version and the Tapewyrm git commit
    qic/qic.c           THE GRAFT: verbs, markers, free-running capture
    mcu/, usb/, ...     GW MCU, USB and board code, untouched
  vendor-seam/
    README.md           which GW primitives stay pristine, and why
  attic/
    qic_skeleton/       the retired pre-vendor skeleton; not built
```

`attic/qic_skeleton/` holds the standalone modules (`arbiter`, `verbs`,
`flux_capture`, `markers`, `transactions`) written against a `gw_stubs.h`
abstraction before GW was vendored. That abstraction fought the reuse-in-place
model and was retired. Its useful logic now lives in `src/qic/qic.c`, bound to
the real GW primitives. Nothing in the attic is built.

## Build the firmware

The firmware builds with PlatformIO, using the `ststm32` platform only as a
toolchain and SCons host. There is no framework. `scripts/pio_post.py` throws
away the platform's bare-metal flags and installs the flag set GW's old
`Rules.mk` used; `scripts/pio_pre.py` adds per-file flags.

| Env | Image | Address |
|---|---|---|
| `bootloader` | GW-compatible application bootloader, 16 KiB | `0x08000000` |
| `tapewyrm` | The application, 48 KiB, flashed through the bootloader | `0x08004000` |

From the repository root:

```sh
just fw          # bootloader + app, merged HEX, into dist/
just fw-dist     # the same plus a combined .upd update file
```

Both run `tools/package.py`, which runs `pio run -e bootloader -e tapewyrm`
in `firmware/`. PlatformIO leaves `firmware.{elf,bin,hex}` for each env under
`firmware/.pio/build/<env>/`. The script then merges the bootloader and
application HEX in pure Python (`tools/ihex.py`) and copies the results to
`dist/`. See [Building and testing](building.md#package-a-release) for the
bundle layout.

- Only the AT32F4 target is wired up. The STM32F1 and F7 sources are still in
  the tree but have no PlatformIO env.
- PlatformIO builds are always release builds (`NDEBUG`). GW's `debug=y`
  serial logging belonged to the make build and has no env.
- The firmware version is `custom_fw_major` / `custom_fw_minor` in
  `platformio.ini` (currently 1.6), read by `build_info.c` and by
  `tools/package.py`.
- `scripts/pio_pre.py` writes `tw_git.h` into the build directory with the
  current commit and a dirty flag (uncommitted changes to tracked files under
  `firmware/` or `protocol/`). `BUILD_INFO` reports both.

!!! note "The toolchain is pinned"
    Code generation affects instruction timing, and this firmware is
    timing-sensitive (pulse cadence, report-bit windows, flux capture).
    `platformio.ini` pins the xPack ARM GCC
    (`platformio/toolchain-gccarmnoneeabi@~1.120301.0`, GCC 12.3), and
    PlatformIO fetches that exact package. Left alone, `ststm32` would pick
    GCC 7.2.1. Do not unpin it.

To confirm the graft linked in:

```sh
arm-none-eabi-nm   firmware/.pio/build/tapewyrm/firmware.elf | grep -i qic
arm-none-eabi-size firmware/.pio/build/tapewyrm/firmware.elf
```

The ARM binutils are in PlatformIO's toolchain package if they are not on your
`PATH`. Many QIC handlers are `static` and inline into `process_command` or
`floppy_read` under `-Os`; `qic_pulses`, `qic_cap` and `qic_timing` are the
symbols to look for.

## Code style

Code inside GW-derived files follows GW's style: match the file you are in,
4-space indents. New Tapewyrm code follows BSD Kernel Normal Form
(`style(9)`). Use `/* ... */` comments and banner comments for sections. A new
file opens with its path, a one-line purpose, the licence line and a "WHAT
THIS IS" explanation. Take every shared constant from `inc/protocol.h`; never
hard-code a value that exists in `protocol/protocol.toml`.

## The vendor seam

Tapewyrm is a hard fork. GW is vendored complete and not tracked against
upstream; mergeability is not a goal. What replaces upstream tracking is the
vendor seam: a set of GW primitives kept pristine so that upstream fixes to
them can still be cherry-picked by hand, and so that the board can always be
flashed back to stock GW. Interoperating with the `gw` host program is not a
goal: `gw` talks to the board only through the bootloader, to revert it.

The seam is not a header. It is the set of GW functions the graft may call,
plus the boot and update layer it must not touch.

| GW primitive | Why it is reused | Where the graft binds |
|---|---|---|
| Flux-capture engine | The timing-critical, hardest-to-get-right code. Tapewyrm only drives it free-running and brackets it with markers. | `floppy_read_prep`, `rdata_encode_flux`, `tim_rdata`/`dma_rdata`, `ST_read_flux`, `u_buf[]`, the `FLUXOP_*` / `0xFF` escape |
| USB CDC-ACM stack | The real-time boundary. The QIC commands ride GW's command framing; a capture stops through GW's `BAUD_CLEAR_COMMS` path. | `src/usb/`, `usb_write`/`ep_tx_ready` via `floppy_read`, `floppy_configure` |
| Bootloader and `gw update` protocol | Keeps the way back to stock GW open. | Not touched by any QIC code |
| AT32 clock and GPIO init, pin map, linker map, startup, watchdog | Board bring-up. | `src/mcu/at32f4/`, `src/board.c`, `write_pin`/`get_*`, `delay_us`, `time_now`, `watchdog_kick` |

### The invariant

!!! danger "Do not touch the boot and update layer"
    Never relocate or resize the bootloader flash region. Never change the
    application entry vector. Never alter the hardware DFU strap or the AT32
    ROM-bootloader path. Never change the USB VID/PID or the update-mode
    commands that `gw update` speaks. Breaking any of these can leave a board
    that only the DFU header or a debug probe can recover, and loses the
    stock-GW fallback.

Diverge freely above the application entry point. The USB product string is
rebranded to `Tapewyrm`; that is cosmetic. The application-mode `CMD_QIC_*`
numbers are separate from the update protocol and may change freely.

Keeping the boot layer intact buys three things:

1. **Revert to stock without a debug probe.** Fit the Update Jumper and run
   `gw update`, or strap the DFU header as a fallback. Then check the drive,
   cable and board with stock GW, and flash Tapewyrm back. The procedure is in
   [Go back to stock Greaseweazle firmware](../guide/flash-greaseweazle.md#go-back-to-stock-greaseweazle-firmware).
2. **A/B against a known-good flux engine.** Stock GW reading a real floppy is
   the reference. Because the tape capture is GW's read engine run
   free-running, the comparison is tight: if GW reads a floppy clean and the
   tape capture is garbage, the fault is in Tapewyrm.
3. **The board keeps its value.** Flashed back to stock, it is a Greaseweazle
   again.

### Cherry-pick an upstream fix

1. Apply the upstream change to the vendored source in place (`src/floppy.c`,
   `src/mcu/at32f4/floppy.c` for the flux engine).
2. Re-check the graft hooks in `src/floppy.c`: `qic_capture_on_index`,
   `qic_capture_account_range` and the session-start, budget and finish
   hooks. Search for `Tapewyrm:` to find every site.
3. Build with `just fw` and dump a track on the bench. Check that the capture
   still verifies against the firmware's `END` accounting.

If a reused primitive needs Tapewyrm-specific changes beyond the hooks, make
them in the vendored source and record the divergence in
`firmware/vendor-seam/README.md`. Do not fork a wider slice of the tree.

## What is still TODO(bench)

The graft is real and builds into the image. The pieces that depend on exact
hardware timing carry `TODO(bench)` markers at their sites in `src/qic/qic.c`.
All of them have worked end to end on one drive, the Colorado Jumbo 350, which
dumped whole tapes that verify against the `END` accounting with one INDEX per
segment. The markers stay until other drive families have been tried.

| Item | What to confirm |
|---|---|
| QIC-117 pulse envelope | The default cadence (`qic_timing`) follows QIC-117 Rev J Table 1. Per-drive values and the report strategy (INDEX edge or fixed settle) come from the drive profile through `CMD_QIC_SET_TIMING`. Confirm per drive with a scope. |
| Open-collector polarity | The graft assumes `write_pin(step, TRUE)` pulls the bus low at the drive and that `LOW` from `get_trk0()`/`get_index()` is the QIC active level. Functionally confirmed; check against the v4.1 schematic. |
| INDEX per segment | During Logical Forward INDEX fires once per segment (about 710 ms apart on the 350), and an idle ready drive emits cue pulses every 2 to 3 ms. Confirm GW's `delay_params.index_mask` debounce does not swallow edges on other drives. |
| Free-running DMA | The capture reuses GW's read-stop and overflow handling unchanged. Confirm the DMA behaviour holds for a continuous stream on other drives. |
| AT32 ROM DFU | Whether the AT32F403 ROM DFU enumerates under stock `dfu-util` or needs Artery's ISP or AT-Link tooling. |

The `TODO(bench)` comments in `qic.c` cite `DESIGN.md §13.6` item numbers that
do not match the numbering above or the current §13.6 list. Go by the comment's
text, not its number.

## See also

- [Device protocol](protocol.md)
- [Greaseweazle v4.1](../hardware/greaseweazle.md)
- [tw flash and tw dfu](../tw/flash.md)
