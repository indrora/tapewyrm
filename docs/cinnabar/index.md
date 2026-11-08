# cinnabar

`cinnabar` shows a QIC tape the way the drive saw it. It opens a raw flux
capture or a tape image in a window and draws one track at a time, segment by
segment, as a stack of lanes, like a logic analyzer with protocol decoders.

## Where it sits

```text
tw dump --> TWRF --> tw convert --> TWTI/TWTZ --> qicsilver extract --> TWVL --> qicsilver tar
              \                        /
               `---- cinnabar --------'
```

`cinnabar` reads through the same libraries as the rest of the pipeline, so
what it labels is what `qicsilver` reads. Use it to see why a segment did not
decode, to check where a volume's fields sit before writing a profile, or to
compare several reads of one track. It never touches hardware, and it never
writes to the files it opens.

## What you can open

| Input | Axis | What you get |
|---|---|---|
| A TWRF capture (`tw dump` output) | Time: seconds from the start of the pass | The flux, the decoded records, and the logical structure. Opening one capture opens every capture of the same dump. |
| A directory of TWRF captures | Time | The same: every capture in the directory, one track each, with every read of a track as one of its passes. |
| A TWTI or TWTZ image (`tw convert` output) | Bytes: 29,696 bytes per segment slot | The sector slots with the segment table's verdict, the bytes, and the logical structure. |

On a time axis, segments are cut at the drive's INDEX pulses. On a byte axis,
each segment is one 29,696-byte slot. Either way the segment is the unit you
step through.

A tape's track list holds its tracks and then its volumes. **Volume N** is that
backup volume's decompressed byte stream, laid out the way `qicsilver extract`
builds it, with each piece titled by the tape segment it came from. On a flux
capture the volumes appear once track 0 has decoded far enough to identify the
tape.

Captures whose header names no track (track -1, a probe that did not seek)
appear together as **Unknown track**, each capture one of its passes.

A TWVL file cannot be opened.

## Synopsis

```text
cinnabar [OPTIONS] PATH
```

From the repository root:

```sh
uv run --project packages/cinnabar cinnabar captures/NAME.twtz
```

## Options

| Option | Default | Description |
|---|---|---|
| `--track N` | first track | Track to open first. |
| `--pass P\|best` | `best` | Which read of the track to open: a `pass_id`, or `best` for the read `dump.jsonl` names. Flux captures only. See [Passes](passes.md). |
| `--volume N` | none | Open volume `N`'s decompressed stream instead of a track. `N` is its index in the volume table. |
| `--volume-profile NAME\|PATH` | `guess` | The volume profile to read the volume table with, as for `qicsilver`. See [profiles](../qicsilver/profiles.md). |
| `--fileset-parser NAME` | the profile's | See [Manual read overrides](../qicsilver/overrides.md). |
| `--fileset-param KEY=VALUE` | none | Repeatable. See [Manual read overrides](../qicsilver/overrides.md). |
| `--extent-offset-bytes 4\|8` | the profile's | See [Manual read overrides](../qicsilver/overrides.md). |
| `--extent-frame-size-bytes 2\|4` | the profile's | See [Manual read overrides](../qicsilver/overrides.md). |
| `--vtbl-field NAME=OFFSET,LENGTH` | the profile's | Repeatable. See [Manual read overrides](../qicsilver/overrides.md). |
| `--theme sketch\|paper\|dark\|contrast` | `sketch` | Colour scheme. See [Themes and settings](settings.md). |
| `--dump` | off | Print the tape's rows and the track's lanes as text and exit. No window opens. |
| `--segment N` | all | With `--dump`: print only this segment. With `--screenshot`: the segment to show (default 0). |
| `--screenshot FILE` | none | Render the window offscreen to a PNG file and exit. |
| `--window START:END` | the whole segment | With `--screenshot`: the view range, in seconds or bytes. |
| `--click X` | none | With `--screenshot`: open the Details panel on the record at axis position `X`. |
| `--progress` | off | Show progress bars on stderr. |
| `-v`, `--verbose` | off | More log output. `-v` turns on debug logging. |
| `-q`, `--quiet` | off | Less log output. `-q` warnings, `-qq` errors. |
| `--version` | | Print the version and exit. |

The volume-read options are the same flags, with the same words, as
`qicsilver`'s, so the viewer labels a tape exactly as `qicsilver extract` with
the same flags would read it.

## The window

From top to bottom:

- **Toolbar**: Open, Stop or Resume, previous and next segment, zoom, fit,
  the **Track** and **Pass** selectors, Wide lanes, and lane height.
- **Ruler**: the axis, in seconds or bytes, with the segment's title.
- **Lanes**: one row per decoder output. See [Lanes](lanes.md).
- **Track strip**: the whole track as a shade strip over a bar of segment
  states (clean, corrected, uncorrectable, missing, bad), with the viewed
  window drawn on it. Drag the window to scrub, or click anywhere to jump.
- **Status bar**: what the cursor is on, and how far decoding has got.

The **Details** panel docks on the right. While nothing is selected it shows
what the tape's header says (name, format, geometry, volumes), which volume
profile read the volume table, its score and parser, and any warnings on that
reading in amber (`low-confidence`, `no-parser`, `untested`, `manual` and
others). Click a span or a byte and it shows that item's decoded fields and a
colour-coded hex dump of its bytes. You can float or close the panel; View >
Details brings it back.

### Export bytes

The Details panel and the File menu export bytes to a file:

- **Export selected bytes** writes the bytes of the clicked span. On a volume
  track, clicking a file's data entry and exporting writes that file.
- **Export segment bytes** writes the whole segment. On a segment that has
  them, you can choose the 29 data sectors, as an image holds them, or all 32
  sectors including the error-correction parity. Sectors that were never read
  come out as zeros, so offsets in the file match offsets in the segment.

!!! warning
    Exported bytes are recovered data. Keep them out of the repository. See
    [Keep captures private](../guide/privacy.md).

## Examples

Open a tape image:

```sh
cinnabar captures/NAME.twtz
```

Open a dump directory of flux captures:

```sh
cinnabar captures/NAME/
```

Open the third read of track 5:

```sh
cinnabar captures/NAME/ --track 5 --pass 3
```

Open volume 0's decompressed stream:

```sh
cinnabar captures/NAME.twtz --volume 0
```

Print segment 5 of track 2 as text, for use over SSH or in a script:

```sh
cinnabar captures/NAME.twtz --dump --track 2 --segment 5
```

Render a screenshot of segment 0 of track 0 in the dark theme:

```sh
cinnabar captures/NAME.twtz --screenshot track0.png --theme dark
```

## The text dump

`--dump` prints the tape's rows (header, volume profile, warnings), then the
track: one line per segment with its range and state, a flux count per segment
for a capture, then every span in every row, then the byte count. It never
loads Qt, so it works without a display. On a flux capture it waits for the
whole track to decode before printing.

## Exit status

`0` on success. `1` when the file cannot be opened or its volume table cannot
be read. `2` for a usage error, such as a `--track` or `--pass` the tape does
not have.

## See also

- [Lanes](lanes.md)
- [Passes and merged reads](passes.md)
- [Themes and settings](settings.md)
- [cinnabar for developers](../dev/cinnabar.md)
- [TWS-1 TWRF](../spec/twrf.md) and [TWS-2 TWTI/TWTZ](../spec/twti.md)
