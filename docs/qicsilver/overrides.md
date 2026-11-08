# Manual read overrides

Override the layers of a volume profile by hand, on the command line, to read
a tape that no profile fits yet. The same flags work on `qicsilver identify`,
`extract`, `inspect` and `tar`, and on `cinnabar`.

## Description

A new tape often fits a shipped volume profile in most layers but not all: the
volume table reads fine, but the volumes want a different parser, a 4-byte
extent offset, or a label four bytes to the left. Writing a TOML before you
have looked at the tape is the wrong order of work. The override flags let you
try a layout first, check it in `identify`, `inspect` and `cinnabar`, and write
the profile once it works.

Each flag replaces one value of the profile that reads the tape, whether you
forced that profile with `--volume-profile` or `guess` chose it. Anything you
do not override keeps the profile's value. For the layers themselves, see
[Cartridge and volume profiles](profiles.md#volume-profiles).

## Options

| Option | Layer | Description |
|---|---|---|
| `--fileset-parser NAME` | `[fileset]` | Read the volumes with this file-set parser instead of the profile's. One of `none`, `qic113-basic`, `qic113-extended`, `qicstream-fset`. |
| `--fileset-param KEY=VALUE` | `[fileset]` | Set one parameter of the parser. Repeatable. |
| `--extent-offset-bytes 4\|8` | `[extent]` | Width of the uncompressed-offset field at the head of each QIC-122 extent. QIC-113 Rev G uses 8; the MTN tapes use 4. |
| `--extent-frame-size-bytes 2\|4` | `[extent]` | Width of each compression frame's size field. QIC-113 Rev G uses 2; NovaStor uses 4. |
| `--vtbl-field NAME=OFFSET,LENGTH` | `[vtbl]` | Place one VTBL field at byte `OFFSET`, `LENGTH` bytes long, replacing the profile's placement of it or adding one. Repeatable. |

The parsers:

| Parser | Reads |
|---|---|
| `qic113-extended` | QIC-113 Extended-OS volumes, as written by Colorado and HP backup software. |
| `qic113-basic` | QIC-113 Basic-DOS volumes, directory first. |
| `qicstream-fset` | QICStream v2 control-code streams. Untested on a real tape. |
| `none` | Nothing: the volume is extracted to TWVL but its files cannot be listed. |

The VTBL field names, and what they hold:

| Field | Contents |
|---|---|
| `multi_cartridge_seq` | Sequence number of this cartridge in a multi-cartridge backup. |
| `dir_section_size` | Size of the directory section, little endian. |
| `data_section_size` | Size of the data section, little endian. |
| `source_label` | Space-filled ASCII label, usually the source drive or machine. |
| `compression` | One byte: bit 7 set means compressed; bits 0-5 are the QIC-123 code. |
| `os_type` | One byte: the operating system the backup came from. |

## Rules

**Values are checked before any file is opened.** A parser that does not
exist, a width other than the ones listed, an unknown field name, a field that
does not lie entirely in bytes 57-127, or a value without the `KEY=VALUE` or
`NAME=OFFSET,LENGTH` shape is a usage error (exit status 2). `OFFSET` and
`LENGTH` are decimal. Bytes 0-56 are fixed by the standard and cannot be moved.

**Parameters are checked against the parser that will read the volume**: the
one you gave with `--fileset-parser`, or else the profile's. A key that parser
does not take is an error (exit status 1) that lists the keys it accepts.
Values are converted to the type of the parser's default for that key:
integers, numbers, or booleans (`true`, `false`, `yes`, `no`, `on`, `off`,
`1`, `0`).

!!! note
    None of the built-in parsers takes parameters yet, so `--fileset-param`
    is refused for every one of them today. The flag is there for parsers
    that will.

**Changing the parser drops the profile's parameters.** They belonged to the
old parser. The new parser starts from its own defaults, plus any
`--fileset-param` you give.

**The fingerprint and the choice stay the profile's.** The overrides say how
to read what the software wrote, not who wrote it. With `guess`, the profiles
are scored on their own layouts, the winner is chosen, and only then is the
winner re-read with your overrides. The tape is never re-ranked, so a moved
field cannot hand it to a different profile. Warnings the selection earned,
such as `low-confidence`, carry over.

**Overrides apply to every volume on the tape.** There is no per-volume form.

**A tape with no volume table** gets nothing from the overrides. `identify`
prints a note saying they were not applied.

## How a hand-tuned read is labelled

A read with any override is named `PROFILE+overrides` (for example
`cms-qic113+overrides`) and carries the `manual` warning wherever the
profile's name appears:

| Where | What you see |
|---|---|
| `qicsilver identify` | The `volume profile` line names `PROFILE+overrides`, the `warnings` line includes `manual`, and a `note:` line lists what was overridden. |
| `identify --json` | `volume_profile` is `PROFILE+overrides`; `warnings` includes `manual`. |
| TWVL header (`extract`) | The profile name, the parser you gave, and the `manual` warning. |
| `inspect`, `tar` | The `directory` and `warnings` lines; the tar's pax global header and the damage report. |
| `cinnabar` | The tape's rows name `PROFILE+overrides`, the Volume fields lane draws the fields where you placed them, and `manual` joins the amber warnings line in the Details panel. |

**`extract` never hands a hand-tuned volume to another profile.** Normally,
when a parser fails its probe, `extract` tries the next profile that would
have built the same bytes. With overrides it keeps your reading and records
the failed probe in the TWVL header, so you can see that the layout is wrong.

**A TWVL file already records its reading.** `inspect` and `tar` on a `.twvl`
file ignore the override flags and use the parser the header names. To change
the reading, extract again with different flags, or give `inspect` or `tar`
the tape image.

!!! note
    In `cinnabar`, the overrides apply to the file you name on the command
    line. A file opened later with File > Open is read with `guess` and no
    overrides.

## Examples

Read the volumes with the Basic-DOS parser and place the source label at byte
102:

```sh
qicsilver identify captures/NAME.twtz --fileset-parser qic113-basic --vtbl-field source_label=102,16
```

Try 4-byte extent offsets and list volume 0 without writing anything:

```sh
qicsilver inspect captures/NAME.twtz --volume 0 --extent-offset-bytes 4
```

Look at the same layout in the viewer:

```sh
cinnabar captures/NAME.twtz --fileset-parser qic113-basic --vtbl-field source_label=102,16
```

Force a profile and override two of its fields:

```sh
qicsilver identify captures/NAME.twtz --volume-profile qic113-rev-g --vtbl-field data_section_size=96,4 --vtbl-field compression=120,1
```

When the reading looks right, extract with the same flags:

```sh
qicsilver extract captures/NAME.twtz captures/NAME-vols --fileset-parser qic113-basic --vtbl-field source_label=102,16
```

## Next steps

The flags do not save anything. When a set of overrides works, write it down as
a volume profile TOML so the next tape from the same software is recognised
by its fingerprint. See [Writing a volume profile](../dev/volume-profiles.md).

## See also

- [Cartridge and volume profiles](profiles.md)
- [qicsilver identify](identify.md)
- [cinnabar](../cinnabar/index.md)
- [Volume table](../formats/volume-table.md)
- [QIC-122 compression](../formats/qic-122.md)
