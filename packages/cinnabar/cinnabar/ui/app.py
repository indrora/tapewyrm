"""The main window: menus, a toolbar, the channel stack, a track strip, a status bar, a details dock.

A :class:`QMainWindow` the way the platform expects one, so it looks
native on macOS, Windows and Linux without a stylesheet: the menu bar is
the system's (global on macOS), the toolbar holds :class:`QAction`s with
the standard icons and shortcuts (``QKeySequence.StandardKey`` where one
exists: Open, Zoom In, Zoom Out, Quit, ...), the details panel is a
:class:`QDockWidget` the user can float or close, and the status bar
shows what the cursor is on and how far decoding has got. Themes colour
the lanes only; the chrome stays the platform's.

    File   Open..., Export segment bytes..., Export selected bytes..., Quit
    View   Zoom in / out, Fit segment, Fit track, Details panel, Theme
    Go     Previous / next segment, first / last segment, previous / next track
    Help   About

Keys: Left / Right step a segment, PageUp / PageDown change track,
Home / End go to the track's ends, Cmd/Ctrl + and - zoom, 0 fits a
segment, Shift+0 the track. The mouse wheel zooms and dragging pans
(pyqtgraph's defaults). A track is loaded off the main thread, with a
busy cursor and the status bar saying so; a capture's segments then land
one by one (``Track.decoder``) and the lanes redraw a few times a second
while the status bar's progress bar counts them.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStyle,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from cinnabar.model import Span, Tape, Track
from cinnabar.ui import palette
from cinnabar.ui.details import DetailsPanel
from cinnabar.ui.lanes import LaneStack, one_line
from cinnabar.ui.overview import Overview
from cinnabar.ui.progress import QtProgress

log = logging.getLogger(__name__)


class _LoadSignals(QObject):
    done = Signal(object)  # Track
    failed = Signal(str)


class _DecodeSignals(QObject):
    """Hops a decoder's completion callback (pool thread) onto the main thread."""

    landed = Signal(int)


class _Load(QRunnable):
    """Loads one track off the main thread, reporting through ``progress``."""

    def __init__(self, tape: Tape, number: int, progress: QtProgress) -> None:
        super().__init__()
        self.tape, self.number, self.progress = tape, number, progress
        self.signals = _LoadSignals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.tape.load(self.number, self.progress))
        except Exception as exc:  # noqa: BLE001 -- shown in the window, logged with trace
            log.exception("track %d failed to load", self.number)
            self.signals.failed.emit(f"track {self.number}: {exc}")


class _OpenSignals(QObject):
    done = Signal(object)  # Tape
    failed = Signal(str)


class _Open(QRunnable):
    """Opens a file off the main thread (a TWTZ decompresses, a directory reads headers)."""

    def __init__(self, path: Path, progress: QtProgress) -> None:
        super().__init__()
        self.path, self.progress = path, progress
        self.signals = _OpenSignals()

    def run(self) -> None:
        from cinnabar.load import open_path

        try:
            self.signals.done.emit(open_path(self.path, self.progress))
        except Exception as exc:  # noqa: BLE001 -- a bad file is a dialog, not a crash
            log.exception("%s failed to open", self.path)
            self.signals.failed.emit(f"{self.path}\n\n{exc}")


def _icon(theme_name: str, fallback: QStyle.StandardPixmap, style: QStyle) -> QIcon:
    """A freedesktop theme icon where the platform has one, else Qt's standard pixmap."""
    icon = QIcon.fromTheme(theme_name)
    return icon if not icon.isNull() else style.standardIcon(fallback)


