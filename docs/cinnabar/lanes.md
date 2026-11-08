# Lanes

Each lane in the `cinnabar` window is one decoder's output along the track's
axis. This page lists every lane, what it shows, and when it appears.

## How lanes are named

Lanes are named by the decoder that produced them, the way a protocol
analyzer names its rows. Each lane's header cell carries a coloured stripe,
the lane's name and, under it, its decoder tag: `MFM`, `GCR`, `TWTI`,
`QIC-80`, `QIC-113`, `QIC-122` or `Volume`. In `--dump` output the same rows
appear as `DECODER: NAME`, for example `MFM: Sectors`.

The record lanes are named after the tape's **physical format**, not a fixed
standard. A QIC-40, QIC-80, QIC-3010 or QIC-3020 tape is MFM-recorded floppy
tape, so its records are `MFM: Sectors`. A QIC-100-MC tape is GCR-recorded, so
its records are `GCR: Records`. On a flux capture, `cinnabar` decides which
from the first two seconds of flux.

Lanes appear only when the track has something for them. They are drawn in
this order, top to bottom.

## Flux lanes

Captures only. An image has no flux left in it.

| Lane | Decoder tag | What it shows |
|---|---|---|
| Flux | transitions / cells / bits | Three rows of ticks: every flux transition, the PLL's bitcells, and the decoded bits. Zoomed out past about two transitions per pixel, the ticks become a density strip, so a gap or a dropout shows as a pale stretch. |
| Stream | interval vs time | Every transition as a dot at its time and its interval since the previous one. An MFM stream falls into bands at 2, 3 and 4 cells; a GCR stream at 1, 2 and 3. Jitter widens the bands, a speed change bends them, a dropout is a hole, and noise is dots between bands. The y axis has a hairline per microsecond. With more than 200,000 transitions in view, the dots become a density image. |

## Bytes

| Lane | What it shows |
|---|---|
| Bytes | The segment's bytes as a shade strip: `0x00` pale, `0xFF` dark, text in between. Zoom in until a byte is wide enough and its hex value is printed. On a capture, the bytes are assembled by sector slot, as an image would hold them. On a volume track, they are the decompressed data. |

## Physical record lanes

| Lane | Appears on | What it shows |
|---|---|---|
| MFM: Sectors | MFM flux captures | One span per sector, labelled with its ID field. |
| MFM: Fields | MFM flux captures | Each sector's sync marks, ID field, data field and CRCs. |
| GCR: Records | QIC-100-MC flux captures | One span per GCR record. |
| GCR: Fields | QIC-100-MC flux captures | Each record's preamble, markers, data field, ID field, CRCC and postamble. |
| TWTI: Sectors | Tape images | The 32 sector slots of each segment, with the segment table's verdict for each, rather than any tape structure. |
| Volume: Source segment | Volume tracks | Which tape segment each stretch of the decompressed stream came from. A lost tape segment is a visible gap. |

!!! note
    The QIC-100-MC decoder has only been tested on synthetic records. No real
    QIC-100-MC tape has been read with it yet.

## Logical lanes

A segment holds one logical structure at a time: the header, the volume table,
part of a volume's data area, or a compression extent. So the logical rows
share three lanes, one per depth, instead of one lane each:

| Shared lane | Header family | Volume table family | Volume data family | Compression family |
|---|---|---|---|---|
| Structure | QIC-80: Header | QIC-113: Volume table | QIC-113: Sections | QIC-113: Extent |
| Parts | QIC-80: Header fields | QIC-113: Volumes | QIC-113: Entries | QIC-122: Frames |
| Fields | | QIC-113: Volume fields | QIC-113: Entry parts | QIC-122: Frame fields |

A type strip along the top of each shared lane names every run of a row in its
decoder's colour. The lane's header reads what is under the view, for example
`Header` or `Header -> Volume table`. With nothing logical in view the headers
read `Structure`, `Parts` and `Fields`.

What each family shows:

| Family | Shows |
|---|---|
| Header | The header segment: the format parameter record and its fields, and the bad-sector map. See [Header and bad-sector map](../formats/header.md). |
| Volume table | The volume table, one span per `VTBL` record, and each record's fields. See [Volume table](../formats/volume-table.md). |
| Volume data | A volume's sections, its directory and data entries, and each entry's parts. See [QIC-113 file sets](../formats/qic-113.md). |
| Compression | QIC-122 extents, their frames and each frame's fields. See [QIC-122 compression](../formats/qic-122.md). |

On a flux capture the logical lanes fill in once the tape has been identified
from the sectors decoded so far. That needs the header segment, which is at the
start of track 0, so track 0 is where they appear first.

### How the volume lanes are laid out

The QIC-113 rows are laid out the way `qicsilver extract` reads the tape. The
volume profile that read the volume table names a file-set parser, and that
parser decides the layout:

| Parser | Layout |
|---|---|
| `qic113-basic` | A Basic-DOS volume, directory first. |
| `qic113-extended` | An Extended-OS volume, placed from its directory. |
| `none` | One span saying the format has no parser yet. |

Nothing is re-decided from the record's own bytes, such as the vendor bit or
the OS type, because real tapes get those wrong.

The **Volume fields** row draws bytes 57-127 of each `VTBL` record where the
profile places them. The bytes its fingerprint matched are drawn in red. With
[manual overrides](../qicsilver/overrides.md), the fields are drawn where you
placed them.

Every volume entry's rows carry the profile, its score, whether its
fingerprint held, its parser and its warnings. A volume track also carries the
parser's probe of the whole stream: the verdict `qicsilver extract` writes into
the TWVL header.

## Hover and click

Hover over a span or a byte and the status bar names it. Click it and the
Details panel shows its decoded fields and a hex dump of its bytes, each byte
shaded with the colour of the deepest span that covers it. See
[cinnabar](index.md#the-window).

## Colours

Each span has a role, and the theme gives each role a colour: the primary hue
for records and long data fields, a lighter primary for their contents, olive
for preambles and postambles, green for short marks and fields, red for bad,
grey for undecoded, amber for corrected or noteworthy, purple for parity and
extensions, teal for sections. See [Themes and settings](settings.md).

## See also

- [cinnabar](index.md)
- [QIC-80-MC recording format](../formats/qic-80-mc.md)
- [QIC-3010 and QIC-3020](../formats/qic-3010-3020.md)
- [Error correction](../formats/ecc.md)
