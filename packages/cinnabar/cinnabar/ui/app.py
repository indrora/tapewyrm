"""The main window: lanes above, track strip below, and the keys that move you around.

Layout, as in the sketch:

    +------------------------------------------------------------------+
    |  (header)        Segment 3           340ms            600ms      |
    |  Flux            |||| ||| |||| ...                               |
    |  QIC-80 Records  < Preamble >< Data Record id=... >< Postamble > |
    |  ...                                                             |
    |  Bytes           ▒▒▒▒▓▓▒▒░░░░▒▒▓▓▓                               |
    +------------------------------------------------------------------+
    |  Track 9  ▲▼   [=====###===============================]         |
    +------------------------------------------------------------------+
    |  hover detail                                                    |
    +------------------------------------------------------------------+

Keys: Left / Right step a segment, Up / Down change track, Home / End go
to the track's ends, + / - zoom, 0 fits one segment. The mouse wheel zooms
and dragging pans (pyqtgraph's defaults). A track is loaded off the main
thread, with a busy cursor and the title saying so, because a flux track
takes the PLL tens of seconds (``Tape.load``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSlider,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from cinnabar.model import Span, Tape, Track
from cinnabar.ui import palette
from cinnabar.ui.details import DetailsPanel
from cinnabar.ui.lanes import LaneStack, describe
from cinnabar.ui.overview import Overview

ZOOM_STEPS = 1000  # slider resolution
ZOOM_RANGE = 5.0  # decades: the window goes from the whole track down to 1e-5 of it

log = logging.getLogger(__name__)


class _LoadSignals(QObject):
    done = Signal(object)  # Track
    failed = Signal(str)


class _DecodeSignals(QObject):
    """Hops a decoder's completion callback (pool thread) onto the main thread."""

    landed = Signal(int)


class _Load(QRunnable):
    def __init__(self, tape: Tape, number: int) -> None:
        super().__init__()
        self.tape, self.number = tape, number
        self.signals = _LoadSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.tape.load(self.number))
        except Exception as exc:  # noqa: BLE001 -- shown in the window, logged with trace
            log.exception("track %d failed to load", self.number)
            self.signals.failed.emit(f"track {self.number}: {exc}")


