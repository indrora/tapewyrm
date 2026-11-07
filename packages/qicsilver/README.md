# qicsilver

Read QIC tape images and get the files back. qicsilver starts where the
hardware stops: `tw dump` reads the tape, `tw convert` turns the captures into
a **TWTI** tape image (or **TWTZ**, the same image zstd-compressed), and
qicsilver does everything after that, offline.

```sh
# from the repository root
uv run --project packages/tapewyrm-cli tw --profile colorado dump captures/NAME
uv run --project packages/tapewyrm-cli tw convert captures/NAME captures/NAME.twtz
uv run --project packages/qicsilver qicsilver identify captures/NAME.twtz
uv run --project packages/qicsilver qicsilver inspect captures/NAME.twtz --volume 0
uv run --project packages/qicsilver qicsilver extract captures/NAME.twtz captures/NAME-vols
uv run --project packages/qicsilver qicsilver inspect captures/NAME-vols/vol-00.twvl
uv run --project packages/qicsilver qicsilver tar captures/NAME-vols/vol-00.twvl captures/NAME.tar
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
  volume table, read through the volume profile that fits best: every
  profile whose fingerprint (bytes the backup program wrote) matches is
  scored for plausibility, the rest are eliminated. The report names the
  profile, the file-set parser it implies and any **warnings** (`generic`,
  `low-confidence`, `untested`, ...; `qiclib.volume.verdict`).
  `--volume-profile NAME|PATH` forces one (default `guess`), `--json` prints
  machine-readable JSON, `--raw` adds the raw records and every profile's
  checks, fingerprint items marked. Only the header and the volume table at
  the start of track 0 are needed, so a short capture of track 0 is enough.
- **`extract IMAGE [OUTDIR]`**: one TWVL file (`OUTDIR/vol-NN.twvl`, OUTDIR
  defaulting to `.`, NN the volume's number in the volume table) per backup
  volume. QIC-122 data is decompressed and each volume is laid out by its
  QIC-113 offsets; byte ranges in unrecovered segments are recorded as holes,
  never silently shifted. `--volumes LIST` picks volumes (`0`, `0,2`, `1-3`,
  `0,2-4`; a number the tape lacks is an error before anything is written),
  `--prefix P` renames the files (`P00.twvl`), `-o FILE` writes the one
  selected volume to FILE (exactly one volume; not with OUTDIR or `--prefix`).
  Takes `--volume-profile` like `identify`. Once a volume is built, the
  profile's file-set parser **probes** it (does it read as that software's
  catalog?); the header records the parser and the probe (TWS-3 section
  3.4) and the warnings (3.5), and a parser that fails hands over to the
  next profile that would have built the same bytes.
- **`inspect VOLUME|IMAGE [PATH]...`**: list a volume like `tar tv`, without
  extracting it. The file is a TWVL volume or a TWTI/TWTZ image, told apart
  by magic; an image is read as `extract` reads it, but nothing is written.
  A tape with one volume lists it; a tape with several needs `--volume N`
  (the number `identify` shows), and `--volume-profile` applies as for
  `extract`. A summary (tape name, volume label, date, directory format,
  file / directory / damaged counts, missing bytes, lost segments), then one
  line per entry: type and mode, size, modification time (UTC), damage
  (`lost N` / `error`, as in tar's damage report) and path. `PATH` globs
  (fnmatch over the whole path) and `--damaged` narrow the listing; `--json`
  prints `{"summary": ..., "entries": [...]}`, the summary naming the
  volume's number as `volume_index`, the `parser` that read it and its
  `warnings`. It shares tar's directory walk (`qicsilver.entries`), so the
  two always agree: both read the parser the TWVL header names, and probe
  every parser only for a header that names none.
- **`tar VOLUME|IMAGE OUT.tar`**: a POSIX (pax) tar of the volume's files and
  directories, with the original names (long Windows 95 names), modification
  times and DOS attributes (pax header `TAPEWYRM.dos_attributes`). The input
  is a TWVL volume or a TWTI/TWTZ image, routed exactly as `inspect` routes
  it (`--volume N` for a tape with several volumes, `--volume-profile` as for
  `extract`), so a tape can be tarred without extracting it first. Both QIC-113
  directory formats work: **extended** (Colorado/HP backup software) and
  **Basic-DOS** (e.g. the "MTN" tapes; QIC-113 attribute bits in
  `TAPEWYRM.qic113_attributes`). The volume's warnings go into the tar's
  pax *global* header as `TAPEWYRM.warnings` and into the damage report.
  It also writes a damage report,
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
| `qicsilver.cli` | The `qicsilver` command group (rich-click). `identify` and `extract` call `qiclib.identify` and `qiclib.extract` (which also selects and names the volumes); `inspect IMAGE` and `tar IMAGE` build one volume in memory with `qiclib.extract.load_volume`, through the one routing (magic, `--volume`, error wording) both commands share. |
| `qicsilver.entries` | Reads a TWVL volume through the file-set parser its header names (`qiclib.volume.fileset`; every parser probed when it names none) into one record per entry: path, size, mtime, attributes and mode, data offset, missing bytes, error flag, plus the volume's warnings. The walk `tar` and `inspect` share, from a `.twvl` file or a volume read straight out of an image. |
| `qicsilver.tar` | Those entries -> pax tar + damage report. |
| `qicsilver.inspect` | Those entries -> the `inspect` listing, filters and `--json` document. |
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
