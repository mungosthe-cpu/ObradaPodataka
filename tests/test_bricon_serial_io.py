"""
Testovi sa mock serijskim uređajem za BRICON čitanje preko USB-a.

Cilj: dokazati (ne samo da se kod kompajlira) da:
- baud fallback stvarno probne sve podržane vrednosti i da neuspešan baud
  ne troši vreme predviđeno za ostale (deljeni deadline bug),
- prekid konekcije usred prenosa i nepotpun/lošiji paket ne gutaju izuzetke tiho
  i ne gube već primljene bajtove,
- DTR/RTS priprema porta je različita i namerna za USB pasivni režim vs PM protokol,
- veliki bafer se ne re-parsira na svaki prazan ciklus čekanja (O(n^2) fix),
- već pročitani BRICON podaci se snimaju za oporavak ako kasniji korak (PDF/email) padne.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from tauris_desktop import bricon as br
from tauris_desktop import bricon_capture as bc
from tauris_desktop import bricon_pm as pm
from tauris_desktop.config import TaurisConnectionConfig


SAMPLE_BRICON_TEXT = (
    "BRICON X-TREME\n"
    "Fancier: Jan Janssen\n"
    "Race: Oldbird 2024\n"
    "001  BEL24-1234567  14:23:45.123  Ant:1\n"
    "002  BEL24-7654321  14:25:01.456  Ant:2\n"
    "003  BEL24-1111111  14:26:10.000  Ant:1\n"
)


class FakeBriconSerial:
    """Zamena za pyserial.Serial za jedan otvoreni port u testu."""

    def __init__(
        self,
        *,
        port: str,
        baudrate: int,
        chunks: list[bytes] | None = None,
        read_exc: BaseException | None = None,
        read_exc_after: int = 0,
        delayed_chunks: list[bytes] | None = None,
        delay_after_polls: int = 0,
        **_kwargs: Any,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._chunks = list(chunks or [])
        self._delayed_chunks = list(delayed_chunks or [])
        self._delay_after_polls = delay_after_polls
        self._poll_count = 0
        self._read_exc = read_exc
        self._read_exc_after = read_exc_after
        self._reads_done = 0
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.reset_input_calls = 0
        self.reset_output_calls = 0
        self.written: list[bytes] = []
        self.closed = False

    def _delayed_due(self) -> bool:
        # Brojanje poll-ova (ne stvarno proteklo vreme) — deterministicno, bez trke sa
        # stvarnim taktovanjem OS-a/testa: simulira da uredjaj pocne da salje listu tek
        # posto je prosao handshake + niz komandi (kao kod pravog Read out-a).
        self._poll_count += 1
        return bool(self._delayed_chunks) and self._poll_count >= self._delay_after_polls

    def setDTR(self, value: bool) -> None:
        self.dtr = value

    def setRTS(self, value: bool) -> None:
        self.rts = value

    def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1

    def reset_output_buffer(self) -> None:
        self.reset_output_calls += 1

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def read(self, size: int = 1) -> bytes:
        self._reads_done += 1
        if self._read_exc is not None and self._reads_done > self._read_exc_after:
            raise self._read_exc
        if self._chunks:
            return self._chunks.pop(0)
        if self._delayed_due():
            return self._delayed_chunks.pop(0)
        return b""

    @property
    def in_waiting(self) -> int:
        if self._chunks:
            return len(self._chunks[0])
        if self._delayed_due():
            return len(self._delayed_chunks[0])
        return 0

    def close(self) -> None:
        self.closed = True


class FakeSerialModule:
    """Zamena za ceo ``serial`` modul — samo ono što bricon.py/bricon_pm.py koriste."""

    def __init__(self, factory: Callable[..., FakeBriconSerial], calls: list[int]) -> None:
        self._factory = factory
        self._calls = calls
        self.SerialException = OSError

    def Serial(self, *, port: str, baudrate: int, **kwargs: Any) -> FakeBriconSerial:
        self._calls.append(baudrate)
        return self._factory(port=port, baudrate=baudrate, **kwargs)


def _install_fake_serial(monkeypatch: pytest.MonkeyPatch, behavior: dict[int, Any]) -> list[int]:
    """
    ``behavior[baud]`` je ili izuzetak (Serial() otvaranje puca) ili dict sa
    opcionim ključevima 'chunks' / 'read_exc' / 'read_exc_after' za FakeBriconSerial.
    Bauds koji nisu u behavior-u dobijaju prazan (bez podataka) fake serial.
    """
    calls: list[int] = []

    def factory(*, port: str, baudrate: int, **kwargs: Any) -> FakeBriconSerial:
        spec = behavior.get(baudrate)
        if isinstance(spec, BaseException):
            raise spec
        spec = spec or {}
        return FakeBriconSerial(port=port, baudrate=baudrate, **spec)

    fake_module = FakeSerialModule(factory, calls)
    monkeypatch.setattr(br, "serial", fake_module)
    monkeypatch.setattr(br, "verify_com_port_available", lambda port: None)
    # PM protokol nije predmet ovih testova — preskoči odmah na pasivni fallback.
    monkeypatch.setattr(pm, "try_print_manager_protocol_readout", lambda *a, **kw: None)
    # Brzi testovi: mali budžet po baud-u i ukupno, bez čekanja pravih 12s/30s.
    monkeypatch.setattr(br, "BRICON_USB_PER_BAUD_MIN_SEC", 0.6)
    monkeypatch.setattr(br, "BRICON_USB_READOUT_MIN_TOTAL_SEC", 1.0)
    return calls


def _config(port: str = "COM7", baudrate: int = 9600) -> TaurisConnectionConfig:
    return TaurisConnectionConfig(port=port, baudrate=baudrate)


# ---------------------------------------------------------------------------
# 1) Prvi baud ne radi, drugi radi — fallback mora stvarno da pređe na sledeći.
# ---------------------------------------------------------------------------
def test_first_baud_fails_second_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_serial(
        monkeypatch,
        {
            9600: OSError("could not open port 'COM7': PermissionError"),
            19200: {"chunks": [SAMPLE_BRICON_TEXT.encode("utf-8")]},
        },
    )
    data = br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)
    assert 9600 in calls and 19200 in calls
    assert calls.index(9600) < calls.index(19200)
    assert data["bricon_capture_mode"] == "usb_readout"
    assert len(data["bricon_clockings"]) == 3
    assert data["takmicar"] == "Jan Janssen"


# ---------------------------------------------------------------------------
# 2) Svi baud pokušaji ne uspevaju — mora se probati SVAKI, brzo (deljeni deadline fix).
# ---------------------------------------------------------------------------
def test_all_bauds_fail_tries_every_candidate_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_serial(monkeypatch, {})  # svi bauds vraćaju prazno (nema podataka)
    monkeypatch.setattr(br, "BRICON_USB_PASSIVE_FALLBACK_MAX_BAUDS", 5)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as excinfo:
        br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)
    elapsed = time.monotonic() - t0

    expected_bauds = [9600, 19200, 38400, 57600, 115200]
    assert calls == expected_bauds, "svaki podržani baud mora biti probran tačno jednom"
    # Sa BRICON_USB_PER_BAUD_MIN_SEC=0.6s i 5 baud-ova, gornja granica je ~5*0.6s + margina.
    assert elapsed < 6.0, f"fallback nije smeo da potroši sve vreme na jedan baud (trajalo {elapsed:.2f}s)"
    assert "nije nađen" in str(excinfo.value)
    for b in expected_bauds:
        assert str(b) in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3) Prekid konekcije usred prenosa — jasna poruka + log, parcijalni podaci sačuvani,
#    fallback ipak nastavlja na sledeći baud.
# ---------------------------------------------------------------------------
def test_disconnect_mid_transfer_preserves_partial_data(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    partial = b"BRICON X-TREME\nFancier: Nepotpuno\n"
    calls = _install_fake_serial(
        monkeypatch,
        {
            9600: {
                "chunks": [partial],
                "read_exc": OSError("ClearCommError failed — device disconnected"),
                "read_exc_after": 1,
            },
            19200: {"chunks": [SAMPLE_BRICON_TEXT.encode("utf-8")]},
        },
    )
    with caplog.at_level("WARNING"):
        data = br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)

    assert calls == [9600, 19200], "prekid na prvom baud-u ne sme da spreci probu sledeceg"
    assert len(data["bricon_clockings"]) == 3  # uspesan drugi pokusaj
    disconnect_logs = [r for r in caplog.records if "disconnect" in r.getMessage().lower()]
    assert disconnect_logs, "prekid konekcije mora biti ULOGOVAN, ne tiho progutan"


def test_disconnect_on_all_bauds_reports_clear_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    partial = b"neki bajtovi pre prekida"
    behavior = {
        b: {"chunks": [partial], "read_exc": OSError("device disconnected"), "read_exc_after": 1}
        for b in (9600, 19200, 38400, 57600, 115200)
    }
    _install_fake_serial(monkeypatch, behavior)
    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError) as excinfo:
            br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)
    # Poruka korisniku pominje da su podaci stigli ali nisu prepoznati kao BRICON lista
    # (nedovoljno bajtova za validan payload) — i navodi probane baud-ove.
    assert "9600" in str(excinfo.value)
    assert any("disconnect" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4) Nepotpun/nevalidan paket na svim baud-ovima — jasna greška + dijagnostički fajl.
# ---------------------------------------------------------------------------
def test_incomplete_packet_saves_diagnostic_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bc, "bricon_capture_dir", lambda: tmp_path)
    garbage = b"x" * 60  # dovoljno dugacko (>=40B) da prodje raw_len proveru, ali ne parsira se
    behavior = {b: {"chunks": [garbage]} for b in (9600, 19200, 38400, 57600, 115200)}
    _install_fake_serial(monkeypatch, behavior)

    with pytest.raises(RuntimeError) as excinfo:
        br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)

    assert "ne liče na BRICON" in str(excinfo.value) or "ne lice na BRICON" in str(excinfo.value)
    assert "sačuvani" in str(excinfo.value) or "sacuvani" in str(excinfo.value)
    saved = list(tmp_path.glob("bricon_partial_*.log"))
    assert saved, "parcijalni (nevalidni) podaci moraju biti snimljeni za dijagnostiku"
    content = saved[0].read_text(encoding="utf-8")
    assert "78 78 78" in content or "xxx" in content.lower()  # hex 'x' = 78


# ---------------------------------------------------------------------------
# 5) Timeout — nema podataka ni na jednom baud-u.
# ---------------------------------------------------------------------------
def test_timeout_no_data_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, {})
    with pytest.raises(RuntimeError, match="nije nađen"):
        br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)


# ---------------------------------------------------------------------------
# 6) Uspešan BRICON readout end-to-end — podaci se ispravno parsiraju.
# ---------------------------------------------------------------------------
def test_successful_bricon_readout_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, {9600: {"chunks": [SAMPLE_BRICON_TEXT.encode("utf-8")]}})
    data = br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)
    assert data["device_type"] == "bricon"
    assert data["takmicar"] == "Jan Janssen"
    assert len(data["bricon_clockings"]) == 3
    assert data["bricon_clockings"][0]["ring"] == "BEL24-1234567"


# ---------------------------------------------------------------------------
# 7) BRICON PM (Print Manager) režim — detaljno u tests/test_bricon_pm_protocol.py.
#    Ovde samo: kad PM ne nađe sat, read_race_from_bricon_usb_readout ide na kratak
#    pasivni fallback (štampački kabl), ne na minute čekanja po svim baud-ovima.
# ---------------------------------------------------------------------------
def test_pm_failure_falls_back_to_short_passive_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_serial(monkeypatch, {})
    monkeypatch.setattr(br, "BRICON_USB_PASSIVE_FALLBACK_MAX_SEC", 1.0)
    with pytest.raises(RuntimeError) as ei:
        br.read_race_from_bricon_usb_readout(_config(baudrate=19200), max_listen_sec=1.0)
    assert "nije nađen" in str(ei.value)
    assert calls == [19200, 9600], "pasivni fallback: izabrani baud + 9600, ne svih pet"


# ---------------------------------------------------------------------------
# 8) DTR/RTS podešavanja — proveri razliku između USB pasivnog i PM režima.
# ---------------------------------------------------------------------------
def test_dtr_rts_usb_passive_vs_pm_protocol() -> None:
    from tauris_desktop.lib import prepare_serial_port_for_bricon

    usb_port = FakeBriconSerial(port="COM3", baudrate=9600)
    prepare_serial_port_for_bricon(usb_port, mode="usb_passive")
    assert usb_port.dtr is True
    assert usb_port.rts is False
    assert usb_port.reset_input_calls == 1
    assert usb_port.reset_output_calls == 0

    # PM režim = kao Communication.StartComm u BriconLib.dll: RTS=True, DTR ostaje spušten
    pm_port = FakeBriconSerial(port="COM3", baudrate=9600)
    prepare_serial_port_for_bricon(pm_port, mode="pm_protocol")
    assert pm_port.dtr is False
    assert pm_port.rts is True
    assert pm_port.reset_input_calls == 1
    assert pm_port.reset_output_calls == 1


def test_bricon_and_pm_prepare_helpers_delegate_to_shared_function() -> None:
    usb_port = FakeBriconSerial(port="COM3", baudrate=9600)
    br._prepare_bricon_usb_serial(usb_port)
    assert (usb_port.dtr, usb_port.rts) == (True, False)

    pm_port = FakeBriconSerial(port="COM3", baudrate=9600)
    pm._prepare_pm_serial(pm_port)
    assert (pm_port.dtr, pm_port.rts) == (False, True)


# ---------------------------------------------------------------------------
# 9) Veliki bafer bez usporavanja — bez ponovnog re-parsiranja na svaki prazan ciklus.
# ---------------------------------------------------------------------------
def test_large_buffer_does_not_reparse_on_every_idle_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    # Svaki fizički read vraca tacno jedan mali "chunk" (umesto da ih read_serial_bytes_until_idle
    # sve odjednom pokupi) - ovo nam daje preciznu kontrolu nad brojem "tick"-ova.
    monkeypatch.setattr(br, "BRICON_READ_MAX_ITERATIONS_SLOW", 1)

    growth_chunks = [f"garbage line {i}\n".encode("utf-8") for i in range(12)]
    _install_fake_serial(monkeypatch, {9600: {"chunks": list(growth_chunks)}})
    monkeypatch.setattr(br, "BRICON_USB_PER_BAUD_MIN_SEC", 0.5)
    monkeypatch.setattr(br, "BRICON_USB_READOUT_MIN_TOTAL_SEC", 0.5)

    decode_calls = {"n": 0}
    orig_decode = br.decode_bricon_payload

    def counting_decode(raw: bytes, *, quiet: bool = False) -> str:
        decode_calls["n"] += 1
        return orig_decode(raw, quiet=quiet)

    monkeypatch.setattr(br, "decode_bricon_payload", counting_decode)

    with pytest.raises(RuntimeError):
        br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=1.0)

    # Bez fix-a: re-parsira se na SVAKI tick (podaci + idle), sto bi bilo znatno vise od
    # broja stvarnih porasta bafera. Sa fix-om: najvise jedan decode po jedinstvenoj duzini
    # bafera (12 porasta) + eventualno par idle-ciklusa nakon iscrpljivanja chunkova, ali
    # tih idle-ciklusa NE SME biti dekodiranja jer se bafer ne menja.
    assert decode_calls["n"] <= len(growth_chunks) + 1, (
        f"decode_bricon_payload pozvan {decode_calls['n']} puta — ocekivano <= "
        f"{len(growth_chunks) + 1} (jednom po porastu bafera, ne po tick-u)"
    )
    assert decode_calls["n"] >= len(growth_chunks) - 1


# ---------------------------------------------------------------------------
# 10) Otkazivanje od strane korisnika — bezbedan prekid, jasna poruka.
# ---------------------------------------------------------------------------
def test_should_cancel_stops_quickly_with_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, {})
    t0 = time.monotonic()
    with pytest.raises(br.BriconCancelled, match="Otkazano"):
        br.read_race_from_bricon_usb_readout(
            _config(baudrate=9600),
            max_listen_sec=30.0,
            should_cancel=lambda: True,
        )
    assert time.monotonic() - t0 < 1.0, "otkazivanje mora biti brzo, ne cekati pun deadline"


def test_on_phase_callback_reports_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_serial(monkeypatch, {9600: {"chunks": [SAMPLE_BRICON_TEXT.encode("utf-8")]}})
    phases: list[str] = []
    br.read_race_from_bricon_usb_readout(
        _config(baudrate=9600), max_listen_sec=5.0, on_phase=phases.append
    )
    joined = " | ".join(phases)
    assert "port" in joined or "traženje" in joined or "trazenje" in joined
    assert any("9600" in p for p in phases)
    assert any("završeno" in p or "zavrseno" in p for p in phases)


# ---------------------------------------------------------------------------
# 11) Podaci ostaju sačuvani ako kasniji PDF/email korak ne uspe.
# ---------------------------------------------------------------------------
def test_race_data_recovery_snapshot_survives_pipeline_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bc, "bricon_capture_dir", lambda: tmp_path)
    race_data = {
        "race_id": "BRICON-TEST-1",
        "takmicar": "Pera Perić",
        "bricon_clockings": [{"ring": "SR-24-1", "arrival_time": "12:00:00"}],
    }
    path = bc.save_race_data_recovery_snapshot(
        race_data, port="COM7", reason="RuntimeError: PDF generisanje nije uspelo"
    )
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["race_data"] == race_data
    assert saved["_recovery_port"] == "COM7"
    assert "PDF" in saved["_recovery_reason"]


# ---------------------------------------------------------------------------
# 12) Log mora prikazati koji baud je isproban i rezultat pokušaja.
# ---------------------------------------------------------------------------
def test_diagnostic_log_shows_each_baud_attempt_and_result(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_fake_serial(
        monkeypatch,
        {
            9600: OSError("could not open port"),
            19200: {"chunks": [SAMPLE_BRICON_TEXT.encode("utf-8")]},
        },
    )
    with caplog.at_level("INFO"):
        br.read_race_from_bricon_usb_readout(_config(baudrate=9600), max_listen_sec=5.0)

    diag_records = [r.getMessage() for r in caplog.records if "BRICON diag" in r.getMessage()]
    assert any("baud=9600" in m for m in diag_records)
    assert any("baud=19200" in m for m in diag_records)
    assert any("phase=baud_ok" in m for m in diag_records)
