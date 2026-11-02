"""The custom graphics items: one per lane kind.

Every item lives in a :class:`pyqtgraph.ViewBox` whose x axis is the
track's axis (seconds or bytes) and whose y axis is fixed at 0..1. The
items draw in **device pixels**: ``paint`` reads the painter's transform
to find where the view's x maps on screen, resets it, and draws there.
That is what keeps a chevron's point the same 7 pixels wide at every zoom
and lets every item do its own level of detail: a span narrower than a
pixel is not worth a polygon, so it becomes a one-pixel tick, and a label
is drawn only when it fits.

``boundingRect`` is the item's data range in view coordinates, which
pyqtgraph uses to decide when to repaint; the range changes when the view
moves, so ``ViewBox.sigRangeChanged`` is connected to ``update``.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPolygonF

from cinnabar.model import ByteRun, Flux, Segment, Span  # noqa: F401 -- Segment for BytesItem
from cinnabar.ui import palette

POINT_PX = 5  # the pointed end of a chevron or hex, in pixels
MIN_LABEL_PX = 28  # a span narrower than this gets no label
BYTE_TEXT_PX = 22  # a byte at least this wide on screen shows its hex value
_FONT_CACHE: dict[int, QFont] = {}


def _font(px: int, bold: bool = False, family: str | None = None) -> QFont:
    key = px * 4 + int(bold) * 2 + int(family is not None)
    if key not in _FONT_CACHE:
        f = QFont(family or palette.MONO)
        f.setPixelSize(px)
        f.setBold(bold)
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]


class _DeviceItem(pg.GraphicsObject):
    """Base: draws in device pixels over the view's current x range."""

    def __init__(self, x0: float, x1: float) -> None:
        super().__init__()
        self._x0, self._x1 = x0, x1

    def boundingRect(self) -> QRectF:  # noqa: N802 (Qt API)
        return QRectF(self._x0, 0.0, max(self._x1 - self._x0, 1e-9), 1.0)

    def set_range(self, x0: float, x1: float) -> None:
        self.prepareGeometryChange()
        self._x0, self._x1 = x0, x1
        self.update()

    @staticmethod
    def _device(p: QPainter) -> tuple[float, float, float, float]:
        """(scale, offset, top, bottom): device x = scale * view x + offset."""
        tr = p.transform()
        a = tr.map(QPointF(0.0, 0.0))
        b = tr.map(QPointF(1.0, 1.0))
        scale = b.x() - a.x()
        top, bottom = min(a.y(), b.y()), max(a.y(), b.y())
        return scale, a.x(), top, bottom


def shape_polygon(x0: float, x1: float, top: float, bottom: float, shape: str) -> QPolygonF:
    """The outline of a span in device pixels; pointed ends never exceed half the width."""
    pt = min(POINT_PX, (x1 - x0) / 2)
    mid = (top + bottom) / 2
    if shape == "block" or pt < 1.5:
        return QPolygonF(
            [QPointF(x0, top), QPointF(x1, top), QPointF(x1, bottom), QPointF(x0, bottom)]
        )
    # chevron and hex: both ends pointed; a hex is just a short one
    return QPolygonF(
        [
            QPointF(x0 + pt, top),
            QPointF(x1 - pt, top),
            QPointF(x1, mid),
            QPointF(x1 - pt, bottom),
            QPointF(x0 + pt, bottom),
            QPointF(x0, mid),
        ]
    )


