"""QIC-117 report decoders (tapewyrm_archive.qic117): vendor IDs and tape types."""

import pytest

from tapewyrm_archive.qic117 import TAPE_TYPES, decode_vendor_id


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
