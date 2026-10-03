# Tapewyrm

**Recover QIC-40, QIC-80, QIC-3010 and QIC-3020 floppy tapes with a
Greaseweazle v4.1.**

These are the "floppy tape" minicartridges of the 1990s, read by QIC-117 drives
such as the Colorado (later HP Colorado), Conner and Iomega Ditto. The drive
plugs into a floppy controller. Tapewyrm puts a Greaseweazle v4.1, running
Tapewyrm firmware, in place of that controller. The board sends the drive its
QIC-117 commands and records the raw flux of each track. Everything after that
runs offline on the host: MFM decoding, Reed-Solomon correction, the volume
table, QIC-122 decompression and the QIC-113 directory. The result is an
ordinary tar file with the backup's own file names, dates and attributes, and a
report of anything that could not be recovered.

The firmware is a hard fork of the Greaseweazle firmware. The host software is
new Python, split into four packages.

## Status

Tapewyrm works on real tapes, but it is bench software: expect rough edges.

- **Drives.** The Colorado Jumbo 350 and the Colorado 1400 have been tested
  (profiles `colorado` and `colorado.1400`). The `conner` and `iomega` drive
  profiles are untested placeholders.
- **Tapes.** QIC-80 bench tapes written by Colorado (CMS) backup software and
  by an "MTN" backup program have been read back to tar files. One damaged tape
  was recovered completely by merging several dumps. A QIC-3020 tape (Verbatim
  QIC-Extra) has been dumped at 1 Mbps and identified.
- **Formats.** QIC-113 extended (Colorado/HP) and Basic-DOS directories, and
  QIC-122 compression, are supported. Other backup programs need a new volume
  profile (a TOML file, not code), described in
  [packages/qiclib](packages/qiclib/README.md).
- **Firmware.** The QIC graft is real and builds into the Greaseweazle image.
  Parts that depend on exact hardware timing are marked `TODO(bench)` in the
  source.

## What you need

- A **Greaseweazle v4.1** with Tapewyrm firmware (flashed with `tw flash`, or
  `tw dfu` the first time; see [firmware/README.md](firmware/README.md)).
- A **QIC-117 floppy-tape drive** (internal, 34-pin floppy interface) and a
  34-pin floppy cable.
- A **power supply for the drive** (5 V and 12 V). The Greaseweazle does not
  power a tape drive.
