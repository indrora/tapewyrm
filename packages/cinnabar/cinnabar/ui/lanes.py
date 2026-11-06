"""The channel stack for one track: a time ruler, then one dense row per lane.

Laid out the way a logic analyzer lays out channels (PulseView, Logic): a
ruler across the top with tick labels, then contiguous rows separated by
hairlines, each with a header cell on the left carrying a coloured stripe,
the row's name and its decoder tag:

    [ruler]      +2.676 s        +2.700 s        +2.725 s   | Seg 2
    |Flux        |||||||| ||| ||||||| ...
    |Bytes       ......
    |Sectors     < id=0/0/65 >< id=0/0/66 > ...
     MFM
    |Fields      <sync><FE><ID><crc>  gap  <sync><FB><    data    ><crc>
     MFM
    |Header      [Header~~~~~~~~~~~~~~~~~~~~~~~~~~][Volume table~~~~~~~~~
     QIC-80      < Format Parameter Record  ><  Bad Sector Map  >< Volume Table >
    |Header fields  <sig><fmt>...                                  <entry><entry>
     QIC-80
    |Fields

The physical rows of :mod:`cinnabar.annotate` (sectors, records, image
slots, a volume track's source segments) each get a lane of their own, in
:data:`cinnabar.annotate.ROW_ORDER`, when they occur in the track. The
logical rows share one lane per depth
(:data:`cinnabar.annotate.LOGICAL_FAMILIES`): a segment holds one logical
structure at a time, so a lane per row sat empty most of the way along a
track. A type strip along the top of a shared lane announces each run of
a row in the decoder's colour, and the lane's header reads what is under
the view right now ("Header", "Header -> Volume table"). "Flux" and
"Bytes" are the two non-span lanes; Bytes sits right under the flux so a
capture, an image and a volume all start the same way. All view boxes
share the x range and are clamped to the track; the wheel zooms, dragging
pans (pyqtgraph's defaults, y locked). Hover and click report the span
under the cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from collections.abc import Callable as _Callable

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsWidget

from cinnabar.annotate import LOGICAL_DEPTH, decoder_of, level_of, ordered_rows, row_title
from cinnabar.model import Span, Track, in_row, rows_of
from cinnabar.ui import palette
from cinnabar.ui.items import (
    BoundaryItem,
    BytesItem,
    FluxItem,
    RulerItem,
    SpanLaneItem,
    StreamItem,
    _DeviceItem,
)

HEADER_WIDTH = 150
# A logical row's 30 px is a 10 px type strip and 20 px of spans.
HEIGHTS = {"ruler": 38, "Flux": 64, "Stream": 96, "records": 36, "row": 30, "Bytes": 24}
# "Wide lanes": the taller defaults, with room for a label and a sublabel in
# every row and three readable rows of flux (the ruler stays).
WIDE_HEIGHTS = {"ruler": 38, "Flux": 110, "Stream": 150, "records": 60, "row": 48, "Bytes": 40}
VSCALE_MIN, VSCALE_MAX = 1.0, 2.5  # the vertical zoom's range
NON_SPAN_ROWS = ("Flux", "Flux: Stream", "Bytes")
ROW_HEIGHT_MIN, ROW_HEIGHT_MAX = 14, 400
GRIP_PX = 6  # the band along a header's bottom edge that drags the row's height
RECORD_ROWS = {"MFM: Sectors", "GCR: Records", "TWTI: Sectors"}
# The shared logical lanes, one per depth: their keys (for dragged heights)
# and the names their headers show while nothing logical is under the view.
LOGICAL_KEYS = tuple(f"Logical {k}" for k in range(LOGICAL_DEPTH))
LOGICAL_NAMES = ("Structure", "Parts", "Fields")


def _logical_name(k: int) -> str:
    return LOGICAL_NAMES[k] if k < len(LOGICAL_NAMES) else f"Level {k}"


def _at_level(spans: Sequence[Span], k: int) -> Iterator[Span]:
    """Every span of a forest whose row is drawn in shared lane ``k``, in order."""
    for s in spans:
        if level_of(s.row) == k:
            yield s
        yield from _at_level(s.children, k)


class _Header(QGraphicsWidget):
    """A channel's header cell: colour stripe, name, decoder tag; drag its bottom edge to resize.

    The bottom ``GRIP_PX`` of the cell is a grip (the cursor says so): a
    drag there changes the row's height through ``on_resize``, the way a
    channel's border drags in Logic.
    """

    def __init__(
        self, name: str, decoder: str, height: int, on_resize: _Callable[[int], None] | None = None
    ) -> None:
        super().__init__()
        self.name, self.decoder = name, decoder
        self._on_resize = on_resize
        self._drag: tuple[float, float] | None = None  # (scene y at press, height at press)
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setMinimumWidth(HEADER_WIDTH)
        self.setMaximumWidth(HEADER_WIDTH)
        if on_resize is not None:
            self.setAcceptHoverEvents(True)

    def _in_grip(self, pos: QPointF) -> bool:
        return self._on_resize is not None and pos.y() >= self.rect().bottom() - GRIP_PX

    def hoverMoveEvent(self, ev) -> None:  # noqa: N802 (Qt API)
        self.setCursor(
            Qt.CursorShape.SizeVerCursor if self._in_grip(ev.pos()) else Qt.CursorShape.ArrowCursor
        )

    def hoverLeaveEvent(self, _ev) -> None:  # noqa: N802 (Qt API)
        self.unsetCursor()

    def mousePressEvent(self, ev) -> None:  # noqa: N802 (Qt API)
        if ev.button() == Qt.MouseButton.LeftButton and self._in_grip(ev.pos()):
            self._drag = (ev.scenePos().y(), self.rect().height())
            ev.accept()
        else:
            ev.ignore()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802 (Qt API)
        if self._drag is None or self._on_resize is None:
            return
        y0, h0 = self._drag
        height = int(min(max(h0 + ev.scenePos().y() - y0, ROW_HEIGHT_MIN), ROW_HEIGHT_MAX))
        self._on_resize(height)

    def mouseReleaseEvent(self, _ev) -> None:  # noqa: N802 (Qt API)
        self._drag = None

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        r = self.rect()
        if not self.name:
            return
        p.fillRect(r, palette.header_bg())
        p.fillRect(QRectF(r.left(), r.top(), 4, r.height()), palette.decoder_colour(self.decoder))
        p.setPen(QPen(palette.text()))
        name_font = QFont(palette.UI)
        name_font.setPixelSize(palette.FONT_PX["lane-title"])
        name_font.setBold(True)
        p.setFont(name_font)
        text_rect = QRectF(r.left() + 10, r.top(), r.width() - 12, r.height())
        name = p.fontMetrics().elidedText(
            self.name, Qt.TextElideMode.ElideRight, int(text_rect.width())
        )
        if self.decoder and self.decoder != self.name and r.height() >= 24:
            half = r.height() / 2
            p.drawText(
                QRectF(text_rect.left(), r.top() + 1, text_rect.width(), half),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                name,
            )
            tag_font = QFont(palette.UI)
            tag_font.setPixelSize(palette.FONT_PX["lane-sub"])
            p.setFont(tag_font)
            p.setPen(QPen(palette.decoder_colour(self.decoder).darker(115)))
            p.drawText(
                QRectF(text_rect.left(), r.top() + half, text_rect.width(), half - 1),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                self.decoder,
            )
        else:
            p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)


class _LaneBox(pg.ViewBox):
    """A lane's view box: a drag pans x through the view and y through the stack's scroll bar.

    pyqtgraph pans only the axes that are mouse-enabled, and y is locked
    on every lane (the rows are fixed height). With the lanes zoomed taller
    than the window a drag still has to move you up and down, so the
    vertical part of the drag goes to ``scroll`` in pixels, which the
    window ties to the scroll area holding the stack.
    """

    def __init__(self, scroll: _Callable[[int], None] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._scroll = scroll

    def mouseDragEvent(self, ev, axis=None) -> None:  # noqa: N802 (Qt/pyqtgraph API)
        if self._scroll is not None and ev.button() == Qt.MouseButton.LeftButton:
            dy = ev.screenPos().y() - ev.lastScreenPos().y()
            if dy:
                self._scroll(int(-dy))
        super().mouseDragEvent(ev, axis)


class LaneStack(pg.GraphicsLayoutWidget):
    hovered = Signal(object, str, float)  # Span | None, row name, x in axis units
    clicked = Signal(object, str, float)  # the same, on a left click
    range_changed = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setBackground(palette.window_bg())
        self.ci.layout.setVerticalSpacing(1)
        self.ci.layout.setHorizontalSpacing(0)
        self.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.ci.layout.setColumnFixedWidth(0, HEADER_WIDTH)
        self.ci.layout.setColumnStretchFactor(1, 1)
        self.track: Track | None = None
        self._lanes: list[tuple[str, pg.ViewBox, _DeviceItem]] = []
        self._headers: dict[str, _Header] = {}  # by lane key, for the live logical headers
        self._ruler: pg.ViewBox | None = None
        # Row heights the user dragged, by row name; kept across rebuilds and tracks.
        self.heights: dict[str, int] = {}
        self.wide = False  # View > Wide lanes: taller defaults
        self.vscale = 1.0  # View > Lane height: every row's height times this
        # Set by the window: scroll the stack vertically by so many pixels (2D drag).
        self.scroll_by: _Callable[[int], None] | None = None
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def total_height(self) -> int:
        """The height all rows need; the window sizes the stack to it so it can scroll."""
        spacing = 1
        rows = [self._ruler] + [vb for _, vb, _ in self._lanes] if self._ruler is not None else []
        return sum(int(vb.maximumHeight()) for vb in rows) + spacing * max(len(rows) - 1, 0) + 4

    def row_height(self, key: str, kind: str) -> int:
        """A row's height now: a dragged height, else the (wide) default, times the vertical zoom."""
        base = self.heights.get(key, (WIDE_HEIGHTS if self.wide else HEIGHTS)[kind])
        return int(round(base * self.vscale))

    def set_wide(self, wide: bool) -> None:
        self.wide = wide
        self.heights.clear()  # dragged heights belong to the other set of defaults
        self._rebuild_keeping_view()

    def set_vscale(self, scale: float) -> None:
        self.vscale = min(max(scale, VSCALE_MIN), VSCALE_MAX)
        self._rebuild_keeping_view()

    def _rebuild_keeping_view(self) -> None:
        if self.track is None:
            return
        a, b = self.view_range()
        self.set_track(self.track)
        self.set_range(a, b)
        self.scene().sigMouseMoved.connect(self._mouse_moved)
        self.scene().sigMouseClicked.connect(self._mouse_clicked)

    # -- building ---------------------------------------------------------

    @staticmethod
    def lane_keys(track: Track) -> list[str]:
        """The span lanes a track needs, in order: its physical rows, then a shared lane per depth in use."""
        present = rows_of([s for seg in track.segments for s in seg.records + seg.fields]) - {""}
        physical = [row for row in ordered_rows(present) if level_of(row) is None]
        levels = [lvl for row in present if (lvl := level_of(row)) is not None]
        return physical + list(LOGICAL_KEYS[: max(levels, default=-1) + 1])

    def set_track(self, track: Track) -> None:
        self.clear()
        # pyqtgraph's clear() drops the grid's column settings with the items,
        # so a rebuild without these left the lane column at zero width.
        self.ci.layout.setVerticalSpacing(1)
        self.ci.layout.setHorizontalSpacing(0)
        self.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.ci.layout.setColumnFixedWidth(0, HEADER_WIDTH)
        self.ci.layout.setColumnStretchFactor(1, 1)
        self._lanes = []
        self._headers = {}
        self.track = track
        x0, x1 = track.start, track.end

        self._ruler = self._row("", "", HEIGHTS["ruler"], transparent=True)
        self._ruler.addItem(RulerItem(track, x0, x1))
        anchor = self._ruler

        if track.flux is not None:
            vb = self._row(
                "Flux",
                "transitions / cells / bits",
                self.row_height("Flux", "Flux"),
                anchor=anchor,
                key="Flux",
            )
            self._lane(vb, FluxItem(track.flux, x0, x1), "Flux", track)
            vb = self._row(
                "Stream",
                "interval vs time",
                self.row_height("Flux: Stream", "Stream"),
                anchor=anchor,
                key="Flux: Stream",
            )
            self._lane(vb, StreamItem(track.flux, x0, x1), "Flux: Stream", track)

        vb = self._row("Bytes", "", self.row_height("Bytes", "Bytes"), anchor=anchor, key="Bytes")
        self._lane(vb, BytesItem(track.segments, x0, x1), "Bytes", track)

        spans = [s for seg in track.segments for s in seg.records + seg.fields]
        for key in self.lane_keys(track):
            if key in LOGICAL_KEYS:
                k = LOGICAL_KEYS.index(key)
                height = self.row_height(key, "row")
                vb = self._row(_logical_name(k), "", height, anchor=anchor, key=key)
                item = SpanLaneItem(
                    list(_at_level(spans, k)),
                    x0,
                    x1,
                    label_px=palette.FONT_PX["field"],
                    sublabel_px=palette.FONT_PX["field-sub"],
                    inset=0.1,
                    strip=True,
                )
                self._lane(vb, item, key, track)
                continue
            is_record = key in RECORD_ROWS
            height = self.row_height(key, "records" if is_record else "row")
            vb = self._row(row_title(key), decoder_of(key), height, anchor=anchor, key=key)
            item = SpanLaneItem(
                list(in_row(spans, key)),
                x0,
                x1,
                label_px=palette.FONT_PX["record" if is_record else "field"],
                sublabel_px=palette.FONT_PX["record-sub" if is_record else "field-sub"],
                bold=is_record,
                inset=0.12,
            )
            self._lane(vb, item, key, track)

        for vb_ in [self._ruler] + [vb for _, vb, _ in self._lanes]:
            vb_.setLimits(xMin=x0, xMax=x1, minXRange=track.axis.min_window)
        self._ruler.sigXRangeChanged.connect(self._x_changed)
        self.setMinimumHeight(self.total_height())
        self._update_headers()

    def _update_headers(self) -> None:
        """A shared lane's header says what is under the view: its rows, first to last."""
        if self._ruler is None:
            return
        a, b = self.view_range()
        for key, _vb, item in self._lanes:
            if key not in LOGICAL_KEYS or not isinstance(item, SpanLaneItem):
                continue
            header = self._headers[key]
            rows = item.rows_in_view(a, b)
            if rows:
                header.name = " \u2192 ".join(row_title(r) for r in rows)
                header.decoder = decoder_of(rows[0])
            else:
                header.name = _logical_name(LOGICAL_KEYS.index(key))
                header.decoder = ""
            header.update()

    def _row(
        self,
        name: str,
        decoder: str,
        height: int,
        *,
        anchor=None,
        transparent=False,
        key: str | None = None,
    ) -> pg.ViewBox:
        row = self.ci.layout.rowCount()
        vb = _LaneBox(lambda dy: self.scroll_by(dy) if self.scroll_by else None, enableMenu=False)
        header: _Header | None = None

        def resize(new_height: int) -> None:
            if key is not None:
                # Stored unscaled, so the vertical zoom still applies on top.
                self.heights[key] = int(round(new_height / self.vscale))
            for widget in (header, vb):
                if widget is not None:
                    widget.setMinimumHeight(new_height)
                    widget.setMaximumHeight(new_height)
            self.ci.layout.invalidate()
            self.setMinimumHeight(self.total_height())

        header = _Header(name, decoder, height, resize if key is not None else None)
        if key is not None:
            self._headers[key] = header
        self.ci.addItem(header, row=row, col=0)
        vb.setMouseEnabled(x=True, y=False)
        vb.setYRange(0, 1, padding=0)
        vb.disableAutoRange()
        vb.setBackgroundColor(QColor(0, 0, 0, 0) if transparent else palette.lane_bg())
        vb.setMinimumHeight(height)
        vb.setMaximumHeight(height)
        vb.setSizePolicy(
            pg.QtWidgets.QSizePolicy.Policy.Expanding, pg.QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.ci.addItem(vb, row=row, col=1)
        if anchor is not None:
            vb.setXLink(anchor)
        return vb

    def _lane(self, vb: pg.ViewBox, item: _DeviceItem, name: str, track: Track) -> None:
        vb.addItem(item)
        vb.addItem(BoundaryItem(track.segments, track.start, track.end))
        vb.sigRangeChanged.connect(lambda *_: item.update())
        self._lanes.append((name, vb, item))

    def refresh(self) -> None:
        """Re-read the track's segments into every lane (after segments decoded)."""
        if self.track is None:
            return
        track = self.track
        wanted = self.lane_keys(track)
        have = [name for name, _, _ in self._lanes if name not in NON_SPAN_ROWS]
        if wanted != have:
            a, b = self.view_range()
            self.set_track(track)
            self.set_range(a, b)
            return
        spans = [s for seg in track.segments for s in seg.records + seg.fields]
        for name, _vb, item in self._lanes:
            if isinstance(item, SpanLaneItem):
                if name in LOGICAL_KEYS:
                    item.set_spans(list(_at_level(spans, LOGICAL_KEYS.index(name))))
                else:
                    item.set_spans(list(in_row(spans, name)))
            elif isinstance(item, BytesItem):
                item.set_segments(track.segments)
            else:
                item.update()
        self._update_headers()

    # -- range -----------------------------------------------------------

    def set_range(self, x0: float, x1: float) -> None:
        if self._ruler is not None:
            self._ruler.setXRange(x0, x1, padding=0)

    def view_range(self) -> tuple[float, float]:
        if self._ruler is None:
            return 0.0, 1.0
        (a, b), _ = self._ruler.viewRange()
        return a, b

    def _x_changed(self, _vb, rng) -> None:
        self._update_headers()
        self.range_changed.emit(rng[0], rng[1])

    # -- hover and click ---------------------------------------------------

    def _mouse_clicked(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or ev.double():
            return
        hit = self._hit(ev.scenePos())
        if hit is not None:
            self.clicked.emit(*hit)

    def _mouse_moved(self, pos: QPointF) -> None:
        hit = self._hit(pos)
        if hit is None:
            self.hovered.emit(None, "", 0.0)
        else:
            self.hovered.emit(*hit)

    def _hit(self, pos: QPointF) -> tuple[Span | None, str, float] | None:
        """(span, row, x) under a scene position, or None when outside every lane."""
        for name, vb, item in self._lanes:
            if vb.sceneBoundingRect().contains(pos):
                x = vb.mapSceneToView(pos).x()
                found: Span | None = None
                if isinstance(item, SpanLaneItem):
                    found = item.span_at(x)
                elif isinstance(item, BytesItem):
                    hit = item.byte_at(x)
                    if hit is not None:
                        off, value = hit
                        printable = chr(value) if 32 <= value < 127 else "."
                        found = Span(
                            x,
                            x,
                            f"0x{value:02X}",
                            "",
                            "neutral",
                            detail=(
                                ("offset", str(off)),
                                ("value", f"0x{value:02X} = {value} = {printable}"),
                            ),
                            row="Bytes",
                        )
                # A shared lane reports the span's own row, not the lane.
                row = found.row if found is not None and name in LOGICAL_KEYS else name
                return found, row, x
        return None


def describe(span: Span | None, lane: str, x: float, fmt: Callable[[float], str]) -> str:
    """HTML for the details panel: the span's label, sublabel, row and detail rows."""
    if span is None:
        return ""
    rows = "".join(
        f"<tr><td style='color:#666'>{k}</td><td>&nbsp;{v}</td></tr>" for k, v in span.detail
    )
    head = f"<b>{span.label}</b>" + (f" <i>{span.sublabel}</i>" if span.sublabel else "")
    where = f"{fmt(span.start)} .. {fmt(span.end)}" if span.end > span.start else fmt(x)
    return (
        f"<div style='font-family:{palette.UI}'>{head}<br>"
        f"<span style='color:#666'>{lane}: {where}</span><table>{rows}</table></div>"
    )


def one_line(span: Span | None, lane: str, x: float, fmt: Callable[[float], str]) -> str:
    """Plain text for the status bar: ``MFM: Sectors   id=0/0/65   2.678s .. 2.696s``."""
    if span is None:
        return ""
    where = f"{fmt(span.start)} .. {fmt(span.end)}" if span.end > span.start else fmt(x)
    sub = f"  {span.sublabel}" if span.sublabel else ""
    return f"{lane}   {span.label}{sub}   {where}"
