"""
Testovi sa mock serijskim uređajem za TAURIS čitanje preko COM porta.

Cilj: dokazati (ne samo da se kod kompajlira) da:
- uređaj se pronalazi i čita kad je sve u redu, i da jasna greška stiže kad nije
  (port ne postoji, pogrešan port, port zauzet, nema podataka/timeout),
- prolazna greška pri otvaranju porta se ponovi jednom i uspe (bez beskonačnog čekanja),
- odgovor koji stiže u više delova (podeljen na mnogo malih read() poziva) se ispravno
  sastavi u celinu,
- nepotpun paket (nedostaje red na kraju liste) i nevažeći/garbage sadržaj ne obaraju
  program i ne gube već primljene bajtove,
- prekid USB veze usred prenosa ne guta izuzetak tiho i ne gubi već primljene bajtove,
- veliki broj rezultata i neispravna stavka između ispravnih ne obaraju parsiranje,
- DTR/RTS priprema porta je ista kao BRICON usb_passive režim (deljena funkcija),
- otkazivanje od strane korisnika je brzo i baca jasnu poruku,
- već pročitani TAURIS podaci se snimaju za oporavak ako kasniji korak (PDF/email) padne.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import tauris_desktop.lib as tdl
import tauris_desktop.tauris_capture as tcap
from tauris_desktop.config import TaurisConnectionConfig


SAMPLE_TAURIS_TEXT = (
    "TauRIS kontrol lista\n"
    "001.001.049.066  Dragan Todorovic      0003 0001-0003\n"
    "0001 SRB 12:34:56\n"
    "0002 SRB 12:35:10\n"
    "0003 SRB 12:36:02\n"
)


class FakeTaurisSerial:
    """Zamena za pyserial.Serial za jedan otvoreni TAURIS port u testu."""

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        chunks: list[bytes] | None = None,
        read_exc: BaseException | None = None,
        read_exc_after: int = 0,
        timeout: float | None = None,
        **_kwargs: Any,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._chunks = list(chunks or [])
        self._read_exc = read_exc
        self._read_exc_after = read_exc_after
        self._reads_done = 0
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.reset_input_calls = 0
        self.reset_output_calls = 0
        self.closed = False

    def setDTR(self, value: bool) -> None:
        self.dtr = value

    def setRTS(self, value: bool) -> None:
        self.rts = value

    def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1

    def reset_output_buffer(self) -> None:
        self.reset_output_calls += 1

    def read(self, size: int = 1) -> bytes:
        self._reads_done += 1
        if self._read_exc is not None and self._reads_done > self._read_exc_after:
            raise self._read_exc
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True


class FakeTaurisSerialModule:
    """Zamena za ceo ``serial`` modul — samo ono što ``read_race_from_device`` koristi.

    ``open_results`` je lista: svaki poziv Serial(...) troši sledeću stavku (izuzetak ->
    otvaranje puca; dict -> spec za FakeTaurisSerial). Kad se lista potroši, svaki sledeći
    poziv dobija prazan (bez podataka) fake serial — dovoljno za testove kojima ne treba
    ponovni pokušaj otvaranja.
    """

    def __init__(self, open_results: list[Any] | None = None) -> None:
        self._open_results = list(open_results or [])
        self.calls: list[int] = []
        self.SerialException = OSError

    def Serial(self, *, port: str, baudrate: int, **kwargs: Any) -> FakeTaurisSerial:
        self.calls.append(baudrate)
        if self._open_results:
            result = self._open_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            spec = result or {}
            return FakeTaurisSerial(port=port, baudrate=baudrate, **spec)
        return FakeTaurisSerial(port=port, baudrate=baudrate)


def _install_fake_serial(
    monkeypatch: pytest.MonkeyPatch,
    open_results: list[Any] | None = None,
    *,
    ports_available: list[str] | None = None,
) -> FakeTaurisSerialModule:
    fake_module = FakeTaurisSerialModule(open_results)
    monkeypatch.setattr(tdl, "serial", fake_module)
    if ports_available is None:
        monkeypatch.setattr(tdl, "verify_com_port_available", lambda port: None)
    else:
        monkeypatch.setattr(tdl, "list_live_com_port_names", lambda: list(ports_available))
        monkeypatch.setattr(tdl, "com_port_usability_issue", lambda port: None)
    # Brzi testovi: mali otvor-retry razmak umesto pravih 0.35s.
    monkeypatch.setattr(tdl, "TAURIS_OPEN_RETRY_DELAY_SEC", 0.01)
    return fake_module


def _fast_config(port: str = "COM7", baudrate: int = 9600) -> TaurisConnectionConfig:
    # Mali broj iteracija/idle-citanja da testovi ne cekaju pravih ~16s po pokusaju.
    return TaurisConnectionConfig(
        port=port,
        baudrate=baudrate,
        read_timeout=0.01,
        read_iterations=20,
        read_idle_empty_reads=3,
        tail_idle_empty_reads=3,
        tail_extra_max_iterations=10,
    )


# ---------------------------------------------------------------------------
# 1) Uređaj pronađen i uspešno pročitan — srećan put.
# ---------------------------------------------------------------------------
def test_tauris_device_found_and_read_successfully(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]}])
    data = tdl.read_race_from_device(_fast_config())
    assert "Dragan Todorovic" in data.get("takmicar", "")
    assert "0002 SRB" in data["raw_com_text"]
    assert "0003 SRB" in data["raw_com_text"]


# ---------------------------------------------------------------------------
# 2) Uređaj nije pronađen — port ne postoji u Windows-u.
# ---------------------------------------------------------------------------
def test_tauris_device_not_found_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, ports_available=["COM3"])
    with pytest.raises(RuntimeError, match="ne postoji"):
        tdl.read_race_from_device(_fast_config(port="COM9"))


# ---------------------------------------------------------------------------
# 3) Pogrešan COM port — otvaranje puca sa "could not open port", bez pokusaja ponavljanja.
# ---------------------------------------------------------------------------
def test_tauris_wrong_com_port_raises_clear_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_serial(
        monkeypatch, [OSError("could not open port 'COM7': FileNotFoundError")]
    )
    with pytest.raises(RuntimeError, match="nije dostupan"):
        tdl.read_race_from_device(_fast_config())
    assert len(fake.calls) == 1, "pogrešan port se ne pokušava ponovo otvarati"


# ---------------------------------------------------------------------------
# 4) Port je zauzet — PermissionError na svaki pokušaj, jasna poruka posle poslednjeg.
# ---------------------------------------------------------------------------
def test_tauris_port_busy_raises_clear_error_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_serial(
        monkeypatch,
        [
            PermissionError("Access is denied."),
            PermissionError("Access is denied."),
        ],
    )
    with pytest.raises(RuntimeError, match="[Gg]reška pri otvaranju"):
        tdl.read_race_from_device(_fast_config())
    assert len(fake.calls) == 2, "prolazna greška se pokušava tačno TAURIS_OPEN_RETRY_ATTEMPTS puta"


# ---------------------------------------------------------------------------
# 5) TAURIS nema handshake — ako se poveže ali uređaj ništa ne pošalje, jasna poruka.
# ---------------------------------------------------------------------------
def test_tauris_no_handshake_no_data_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": []}])
    with pytest.raises(RuntimeError, match="Nema podataka sa TAURIS"):
        tdl.read_race_from_device(_fast_config())


# ---------------------------------------------------------------------------
# 6) Prvi pokušaj otvaranja ne uspe (prolazna greška), ponovni uspe.
# ---------------------------------------------------------------------------
def test_tauris_first_open_attempt_fails_retry_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_serial(
        monkeypatch,
        [
            PermissionError("Access is denied."),
            {"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]},
        ],
    )
    data = tdl.read_race_from_device(_fast_config())
    assert len(fake.calls) == 2
    assert "Dragan Todorovic" in data.get("takmicar", "")


# ---------------------------------------------------------------------------
# 7) Odgovor stiže u više malih delova — mora se ispravno sastaviti.
# ---------------------------------------------------------------------------
def test_tauris_response_arrives_in_many_small_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = SAMPLE_TAURIS_TEXT.encode("utf-8")
    chunks = [raw[i : i + 3] for i in range(0, len(raw), 3)]  # namerno sitni delovi
    _install_fake_serial(monkeypatch, [{"chunks": chunks}])
    # dovoljno iteracija da se poberu svi sitni delovi (43) + idle-cekanje na kraju
    cfg = TaurisConnectionConfig(
        port="COM7", baudrate=9600, read_timeout=0.005,
        read_iterations=200, read_idle_empty_reads=3,
        tail_idle_empty_reads=3, tail_extra_max_iterations=20,
    )
    data = tdl.read_race_from_device(cfg)
    # parse_tauris_payload_to_race_dict radi .strip() na celom tekstu — poredi sa strip-ovanim.
    assert data["raw_com_text"] == SAMPLE_TAURIS_TEXT.strip()
    assert "0001 SRB" in data["raw_com_text"]
    assert "0003 SRB" in data["raw_com_text"]


# ---------------------------------------------------------------------------
# 8) Nepotpun paket — nedostaje poslednji red, "tail" dopunsko čitanje ne dobija ništa
#    novo pa se korektno odustaje umesto večitog čekanja, i dijagnostika se snima.
# ---------------------------------------------------------------------------
def test_tauris_rezult_list_without_last_lag_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Rezult.list ne mora da sadrži sve ukorpane golubove — izostanak reda „0003 SRB“ je kraj
    čitanja (INFO), ne upozorenje i ne „parcijalni“ dijagnostički fajl (ranije je SVAKO terensko
    čitanje završavalo lažnim upozorenjem).
    """
    monkeypatch.setattr(tcap, "tauris_capture_dir", lambda: tmp_path)
    incomplete = (
        "TauRIS kontrol lista\n"
        "001.001.049.066  Dragan Todorovic      0003 0001-0003\n"
        "0001 SRB 12:34:56\n"
        "0002 SRB 12:35:10\n"
        # nedostaje 0003 SRB
    ).encode("utf-8")
    _install_fake_serial(monkeypatch, [{"chunks": [incomplete]}])
    with caplog.at_level("INFO"):
        data = tdl.read_race_from_device(_fast_config())
    assert "0002 SRB" in data["raw_com_text"]
    assert "0003 SRB" not in data["raw_com_text"]
    assert not any(r.levelname == "WARNING" and "nema reda" in r.getMessage() for r in caplog.records)
    assert any("kraj prenosa" in r.getMessage() for r in caplog.records)
    assert not list(tmp_path.glob("tauris_partial_*.log")), "kompletan čitljiv prenos ne pravi parcijalni fajl"