class MainWindow(QMainWindow):
    def __init__(self, tape: Tape, first: int) -> None:
        super().__init__()
        self.tape = tape
        self.track: Track | None = None
        self._pool = QThreadPool.globalInstance()
        self._pending: int | None = None
        self._decode_signals = _DecodeSignals()
        self._decode_signals.landed.connect(self._segment_landed)
        self._dirty = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(250)  # redraw at most four times a second as segments land
        self._refresh_timer.timeout.connect(self._refresh_if_dirty)
        self._refresh_timer.start()
        self.setWindowTitle(f"cinnabar: {Path(tape.source).name}")
        self.resize(1500, 900)

        self.lanes = LaneStack()
        self.overview = Overview()
        self.track_label = QLabel("Track -")
        font = QFont(palette.MONO)
        font.setPixelSize(palette.FONT_PX["track-title"])
        font.setBold(True)
        self.track_label.setFont(font)
        self.track_label.setFixedWidth(170)
        self.up = QToolButton()
        self.up.setArrowType(Qt.ArrowType.UpArrow)
        self.down = QToolButton()
        self.down.setArrowType(Qt.ArrowType.DownArrow)
        arrows = QVBoxLayout()
        arrows.setSpacing(0)
        arrows.addWidget(self.up)
        arrows.addWidget(self.down)
        # The zoom slider: left is the whole track, right is 1e-5 of it, log scale.
        self.zoom = QSlider(Qt.Orientation.Horizontal)
        self.zoom.setRange(0, ZOOM_STEPS)
        self.zoom.setFixedWidth(220)
        self.zoom.setToolTip("zoom (the mouse wheel over the lanes zooms too)")
        zoom_box = QVBoxLayout()
        zoom_box.addWidget(QLabel("zoom"))
        zoom_box.addWidget(self.zoom)
        bottom = QHBoxLayout()
        bottom.addWidget(self.track_label)
        bottom.addLayout(arrows)
        bottom.addWidget(self.overview, 1)
        bottom.addLayout(zoom_box)
        self.detail = QLabel("")
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setFixedHeight(72)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.detail.setContentsMargins(LANE_LABEL_PAD, 4, 4, 4)
        self.details = DetailsPanel()

        left = QWidget()
        root = QVBoxLayout(left)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.lanes, 1)
        root.addLayout(bottom)
        root.addWidget(self.detail)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([1100, 420])
        central = split
        self._central = central
        self.setCentralWidget(central)
        self._apply_theme()
        self._zoom_guard = False
        self.zoom.valueChanged.connect(self._zoom_slid)
        self.lanes.range_changed.connect(self._range_to_slider)
        self.lanes.clicked.connect(self._clicked)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction("Export segment bytes…").triggered.connect(
            lambda: self.details._export(segment=True)
        )
        file_menu.addAction("Export selected bytes…").triggered.connect(
            lambda: self.details._export(segment=False)
        )
        view = self.menuBar().addMenu("&View")
        for name in palette.THEMES:
            action = view.addAction(f"{name.capitalize()} theme")
            action.triggered.connect(lambda _checked=False, n=name: self.set_theme(n))

        self.up.clicked.connect(lambda: self.step_track(-1))
        self.down.clicked.connect(lambda: self.step_track(+1))
        self.lanes.range_changed.connect(self.overview.set_window)
        self.overview.window_moved.connect(self.lanes.set_range)
        self.lanes.hovered.connect(self._hovered)
        self.open_track(first)

    # -- theme -------------------------------------------------------------

    def _apply_theme(self) -> None:
        self._central.setStyleSheet(
            f"background: {palette.window_bg().name()}; color: {palette.text().name()};"
            f" font-family: '{palette.MONO}'"
        )
        self.lanes.setBackground(palette.window_bg())
        self.overview.setBackground(palette.window_bg())
        self.overview.vb.setBackgroundColor(palette.lane_bg())

    def set_theme(self, name: str) -> None:
        palette.set_theme(name)
        self._apply_theme()
        if self.track is not None:
            a, b = self.lanes.view_range()
            self.lanes.set_track(self.track)
            self.lanes.set_range(a, b)
            self.overview.set_track(self.track)
            self.overview.set_window(a, b)

    # -- tracks ----------------------------------------------------------

    def open_track(self, number: int) -> None:
        if number not in self.tape.track_numbers or self._pending is not None:
            return
        self._pending = number
        self.setWindowTitle(f"cinnabar: {Path(self.tape.source).name} (loading track {number}...)")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        job = _Load(self.tape, number)
        job.signals.done.connect(self._loaded)
        job.signals.failed.connect(self._failed)
        self._pool.start(job)

    @Slot(object)
    def _loaded(self, track: Track) -> None:
        QApplication.restoreOverrideCursor()
        self._pending = None
        self.track = track
        self.setWindowTitle(f"cinnabar: {Path(self.tape.source).name}")
        self.track_label.setText(track.label)
        self.lanes.set_track(track)
        self.overview.set_track(track)
        self.details.clear()
        if track.decoder is not None:
            track.decoder.on_segment(self._decode_signals.landed.emit)
        self.show_segment(0)
        self._status()

    @Slot(str)
    def _failed(self, message: str) -> None:
        QApplication.restoreOverrideCursor()
        self._pending = None
        self.setWindowTitle(f"cinnabar: {message}")
        self.detail.setText(f"<b style='color:{palette.colour('record-bad').name()}'>{message}</b>")

    def step_track(self, delta: int) -> None:
        if self.track is None:
            return
        numbers = self.tape.track_numbers
        i = numbers.index(self.track.number) + delta
        if 0 <= i < len(numbers):
            self.open_track(numbers[i])

    # -- segments ----------------------------------------------------------

    def show_segment(self, index: int) -> None:
        if self.track is None or not self.track.segments:
            return
        index = min(max(index, 0), len(self.track.segments) - 1)
        seg = self.track.segments[index]
        # A little of the neighbours on either side, like the sketch.
        margin = seg.width * 0.12
        self.lanes.set_range(seg.start - margin, seg.end + margin)
        if self.track.decoder is not None:
            self.track.decoder.focus(index)

    def current_segment(self) -> int:
        if self.track is None:
            return 0
        a, b = self.lanes.view_range()
        seg = self.track.segment_at((a + b) / 2)
        return seg.index if seg is not None else 0

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt API)
        key = event.key()
        if key == Qt.Key.Key_Right:
            self.show_segment(self.current_segment() + 1)
        elif key == Qt.Key.Key_Left:
            self.show_segment(self.current_segment() - 1)
        elif key == Qt.Key.Key_Up:
            self.step_track(-1)
        elif key == Qt.Key.Key_Down:
            self.step_track(+1)
        elif key == Qt.Key.Key_Home:
            self.show_segment(0)
        elif key == Qt.Key.Key_End and self.track is not None:
            self.show_segment(len(self.track.segments) - 1)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._zoom(0.5)
        elif key == Qt.Key.Key_Minus:
            self._zoom(2.0)
        elif key == Qt.Key.Key_0:
            self.show_segment(self.current_segment())
        else:
            super().keyPressEvent(event)

    def _zoom(self, factor: float) -> None:
        a, b = self.lanes.view_range()
        mid, half = (a + b) / 2, (b - a) / 2 * factor
        self.lanes.set_range(mid - half, mid + half)

    # -- zoom ---------------------------------------------------------------

    def _zoom_slid(self, value: int) -> None:
        if self._zoom_guard or self.track is None:
            return
        a, b = self.lanes.view_range()
        mid = (a + b) / 2
        width = (self.track.end - self.track.start) * 10 ** (-ZOOM_RANGE * value / ZOOM_STEPS)
        self._zoom_guard = True
        self.lanes.set_range(mid - width / 2, mid + width / 2)
        self._zoom_guard = False

    def _range_to_slider(self, a: float, b: float) -> None:
        if self._zoom_guard or self.track is None:
            return
        whole = self.track.end - self.track.start
        if whole <= 0 or b <= a:
            return
        import math

        value = int(round(-math.log10((b - a) / whole) / ZOOM_RANGE * ZOOM_STEPS))
        self._zoom_guard = True
        self.zoom.setValue(min(max(value, 0), ZOOM_STEPS))
        self._zoom_guard = False

    # -- click --------------------------------------------------------------

    def _clicked(self, span: Span | None, lane: str, x: float) -> None:
        if self.track is not None:
            self.details.show(self.track, span, lane, x)

    # -- decoding ----------------------------------------------------------

    @Slot(int)
    def _segment_landed(self, _index: int) -> None:
        self._dirty = True

    def _refresh_if_dirty(self) -> None:
        if not self._dirty or self.track is None:
            return
        self._dirty = False
        self.lanes.refresh()
        self.overview.set_track(self.track)
        self._status()

    def _status(self) -> None:
        if self.track is None:
            return
        left = self.track.decoder.pending() if self.track.decoder is not None else 0
        name = Path(self.tape.source).name
        self.setWindowTitle(
            f"cinnabar: {name}" + (f" (decoding, {left} segments left)" if left else "")
        )

    # -- hover ----------------------------------------------------------

    def _hovered(self, span: Span | None, lane: str, x: float) -> None:
        if self.track is None:
            return
        self.detail.setText(describe(span, lane, x, self.track.axis.format))


