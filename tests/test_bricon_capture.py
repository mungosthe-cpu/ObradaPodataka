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


def test_analyze_detects_pm_handshake(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tauris_desktop.bricon_capture.bricon_capture_dir",
        lambda: tmp_path,
    )
    cap = BriconComCapture("COM3")
    cap.log_tx(PM_SEND_VERSION_INFO, 38400)
    cap.log_rx(bytes([0, RS_SWITCH_TO_9600]), 38400)
    log_path = cap.close()
    report = analyze_capture_file(log_path)
    assert "SendVersionInfo" in report
    assert "9600" in report
    assert "[OK]" in report
