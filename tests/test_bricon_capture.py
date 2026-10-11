"""Testovi za BRICON COM snimak."""

from __future__ import annotations

from pathlib import Path

from tauris_desktop.bricon_capture import BriconComCapture, analyze_capture_file
from tauris_desktop.bricon_pm import PM_SEND_VERSION_INFO, RS_SWITCH_TO_9600


def test_capture_logs_tx_rx(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tauris_desktop.bricon_capture.bricon_capture_dir",
        lambda: tmp_path,
    )
    cap = BriconComCapture("COM9", note="test")
    cap.log_tx(PM_SEND_VERSION_INFO, 38400)
    cap.log_rx(bytes([RS_SWITCH_TO_9600, 0, 1, 2]), 38400)
    cap.close(summary="test done")
    text = cap.path.read_text(encoding="utf-8")
    assert "TX 22 B" in text
    assert "RX 4 B" in text
    assert "10 70 13" in text


def test_analyze_detects_pm_frames(tmp_path: Path, monkeypatch) -> None:
    from tauris_desktop.bricon_pm import (
        PM_CMD_ACTIVATE_MASTER,
        cmd_activate_ba,
        pm_build_response_frame,
        pm_encode_frame,
    )

    monkeypatch.setattr(
        "tauris_desktop.bricon_capture.bricon_capture_dir",
        lambda: tmp_path,
    )
    cap = BriconComCapture("COM3")
    cap.log_tx(pm_encode_frame(PM_CMD_ACTIVATE_MASTER), 9600)
    cap.log_tx(pm_encode_frame(cmd_activate_ba(0x82)), 9600)
    cap.log_rx(pm_build_response_frame(0x82, 0xC0, 0, b"SPD 2.07"), 9600)
    log_path = cap.close()
    report = analyze_capture_file(log_path)
    assert "0x6C" in report
    assert "ActivateMaster" in report
    assert "ActivateBA" in report
    assert "0x33" in report
    assert "[OK]" in report


def test_analyze_flags_silent_clock(tmp_path: Path, monkeypatch) -> None:
    from tauris_desktop.bricon_pm import PM_CMD_ACTIVATE_MASTER, pm_encode_frame

    monkeypatch.setattr(
        "tauris_desktop.bricon_capture.bricon_capture_dir",
        lambda: tmp_path,
    )
    cap = BriconComCapture("COM3")
    cap.log_tx(pm_encode_frame(PM_CMD_ACTIVATE_MASTER), 9600)
    report = analyze_capture_file(cap.close())
    assert "RX=0" in report
