# cinnabar

Look at a QIC tape the way the drive saw it. cinnabar opens a raw flux
capture (`tw dump` output, TWRF) or a tape image (`tw convert` output, TWTI
or TWTZ) in a window and shows one track at a time, segment by segment, as a
stack of lanes:

```
Flux transitions   every transition, the PLL's bitcells, the decoded bits   (captures only)
QIC-80 Records     one span per MFM sector (or per QIC-100-MC record on a GCR tape)
QIC-80 Fields      the sector's sync marks, ID field, data field and CRCs
Fields             what the bytes mean: FPR and bad-sector map, volume-table entries,
                   directory and data entries, compression extents and frames
Bytes              the segment's bytes as a heat strip
```

and, under them, the whole track as a strip with the viewed window on it.
Scrub the strip, step tracks with the arrows, hover anything for its decoded
value.

```sh
# from the repository root
uv run --project packages/cinnabar cinnabar captures/NAME.twtz
uv run --project packages/cinnabar cinnabar captures/NAME/            # a directory of TWRF captures
uv run --project packages/cinnabar cinnabar captures/NAME.twtz --dump --track 2 --segment 5
```

A flux capture is on a **time** axis (seconds from the start of the pass); an
image, which has lost time, is on a **byte** axis (29,696 bytes per segment
slot). Both are cut into segments, and the segment is what you scrub by.

`--dump` prints a track's lanes as text and never imports Qt; it is what the
tests exercise and what to reach for over SSH.

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