class SpanLaneItem(_DeviceItem):
    """One lane: a flat list of spans (one depth of the tree), with labels when they fit."""

    def __init__(
        self,
        spans: Sequence[Span],
        x0: float,
        x1: float,
        *,
        label_px: int = 13,
        sublabel_px: int = 11,
        bold: bool = False,
        inset: float = 0.08,
    ) -> None:
        super().__init__(x0, x1)
        self._label_px = label_px
        self._sublabel_px = sublabel_px
        self._bold = bold
        self._inset = inset
        self.spans: list[Span] = []
        self._starts = np.zeros(0)
        self._ends = np.zeros(0)
        self.set_spans(spans)

    def set_spans(self, spans: Sequence[Span]) -> None:
        self.spans = sorted(spans, key=lambda s: s.start)
        self._starts = np.array([s.start for s in self.spans])
        self._ends = np.array([s.end for s in self.spans])
        self.update()

    def span_at(self, x: float) -> Span | None:
        """The span under view x, if any (the latest-starting one that covers it)."""
        i = int(np.searchsorted(self._starts, x, side="right")) - 1
        for k in range(i, max(i - 8, -1), -1):
            if self.spans[k].start <= x < self.spans[k].end:
                return self.spans[k]
        return None

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None or not self.spans:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        h = bottom - top
        t = top + h * self._inset
        b = bottom - h * self._inset
        lo = int(np.searchsorted(self._ends, vx0, side="right"))
        hi = int(np.searchsorted(self._starts, vx1, side="left"))
        # Thin spans collapse into ticks; many ticks on one pixel are one tick.
        last_tick = -1e9
        for s in self.spans[lo:hi]:
            dx0 = scale * s.start + offset
            dx1 = scale * s.end + offset
            w = dx1 - dx0
            col = palette.colour(s.style)
            if w < 1.5:
                if dx0 - last_tick < 1.0:
                    continue
                last_tick = dx0
                p.setPen(QPen(col, 1))
                p.drawLine(QPointF(dx0, t), QPointF(dx0, b))
                continue
            # Outlines only once a span is wide enough to be a shape; packed
            # thin spans with outlines read as a solid dark bar.
            p.setPen(QPen(col.darker(130), 1) if w >= 6 else Qt.PenStyle.NoPen)
            p.setBrush(
                QBrush(
                    col,
                    Qt.BrushStyle.BDiagPattern
                    if s.style == "pending"
                    else Qt.BrushStyle.SolidPattern,
                )
            )
            p.drawPolygon(shape_polygon(dx0, dx1, t, b, s.shape.value))
            if w >= MIN_LABEL_PX and s.label:
                self._label(p, s, dx0, dx1, t, b)
        p.restore()

    def _label(self, p: QPainter, s: Span, x0: float, x1: float, t: float, b: float) -> None:
        inner = QRectF(x0 + POINT_PX, t, max(x1 - x0 - 2 * POINT_PX, 1), b - t)
        text_col = palette.text_on_dark() if palette.dark(s.style) else palette.text()
        p.setPen(QPen(text_col))
        two_lines = bool(s.sublabel) and (b - t) >= self._label_px + self._sublabel_px + 6
        p.setFont(_font(self._label_px, self._bold))
        width = int(inner.width())
        # Level of detail, by what fits: FULL is the label and sublabel on
        # two lines; SMALL is the label alone, or its first word; EMPTY is
        # the shape with no text ("Se..." says nothing, and the details
        # panel has the text). Narrower still and the span is a tick (paint).
        metrics = p.fontMetrics()
        if metrics.horizontalAdvance(s.label) <= width:
            label = s.label
        elif metrics.horizontalAdvance(s.short) <= width:
            label, two_lines = s.short, False
        else:
            return
        if not two_lines:
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, label)
            return
        half = inner.height() / 2
        p.drawText(
            QRectF(inner.x(), inner.y(), inner.width(), half),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
            label,
        )
        p.setFont(_font(self._sublabel_px, self._bold))
        sub = p.fontMetrics().elidedText(s.sublabel, Qt.TextElideMode.ElideRight, width)
        p.drawText(
            QRectF(inner.x(), inner.y() + half, inner.width(), half),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            sub,
        )


