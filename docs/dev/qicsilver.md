# qicsilver

`qicsilver` is the CLI that turns a TWTI/TWTZ tape image back into files:
`identify`, `extract`, `inspect` and `tar`. It never touches hardware. Most of
the work is in `qiclib`; this package owns the command line, the directory walk
`tar` and `inspect` share, and the tar writer. For the commands themselves, see
the [qicsilver reference](../qicsilver/index.md).

## Package facts

| | |
|---|---|
| Directory | `packages/qicsilver` |
| Distribution | `qicsilver` |
| Import name | `qicsilver` |
| Scripts | `qicsilver` (`qicsilver.cli:cli`) |
| Depends on | `qiclib`, `tapewyrm-archive` (editable path sources); `rich`, `rich-click` |
| Depended on by | nothing |

It has no dependency on `tapewyrm-cli`, so it installs and runs with no
hardware stack.

## Module map

| Module | Purpose |
|---|---|
| `qicsilver.cli` | The `qicsilver` command group. `identify` and `extract` call `qiclib.identify` and `qiclib.extract`. `inspect IMAGE` and `tar IMAGE` build one volume in memory with `qiclib.extract.load_volume`, through one shared routing (magic, `--volume`, error wording). Also defines the volume-read options (`--volume-profile` and the hand-tuning flags). |
| `qicsilver.entries` | The one directory walk `tar` and `inspect` share: a volume through the parser its TWVL header names, into a `VolumeListing` (the volume, its VTBL record, the parser's name, one `VolumeEntry` per entry and the volume's warnings). `read_volume` (from a `.twvl`), `list_volume` (from a `Volume` in memory), `entry_data`. |
| `qicsilver.tar` | Entries to a pax tar and the damage report. |
| `qicsilver.inspect` | Entries to the `inspect` listing, its filters and its `--json` document. |
| `qicsilver.console` | Rich console, log handler and progress bars. A deliberate copy of `tapewyrm.console`; keep the copies in step. |

## Data flow

```text
IMAGE (.twti/.twtz)
  ├─ identify ─► qiclib.identify ─► report / --json
  ├─ extract  ─► qiclib.extract  ─► OUTDIR/vol-NN.twvl
  └─ inspect / tar IMAGE ─► qiclib.extract.load_volume ─┐
                                                         ├─► qicsilver.entries ─► qicsilver.inspect | qicsilver.tar
VOLUME (.twvl) ──────────────────────────────────────────┘
```

`inspect` and `tar` accept either a TWVL volume or an image, told apart by
magic. An image is read exactly as `extract` reads it, in memory, so a tape
can be tarred without writing TWVL files first.

## Key types and extension points

- **The volume-read options** are one click decorator,
  `volume_read_options` in `qicsilver.cli`, applied to `identify`,
  `extract`, `inspect` and `tar`. `cinnabar.cli` has an identical copy,
  because cinnabar may not import qicsilver. A new volume-read flag goes into
  both copies, with the same words. The flags build a
  `qiclib.volume.profile.Overrides`; see
  [Manual read overrides](../qicsilver/overrides.md).
- **`entries` is the only walk.** `tar` and `inspect` must list the same
  entries with the same damage. Put any new per-entry field in
  `qicsilver.entries` so both get it.
- **Parser dispatch** comes from the TWVL header: `extract` stamps the parser,
  its probe and the warnings into the header (TWS-3 sections 3.4 and 3.5).
  `entries` reads that parser; only a header that names none is probed with
  every parser (`qiclib.volume.fileset.probe_all`).
- **Warnings** come from `qiclib.volume.verdict.VolumeWarning`. `tar` writes
  them into the pax global header as `TAPEWYRM.warnings` and into the damage
  report; `inspect --json` puts them in its summary.

## Test

```sh
cd packages/qicsilver
uv sync --extra dev
uv run pytest
```

Or `just check qicsilver qicsilver` from the repository root. The tests build
synthetic volumes and images with `qiclib.testing.builders` (`build_twvl`,
`build_ext_volume`, `build_fset_volume` and friends). `tests/test_cli.py`
drives the commands end to end through click's runner.

## Gotchas

- **No hardware flags.** qicsilver has no `--port`, `--profile` or `--config`.
  It does have the global `--progress`, `-v` and `-q`, with the same meaning
  as in `tw`.
- **Results to stdout, logs to stderr.** `--json` output must stay parseable;
  never log to stdout.
- **Do not re-decide the format.** If `inspect` or `tar` lists a volume wrongly,
  fix the profile or the parser in qiclib, not the walk here.
- **Never commit real listings.** Test fixtures are synthetic. A listing from a
  real tape is somebody's files.

## See also

- [qicsilver reference](../qicsilver/index.md)
- [qiclib](qiclib.md)
- [TWS-3 TWVL](../spec/twvl.md)
