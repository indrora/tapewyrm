"""The bottom strip: the whole track, with the viewed window drawn on it.

The strip is the track's bytes as a shade image (:meth:`Track.heat`), one
column per pixel, over a thin bar of segment states coloured as the
segment table says (clean, corrected, uncorrectable, missing, bad). A
:class:`pyqtgraph.LinearRegionItem` is the window: drag it to scrub, click
anywhere to jump there. Moving the main view moves the region and the
other way round; a guard stops the two from feeding each other.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Signal

from cinnabar.model import Track
from cinnabar.ui import palette

HEIGHT = 72
COLUMNS = 1200


class Overview(pg.GraphicsLayoutWidget):
    window_moved = Signal(float, float)

    def __init__(self) -> None:
        super().__init__()
        self.setBackground(palette.window_bg())
        self.setFixedHeight(HEIGHT)
        self.ci.layout.setContentsMargins(0, 0, 0, 0)
        self.vb = pg.ViewBox(enableMenu=False)
        self.vb.setMouseEnabled(x=False, y=False)
        self.vb.setBackgroundColor(palette.lane_bg())
        self.ci.addItem(self.vb)
        self._heat = pg.ImageItem()
        self._states = pg.ImageItem()
        self.vb.addItem(self._heat)
        self.vb.addItem(self._states)
        self.region = pg.LinearRegionItem(
            brush=palette.window_tint(),
            hoverBrush=palette.window_tint().lighter(130),
            pen=pg.mkPen(palette.boundary(), width=1),
            movable=True,
        )
        self.region.setZValue(10)
        self.vb.addItem(self.region)
        self.region.sigRegionChanged.connect(self._region_changed)
        self.vb.scene().sigMouseClicked.connect(self._clicked)
        self._guard = False
        self.track: Track | None = None

    def set_track(self, track: Track) -> None:
        self.track = track
        x0, x1 = track.start, track.end
        heat = track.heat(COLUMNS)
        rgba = palette.byte_shade(np.nan_to_num(heat, nan=0.0))
        bg = palette.lane_bg()
        rgba[np.isnan(heat)] = (bg.red(), bg.green(), bg.blue(), 255)
        self._heat.setImage(rgba[:, None, :], autoLevels=False)
        self._heat.setRect(QRectF(x0, 0.2, x1 - x0, 0.8))
        states = np.array(
            [palette.colour(s).getRgb() for s in track.overview] or [bg.getRgb()],
            dtype=np.uint8,
        )
        self._states.setImage(states[:, None, :], autoLevels=False)
        self._states.setRect(QRectF(x0, 0.0, x1 - x0, 0.18))
        self.vb.setLimits(xMin=x0, xMax=x1)
        self.vb.setRange(xRange=(x0, x1), yRange=(0, 1), padding=0)
        self.region.setBounds((x0, x1))

    def set_window(self, a: float, b: float) -> None:
        if self._guard:
            return
        self._guard = True
        self.region.setRegion((a, b))
        self._guard = False

    def _region_changed(self) -> None:
        if self._guard:
            return
        a, b = self.region.getRegion()
        self._guard = True
        self.window_moved.emit(a, b)
        self._guard = False

    def _clicked(self, ev) -> None:
        if self.track is None or ev.button() != pg.QtCore.Qt.MouseButton.LeftButton:
            return
        if self.region.sceneBoundingRect().contains(ev.scenePos()):
            return  # a click on the window is the start of a drag
        x = self.vb.mapSceneToView(ev.scenePos()).x()
        a, b = self.region.getRegion()
        half = (b - a) / 2
        x0 = min(max(x - half, self.track.start), self.track.end - 2 * half)
        self.region.setRegion((x0, x0 + 2 * half))