class FluxItem(_DeviceItem):
    """The flux lane: transitions, PLL bitcells and decoded bits as three rows of ticks.

    Zoomed out past ~2 transitions per pixel the ticks become a density
    strip (darker where transitions are denser), so a gap or a dropout
    still shows as a pale stretch.
    """

    def __init__(self, flux: Flux, x0: float, x1: float) -> None:
        super().__init__(x0, x1)
        self._flux = flux

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        h = bottom - top
        rows = (
            (top + h * 0.08, top + h * 0.36),
            (top + h * 0.42, top + h * 0.66),
            (top + h * 0.72, top + h * 0.94),
        )
        flux = self._flux
        width = max(int(scale * (vx1 - vx0)), 1)
        if flux.transition_count(vx0, vx1) <= 400 * width:
            transitions = flux.transitions_between(vx0, vx1)
            self._row(
                p, transitions, None, vx0, vx1, scale, offset, rows[0], palette.colour("flux")
            )
        else:
            # Hundreds of transitions per pixel: a flat strip says "flux here".
            px0, px1 = scale * vx0 + offset, scale * vx1 + offset
            p.fillRect(
                QRectF(px0, rows[0][0], px1 - px0, rows[0][1] - rows[0][0]), palette.colour("flux")
            )
        # Cells are computed for the window only, and only once there are
        # few enough to draw as ticks; zoomed out, the rows are a flat strip.
        if flux.cell_count_between(vx0, vx1) <= 2 * width:
            cells, bits = flux.cells_between(vx0, vx1)
            self._row(p, cells, None, vx0, vx1, scale, offset, rows[1], QColor("#7a7a7a"))
            self._row(p, cells, bits, vx0, vx1, scale, offset, rows[2], QColor("#404040"))
        else:
            px0, px1 = scale * vx0 + offset, scale * vx1 + offset
            for t, b in rows[1:]:
                p.fillRect(QRectF(px0, (t + b) / 2 - 1, px1 - px0, 2), QColor("#9a9a9a"))
        p.restore()

    @staticmethod
    def _row(p, times, bits, vx0, vx1, scale, offset, row, col) -> None:
        t, b = row
        lo = int(np.searchsorted(times, vx0))
        hi = int(np.searchsorted(times, vx1))
        if hi <= lo:
            return
        visible = times[lo:hi]
        px0 = scale * vx0 + offset
        px1 = scale * vx1 + offset
        width = max(int(px1 - px0), 1)
        if len(visible) > 2 * width:
            # Density: count per pixel column, draw as grey level.
            counts, _edges = np.histogram(visible, bins=width, range=(vx0, vx1))
            peak = max(int(counts.max()), 1)
            for k, c in enumerate(counts):
                if c == 0:
                    continue
                shade = QColor(col)
                shade.setAlphaF(min(1.0, 0.25 + 0.75 * c / peak))
                p.fillRect(QRectF(px0 + k, t, 1, b - t), shade)
            return
        p.setPen(QPen(col, 2))
        if bits is None:
            for x in visible:
                dx = scale * x + offset
                p.drawLine(QPointF(dx, t), QPointF(dx, b))
            return
        # Decoded bits: a 1 is a full tick, a 0 a short one at the baseline.
        for x, bit in zip(visible, bits[lo:hi], strict=False):
            dx = scale * x + offset
            if bit:
                p.drawLine(QPointF(dx, t), QPointF(dx, b))
            else:
                p.drawLine(QPointF(dx, b - 3), QPointF(dx, b))


