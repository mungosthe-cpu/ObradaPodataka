"""Testovi za BRICON Print Manager protokol (konstante iz BriconLib.dll)."""

from __future__ import annotations

from tauris_desktop import bricon_pm as pm


def test_send_version_info_packet_length() -> None:
    assert len(pm.PM_SEND_VERSION_INFO) == 22
    assert pm.PM_SEND_VERSION_INFO[0:3] == bytes([0x10, 0x70, 0x13])


def test_detect_baud_switch_status() -> None:
    assert pm._detect_baud_switch_status(bytes([0, pm.RS_SWITCH_TO_9600, 0])) == 9600
    assert pm._detect_baud_switch_status(bytes([pm.RS_SWITCH_TO_38400])) == 38400
    assert pm._detect_baud_switch_status(b"") is None


def test_pm_command_frames() -> None:
    assert pm.PM_CMD_START_READOUT == bytes([0x10, 0xE1, 0x00])
    assert pm.PM_CMD_GET_FANCIER_COUNT == bytes([0xFF, 0xFA, 0x02, 0x0A, 0x00])
    assert pm.PM_CMD_GET_FLIGHT_NAMES == bytes([0xFF, 0xFA, 0x02, 0x0C, 0x00])
