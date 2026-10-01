"""GW-packet framing, capability gate and verb layouts (DESIGN.md §6A.2, §13.3).

Response payloads here are built from the layouts in firmware/src/qic/qic.c and
firmware/inc/cdc_acm_protocol.h -- deliberately NOT from the host's own structs,
so a host-side layout mistake cannot validate itself.
"""

import struct

import pytest

from tapewyrm.link.device import (
    DeviceLink,
    LinkClosed,
    LinkError,
    LinkVersionError,
    ScopeTrace,
)
from tapewyrm.link.protocol import CAPS, PROTO_VERSION, Txn
from tapewyrm.link.transport import (
    FakeTransport,
    TransportError,
    decode_frame_header,
    encode_frame,
)
from tapewyrm.types import SelectHint, StopCond, TimingParams

# GW-native command numbers / ACKs (firmware/inc/cdc_acm_protocol.h).
GW_GET_INFO, GW_MOTOR, GW_SELECT, GW_DESELECT, GW_SET_BUS_TYPE = 0, 6, 12, 13, 14
ACK_BAD_COMMAND, ACK_BAD_UNIT = 1, 9

# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------


def test_request_frame_is_gw_packet():
    # {cmd, total_len (incl. 2-byte header), payload}
    assert encode_frame(Txn.COMMAND_TXN, b"\x06\x08") == bytes([0x82, 4, 6, 8])


def test_empty_payload_frame():
    assert encode_frame(Txn.INFO) == bytes([0x80, 2])


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(ValueError):
        encode_frame(Txn.INFO, b"\x00" * 254)  # total_len is a u8


def test_response_header_is_echo_and_ack():
    assert decode_frame_header(bytes([0x82, 0])) == (0x82, 0)


def test_mismatched_echo_is_a_desync():
    t = FakeTransport()
    t.open()
    t.queue_response(Txn.INFO, b"")
    with pytest.raises(TransportError):
        t.recv_frame(int(Txn.COMMAND_TXN))


# ---------------------------------------------------------------------------
# open(): GW GET_INFO identity + our INFO capability gate
# ---------------------------------------------------------------------------


def _gw_info(hw_model=4, hw_sub=2, fw=(1, 6), usb_speed=0) -> bytes:
    # struct gw_info, zero-padded to 32 bytes on the wire.
    body = struct.pack(
        "<4BI4B3H", fw[0], fw[1], 1, 22, 72_000_000, hw_model, hw_sub, usb_speed, 2, 216, 224, 128
    )
    return body.ljust(32, b"\x00")


def _qic_info(proto_ver=PROTO_VERSION, caps=("verbs", "capture", "markers")) -> bytes:
    bits = {"verbs": 0, "capture": 1, "markers": 2}
    mask = sum(1 << bits[c] for c in caps)
    return struct.pack("<BIII", proto_ver, mask, 131072, 72_000_000)


def _good_link(**info_kw) -> tuple[DeviceLink, FakeTransport]:
    t = FakeTransport()
    t.queue_response(GW_GET_INFO, _gw_info())
    t.queue_response(Txn.INFO, _qic_info(**info_kw))
    link = DeviceLink(t)
    link.open()
    t.sent_frames.clear()
    return link, t


def test_open_identifies_board_as_tapewyrm_gw():
    link, _t = _good_link()
    assert link.info is not None
    assert link.info.model == "tapewyrm-GW V4.1"  # (4, 2) is the V4.1, per gw info
    assert link.info.firmware == "1.6"
    assert link.info.qic_caps == frozenset({"verbs", "capture", "markers"})
    assert link.info.proto_ver == PROTO_VERSION


def test_open_sends_get_info_firmware_index():
    t = FakeTransport()
    t.queue_response(GW_GET_INFO, _gw_info())
    t.queue_response(Txn.INFO, _qic_info())
    DeviceLink(t).open()
    assert t.sent_frames[0] == (GW_GET_INFO, b"\x00")
    assert t.sent_frames[1] == (int(Txn.INFO), b"")


def test_gate_rejects_stock_firmware():
    # Stock GW answers our INFO verb with BAD_COMMAND (header only, no payload).
    t = FakeTransport()
    t.queue_response(GW_GET_INFO, _gw_info())
    t.queue_response(Txn.INFO, b"", ack=ACK_BAD_COMMAND)
    with pytest.raises(LinkVersionError):
        DeviceLink(t).open()


def test_gate_rejects_old_version():
    with pytest.raises(LinkVersionError):
        _good_link(proto_ver=PROTO_VERSION - 1)


def test_gate_rejects_missing_caps():
    with pytest.raises(LinkVersionError):
        _good_link(caps=("verbs",))


def test_required_caps_subset_of_generated_caps():
    from tapewyrm.link.device import REQUIRED_CAPS

    assert REQUIRED_CAPS <= CAPS


# ---------------------------------------------------------------------------
# COMMAND_TXN: {cmd_n, report_bits} -> {flags, bits:u16, nbits}
# ---------------------------------------------------------------------------


def _cmd_resp(ack: bool, bits: int, final: bool, nbits: int = 8, timeout=False) -> bytes:
    flags = (1 if ack else 0) | (2 if final else 0) | (4 if timeout else 0)
    return struct.pack("<BHB", flags, bits, nbits)


def test_command_txn_request_layout():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0x77, True))
    link.command_txn(6, report_bits=8)
    assert t.sent_frames == [(int(Txn.COMMAND_TXN), bytes([6, 8]))]


