# Writing a volume profile

This page shows you how to add a volume profile, for a backup program whose
tapes no shipped profile reads, and a cartridge profile, for a cartridge
`qicsilver identify` does not name. Both are TOML files; neither needs a code
change. For what profiles are and how `qicsilver` chooses one, see
[Cartridge and volume profiles](../qicsilver/profiles.md).

| Profile | Describes | Directory | Loader |
|---|---|---|---|
| Volume | How one backup program laid out its volume-table records, extents and catalog | `packages/qiclib/qiclib/volume/profiles/` | `qiclib.volume.profile` |
| Cartridge | A physical cartridge: standard, tracks, width, length | `packages/qiclib/qiclib/profiles/cartridge/` | `qiclib.cartridge` |

Drive profiles are a third kind, in tapewyrm-cli. See
[tapewyrm-cli](tapewyrm-cli.md#drive-profiles).

## How a volume profile is used

A volume is read in three layers, and a volume profile names all three:

1. **`[vtbl]`**: where the fields in bytes 57-127 of each 128-byte `VTBL`
   record sit. QIC-80-MC Rev N §8 fixes only bytes 0-56 for every writer.
2. **`[extent]`**: how wide each QIC-122 extent's uncompressed-offset field and
   each frame's size field are.
3. **`[fileset]`**: which file-set parser reads the volume's catalog.

`qicsilver identify` and `extract` run every built-in profile against the
volume table (`--volume-profile guess`, the default):

1. **Gate.** Each `[match]` item is checked against the record. A profile that
   fails any item is eliminated for this tape.
2. **Score.** Each survivor decodes every record and is scored with
   plausibility checks: is the segment range in the data area, is the date
   sane, is the label printable, is the compression code known, does the
   claimed size fit the segments. Each check is +1 or -1; gate items count
   +2 or -2 in the `--raw` display. The highest total wins.
4. **Probe.** Once `extract` has built the volume, the winner's parser reads
   it and returns a verdict. A parser that fails hands the volume to the next
   survivor that would have built the same bytes.

The winner carries `low-confidence` when the runner-up is within
`CLOSE_MARGIN` (2) points, and `vtbl-ambiguous` when two profiles read the
table identically.

## Volume profile reference

```toml
name = "mtn"
description = "'MTN' at VTBL byte 58, vendor bit clear, label at 102 (3M DC2120 bench tape)"
status = "bench"
tapes = ["3m-unknown-1", "old-connor"]
warnings = []

[match]
vtbl_bytes = [[58, "4d544e"]]   # "MTN"

[vtbl]
multi_cartridge_seq = [57, 1]
dir_section_size = [92, 4]
data_section_size = [96, 4]
source_label = [102, 16]
compression = [120, 1]

[extent]
offset_bytes = 4

[fileset]
parser = "qic113-basic"
```

### Top-level keys

| Key | Required | Default | Description |
|---|---|---|---|
| `name` | Yes | | The profile's name. `--volume-profile NAME` loads `NAME.toml` from the profiles directory, so make the file name match. |
| `description` | No | `""` | One line. The built-in profiles test requires one for every shipped profile. |
| `status` | No | `"untested"` | `"spec"` (from a standard), `"bench"` (confirmed on the tapes in `tapes`) or `"untested"` (from a spec or another program's reader; raises `untested`). |
| `tapes` | No | `[]` | Names of the bench tapes the profile was confirmed on. A `bench` profile with one tape raises `single-tape`. |
| `warnings` | No | `[]` | Static warnings the profile always raises, by `VolumeWarning` name (for example `"special-fs"`). An unknown name fails at load. |

### `[match]`

Every item is a gate: all must hold. A profile with no `vtbl_bytes`,
`volume_bytes` or `format_codes` has no positive fingerprint, only ever wins
by default, and raises `generic`.

| Key | Form | Checks |
|---|---|---|
| `vtbl_bytes` | `[[offset, "hex"], ...]` | These bytes at this offset in the `VTBL` record. Must lie within the 128 bytes. |
| `format_codes` | `[code, ...]` | The header's format code is one of these. |
| `vendor_bit` | `true` / `false` | `VTBL` byte 56 bit 0. Use only when the layout depends on it (see below). |
| `volume_bytes` | `[[offset, "hex"], ...]` | These bytes at this offset from the start of the built volume. Checked by `extract`, not at the table stage. |

Any other key in `[match]` fails at load.

### `[vtbl]`

Each field is `[offset, length]` in the record and must lie within bytes
57-127. Numeric fields are little-endian. Leave a field out when its place is
unknown.

| Field | Meaning |
|---|---|
| `multi_cartridge_seq` | Cartridge number within a multi-cartridge set. |
| `dir_section_size` | Size of the directory section, in bytes. |
| `data_section_size` | Size of the data section, in bytes. |
| `source_label` | Space-filled ASCII label. |
| `compression` | One byte: bit 7 set means compressed, bits 0-5 are the QIC-123 code (1 = QIC-122). |
| `os_type` | One byte: the OS type. |

Any other field name fails at load.

### `[extent]`

| Key | Values | Default | Meaning |
|---|---|---|---|
| `offset_bytes` | `4`, `8` | `8` | Width of each data segment's uncompressed-offset field. QIC-113 Rev G says 8; the MTN tapes use 4. |
| `frame_size_bytes` | `2`, `4` | `2` | Width of each compressed frame's size field. Rev G says 2; NovaStor uses 4. |

### `[fileset]`

| Key | Default | Meaning |
|---|---|---|
| `parser` | `"none"` | A registered parser: `qic113-basic`, `qic113-extended`, `qicstream-fset` or `none` (recognised, but nothing can list it; raises `no-parser`). |
| any other key | | A parameter of that parser. The parser's `params` declares the keys it accepts and their defaults; any other key fails at load. No built-in parser takes parameters yet. |

## Matching rules

- **Match on bytes the writer wrote, never on a flag it may or may not have
  set.** `113` at byte 58, `"CMS"` at 62, `"MTN"` at 58 are good fingerprints.
  The CMS profile once matched on the vendor-specific bit, which the same
  software set on a QIC-80 cartridge and cleared on a QIC-3010 one. The
  QIC-3010 tape was routed to the Rev N layout and the Basic-DOS parser, which
  listed 186 nameless empty files without complaint.
- **A failed gate item is final.** Do not add an item you are not sure every
  tape from that writer carries.
- **A profile that fingerprints only the volume** (`volume_bytes` alone, like
  `qicstream-v2`'s `FSET`) has nothing the table stage can check. It is ranked
  behind every survivor the table vouches for and is chosen only when the
  winner's parser fails.
- **An `untested` profile must earn its place.** It needs a positive
  fingerprint or a parser other than `none`. Otherwise it survives every gate,
  fails every probe and is noise in `identify --raw`.
- **Cite where every offset came from**, in a comment: the standard and section,
  or the bench tape and the bytes you read. See the comment block at the top of
  `mtn.toml` and `cms-qic113.toml`.

## Add a volume profile

### Prerequisites

- A TWTI or TWTZ image of the tape. A capture of track 0 is enough for the
  volume table; you need the volume's segments to test the parser.
- The repository checked out and `packages/qiclib` synced
  (`uv sync --extra dev`).

### Steps

1. **See how the shipped profiles read the tape.**

    ```sh
    just qicsilver identify captures/NAME.twtz --raw
    ```

    `--raw` prints the raw records and every profile's checks, with the
    fingerprint items marked. Note which bytes in 57-127 look like a label,
    sizes and a compression byte.

2. **Hand-tune the read before you write any TOML.** Override the winning (or
   a forced) profile one layer at a time:

    ```sh
    just qicsilver identify captures/NAME.twtz --volume-profile qic80-rev-n \
        --vtbl-field source_label=102,16 --extent-offset-bytes 4 \
        --fileset-parser qic113-basic
    ```

    The report names the profile `NAME+overrides` and carries the `manual`
    warning. The same flags work on `extract`, `inspect`, `tar` and `cinnabar`,
    so you can list the volume and look at its lanes with the layout you are
    testing. See [Manual read overrides](../qicsilver/overrides.md).

3. **Write the profile.** Create
   `packages/qiclib/qiclib/volume/profiles/NAME.toml` with the layout that
   worked. Give it a `[match]` fingerprint from bytes the writer wrote, a
   comment citing every offset, `status = "bench"` and the tape in `tapes`.

4. **Check that it is chosen.**

    ```sh
    just qicsilver identify captures/NAME.twtz
    just qicsilver inspect captures/NAME.twtz --volume 0
    ```

    The report should name your profile with no `manual` warning. Expect
    `single-tape` until a second tape confirms it.

5. **Run the qiclib tests.**

    ```sh
    just check qiclib qiclib
    ```

    The tests below must still pass. If your profile changes which profile a
    bench tape gets, a test says so.

6. **Add a test.** In `packages/qiclib/tests/test_volume_profile.py`, add a
   test that `guess` picks your profile for a record that carries your
   fingerprint. Build the record with `qiclib.testing.builders.build_vtbl_entry`
   or, for a bench tape, add its header and `VTBL` bytes to a
   `qiclib.testing.bench_*` module. Header and volume-table bytes only: never
   directory or file contents.

### Tests that check every profile

| Test (`tests/test_volume_profile.py`) | Checks |
|---|---|
| `test_builtin_profiles_load` | Every shipped profile loads, has a description, a valid status and a registered parser. |
| `test_untested_profiles_earn_their_place` | Every `untested` profile has a positive fingerprint or a listable parser, and does not also raise `generic`. |
| `test_reader_fingerprints_stay_quiet_on_every_known_record` | No `untested` profile with `vtbl_bytes` fires on any bench or synthetic record. |
| `test_guess_picks_*` | The bench tapes still get `cms-qic113` (jc, unk-co) and `mtn` (3M). |
| `test_profiles_agree_with_the_builtin_parser` | `qic80-rev-n` and `qic113-rev-g` read records exactly as `qiclib.volume.table` does. |

## Add a cartridge profile

A cartridge profile names a cartridge from the header's track count and
segments per track. `qiclib.cartridge.guess` compares the header against
every profile, using the standards' §5.4.1 minimum-length equations to turn a
segment count into a tape length.

```toml
# QIC-80-MC Rev N (docs/qic-standards/qic80n.pdf) cover.

name = "qic80-425ft"
description = "QIC-80, 425 ft"
standard = "QIC-80"
tracks = 28
width_in = 0.250
length_ft = 425
capacity_mb = 172
coercivity = "550 Oe"
```

| Key | Required | Type | Description |
|---|---|---|---|
| `name` | Yes | string | The profile's name; match the file name. |
| `standard` | Yes | string | `QIC-40`, `QIC-80`, `QIC-3010` or `QIC-3020`. |
| `tracks` | Yes | integer | Tracks per cartridge. |
| `width_in` | Yes | number | Tape width in inches (`0.250` or `0.315`). |
| `length_ft` | Yes | number | Tape length in feet. |
| `capacity_mb` | No | integer | The cover capacity, decimal MB. |
| `coercivity` | No | string | For example `"550 Oe"`, `"900 Oe"`. |
| `segments_per_track` | No | integer | A fixed count, for formats where the standard gives one. |
| `model` | No | string | The model the standard's cover uses, for example `DC2120`. |
| `vendor` | No | string | The maker, for a profile named after one product. |
| `label` | No | string | The product name on the cartridge. |
| `label_capacity` | No | string | The capacity printed on the label, which may be binary. |
| `description` | No | string | One line. |

Any other key, or a value of the wrong type, fails at load.

To add one:

1. Create `packages/qiclib/qiclib/profiles/cartridge/NAME.toml`. Start it with
   a comment citing the standard and section, or the bench tape and its
   header geometry.
2. Run `just qicsilver identify` on the tape and check the cartridge line.
3. Add a case to `packages/qiclib/tests/test_cartridge.py` and run
   `just check qiclib qiclib`.

!!! note
    QIC-3010 and QIC-3020 share track counts. Geometry alone names a cartridge
    only when one standard puts the segment count near a catalogued length.
    `guess` takes a standard hint from the drive's Report Tape Status or from
    format code 6; with no hint and two fits it lists both.

## See also

- [Cartridge and volume profiles](../qicsilver/profiles.md)
- [Manual read overrides](../qicsilver/overrides.md)
- [Volume table](../formats/volume-table.md)
- [qiclib](qiclib.md)
