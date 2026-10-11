"""Red neposlatih paketa — offline teren, naknadno slanje."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tauris_desktop import pending_sends as ps
from tauris_desktop.config import DeliveryConfig


def _cfg(**over) -> DeliveryConfig:
    base = dict(
        mode="email", signing_key="k", timeout_sec=5, smtp_host="smtp.x", smtp_port=587,
        smtp_user="u", smtp_password="p", mail_from="a@x", mail_to="b@x",
    )
    base.update(over)
    return DeliveryConfig(**base)


@pytest.fixture(autouse=True)
def _isolated_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    q = tmp_path / "pending_sends.json"
    monkeypatch.setattr(ps, "pending_sends_path", lambda: q)
    return q


def test_enqueue_dedupes_and_counts(tmp_path: Path) -> None:
    b = tmp_path / "005" / "x.taurislock"
    b.parent.mkdir()
    b.write_bytes(b"lock")
    assert ps.enqueue_pending_send(b, broj_trke="005", grupacija="Grupacija 13", error="DNS") == 1
    assert ps.enqueue_pending_send(b, error="DNS again") == 1, "isti paket se ne duplira"
    items = ps.load_pending_sends()
    assert items[0].attempts == 2 and items[0].broj_trke == "005"
    assert "Neposlatih paketa: 1" in ps.summarize_pending_for_user()


def test_retry_sends_and_removes(tmp_path: Path) -> None:
    b1 = tmp_path / "a.taurislock"; b1.write_bytes(b"1")
    b2 = tmp_path / "b.taurislock"; b2.write_bytes(b"2")
    gone = tmp_path / "gone.taurislock"
    for b in (b1, b2, gone):
        ps.enqueue_pending_send(b)
    sent_paths: list[str] = []

    def fake_sender(item: ps.PendingSend, cfg: DeliveryConfig) -> str:
        sent_paths.append(item.path.name)
        return "ok"

    sent, remaining, notes = ps.retry_pending_sends(_cfg(), sender=fake_sender)
    assert sent == 2 and remaining == 0
    assert sorted(sent_paths) == ["a.taurislock", "b.taurislock"]
    assert any("fajl ne postoji" in n for n in notes)
    assert ps.load_pending_sends() == []


def test_retry_stops_on_network_error_and_keeps_queue(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        p = tmp_path / f"{name}.taurislock"
        p.write_bytes(b"x")
        ps.enqueue_pending_send(p)
    calls = []

    def offline(item: ps.PendingSend, cfg: DeliveryConfig) -> str:
        calls.append(item.path.name)
        raise socket.gaierror(11001, "getaddrinfo failed")

    sent, remaining, notes = ps.retry_pending_sends(_cfg(), sender=offline)
    assert sent == 0 and remaining == 3
    assert len(calls) == 1, "posle prve mrežne greške ne proba ostale"
    assert any("Nema internet veze" in n for n in notes)
    assert ps.load_pending_sends()[0].attempts == 2


def test_retry_without_smtp_settings_keeps_everything(tmp_path: Path) -> None:
    p = tmp_path / "a.taurislock"; p.write_bytes(b"x")
    ps.enqueue_pending_send(p)
    sent, remaining, notes = ps.retry_pending_sends(_cfg(smtp_password=""))
    assert (sent, remaining) == (0, 1)
    assert "Podešavanja" in notes[0]


def test_run_pipeline_offline_enqueues_without_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import tauris_desktop.lib as tdl

    out_dir = tmp_path / "005-Grupacija_13"
    out_dir.mkdir()
    monkeypatch.setattr(tdl, "resolve_output_directory", lambda rd: out_dir)
    monkeypatch.setattr(tdl, "lock_results_folder", lambda p: None)

    def boom(*a, **k):
        raise OSError(11001, "getaddrinfo failed")

    monkeypatch.setattr(tdl, "_smtp_send_email_message", boom)
    race = {"race_id": "T-1", "start_time": "2026-01-01T10:00:00", "duration_sec": 0,
            "raw_com_text": "TauRIS kontrol lista\n     0001 SRB 12:00:00\n", "takmicar": "Test Takmicar"}
    with caplog.at_level("WARNING"):
        bundle, note, _pdf = tdl.run_pipeline(race, "Test Takmicar", "COM9", _cfg(), lock_password="pw")
    assert bundle.is_file()
    assert "red za slanje" in note
    assert not any(r.levelname == "ERROR" for r in caplog.records), "offline nije ERROR sa traceback-om"
    items = ps.load_pending_sends()
    assert len(items) == 1 and Path(items[0].bundle_path) == bundle.resolve()
    assert items[0].broj_trke == "005" and items[0].grupacija == "Grupacija_13"