def test_command_txn_no_report_returns_empty():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(False, 0, False, nbits=0))
    assert link.command_txn(18) == b""  # no report: ack/final not checked


def test_command_txn_returns_report_bytes():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0x77, True))
    assert link.command_txn(6, report_bits=8) == b"\x77"


def test_command_txn_16_bits():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0x001A, True, nbits=16))
    assert link.command_txn(7, report_bits=16) == b"\x1a\x00"


def test_command_txn_no_ack_raises():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(False, 0, False, nbits=0, timeout=True))
    with pytest.raises(LinkError, match="no ACK"):
        link.command_txn(6, report_bits=8)


def test_command_txn_bad_final_raises():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, _cmd_resp(True, 0x12, False))
    with pytest.raises(LinkError, match="Final"):
        link.command_txn(6, report_bits=8)


def test_command_txn_rejected_ack_raises():
    link, t = _good_link()
    t.queue_response(Txn.COMMAND_TXN, b"", ack=ACK_BAD_COMMAND)
    with pytest.raises(LinkError, match="BAD_COMMAND"):
        link.command_txn(6)


def test_command_txn_validates_ranges():
    link, _t = _good_link()
    with pytest.raises(ValueError):
        link.command_txn(256)
    with pytest.raises(ValueError):
        link.command_txn(6, report_bits=17)


# ---------------------------------------------------------------------------
# SET_TIMING / select / WAIT_READY / SCOPE
# ---------------------------------------------------------------------------


def test_set_timing_binary_layout():
    link, t = _good_link()
    t.queue_response(Txn.SET_TIMING, b"")
    link.set_timing(TimingParams(pulse_us=5, report_on_index=True))
    # pulse, inter_pulse, terminate_gap, tack, tbit (u16) + report_on_index (u8)
    expect = struct.pack("<5HB", 5, 2000, 3000, 2500, 900, 1)
    assert t.sent_frames == [(int(Txn.SET_TIMING), expect)]


def test_select_uses_gw_native_commands():
    link, t = _good_link()
    for cmd in (GW_SET_BUS_TYPE, GW_SELECT, GW_MOTOR):
        t.queue_response(cmd, b"")
    link.select(SelectHint(bus="ibmpc", unit=1, motor=True))
    assert t.sent_frames == [
        (GW_SET_BUS_TYPE, b"\x01"),
        (GW_SELECT, b"\x01"),
        (GW_MOTOR, b"\x01\x01"),
    ]


def test_select_bad_unit_raises():
    link, t = _good_link()
    t.queue_response(GW_SET_BUS_TYPE, b"")
    t.queue_response(GW_SELECT, b"", ack=ACK_BAD_UNIT)
    with pytest.raises(LinkError, match="BAD_UNIT"):
        link.select(SelectHint(bus="shugart", unit=3))


def test_select_rejects_unknown_bus():
    link, _t = _good_link()
    with pytest.raises(ValueError):
        link.select(SelectHint(bus="apple2"))


def test_wait_ready_zero_means_ready():
    # Firmware: status 0 = ready, 1 = timed out (NOT a ready bit).
    link, t = _good_link()
    t.queue_response(Txn.WAIT_READY, b"\x00")
    assert link.wait_ready(15000) is True
    t.queue_response(Txn.WAIT_READY, b"\x01")
    assert link.wait_ready(15000) is False
    assert t.sent_frames[0] == (int(Txn.WAIT_READY), struct.pack("<H", 15))


def test_scope_parses_edge_log():
    link, t = _good_link()
    head = struct.pack("<3B4H", 0, 2, 0, 1, 1, 0, 0)  # initial, n_edges, overflow, counts
    edges = struct.pack("<IB", 2987, 0b01) + struct.pack("<IB", 3158, 0b11)
    t.queue_response(Txn.SCOPE, head + edges)
    tr = link.scope(6, 20)
    assert t.sent_frames == [(int(Txn.SCOPE), struct.pack("<BH", 6, 20))]
    assert tr.edges == ((2987, 1), (3158, 3))
    assert tr.counts == {"TRK0": 1, "INDEX": 1, "WRPROT": 0, "PIN34": 0}
    assert not tr.overflow
    assert ScopeTrace.describe(3) == "TRK0+INDEX"


# ---------------------------------------------------------------------------
# CAPTURE
# ---------------------------------------------------------------------------


def test_capture_sends_binary_request_then_streams():
    link, t = _good_link()
    t.queue_response(Txn.CAPTURE, b"")
    t.queue_stream(b"\x01\x02", b"\x03")
    chunks = []
    with link.capture(10, StopCond(byte_budget=100), pass_id=7) as cap:
        chunks.extend(cap.chunks())
    assert b"".join(chunks) == b"\x01\x02\x03"
    assert t.sent_frames[-1] == (
        int(Txn.CAPTURE),
        struct.pack("<BHHBHI", 10, 0, 0, 0, 7, 100),
    )


def test_capture_abort_sends_control():
    link, t = _good_link()
    t.queue_response(Txn.CAPTURE, b"")
    t.queue_stream(b"\xaa")
    cap = link.capture(10, StopCond())
    cap.abort()
    assert t.sent_control[-1][0] == int(Txn.ABORT)
    cap.__exit__(None, None, None)


def test_closed_link_raises():
    link = DeviceLink(FakeTransport())
    with pytest.raises(LinkClosed):
        link.command_txn(6, report_bits=8)
