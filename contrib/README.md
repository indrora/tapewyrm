# contrib: tools that work on dumped tapes

Utilities that operate on what `tw dump` produced, as opposed to the device.
They use the `tapewyrm` package from `packages/tapewyrm-host/`, so run them through its
environment from the repository root:

```bash
uv run --project packages/tapewyrm-host python contrib/<tool>.py ...
```

(`tools/` at the repository root holds build helpers used by `just`; these are
not those.)

## qic2tar.py: QIC-113 backup volume to tar

The last step of the recovery workflow:

```bash
cd host
uv run tw dump --tracks 0-12 --out ../captures/jc     # tape -> TWRF flux captures
uv run tw convert ../captures/jc -o ../captures/jc.twti   # -> logical tape image
uv run tw extract ../captures/jc.twti -o ../captures/jc-vols  # -> vol-NN.twvl
cd ..
just qic2tar captures/jc-vols/vol-00.twvl jc.tar      # -> tar
```

It turns a TWVL volume into a POSIX (pax) tar of the backup's files and
directories, with original names (long Windows 95 names), modification times
and DOS attributes (`TAPEWYRM.dos_attributes` pax header).

It writes a damage report next to the tar (`jc-1998.tar.damaged.txt`):

* `lost`: some of the file's bytes were in tape segments that could not be
  recovered; the tar holds them zero-filled (or use `--skip-damaged`).
* `error`: the original backup software could not read the file at backup
  time (QIC-113 "file error" bit), so the tape never had its contents.

Supports QIC-113 extended-format volumes (as written by Colorado/HP backup
software), compressed with QIC-122 or not; extracts the first volume.

`tw convert` is the slow step (decoding flux, about 20 s per track);
`tw extract` and qic2tar take seconds.
