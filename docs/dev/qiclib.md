# qiclib

`qiclib` is everything about where data sits on a QIC tape and what it means,
with no hardware attached: segments and their Reed-Solomon parity, the header
segment, the volume table, QIC-113 directories, QIC-122 compression, and the
cartridge and volume profiles. `tapewyrm-cli` hands it decoded sectors and gets
a TWTI image back; `qicsilver` and `cinnabar` use it to read that image.

## Package facts

| | |
|---|---|
| Directory | `packages/qiclib` |
| Distribution | `qiclib` |
| Import name | `qiclib` |
| Scripts | none |
| Depends on | `tapewyrm-archive` only |
| Depended on by | `tapewyrm-cli`, `qicsilver`, `cinnabar` |

It is a library and is stdlib-only apart from `tapewyrm-archive`: no `rich`,
no `click`, no hardware code. The TOML profiles under `qiclib/profiles/` and
`qiclib/volume/profiles/` ship inside the wheel as package data.

## Module map

### Tape layout and recovery

| Module | Purpose |
|---|---|
| `qiclib.types` | Layout types: `RawSector`, `Segment`, `SegmentStatus`, `SegmentResult`, `FileEntry`, `FileSet`. |
| `qiclib.geometry` | Serpentine track geometry and the sector-ID coordinate algebra. |
| `qiclib.place` | Place recovered sectors into 32-sector segment bins by their own address. |
| `qiclib.merge` | Several passes merged: `union` (every CRC-good sector of every read) or `select` (each track or segment whole from its best read, `Best` and `Rank`). |
| `qiclib.vote` | Best out of N: a sector no read got right rebuilt by byte-wise majority (CRC-checked); a segment that will not correct rebuilt column by column (parity-checked). |
| `qiclib.mergelog` | The per-track and per-segment account of a merge that `tw convert` logs. |
| `qiclib.rs` | GF(256) Reed-Solomon erasure decoder (redundancy 3), per-segment `correct`, and `verify`, the spare-parity check that catches a CRC-good sector that is wrong. |
| `qiclib.segment` | Segment assembly helpers. |
| `qiclib.build` | Sectors to TWTI image: the layout half of `tw convert` (`build_image`). |
| `qiclib.qic100` | QIC-100-MC, the GCR serial-recorded minicartridge format: record framing. Exercised only on synthetic data. |

### What is on the tape

| Module | Purpose |
|---|---|
| `qiclib.header` | The header segment: format parameter record and bad-sector map, `locate_header`, `apply_bsm`, `parse_header` / `parse_header_data`. |
| `qiclib.cartridge` | Which cartridge a header's geometry implies, from the cartridge profiles and the standards' minimum-length equations (`guess`). |
| `qiclib.identify` | What is on a tape, from an image or from loose sectors (`qicsilver identify`, `cinnabar`): `TapeInfo`, `from_image`, `from_sectors`. |
| `qiclib.extract` | Image to TWVL volumes (`qicsilver extract`): `read_volume_table`, `build_volume`, `extract`, `load_volume`. |

### The volume (`qiclib.volume`)

`qiclib.volume` re-exports nothing. Import the submodule you mean.

| Module | Purpose |
|---|---|
| `qiclib.volume.table` | The 128-byte `VTBL` records, `VtblEntry`, the fixed Rev N / QIC-113 reading of bytes 57-127, `volume_streams`. |
| `qiclib.volume.profile` | Volume profiles: the TOML loader, the `[match]` gate, scoring, `guess`, `rank`, `contenders`, and hand-tuned `Overrides`. |
| `qiclib.volume.verdict` | The shared vocabulary of a read: `Check`, the closed `VolumeWarning` enum, `Probe`, `CLOSE_MARGIN`. |
| `qiclib.volume.qic122` | QIC-122 (Stac LZS) decompression and QIC-113 compression extents. |
| `qiclib.volume.qic113` | QIC-113 Basic-DOS file sets (Rev G section 7). |
| `qiclib.volume.qic113ext` | QIC-113 extended-OS volumes (Rev G section 8): directory and data entries. |
| `qiclib.volume.fileset` | The file-set parser package. Importing it registers the built-in parsers. |
| `qiclib.volume.fileset.base` | `FileSetParser`, `Reading`, and the parser registry (`register`, `get`, `names`, `probe_all`). |
| `qiclib.volume.fileset.common` | What every parser shares: the TWVL header's VTBL and the standard checks. |
| `qiclib.volume.fileset.entry` | `VolumeEntry`, one entry as every parser reports it. |
| `qiclib.volume.fileset.qic113_basic` | Parser `qic113-basic`. |
| `qiclib.volume.fileset.qic113_extended` | Parser `qic113-extended`. |
| `qiclib.volume.fileset.qicstream_fset` | Parser `qicstream-fset`: QICStream v2 "FSET" volumes. |
| `qiclib.volume.fileset.none` | Parser `none`: a format that is recognised but cannot be listed. |

