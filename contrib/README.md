# contrib: tools that work on dumped tapes

Utilities that operate on what `tw dump` produced, as opposed to the device.
They use the `tapewyrm` package from `host/`, so run them through its
environment from the repository root:

```bash
uv run --project host python contrib/<tool>.py ...
```

(`tools/` at the repository root holds build helpers used by `just`; these are
not those.)

## qic2tar.py: QIC-80 backup to tar

Turns a dump directory (`track-NN.raw` files) into a POSIX (pax) tar of the
backup's files and directories, with original names (long Windows 95 names),
modification times and DOS attributes (`TAPEWYRM.dos_attributes` pax header).

```bash
uv run --project host python contrib/qic2tar.py captures/jc-1998 -o jc-1998.tar
# or
just qic2tar captures/jc-1998 jc-1998.tar
```

It writes a damage report next to the tar (`jc-1998.tar.damaged.txt`):

* `lost`: some of the file's bytes were in tape segments that could not be
  recovered; the tar holds them zero-filled (or use `--skip-damaged`).
* `error`: the original backup software could not read the file at backup
  time (QIC-113 "file error" bit), so the tape never had its contents.

Supports QIC-113 extended-format volumes (as written by Colorado/HP backup
software), compressed with QIC-122 or not; extracts the first volume.

Decoding the flux is the slow part (about 20 s per track); the recovered
sectors are cached in `DUMP_DIR/sectors.pkl`, so later runs take seconds.
