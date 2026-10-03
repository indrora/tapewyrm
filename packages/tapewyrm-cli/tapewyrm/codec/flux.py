"""Synthetic-fixture flux loader for ``codec.pipeline`` (DESIGN.md §6A.5).

``load`` turns a ``RawFluxCapture`` built by the pipeline tests into a
:class:`FluxStream`. It is NOT a reader of real captures: those are GW flux
streams (TWS-1 §5) and go through ``tapewyrm_archive.twrf.parse_body`` (via
``codec.gwstream``), the PLL and ``codec.mfm`` in ``tw convert``.

Fixture convention: the capture's ``flux`` IS the decoded MFM byte stream,
with no markers and no GW encoding. Each byte becomes one "interval" verbatim,
and ``codec.mfm.intervals_to_bytes`` recognizes byte-valued intervals and
passes them straight through, so the whole pipeline is testable without a PLL.

Earlier this module split markers out with the TWRF helpers' old 0xFF-stuffing
model. Those helpers now tokenize the real GW stream, which a raw MFM byte
stream (full of 0x00 and 0xFF bytes) is not, so the fixture path no longer
pretends to carry markers; the pipeline never used them.
"""

from __future__ import annotations

import logging

from tapewyrm_archive.types import Marker

from tapewyrm.types import FluxStream

log = logging.getLogger(__name__)


def load(cap: object) -> tuple[FluxStream, list[Marker]]:
    """Wrap a fixture capture's bytes as a :class:`FluxStream`; no markers.

    ``cap`` is a ``RawFluxCapture`` (typed as ``object`` to avoid importing the
    container dataclass into the type signature; we only touch ``.flux`` and
    ``.header.sample_clock_hz``).
    """
    flux_blob: bytes = cap.flux  # type: ignore[attr-defined]
    sample_clock_hz: int = cap.header.sample_clock_hz  # type: ignore[attr-defined]
    log.debug("fixture flux: %d decoded MFM bytes taken verbatim as intervals", len(flux_blob))
    return FluxStream(intervals=list(flux_blob), sample_clock_hz=sample_clock_hz), []