### Tests and fixtures

| Module | Purpose |
|---|---|
| `qiclib.testing.builders` | Synthetic header segments, volume tables, QIC-113 volumes, extended volumes, FSET volumes and TWVL files. Shared with every package's tests. |
| `qiclib.testing.bench_3m`, `bench_qicextra`, `bench_unk_co` | Real header and volume-table bytes from three bench tapes. No file contents. |

## How a tape is read

```text
RawSector passes ──► place ──► merge (union | select) ──► vote ──► rs.correct ──► build_image ──► TWTI
                                                                                         │
TWTI ──► header ──► cartridge.guess                                                      │
     └─► volume.table ──► volume.profile.guess ──► qic122 extents ──► fileset parser ──► TWVL
```

A volume is read in three layers, each named by the volume profile: the VTBL
field offsets (`[vtbl]`), the extent widths (`[extent]`) and the file-set
parser (`[fileset]`). See [Writing a volume profile](volume-profiles.md).

## Key types and extension points

- **`Segment`** and **`RawSector`** (`types`) are what the physical layer hands
  in. A parser that may need the raw bytes later keeps them next to the parsed
  fields (`VtblEntry.raw`).
- **Parsers come in pairs**: `parse_x(seg)` wraps `parse_x_data(data)`, so
  tests and images can feed bytes directly. Follow the pair when you add one.
- **A new cartridge** is a new TOML file in `qiclib/profiles/cartridge/`.
  See [Writing a volume profile](volume-profiles.md#add-a-cartridge-profile).
- **A new backup program** is a new TOML file in `qiclib/volume/profiles/`,
  and a new parser only if no existing one reads its catalog.
- **A new file-set parser** subclasses `FileSetParser`, sets `name`,
  `description` and `params`, implements `read`, and is registered by its
  module on import (add the import to `qiclib/volume/fileset/__init__.py`).
  `probe` comes free: it adds the standard checks and turns an exception into a
  fatal verdict, so a parser never crashes a probe.
- **A new warning** is a new `VolumeWarning` member with a one-line meaning in
  `WARNING_MEANINGS`. Never a free string: warnings travel in
  `identify --json`, the TWVL header, `inspect` and the tar's pax global
  header, and every consumer compares against members.

## Test

```sh
cd packages/qiclib
uv sync --extra dev
uv run pytest
```

Or `just check qiclib qiclib` from the repository root. There is one
`tests/test_<module>.py` per module. Build tape structures with
`qiclib.testing.builders`; extend the builders rather than hand-rolling bytes
in a test. `tests/test_volume_bench.py` and `tests/test_volume_profile.py`
pin the bench-tape verdicts, so a profile change that moves a real tape's
reading fails a test.

## Gotchas

- **`qiclib/types.py` shadows the stdlib `types` module** if you run Python
  with `packages/qiclib/qiclib` as the working directory or on `sys.path[0]`.
  Run scripts from elsewhere, or with `python -I`.
- **Nothing downstream re-decides.** `qicsilver inspect` and `tar` read the
  parser the TWVL header names. They probe every parser only for a header that
  names none. Do not add format sniffing downstream of `extract`.
- **Real tapes lie about their flags.** Never route a read on the vendor bit or
  the OS type; route on bytes the writer wrote. See
  [Writing a volume profile](volume-profiles.md#matching-rules).
- **Holes stay holes.** A byte range in an unrecovered segment is recorded as a
  hole in the TWVL, never shifted over.
- **`qiclib.volume.table` is the reference.** The `qic80-rev-n` and
  `qic113-rev-g` profiles must read every record as the fixed reading does;
  `tests/test_volume_profile.py` checks it.

## See also

- [Writing a volume profile](volume-profiles.md)
- [Cartridge and volume profiles](../qicsilver/profiles.md)
- [Formats](../formats/index.md)