class StreamItem(_DeviceItem):
    """The stream view: every transition as a dot at (time, interval), like HxC's.

    An MFM stream falls into bands at 2, 3 and 4 cells; a GCR one at 1, 2
    and 3; jitter widens the bands, a speed change bends them, a dropout
    is a hole and noise is dots between bands. Up to ``DOTS_MAX`` points
    in view are drawn as dots; more than that becomes a density image
    (one cell per pixel, darker where more transitions fall), so the
    bands still show over a whole track. The y axis runs from 0 to the
    track's :meth:`Flux.interval_ceiling`, with a hairline per microsecond.
    """

    DOTS_MAX = 200_000

    def __init__(self, flux: Flux, x0: float, x1: float) -> None:
        super().__init__(x0, x1)
        self._flux = flux
        self._ceiling = flux.interval_ceiling()

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        h = bottom - top
        y0, y1 = top + h * 0.06, bottom - h * 0.06  # inset; y grows upward in interval
        px0, px1 = scale * vx0 + offset, scale * vx1 + offset
        width = max(int(px1 - px0), 1)
        height = max(int(y1 - y0), 1)
        ceiling = self._ceiling
        # Microsecond guide lines.
        guide = QColor(palette.text())
        guide.setAlphaF(0.12)
        p.setPen(QPen(guide, 1))
        us = 1e-6
        while us < ceiling:
            gy = y1 - (us / ceiling) * (y1 - y0)
            p.drawLine(QPointF(px0, gy), QPointF(px1, gy))
            us += 1e-6
        times, intervals = self._flux.intervals_between(vx0, vx1)
        if not len(times):
            p.restore()
            return
        ys = y1 - np.clip(intervals / ceiling, 0, 1) * (y1 - y0)
        xs = scale * times + offset
        col = palette.colour("flux").darker(140)
        if len(times) <= self.DOTS_MAX:
            p.setPen(QPen(col, 2.5))
            p.drawPoints(
                QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])
            )
        else:
            counts, _, _ = np.histogram2d(
                xs, ys, bins=(width, height), range=((px0, px1), (y0, y1))
            )
            peak = float(np.log1p(np.amax(counts))) if counts.size else 1.0
            shade = np.log1p(counts.T) / max(peak, 1e-9)  # rows = y
            rgba = np.zeros((height, width, 4), dtype=np.uint8)
            rgba[..., 0], rgba[..., 1], rgba[..., 2] = col.red(), col.green(), col.blue()
            rgba[..., 3] = (shade * 255).astype(np.uint8)
            img = QImage(rgba.tobytes(), width, height, width * 4, QImage.Format.Format_RGBA8888)
            p.drawImage(QRectF(px0, y0, width, height), img.copy())
        # Ceiling label, so the bands have a scale.
        p.setPen(QPen(palette.text()))
        p.setFont(_font(palette.FONT_PX["field-sub"], family=palette.UI))
        p.drawText(
            QRectF(px0 + 3, top, 80, 12), Qt.AlignmentFlag.AlignLeft, f"{ceiling * 1e6:.1f} us"
        )
        p.drawText(QRectF(px0 + 3, bottom - 13, 80, 12), Qt.AlignmentFlag.AlignLeft, "0")
        p.restore()


