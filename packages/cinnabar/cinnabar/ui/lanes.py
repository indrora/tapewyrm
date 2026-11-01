"""The stack of lanes for one track, x-linked, with the sketch's row labels on the left.

Rows, top to bottom, each a label and a :class:`pyqtgraph.ViewBox`:

    (header)            segment titles and boundary positions
    Flux Transitions    only when a segment carries flux (a TWRF track)
    <format> Records    depth 0 of every segment's ``records``
    <format> Fields     depth 1 of ``records``
    Fields              depth 0 of ``fields``
    Fields              depth 1 ... as deep as the track's structure goes
    Bytes               the byte strip

All view boxes share the x range (``setXLink``) and are clamped to the
track, pan with the mouse and zoom with the wheel (pyqtgraph's defaults,
with y locked). Hovering reports the span under the cursor through
``hovered`` so the window can show its detail rows.
"""

from __future__ import annotations

from collections.abc import Callable

import pyqtgraph as pg
from PySide6.QtCore import QPointF, Signal
from PySide6.QtGui import QColor

from cinnabar.model import Span, Track, depth, level
from cinnabar.ui import palette
from cinnabar.ui.items import BoundaryItem, BytesItem, FluxItem, SpanLaneItem, _DeviceItem

LABEL_WIDTH = 170
HEIGHTS = {"header": 40, "flux": 120, "records": 96, "record_fields": 50, "fields": 50, "bytes": 50}


