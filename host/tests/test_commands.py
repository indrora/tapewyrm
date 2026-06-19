"""QIC-117 command table integrity + N+2 argument encoding (DESIGN.md §13.1)."""

import pytest

from tapewyrm.qic117.commands import (
    BY_CODE,
    SOFT_SELECT_PULSES,
    TABLE,
    Cmd,
    Kind,
    encode_arg,
)

# ---------------------------------------------------------------------------
# Table integrity
# ---------------------------------------------------------------------------


def test_table_keyed_by_normalized_name():
    assert TABLE["LOGICAL_FORWARD"].code == 10
    assert TABLE["SEEK_HEAD_TO_TRACK"].code == 13
    assert TABLE["SOFT_SELECT"].code == 23
    assert TABLE["REPORT_DRIVE_STATUS"].code == 6


def test_codes_unique_and_in_range():
    codes = [c.code for c in TABLE.values()]
    assert len(codes) == len(set(codes))  # unique
    assert all(1 <= code <= 47 for code in codes)


def test_reserved_codes_absent():
    # 19, 20, 39 are reserved (DESIGN.md §13.1).
    for reserved in (19, 20, 39):
        assert reserved not in BY_CODE


def test_vendor_unique_codes_present():
    # 31, 40-45 are vendor-unique but defined slots (DESIGN.md §13.1).
    for code in (31, 40, 41, 42, 43, 44, 45):
        assert code in BY_CODE


def test_kinds_assigned():
    assert TABLE["SOFT_RESET"].kind is Kind.RESET
    assert TABLE["REPORT_NEXT_BIT"].kind is Kind.INTERNAL
    assert TABLE["PAUSE"].kind is Kind.MOTION
    assert TABLE["REPORT_DRIVE_STATUS"].kind is Kind.REPORT
    assert TABLE["LOGICAL_FORWARD"].kind is Kind.STREAM
    assert TABLE["ENTER_FORMAT_MODE"].kind is Kind.MODE
    assert TABLE["SOFT_SELECT"].kind is Kind.SELECT
    assert TABLE["SELECT_RATE_OR_FORMAT"].kind is Kind.CONFIG


def test_logical_forward_is_streaming():
    lf = TABLE["LOGICAL_FORWARD"]
    assert lf.is_streaming is True
    assert lf.kind is Kind.STREAM


def test_high_speed_flags():
    assert TABLE["PHYSICAL_REVERSE"].high_speed is True
    assert TABLE["PHYSICAL_FORWARD"].high_speed is True
    assert TABLE["LOGICAL_FORWARD"].high_speed is False


def test_non_intr_flags():
    assert TABLE["SEEK_LOAD_POINT"].non_intr is True
    assert TABLE["STOP_TAPE"].non_intr is True
    assert TABLE["LOGICAL_FORWARD"].non_intr is False


# ---------------------------------------------------------------------------
# N+2 argument encoding
# ---------------------------------------------------------------------------


def test_plain_arg_is_value_plus_two():
    # Seek Head to Track (13): Track+2, single train.
    assert encode_arg(TABLE["SEEK_HEAD_TO_TRACK"], 0) == [2]
    assert encode_arg(TABLE["SEEK_HEAD_TO_TRACK"], 5) == [7]
    assert encode_arg(TABLE["SEEK_HEAD_TO_TRACK"], 27) == [29]


def test_select_rate_or_format_plus_two():
    # Select Rate or Format (27): N+2 single train.
    assert encode_arg(TABLE["SELECT_RATE_OR_FORMAT"], 2) == [4]


def test_phantom_select_plus_two():
    assert encode_arg(TABLE["PHANTOM_SELECT"], 1) == [3]


def test_soft_select_is_literal_20():
    # Soft Select (23): a literal 20 pulses, value ignored (DESIGN.md §2.1).
    assert encode_arg(TABLE["SOFT_SELECT"], 0) == [SOFT_SELECT_PULSES]
    assert encode_arg(TABLE["SOFT_SELECT"], 99) == [20]


def test_skip_n_segs_two_nibbles():
    # Skip N Segs Reverse/Forward (25/26): (N&15)+2, (N>>4)+2.
    # N = 0x35 -> low nibble 5 -> 7, high nibble 3 -> 5.
    assert encode_arg(TABLE["SKIP_N_SEGS_FORWARD"], 0x35) == [7, 5]
    assert encode_arg(TABLE["SKIP_N_SEGS_REVERSE"], 0x35) == [7, 5]
    # N = 0 -> [2, 2].
    assert encode_arg(TABLE["SKIP_N_SEGS_FORWARD"], 0) == [2, 2]
    # N = 0xFF -> low 0xF -> 17, high 0xF -> 17.
    assert encode_arg(TABLE["SKIP_N_SEGS_FORWARD"], 0xFF) == [17, 17]


def test_skip_n_ext_three_nibbles():
    # Skip N Ext (34/35): three nibbles, each +2.
    # N = 0x123 -> nibbles 3,2,1 -> 5,4,3.
    assert encode_arg(TABLE["SKIP_N_EXT_FORWARD"], 0x123) == [5, 4, 3]
    assert encode_arg(TABLE["SKIP_N_EXT_REVERSE"], 0x123) == [5, 4, 3]
    assert encode_arg(TABLE["SKIP_N_EXT_FORWARD"], 0) == [2, 2, 2]


def test_set_n_format_segments_three_nibbles():
    # Set N Format Segments (38): three nibbles, each +2.
    assert encode_arg(TABLE["SET_N_FORMAT_SEGMENTS"], 0x123) == [5, 4, 3]
    # 207 = 0x0CF -> nibbles F, C, 0 -> 17, 14, 2.
    assert encode_arg(TABLE["SET_N_FORMAT_SEGMENTS"], 207) == [17, 14, 2]


def test_encode_arg_rejects_non_arg_command():
    with pytest.raises(ValueError):
        encode_arg(TABLE["STOP_TAPE"], 1)


def test_encode_arg_rejects_negative():
    with pytest.raises(ValueError):
        encode_arg(TABLE["SEEK_HEAD_TO_TRACK"], -1)


def test_cmd_is_frozen():
    import dataclasses

    c = Cmd(99, Kind.MOTION, False, "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.code = 1  # type: ignore[misc]
