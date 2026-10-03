"""The Zstandard module: stdlib ``compression.zstd``, or its official backport.

TWTZ images (``twti.py``) are a TWTI byte stream through one zstd compressor.
Python 3.14 ships zstd in the standard library as ``compression.zstd``
(PEP 784); older interpreters get the same module, API for API, from the
``backports.zstd`` package, which pyproject.toml only installs below 3.14.
Everything in this package imports zstd from here so the choice is made once.
"""

from __future__ import annotations

try:
    from compression import zstd  # type: ignore[import-not-found,unused-ignore]
except ImportError:  # Python < 3.14: the PEP 784 backport, same API
    from backports import zstd  # type: ignore[import-not-found,no-redef,unused-ignore]

__all__ = ["zstd"]
