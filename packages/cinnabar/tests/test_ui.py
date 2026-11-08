"""cinnabar.ui: the window renders offscreen, in every theme, for an image and a capture."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
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


class _Window:
    """A MainWindow on ``tape`` for a test: shown offscreen, taken down in Qt's order after.

    The teardown is :func:`cinnabar.ui.app.screenshot`'s, for the reason
    given there (PySide6 segfaults at interpreter exit otherwise).
    """

    def __init__(self, tape, first: int) -> None:
        from cinnabar.ui.app import MainWindow

        self.app = QApplication.instance() or QApplication([])
        self.main = MainWindow(tape, first)
        self.main.show()
        self.tracks = []

    def settle(self):
        """Wait out the pending load; the track it landed."""
        while self.main.track is None or self.main._job is not None:
            self.app.processEvents()
            self.main._pool.waitForDone(50)
        self.tracks.append(self.main.track)
        return self.main.track

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        from PySide6.QtCore import QEvent

        for track in self.tracks:
            if track.decoder is not None:
                track.decoder.close()
        self.main._pool.waitForDone()
        self.main.close()
        self.main.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_pass_selector_lists_a_tracks_reads_and_opens_the_one_chosen(
    qic80_capture: Path, tmp_path: Path
):
    from tests.conftest import keep_all_dump

    keep_all_dump(qic80_capture, tmp_path / "dump", passes=3, best=2)
    with _Window(open_path(tmp_path / "dump"), 0) as win:
        box = win.main.pass_box
        assert win.settle().pass_index == 1  # opens on dump.jsonl's pick
        assert box.isEnabled() and box.count() == 3 and box.currentIndex() == 1
        assert [box.itemText(i) for i in range(3)] == ["Pass 1", "Pass 2 (best)", "Pass 3"]
        assert box.itemData(2, Qt.ItemDataRole.ToolTipRole) == "track-00.3.twrf"
        assert win.main.track_box.count() == 1  # one track, however many reads
        box.setCurrentIndex(2)
        track = win.settle()
        assert track.pass_index == 2 and dict(track.detail)["pass"] == "3"
        assert box.currentIndex() == 2


def test_pass_selector_is_greyed_out_for_an_image(image_path: Path):
    with _Window(open_path(image_path), 0) as win:
        assert win.settle().pass_index is None
        box = win.main.pass_box
        assert not box.isEnabled() and box.count() == 1 and box.itemText(0) == "-"


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


def test_clicks_report_from_the_first_build_and_once_per_click(image_path: Path):
    """Hover and click are wired once, at construction: not only after a wide/height rebuild."""
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.lanes import LaneStack

    app = QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.resize(1200, 600)
    stack.show()
    stack.set_track(track)
    app.processEvents()
    sectors_vb = next(vb for name, vb, _ in stack._lanes if name == "TWTI: Sectors")
    pos = sectors_vb.sceneBoundingRect().center()

    class _Click:
        def button(self):
            return Qt.MouseButton.LeftButton

        def double(self):
            return False

        def scenePos(self):  # noqa: N802 (Qt API)
            return pos

    clicks: list[str] = []
    stack.clicked.connect(lambda span, row, x: clicks.append(row))
    stack.scene().sigMouseClicked.emit(_Click())
    assert clicks == ["TWTI: Sectors"]
    stack.set_wide(True)
    stack.set_vscale(1.5)
    app.processEvents()
    pos = (
        next(vb for name, vb, _ in stack._lanes if name == "TWTI: Sectors")
        .sceneBoundingRect()
        .center()
    )
    stack.scene().sigMouseClicked.emit(_Click())
    assert clicks == ["TWTI: Sectors", "TWTI: Sectors"]
    stack.close()
    stack.deleteLater()  # a shown stack torn down at interpreter exit upsets Qt
    app.processEvents()


def test_viewport_clamps_pixels_to_the_band_around_the_view():
    """Axis positions far outside the view map to the band's edges, never to whole-track pixels."""
    import numpy as np

    from cinnabar.ui.items import MARGIN_PX, Viewport

    v = Viewport(scale=1e7, offset=-2e9, top=0.0, bottom=10.0, vx0=200.0, vx1=200.0001)
    assert v.left == pytest.approx(0.0) and v.right == pytest.approx(1000.0)
    assert v.px(200.00005) == pytest.approx(500.0)
    assert v.px(0.0) == pytest.approx(-MARGIN_PX)  # 2e9 px to the left, clamped
    assert v.px(212.0) == pytest.approx(1000.0 + MARGIN_PX)
    assert list(v.px_array(np.array([0.0, 200.00005]))) == pytest.approx([-MARGIN_PX, 500.0])
    assert v.lo == pytest.approx(200.0 - MARGIN_PX / 1e7) and v.hi > 200.0001


