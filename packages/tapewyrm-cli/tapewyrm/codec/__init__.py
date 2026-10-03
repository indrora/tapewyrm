"""Offline physical-layer codec for Tapewyrm (DESIGN.md §6.4, §6A.5, §13.5).

Pure-Python, hardware-free: a TWRF capture's GW flux stream -> tick intervals
(``gwstream``, the archive's ``parse_body``) -> MFM bitcells (``gwpll``,
Greaseweazle's PLL, vendored) -> sync-aligned bytes -> sectors (``mfm``). That
is what ``tw convert`` and ``tw dump --check`` run; everything above the
sector (merge, placement, Reed-Solomon, volumes, QIC-113) is qiclib's.

``flux`` and ``pipeline`` are the synthetic-fixture path: decoded MFM bytes
standing in for flux, run through qiclib end to end in tests/test_pipeline.py.
"""

from __future__ import annotations
