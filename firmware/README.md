# Tapewyrm firmware

QIC-117 / QIC-80 floppy-tape recovery firmware for the **Greaseweazle v4.1**
(AT32F403, Cortex-M4). A **hard fork** of Greaseweazle (GW) v4.1 firmware that
adds, *above the application entry point*:

- **QIC-117 command verbs** — verbatim STEP-pulse command emit, N+2 arguments,
  TRK0 report-bit clocking, wait-ready (the out-of-band command/status channel).
- **A bus arbiter** — a single-lease state machine that owns drive-select and
  serializes the two channels so flux can never collide with a command.
- **Free-running flux capture** — RDATA flux recorded continuously (not gated to
  one revolution), with per-segment INDEX markers, on the in-band data channel.

This directory currently holds the **firmware skeleton**: self-consistent C that
documents the state machines and transaction handling and compiles in isolation
via `extern` stubs. The real GW firmware tree is **not vendored here yet** — the
skeleton stands alone so the design is reviewable and CI can syntax-check it.

See `../DESIGN.md` for the full design (§4 architecture, §5 firmware design,
§12 toolchain, §13 implementation reference).

## Layout

```
firmware/
  protocol.h            generated contract (DO NOT EDIT) — opcodes, marker codes,
                        version; #included by every QIC source as ../protocol.h
  Makefile              SKELETON: `make syntax-check` only; real build = GW tree
  qic/
    gw_stubs.h          extern seam: every GW/hardware primitive (TODO(bench))
    arbiter.{h,c}       lease state machine §5.2; Quiesce funnel; dead-man triggers
    verbs.{h,c}         pulse/N+2 arg/report-clock/wait-ready §5.3
    flux_capture.{h,c}  free-running capture pipeline + markers + overflow §5.4
    markers.{h,c}       marker injection on the opcode-escape channel §7.2
    transactions.{h,c}  USB transaction dispatch §5.1/§13.3 (txn_dispatch)
  vendor-seam/
    README.md           which GW primitives stay pristine, and why (§12.5)
```

## Building

### The real build (TODO(bench) §12.1)

The real firmware is built by **merging `qic/*` and `protocol.h` into the
vendored Greaseweazle Make tree** and building the `at32f4` target there. GW's
build is the **ARM GNU toolchain via Make** (`gcc-arm-none-eabi`, plus
`srecord`, `stm32flash`, and a few Python packages). It produces:

```
out/at32f4/prod/greaseweazle/target.hex     # release
out/at32f4/debug/greaseweazle/target.hex    # bring-up: 3 Mbaud serial logging
```

Build **prod** for releases; build **debug** during bring-up — it enables the
3 Mbaud serial logging you want while debugging the report-bit loop and the
arbiter lease machine.

> **Pin the toolchain version.** `gcc-arm-none-eabi`'s version affects code
> generation and therefore instruction timing — and this firmware is
> timing-sensitive (QIC pulse cadence, report-bit windows, flux capture).
> Reproduce via the project's pinned container / Nix flake (§12.6, §12.8).

Integration work to wire the skeleton into GW (all `TODO(bench)`):

- bind the `gw_*` stubs in `qic/gw_stubs.h` to real GW primitives;
- hook `txn_dispatch()` into GW's USB command handler and
  `txn_capture_service()` into GW's main loop;
- keep the boot/update layer pristine (see `vendor-seam/README.md`).

### Syntax-checking the skeleton (works today)

```
make -C firmware syntax-check
```

Runs `arm-none-eabi-gcc -fsyntax-only` (or a host `gcc`/`cc`/`clang` if no ARM
toolchain is installed) over every `qic/*.c` with `-Ifirmware -Ifirmware/qic`.
The `gw_stubs.h` externs make the sources self-contained, so this verifies the C
is well-formed without needing the GW tree. It does **not** produce a `.hex`.

## Flashing — three tiers (DESIGN.md §12.3)

The hardware DFU header makes this robust: there is always a probe-less way back,
even from a bad flash.

All flashing is driven by **`tw`** (the Tapewyrm host tool), never the `gw`
executable. The GW-compatible bootloader protocol is kept wire-compatible so the
board stays a dual citizen, but `tw` carries its own flashing client.

