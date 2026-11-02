"""The channel stack for one track: a time ruler, then one dense row per decoder row.

Laid out the way a logic analyzer lays out channels (PulseView, Logic): a
ruler across the top with tick labels, then contiguous rows separated by
hairlines, each with a header cell on the left carrying a coloured stripe,
the row's name and its decoder tag:

    [ruler]      +2.676 s        +2.700 s        +2.725 s   | Seg 2
    |Flux        |||||||| ||| ||||||| ...
    |Sectors     < id=0/0/65 >< id=0/0/66 > ...
     MFM
    |Fields      <sync><FE><ID><crc>  gap  <sync><FB><    data    ><crc>
     MFM
    |Volume table  < Volume Table >
     QIC-113
    ...
    |Bytes       ......

Rows are the span rows of :mod:`cinnabar.annotate` (``Span.row``), shown
in :data:`cinnabar.annotate.ROW_ORDER`, only those that occur in the
track; "Flux" and "Bytes" are the two non-span lanes. All view boxes
share the x range and are clamped to the track; the wheel zooms, dragging
pans (pyqtgraph's defaults, y locked). Hover and click report the span
under the cursor.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Callable as _Callable

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsWidget

from cinnabar.annotate import decoder_of, ordered_rows
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
HEIGHTS = {"ruler": 38, "Flux": 64, "Stream": 96, "records": 36, "row": 26, "Bytes": 24}
NON_SPAN_ROWS = ("Flux", "Flux: Stream", "Bytes")
ROW_HEIGHT_MIN, ROW_HEIGHT_MAX = 14, 400
GRIP_PX = 6  # the band along a header's bottom edge that drags the row's height
RECORD_ROWS = {"MFM: Sectors", "GCR: Records", "TWTI: Sectors"}


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
        if self.decoder and self.decoder != self.name and r.height() >= 24:
            half = r.height() / 2
            p.drawText(
                QRectF(text_rect.left(), r.top() + 1, text_rect.width(), half),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                self.name,
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
            p.drawText(
                text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.name
            )


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
        self._ruler: pg.ViewBox | None = None
        # Row heights the user dragged, by row name; kept across rebuilds and tracks.
        self.heights: dict[str, int] = {}
        self.scene().sigMouseMoved.connect(self._mouse_moved)
        self.scene().sigMouseClicked.connect(self._mouse_clicked)

    # -- building ---------------------------------------------------------

    @staticmethod
    def rows(track: Track) -> list[str]:
        present = rows_of([s for seg in track.segments for s in seg.records + seg.fields])
        return ordered_rows(present - {""})

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
        self.track = track
        x0, x1 = track.start, track.end

        self._ruler = self._row("", "", HEIGHTS["ruler"], transparent=True)
        self._ruler.addItem(RulerItem(track, x0, x1))
        anchor = self._ruler

        if track.flux is not None:
            vb = self._row(
                "Flux",
                "transitions / cells / bits",
                self.heights.get("Flux", HEIGHTS["Flux"]),
                anchor=anchor,
                key="Flux",
            )
            self._lane(vb, FluxItem(track.flux, x0, x1), "Flux", track)
            vb = self._row(
                "Stream",
                "interval vs time",
                self.heights.get("Flux: Stream", HEIGHTS["Stream"]),
                anchor=anchor,
                key="Flux: Stream",
            )
            self._lane(vb, StreamItem(track.flux, x0, x1), "Flux: Stream", track)

        spans = [s for seg in track.segments for s in seg.records + seg.fields]
        for row in self.rows(track):
            decoder = decoder_of(row)
            name = row.split(":", 1)[1].strip() if ":" in row else row
            is_record = row in RECORD_ROWS
            height = self.heights.get(row, HEIGHTS["records"] if is_record else HEIGHTS["row"])
            vb = self._row(name, decoder, height, anchor=anchor, key=row)
            item = SpanLaneItem(
                list(in_row(spans, row)),
                x0,
                x1,
                label_px=palette.FONT_PX["record" if is_record else "field"],
                sublabel_px=palette.FONT_PX["record-sub" if is_record else "field-sub"],
                bold=is_record,
                inset=0.12,
            )
            self._lane(vb, item, row, track)

        vb = self._row(
            "Bytes", "", self.heights.get("Bytes", HEIGHTS["Bytes"]), anchor=anchor, key="Bytes"
        )
        self._lane(vb, BytesItem(track.segments, x0, x1), "Bytes", track)

        for vb_ in [self._ruler] + [vb for _, vb, _ in self._lanes]:
            vb_.setLimits(xMin=x0, xMax=x1, minXRange=track.axis.min_window)
        self._ruler.sigXRangeChanged.connect(self._x_changed)

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
        vb = pg.ViewBox(enableMenu=False)
        header: _Header | None = None

        def resize(new_height: int) -> None:
            if key is not None:
                self.heights[key] = new_height
            for widget in (header, vb):
                if widget is not None:
                    widget.setMinimumHeight(new_height)
                    widget.setMaximumHeight(new_height)
            self.ci.layout.invalidate()

        header = _Header(name, decoder, height, resize if key is not None else None)
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
        wanted = self.rows(track)
        have = [name for name, _, _ in self._lanes if name not in NON_SPAN_ROWS]
        if wanted != have:
            a, b = self.view_range()
            self.set_track(track)
            self.set_range(a, b)
            return
        spans = [s for seg in track.segments for s in seg.records + seg.fields]
        for name, _vb, item in self._lanes:
            if isinstance(item, SpanLaneItem):
                item.set_spans(list(in_row(spans, name)))
            elif isinstance(item, BytesItem):
                item.set_segments(track.segments)
            else:
                item.update()

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
                return found, name, x
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