class BytesItem(_DeviceItem):
    """The Bytes lane: each run's bytes as a shade strip, 0x00 pale to 0xFF dark.

    One column per pixel when a run is wider than its byte count (the
    column is the mean of the bytes it covers); one cell per byte once
    zoomed in, with the hex value printed in the cell from ``BYTE_TEXT_PX``
    wide. Good enough to see patterns, exact once you are close.
    """

    def __init__(self, segments: Sequence[Segment], x0: float, x1: float) -> None:
        super().__init__(x0, x1)
        self._runs: list[ByteRun] = []
        self._starts = np.zeros(0)
        self._ends = np.zeros(0)
        self.set_segments(segments)

    def set_segments(self, segments: Sequence[Segment]) -> None:
        self._runs = sorted(
            (r for s in segments for r in s.byte_runs if r.data), key=lambda r: r.start
        )
        self._starts = np.array([r.start for r in self._runs])
        self._ends = np.array([r.end for r in self._runs])
        self.update()

    def byte_at(self, x: float) -> tuple[int, int] | None:
        """(offset in run, value) of the byte under view x, if any."""
        i = int(np.searchsorted(self._starts, x, side="right")) - 1
        if i < 0 or x >= self._runs[i].end:
            return None
        run = self._runs[i]
        k = int((x - run.start) / (run.end - run.start) * len(run.data))
        k = min(max(k, 0), len(run.data) - 1)
        return k, run.data[k]

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None or not self._runs:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        h = bottom - top
        t, b = top + h * 0.1, bottom - h * 0.1
        lo = int(np.searchsorted(self._ends, vx0, side="right"))
        hi = int(np.searchsorted(self._starts, vx1, side="left"))
        for run in self._runs[lo:hi]:
            buf = np.frombuffer(run.data, dtype=np.uint8)
            per_byte = scale * (run.end - run.start) / len(buf)  # pixels per byte
            dx0 = scale * run.start + offset
            dx1 = scale * run.end + offset
            if per_byte < 1.0:
                # Zoomed out: resample to one column per pixel.
                width = max(int(dx1 - dx0), 1)
                edges = np.linspace(0, len(buf), width + 1).astype(int)
                sums = np.add.reduceat(buf.astype(np.float64), np.minimum(edges[:-1], len(buf) - 1))
                counts = np.maximum(np.diff(edges), 1)
                cols = sums[:width] / counts[:width]
                p.drawImage(QRectF(dx0, t, dx1 - dx0, b - t), _strip(cols))
                continue
            # Zoomed in: only the visible bytes, one cell each.
            k0 = max(int((vx0 - run.start) / (run.end - run.start) * len(buf)) - 1, 0)
            k1 = min(int((vx1 - run.start) / (run.end - run.start) * len(buf)) + 2, len(buf))
            cells = buf[k0:k1]
            cx0 = dx0 + k0 * per_byte
            p.drawImage(QRectF(cx0, t, len(cells) * per_byte, b - t), _strip(cells))
            if per_byte >= BYTE_TEXT_PX:
                p.setFont(_font(min(12, int(per_byte / 2))))
                for j, value in enumerate(cells):
                    x = cx0 + j * per_byte
                    p.setPen(
                        QPen(
                            palette.text_on_dark()
                            if palette.byte_text_dark(value)
                            else palette.text()
                        )
                    )
                    p.drawText(
                        QRectF(x, t, per_byte, b - t), Qt.AlignmentFlag.AlignCenter, f"{value:02X}"
                    )
        p.restore()


def _strip(values: np.ndarray) -> QImage:
    """A 1-pixel-high image of byte shades (see :func:`palette.byte_shade`)."""
    rgba = np.ascontiguousarray(palette.byte_shade(values))
    img = QImage(rgba.tobytes(), len(values), 1, len(values) * 4, QImage.Format.Format_RGBA8888)
    return img.copy()  # detach from the numpy buffer


BOUNDARY_FADE_PX = (12.0, 60.0)  # boundaries this close fade: invisible at 12 px apart, full at 60


def boundary_colour(edges: np.ndarray, scale: float) -> QColor | None:
    """The boundary pen colour for this zoom, faded by how close the lines sit; None when invisible.

    Zoomed out, 150 segment lines over a thousand pixels are a picket fence
    that hides the lanes, so they fade with their spacing and are gone once
    they are a dozen pixels apart (the track strip still shows the segments).
    """
    if len(edges) < 2:
        return palette.boundary()
    spacing = float(np.median(np.diff(edges))) * scale
    lo, hi = BOUNDARY_FADE_PX
    if spacing <= lo:
        return None
    colour = QColor(palette.boundary())
    colour.setAlphaF(min(1.0, (spacing - lo) / (hi - lo)))
    return colour


