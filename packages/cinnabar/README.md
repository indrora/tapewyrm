# cinnabar

Look at a QIC tape the way the drive saw it. cinnabar opens a raw flux
capture (`tw dump` output, TWRF) or a tape image (`tw convert` output, TWTI
or TWTZ) in a window and shows one track at a time, segment by segment, as a
stack of lanes:

```
Flux                every transition, the PLL's bitcells, the decoded bits   (captures only)
Bytes               the segment's bytes as a shade strip, hex values when zoomed in
MFM: Sectors        one span per MFM sector (GCR: Records on a QIC-100-MC tape;
                    TWTI: Sectors on an image)
MFM: Fields         the sector's sync marks, ID field, data field and CRCs
Structure  }        three shared lanes for the logical rows, one per depth:
Parts      }        QIC-80: Header / Header fields; QIC-113: Volume table, Volumes,
Fields     }        Volume fields; Sections, Entries, Entry parts; QIC-122 extents,
                    frames, frame fields
```

The QIC-113 rows are laid out the way `qicsilver extract` reads the tape:
the volume profile that read the volume table (`qiclib.volume_profile`,
DESIGN.md §7.8) names a file-set parser, `qic113-basic`, `qic113-extended`
or `none`, and that parser decides whether a volume's lanes show a
Basic-DOS directory-first layout, an Extended-OS one placed from its
directory, or one span saying the format has no parser yet. Nothing is
re-decided from the record's bytes (the vendor bit, the OS type), since
real tapes lie about those. The Volume fields lane draws bytes 57-127 of
each VTBL record where that profile places them, with the bytes its
fingerprint matched in red, and every entry's rows carry the profile, its
score, whether its fingerprint held, its parser and its warnings; a volume's
decompressed stream (the Volume N entries of the track menu) also carries
the parser's probe of the whole stream, the verdict `extract` stamps into a
TWVL header. A hand-built stream with no profile behind it is probed with
every parser, as `qicsilver inspect` probes a bare TWVL.

The volume read takes `qicsilver`'s flags, with the same words:
`--volume-profile NAME|PATH` forces the profile (default `guess`), and the
hand-tuning flags `--fileset-parser NAME`, `--fileset-param KEY=VALUE`,
`--extent-offset-bytes 4|8`, `--extent-frame-size-bytes 2|4` and
`--vtbl-field NAME=OFFSET,LENGTH` override that profile's layers, so a tape
no profile fits yet can be laid out by hand and looked at before its TOML
is written (DESIGN.md §7.8). The rows then name the profile as
`NAME+overrides`, the Volume fields lane draws the fields where they were
placed, and the warning `manual` joins the amber line.

Rows are named by the decoder that produced them, like a protocol
analyzer's. The physical rows each get a lane when they occur in the
track; the logical rows share three lanes, since a segment holds one
logical structure at a time: a type strip along each shared lane's top
names every run of a row in its decoder's colour, and the lane's header
reads what is under the view ("Header", "Header -> Volume table"). A ruler runs across
the top, the whole track is a strip along the bottom with the viewed window
on it, and a Details panel docks on the right: while nothing is selected
it shows what the tape's header said (name, format, geometry, volumes),
which volume profile read the volume table, its score and its parser, and,
on a line of their own in amber, any warnings on that reading
(`low-confidence`, `no-parser`, `untested`, `manual`...); a click on any span or
byte shows its decoded fields and a colour-coded
hexdump of its bytes, with buttons to export them or the whole segment to a
file (a segment export offers the 29 data sectors, as an image holds them,
or all 32 with the parity). The toolbar and menus step segments and
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
uv run --project packages/cinnabar cinnabar captures/NAME.twtz --fileset-parser qic113-basic --vtbl-field source_label=102,16
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