| Tier | Mechanism | Driven by | Use it for | Needs |
|---|---|---|---|---|
| **Routine** | GW-compatible application bootloader, over USB | `tw flash` | normal updates | nothing extra |
| **Recovery / un-brick** | Hardware DFU header → AT32 built-in ROM bootloader | `tw dfu` (wraps `dfu-util`) | flashing when the app bootloader is broken / half-flashed; first flash | `dfu-util` — **or** Artery ISP / AT-Link if the AT32 ROM-DFU descriptor doesn't enumerate cleanly under stock `dfu-util` |
| **Debug** | SWD via ST-Link / Black Magic Probe + OpenOCD | a debug probe | live debugging, breakpoints | a debug probe |

The middle tier is the important one for this project: iterating on the arbiter
and timing code risks bricking the application, and the DFU header guarantees an
application-independent recovery path that needs no debug probe. (Because the
protocol is wire-compatible, stock `gw update` *also* works on the same board —
but Tapewyrm never requires it.)

> **Open item (TODO(bench)):** confirm whether the AT32F403 ROM DFU enumerates
> under stock `dfu-util` or requires Artery's ISP / AT-Link tooling — settle this
> early, since it's the recovery path everything else leans on.

## Fork posture (DESIGN.md §1, §12.5)

This is a **hard fork**, not a tracked branch. GW's tree is **vendored**; the
arbiter / verbs / capture changes are structural and the hardware is fixed at
v4.1, so tracking upstream buys nothing. The one retained discipline is a clean
**vendor seam** (`vendor-seam/`) isolating the genuinely-reused GW primitives
(flux-capture timing, USB, **bootloader / update protocol**) so upstream fixes
can still be cherry-picked by hand and the board stays a **dual citizen** — the
same board flashes stock GW *or* Tapewyrm with one flash each (`tw flash` for
Tapewyrm; stock `gw update` still works too). **Do not
touch the bootloader flash region, application entry vector, DFU strap, or USB
update-mode descriptors / commands.** Details in `vendor-seam/README.md`.

## The protocol contract (`protocol.h`)

`protocol.h` is **generated** from the single source of truth in `protocol/`
(`protocol.toml` → `protocol/generate.py` → `firmware/protocol.h` +
`host/tapewyrm/link/protocol.py`), so the device and host opcode / marker tables
**never drift** (§12.4). CI regenerates and fails on `git diff`. **Do not edit
`protocol.h` by hand** — change `protocol.toml` and regenerate. The QIC sources
`#include "../protocol.h"` and use its symbols verbatim
(`TW_TXN_*`, `TW_MARK_*`, `TW_EVT_*`, `TW_END_*`, `TW_CAP_*`, `TW_PROTO_VERSION`).

The marker **payload layouts** the firmware emits (`markers.c`) are the other
half of the contract; they must stay byte-for-byte identical to what the host
parses in `host/tapewyrm/rawflux/container.py` (little-endian; see `markers.h`).

## TODO(bench) checklist (the firmware seams, DESIGN.md §13.6)

These are the points where the skeleton must stop and either read GW source or
touch hardware. Every one is marked `TODO(bench)` at its site in the code.

1. **GW flux opcode byte values + long-flux continuation.** Read from
   `greaseweazle-firmware`; finalize `gw_flux_encode_interval()` /
   `gw_flux_opcode_escape()` (`flux_capture.c`, `markers.c`) and the host
   `container.py` framing in lockstep. (§13.6 item 1)
2. **GW handler / read-stop integration points.** Exactly how `txn_dispatch()`
   hooks GW's command handler and how `cap_arm`/`cap_disarm` / ABORT map onto
   GW's flux read arm / read-stop control (`transactions.c`, `flux_capture.c`,
   `gw_stubs.h`). (§13.6 item 2)
3. **Target `DriveProfile` timing.** Real per-drive pulse cadence, report
   strategy (INDEX-edge vs fixed-settle), CCS level, and quirks — characterize
   via `gw pin` + scope; feed through `SET_TIMING` into `verbs.c`. (§13.6 item 3)
4. **v4.1 schematic + 34-pin open-collector confirm.** Verify TRK0 / INDEX land
   on pollable GPIO/EXTI and the STEP buffer swings the bus at cadence; confirm
   the target drive is the 34-pin **open-collector** interface (not the 40-pin
   tri-state variant) so the polarity baked into `gw_pin_write`/`gw_pin_read` is
   right. (§13.6 item 4)