def _nice_step(span: float, target_ticks: int = 8) -> float:
    """A 1-2-5 step so that ``span`` holds about ``target_ticks`` major ticks."""
    import math

    raw = span / max(target_ticks, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


class RulerItem(_DeviceItem):
    """The time (or byte) ruler across the top: 1-2-5 ticks with labels, and segment tags.

    Labels are the axis's own (``+2.680 s``, ``89,088 B``); each segment
    boundary gets a thin mark and, when the segment is wide enough, its
    title on the right of the boundary, the way Logic marks captures.
    """

    def __init__(self, track, x0: float, x1: float) -> None:
        super().__init__(x0, x1)
        self._track = track

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        base = bottom - 1
        p.setPen(QPen(palette.boundary(), 1))
        p.drawLine(QPointF(scale * vx0 + offset, base), QPointF(scale * vx1 + offset, base))
        step = _nice_step(vx1 - vx0)
        minor = step / 5
        axis = self._track.axis

        def fmt(value: float) -> str:
            return axis.format_tick(value, step)

        p.setFont(_font(palette.FONT_PX["boundary"], family=palette.UI))
        x = (int(vx0 / minor) - 1) * minor
        k = 0
        while x <= vx1:
            dx = scale * x + offset
            major = abs(x / step - round(x / step)) < 1e-6
            p.drawLine(QPointF(dx, base), QPointF(dx, base - (9 if major else 4)))
            if major:
                p.setPen(QPen(palette.text()))
                p.drawText(
                    QRectF(dx + 3, base - 22, 140, 12),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    fmt(x),
                )
                p.setPen(QPen(palette.boundary(), 1))
            x += minor
            k += 1
            if k > 2000:
                break
        # Segment tags, faded with the boundaries.
        tag_font = _font(palette.FONT_PX["segment-title"], bold=True, family=palette.UI)
        edges = np.array([s.start for s in self._track.segments])
        colour = boundary_colour(edges, scale)
        for seg in self._track.segments:
            if colour is None or seg.end < vx0 or seg.start > vx1:
                continue
            dx0 = scale * seg.start + offset
            dx1 = scale * seg.end + offset
            p.setPen(QPen(colour, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(dx0, top), QPointF(dx0, bottom))
            if dx1 - dx0 >= 70:
                # Tags live on the ruler's top line, tick labels on its bottom one.
                p.setFont(tag_font)
                p.setPen(QPen(palette.decoder_colour("QIC-80").darker(120)))
                p.drawText(
                    QRectF(dx0 + 4, top + 1, min(dx1 - dx0 - 6, 160), 12),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    seg.title,
                )
        p.restore()


class BoundaryItem(_DeviceItem):
    """Vertical segment boundary lines across a lane (dashed, hairline).

    With ``labels`` (the axis formatter) it also draws boundary positions
    and segment titles; the lane stack uses :class:`RulerItem` for that
    now and keeps this for the lines.
    """

    def __init__(self, segments: Sequence[Segment], x0: float, x1: float, *, labels=None) -> None:
        super().__init__(x0, x1)
        self._segments = list(segments)
        edges = [s.start for s in self._segments]
        if self._segments:
            edges.append(self._segments[-1].end)
        self._edges = np.array(edges)
        self._labels = labels  # Axis.format, or None for a line-only item

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        vb = self.getViewBox()
        if vb is None or not self._segments:
            return
        (vx0, vx1), _ = vb.viewRange()
        scale, offset, top, bottom = self._device(p)
        p.save()
        p.resetTransform()
        colour = boundary_colour(self._edges, scale)
        if colour is None:
            p.restore()
            return
        pen = QPen(colour, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        p.setPen(pen)
        lo = max(int(np.searchsorted(self._edges, vx0)) - 1, 0)
        hi = int(np.searchsorted(self._edges, vx1)) + 1
        px_last = -1e9
        for x in self._edges[lo:hi]:
            dx = scale * x + offset
            if dx - px_last < 3:
                continue
            px_last = dx
            if self._labels is None:
                p.drawLine(QPointF(dx, top), QPointF(dx, bottom))
            else:
                p.drawLine(QPointF(dx, bottom - 14), QPointF(dx, bottom))
                p.setFont(_font(palette.FONT_PX["boundary"]))
                p.drawText(
                    QRectF(dx + 3, bottom - 30, 140, 16),
                    Qt.AlignmentFlag.AlignLeft,
                    self._labels(x),
                )
        if self._labels is not None:
            p.setFont(_font(palette.FONT_PX["segment-title"], bold=True))
            for seg in self._segments[lo : hi + 1]:
                dx0 = scale * seg.start + offset
                dx1 = scale * seg.end + offset
                if dx1 - dx0 < 90:
                    continue
                p.drawText(QRectF(dx0, top, dx1 - dx0, 20), Qt.AlignmentFlag.AlignCenter, seg.title)
        p.restore()