def test_items_follow_the_view_and_are_bounded_by_the_band_around_it(image_path: Path):
    """At a deep zoom an item's rectangle is the view plus the margin, not the whole track."""
    from tapewyrm_archive.progress import NULL_PROGRESS

    from cinnabar.ui.items import MARGIN_PX
    from cinnabar.ui.lanes import LaneStack

    app = QApplication.instance() or QApplication([])
    track = open_path(image_path).load(0, NULL_PROGRESS)
    stack = LaneStack()
    stack.resize(1200, 600)
    stack.show()
    stack.set_track(track)
    app.processEvents()
    vb, item = next((vb, item) for name, vb, item in stack._lanes if name == "Logical 0")
    assert item.boundingRect().left() == track.start  # the whole track until the view moves
    stack.set_range(track.end - 8.0, track.end)
    app.processEvents()
    rect = item.boundingRect()
    margin = 8.0 / vb.width() * MARGIN_PX
    assert rect.right() == pytest.approx(track.end)  # never past the track
    assert rect.left() == pytest.approx(track.end - 8.0 - margin, abs=1e-6)
    assert rect.left() > track.start
    stack.close()
    stack.deleteLater()
    app.processEvents()


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


def _slow_parse(monkeypatch, times: int = 1) -> threading.Event:
    """Make the next ``times`` flux parses report progress for ten seconds first, like a huge capture.

    Returns an Event set once one is under way, so a test can act mid-load
    without racing the worker.
    """
    from cinnabar.load import flux

    real = flux.parse_body
    under_way = threading.Event()
    left = [times]

    def parse(blob, *, progress):
        if left[0] > 0:
            left[0] -= 1
            with progress.task("parsing flux stream", total=1000) as bar:
                under_way.set()
                for i in range(1000):
                    bar.update(i)
                    time.sleep(0.01)
        return real(blob, progress=progress)

    monkeypatch.setattr(flux, "parse_body", parse)
    return under_way


def test_closing_mid_load_cancels_the_load_instead_of_outliving_the_window(
    qic80_capture: Path, monkeypatch, caplog
):
    """The crash this guards against: closing during a load let it run on into interpreter exit.

    It then emitted into QObjects PySide had deleted ("Signal source has
    been deleted") and leaked its decoder's shared memory.
    """
    under_way = _slow_parse(monkeypatch)
    with _Window(open_path(qic80_capture), 0) as win:
        assert under_way.wait(10)
        t0 = time.monotonic()
        win.main.close()
        # Stopped at its next report, not after its ten seconds of "parsing".
        assert time.monotonic() - t0 < 8
        assert win.main._pool.activeThreadCount() == 0
    assert "failed to load" not in caplog.text


def test_choosing_another_pass_mid_load_cancels_it_and_opens_that_pass(
    qic80_capture: Path, tmp_path: Path, monkeypatch
):
    from tests.conftest import keep_all_dump

    keep_all_dump(qic80_capture, tmp_path / "dump", passes=3, best=2)
    under_way = _slow_parse(monkeypatch)  # the first load only
    with _Window(open_path(tmp_path / "dump"), 0) as win:
        assert under_way.wait(10)
        t0 = time.monotonic()
        win.main.open_track(0, 2)  # used to be ignored while a load was pending
        track = win.settle()
        assert time.monotonic() - t0 < 8
        assert track.pass_index == 2 and win.main.pass_box.currentIndex() == 2
    assert track.decoder.decoder.closed  # closing the window closed the tape's tracks


def test_stop_cancels_a_load_in_flight(qic80_capture: Path, monkeypatch):
    under_way = _slow_parse(monkeypatch)
    with _Window(open_path(qic80_capture), 0) as win:
        assert under_way.wait(10)
        win.main.stop()
        assert win.main._pool.waitForDone(8000)
        win.app.processEvents()
        assert win.main._job is None and win.main.track is None
        assert win.main.statusBar().currentMessage() == "Stopped"


def test_stop_turns_into_resume_while_the_shown_track_is_paused(image_path: Path):
    class Decoding:
        """Stands in for a capture's decoder, with segments left to decode."""

        paused = False
        left = 3

        def pending(self) -> int:
            return self.left

        def pause(self) -> None:
            self.paused = True

        def resume(self, focus: int = 0) -> None:
            self.paused = False

        def focus(self, index: int) -> None:
            pass

    with _Window(open_path(image_path), 0) as win:
        main, track = win.main, win.settle()
        track.decoder = stub = Decoding()
        try:
            main._status()
            assert main.act_stop.isVisible() and main.act_stop.isEnabled()
            assert not main.act_resume.isVisible()
            main.stop()
            assert stub.paused and main.act_resume.isVisible() and not main.act_stop.isVisible()
            assert main.progress.format() == "paused %v/%m"
            main.resume()
            assert not stub.paused and main.act_stop.isVisible()
            stub.left = 0
            main._status()
            assert main.act_stop.isVisible() and not main.act_stop.isEnabled()
        finally:
            track.decoder = None  # the image loader caches this Track
