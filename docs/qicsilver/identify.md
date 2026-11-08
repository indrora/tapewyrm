# qicsilver identify

Reports what is on a tape: the cartridge, the factory stamp, dates, bad
sectors and the volume table.

## Synopsis

```text
qicsilver identify [OPTIONS] IMAGE
```

## Description

`identify` reads the header segment and the volume table of a TWTI or TWTZ
tape image and prints them. Run it first on every tape: it tells you how many
backup volumes the tape holds, what number each one has, and which volume
profile and file-set parser the other commands will use to read them.

Both structures sit at the start of track 0, so an image converted from a
short capture of track 0 is enough. `identify` does not read raw captures;
run [`tw convert`](../tw/convert.md) on them first.

`identify` reads the volume table through the volume profile that fits best.
Every built-in profile whose fingerprint matches the tape is scored; the
others are eliminated. Use `--volume-profile` to force one. See
[Cartridge and volume profiles](profiles.md) for how the choice is made.

The report names the chosen profile, the file-set parser it implies, and any
warnings on the reading (`generic`, `low-confidence`, `untested`, `manual`
and others). The parser is named, not run: `identify` does not build the
volumes, so the line says `per the profile (not probed)`. `extract`,
`inspect` and `tar` probe the parser against the volume's bytes.

## Options

| Option | Default | Description |
|---|---|---|
| `--json` | off | Print the report as JSON instead of text. |
| `--raw` | off | Also print the raw format parameter record, every raw VTBL record as a hex dump, and every profile's checks with their points. Fingerprint checks are marked `[fingerprint]`. |
| `--volume-profile NAME\|PATH` | `guess` | The volume profile to read the volume table with: a built-in name, a path to a TOML file, or `guess` to score them all. See [profiles](profiles.md). |
| `--fileset-parser NAME` | the profile's | Read the volumes with this file-set parser. See [overrides](overrides.md). |
| `--fileset-param KEY=VALUE` | none | A parameter of the file-set parser. Repeatable. See [overrides](overrides.md). |
| `--extent-offset-bytes 4\|8` | the profile's | Width of the uncompressed-offset field at the head of each QIC-122 extent. See [overrides](overrides.md). |
| `--extent-frame-size-bytes 2\|4` | the profile's | Width of each compression frame's size field. See [overrides](overrides.md). |
| `--vtbl-field NAME=OFFSET,LENGTH` | the profile's | Place one VTBL field (bytes 57-127) by hand. Repeatable. See [overrides](overrides.md). |

`--raw` is not `-v`: `-v` is the global log-level option and goes before the
command name.

## Output

Illustrative output for a tape with one compressed volume:

```text
tape name     EXAMPLE  (named 1998-12-20 10:00:00)
manufacturer  -  (lot -)
              no factory stamp
cartridge     QIC-80, 425 ft x 0.250 in ...
format        code 5, ...
geometry      28 tracks x 207 segments = 5796 segments; ...
data area     segments 2-5795
formatted     1998-12-20 10:00:00  (first 1998-12-20 10:00:00, 1 times)
last written  1998-12-23 05:13:20
bad sectors   0 sectors + 0 whole segments in the map
header        segment 0 (clean); copies at 0 and 1
volume table  segment 2 (clean); 1 volume(s)
volume profile  cms-qic113 (score 14); others: qic113-rev-g (12), qic80-rev-n (3), mtn (-2, eliminated)
file set      qic113-extended parser, per the profile (not probed)

   #  segments     date                       size  compression  description
   0  3-120        1998-12-23 05:13:20     3.3 MB  QIC-122      EXAMPLE BACKUP  [EXAMPLE-PC]
```

The fields you will use most:

| Line | Meaning |
|---|---|
| `cartridge` | The cartridge type, from the cartridge profile that fits the header's track and segment counts. |
| `drive saw` | What the drive reported while dumping, from the TWRF headers. Present when the image was converted from captures that recorded it. |
| `bad sectors` | The bad-sector map written at format time. Those sectors were never used, so they are not losses. |
| `header`, `volume table` | The segment each was found in, and whether it was recovered `clean`, `corrected`, or not at all. |
| `volume profile` | The chosen profile and its score, then every other profile's score. `eliminated` means that profile's fingerprint did not match. |
| `file set` | The file-set parser the chosen profile names. |
| `warnings` | Caveats on the reading, when there are any. |
| volume rows | One row per volume: its number (`#`), segment range, date, size, compression (`no`, `QIC-122`, `code N` or `?`) and description. A source label follows in brackets. A size with `~` is the on-tape span, used when the profile does not know the section sizes. |
| `note:` | Anything the reader wants you to know, for example that no profile's fingerprint matched, or that two profiles tied. |

The volume number in the `#` column is the number `extract --volumes`,
`inspect --volume` and `tar --volume` take.

### JSON

`--json` prints one object. Its main members are `header`, `header_raw_hex`,
`bad_sectors`, `bad_segments`, `header_seg`, `header_state`, `vtbl_seg`,
`vtbl_state`, `cartridge`, `drive`, `volume_profile` (the chosen profile's
name), `volume_profile_scores` (name to score), `volume_profiles` (every
profile's full reading), `fileset_parser`, `warnings`, `volumes` (each with
its decoded fields and `raw_hex`) and `notes`.

## Examples

Identify a tape image:

```sh
qicsilver identify captures/NAME.twtz
```

Show every profile's score and checks, to see why one won:

```sh
qicsilver identify --raw captures/NAME.twtz
```

Force the Colorado backup layout:

```sh
qicsilver identify --volume-profile cms-qic113 captures/NAME.twtz
```

Read the volume table with a profile you wrote:

```sh
qicsilver identify --volume-profile ./my-backup.toml captures/NAME.twtz
```

Print the report as JSON and pull out the chosen profile with `jq`:

```sh
qicsilver identify --json captures/NAME.twtz | jq -r .volume_profile
```

## See also

- [Cartridge and volume profiles](profiles.md)
- [Manual read overrides](overrides.md)
- [Header and bad-sector map](../formats/header.md)
- [Volume table](../formats/volume-table.md)
- [qicsilver extract](extract.md)
