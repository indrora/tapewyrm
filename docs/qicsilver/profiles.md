# Cartridge and volume profiles

`qicsilver` uses two kinds of profile to read a tape. A **cartridge profile**
names the physical cartridge. A **volume profile** says how the backup program
that wrote the tape laid out its volume table and volumes. Both are TOML files
that ship with `qiclib`. This page covers how they are chosen and how to see or
force the choice. To write a new volume profile, see
[Writing a volume profile](../dev/volume-profiles.md).

Neither is a *drive* profile. Drive profiles describe how a drive behaves on
the bus and are used by `tw`; see [Drive profiles](../tw/profiles.md).

## Cartridge profiles

A cartridge profile describes one cartridge: its standard, track count, tape
width and length, capacity, coercivity, and where known its model, vendor and
label. The built-in profiles are:

| Standard | Profiles |
|---|---|
| QIC-40 | `qic40-dc2000`, `qic40-307ft`, `qic40-1100ft` |
| QIC-80 | `qic80-dc2080`, `qic80-dc2120`, `qic80-425ft`, `qic80-1000ft`, `qic80-wide-400ft`, `qic80-wide-750ft`, `qic80-wide-1000ft` |
| QIC-3010 | `qic3010-400ft`, `qic3010-1000ft`, `qic3010-wide-400ft`, `qic3010-wide-750ft`, `qic3010-wide-1000ft` |
| QIC-3020 | `qic3020-400ft`, `qic3020-wide-400ft`, `qic3020-wide-750ft`, `qic-extra` |

For what each cartridge is, see [Cartridges](../hardware/cartridges.md).

### How a cartridge profile is chosen

The header segment records tracks per cartridge and segments per track. Those
two numbers are fixed by the standard and the tape's length, so together they
name the cartridge:

1. Only profiles with the header's track count are candidates.
2. If a candidate's standard fixes the segment count (QIC-40) and it matches
   exactly, that profile is chosen.
3. Otherwise each variable-length standard turns the segment count back into a
   tape length, using its own minimum-segments equation, and keeps the nearest
   cartridge if it lies within 10% of that length.
4. QIC-3010 and QIC-3020 use the same track counts. When both fit, a hint
   narrows the choice: the drive's tape status recorded in the captures, or
   header format code 6, which only QIC-3020 defines. With no hint, the report
   lists both.

You cannot force a cartridge profile. The choice only affects the
`cartridge` line of the report; it does not change how the volumes are read.

### See which cartridge profile matched

The `cartridge` line of [`qicsilver identify`](identify.md) shows the result.
With `--json`, the `cartridge` member holds the description, the matched
catalogue entry (`catalogue`), the estimated length (`estimated_ft`), whether
the match was exact (`exact`), and any `alternatives`.

## Volume profiles

The QIC-80-MC standard fixes only bytes 0-56 of each 128-byte volume table
(`VTBL`) record. Where the rest goes, how the volume's segments become bytes,
and what catalog format the volume holds all depend on the backup program. A
volume profile answers all three, one layer each:

| Layer | TOML table | What it sets |
|---|---|---|
| VTBL fields | `[vtbl]` | Where bytes 57-127 hold the section sizes, source label, compression byte, OS type and multi-cartridge sequence. |
| Extents | `[extent]` | The width of each QIC-122 extent's uncompressed-offset field (4 or 8 bytes) and of each frame's size field (2 or 4 bytes). |
| File set | `[fileset]` | The file-set parser that reads the volume's catalog, and its parameters. |

A profile also carries a **fingerprint** (`[match]`): bytes the backup
program wrote, such as `MTN` at VTBL byte 58. The built-in profiles are:

| Profile | Written by | Fingerprint | Parser | Status |
|---|---|---|---|---|
| `cms-qic113` | Colorado/HP CMS backup | `113` at VTBL byte 58 and `CMS` at 62 | `qic113-extended` | bench |
| `mtn` | The program that writes "MTN" (confirmed on two bench tapes, one a 3M DC2120) | `MTN` at VTBL byte 58 | `qic113-basic` | bench |
| `qic113-rev-g` | Any QIC-113 Rev G extended writer | `113` at VTBL byte 58 | `qic113-extended` | spec |
| `qic80-rev-n` | Software that follows QIC-80-MC Rev N §8 exactly | vendor-specific bit clear (no positive fingerprint) | `qic113-basic` | spec |
| `vendor-unknown` | A vendor-specific entry with no known layout; bytes 0-56 only | vendor-specific bit set (no positive fingerprint) | `qic113-basic` | spec |
| `qicstream-v2` | QICStream v2 | `FSET` at volume byte 4 | `qicstream-fset` | untested |
| `novastor` | NovaStor | `<<NoVaStOr>>` at volume byte 116 | `none` (no parser yet) | untested |