def test_tauris_indented_rows_satisfy_lag_check() -> None:
    """Stvarni ispis sata uvlači redove razmacima — provera poslednjeg Lag-a mora to da toleriše."""
    text = "     0039 SRB x\n     0040 SRB y\n"
    assert tdl.tauris_text_has_lag_row(text, 40)
    assert tdl.tauris_text_has_lag_row("0040 SRB", 40)
    assert not tdl.tauris_text_has_lag_row(text, 41)


def test_tauris_corrupted_transfer_is_rejected_with_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Oštećen prenos (kao TXT od 23.08.2026: crtice -> 0xD6, razmaci -> 0x02/0x06) se ne čuva kao rezultat."""
    monkeypatch.setattr(tcap, "tauris_capture_dir", lambda: tmp_path)
    corrupted = (
        b"\x02\x02\x02\x02\xfe\xff\xffok" + b"\xd6" * 70 + b"\n"
        b"H\x06\x02\x06\x06\x02\x02\x02\x02\x02\x06\x06\x06F-\xabRIS  Rez\xdd\x1b7\x19[\xdb\xa3\x03\x06\x02"
        b"\x92M\xe5.00/1            23.08.26-19:28:52\n"
        b"\xc8\x02\x06\x02" + b"\xd6" * 70 + b"\n"
        b"\x06\x02\x06\x06\x02\xa6\xf5ran Ziramov         0077 0001-0077\n"
    )
    _install_fake_serial(monkeypatch, [{"chunks": [corrupted]}])
    with pytest.raises(RuntimeError, match="OŠTEĆEN"):
        tdl.read_race_from_device(_fast_config())
    saved = list(tmp_path.glob("tauris_partial_*.log"))
    assert saved, "oštećen prenos mora ostaviti sirove bajtove za dijagnostiku"
    body = saved[0].read_text(encoding="utf-8")
    assert "ceo payload" in body and "0001-0077" in body


def test_tauris_quality_gate_accepts_serbian_letters() -> None:
    good = "TauRIS  Rezult.list\n     001.001.008.081  ŠĆEPANOVIĆ ĐORĐE   0029 0001-0029\n".encode("cp1250")
    ok, _ = tdl.assess_tauris_transfer_quality(good)
    assert ok


# ---------------------------------------------------------------------------
# 9) Nevažeći/garbage sadržaj — ne obara program, čuva se kao raw_com_text.
# ---------------------------------------------------------------------------
def test_tauris_invalid_garbage_payload_is_rejected_as_corrupted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tcap, "tauris_capture_dir", lambda: tmp_path)
    garbage = bytes(range(256)) * 2
    _install_fake_serial(monkeypatch, [{"chunks": [garbage]}])
    with pytest.raises(RuntimeError, match="OŠTEĆEN"):
        tdl.read_race_from_device(_fast_config())


# ---------------------------------------------------------------------------
# 10) Timeout — nema nikakvih podataka.
# ---------------------------------------------------------------------------
def test_tauris_timeout_no_data_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": []}])
    with pytest.raises(RuntimeError, match="Nema podataka sa TAURIS"):
        tdl.read_race_from_device(_fast_config())


# ---------------------------------------------------------------------------
# 11) USB prekid usred prenosa — izuzetak se ne guta tiho, već primljeni podaci ostaju.
# ---------------------------------------------------------------------------
def test_tauris_usb_disconnect_mid_transfer_preserves_partial_data(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    partial = b"TauRIS kontrol lista\n001.001.049.066  Nepotpuno   0003 0001-0003\n0001 SRB 1\n"
    _install_fake_serial(
        monkeypatch,
        [
            {
                "chunks": [partial],
                "read_exc": OSError("ClearCommError failed — device disconnected"),
                "read_exc_after": 1,
            }
        ],
    )
    with caplog.at_level("WARNING"):
        data = tdl.read_race_from_device(_fast_config())
    assert "0001 SRB" in data["raw_com_text"]
    disconnect_logs = [
        r for r in caplog.records if "prekinuto" in r.getMessage().lower() or "read_error" in r.getMessage()
    ]
    assert disconnect_logs, "prekid konekcije mora biti ULOGOVAN, ne tiho progutan"


def test_tauris_usb_disconnect_before_any_data_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_serial(
        monkeypatch,
        [{"chunks": [], "read_exc": OSError("device disconnected"), "read_exc_after": 0}],
    )
    with pytest.raises(RuntimeError, match="prekinuta"):
        tdl.read_race_from_device(_fast_config())


# ---------------------------------------------------------------------------
# 12) Veliki broj rezultata — sve mora stići, bez gubitka i bez pada.
# ---------------------------------------------------------------------------
def test_tauris_large_result_count_all_rows_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    n = 500
    header = f"TauRIS kontrol lista\n001.001.049.066  Mnogo Golubova      {n:04d} 0001-{n:04d}\n"
    rows = "".join(f"{i:04d} SRB 12:{i % 60:02d}:00\n" for i in range(1, n + 1))
    full_text = header + rows
    raw = full_text.encode("utf-8")
    chunk_size = 97  # namerno neravno da isprovocira mnogo malih read() poziva
    chunks = [raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)]
    cfg = _fast_config()
    cfg = TaurisConnectionConfig(
        port=cfg.port,
        baudrate=cfg.baudrate,
        read_timeout=0.005,
        read_iterations=2000,
        read_idle_empty_reads=3,
        tail_idle_empty_reads=3,
        tail_extra_max_iterations=50,
    )
    _install_fake_serial(monkeypatch, [{"chunks": chunks}])
    data = tdl.read_race_from_device(cfg)
    # parse_tauris_payload_to_race_dict radi .strip() na celom tekstu — poredi sa strip-ovanim.
    assert data["raw_com_text"] == full_text.strip()
    assert f"{n:04d} SRB" in data["raw_com_text"]
    assert data["raw_com_text"].count(" SRB ") == n


# ---------------------------------------------------------------------------
# 13) Neispravna stavka između ispravnih — ne sme da obori ni izgubi ostale.
# ---------------------------------------------------------------------------
def test_tauris_invalid_row_between_valid_rows_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        "TauRIS kontrol lista\n"
        "001.001.049.066  Test Takmicar      0003 0001-0003\n"
        "0001 SRB 12:00:00\n"
        "###OSTECEN_RED###\n"
        "0003 SRB 12:05:00\n"
    )
    _install_fake_serial(monkeypatch, [{"chunks": [text.encode("utf-8")]}])
    data = tdl.read_race_from_device(_fast_config())
    assert "0001 SRB" in data["raw_com_text"]
    assert "0003 SRB" in data["raw_com_text"]
    assert "###OSTECEN_RED###" in data["raw_com_text"]


# ---------------------------------------------------------------------------
# 14) DTR/RTS — TAURIS koristi istu pasivnu pripremu kao BRICON usb_passive.
# ---------------------------------------------------------------------------
def test_tauris_dtr_rts_matches_passive_listen_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]}])
    captured: dict[str, FakeTaurisSerial] = {}
    orig_prepare = tdl.prepare_serial_port_for_passive_listen

    def spy(ser: Any, **kwargs: Any) -> None:
        captured["ser"] = ser
        orig_prepare(ser, **kwargs)

    monkeypatch.setattr(tdl, "prepare_serial_port_for_passive_listen", spy)
    tdl.read_race_from_device(_fast_config())
    ser = captured["ser"]
    assert ser.dtr is True
    assert ser.rts is False
    assert ser.reset_input_calls == 1
    assert ser.reset_output_calls == 0


# ---------------------------------------------------------------------------
# 15) Otkazivanje od strane korisnika — brzo, jasna poruka.
# ---------------------------------------------------------------------------
def test_tauris_should_cancel_stops_quickly_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]}])
    t0 = time.monotonic()
    with pytest.raises(tdl.TaurisCancelled, match="Otkazano"):
        tdl.read_race_from_device(_fast_config(), should_cancel=lambda: True)
    assert time.monotonic() - t0 < 1.0, "otkazivanje mora biti brzo, ne cekati pun ciklus citanja"


def test_tauris_on_phase_callback_reports_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]}])
    phases: list[str] = []
    tdl.read_race_from_device(_fast_config(), on_phase=phases.append)
    joined = " | ".join(phases)
    assert "traženje" in joined or "trazenje" in joined
    assert "povezivanje" in joined
    assert "parsiranje" in joined
    assert "čuvanje" in joined or "cuvanje" in joined


# ---------------------------------------------------------------------------
# 16) Log mora prikazati fazu, port, baud i rezultat parsiranja.
# ---------------------------------------------------------------------------
def test_tauris_diagnostic_log_shows_port_baud_and_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fake_serial(monkeypatch, [{"chunks": [SAMPLE_TAURIS_TEXT.encode("utf-8")]}])
    with caplog.at_level("INFO"):
        tdl.read_race_from_device(_fast_config())
    diag_records = [r.getMessage() for r in caplog.records if "TAURIS diag" in r.getMessage()]
    assert any("port=COM7" in m for m in diag_records)
    assert any("baud=9600" in m for m in diag_records)
    assert any("phase=open_ok" in m for m in diag_records)
    assert any("phase=done" in m and "parsiranje=ok" in m for m in diag_records)


# ---------------------------------------------------------------------------
# 17) Rezultati ostaju sačuvani ako kasniji PDF/zaključavanje/email korak ne uspe.
# ---------------------------------------------------------------------------
def test_tauris_race_data_recovery_snapshot_survives_pipeline_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tcap, "tauris_capture_dir", lambda: tmp_path)
    race_data = {
        "race_id": "TAURIS-TEST-1",
        "takmicar": "Pera Perić",
        "raw_com_text": SAMPLE_TAURIS_TEXT,
    }
    path = tcap.save_tauris_race_data_recovery_snapshot(
        race_data, port="COM7", reason="RuntimeError: PDF generisanje nije uspelo"
    )
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["race_data"] == race_data
    assert saved["_recovery_port"] == "COM7"
    assert "PDF" in saved["_recovery_reason"]


# ---------------------------------------------------------------------------
# 18) gui.py wiring: TAURIS greška posle uspešnog čitanja mora koristi TAURIS (ne BRICON)
#     recovery snapshot funkciju — testira stvarni kod u gui.py._worker_live_download,
#     ne kopiju logike, tako da refaktor koji pokvari granu odmah pukne ovde.
# ---------------------------------------------------------------------------
def test_gui_worker_uses_tauris_recovery_snapshot_on_pipeline_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("nema dostupnog Tk display-a u ovom okruženju")
    root.withdraw()
    try:
        import tauris_desktop.gui as gui_mod

        monkeypatch.setattr(tcap, "tauris_capture_dir", lambda: tmp_path)

        app = gui_mod.TaurisApp.__new__(gui_mod.TaurisApp)
        app.root = root
        app.worker_queue = queue.Queue()
        app.slow_tauris_var = type("V", (), {"get": lambda self: False})()
        app._cancel_download = threading.Event()
        app.delivery_config = None

        race_data = {"race_id": "TAURIS-GUI-TEST", "raw_com_text": SAMPLE_TAURIS_TEXT}

        def fake_read_race_from_clock(config, device_kind, **kwargs):
            return race_data

        def fake_run_pipeline(*args, **kwargs):
            raise RuntimeError("PDF generisanje nije uspelo (test)")

        monkeypatch.setattr(gui_mod, "read_race_from_clock", fake_read_race_from_clock)
        monkeypatch.setattr(gui_mod, "apply_staza_ui_to_race_data", lambda rd, **kw: rd)
        monkeypatch.setattr(gui_mod, "resolve_pilot_name_for_output", lambda rd, name: name)
        monkeypatch.setattr(gui_mod, "run_pipeline", fake_run_pipeline)

        from tauris_desktop.config import CLOCK_DEVICE_TAURIS

        app._worker_live_download(
            _fast_config(), "Test Pilot", ("Grupacija 1", 1, ""), "lozinka", CLOCK_DEVICE_TAURIS
        )

        event_type, payload = app.worker_queue.get_nowait()
        assert event_type == "error"
        assert "TAURIS podaci su uspešno pročitani i sačuvani" in str(payload)
        saved = list(tmp_path.glob("tauris_racedata_recovery_*.json"))
        assert saved, "gui.py mora pozvati TAURIS (ne BRICON) recovery snapshot za CLOCK_DEVICE_TAURIS"
    finally:
        root.destroy()
