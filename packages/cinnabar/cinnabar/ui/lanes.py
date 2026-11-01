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
    _DeviceItem,
)

HEADER_WIDTH = 150
HEIGHTS = {"ruler": 38, "Flux": 64, "records": 36, "row": 26, "Bytes": 24}
RECORD_ROWS = {"MFM: Sectors", "GCR: Records", "TWTI: Sectors"}


class _Header(QGraphicsWidget):
    """A channel's header cell: colour stripe, name, decoder tag."""

    def __init__(self, name: str, decoder: str, height: int) -> None:
        super().__init__()
        self.name, self.decoder = name, decoder
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setMinimumWidth(HEADER_WIDTH)
        self.setMaximumWidth(HEADER_WIDTH)

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
            vb = self._row("Flux", "transitions / cells / bits", HEIGHTS["Flux"], anchor=anchor)
            self._lane(vb, FluxItem(track.flux, x0, x1), "Flux", track)

        spans = [s for seg in track.segments for s in seg.records + seg.fields]
        for row in self.rows(track):
            decoder = decoder_of(row)
            name = row.split(":", 1)[1].strip() if ":" in row else row
            is_record = row in RECORD_ROWS
            height = HEIGHTS["records"] if is_record else HEIGHTS["row"]
            vb = self._row(name, decoder, height, anchor=anchor)
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

        vb = self._row("Bytes", "", HEIGHTS["Bytes"], anchor=anchor)
        self._lane(vb, BytesItem(track.segments, x0, x1), "Bytes", track)

        for vb_ in [self._ruler] + [vb for _, vb, _ in self._lanes]:
            vb_.setLimits(xMin=x0, xMax=x1)
        self._ruler.sigXRangeChanged.connect(self._x_changed)

    def _row(
        self, name: str, decoder: str, height: int, *, anchor=None, transparent=False
    ) -> pg.ViewBox:
        row = self.ci.layout.rowCount()
        self.ci.addItem(_Header(name, decoder, height), row=row, col=0)
        vb = pg.ViewBox(enableMenu=False)
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
        have = [name for name, _, _ in self._lanes if name not in ("Flux", "Bytes")]
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
