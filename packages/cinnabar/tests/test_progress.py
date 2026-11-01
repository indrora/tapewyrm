"""cinnabar.ui.progress: the Progress protocol becomes Qt signals."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from cinnabar.ui.progress import QtProgress  # noqa: E402


def test_qt_progress_emits_start_advance_finish():
    QApplication.instance() or QApplication([])
    prog = QtProgress()
    seen: list[tuple] = []
    prog.started.connect(lambda d, t, u: seen.append(("start", d, t, u)))
    prog.advanced.connect(lambda c: seen.append(("adv", c)))
    prog.finished.connect(lambda: seen.append(("done",)))
    with prog.task("parsing", total=10, unit="bytes") as task:
        task.advance(3)
        task.update(7)
        with prog.task("inner"):
            pass  # nested: finished fires only when the outermost ends
    assert seen == [
        ("start", "parsing", 10.0, "bytes"),
        ("adv", 3.0),
        ("adv", 7.0),
        ("start", "inner", 0.0, ""),
        ("done",),
    ]
