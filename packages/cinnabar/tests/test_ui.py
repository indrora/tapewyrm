"""cinnabar.ui: the window renders offscreen, in every theme, for an image and a capture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tapewyrm_archive.progress import NULL_PROGRESS  # noqa: E402

from cinnabar.load import open_path  # noqa: E402
from cinnabar.ui import palette  # noqa: E402
from cinnabar.ui.app import screenshot  # noqa: E402


@pytest.mark.parametrize("theme", sorted(palette.THEMES))
def test_screenshot_of_image_in_every_theme(image_path: Path, tmp_path: Path, theme: str):
    out = tmp_path / f"{theme}.png"
    screenshot(open_path(image_path), 0, out, segment=0, theme=theme)
    assert out.stat().st_size > 1000


def test_screenshot_of_capture_zoomed_to_bytes(qic80_capture: Path, tmp_path: Path):
    tape = open_path(qic80_capture)
    track = tape.load(0, NULL_PROGRESS)
    seg = track.segments[2]
    out = tmp_path / "zoom.png"
    screenshot(tape, 0, out, segment=2, window=(seg.start, seg.start + 0.0005))
    assert out.stat().st_size > 1000


def test_palette_roles_cover_every_style_the_annotators_emit():
    import re

    root = Path(__file__).resolve().parents[1] / "cinnabar"
    used: set[str] = set()
    for py in list((root / "annotate").glob("*.py")) + list((root / "load").glob("*.py")):
        used |= set(re.findall(r'"([a-z]+(?:-[a-z]+)*)"', py.read_text()))
    known = set(palette._ROLES)
    # Every token the code hands to colour() must be a role; strings that are
    # not tokens (labels, keys) are filtered by requiring they look like one.
    labels = {"dir-last", "multi-cart", "seg-spanning"}  # flag names, not styles
    tokens = {
        t
        for t in used - labels
        if t in known or t.startswith(("slot-", "vtbl-", "dir-", "frame-", "record-"))
    }
    assert tokens <= known, tokens - known
