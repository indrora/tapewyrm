# qicsilver extract

Writes one TWVL file per backup volume in a tape image.

## Synopsis

```text
qicsilver extract [OPTIONS] IMAGE [OUTDIR]
```

## Description

`extract` reads the volume table of a TWTI or TWTZ image through the volume
profile that [`identify`](identify.md) would pick, then builds each volume:
it decompresses QIC-122 data and lays the bytes out by their QIC-113 offsets.
Byte ranges that fell in unrecovered segments are recorded as holes in the
TWVL file. Nothing after a hole is shifted.

By default every volume is written to `OUTDIR/vol-NN.twvl`, where `NN` is the
volume's number in the volume table, as `identify` lists it. `OUTDIR` defaults
to the current directory. `extract` prints the path of each file it writes.

Once a volume is built, the profile's file-set parser **probes** it: does the
volume read as that backup program's catalog? The TWVL header records the
parser, the result of the probe and the reading's warnings
([TWS-3 TWVL](../spec/twvl.md) sections 3.4 and 3.5). If the parser fails the
probe, `extract` hands the volume to the next profile that would have built
the same bytes. A hand-tuned read is never handed over: its header names
`NAME+overrides`, the parser you gave and the `manual` warning, even when the
probe fails. See [Manual read overrides](overrides.md).

You do not need `extract` to look at a volume or to tar it: `inspect` and
`tar` both read a volume straight from the image. Use `extract` when you want
the volumes on disk, for example to keep them or to read them many times.

## Options

| Option | Default | Description |
|---|---|---|
| `--volumes LIST` | all | Volumes to extract, by number: `0`, `0,2`, `1-3`, `0,2-4`. A number the tape does not have is an error before anything is written. |
| `--prefix TEXT` | `vol-` | Output file names: `OUTDIR/PREFIXNN.twvl`. |
| `-o`, `--outfile FILE` | none | Write the one selected volume to `FILE`. Exactly one volume must be selected, by `--volumes N` or because the tape has only one. Not with `OUTDIR` or `--prefix`. |
| `--volume-profile NAME\|PATH` | `guess` | The volume profile to read the volume table with. See [profiles](profiles.md). |
| `--fileset-parser NAME` | the profile's | See [overrides](overrides.md). |
| `--fileset-param KEY=VALUE` | none | Repeatable. See [overrides](overrides.md). |
| `--extent-offset-bytes 4\|8` | the profile's | See [overrides](overrides.md). |
| `--extent-frame-size-bytes 2\|4` | the profile's | See [overrides](overrides.md). |
| `--vtbl-field NAME=OFFSET,LENGTH` | the profile's | Repeatable. See [overrides](overrides.md). |

## Examples

Extract every volume into a directory:

```sh
qicsilver --progress extract captures/NAME.twtz captures/NAME-vols
```

Extract volumes 0 and 2 to 4 only:

```sh
qicsilver extract captures/NAME.twtz captures/NAME-vols --volumes 0,2-4
```

Name the files `mytape-00.twvl`, `mytape-01.twvl` and so on:

```sh
qicsilver extract captures/NAME.twtz captures/NAME-vols --prefix mytape-
```

Write volume 1 to a file of your choosing:

```sh
qicsilver extract captures/NAME.twtz --volumes 1 -o docs.twvl
```

Extract with a forced volume profile:

```sh
qicsilver extract captures/NAME.twtz captures/NAME-vols --volume-profile mtn
```

## Output

One path per line, for each TWVL file written. Illustrative:

```text
captures/NAME-vols/vol-00.twvl
captures/NAME-vols/vol-01.twvl
```

## Exit status

`0` on success. `1` when the image cannot be read, a selected volume does not
exist, or `-o` is given for a tape with several volumes and no `--volumes N`.
`2` for a usage error: a malformed `--volumes` list, `-o` with `OUTDIR` or
`--prefix`, or `-o` with more than one volume selected.

## See also

- [qicsilver identify](identify.md): lists the volume numbers.
- [qicsilver inspect](inspect.md) and [qicsilver tar](tar.md): read the TWVL files.
- [QIC-122 compression](../formats/qic-122.md)
- [QIC-113 file sets](../formats/qic-113.md)
- [TWS-3 TWVL](../spec/twvl.md)
