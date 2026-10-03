"""Rev J error names, reset classification, vendor ID and tape-type decoding."""

from tapewyrm.qic117.status import BENIGN_ERRORS, classify_error, error_name


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
