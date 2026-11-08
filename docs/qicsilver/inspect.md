# qicsilver inspect

Lists the files in a backup volume, like `tar tv`, without extracting
anything.

## Synopsis

```text
qicsilver inspect [OPTIONS] VOLUME|IMAGE [PATH]...
```

## Description

`inspect` takes either a TWVL volume (`extract` output) or a TWTI or TWTZ tape
image. It tells them apart by their magic bytes, not their file names, so a
renamed file still opens. A raw TWRF capture is refused with a message to run
`tw convert` first.

- **A TWVL volume** is read through the file-set parser its header names. If
  the header names none, every parser is probed and the first that reads the
  volume is used. `--volume`, `--volume-profile` and the override flags do not
  apply: the TWVL header already records how the volume was read.
- **A tape image** is read exactly as `extract` reads it, volume table and
  all, but nothing is written. A tape with one volume needs no `--volume`. A
  tape with several needs `--volume N`, the number `identify` shows; without
  it, the error says how many volumes there are.

`inspect` uses the same directory walk as [`tar`](tar.md), so its counts
always match what `tar` writes.

## Options

| Option | Default | Description |
|---|---|---|
| `--volume N` | none | Which volume of a tape image to list. Required when the image has more than one. A usage error with a TWVL file. |
| `--json` | off | Print the summary and the entries as JSON. |
| `--damaged` | off | List only damaged files. |
| `--volume-profile NAME\|PATH` | `guess` | Images only. See [profiles](profiles.md). |
| `--fileset-parser NAME` | the profile's | Images only. See [overrides](overrides.md). |
| `--fileset-param KEY=VALUE` | none | Images only. Repeatable. See [overrides](overrides.md). |
| `--extent-offset-bytes 4\|8` | the profile's | Images only. See [overrides](overrides.md). |
| `--extent-frame-size-bytes 2\|4` | the profile's | Images only. See [overrides](overrides.md). |
| `--vtbl-field NAME=OFFSET,LENGTH` | the profile's | Images only. Repeatable. See [overrides](overrides.md). |

Each `PATH` is a glob matched against the whole path of an entry (Python
`fnmatch`). An entry is listed if it matches any of them. Quote the globs so
the shell does not expand them: `'*.TXT'`, `'C:/WINDOWS/*'`.

## Output

A summary, a blank line, then one line per entry. Illustrative output from a
synthetic volume:

```text
tape name     EXAMPLE TAPE
volume        EXAMPLE BACKUP
date          1998-12-23 05:13:20
directory     QIC-113 Extended (qic113-extended parser, profile cms-qic113)
entries       3 files, 2 directories, 2 damaged
missing       50 bytes missing of 1.4 kB; 1 lost segment

drwxr-xr-x    0  1970-01-01 00:00:00           C:
drwxr-xr-x    0  1998-12-23 05:13:20           C:/DOCS
-r--r--r--   12  1998-12-23 05:13:20           C:/EXAMPLE.TXT
-rw-r--r--  100  1998-12-23 05:13:20  lost 50  C:/BROKEN.DAT
-rw-r--r--    4  1998-12-23 05:13:20  error    C:/DOCS/NOTES.TXT
```

| Summary line | Meaning |
|---|---|
| `tape name` | The name in the tape header. |
| `volume` | The volume's description from the volume table. |
| `date` | The backup date. |
| `directory` | The directory format (`QIC-113 Extended`, `QIC-113 Basic-DOS`, ...), the parser that read it, and the volume profile behind it. |
| `entries` | File, directory and damaged-file counts. |
| `missing` | Bytes that fell in unrecovered segments, out of the volume's size, and the number of lost segments. |
| `warnings` | The volume's warnings (`low-confidence`, `directory-damaged`, `manual`, ...), when there are any. |

Each entry line shows the type and mode, the size in bytes, the modification
time in UTC, the damage, and the path. The damage column is empty for an
intact file, and otherwise one of:

| Damage | Meaning |
|---|---|
| `lost N` | `N` of the file's bytes were in tape segments that could not be recovered. Re-dumping the bad tracks and converting again can bring them back. |
| `error` | The original backup software could not read the file when the backup was made (the QIC-113 "file error" bit). The tape never held its contents. |

### JSON

`--json` prints `{"summary": ..., "entries": [...]}`. The summary includes the
volume's number as `volume_index`, the `parser` that read it, and its
`warnings`.

## Examples

List the only volume of a tape image:

```sh
qicsilver inspect captures/NAME.twtz
```

List volume 1 of a tape that has several:

```sh
qicsilver inspect captures/NAME.twtz --volume 1
```

List an extracted volume:

```sh
qicsilver inspect captures/NAME-vols/vol-00.twvl
```

List only the damaged text files:

```sh
qicsilver inspect captures/NAME-vols/vol-00.twvl --damaged '*.TXT'
```

Print the listing as JSON:

```sh
qicsilver inspect --json captures/NAME-vols/vol-00.twvl
```

## See also

- [qicsilver tar](tar.md)
- [qicsilver extract](extract.md)
- [QIC-113 file sets](../formats/qic-113.md)
- [Keep captures private](../guide/privacy.md): listings of real tapes are
  private data.
