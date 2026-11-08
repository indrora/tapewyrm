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
(pyqtgraph's defaults). A track is loaded off the main thread, with the
status bar saying so; a capture's segments then land one by one
(``Track.decoder``) and the lanes redraw a few times a second while the
status bar's progress bar counts them.

Stopping: Esc (or the toolbar's Stop) cancels the load in flight, and the
window stays on the track it shows. It also pauses that track's decoding,
and the button turns into Resume. Picking another track or pass while one
is loading cancels that load and starts the new one: the latest request
wins, and only one load runs at a time (see "background jobs" below).
Closing the window cancels and waits for whatever is running before Qt
deletes the objects the workers report to.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
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
    QScrollArea,
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
from cinnabar.ui.progress import Cancellable, Cancelled, QtProgress

log = logging.getLogger(__name__)


class _LoadSignals(QObject):
    # Every job emits exactly one of these; the window's handler for each
    # starts with _job_ended (see MainWindow's "background jobs").
    done = Signal(object)  # Track
    failed = Signal(str)
    cancelled = Signal()


class _DecodeSignals(QObject):
    """Hops a decoder's completion callback (pool thread) onto the main thread."""

    landed = Signal(int)


class _Load(QRunnable):
    """Loads one track (one of its passes; None: the default) off the main thread, reporting through ``progress``.

    ``progress`` is the job's :class:`Cancellable`: cancelling it makes the
    load raise :class:`Cancelled` at its next progress report.
    """

    def __init__(
        self, tape: Tape, number: int, pass_index: int | None, progress: Cancellable
    ) -> None:
        super().__init__()
        self.tape, self.number, self.pass_index, self.progress = tape, number, pass_index, progress
        self.signals = _LoadSignals()

    def run(self) -> None:
        # Cancelled is a BaseException, so it is caught by name, before Exception.
        try:
            track = self.tape.load(self.number, self.progress, self.pass_index)
        except Cancelled:
            log.info("loading track %d cancelled", self.number)
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 -- shown in the window, logged with trace
            log.exception("track %d failed to load", self.number)
            self.signals.failed.emit(f"track {self.number}: {exc}")
        else:
            self.signals.done.emit(track)


class _WarmUp(QRunnable):
    """Spawns the decode worker processes in the background at start-up."""

    def run(self) -> None:
        from cinnabar.decode.track import warm_up

        try:
            warm_up()
        except Exception:  # noqa: BLE001 -- the first decode will try again
            log.exception("decode pool warm-up failed")


class _OpenSignals(QObject):
    done = Signal(object)  # Tape
    failed = Signal(str)
    cancelled = Signal()


class _Open(QRunnable):
    """Opens a file off the main thread (a TWTZ decompresses, a directory reads headers)."""

    def __init__(self, path: Path, progress: Cancellable) -> None:
        super().__init__()
        self.path, self.progress = path, progress
        self.signals = _OpenSignals()

    def run(self) -> None:
        from cinnabar.load import open_path

        try:
            tape = open_path(self.path, self.progress)
        except Cancelled:
            log.info("opening %s cancelled", self.path)
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 -- a bad file is a dialog, not a crash
            log.exception("%s failed to open", self.path)
            self.signals.failed.emit(f"{self.path}\n\n{exc}")
        else:
            self.signals.done.emit(tape)


def _icon(theme_name: str, fallback: QStyle.StandardPixmap, style: QStyle) -> QIcon:
    """A freedesktop theme icon where the platform has one, else Qt's standard pixmap."""
    icon = QIcon.fromTheme(theme_name)
    return icon if not icon.isNull() else style.standardIcon(fallback)


class MainWindow(QMainWindow):
    def __init__(self, tape: Tape, first: int, pass_index: int | None = None) -> None:
        super().__init__()
        self.tape = tape
        self.track: Track | None = None
        self._pool = QThreadPool.globalInstance()
        # The background job in flight (an open or a track load), by its
        # cancellation token; None when idle. See "background jobs".
        self._job: Cancellable | None = None
        self._next: Callable[[], None] | None = None  # the request waiting for it to wind down
        self._opening = False  # the job in flight is an open: track requests wait for the new tape
        self._wanted: int | None = None  # the track last asked for, for stepping while it loads
        self._closing = False
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

        # Central: the channel stack (in a scroll area, since zoomed rows can
        # outgrow the window) over the track strip.
        self.lanes = LaneStack()
        self.overview = Overview()
        self.lane_scroll = QScrollArea()  # not "scroll": QWidget.scroll() is a method
        self.lane_scroll.setWidget(self.lanes)
        self.lane_scroll.setWidgetResizable(True)
        self.lane_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.lane_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lane_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        bar = self.lane_scroll.verticalScrollBar()
        self.lanes.scroll_by = lambda dy: bar.setValue(bar.value() + dy)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.lane_scroll, 1)
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
        self._pool.start(_WarmUp())
        self.open_track(first, pass_index)

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
        # Stop and Resume share the toolbar slot: _status shows one or the
        # other. Esc stops, as in a browser; Resume has no key, so a stray
        # Esc never starts work.
        self.act_stop = action(
            "&Stop",
            self.stop,
            icon=_icon("process-stop", sp.SP_BrowserStop, st),
            shortcut=QKeySequence(Qt.Key.Key_Escape),
        )
        self.act_stop.setToolTip("Stop loading, and pause decoding this track (Esc)")
        self.act_resume = action(
            "&Resume decoding",
            self.resume,
            icon=_icon("media-playback-start", sp.SP_MediaPlay, st),
        )
        self.act_resume.setToolTip("Resume decoding this track's segments")
        self.act_resume.setVisible(False)

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
        self.act_wide = QAction("&Wide lanes", self)
        self.act_wide.setCheckable(True)
        self.act_wide.setShortcut("W")
        self.act_wide.toggled.connect(self.lanes.set_wide)
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
        view.addAction(self.act_stop)
        view.addAction(self.act_resume)
        view.addSeparator()
        view.addAction(self.act_zoom_in)
        view.addAction(self.act_zoom_out)
        view.addAction(self.act_fit_segment)
        view.addAction(self.act_fit_track)
        view.addSeparator()
        view.addAction(self.act_wide)
        heights = view.addMenu("Lane &height")
        self._height_actions = []
        for pct in (100, 125, 150, 200, 250):
            act = heights.addAction(f"{pct}%")
            act.setCheckable(True)
            act.setChecked(pct == 100)
            act.triggered.connect(lambda _checked=False, p=pct: self.set_vscale(p / 100))
            self._height_actions.append(act)
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
        bar.addAction(self.act_stop)
        bar.addAction(self.act_resume)
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
        self.track_box.setMinimumWidth(160)
        self._fill_track_box()
        self.track_box.currentIndexChanged.connect(self._track_chosen)
        bar.addWidget(self.track_box)
        bar.addAction(self.act_prev_track)
        bar.addAction(self.act_next_track)
        # Which read of the track is showing. Beside the track arrows rather
        # than in the track list, so stepping tracks visits each track once
        # whatever number of reads a --keep-all dump kept. Greyed out, not
        # hidden, when there is one read (or an image): the toolbar does not
        # shift as tracks change.
        bar.addWidget(QLabel(" Pass "))
        self.pass_box = QComboBox()
        self.pass_box.setMinimumWidth(120)
        self.pass_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.pass_box.setToolTip("Which read of this track to show")
        self._fill_pass_box()
        self.pass_box.currentIndexChanged.connect(self._pass_chosen)
        bar.addWidget(self.pass_box)
        bar.addSeparator()
        bar.addAction(self.act_wide)
        bar.addWidget(QLabel(" Height "))
        self.height_box = QComboBox()
        for pct in (100, 125, 150, 200, 250):
            self.height_box.addItem(f"{pct}%", pct)
        self.height_box.currentIndexChanged.connect(
            lambda i: self.set_vscale(self.height_box.itemData(i) / 100)
        )
        bar.addWidget(self.height_box)

    def _fill_track_box(self) -> None:
        """The track selector lists the tape's tracks, then its volumes (decompressed streams)."""
        current = self.track_box.currentData()
        self.track_box.blockSignals(True)
        self.track_box.clear()
        for n in self.tape.track_numbers:
            self.track_box.addItem(self.tape.label(n), n)
        if current is not None and (i := self.track_box.findData(current)) >= 0:
            self.track_box.setCurrentIndex(i)
        self.track_box.blockSignals(False)

    def _fill_pass_box(self) -> None:
        """The pass selector lists the showing track's reads, each one's file name its tooltip."""
        self.pass_box.blockSignals(True)
        self.pass_box.clear()
        number = self.track.number if self.track is not None else None
        # Both guarded on number: pass_labels takes an int, and with no track
        # showing there is nothing to list (mypy can't narrow through reads).
        reads = self.tape.passes.get(number, ()) if number is not None else ()
        labels = self.tape.pass_labels(number) if number is not None else []
        for i, (read, text) in enumerate(zip(reads, labels, strict=True)):
            self.pass_box.addItem(text, i)
            self.pass_box.setItemData(i, read.file, Qt.ItemDataRole.ToolTipRole)
        if not reads:
            self.pass_box.addItem("-")  # an image's track, or a volume: nothing to choose
        elif self.track is not None and self.track.pass_index is not None:
            self.pass_box.setCurrentIndex(self.track.pass_index)
        self.pass_box.setEnabled(len(reads) > 1)
        self.pass_box.blockSignals(False)

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

    # -- lane height --------------------------------------------------------

    def set_vscale(self, scale: float) -> None:
        """The vertical zoom, 1x to 2.5x, from the toolbar box or the View menu."""
        self.lanes.set_vscale(scale)
        pct = int(round(self.lanes.vscale * 100))
        for act in getattr(self, "_height_actions", []):
            act.setChecked(act.text() == f"{pct}%")
        box = getattr(self, "height_box", None)
        if box is not None and box.currentData() != pct and (i := box.findData(pct)) >= 0:
            box.blockSignals(True)
            box.setCurrentIndex(i)
            box.blockSignals(False)

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
        if not path:
            return
        self.statusBar().showMessage(f"Opening {Path(path).name}...")
        self._request(lambda: self._start_open(Path(path)))

    def _start_open(self, path: Path) -> None:
        job = _Open(path, Cancellable(self.progress_source))
        job.signals.done.connect(self._opened)
        job.signals.failed.connect(self._failed)
        job.signals.cancelled.connect(self._cancelled)
        self._opening = True
        self._run(job)

    @Slot(object)
    def _opened(self, tape: Tape) -> None:
        if self._job_ended() or self._closing:
            tape.close()  # superseded by another open, or the window is going
            return
        old, self.tape = self.tape, tape
        old.close()  # its decoders stop and free their shared memory
        self.track = None
        self.status_source.setText(Path(tape.source).name)
        self.setWindowTitle(f"{Path(tape.source).name} - cinnabar")
        self._fill_track_box()
        self._fill_pass_box()  # empties it until the new tape's first track lands
        self.open_track(tape.track_numbers[0])

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About cinnabar",
            "<b>cinnabar</b><br>A logic analyzer for QIC tapes: flux, records, fields and "
            "bytes, track by track.<br><br>Part of Tapewyrm. Public domain (Unlicense).",
        )

    # -- tracks ----------------------------------------------------------

    def open_track(self, number: int, pass_index: int | None = None) -> None:
        """Load ``number`` (its ``pass_index``-th read; None: Tape.default_pass) on a worker.

        A load already in flight is cancelled and this one starts once it
        has wound down. While a file is opening, the request is dropped:
        it would be for a track of the tape that is going away.
        """
        if self._closing or self._opening or number not in self.tape.track_numbers:
            return
        self._wanted = number
        # No wait cursor: on macOS that is the beachball, and the window is
        # not stuck, it is loading on a worker. The status bar says so.
        what = self.tape.label(number)
        labels = self.tape.pass_labels(number)
        if len(labels) > 1:
            shown = pass_index if pass_index is not None else self.tape.default_pass(number)
            what += f", {labels[shown or 0]}"
        self.statusBar().showMessage(f"Loading {what}...")
        self._request(lambda: self._start_load(number, pass_index))

    def _start_load(self, number: int, pass_index: int | None) -> None:
        job = _Load(self.tape, number, pass_index, Cancellable(self.progress_source))
        job.signals.done.connect(self._loaded)
        job.signals.failed.connect(self._failed)
        job.signals.cancelled.connect(self._cancelled)
        self._run(job)

    @Slot(object)
    def _loaded(self, track: Track) -> None:
        if self._job_ended() or self._closing:
            # Superseded: a newer request is loading. The track stays in the
            # tape's cache, and the flux loader pauses it when the next one
            # starts, so it does not hold up the pool.
            return
        self.track = track
        self.statusBar().clearMessage()
        self._fill_track_box()
        self._sync_boxes()
        self.lanes.set_track(track)
        self.overview.set_track(track)
        self.details.set_overview(self.tape, track)
        self.details.clear()
        if track.decoder is not None:
            track.decoder.on_segment(self._decode_signals.landed.emit)
        self.show_segment(0)
        self._status()

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._job_ended() or self._closing:
            return  # superseded: the user has moved on, no dialog for the old request
        self._sync_boxes()
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "cinnabar", message)

    @Slot()
    def _cancelled(self) -> None:
        if self._job_ended() or self._closing:
            return  # cancelled to make way for a newer request, which is now loading
        self._sync_boxes()
        self.statusBar().showMessage("Stopped", 3000)

    def _sync_boxes(self) -> None:
        """Put the track and pass selectors back on the track that is showing."""
        if self.track is None:
            return
        idx = self.track_box.findData(self.track.number)
        if idx >= 0:
            self.track_box.blockSignals(True)
            self.track_box.setCurrentIndex(idx)
            self.track_box.blockSignals(False)
        self._fill_pass_box()

    # -- background jobs -------------------------------------------------
    #
    # One job (an open or a track load) runs at a time. A request made while
    # one runs cancels it (its Cancellable raises at the job's next progress
    # report) and waits for it to wind down before starting. So the latest
    # request wins: three quick PageDowns load one track, not three in
    # turn. And two loads never run at once over the tape's track cache or
    # the shared progress bar. Every job ends with exactly one of done,
    # failed or cancelled, and each handler calls _job_ended first. When
    # that started the waiting request, the handler's own result is stale
    # and dropped.

    def _request(self, start: Callable[[], None]) -> None:
        """Run ``start`` now, or, with a job in flight, cancel that and run ``start`` when it has ended."""
        if self._closing:
            return
        if self._job is None:
            start()
        else:
            self._next = start
            self._job.cancel()
        self._status()

    def _run(self, job: _Load | _Open) -> None:
        self._job = job.progress
        self._pool.start(job)
        self._status()

    def _job_ended(self) -> bool:
        """The in-flight job is over: start the request waiting on it, if any. True if one started."""
        self._job = None
        self._opening = False
        start, self._next = self._next, None
        if start is not None and not self._closing:
            start()
            return True
        self._wanted = None
        self._status()
        return False

    def stop(self) -> None:
        """Esc / Stop: cancel the job in flight, drop any request waiting on it, pause the shown track."""
        self._next = None
        if self._job is not None:
            self._job.cancel()
        if self.track is not None and self.track.decoder is not None:
            self.track.decoder.pause()
        self._status()

    def resume(self) -> None:
        """Resume decoding the shown track, from the segment in view outwards."""
        if self.track is not None and self.track.decoder is not None:
            self.track.decoder.resume(self.current_segment())
        self._status()

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's name
        """Stop everything in the background before the window and its signal objects go away.

        Closing used to return from ``app.exec()`` with a load still running
        on the pool. The interpreter then shut down around it: exit handlers
        ran while the load was building its decoder, and PySide deleted the
        window's QObjects while the load was still emitting progress to
        them ("Signal source has been deleted", and a leaked shared-memory
        block). Now the load is cancelled and waited for (it stops at its
        next progress report, a second or two at most), and every track's
        decoder is closed, before the window goes.
        """
        self._closing = True
        self._next = None
        self._refresh_timer.stop()
        if self._job is not None:
            self._job.cancel()
        self.hide()  # gone from the screen while the workers wind down
        self._pool.clear()  # a job queued and not started never starts
        if not self._pool.waitForDone(30_000):
            log.warning("a background job did not stop within 30 s; closing anyway")
        if self.track is not None and self.track.decoder is not None:
            self.track.decoder.close()  # a volume's feeding track, or one not in a cache
        self.tape.close()
        super().closeEvent(event)

    def _track_chosen(self, index: int) -> None:
        number = self.track_box.itemData(index)
        if number is not None and (self.track is None or number != self.track.number):
            self.open_track(int(number))

    def _pass_chosen(self, index: int) -> None:
        # A choice made while another load is pending is dropped by
        # open_track; _loaded then puts the box back on what is showing.
        pass_index = self.pass_box.itemData(index)
        if (
            self.track is not None
            and pass_index is not None
            and pass_index != self.track.pass_index
        ):
            self.open_track(self.track.number, int(pass_index))

    def step_track(self, delta: int) -> None:
        # From the track last asked for, so PageDown pressed three times
        # while one loads lands three tracks on, not one.
        current = (
            self._wanted
            if self._wanted is not None
            else (self.track.number if self.track is not None else None)
        )
        if current is None:
            return
        numbers = self.tape.track_numbers
        i = numbers.index(current) + delta
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
        if not self._dirty or self.track is None or self._closing:
            return
        self._dirty = False
        self.lanes.refresh()
        self.overview.set_track(self.track)
        self.details.set_overview(self.tape, self.track)  # identification adds rows
        if self.track_box.count() != len(self.tape.track_numbers):
            self._fill_track_box()  # a flux tape's volumes appear once identified
        self._status()

    def _status(self) -> None:
        """The Stop / Resume buttons, and the decode count in the progress bar.

        Stop is live while something is loading or the shown track is
        decoding; it becomes Resume once that track is paused with segments
        left. While a job runs, the progress bar is the job's (QtProgress),
        so the decode count waits until it ends.
        """
        loading = self._job is not None
        decoder = self.track.decoder if self.track is not None else None
        left = decoder.pending() if decoder is not None else 0
        # left > 0 already implies a decoder; the explicit None check is for mypy.
        paused = decoder is not None and bool(left) and decoder.paused
        self.act_resume.setVisible(paused and not loading)
        self.act_stop.setVisible(not self.act_resume.isVisible())
        self.act_stop.setEnabled(loading or (bool(left) and not paused))
        if self.track is None or loading:
            return
        total = len(self.track.segments)
        if left:
            self.progress.setFormat("paused %v/%m" if paused else "decoding %v/%m")
            self.progress.setRange(0, total)
            self.progress.setValue(total - left)
            self.progress.show()
        else:
            self.progress.hide()


