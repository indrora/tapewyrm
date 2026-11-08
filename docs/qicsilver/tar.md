# qicsilver tar

Writes the files of a backup volume to a POSIX (pax) tar archive, plus a
damage report.

## Synopsis

```text
qicsilver tar [OPTIONS] VOLUME|IMAGE OUTPUT
```

## Description

`tar` takes a TWVL volume or a TWTI or TWTZ tape image, routed exactly as
[`inspect`](inspect.md) routes it: told apart by magic bytes, `--volume N`
for an image with several volumes, and `--volume-profile` and the override
flags for an image only. A tape can be tarred without extracting it first;
no `.twvl` file is written.

The archive keeps the backup's own paths, including long Windows 95 names,
and modification times. Attributes go into pax extended headers:

| pax header | When |
|---|---|
| `TAPEWYRM.dos_attributes` | QIC-113 Extended volumes (Colorado and HP backup software). |
| `TAPEWYRM.qic113_attributes` | QIC-113 Basic-DOS volumes, for example the "MTN" tapes. |
| `TAPEWYRM.warnings` | The pax *global* header, when the volume carries warnings. |

Both QIC-113 directory formats are supported: Extended and Basic-DOS.

### Damaged files

A file with bytes in unrecovered segments is written with those bytes
zero-filled. `--skip-damaged` leaves such files out of the archive instead.
Either way, each damaged file gets one line in the damage report.

## Options

| Option | Default | Description |
|---|---|---|
| `--volume N` | none | Which volume of a tape image to tar. Required when the image has more than one. A usage error with a TWVL file. |
| `--report FILE` | `OUTPUT.damaged.txt` | Where to write the damage report. |
| `--skip-damaged` | off | Leave damaged files out of the tar instead of zero-filling them. |
| `--volume-profile NAME\|PATH` | `guess` | Images only. See [profiles](profiles.md). |
| `--fileset-parser NAME` | the profile's | Images only. See [overrides](overrides.md). |
| `--fileset-param KEY=VALUE` | none | Images only. Repeatable. See [overrides](overrides.md). |
| `--extent-offset-bytes 4\|8` | the profile's | Images only. See [overrides](overrides.md). |
| `--extent-frame-size-bytes 2\|4` | the profile's | Images only. See [overrides](overrides.md). |
| `--vtbl-field NAME=OFFSET,LENGTH` | the profile's | Images only. Repeatable. See [overrides](overrides.md). |

## Output

`tar` prints one line on stdout. Illustrative:

```text
captures/NAME.tar: 2000 files, 150 directories, 12 damaged (report: captures/NAME.tar.damaged.txt)
```

The damage report is a text file. Lines starting with `#` describe the volume:
the source file, the tape and volume names, the parser that read it, its
warnings, and the lost tape segments. Then there is one line per damaged file:
missing bytes, size, flag and path. Illustrative:

```text
# qicsilver damage report for captures/NAME-vols/vol-00.twvl
# tape 'EXAMPLE TAPE'; volume 'EXAMPLE BACKUP'
# read with the qic113-extended parser
# lost tape segments: 1 [417]
# missing_bytes / size   flag   path
#   lost  = bytes fell in tape segments we could not recover (zero-filled)
#   error = the original backup software itself could not read the file (QIC-113
#           'file error' bit; often a locked file, sometimes a placeholder name)
        50 / 100        lost   C:/BROKEN.DAT
         0 / 4          error  C:/DOCS/NOTES.TXT
```

| Flag | Meaning | What to do |
|---|---|---|
| `lost` | Some of the file's bytes were in tape segments that could not be recovered. For a Basic-DOS file whose data header was lost, all of them were. | Dump the affected tracks again and convert with every read; see [tw dump](../tw/dump.md) and [tw convert](../tw/convert.md). |
| `error` | The backup software could not read the file at backup time (the QIC-113 "file error" bit), often because the file was open or locked. | Nothing. The tape never held the contents, so no re-read helps. |

!!! warning
    The damage report and the tar hold real file names and contents. Keep them
    out of the repository. See [Keep captures private](../guide/privacy.md).

## Examples

Tar an extracted volume:

```sh
qicsilver --progress tar captures/NAME-vols/vol-00.twvl captures/NAME.tar
```

Tar volume 2 straight from the tape image:

```sh
qicsilver --progress tar captures/NAME.twtz captures/NAME-2.tar --volume 2
```

Leave damaged files out and put the report somewhere else:

```sh
qicsilver tar captures/NAME-vols/vol-00.twvl captures/NAME.tar --skip-damaged --report captures/NAME-damage.txt
```

## See also

- [qicsilver inspect](inspect.md): the same listing, without writing anything.
- [qicsilver extract](extract.md)
- [QIC-113 file sets](../formats/qic-113.md)