LANE_LABEL_PAD = 12


def run(tape: Tape, first: int, *, theme: str = "sketch") -> int:
    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(tape, first)
    window.show()
    return app.exec()


def screenshot(
    tape: Tape,
    first: int,
    out: Path,
    *,
    segment: int = 0,
    size=(1500, 900),
    theme: str = "sketch",
    window: tuple[float, float] | None = None,
    click: float | None = None,
) -> None:
    """Render one track's window offscreen to ``out`` (PNG); what the tests and docs use.

    ``window`` is an (x0, x1) view range in axis units to show instead of
    the whole of ``segment``.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(sys.argv)
    main = MainWindow(tape, first)
    main.resize(*size)
    main.show()
    while main.track is None or main._pending is not None:
        app.processEvents()
        main._pool.waitForDone(50)
    win = window
    main.show_segment(segment)
    if main.track is not None and main.track.decoder is not None:
        main.track.decoder.wait()
    for _ in range(5):
        app.processEvents()
    main._refresh_if_dirty()
    if win is not None:
        main.lanes.set_range(*win)
    if click is not None and main.track is not None:
        # "Click" the records lane at an axis position: the details panel fills.
        seg = main.track.segment_at(click)
        hit = next((r for r in (seg.records if seg else []) if r.start <= click < r.end), None)
        main.details.show(main.track, hit, f"{main.track.physical} Records", click)
    for _ in range(5):
        app.processEvents()
    main.grab().save(str(out))
