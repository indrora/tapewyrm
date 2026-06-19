"""GW flux bytes -> FluxStream intervals + split markers (DESIGN.md §6A.5, §13.6).

``load`` splits the two channels carried in a ``RawFluxCapture``'s flux blob:

  * the **marker** opcode-escape channel (fully real — reuses
    :func:`tapewyrm.rawflux.container.iter_markers`), and
  * the **flux data** bytes (fully real split via
    :func:`tapewyrm.rawflux.container.flux_data_only`), which are then decoded
    into inter-transition intervals.

The interval-decode body is the genuine bench unknown (DESIGN.md §13.6 item 1):
the exact GW flux opcode byte values and the long-flux continuation scheme are
read from greaseweazle-firmware on the bench. It is marked ``TODO(bench)`` and
implemented here as a clean, documented best-effort decoder; the marker-splitting
half above is real and fully tested.
"""

from __future__ import annotations

from tapewyrm.rawflux import container as rawflux
from tapewyrm.types import FluxStream, Marker


def load(cap: object) -> tuple[FluxStream, list[Marker]]:
    """Split a RawFluxCapture into a :class:`FluxStream` and its markers.

    ``cap`` is a ``RawFluxCapture`` (typed as ``object`` to avoid importing the
    container dataclass into the type signature; we only touch ``.flux`` and
    ``.header.sample_clock_hz``).
    """
    flux_blob: bytes = cap.flux  # type: ignore[attr-defined]
    sample_clock_hz: int = cap.header.sample_clock_hz  # type: ignore[attr-defined]

    markers = list(rawflux.iter_markers(flux_blob))
    data = rawflux.flux_data_only(flux_blob)
    intervals = decode_flux_intervals(data)
    return FluxStream(intervals=intervals, sample_clock_hz=sample_clock_hz), markers


def decode_flux_intervals(data: bytes) -> list[int]:
    """Decode GW flux **data** bytes into inter-transition intervals.

    TODO(bench), DESIGN.md §13.6 item 1: the exact GW flux opcode byte values and
    the long-flux continuation/escape scheme are a genuine bench unknown — read
    them from greaseweazle-firmware before this runs against a real capture. GW's
    on-wire encoding is roughly: small intervals as a single direct byte, and a
    multi-byte continuation escape for long intervals (tape dropouts and
    inter-segment gaps produce long intervals that must not saturate or be lost).

    Offline/fixture convention (real, lossless): we treat every flux data byte
    as one interval value, **verbatim and order-preserving**, so a fixture whose
    flux data *is* a decoded MFM byte stream round-trips end-to-end without a PLL
    (``codec.mfm.intervals_to_bytes`` recognizes byte-valued intervals and passes
    them straight through). This keeps the whole stack testable hardware-free.

    Real-flux decoder shape (the bench TODO above): GW's on-wire encoding packs
    small intervals as single direct bytes and long intervals (tape dropouts /
    inter-segment gaps) behind a multi-byte continuation escape. Once the actual
    opcode/escape values are read from greaseweazle-firmware, this function emits
    true tick-count intervals and ``codec.mfm`` routes them through the PLL.
    """
    # Verbatim, lossless pass-through (offline fixture path). Replace the body
    # with the real GW opcode/continuation decode once it is read on the bench.
    return list(data)
