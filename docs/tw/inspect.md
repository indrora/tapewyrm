# tw inspect

Shows the header of a TWRF capture, a TWTI or TWTZ image, or a TWVL volume.

## Synopsis

```text
tw [GLOBAL OPTIONS] inspect [--json] FILE
```

## Description

`tw inspect` recognises the file by its magic bytes, not its name. It reads
only the header, plus an image's segment table for the segment counts, so it
returns at once even on a 1.75 GB image. A `.twtz` file is decompressed only
as far as its segment table. It needs no hardware.

By default it prints a readable summary: the drive's report bytes decoded,
the geometry, the segment counts and, for a volume, its holes. With `--json`
it prints the JSON header exactly as stored in the file, byte for byte, with
the keys in stored order, followed by one line break.

To see what is on a tape rather than how it was captured, use
[qicsilver identify](../qicsilver/identify.md).

## Options

| Option | Default | Description |
|---|---|---|
| `FILE` | | A `.twrf`, `.twti`, `.twtz` or `.twvl` file. |
| `--json` | off | Print the header exactly as stored, instead of the summary. |

## Output

An excerpt of the summary for an image:

```text
Segments
╭───────────────┬─────────────────╮
│ missing       │ 57,431 (97.34%) │
│ clean         │ 1,445 (2.45%)   │
│ corrected     │ 12 (0.02%)      │
│ uncorrectable │ 14 (0.02%)      │
│ bad           │ 98 (0.17%)      │
╰───────────────┴─────────────────╯
...
Drive
╭─────────────────┬──────────────────────────────────────────────────────╮
│ Drive config    │ 0xd8 -> 1,000 kbit/s, QIC-80 mode, extra-length tape │
│ Vendor          │ 0x11c6 -> Colorado Memory Systems (make 71, model 6) │
│ Tape status     │ 0x63 -> QIC3020, variable length 900 Oe              │
╰─────────────────┴──────────────────────────────────────────────────────╯
```

(From a blank, factory-formatted QIC-Extra tape.) The header fields of each
format are defined in [TWS-1 TWRF](../spec/twrf.md),
[TWS-2 TWTI/TWTZ](../spec/twti.md) and [TWS-3 TWVL](../spec/twvl.md).

## Examples

Show the summary for an image:

```sh
tw inspect captures/mytape.twtz
```

Show which drive and rate recorded a capture:

```sh
tw inspect captures/mytape/track-00.twrf
```

Extract the geometry from an image's stored header with `jq`:

```sh
tw inspect --json captures/mytape.twtz | jq .geometry
```

## Exit status

`0` on success. `1` if the file is not one of the three formats or cannot be
read.

## See also

- [tw convert](convert.md)
- [qicsilver identify](../qicsilver/identify.md)
- [qicsilver inspect](../qicsilver/inspect.md), which lists the files in a
  volume
