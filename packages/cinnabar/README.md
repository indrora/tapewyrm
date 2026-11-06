# cinnabar

Look at a QIC tape the way the drive saw it. cinnabar opens a raw flux
capture (`tw dump` output, TWRF) or a tape image (`tw convert` output, TWTI
or TWTZ) in a window and shows one track at a time, segment by segment, as a
stack of lanes:

```
Flux                every transition, the PLL's bitcells, the decoded bits   (captures only)
MFM: Sectors        one span per MFM sector (GCR: Records on a QIC-100-MC tape;
                    TWTI: Sectors on an image)
MFM: Fields         the sector's sync marks, ID field, data field and CRCs
QIC-80: Header      the format parameter record and bad-sector map, and their fields
QIC-113: ...        volume table, volumes, volume fields; sections, entries, entry parts
QIC-122: ...        compression extents, frames, frame fields
Bytes               the segment's bytes as a shade strip, hex values when zoomed in
```

Rows are named by the decoder that produced them, like a protocol
analyzer's; a track shows the rows that occur in it. A ruler runs across
the top, the whole track is a strip along the bottom with the viewed window
on it, and a Details panel docks on the right: while nothing is selected
it shows what the tape's header said (name, format, geometry, volumes), and
a click on any span or byte shows its decoded fields and a colour-coded
hexdump of its bytes, with buttons to export them or the whole segment to a
file. The toolbar and menus step segments and
tracks, zoom, fit, and switch themes (`--theme sketch|paper|dark|contrast`);
the status bar shows what the cursor is on and how far decoding has got.
Keys: Left / Right segment, PageUp / PageDown track, Home / End, Cmd/Ctrl +
and - zoom, 0 fit segment, Shift+0 fit track; the wheel zooms, dragging
pans.

```sh
# from the repository root
uv run --project packages/cinnabar cinnabar captures/NAME.twtz
uv run --project packages/cinnabar cinnabar captures/NAME/            # a directory of TWRF captures
uv run --project packages/cinnabar cinnabar captures/NAME.twtz --dump --track 2 --segment 5
```

A flux capture is on a **time** axis (seconds from the start of the pass); an
image, which has lost time, is on a **byte** axis (29,696 bytes per segment
slot). Both are cut into segments, and the segment is what you scrub by.

`--dump` prints the tape's and the track's rows, then the track's lanes as
text, and never imports Qt; it is what the tests exercise and what to reach
for over SSH.

Global flags follow `tw`'s: `--progress` (bars on stderr), `-v` (debug),
`-q` / `-qq`. cinnabar never touches hardware.

## Where it sits

```
tw dump --> TWRF --> tw convert --> TWTI/TWTZ --> qicsilver extract --> TWVL --> qicsilver tar
              \                        /
               `---- cinnabar --------'
```

It reads through the same libraries the pipeline uses (`tapewyrm_archive`
for the files, `qiclib` for the formats, `tapewyrm.codec` for the flux
decoder), so what cinnabar labels is what `qicsilver` would read. The
package graph in `STYLE.md` has it as a leaf: nothing depends on cinnabar, and
`tw` never imports Qt.

## Layout

- `cinnabar/model.py`: the data the widgets draw: tracks, segments, lanes of
  nested spans. Read this first.
- `cinnabar/load/`: readers (image, flux) that build a `Tape` from a file.
- `cinnabar/annotate/`: decoders that label bytes: header, volume, sectors,
  QIC-100-MC records.
- `cinnabar/ui/`: the pyqtgraph window: lanes, the segment header, the track
  overview strip.
- `cinnabar/cli.py`: `cinnabar` itself.
