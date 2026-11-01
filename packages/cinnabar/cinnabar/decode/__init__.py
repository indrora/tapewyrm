"""Decoding a flux capture one segment at a time, in worker processes.

A whole QIC-80 track through the pure-Python PLL is half a minute; the
viewer should not wait for that before showing anything. So a track is
cut into segments as soon as its stream is parsed (that is seconds), the
lanes come up with "decoding" placeholders, and every segment is decoded
on its own by a pool of worker processes, nearest-to-the-view first,
landing in the lanes as it finishes. Processes, not threads: the PLL is
Python under the GIL, and fourteen cores are sitting there.

- :mod:`cinnabar.decode.jobs`: what a worker does: PLL, frame, scan one
  slice of intervals (QIC-80 MFM sectors or QIC-100-MC GCR records) and
  return the pieces, times already in seconds. Importable by a spawned
  process without Qt.
- :mod:`cinnabar.decode.track`: :class:`TrackDecoder`, the orchestrator:
  shares the intervals with the pool through shared memory, submits the
  segments, re-prioritises when the view moves, calls back as they land,
  and can be waited on (``cinnabar --dump``).
"""