`status` says how far a profile has been exercised: `spec` (written from a
standard), `bench` (confirmed on real tapes) or `untested` (written from a spec
or another program's reader, never run on a tape here).

For the record layouts, see [Volume table](../formats/volume-table.md) and
the vendor pages for [Colorado (CMS)](../formats/vendor/cms.md) and
[MTN](../formats/vendor/mtn.md).

### How a volume profile is chosen

With `--volume-profile guess`, the default:

1. **Fingerprint gate.** Each profile's fingerprint is checked against every
   VTBL record. A profile whose fingerprint fails is eliminated for this tape.
   A fingerprint is something the software wrote, so a failure is final.
2. **Scoring.** Every surviving profile decodes every record, and the reading
   is scored with plausibility checks: does the segment range sit in the data
   area, is the date sane, is the label printable, is the compression code
   known, does the claimed size fit in the segments the volume occupies. Each
   check is worth +1 or -1. A wrong layout reads neighbouring fields as sizes
   and labels, and fails these checks badly, so the margin is usually wide.
3. **Winner.** The highest score wins. If the runner-up is within 2 points,
   the reading carries the `low-confidence` warning. If two profiles read the
   table identically, it carries `vtbl-ambiguous`. If two tie on score, the
   first is used and `identify` prints a note telling you to pick one.
4. **Parser probe.** When `extract`, `inspect` or `tar` builds a volume, the
   winner's file-set parser probes it. If the probe fails, the next profile
   that would have built the same bytes takes over.

A profile whose only fingerprint is in the volume bytes (`qicstream-v2`,
`novastor`) cannot be confirmed from the volume table. It never wins the
scoring. It is tried only when the winner's parser fails its probe.

If every profile is eliminated, the best-scoring reading is shown anyway, with
a note that the volumes may be wrong.

### Warnings

A reading's warnings appear in the `identify` report, in `identify --json`,
in the `inspect` summary, in the TWVL header, in the tar's pax global header
and in the damage report. The ones that come from the profile and its
selection:

| Warning | Meaning |
|---|---|
| `generic` | A default layout won because nothing positive matched the tape (`qic80-rev-n`, `vendor-unknown`). |
| `untested` | The profile has never been run on a real tape. |
| `single-tape` | The profile is confirmed on exactly one bench tape. |
| `low-confidence` | The runner-up scored within 2 points, or a probe passed with failures. |
| `vtbl-ambiguous` | Two profiles read the volume table identically. |
| `manual` | A layer was overridden by hand. See [Manual read overrides](overrides.md). |
| `no-parser` | The volume can be extracted to TWVL, but nothing can list its files yet. |

Other warnings describe the volume itself, for example `directory-damaged`,
`compression-unknown` or `spans-cartridges`. The full list is in
[TWS-3 TWVL](../spec/twvl.md) section 3.5.

### See which volume profile matched

Run [`qicsilver identify`](identify.md). The `volume profile` line names the
winner and its score, then every other profile's score, with `eliminated`
after the ones whose fingerprint failed. The `file set` line names the parser.
The `warnings` line follows when there are any.

To see why, add `--raw`. For each profile it prints every check with its
points, `ok` or `FAIL`, and marks fingerprint checks `[fingerprint]`:

```sh
qicsilver identify --raw captures/NAME.twtz
```

After a volume is built, `inspect` shows the profile and parser that actually
read it on its `directory` line, and the TWVL header records them.

### Force a volume profile

Pass `--volume-profile` to `identify`, `extract`, `inspect` or `tar` (and to
[cinnabar](../cinnabar/index.md)). Give the same value to every command you
run on the tape.

```sh
qicsilver identify --volume-profile mtn captures/NAME.twtz
qicsilver extract --volume-profile mtn captures/NAME.twtz captures/NAME-vols
```

The value is one of:

- A built-in name, such as `cms-qic113`. A misspelled name is an error that
  lists the built-in names.
- A path to a TOML file. A value is taken as a path when it ends in `.toml`,
  names a file that exists, is absolute, or contains a directory separator.
- `guess`, the default.

A forced profile is used even when its fingerprint does not match. `identify`
then prints a note naming the checks that failed.

When no profile reads the tape correctly, override its layers by hand first
([Manual read overrides](overrides.md)), then keep what worked as a new TOML
([Writing a volume profile](../dev/volume-profiles.md)).

## See also

- [qicsilver identify](identify.md)
- [Manual read overrides](overrides.md)
- [Writing a volume profile](../dev/volume-profiles.md)
- [Troubleshooting](../guide/troubleshooting.md)
