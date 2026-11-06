"""cinnabar.ui: the window renders offscreen, in every theme, for an image and a capture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
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


def test_zoom_floor_and_boundary_fade():
    import numpy as np

    from cinnabar.ui.items import BOUNDARY_FADE_PX, boundary_colour

    edges = np.arange(0, 10) * 29_696.0
    lo, hi = BOUNDARY_FADE_PX
    assert boundary_colour(edges, hi / 29_696.0).alphaF() == 1.0  # 60 px apart: solid
    mid = boundary_colour(edges, (lo + hi) / 2 / 29_696.0)
    assert mid is not None and 0.4 < mid.alphaF() < 0.6
    assert boundary_colour(edges, lo / 29_696.0) is None  # 12 px apart: gone
    assert boundary_colour(np.array([0.0]), 1e-9) is not None  # one boundary never fades


def test_row_heights_persist_across_rebuilds(image_path: Path):
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.lanes import LaneStack

    QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.set_track(track)
    stack.heights["TWTI: Sectors"] = 90
    stack.set_track(track)
    vb = next(vb for name, vb, _ in stack._lanes if name == "TWTI: Sectors")
    assert vb.maximumHeight() == 90


def test_wide_lanes_and_vertical_zoom(image_path: Path):
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.lanes import HEIGHTS, WIDE_HEIGHTS, LaneStack

    QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.set_track(track)

    def height(name: str) -> int:
        return next(vb.maximumHeight() for n, vb, _ in stack._lanes if n == name)

    assert height("TWTI: Sectors") == HEIGHTS["records"]
    stack.set_wide(True)
    assert (
        height("TWTI: Sectors") == WIDE_HEIGHTS["records"]
        and height("Bytes") == WIDE_HEIGHTS["Bytes"]
    )
    stack.set_vscale(2.5)
    assert height("Bytes") == round(WIDE_HEIGHTS["Bytes"] * 2.5)
    stack.set_vscale(9.0)  # clamped
    assert stack.vscale == 2.5
    stack.set_wide(False)
    stack.set_vscale(1.0)
    assert height("TWTI: Sectors") == HEIGHTS["records"]


def test_shared_logical_lanes_announce_their_rows(image_path: Path):
    """Bytes under the ruler, physical rows, then one shared lane per depth whose header follows the view."""
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.items import SpanLaneItem
    from cinnabar.ui.lanes import LaneStack

    QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.set_track(track)
    assert [name for name, _, _ in stack._lanes] == [
        "Bytes",
        "TWTI: Sectors",
        "Logical 0",
        "Logical 1",
        "Logical 2",
    ]
    header_seg = track.segments[0]
    vtbl_seg = next(s for s in track.segments if dict(s.detail)["holds"] == "Volume Table")
    structure = next(item for name, _, item in stack._lanes if name == "Logical 0")
    assert isinstance(structure, SpanLaneItem) and structure.strip
    assert structure.rows_in_view(header_seg.start, header_seg.end) == ["QIC-80: Header"]
    assert structure.rows_in_view(header_seg.start, vtbl_seg.end) == [
        "QIC-80: Header",
        "QIC-113: Volume table",
    ]
    stack.set_range(header_seg.start, header_seg.end)
    stack._update_headers()
    assert stack._headers["Logical 0"].name == "Header"
    assert stack._headers["Logical 0"].decoder == "QIC-80"
    assert stack._headers["Logical 2"].name == "Fields"  # the header family is two deep
    stack.set_range(header_seg.start, vtbl_seg.end)
    stack._update_headers()
    assert stack._headers["Logical 0"].name == "Header \u2192 Volume table"


def test_stack_reports_its_height_and_scrolls_on_drag(image_path: Path):
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.lanes import LaneStack

    QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.set_track(track)
    one_x = stack.total_height()
    assert stack.minimumHeight() == one_x
    stack.set_vscale(2.5)
    assert stack.total_height() > 2 * one_x and stack.minimumHeight() == stack.total_height()
    scrolled: list[int] = []
    stack.scroll_by = scrolled.append
    _, vb, _ = stack._lanes[0]
    vb._scroll(-7)  # what a 7 px upward drag hands the window
    assert scrolled == [-7]
