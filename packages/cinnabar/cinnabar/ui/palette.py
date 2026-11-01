"""Colour schemes, and the one place a span style becomes a colour.

Every annotator tags its spans with a *style token* ("record", "crc-bad",
"vtbl-entry"...). Tokens map to a small set of **roles**, and a
:class:`Theme` gives each role a colour. Roles are the sketch's
vocabulary: the primary hue for records and long data fields, a lighter
primary for their contents, olive for pre/postambles, green for short
marks and fields, red for bad, grey for undecoded, amber for corrected or
noteworthy, purple for parity and extensions, teal for sections. The
Bytes lane is not a token: a byte's value is its shade between the
theme's ``byte_light`` (0x00) and ``byte_dark`` (0xFF), so zeros are pale,
0xFF fill is a dark bar, and text sits in between; rough on purpose, the
value is printed once a byte is wide enough on screen.

Four schemes ship (:data:`THEMES`): ``sketch`` (the design sketch's light
grey lanes and blues), ``paper`` (warmer, lower contrast), ``dark`` and
``contrast`` (high contrast, for projectors and bad eyes). ``cinnabar
--theme`` picks one; the View menu switches at run time. Widgets read
colours through :func:`colour` and the module-level names at draw time,
so a switch is a repaint.

One font family, :data:`MONO`, everywhere, at the sizes in
:data:`FONT_PX`: the lane labels, span labels, the hover panel and the
track title all use it, so the window reads as one thing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtGui import QColor

MONO = "Courier New"  # bytes, hexdumps, span labels: things that line up
UI = "sans-serif"  # channel names, ruler, panels: the platform's UI face
FONT_PX = {
    "record": 12,  # records lane label
    "record-sub": 10,
    "field": 10,  # every other span lane
    "field-sub": 9,
    "lane-title": 11,  # channel names in the header column
    "lane-sub": 9,  # the decoder tag under a channel name
    "boundary": 10,  # the time ruler's labels
    "segment-title": 10,
}

# Colour of a decoder's tag and stripe in the channel header column, the
# way Logic colours each channel and PulseView each decoder.
DECODER_ROLE: dict[str, str] = {
    "Flux": "grey",
    "MFM": "primary",
    "GCR": "purple",
    "TWTI": "primary",
    "QIC-80": "teal",
    "QIC-113": "green",
    "QIC-122": "amber",
    "Bytes": "grey",
}


def decoder_colour(decoder: str) -> QColor:
    return QColor(getattr(_current, DECODER_ROLE.get(decoder, "grey")))


@dataclass(frozen=True)
class Theme:
    name: str
    window_bg: str
    header_bg: str  # the channel header column
    lane_bg: str
    text: str
    text_on_dark: str
    boundary: str
    primary: str
    primary_light: str
    olive: str
    green: str
    red: str
    grey: str
    grey_light: str
    amber: str
    purple: str
    teal: str
    pending: str
    byte_light: str
    byte_dark: str
    window_tint: str  # the overview's viewed-window overlay, with alpha


THEMES: dict[str, Theme] = {
    "sketch": Theme(
        name="sketch",
        window_bg="#ffffff",
        header_bg="#f3f3f3",
        lane_bg="#ebebeb",
        text="#111111",
        text_on_dark="#f4f4f4",
        boundary="#202020",
        primary="#5aaee0",
        primary_light="#9bd3ee",
        olive="#8b8b3a",
        green="#7dcea0",
        red="#e74c3c",
        grey="#8a8a8a",
        grey_light="#bdbdbd",
        amber="#e0b050",
        purple="#b39ddb",
        teal="#4db6ac",
        pending="#cfcfcf",
        byte_light="#e8f1f8",
        byte_dark="#0b1f4a",
        window_tint="#5a5a5a40",
    ),
    "paper": Theme(
        name="paper",
        window_bg="#f7f3ea",
        header_bg="#efe9dc",
        lane_bg="#e9e3d6",
        text="#2b2520",
        text_on_dark="#f7f3ea",
        boundary="#4a4038",
        primary="#6a93b8",
        primary_light="#a9c3d8",
        olive="#8f8a4f",
        green="#8fb996",
        red="#c0504d",
        grey="#948c82",
        grey_light="#c4bcb0",
        amber="#d2a24c",
        purple="#a08cc0",
        teal="#5f9e95",
        pending="#d6cfc2",
        byte_light="#f3eee3",
        byte_dark="#2e2a40",
        window_tint="#4a403840",
    ),
    "dark": Theme(
        name="dark",
        window_bg="#16181d",
        header_bg="#1d2026",
        lane_bg="#23262e",
        text="#e6e6e6",
        text_on_dark="#f4f4f4",
        boundary="#c8c8c8",
        primary="#3f8fc4",
        primary_light="#6fb3dc",
        olive="#9a9a46",
        green="#5fb98a",
        red="#d9534f",
        grey="#6e727a",
        grey_light="#4b4f57",
        amber="#d6a23c",
        purple="#9d86c9",
        teal="#3fa39a",
        pending="#2f333c",
        byte_light="#2a3140",
        byte_dark="#a8c8f0",
        window_tint="#ffffff30",
    ),
    "contrast": Theme(
        name="contrast",
        window_bg="#ffffff",
        header_bg="#e8e8e8",
        lane_bg="#f6f6f6",
        text="#000000",
        text_on_dark="#ffffff",
        boundary="#000000",
        primary="#0057b8",
        primary_light="#7fb2ff",
        olive="#6b6b00",
        green="#008a3e",
        red="#d50000",
        grey="#5a5a5a",
        grey_light="#a0a0a0",
        amber="#c77700",
        purple="#6a1b9a",
        teal="#00796b",
        pending="#d0d0d0",
        byte_light="#ffffff",
        byte_dark="#000000",
        window_tint="#0057b830",
    ),
}

# style token -> role (a Theme attribute)
_ROLES: dict[str, str] = {
    "neutral": "grey",
    "unknown": "grey",
    # physical records (sectors.py, qic100.py)
    "record": "primary",
    "record-bad": "red",
    "record-id": "green",
    "record-parity": "purple",
    "record-man": "amber",
    "record-uselog": "amber",
    "record-fs": "primary",
    "preamble": "olive",
    "postamble": "olive",
    "mark": "green",
    "idfield": "green",
    "idfield-sub": "green",
    "data": "primary",
    "data-bad": "red",
    "data-sys": "primary_light",
    "crc": "green",
    "crc-bad": "red",
    "gap": "grey_light",
    "ecc": "purple",
    "slot-missing": "grey",
    "slot-excluded": "grey_light",
    "slot-bad": "grey",
    "slot-uncorrectable": "red",
    "slot-corrected": "amber",
    # header (header.py)
    "fpr": "primary",
    "fpr-field": "primary_light",
    "bsm": "teal",
    "bsm-entry": "amber",
    "bsm-end": "grey",
    # volume table and volumes (volume.py)
    "vtbl": "primary",
    "vtbl-entry": "primary_light",
    "vtbl-ext": "purple",
    "vtbl-end": "grey",
    "vtbl-field": "green",
    "vendor": "grey_light",
    "sig": "red",
    "dir-section": "teal",
    "data-section": "primary",
    "dir-entry": "primary_light",
    "dir-subdir": "teal",
    "dir-fixed": "green",
    "dir-name": "primary_light",
    "data-entry": "primary",
    "path": "green",
    "filedata": "primary_light",
    "data-area": "purple",
    "extent": "primary",
    "extent-next": "green",
    "extent-offset": "green",
    "frame": "primary_light",
    "frame-raw": "purple",
    "frame-size": "green",
    "fill": "grey_light",
    # overview strip states (TWS-2 section 5.2) and decode states
    "clean": "primary",
    "corrected": "amber",
    "uncorrectable": "red",
    "missing": "grey_light",
    "bad": "grey",
    "flux": "primary",
    "pending": "pending",
}

_current: Theme = THEMES["sketch"]


def theme() -> Theme:
    return _current


def set_theme(name: str) -> Theme:
    global _current
    _current = THEMES[name]
    return _current


def colour(style: str) -> QColor:
    return QColor(getattr(_current, _ROLES.get(style, "grey")))


def dark(style: str) -> bool:
    """Whether a label on this fill wants light text."""
    return colour(style).lightnessF() < 0.55


def lane_bg() -> QColor:
    return QColor(_current.lane_bg)


def header_bg() -> QColor:
    return QColor(_current.header_bg)


def window_bg() -> QColor:
    return QColor(_current.window_bg)


def text() -> QColor:
    return QColor(_current.text)


def text_on_dark() -> QColor:
    return QColor(_current.text_on_dark)


def boundary() -> QColor:
    return QColor(_current.boundary)


def window_tint() -> QColor:
    c = _current.window_tint
    q = QColor(c[:7])
    q.setAlpha(int(c[7:9], 16) if len(c) == 9 else 64)
    return q


def _rgb(hex6: str) -> np.ndarray:
    return np.array([int(hex6[1:3], 16), int(hex6[3:5], 16), int(hex6[5:7], 16)], dtype=np.float32)


def byte_shade(values: np.ndarray) -> np.ndarray:
    """RGBA rows for byte values (0..255, any float array): light for 0x00, dark for 0xFF."""
    v = (np.clip(values, 0, 255).astype(np.float32) / 255.0)[:, None]
    rgb = (_rgb(_current.byte_light) * (1 - v) + _rgb(_current.byte_dark) * v).astype(np.uint8)
    alpha = np.full((len(rgb), 1), 255, dtype=np.uint8)
    return np.concatenate([rgb, alpha], axis=1)


def byte_text_dark(value: float) -> bool:
    """Whether a value printed on its own shade wants light text."""
    light = QColor(_current.byte_light).lightnessF()
    dark_ = QColor(_current.byte_dark).lightnessF()
    mix = light + (dark_ - light) * (value / 255.0)
    return mix < 0.55
