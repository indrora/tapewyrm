"""Rev J error names, reset classification, vendor ID and tape-type decoding."""

import pytest

from tapewyrm.qic117.status import (
    BENIGN_ERRORS,
    TAPE_TYPES,
    classify_error,
    decode_vendor_id,
    error_name,
)


def test_error_names_follow_rev_j():
    assert error_name(0) == "no error"
    assert error_name(1) == "Command Received while Drive Not Ready"
    assert error_name(26) == "Power On Reset Occurred"
    assert error_name(41) == "Drive Wakeup Reset Occurred"
    assert error_name(99) == "unknown/vendor error 99"


def test_reset_codes_are_benign_and_code_1_is_not_a_reset():
    # The old table called code 1 "reset occurred"; Rev J says resets are 26/27/41.
    assert {26, 27, 41} <= BENIGN_ERRORS
    assert 1 not in BENIGN_ERRORS
    assert classify_error(10)  # broken tape stays fatal
    assert not classify_error(41)


def test_vendor_id_make_model_split():
    # Make in bits 6-15, model in 0-5: Archive/Conner (5), model 3.
    assert decode_vendor_id((5 << 6) | 3) == (5, 3, "Archive/Conner")
    assert decode_vendor_id(546 << 6)[2] == "Iomega Inc."


def test_colorado_legacy_vendor_id_is_not_mis_split():
    # The bench Colorado Jumbo 350 reports 0x0047; split naively it would read
    # as make 1 "Alloy Computer Products", model 7.
    make, model, name = decode_vendor_id(0x0047)
    assert (make, model) == (71, 0)
    assert "Colorado" in name


@pytest.mark.parametrize(
    "code,text", [(1, "205 ft or 425+ ft, 550 Oe"), (6, "variable length 900 Oe")]
)
def test_tape_types(code, text):
    assert TAPE_TYPES[code] == text
