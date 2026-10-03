# qicsilver

Read QIC tape images and get the files back. It starts where the hardware
stops: `tw dump` reads the tape, `tw convert` turns the captures into a
**TWTI** tape image, and qicsilver does everything after that, offline.

```bash
cd packages/tapewyrm-cli
uv run tw dump --tracks 0-12 --out ../../captures/jc         # tape -> TWRF flux captures
uv run tw convert ../../captures/jc -o ../../captures/jc.twti # -> TWTI tape image
cd ../qicsilver
uv run qicsilver identify ../../captures/jc.twti              # what is on it
uv run qicsilver extract ../../captures/jc.twti -o ../../captures/jc-vols  # -> vol-NN.twvl
uv run qicsilver tar ../../captures/jc-vols/vol-00.twvl -o jc.tar          # -> tar + damage report
```

`tw convert` is the slow step (decoding flux, about 20 s per track);
everything here takes seconds.

## Commands

- **identify**: cartridge, factory stamp, dates, bad sectors and the volume
  table, read through the tape profile that fits (`--tape-profile` to force
  one, `--json` for machines, `--raw` for the raw records).
- **extract**: one TWVL file per backup volume. QIC-122 is decompressed and
  each volume is laid out by its QIC-113 offsets; byte ranges in unrecovered
  segments are recorded as holes, never silently shifted.
- **tar**: a POSIX (pax) tar of the volume's files and directories, with
  original names (long Windows 95 names), modification times and DOS
  attributes (`TAPEWYRM.dos_attributes` pax header). Both QIC-113 directory
  formats work: **extended** (Colorado/HP backup software) and **Basic-DOS**
  (e.g. the "MTN" tapes; QIC-113 attribute bits in `TAPEWYRM.qic113_attributes`).
  It also writes a damage report next to the tar (`OUT.damaged.txt`):
  - `lost`: some (or, for a Basic-DOS file whose data header was lost, all) of
    the file's bytes were in tape segments that could not be recovered; the tar
    holds them zero-filled (or use `--skip-damaged`).
  - `error`: the original backup software could not read the file at backup
    time (QIC-113 "file error" bit), so the tape never had its contents.

Global flags, the same as `tw`: `--progress` (bars on stderr), `-v` (debug),
`-q` / `-qq`. Results go to stdout, logs to stderr.

## Layout

Depends on `qiclib` (formats and layout) and `tapewyrm-archive` (TWTI/TWVL),
never on the hardware package. `qicsilver/console.py` is a deliberate copy of
`tapewyrm-cli`'s; keep them in step (STYLE.md §2).
