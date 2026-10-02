"""Cartridge guess from header geometry (tape/cartridge.py)."""

from __future__ import annotations

import pytest

from tapewyrm.tape import cartridge


def test_rev_n_worked_example():
    """QIC-80-MC Rev N §5.4.1: a 425 ft tape needs at least 207 segments/track."""
    assert cartridge.min_segments_per_track(425) == 207


@pytest.mark.parametrize("segments", [99, 100, 149, 150, 207, 487])
def test_length_inverts_the_minimum(segments):
    length = cartridge.length_from_segments(segments)
    assert cartridge.min_segments_per_track(length) == segments


def test_3m_dc2120_bench_tape():
    """Format code 2, 28 x 150: the 3M DC2120 (307.5 ft, 120 MB)."""
    guess = cartridge.guess(28, 150)
    assert guess.cartridge is not None
    assert (guess.cartridge.name, guess.cartridge.length_ft) == ("DC2120", 307.5)
    assert not guess.exact  # fixed format: 150, one over the Rev N minimum of 149


def test_colorado_425ft_bench_tape():
    guess = cartridge.guess(28, 207)
    assert guess.cartridge is not None
    assert guess.cartridge.length_ft == 425 and guess.exact
    assert "550 Oe" in guess.describe()


def test_dc2080():
    guess = cartridge.guess(28, 100)
    assert guess.cartridge is not None and guess.cartridge.name == "DC2080"


def test_wide_tape():
    guess = cartridge.guess(36, cartridge.min_segments_per_track(750))
    assert guess.cartridge is not None
    assert (guess.cartridge.width_in, guess.cartridge.length_ft) == (0.315, 750)


def test_qic40_counts_are_exact():
    """QIC-40-MC Rev M §5.3 fixes 68 / 102 / 365 segments per track."""
    guess = cartridge.guess(20, 68)
    assert guess.cartridge is not None and guess.cartridge.name == "DC2000" and guess.exact
    assert cartridge.guess(20, 70).cartridge is None


def test_unknown_track_count():
    guess = cartridge.guess(40, 150)
    assert guess.cartridge is None
    assert "40 tracks" in guess.describe()


def test_length_far_from_any_cartridge():
    assert cartridge.guess(28, 300).cartridge is None  # ~617 ft: nothing catalogued