def _track_name(n: int) -> str:
    return f"Track {n}" if n < 1000 else f"Pass {n - 1000}"


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
        self.progress_source = QtProgress()
        self.progress_source.started.connect(self._progress_started)
        self.progress_source.advanced.connect(self._progress_advanced)
        self.progress_source.finished.connect(self._progress_finished)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(250)  # redraw at most four times a second as segments land
        self._refresh_timer.timeout.connect(self._refresh_if_dirty)
        self._refresh_timer.start()
        self.setWindowTitle(f"{Path(tape.source).name} - cinnabar")
        self.resize(1500, 900)

        # Central: the channel stack over the track strip.
        self.lanes = LaneStack()
        self.overview = Overview()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.lanes, 1)
        layout.addWidget(self.overview)
        self.setCentralWidget(central)

        # Details dock, right.
        self.details = DetailsPanel()
        self.dock = QDockWidget("Details", self)
        self.dock.setObjectName("details")
        self.dock.setWidget(self.details)
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        self._actions()
        self._menus()
        self._toolbar()
        self._statusbar()
        self._apply_theme()

        self.lanes.range_changed.connect(self.overview.set_window)
        self.overview.window_moved.connect(self.lanes.set_range)
        self.lanes.hovered.connect(self._hovered)
        self.lanes.clicked.connect(self._clicked)
        self.open_track(first)

    # -- actions, menus, toolbar, status bar ---------------------------------

    def _actions(self) -> None:
        st = self.style()
        sp = QStyle.StandardPixmap

        def action(text: str, slot, *, icon=None, shortcut=None, role=None) -> QAction:
            act = QAction(text, self) if icon is None else QAction(icon, text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
            if role is not None:
                act.setMenuRole(role)
            act.triggered.connect(slot)
            return act

        sk = QKeySequence.StandardKey
        self.act_open = action(
            "&Open...",
            self._open_dialog,
            icon=_icon("document-open", sp.SP_DialogOpenButton, st),
            shortcut=sk.Open,
        )
        self.act_export_segment = action(
            "Export &segment bytes...", lambda: self.details._export(segment=True)
        )
        self.act_export_selection = action(
            "Export selected &bytes...", lambda: self.details._export(segment=False)
        )
        self.act_quit = action(
            "&Quit", self.close, shortcut=sk.Quit, role=QAction.MenuRole.QuitRole
        )

        self.act_zoom_in = action(
            "Zoom &in",
            lambda: self._zoom(0.5),
            icon=_icon("zoom-in", sp.SP_ArrowUp, st),
            shortcut=sk.ZoomIn,
        )
        self.act_zoom_out = action(
            "Zoom &out",
            lambda: self._zoom(2.0),
            icon=_icon("zoom-out", sp.SP_ArrowDown, st),
            shortcut=sk.ZoomOut,
        )
        self.act_fit_segment = action(
            "Fit se&gment",
            lambda: self.show_segment(self.current_segment()),
            icon=_icon("zoom-original", sp.SP_FileDialogContentsView, st),
            shortcut="0",
        )
        self.act_fit_track = action(
            "Fit &track",
            self._fit_track,
            icon=_icon("zoom-fit-best", sp.SP_FileDialogListView, st),
            shortcut="Shift+0",
        )

        self.act_prev_segment = action(
            "&Previous segment",
            lambda: self.show_segment(self.current_segment() - 1),
            icon=_icon("go-previous", sp.SP_ArrowBack, st),
            shortcut=QKeySequence(Qt.Key.Key_Left),
        )
        self.act_next_segment = action(
            "&Next segment",
            lambda: self.show_segment(self.current_segment() + 1),
            icon=_icon("go-next", sp.SP_ArrowForward, st),
            shortcut=QKeySequence(Qt.Key.Key_Right),
        )
        self.act_first_segment = action(
            "&First segment", lambda: self.show_segment(0), shortcut=sk.MoveToStartOfLine
        )
        self.act_last_segment = action(
            "&Last segment",
            lambda: self.show_segment(len(self.track.segments) - 1) if self.track else None,
            shortcut=sk.MoveToEndOfLine,
        )
        self.act_prev_track = action(
            "Previous trac&k",
            lambda: self.step_track(-1),
            icon=_icon("go-up", sp.SP_ArrowUp, st),
            shortcut=sk.MoveToPreviousPage,
        )
        self.act_next_track = action(
            "Next trac&k",
            lambda: self.step_track(+1),
            icon=_icon("go-down", sp.SP_ArrowDown, st),
            shortcut=sk.MoveToNextPage,
        )
        self.act_about = action("&About cinnabar", self._about, role=QAction.MenuRole.AboutRole)

    def _menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addAction(self.act_open)
        file_menu.addSeparator()
        file_menu.addAction(self.act_export_segment)
        file_menu.addAction(self.act_export_selection)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        view = bar.addMenu("&View")
        view.addAction(self.act_zoom_in)
        view.addAction(self.act_zoom_out)
        view.addAction(self.act_fit_segment)
        view.addAction(self.act_fit_track)
        view.addSeparator()
        view.addAction(self.dock.toggleViewAction())
        view.addSeparator()
        themes = view.addMenu("&Theme")
        for name in palette.THEMES:
            act = themes.addAction(name.capitalize())
            act.setCheckable(True)
            act.setChecked(name == palette.theme().name)
            act.triggered.connect(lambda _checked=False, n=name: self.set_theme(n))
        self._theme_actions = themes.actions()

        go = bar.addMenu("&Go")
        go.addAction(self.act_prev_segment)
        go.addAction(self.act_next_segment)
        go.addAction(self.act_first_segment)
        go.addAction(self.act_last_segment)
        go.addSeparator()
        go.addAction(self.act_prev_track)
        go.addAction(self.act_next_track)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setObjectName("main")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(bar)
        bar.addAction(self.act_open)
        bar.addSeparator()
        bar.addAction(self.act_prev_segment)
        bar.addAction(self.act_next_segment)
        bar.addSeparator()
        bar.addAction(self.act_zoom_in)
        bar.addAction(self.act_zoom_out)
        bar.addAction(self.act_fit_segment)
        bar.addAction(self.act_fit_track)
        bar.addSeparator()
        bar.addWidget(QLabel(" Track "))
        self.track_box = QComboBox()
        self.track_box.setMinimumWidth(100)
        for n in self.tape.track_numbers:
            self.track_box.addItem(_track_name(n), n)
        self.track_box.currentIndexChanged.connect(self._track_chosen)
        bar.addWidget(self.track_box)
        bar.addAction(self.act_prev_track)
        bar.addAction(self.act_next_track)

    def _statusbar(self) -> None:
        sb = self.statusBar()
        self.status_hover = QLabel("")
        sb.addWidget(self.status_hover, 1)
        self.status_source = QLabel(Path(self.tape.source).name)
        sb.addPermanentWidget(self.status_source)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setTextVisible(True)
        self.progress.setFormat("decoding %v/%m")
        self.progress.hide()
        sb.addPermanentWidget(self.progress)

    # -- theme -------------------------------------------------------------

    def _apply_theme(self) -> None:
        self.lanes.setBackground(palette.window_bg())
        self.overview.setBackground(palette.window_bg())
        self.overview.vb.setBackgroundColor(palette.lane_bg())

    def set_theme(self, name: str) -> None:
        palette.set_theme(name)
        for act in getattr(self, "_theme_actions", []):
            act.setChecked(act.text().lower() == name)
        self._apply_theme()
        if self.track is not None:
            a, b = self.lanes.view_range()
            self.lanes.set_track(self.track)
            self.lanes.set_range(a, b)
            self.overview.set_track(self.track)
            self.overview.set_window(a, b)

    # -- files ---------------------------------------------------------------

    def _open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open capture or image",
            "",
            "Tape captures and images (*.twrf *.twti *.twtz);;All files (*)",
        )
        if not path or self._pending is not None:
            return
        self._pending = -1
        self.statusBar().showMessage(f"Opening {Path(path).name}...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        job = _Open(Path(path), self.progress_source)
        job.signals.done.connect(self._opened)
        job.signals.failed.connect(self._failed)
        self._pool.start(job)

    @Slot(object)
    def _opened(self, tape: Tape) -> None:
        QApplication.restoreOverrideCursor()
        self._pending = None
        self.tape = tape
        self.track = None
        self.status_source.setText(Path(tape.source).name)
        self.setWindowTitle(f"{Path(tape.source).name} - cinnabar")
        self.track_box.blockSignals(True)
        self.track_box.clear()
        for n in tape.track_numbers:
            self.track_box.addItem(_track_name(n), n)
        self.track_box.blockSignals(False)
        self.open_track(tape.track_numbers[0])

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About cinnabar",
            "<b>cinnabar</b><br>A logic analyzer for QIC tapes: flux, records, fields and "
            "bytes, track by track.<br><br>Part of Tapewyrm. Public domain (Unlicense).",
        )

    # -- tracks ----------------------------------------------------------

    def open_track(self, number: int) -> None:
        if number not in self.tape.track_numbers or self._pending is not None:
            return
        self._pending = number
        self.statusBar().showMessage(f"Loading {_track_name(number).lower()}...")
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        job = _Load(self.tape, number, self.progress_source)
        job.signals.done.connect(self._loaded)
        job.signals.failed.connect(self._failed)
        self._pool.start(job)

    @Slot(object)
    def _loaded(self, track: Track) -> None:
        QApplication.restoreOverrideCursor()
        self._pending = None
        self.track = track
        self.statusBar().clearMessage()
        idx = self.track_box.findData(track.number)
        if idx >= 0:
            self.track_box.blockSignals(True)
            self.track_box.setCurrentIndex(idx)
            self.track_box.blockSignals(False)
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
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "cinnabar", message)

    def _track_chosen(self, index: int) -> None:
        number = self.track_box.itemData(index)
        if number is not None and (self.track is None or number != self.track.number):
            self.open_track(int(number))

    def step_track(self, delta: int) -> None:
        if self.track is None:
            return
        numbers = self.tape.track_numbers
        i = numbers.index(self.track.number) + delta
        if 0 <= i < len(numbers):
            self.open_track(numbers[i])

    # -- segments and zoom ---------------------------------------------------

    def show_segment(self, index: int) -> None:
        if self.track is None or not self.track.segments:
            return
        index = min(max(index, 0), len(self.track.segments) - 1)
        seg = self.track.segments[index]
        margin = seg.width * 0.05  # a sliver of the neighbours, so the boundary shows
        self.lanes.set_range(seg.start - margin, seg.end + margin)
        if self.track.decoder is not None:
            self.track.decoder.focus(index)

    def current_segment(self) -> int:
        if self.track is None:
            return 0
        a, b = self.lanes.view_range()
        seg = self.track.segment_at((a + b) / 2)
        return seg.index if seg is not None else 0

    def _fit_track(self) -> None:
        if self.track is not None:
            self.lanes.set_range(self.track.start, self.track.end)

    def _zoom(self, factor: float) -> None:
        a, b = self.lanes.view_range()
        mid, half = (a + b) / 2, (b - a) / 2 * factor
        self.lanes.set_range(mid - half, mid + half)

    # -- click, hover ---------------------------------------------------------

    def _clicked(self, span: Span | None, lane: str, x: float) -> None:
        if self.track is not None:
            self.details.show(self.track, span, lane, x)
            if not self.dock.isVisible():
                # Showing the dock resizes the lanes; re-assert the view after the relayout.
                a, b = self.lanes.view_range()
                self.dock.show()
                QTimer.singleShot(0, lambda: self.lanes.set_range(a, b))

    def _hovered(self, span: Span | None, lane: str, x: float) -> None:
        if self.track is not None:
            self.status_hover.setText(one_line(span, lane, x, self.track.axis.format))

    # -- loading progress (from the worker, via QtProgress) -------------------

    @Slot(str, float, str)
    def _progress_started(self, description: str, total: float, unit: str) -> None:
        self.statusBar().showMessage(f"{description}...")
        self.progress.setFormat(f"{description} %p%" if total else description)
        self.progress.setRange(0, int(total) if total else 0)  # 0..0 is Qt's busy bar
        self.progress.setValue(0)
        self.progress.show()

    @Slot(float)
    def _progress_advanced(self, completed: float) -> None:
        if self.progress.maximum():
            self.progress.setValue(min(int(completed), self.progress.maximum()))

    @Slot()
    def _progress_finished(self) -> None:
        self.progress.hide()
        self.statusBar().clearMessage()

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
        total = len(self.track.segments)
        left = self.track.decoder.pending() if self.track.decoder is not None else 0
        if left:
            self.progress.setFormat("decoding %v/%m")
            self.progress.setRange(0, total)
            self.progress.setValue(total - left)
            self.progress.show()
        elif self._pending is None:
            self.progress.hide()


def run(tape: Tape, first: int, *, theme: str = "sketch") -> int:
    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("cinnabar")
    app.setOrganizationName("Tapewyrm")
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
    the whole of ``segment``; ``click`` fills the details panel with the
    record at that axis position.
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
    main.show_segment(segment)
    if main.track is not None and main.track.decoder is not None:
        main.track.decoder.wait()
    for _ in range(5):
        app.processEvents()
    main._refresh_if_dirty()
    if window is not None:
        main.lanes.set_range(*window)
    if click is not None and main.track is not None:
        seg = main.track.segment_at(click)
        hit = next((r for r in (seg.records if seg else []) if r.start <= click < r.end), None)
        main.details.show(main.track, hit, hit.row if hit is not None else "", click)
    for _ in range(5):
        app.processEvents()
    main.grab().save(str(out))
