"""The RawFluxCapture linear-track-stack container (DESIGN.md §7.1, §6A.6)."""

from tapewyrm.rawflux.container import (
    RawFluxCapture,
    frame_marker,
    iter_markers,
)

__all__ = ["RawFluxCapture", "frame_marker", "iter_markers"]
