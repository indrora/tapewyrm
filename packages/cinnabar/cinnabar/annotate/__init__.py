"""Decoders that turn bytes into labelled :class:`cinnabar.model.Span` trees.

Each module here knows one structure and nothing about axes or widgets: it
takes bytes and returns spans positioned in **byte offsets of the thing it
parsed** (a segment's data, a volume's byte stream). The loaders in
:mod:`cinnabar.load` clip and map those onto the track's axis.

- :mod:`cinnabar.annotate.header`: the header segment's format parameter
  record and bad-sector map (QIC-80-MC Rev N section 7.1, DESIGN.md 7.3).
- :mod:`cinnabar.annotate.volume`: the volume table (QIC-80-MC section 8 /
  QIC-113 Rev G section 5) and each volume's data area: Basic-DOS and
  Extended-OS directory and data entries (QIC-113 sections 7 and 8) and
  compression extents and frames (section 9).
- :mod:`cinnabar.annotate.sectors`: the physical QIC-80 records lane: MFM
  sectors with their sync marks, ID fields, data fields and CRCs
  (QIC-80-MC section 5), or the 32 slots of an image segment.
- :mod:`cinnabar.annotate.qic100`: the physical QIC-100-MC records lane:
  GCR records with preamble, markers, data field, ID field, CRCC and
  postamble (QIC-100-MC Rev F section 6), from :mod:`qiclib.qic100`.

Every span carries a **row** name, ``"<decoder>: <what>"``, the way a
protocol decoder's rows are labelled in PulseView ("I2C: Address/Data")
and Logic. ``--dump`` lists a track by those rows, in :data:`ROW_ORDER`.
The window gives each physical row its own lane but stacks the logical
rows into one lane per depth (:data:`LOGICAL_FAMILIES`): a segment holds
one logical structure at a time, so the lanes would otherwise sit empty
most of the way along a track.
"""

# Display order of every row an annotator can emit. A track shows the
# rows that occur in it, in this order; "Flux" and "Bytes" are the two
# lanes that are not spans.
ROW_ORDER: tuple[str, ...] = (
    "Flux",
    "MFM: Sectors",
    "MFM: Fields",
    "GCR: Records",
    "GCR: Fields",
    "TWTI: Sectors",
    "Volume: Source segment",
    "QIC-80: Header",
    "QIC-80: Header fields",
    "QIC-113: Volume table",
    "QIC-113: Volumes",
    "QIC-113: Volume fields",
    "QIC-113: Sections",
    "QIC-113: Entries",
    "QIC-113: Entry parts",
    "QIC-113: Extent",
    "QIC-122: Frames",
    "QIC-122: Frame fields",
    "Bytes",
)

# The logical rows, grouped by the structure they describe and ordered by
# depth within it: ``LOGICAL_FAMILIES[k][d]`` is family ``k``'s row at
# depth ``d``. A segment holds one such structure at a time (the header,
# the volume table, a volume's data area, a compression extent), so the
# window stacks one shared lane per depth and every family draws in them,
# each run of a row announced by a type strip along the lane's top
# (:class:`cinnabar.ui.lanes.LaneStack`); ``level_of`` says which lane a
# row goes to. The physical rows (sectors, GCR records, image slots, a
# volume track's source segments) are not here: each is its own lane.
# These tuples repeat the annotators' own ROWS constants rather than
# import them (the annotators import this package); test_annotate keeps
# them in step.
LOGICAL_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("QIC-80: Header", "QIC-80: Header fields"),
    ("QIC-113: Volume table", "QIC-113: Volumes", "QIC-113: Volume fields"),
    ("QIC-113: Sections", "QIC-113: Entries", "QIC-113: Entry parts"),
    ("QIC-113: Extent", "QIC-122: Frames", "QIC-122: Frame fields"),
)
LOGICAL_DEPTH = max(len(family) for family in LOGICAL_FAMILIES)
_LEVEL = {row: depth for family in LOGICAL_FAMILIES for depth, row in enumerate(family)}


def level_of(row: str) -> int | None:
    """The shared logical lane (a depth) a row is drawn in; None for a physical row."""
    return _LEVEL.get(row)


def row_title(row: str) -> str:
    """A row name without its decoder tag: ``"QIC-113: Volumes"`` -> ``"Volumes"``."""
    return row.split(":", 1)[1].strip() if ":" in row else row


def decoder_of(row: str) -> str:
    """The decoder tag of a row name: ``"QIC-113: Volumes"`` -> ``"QIC-113"``."""
    return row.split(":", 1)[0]


def ordered_rows(present: set[str]) -> list[str]:
    """``present`` rows in display order (unknown rows last, alphabetically)."""
    known = [r for r in ROW_ORDER if r in present]
    return known + sorted(present - set(ROW_ORDER) - {""})
