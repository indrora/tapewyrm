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
"""
