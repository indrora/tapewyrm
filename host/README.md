# tapewyrm (host)

Host-side software for **Tapewyrm** — QIC-80 floppy-tape recovery over a
Greaseweazle v4.1. Pure Python; the offline decode pipeline (`codec/`) runs with
no hardware attached. See `../docs/DESIGN.md` for the full design record.

## Quick start

```bash
uv sync --extra dev
uv run pytest          # hardware-free codec + control tests
uv run ruff check .
uv run mypy tapewyrm
```

## Layout

| Package      | Role                                                              |
|--------------|------------------------------------------------------------------|
| `link/`      | USB transaction client + firmware flashing (`update.py`); no QIC semantics |
| `qic117/`    | QIC-117 command table, dispatch, status/error decode, profiles   |
| `tape/`      | geometry (coordinate algebra) + transport / capture orchestration |
| `codec/`     | offline decode: flux → MFM → place → RS → volume → QIC-113 files  |
| `rawflux/`   | the `RawFluxCapture` linear-track-stack container                |
| `profiles/`  | per-drive `DriveProfile` TOML data                               |

## CLI

The shipped command is **`tw`** (long alias: `tapewyrm`). It is the single
Tapewyrm tool — including firmware flashing — and never requires the `gw`
executable.

```
tw probe      # open device, identify, print config/geometry
tw capture    # sweep tracks -> RawFluxCapture files
tw decode     # RawFluxCapture(s) -> files + recovery report (no hardware)
tw recover    # capture + decode + multi-pass retries
tw replay     # re-decode saved flux with different PLL/RS options
tw flash      # update firmware via the GW-compatible app bootloader (over USB)
tw dfu        # recovery / first flash via the AT32 ROM bootloader (dfu-util)
```