class LaneStack(pg.GraphicsLayoutWidget):
    hovered = Signal(object, str, float)  # Span | None, lane name, x in axis units
    clicked = Signal(object, str, float)  # the same, on a left click
    range_changed = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setBackground(palette.window_bg())
        self.ci.layout.setVerticalSpacing(10)
        self.ci.layout.setHorizontalSpacing(8)
        self.ci.layout.setColumnFixedWidth(0, LABEL_WIDTH)
        self.ci.layout.setColumnStretchFactor(1, 1)
        self.track: Track | None = None
        self._lanes: list[tuple[str, pg.ViewBox, _DeviceItem]] = []
        self._header: pg.ViewBox | None = None
        self.scene().sigMouseMoved.connect(self._mouse_moved)
        self.scene().sigMouseClicked.connect(self._mouse_clicked)

    # -- building ---------------------------------------------------------

    def set_track(self, track: Track) -> None:
        self.clear()
        self._lanes = []
        self.track = track
        x0, x1 = track.start, track.end
        fmt = track.axis.format

        self._header = self._row("", HEIGHTS["header"], transparent=True)
        self._header.addItem(BoundaryItem(track.segments, x0, x1, labels=fmt))
        anchor = self._header

        if track.flux is not None:
            vb = self._row("Flux\nTransitions", HEIGHTS["flux"], anchor=anchor)
            item = FluxItem(track.flux, x0, x1)
            self._lane(vb, item, "Flux Transitions", track)

        records = [s for seg in track.segments for s in seg.records]
        vb = self._row(f"{track.physical}\nRecords", HEIGHTS["records"], anchor=anchor)
        item = SpanLaneItem(
            records,
            x0,
            x1,
            label_px=palette.FONT_PX["record"],
            sublabel_px=palette.FONT_PX["record-sub"],
            bold=True,
        )
        self._lane(vb, item, f"{track.physical} Records", track)
        if depth(records) > 1:
            vb = self._row(f"{track.physical}\nFields", HEIGHTS["record_fields"], anchor=anchor)
            item = SpanLaneItem(
                list(level(records, 1)),
                x0,
                x1,
                label_px=palette.FONT_PX["field"],
                sublabel_px=palette.FONT_PX["field-sub"],
            )
            self._lane(vb, item, f"{track.physical} Fields", track)

        fields = [s for seg in track.segments for s in seg.fields]
        for lvl in range(depth(fields)):
            vb = self._row("Fields", HEIGHTS["fields"], anchor=anchor)
            item = SpanLaneItem(
                list(level(fields, lvl)),
                x0,
                x1,
                label_px=palette.FONT_PX["field"],
                sublabel_px=palette.FONT_PX["field-sub"],
            )
            self._lane(vb, item, f"Fields {lvl}", track)

        vb = self._row("Bytes", HEIGHTS["bytes"], anchor=anchor)
        self._lane(vb, BytesItem(track.segments, x0, x1), "Bytes", track)

        for vb_ in [self._header] + [vb for _, vb, _ in self._lanes]:
            vb_.setLimits(xMin=x0, xMax=x1)
        self._header.sigXRangeChanged.connect(self._x_changed)

    def _row(self, label: str, height: int, *, anchor=None, transparent=False) -> pg.ViewBox:
        row = self.ci.layout.rowCount()
        text = pg.LabelItem(
            label.replace("\n", "<br>"),
            justify="right",
            size=f"{palette.FONT_PX['lane-title']}px",
            bold=True,
            color=palette.text().name(),
        )
        text.item.setFont(pg.QtGui.QFont(palette.MONO))
        text.setMinimumHeight(height)
        text.setMaximumHeight(height)
        self.ci.addItem(text, row=row, col=0)
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
        records = [s for seg in track.segments for s in seg.records]
        fields = [s for seg in track.segments for s in seg.fields]
        want_levels = depth(fields)
        have_levels = sum(1 for name, _, _ in self._lanes if name.startswith("Fields "))
        if want_levels != have_levels or (depth(records) > 1) != any(
            name == f"{track.physical} Fields" for name, _, _ in self._lanes
        ):
            a, b = self.view_range()
            self.set_track(track)
            self.set_range(a, b)
            return
        for name, _vb, item in self._lanes:
            if name == f"{track.physical} Records" and isinstance(item, SpanLaneItem):
                item.set_spans(records)
            elif name == f"{track.physical} Fields" and isinstance(item, SpanLaneItem):
                item.set_spans(list(level(records, 1)))
            elif name.startswith("Fields ") and isinstance(item, SpanLaneItem):
                item.set_spans(list(level(fields, int(name.split()[1]))))
            elif isinstance(item, BytesItem):
                item.set_segments(track.segments)
            else:
                item.update()

    # -- range -----------------------------------------------------------

    def set_range(self, x0: float, x1: float) -> None:
        if self._header is not None:
            self._header.setXRange(x0, x1, padding=0)

    def view_range(self) -> tuple[float, float]:
        if self._header is None:
            return 0.0, 1.0
        (a, b), _ = self._header.viewRange()
        return a, b

    def _x_changed(self, _vb, rng) -> None:
        self.range_changed.emit(rng[0], rng[1])

    # -- hover -----------------------------------------------------------

    def _mouse_clicked(self, ev) -> None:
        if ev.button() != pg.QtCore.Qt.MouseButton.LeftButton or ev.double():
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
        """(span, lane, x) under a scene position, or None when outside every lane."""
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
                        found = Span(
                            x,
                            x,
                            f"0x{value:02X}",
                            "",
                            "neutral",
                            detail=(
                                ("offset", str(off)),
                                (
                                    "value",
                                    f"0x{value:02X} = {value} = {chr(value) if 32 <= value < 127 else '.'}",
                                ),
                            ),
                        )
                return found, name, x
        return None


def describe(span: Span | None, lane: str, x: float, fmt: Callable[[float], str]) -> str:
    """HTML for the hover panel: the span's label, sublabel and detail rows."""
    if span is None:
        return ""
    rows = "".join(
        f"<tr><td style='color:#666'>{k}</td><td>&nbsp;{v}</td></tr>" for k, v in span.detail
    )
    head = f"<b>{span.label}</b>" + (f" <i>{span.sublabel}</i>" if span.sublabel else "")
    where = f"{fmt(span.start)} .. {fmt(span.end)}" if span.end > span.start else fmt(x)
    return f"<div style='font-family:{palette.MONO}'>{head}<br><span style='color:#666'>{lane}: {where}</span><table>{rows}</table></div>"