- **[uv](https://docs.astral.sh/uv/)** on the host, and
  [just](https://github.com/casey/just) if you want the task recipes.

Start with the **[quick start guide](docs/quickstart.md)**.

## The pipeline

```
 tape in drive
      |
      |  tw dump          (hardware: one Logical Forward pass per track)
      v
 captures/NAME/track-NN.twrf     TWRF: raw flux + drive identity    [TWS-1]
      |
      |  tw convert       (offline: flux -> MFM -> sectors -> RS ECC; merges dumps)
      v
 NAME.twti / NAME.twtz           TWTI: logical tape image           [TWS-2]
      |                          (TWTZ = the same, zstd-compressed)
      |  qicsilver identify  -> cartridge, header, volume table (read only)
      |  qicsilver extract   (QIC-122 decompression, QIC-113 offsets)
      v
 vols/vol-NN.twvl                TWVL: one backup volume + holes    [TWS-3]
      |
      |  qicsilver tar
      v
 NAME.tar + NAME.tar.damaged.txt  pax tar + damage report
```

Only `tw dump` (and `tw info`, `tw drive`, `tw flash`, `tw dfu`) needs hardware.
Keep the TWRF captures: decoding can be run again with better software later,
but reading an old tape again may not be possible.

## Repository layout

| Path | What |
|---|---|
| [`packages/tapewyrm-archive/`](packages/tapewyrm-archive/README.md) | On-disk formats (TWRF, TWTI/TWTZ, TWVL), QIC-117 report decoders, the progress-hook protocol. Stdlib only (plus `backports.zstd` below Python 3.14). |
| [`packages/qiclib/`](packages/qiclib/README.md) | QIC layout: geometry, sector placement, merge, Reed-Solomon ECC, header/bad-sector map/volume table, cartridge and volume profiles, QIC-113, QIC-122, image build, identify, extract. Stdlib only. |
| [`packages/tapewyrm-cli/`](packages/tapewyrm-cli/README.md) | `tw`: the device link, the QIC-117 drive layer, drive profiles, `dump`, flux/MFM decoding and `convert`, firmware flashing. |
| [`packages/qicsilver/`](packages/qicsilver/README.md) | `qicsilver`: `identify`, `extract` and `tar` on TWTI/TWTZ images. No hardware. |
| [`firmware/`](firmware/README.md) | Greaseweazle firmware, vendored, with the QIC verbs and free-running flux capture grafted in. |
| `protocol/` | `protocol.toml`, the single source of the host/firmware contract, and `generate.py`, which writes `firmware/inc/protocol.h` and `tapewyrm/link/protocol.py`. |
| `docs/` | [quick start](docs/quickstart.md), [design record](docs/DESIGN.md), [format specifications](docs/spec/README.md), and the QIC standards as PDFs (`docs/qic-standards/`). |
| `tools/` | Build helpers run by `just`: `package.py` (wheel + firmware), `ihex.py`, `clean.py`. |
| `captures/` | Your tape dumps and images. Gitignored; never publish them (see the [quick start](docs/quickstart.md#keep-captures-private)). |

## Documentation

- [docs/quickstart.md](docs/quickstart.md): from a drive on the bench to a tar
  file.
- [docs/spec/](docs/spec/README.md): the Tapewyrm Specification (TWS) series,
  which defines the TWRF, TWTI/TWTZ and TWVL file formats.
- [docs/DESIGN.md](docs/DESIGN.md): the design record, with the reasons behind
  the architecture. Parts of it describe earlier plans (module names, commands)
  and are out of date. Where it disagrees with the code, the code is right.
- [STYLE.md](STYLE.md): the coding conventions. Read it before changing code.
- [firmware/README.md](firmware/README.md): how the firmware graft works, how
  to build it, and how to flash it.

## Building and testing

Every Python package is a separate uv project. The `justfile` wraps the common
tasks (`just --list` shows them all):

| Recipe | Does |
|---|---|
| `just test` | Run the tests of every package. No hardware needed. |
| `just host` | Sync, lint (ruff), format-check, type-check (mypy) and test every package. Run it before calling work done. |
| `just check PKG MODULE` | The same for one package, e.g. `just check qiclib qiclib`. |
| `just lint` | ruff + mypy for every package, without tests. |
| `just gen` / `just gen-check` | Regenerate the protocol files from `protocol/protocol.toml` / check that they are in sync. |
| `just fw` | Build the firmware with PlatformIO into `dist/`. |
| `just package` | Build the host wheel and the firmware into `dist/`. |
| `just flash` / `just dfu` | Flash the PlatformIO firmware build with `tw flash` / `tw dfu`. |
| `just tapewyrm ARGS` | Run `tw ARGS` from the source tree. |
| `just qicsilver ARGS` | Run `qicsilver ARGS` from the source tree. |
| `just clean` | Remove build, package and cache output. |
| `just ci` | Everything CI runs. |

Without `just`, run a CLI straight from the tree:

```sh
uv run --project packages/tapewyrm-cli tw --help
uv run --project packages/qicsilver qicsilver --help
```

## License

Tapewyrm is released into the public domain under **The Unlicense**; see
[UNLICENSE](UNLICENSE). This covers the host packages and the firmware.

The firmware under `firmware/` is derived from the
[Greaseweazle firmware](https://github.com/keirf/greaseweazle-firmware) by
Keir Fraser, which is itself released under the Unlicense
([firmware/COPYING](firmware/COPYING)). Tapewyrm's changes to it are public
domain too. `packages/tapewyrm-cli/tapewyrm/codec/gwpll.py` vendors the
software PLL from the [Greaseweazle host tools](https://github.com/keirf/greaseweazle),
also released under the Unlicense.

No GPL code is included. The ftape project (GPL) was consulted only as a
reference for drive behaviour; no ftape code was copied. The QIC standards in
`docs/qic-standards/` are reference documents from their publishers and are not
covered by the Unlicense.