def run(tape: Tape, first: int, *, pass_index: int | None = None, theme: str = "sketch") -> int:
    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("cinnabar")
    app.setOrganizationName("Tapewyrm")
    window = MainWindow(tape, first, pass_index)
    window.show()
    return app.exec()


def screenshot(
    tape: Tape,
    first: int,
    out: Path,
    *,
    pass_index: int | None = None,
    segment: int = 0,
    size=(1500, 900),
    theme: str = "sketch",
    window: tuple[float, float] | None = None,
    click: float | None = None,
) -> None:
    """Render one track's window offscreen to ``out`` (PNG); what the tests and docs use.

    ``pass_index`` picks the read of ``first`` to show (None: the default);
    ``window`` is an (x0, x1) view range in axis units to show instead of
    the whole of ``segment``; ``click`` fills the details panel with the
    record at that axis position.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    palette.set_theme(theme)
    app = QApplication.instance() or QApplication(sys.argv)
    main = MainWindow(tape, first, pass_index)
    main.resize(*size)
    main.show()
    while main.track is None or main._job is not None:
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
    # The window has done its job: take it down now, in Qt's order (children,
    # scene and items under their window, the window under the application),
    # instead of leaving it shown for interpreter exit, where the garbage
    # collector destroys the Python wrappers in whatever order it likes and
    # PySide6 segfaults about one run in four (the test suite renders nine of
    # these; 2026-11-07, "QObject: shared QObject was deleted directly", exit
    # 139 after every test had passed). deleteLater() posted outside an event
    # loop is only honoured by an explicit flush of the DeferredDelete events,
    # not by processEvents() (QCoreApplication::sendPostedEvents).
    main._pool.waitForDone()
    main.close()
    main.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
