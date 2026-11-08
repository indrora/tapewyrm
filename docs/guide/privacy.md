# Keep captures private

Tapes bought second-hand are other people's backups. The bench tapes came from
a PC recycler and hold strangers' documents, mail and accounts. Treat every
capture, and everything made from it, as personal data.

## Rules

- **Never publish** captures, images, volumes, tars or damage reports, or
  anything from inside them: file names, tape labels, directory listings or
  file contents. Not in issues, pull requests, test fixtures, screenshots or
  logs.
- **Header and volume-table metadata is fine to share** when you report a
  problem: the format code, the geometry, the drive's report bytes, segment
  counts. `tw inspect` and `qicsilver identify` print these. Check the output
  first: the tape name and the volume descriptions were typed by the tape's
  owner, so remove them.
- **Keep the files in `captures/`.** The repository ignores `captures/`, and
  ignores these files wherever they are: `*.twrf`, `*.twti`, `*.twtz`,
  `*.twvl`, `*.damaged.txt` and `dump.jsonl`. Tar files are **not** ignored
  outside `captures/`, so write them there.
- **Never `git add -f`** anything under `captures/`.
- **Tests use synthetic tapes** built with `qiclib.testing`, or a blank
  factory-formatted tape. Never a real backup.
- **Logs can hold names.** The `tw convert` log prints the tape name at the
  default level, and `-v` adds file paths and more. Read a log before you
  attach it.

## See also

- [Read a tape](read-a-tape.md)
- [Configuration and logging](../tw/config.md)
