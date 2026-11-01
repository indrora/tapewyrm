"""The pyqtgraph window. Nothing here knows QIC; it draws :mod:`cinnabar.model`.

- :mod:`cinnabar.ui.palette`: style token -> colour, the one place colours live.
- :mod:`cinnabar.ui.items`: the custom ``GraphicsObject``s: a lane of
  spans, the flux rows, the byte heat strip, the segment boundaries.
- :mod:`cinnabar.ui.lanes`: the stack of lanes for one track, x-linked.
- :mod:`cinnabar.ui.overview`: the whole-track strip with the viewed window.
- :mod:`cinnabar.ui.app`: the main window, track and segment navigation,
  background track loading, and :func:`run`.

Qt is imported only here, so ``cinnabar --dump`` and the tests never need
a display (the screenshot test uses the offscreen platform plugin).
"""
