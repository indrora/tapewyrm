# Tapewyrm

**QIC-80 floppy-tape recovery over a Greaseweazle v4.1.** Capture raw flux from
QIC-40/80/3010/3020 cartridges with a Greaseweazle v4.1 and decode it **offline**
into files. A hard fork of Greaseweazle (firmware) plus a new Python host.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design record (the *why* behind
every decision). This README is the map; the design doc is the territory.

## Layout (monorepo)

| Path        | What                                                                       |
|-------------|----------------------------------------------------------------------------|
| `host/`     | Python host: device link, QIC-117 drive layer, tape transport, decode codec |
| `firmware/` | C firmware skeleton (QIC verbs, bus arbiter, free-running flux capture)      |
| `protocol/` | Single source of truth (`protocol.toml`) + generator for both ends          |
| `docs/`     | DESIGN.md and design notes                                                  |
| `justfile`  | Task runner (`just --list`)                                                |

## Quick start

```bash
# host (no hardware needed — the whole decode path is testable offline)
cd host
uv sync --extra dev
uv run pytest                 # 137 tests, all offline
uv run tw --help              # `tw` is the single Tapewyrm tool (alias: tapewyrm)

# protocol contract (regenerates firmware/protocol.h + host link/protocol.py)
python protocol/generate.py

# firmware: REAL Cortex-M4 cross-compile of the QIC skeleton (needs arm-none-eabi-gcc)
make -f firmware/Makefile cross-check
```

`tw` owns everything, including firmware flashing (`tw flash` over the
GW-compatible bootloader, `tw dfu` for recovery via `dfu-util`) — the `gw`
executable is never required.

## Status

The host **codec** (flux → MFM → placement → Reed–Solomon erasure → volume/BSM →
QIC-113 file extraction), the host **control stack** (link, QIC-117 command table
+ dispatch, tape transport, CLI, drive profiles), and the **firmware skeleton**
(arbiter lease machine, verbs engine, flux capture, markers, transaction dispatch)
are all implemented and green. The genuine hardware/bench seams (GW flux opcode
byte values + PLL reuse, GW firmware integration points, the target `DriveProfile`,
v4.1 schematic confirmation, and the STAC/DCLZ decompressor) are isolated and
marked `TODO(bench)` per [DESIGN.md §13.6](docs/DESIGN.md).

## License

**The Unlicense** (public domain) — see [UNLICENSE](UNLICENSE). This applies to
the whole project: the host software and the firmware (which vendors the
Greaseweazle firmware, itself Unlicense). The QIC standards and ftape were
references only; no GPL code is incorporated.
