# Tapewyrm firmware

QIC-117 / QIC-80 floppy-tape recovery firmware for the **Greaseweazle v4.1**
(AT32F403, Cortex-M4).

Greaseweazle (GW) v1.6 firmware is **vendored complete in-tree** here and builds
in-place (see [Building](#building)). Tapewyrm does **not** ship a parallel
application; it **reuses GW's control loop and its (now vestigial, for our
purposes) MFM flux-recording engine** and grafts the QIC functionality directly
onto them:

- **GW's command loop is reused.** The QIC verbs are new `case` entries grafted
  into GW's `process_command()` switch in `src/floppy.c`, dispatched by the same
  `floppy_process()` state machine in `src/main.c`'s loop.
- **GW's flux recording IS our capture engine.** A free-running tape capture is,
  structurally, a GW flux read with no index limit and no tick deadline. The
  capture reuses GW's RDATA flux-read engine wholesale — the same
  `tim_rdata`/`dma_rdata` input-capture front-end, the same `rdata_encode_flux()`
  byte encoder, the same `ST_read_flux` state machine and `u_buf[]` USB pump
  (`floppy_read`). The hardware INDEX edge GW already records (once per
  revolution for a floppy) becomes our per-**segment** boundary on tape (§2.2).
- **GW's primitives are reused directly.** STEP is driven via GW's
  `write_pin(step,…)`; TRK0/INDEX are read via `get_trk0()`/`get_index()`;
  cadence via `delay_us`/`delay_ms`; timeouts via `time_now()`/`time_since()`;
  plus `act_led()` and the existing watchdog.

We do **not** preserve GW's command protocol for external compatibility — the
QIC verbs are added freely as new command numbers (collisions with GW's numbers
would not matter; in fact the QIC verbs sit at `0x80+`, clearly separate from
GW's `0..22`, so a stock `gw` host simply gets `ACK_BAD_COMMAND`).

See `../docs/DESIGN.md` for the full design (§4 architecture, §5 firmware design
— esp. §5.3 verbs, §5.4 capture, §7.2 markers, §13.3 wire protocol).

## How the graft is wired

Because GW's flux engine and pin macros are `static` inside `src/floppy.c`, the
QIC code lives in **`src/qic/qic.c`**, which is `#include`d into `src/floppy.c`
(just before `process_command()`) so it can bind to those statics directly —
rather than sitting behind an abstraction layer. There is no `gw_stubs.h` seam
any more; everything binds to the real GW primitives.

```
src/floppy.c
  ├─ #include "protocol.h"            (generated marker/opcode contract)
  ├─ forward decls of the QIC hooks   (so the flux engine can call them)
  ├─ rdata_encode_flux()              ── hook ─► qic_capture_on_index()  (SEGMENT marker)
  │                                   ── hook ─► qic_capture_account_range() (END accounting)
  ├─ floppy_read()                    ── hook ─► qic_capture_emit_session_start_if_needed()
  │                                   ── hook ─► qic_capture_budget_reached()/finish()
  ├─ floppy_configure()/floppy_reset()── hook ─► qic_capture_abort_silent()  (safe-stop)
  ├─ #include "qic/qic.c"             ◄── the QIC verbs + markers + capture live here
  └─ process_command()                ── new cases ─► CMD_QIC_INFO / SET_TIMING / PULSES
                                                      / WAIT_READY / CAPTURE
```

`src/qic/qic.c` is **not** a separately-compiled translation unit (it would not
build standalone — it references GW's `floppy.c` statics). It rides along inside
`floppy.o` via the `#include`, and the build's auto-generated dependency files
pick it up as a prerequisite of `floppy.o`, so edits to it trigger a rebuild.

### QIC command set (grafted into `process_command()`)

| Cmd | Number | Does |
|---|---|---|
| `CMD_QIC_INFO`       | `0x80` | device info + QIC capability gate (proto ver, caps, SRAM, sample clock) |
| `CMD_QIC_SET_TIMING` | `0x81` | set the QIC-117 pulse/report cadence (idle-only) |
| `CMD_QIC_PULSES`     | `0x82` | emit *N* STEP pulses verbatim; optionally clock *k* report bits off TRK0 |
| `CMD_QIC_WAIT_READY` | `0x83` | poll the INDEX/ready line until ready or timeout |
| `CMD_QIC_CAPTURE`    | `0x84` | issue motion verbatim, then arm a free-running flux capture (reuses the GW read path) |

The marker codes and payload layouts the firmware emits are the firmware↔host
contract; they come from the generated `inc/protocol.h` and must stay
byte-for-byte identical to what the host parses in
`host/tapewyrm/rawflux/container.py` (little-endian; ESC = `0xFF`).

## Layout

```
firmware/
  Makefile, Rules.mk      vendored GW build (builds in-tree; see below)
  inc/
    protocol.h            GENERATED contract (DO NOT EDIT) — opcodes, marker codes, version
    cdc_acm_protocol.h    GW command set + flux byte encoding (FLUXOP_*, the GW opcode escape)
    ...                   GW headers (util.h, time.h, usb.h, …)
  src/
    floppy.c              GW floppy/flux engine + the QIC graft hooks + process_command() cases
    main.c                GW init + floppy_process() main loop (reused)
    qic/
      qic.c               THE QIC GRAFT — verbs, tape markers, free-running capture
                          (#included into floppy.c; sees GW statics directly)
    mcu/, usb/, …         GW MCU/USB/board code (reused untouched)
  vendor-seam/
    README.md             which GW primitives stay pristine, and why (§12.5)
  attic/
    qic_skeleton/         the RETIRED pre-vendor skeleton (arbiter/verbs/flux_capture/
                          markers/transactions + gw_stubs.h) — kept for reference only,
                          NOT in the build
```

> **Retired skeleton.** Before GW was vendored in-tree, the QIC layer was a set
> of standalone modules (`arbiter`, `verbs`, `flux_capture`, `markers`,
> `transactions`) written against a `gw_stubs.h` `extern` abstraction so they
> could syntax-check in isolation. That abstraction fought the "reuse GW's loop +
> flux engine in place" model, so it has been **retired to `attic/qic_skeleton/`**.
> Its still-useful logic was carried forward and rebound to the real GW
> primitives in `src/qic/qic.c`: the verbs cadence (`qic_pulses` / report clock /
> `qic_wait_ready`), the marker serialization (now writing into GW's `u_buf[]`
> via the GW opcode escape instead of a `gw_usb_write` stub), and the
> stop-first-then-seal teardown ordering (now folded into GW's existing
> read-stop/abort paths). No `gw_stubs.h` `extern` stubs remain in the built
> image.

## Building

The vendored GW tree builds in-tree with the ARM GNU toolchain via Make. To
build the combined Tapewyrm `tapewyrm` application image:

```sh
cd firmware
export ROOT="$(pwd)" FW_MAJOR=1 FW_MINOR=6
mkdir -p out/at32f4/prod/tapewyrm
make -C out/at32f4/prod/tapewyrm -f "$ROOT/Rules.mk" \
    target.bin mcu=at32f4 prod=y tapewyrm=y PYTHON=python
```

This produces `out/at32f4/prod/tapewyrm/target.{elf,bin}`. Build **prod**
for releases; swap `prod=y` for `debug=y` during bring-up to enable the 3 Mbaud
serial logging (useful while debugging the report-bit loop and the capture
path). Run `make clean` between attempts.

> The `.hex` step needs `srec_cat` (srecord), which stitches in the bootloader.
> If `srec_cat` is absent locally, build `target.bin`/`target.elf` to verify; CI
> has srecord and produces the `.hex`.

> **Pin the toolchain version.** The toolchain affects code generation and
> therefore instruction timing — and this firmware is timing-sensitive (QIC
> pulse cadence, report-bit windows, flux capture). Reproduce via the project's
> pinned container / Nix flake (DESIGN.md §12.6, §12.8).

Verify the QIC code linked in:

```sh
arm-none-eabi-nm  out/at32f4/prod/tapewyrm/target.elf | grep -i qic
arm-none-eabi-size out/at32f4/prod/tapewyrm/target.elf
```

(Many of the QIC handlers are `static` and get inlined into `process_command` /
`floppy_read` under `-Os`; symbols such as `qic_pulses`, `qic_cap`, and
`qic_timing` confirm the graft is present, and `size` shows the image grew over a
stock GW build by the QIC logic.)

## Flashing — three tiers (DESIGN.md §12.3)

The hardware DFU header makes this robust: there is always a probe-less way back,
even from a bad flash. All flashing is driven by **`tw`** (the Tapewyrm host
tool), never the `gw` executable. The GW-compatible bootloader protocol is kept
wire-compatible so the board stays a dual citizen, but `tw` carries its own
flashing client.

| Tier | Mechanism | Driven by | Use it for | Needs |
|---|---|---|---|---|
| **Routine** | GW-compatible application bootloader, over USB | `tw flash` | normal updates | nothing extra |
| **Recovery / un-brick** | Hardware DFU header → AT32 built-in ROM bootloader | `tw dfu` (wraps `dfu-util`) | flashing when the app bootloader is broken / half-flashed; first flash | `dfu-util` — **or** Artery ISP / AT-Link if the AT32 ROM-DFU descriptor doesn't enumerate cleanly under stock `dfu-util` |
| **Debug** | SWD via ST-Link / Black Magic Probe + OpenOCD | a debug probe | live debugging, breakpoints | a debug probe |

The middle tier is the important one for this project: iterating on the QIC
timing code risks bricking the application, and the DFU header guarantees an
application-independent recovery path that needs no debug probe. (Because the
protocol is wire-compatible, stock `gw update` *also* works on the same board —
but Tapewyrm never requires it.)

> **Open item (TODO(bench)):** confirm whether the AT32F403 ROM DFU enumerates
> under stock `dfu-util` or requires Artery's ISP / AT-Link tooling — settle this
> early, since it's the recovery path everything else leans on.

## Fork posture (DESIGN.md §1, §12.5)

This is a **hard fork**, not a tracked branch. GW's tree is **vendored complete
in-tree**; the QIC verbs / capture changes are structural and the hardware is
fixed at v4.1, so tracking upstream buys nothing. The one retained discipline is
a clean **vendor seam** (`vendor-seam/`) isolating the genuinely-reused GW
primitives (flux-capture timing, USB, **bootloader / update protocol**) so
upstream fixes can still be cherry-picked by hand and the board stays a **dual
citizen** — the same board flashes stock GW *or* Tapewyrm with one flash each
(`tw flash` for Tapewyrm; stock `gw update` still works too). **Do not touch the
bootloader flash region, application entry vector, DFU strap, or USB update-mode
descriptors / commands.** Details in `vendor-seam/README.md`.

## The protocol contract (`inc/protocol.h`)

`inc/protocol.h` is **generated** from the single source of truth in `protocol/`
(`protocol.toml` → `protocol/generate.py` → `firmware/inc/protocol.h` +
`host/tapewyrm/link/protocol.py`), so the device and host opcode / marker tables
**never drift** (§12.4). CI regenerates and fails on `git diff`. **Do not edit
`protocol.h` by hand** — change `protocol.toml` and regenerate. The QIC graft
`#include`s it and uses its symbols verbatim (`TW_MARK_*`, `TW_EVT_*`,
`TW_END_*`, `TW_CAP_*`, `TW_PROTO_VERSION`).

The marker **payload layouts** the firmware emits (in `src/qic/qic.c`) are the
other half of the contract; they must stay byte-for-byte identical to what the
host parses in `host/tapewyrm/rawflux/container.py` (little-endian).

## What is real vs `TODO(bench)`

The graft is **real, present, and compiles+links into the image**. The pieces
that are genuinely hardware-timing-faithful and need a real drive + scope to
finalize are marked `TODO(bench)` at their sites in `src/qic/qic.c` and the
graft hooks in `src/floppy.c` (DESIGN.md §13.6):

1. **Exact QIC-117 pulse envelope.** The default cadence (`qic_timing`) tracks
   QIC-117 Rev J Table 1; the real per-drive values + report strategy
   (INDEX-edge vs fixed-settle) come from scoping the target drive and arrive via
   `CMD_QIC_SET_TIMING`.
2. **34-pin open-collector polarity.** The graft assumes asserting STEP via
   `write_pin(step,TRUE)` pulls the bus low at the drive, and that
   `get_trk0()`/`get_index()` `LOW` is the QIC "active" level. Confirm against
   the v4.1 schematic.
3. **INDEX-per-segment edge handling.** On tape INDEX fires per *segment*
   (~2 ms cadence), not per *revolution*. The graft rides GW's existing index
   detection; confirm GW's `index_mask` debounce (`delay_params.index_mask`)
   doesn't swallow segment edges at that cadence.
4. **Free-running-capture DMA details.** The capture reuses GW's read-stop and
   overflow handling unchanged; confirm the DMA edge behaviour holds for a
   continuous (non-revolution-gated) stream.
