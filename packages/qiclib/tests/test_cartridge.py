"""Cartridge guess from header geometry (tape/cartridge.py)."""

from __future__ import annotations

import pytest

from qiclib import cartridge


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
    assert (guess.cartridge.model, guess.cartridge.length_ft) == ("DC2120", 307.5)
    assert not guess.exact  # fixed format: 150, one over the Rev N minimum of 149


def test_colorado_425ft_bench_tape():
    guess = cartridge.guess(28, 207)
    assert guess.cartridge is not None
    assert guess.cartridge.length_ft == 425 and guess.exact
    assert "550 Oe" in guess.describe()


def test_dc2080():
    guess = cartridge.guess(28, 100)
    assert guess.cartridge is not None and guess.cartridge.model == "DC2080"


def test_wide_tape():
    guess = cartridge.guess(36, cartridge.min_segments_per_track(750))
    assert guess.cartridge is not None
    assert (guess.cartridge.width_in, guess.cartridge.length_ft) == (0.315, 750)


def test_qic40_counts_are_exact():
    """QIC-40-MC Rev M §5.3 fixes 68 / 102 / 365 segments per track."""
    guess = cartridge.guess(20, 68)
    assert guess.cartridge is not None and guess.cartridge.model == "DC2000" and guess.exact
    assert cartridge.guess(20, 70).cartridge is None


def test_unknown_track_count():
    guess = cartridge.guess(44, 150)
    assert guess.cartridge is None
    assert "44 tracks" in guess.describe()


def test_length_far_from_any_cartridge():
    assert cartridge.guess(28, 300).cartridge is None  # ~617 ft: nothing catalogued


# ---------------------------------------------------------------------------
# QIC-3010 / QIC-3020
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("standard", "expected"), [("QIC-3010", 219), ("QIC-3020", 429)])
def test_30x0_worked_examples(standard, expected):
    """§5.4.1 of each standard works a 300 ft tape: 219 and 429 segments/track."""
    assert cartridge.min_segments_per_track(300, standard) == expected


@pytest.mark.parametrize("standard", ["QIC-3010", "QIC-3020"])
@pytest.mark.parametrize("segments", [219, 292, 429, 572, 1431, 1475])
def test_30x0_length_inverts_the_minimum(standard, segments):
    length = cartridge.length_from_segments(segments, standard)
    assert cartridge.min_segments_per_track(length, standard) == segments


@pytest.mark.parametrize(
    ("tracks", "width", "length", "capacity"),
    [(40, 0.250, 400, 680), (40, 0.250, 1000, 1700), (50, 0.315, 400, 833), (50, 0.315, 750, 1600)],
)
def test_qic3020_cover_lengths(tracks, width, length, capacity):
    """Every QIC-3020-MC Rev H cover tape, at its §5.4.1 minimum, guesses itself."""
    spt = cartridge.min_segments_per_track(length, "QIC-3020")
    guess = cartridge.guess(tracks, spt, "QIC-3020", "test")
    c = guess.cartridge
    assert c is not None and guess.exact
    assert (c.standard, c.width_in, c.length_ft, c.capacity_mb) == (
        "QIC-3020",
        width,
        length,
        capacity,
    )
    assert "900 Oe" in guess.describe()


def test_qicextra_bench_geometry():
    """40 x 1475 is only QIC-3020 (~1,030 ft); QIC-3010 would need ~2,019 ft."""
    guess = cartridge.guess(40, 1475)
    assert guess.cartridge is not None
    assert (guess.cartridge.model, guess.cartridge.length_ft) == ("MC3020EX", 1000)
    assert guess.estimated_ft is not None and 1025 < guess.estimated_ft < 1035
    text = guess.describe()
    assert text.startswith("QIC-3020, 1,000 ft x 0.250 in (MC3020EX class, Verbatim QIC-Extra)")
    assert "1.7 GB native" in text and "only QIC-3020 fits" in text


def test_ambiguous_30x0_lists_both():
    """50 x 547 is QIC-3010 750 ft or QIC-3020 400 ft (~382 ft): no pick without a hint."""
    guess = cartridge.guess(50, 547)
    assert guess.cartridge is None and guess.standard is None
    assert [(c.standard, c.length_ft) for c in guess.alternatives] == [
        ("QIC-3010", 750),
        ("QIC-3020", 400),
    ]
    assert " or " in guess.describe() and "tape status" in guess.describe()


@pytest.mark.parametrize(("hint", "length"), [("QIC-3010", 750), ("QIC-3020", 400)])
def test_hint_settles_30x0(hint, length):
    guess = cartridge.guess(50, 547, hint, "the drive's tape status")
    assert guess.cartridge is not None
    assert (guess.cartridge.standard, guess.cartridge.length_ft) == (hint, length)
    assert f"{hint} per the drive's tape status" in guess.describe()


def test_hint_against_geometry_finds_nothing():
    """A QIC-3010 hint on the QIC-Extra's 1475 segments: ~2,019 ft is no 3010 tape."""
    guess = cartridge.guess(40, 1475, "QIC-3010", "test")
    assert guess.cartridge is None and "2,019 ft" in guess.describe()


def test_hint_for_another_track_count_is_ignored():
    """A QIC-80 report can't overrule a 40-track header."""
    guess = cartridge.guess(40, 1475, "QIC-80", "test")
    assert guess.cartridge is not None and guess.cartridge.standard == "QIC-3020"


# ---------------------------------------------------------------------------
# Cartridge profiles (TOML)
# ---------------------------------------------------------------------------


def test_builtin_profiles_load():
    """Every packaged profile loads, and its file name is its profile name."""
    names = cartridge.builtin_names()
    assert "qic-extra" in names and "qic80-dc2120" in names
    assert [c.profile for c in map(cartridge.load, names)] == names
    assert len(cartridge.catalogue()) == len(names)


def test_qic_extra_profile():
    c = cartridge.load("qic-extra")
    assert (c.standard, c.tracks, c.width_in, c.length_ft) == ("QIC-3020", 40, 0.25, 1000)
    assert (c.model, c.vendor, c.label, c.label_capacity) == (
        "MC3020EX",
        "Verbatim",
        "QIC-Extra",
        "1.6 GB",
    )
    assert c.coercivity == "900 Oe"


def test_load_from_path(tmp_path):
    path = tmp_path / "mine.toml"
    path.write_text(
        'name = "mine"\nstandard = "QIC-80"\ntracks = 28\nwidth_in = 0.25\nlength_ft = 425\n'
    )
    c = cartridge.load(str(path))
    assert (c.profile, c.capacity_mb, c.path) == ("mine", 0, str(path))


GOOD = 'name = "x"\nstandard = "QIC-80"\ntracks = 28\nwidth_in = 0.25\nlength_ft = 425\n'


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name = ", "x.toml"),  # not TOML
        (GOOD.replace('name = "x"\n', ""), "missing 'name'"),
        (GOOD + "colour = 1\n", "unknown key"),
        (GOOD.replace("tracks = 28", 'tracks = "28"'), "tracks = '28' must be int"),
        (GOOD.replace("QIC-80", "QIC-999"), "standard 'QIC-999'"),
        (GOOD.replace("QIC-80", "QIC-40"), "give segments_per_track"),
    ],
)
def test_malformed_profile(tmp_path, text, message):
    path = tmp_path / "x.toml"
    path.write_text(text)
    with pytest.raises(cartridge.CartridgeProfileError, match=message):
        cartridge.load(str(path))


def test_unknown_profile_name_lists_builtins():
    with pytest.raises(cartridge.CartridgeProfileError, match="qic-extra"):
        cartridge.load("no-such-cartridge")
