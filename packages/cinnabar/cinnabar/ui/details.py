"""The details panel: what you clicked, its decoded rows, and its bytes as a colour-coded hexdump.

Click a span in any lane and the panel shows its label, its hover rows
and the bytes under it, sixteen to a line, each byte shaded with the
colour of the deepest span that covers it in the lane's tree. The same
colour code the lanes use, so a VTBL entry's hexdump shows its
signature red, its description blue and its flags green, exactly where
they sit. Click a byte in the Bytes lane and the panel shows the
segment's bytes around it. Export writes the shown bytes, or the whole
segment's, to a file.

:func:`hexdump_html` is the pure part (bytes and colours in, HTML out),
tested on its own; the widget only glues it to Qt.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cinnabar.model import BytePiece, Segment, Span, Tape, Track
from cinnabar.ui import palette
from cinnabar.ui.lanes import describe

BYTES_PER_LINE = 16
MAX_LINES = 2048  # a whole 29,696-byte segment is 1,856 lines; past that, say so


def hexdump_html(
    pieces: Sequence[BytePiece],
    colour_at: Callable[[float], QColor | None],
    *,
    max_lines: int = MAX_LINES,
) -> str:
    """Bytes as HTML: offset, sixteen hex cells each on its span's colour, ASCII.

    ``colour_at`` gives the fill for a byte at an axis position (None for
    no colour). Pieces that are not contiguous in segment offset start a
    new line with a gap marker, so a missing sector shows as a hole.
    """
    rows: list[str] = []
    lines = 0
    mono = palette.MONO
    prev_end: int | None = None
    for piece in pieces:
        if prev_end is not None and piece.offset != prev_end:
            rows.append(
                f"<tr><td colspan='3' style='color:#888'>&nbsp;&nbsp;... {piece.offset - prev_end:,} bytes not read ...</td></tr>"
            )
        start = piece.offset - (piece.offset % BYTES_PER_LINE)
        for line_off in range(start, piece.offset + len(piece.data), BYTES_PER_LINE):
            if lines >= max_lines:
                rows.append(
                    f"<tr><td colspan='3' style='color:#888'>&nbsp;&nbsp;... {max_lines:,} lines shown ...</td></tr>"
                )
                return _table(rows, mono)
            cells: list[str] = []
            ascii_cells: list[str] = []
            for k in range(BYTES_PER_LINE):
                off = line_off + k
                i = off - piece.offset
                if i < 0 or i >= len(piece.data):
                    cells.append("<span>&nbsp;&nbsp;</span>")
                    ascii_cells.append(" ")
                    continue
                value = piece.data[i]
                col = colour_at(piece.run.axis_of(piece.first + i))
                style = ""
                if col is not None:
                    fg = palette.text_on_dark() if col.lightnessF() < 0.55 else palette.text()
                    style = f" style='background:{col.name()};color:{fg.name()}'"
                cells.append(f"<span{style}>{value:02X}</span>")
                ch = chr(value) if 32 <= value < 127 else "."
                ascii_cells.append(html.escape(ch))
            gap = "&nbsp;"
            hexes = gap.join(cells[:8]) + gap + gap + gap.join(cells[8:])
            rows.append(
                f"<tr><td style='color:#888;white-space:pre'>{line_off:06x}&nbsp;&nbsp;</td>"
                f"<td style='white-space:pre'>{hexes}</td>"
                f"<td style='white-space:pre'>&nbsp;&nbsp;|{''.join(ascii_cells)}|</td></tr>"
            )
            lines += 1
        prev_end = piece.offset + len(piece.data)
    if not rows:
        rows.append("<tr><td style='color:#888'>(no bytes were read here)</td></tr>")
    return _table(rows, mono)


def _table(rows: list[str], mono: str) -> str:
    return (
        f"<table style='font-family:{mono};font-size:11px;border-spacing:0'>"
        + "".join(rows)
        + "</table>"
    )


HINT = "Click a span or a byte for its details."


class DetailsPanel(QWidget):
    """The right-hand panel. ``show`` takes what the lanes reported on a click.

    While nothing is selected it shows the tape's and the track's rows
    (``set_overview``): for a flux capture that is where the header's
    geometry and the volumes appear once the tape has been identified.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumWidth(420)
        self.track: Track | None = None
        self.segment: Segment | None = None
        self.span: Span | None = None
        self._bytes: list[BytePiece] = []
        self._overview = ""  # the tape's and track's rows, shown while nothing is selected
        self.title = QLabel(HINT)
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setWordWrap(True)
        self.title.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.dump = QTextBrowser()
        self.dump.setFont(QFont(palette.MONO))
        self.dump.setOpenLinks(False)
        self.export_span = QPushButton("Export these bytes…")
        self.export_segment = QPushButton("Export segment…")
        self.export_span.clicked.connect(lambda: self._export(segment=False))
        self.export_segment.clicked.connect(lambda: self._export(segment=True))
        buttons = QHBoxLayout()
        buttons.addWidget(self.export_span)
        buttons.addWidget(self.export_segment)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.title)
        layout.addWidget(self.dump, 1)
        layout.addLayout(buttons)
        self._enable(False)

    def _enable(self, on: bool) -> None:
        self.export_span.setEnabled(on and bool(self._bytes))
        self.export_segment.setEnabled(on and self.segment is not None)

    def clear(self) -> None:
        self.track = self.segment = self.span = None
        self._bytes = []
        self.title.setText(self._overview or HINT)
        self.dump.clear()
        self._enable(False)

    def set_overview(self, tape: Tape, track: Track | None) -> None:
        """What the panel shows while nothing is selected: the tape's rows, then the track's.

        A flux tape's rows grow once it is identified (name, format,
        geometry, volumes), so the window calls this on every refresh; the
        text on screen only changes while nothing is selected.
        """
        parts = [f"<b>{html.escape(Path(tape.source).name)}</b>", _rows(tape.detail)]
        if track is not None:
            parts += [f"<b>{html.escape(track.label)}</b>", _rows(track.detail)]
        parts.append(f"<span style='color:#666'>{HINT}</span>")
        self._overview = f"<div style='font-family:{palette.UI}'>{''.join(parts)}</div>"
        if self.span is None and self.segment is None:
            self.title.setText(self._overview)

    def show(self, track: Track, span: Span | None, lane: str, x: float) -> None:  # type: ignore[override]
        """Show ``span`` (from ``lane``, clicked at ``x``) of ``track``."""
        self.track = track
        self.segment = track.segment_at(x)
        self.span = span
        if self.segment is None:
            self.clear()
            return
        seg = self.segment
        fmt = track.axis.format
        if span is None or span.end <= span.start:
            # A byte, or empty lane: show the segment's bytes around x.
            lo, hi = seg.start, seg.end
            head = describe(span, lane, x, fmt) if span is not None else ""
            head += f"<div style='font-family:{palette.MONO}'><b>{seg.title}</b> {fmt(seg.start)} .. {fmt(seg.end)}</div>"
            head += _rows(seg.detail)
            tree: list[Span] = seg.records + seg.fields
        else:
            lo, hi = span.start, span.end
            head = describe(span, lane, x, fmt)
            # The logical structure colours the bytes where it covers them
            # (a VTBL entry's fields inside a sector); the clicked span's own
            # tree colours the rest (a sector's data, a record's ID field).
            tree = seg.fields + [span]
        self.title.setText(head)
        self._bytes = seg.bytes_between(lo, hi)

        def colour_at(pos: float) -> QColor | None:
            for root in tree:
                if root.start <= pos < root.end:
                    deepest = root.deepest_at(pos)
                    return palette.colour(deepest.style)
            return None

        self.dump.setHtml(hexdump_html(self._bytes, colour_at))
        self._enable(True)

    def _export(self, *, segment: bool) -> None:
        if self.track is None or self.segment is None:
            return
        if segment:
            pieces = self.segment.bytes_between(self.segment.start, self.segment.end)
            stem = f"{self.track.label}-{self.segment.title}".lower().replace(" ", "-")
        else:
            pieces = self._bytes
            label = (self.span.label if self.span is not None else "bytes").lower()
            stem = (
                f"{self.track.label}-{self.segment.title}-{label}".lower()
                .replace(" ", "-")
                .replace("/", "-")
            )
        path, _ = QFileDialog.getSaveFileName(
            self, "Export bytes", f"{stem}.bin", "Binary (*.bin);;All files (*)"
        )
        if path:
            write_pieces(Path(path), pieces)


def write_pieces(path: Path, pieces: Sequence[BytePiece]) -> int:
    """Write the pieces' bytes to ``path`` at their segment offsets, zero where nothing was read.

    Returns the byte count written. A hole (a sector never read) comes out
    as zeros so offsets inside the file match offsets in the segment.
    """
    if not pieces:
        path.write_bytes(b"")
        return 0
    base = min(p.offset for p in pieces)
    end = max(p.offset + len(p.data) for p in pieces)
    buf = bytearray(end - base)
    for p in pieces:
        buf[p.offset - base : p.offset - base + len(p.data)] = p.data
    path.write_bytes(bytes(buf))
    return len(buf)


def _rows(detail: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f"<tr><td style='color:#666'>{html.escape(k)}</td><td>&nbsp;{html.escape(v)}</td></tr>"
        for k, v in detail
    )
    return f"<table style='font-family:{palette.MONO}'>{cells}</table>"
