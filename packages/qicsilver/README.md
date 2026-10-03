# qicsilver

Read QIC tape images and get the files back. qicsilver starts where the
hardware stops: `tw dump` reads the tape, `tw convert` turns the captures into
a **TWTI** tape image (or **TWTZ**, the same image zstd-compressed), and
qicsilver does everything after that, offline.

```sh
# from the repository root
uv run --project packages/tapewyrm-cli tw --profile colorado dump --tracks 0-27 --out captures/NAME
uv run --project packages/tapewyrm-cli tw convert captures/NAME -o captures/NAME.twtz
uv run --project packages/qicsilver qicsilver identify captures/NAME.twtz
uv run --project packages/qicsilver qicsilver extract captures/NAME.twtz -o captures/NAME-vols
uv run --project packages/qicsilver qicsilver tar captures/NAME-vols/vol-00.twvl -o captures/NAME.tar
```

`tw convert` is the slow step (roughly a quarter of the tape's running time: a
9-minute QIC-3020 track takes about 2.5 minutes); everything here takes
seconds to a few minutes. `just qicsilver ARGS`
runs the same thing. The [quick start](../../docs/quickstart.md) walks through
it with example output.

## Commands

Global flags, the same as `tw`'s: `--progress` (bars on stderr), `-v` (debug),
`-q` / `-qq`. Results go to stdout, logs to stderr. (qicsilver has no
`--port`, `--profile` or `--config`; it never touches hardware.)

- **`identify IMAGE`**: cartridge, factory stamp, dates, bad sectors and the
  volume table, read through the volume profile that fits best.
  `--volume-profile NAME|PATH` forces one (default `guess`), `--json` prints
  machine-readable JSON, `--raw` adds the raw records and the profile scoring.
  Only the header and the volume table at the start of track 0 are needed, so
  a short capture of track 0 is enough.
- **`extract IMAGE -o DIR`**: one TWVL file (`DIR/vol-NN.twvl`) per backup
  volume. QIC-122 data is decompressed and each volume is laid out by its
  QIC-113 offsets; byte ranges in unrecovered segments are recorded as holes,
  never silently shifted. Takes `--volume-profile` like `identify`.
- **`tar VOLUME -o OUT.tar`**: a POSIX (pax) tar of the volume's files and
  directories, with the original names (long Windows 95 names), modification
  times and DOS attributes (pax header `TAPEWYRM.dos_attributes`). Both QIC-113
  directory formats work: **extended** (Colorado/HP backup software) and
  **Basic-DOS** (e.g. the "MTN" tapes; QIC-113 attribute bits in
  `TAPEWYRM.qic113_attributes`). It also writes a damage report,
  `OUT.tar.damaged.txt` (or `--report PATH`), one line per damaged file:
  - `lost`: some of the file's bytes (or, for a Basic-DOS file whose data
    header was lost, all of them) were in tape segments that could not be
    recovered. The tar holds them zero-filled; `--skip-damaged` leaves the file
    out instead.
  - `error`: the original backup software could not read the file at backup
    time (QIC-113 "file error" bit), so the tape never had its contents.

## Modules

| Module | What |
|---|---|
| `qicsilver.cli` | The `qicsilver` command group (rich-click). `identify` and `extract` call `qiclib.identify` and `qiclib.extract`. |
| `qicsilver.tar` | TWVL volume -> pax tar + damage report. |
| `qicsilver.console` | Log handler and progress bars: a deliberate copy of `tapewyrm.console`; keep them in step ([STYLE.md](../../STYLE.md) §2). |

Depends on [qiclib](../qiclib/README.md) (layout and backup formats) and
[tapewyrm-archive](../tapewyrm-archive/README.md) (TWTI/TWTZ and TWVL), never
on the hardware package. The formats are specified in
[docs/spec](../../docs/spec/README.md).

## Develop

```sh
cd packages/qicsilver
uv sync --extra dev
uv run pytest
```

Or, from the repository root, `just check qicsilver qicsilver`.
