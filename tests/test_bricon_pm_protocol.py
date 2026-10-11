"""
Testovi za BRICON Print Manager binarni protokol (rekonstruisan iz BriconLib.dll).

``FakePmClock`` simulira Speedy sat na drugom kraju USB kabla: prima PM okvire
(0x6C | payload | ck), proverava checksum i odgovara okvirima 0x33 | eho | len | status |
data | ck — tačno onako kako to radi ``Communication.SendCommand`` u Print Manageru.
"""

from __future__ import annotations

from typing import Any

import pytest

from tauris_desktop import bricon as br
from tauris_desktop import bricon_pm as pm


def _pigeon_record(el_band: str, fed_band: str, date6: str, time6: str, *, evaluation: str = "0") -> bytes:
    # [2 B] + ElBand 8 + FedBand 22 + club 4 + flight 4 + 4x2 pozicije + ddMMyy + HHmmss + eval + 2 + team
    # (dužine 53/54/60/61/70/71 B znače 14-znakovni FedBand — kao u GetPigeonCommand.HandleResponse)
    data = b"\x00\x00"
    data += el_band.encode("ascii").ljust(8, b"\x00")
    data += fed_band.encode("ascii").ljust(22, b"\x00")
    data += b"0001" + b"0001"
    data += bytes([0, 1, 0, 2, 0, 3, 0, 4])
    data += date6.encode("ascii") + time6.encode("ascii")
    data += evaluation.encode("ascii")
    data += b"\x00\x00" + bytes([1])  # 2 rezervisana bajta + TeamNbr (ukupno 64 B)
    return data


def _timer_record() -> bytes:
    data = b"\x00" + b"0001" + b"0001"
    data += b"090526" + b"200342" + b"200342" + b"0000"
    data += b"100526" + b"180336" + b"180336" + b"0000"
    data += b"0000"
    return data


def _fancier_record(licence: str = "C5397", name: str = "KUSIC DRAGAN", town: str = "Leskovac") -> bytes:
    data = licence.encode().ljust(8, b"\x00")
    data += name.encode().ljust(25, b"\x00")
    data += b"Ulica 1".ljust(25, b"\x00")
    data += b"16000".ljust(6, b"\x00")
    data += town.encode().ljust(20, b"\x00")
    data += b"0" * 20
    return data


class FakePmClock:
    """Simulirani BRICON Speedy na adresi 0x82 koji govori PM protokol."""

    def __init__(
        self,
        *,
        port: str = "COM7",
        baudrate: int = 9600,
        addr: int = 0x82,
        pigeons: list[bytes] | None = None,
        timers: list[bytes] | None = None,
        version: bytes = b"SPD 2.07 C5397",
        corrupt_first_pigeon_frame: bool = False,
        silent: bool = False,
        **_kw: Any,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = 0.05
        self.addr = addr
        self.version = version
        self.pigeons = pigeons if pigeons is not None else []
        self.timers = timers if timers is not None else []
        self.corrupt_first_pigeon_frame = corrupt_first_pigeon_frame
        self.silent = silent
        self._rx = bytearray()
        self.written: list[bytes] = []
        self.dtr: bool | None = None
        self.rts: bool | None = None
        self.closed = False
        self.frames_rejected = 0

    # --- pyserial API ---
    def setDTR(self, v: bool) -> None:
        self.dtr = v

    def setRTS(self, v: bool) -> None:
        self.rts = v

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def reset_output_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    @property
    def in_waiting(self) -> int:
        return len(self._rx)

    def read(self, n: int = 1) -> bytes:
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def write(self, data: bytes) -> int:
        frame = bytes(data)
        self.written.append(frame)
        if self.silent or self.baudrate != 9600:
            return len(frame)
        # Validacija okvira kao na satu
        if len(frame) < 5 or frame[0] != pm.PM_TX_START or frame[-1] != pm.pm_checksum_tx(frame[1:-1]):
            self.frames_rejected += 1
            return len(frame)
        payload = frame[1:-1]
        addr, cmd = payload[0], payload[1]
        if addr != self.addr:
            return len(frame)  # nije za mene — tišina
        self._rx.extend(self._respond(addr, cmd, payload[3:]))
        return len(frame)

    def _respond(self, addr: int, cmd: int, data: bytes) -> bytes:
        if cmd == 0xC0:  # ActivateBA
            return pm.pm_build_response_frame(addr, cmd, 0, self.version)
        if cmd == 0xC1:  # GetFancier
            return pm.pm_build_response_frame(addr, cmd, 0, _fancier_record())
        if cmd == 0xC8:  # GetPigeon
            idx = (data[0] << 8) | data[1]
            if idx >= len(self.pigeons):
                return pm.pm_build_response_frame(addr, cmd, pm.RS_NO_MORE_RECORDS)
            frame = pm.pm_build_response_frame(addr, cmd, 0, self.pigeons[idx])
            if idx == 0 and self.corrupt_first_pigeon_frame:
                self.corrupt_first_pigeon_frame = False
                frame = frame[:-1] + bytes([(frame[-1] + 1) & 0xFF])
            return frame
        if cmd == 0xCD:  # GetTimer
            idx = data[0]
            if idx >= len(self.timers):
                return pm.pm_build_response_frame(addr, cmd, pm.RS_NO_MORE_RECORDS)
            return pm.pm_build_response_frame(addr, cmd, 0, self.timers[idx])
        return pm.pm_build_response_frame(addr, cmd, 17)  # UnknownCommand


class FakeSerialModule:
    def __init__(self, factory, calls: list[tuple[int, int]]) -> None:
        self._factory = factory
        self._calls = calls
        self.SerialException = OSError

    def Serial(self, *, port: str, baudrate: int, **kwargs: Any):
        self._calls.append((baudrate, kwargs.get("stopbits", 1)))
        return self._factory(port=port, baudrate=baudrate, **kwargs)


@pytest.fixture(autouse=True)
def _fast_pm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pm, "PM_DELAY_AFTER_COMMAND_SEC", 0.0)
    monkeypatch.setattr(pm, "PM_DISCOVERY_ROUND_PAUSE_SEC", 0.01)
    monkeypatch.setattr(pm, "PM_ACTIVATE_TIMEOUT_SEC", 0.03)
    monkeypatch.setattr(pm, "PM_COMMAND_TIMEOUT_SEC", 0.2)


# --- okvir -----------------------------------------------------------------------------


def test_tx_frame_matches_print_manager() -> None:
    # Communication.SendCommand: 0x6C | payload | (0x6C + sum) & 0xFF
    assert pm.pm_encode_frame(pm.PM_CMD_ACTIVATE_MASTER).hex() == "6c106100dd"
    assert pm.pm_encode_frame(pm.PM_SEND_VERSION_INFO)[-1] == 0x60
    assert len(pm.PM_SEND_VERSION_INFO) == 22
    assert pm.PM_SEND_VERSION_INFO[0:3] == bytes([0x10, 0x70, 0x13])


def test_command_payloads_match_dll_tobytes() -> None:
    assert pm.cmd_activate_ba(0x82) == bytes([0x82, 0xC0, 0x01, 0x00])
    assert pm.cmd_get_fancier(0x82) == bytes([0x82, 0xC1, 0x01, 0x00])
    assert pm.cmd_get_pigeon(0x82, 0x0102) == bytes([0x82, 0xC8, 0x0A, 0x01, 0x02]) + b"0000" + b"0000"
    assert pm.cmd_get_timer(0x82, 3) == bytes([0x82, 0xCD, 0x05, 0x03]) + b"0000"


def test_rx_frame_roundtrip_and_checksum() -> None:
    # Primer iz BriconLib.Monitor.RespondCommand.ToBytes: 33 85 10 02 00 00 + ck
    sample = bytes([0x33, 0x85, 0x10, 0x02, 0x00, 0x00])
    sample += bytes([sum(sample) & 0xFF])
    resp, used = pm.pm_parse_response(sample, bytes([0x85, 0x10]))
    assert resp is not None and resp.status == 0 and resp.data == b"\x00" and used == len(sample)

    frame = pm.pm_build_response_frame(0x82, 0xC1, 0, b"ABC")
    resp2, used2 = pm.pm_parse_response(frame, bytes([0x82, 0xC1]))
    assert resp2 is not None and resp2.data == b"ABC" and used2 == len(frame)

    bad = frame[:-1] + bytes([frame[-1] ^ 0xFF])
    with pytest.raises(pm.PmFrameError):
        pm.pm_parse_response(bad, bytes([0x82, 0xC1]))
    assert pm.pm_parse_response(frame[:4], bytes([0x82, 0xC1])) == (None, 0)


def test_parse_pigeon_and_timer_records() -> None:
    p = pm.parse_pigeon_response(0, _pigeon_record("C507F0C6", "SR-23-0000464835", "100526", "120435"))
    assert p.el_band == "C507F0C6"
    assert p.fed_band == "SR-23-0000464835"
    assert p.positions == (1, 2, 3, 4)
    assert p.arrival is not None and p.arrival.strftime("%d/%m/%y %H:%M:%S") == "10/05/26 12:04:35"
    not_arrived = pm.parse_pigeon_response(1, _pigeon_record("C507F033", "SR-23-0000464840", "000000", "000000"))
    assert not_arrived.arrival is None
    t = pm.parse_timer_response(0, _timer_record())
    assert t.basketing_master is not None and t.basketing_master.strftime("%d/%m/%y %H:%M:%S") == "09/05/26 20:03:42"
    assert t.readout_internal is not None and t.readout_internal.strftime("%H:%M:%S") == "18:03:36"
    f = pm.parse_fancier_response(_fancier_record())
    assert (f.licence, f.name, f.town) == ("C5397", "KUSIC DRAGAN", "Leskovac")


# --- ceo tok ----------------------------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, clock: FakePmClock) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(pm, "serial", FakeSerialModule(lambda **kw: clock, calls))
    return calls


def test_full_readout_over_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakePmClock(
        pigeons=[
            _pigeon_record("C507F0C6", "SR-23-0000464835", "100526", "120435"),
            _pigeon_record("C507F033", "SR-23-0000464840", "100526", "120512"),
            _pigeon_record("C507F012", "SR-23-0000464845", "000000", "000000"),
        ],
        timers=[_timer_record()],
    )
    calls = _install(monkeypatch, clock)
    phases: list[str] = []
    ro = pm.read_readout_via_print_manager_protocol("COM7", listen_sec=2.0, on_phase=phases.append)

    assert calls == [(9600, 2)], "port se otvara na 9600 sa 2 stop-bita kao Print Manager"
    assert clock.rts is True and clock.dtr is False
    assert clock.frames_rejected == 0, "svaki poslati okvir mora imati ispravan 0x6C prefiks i checksum"
    assert ro.device.addr == 0x82 and ro.device.model == "Bricon Speedy"
    assert ro.fancier.name == "KUSIC DRAGAN"
    assert len(ro.pigeons) == 3 and len(ro.timers) == 1
    # Prva komanda je ActivateMaster na 0x10, pa ActivateBA redom
    assert clock.written[0] == pm.pm_encode_frame(pm.PM_CMD_ACTIVATE_MASTER)
    assert any(w == pm.pm_encode_frame(pm.cmd_activate_ba(0x82)) for w in clock.written)

    text = pm.render_lista_doleta(ro)
    rows = br.parse_bricon_speedy_arrival_rows(text)
    assert [r["chip_id"] for r in rows] == ["C507F0C6", "C507F033"]
    assert rows[0]["arrival_time"] == "12:04:35"
    assert br.looks_like_bricon_text(text)
    data = br.parse_bricon_payload_to_race_dict(text)
    assert data["takmicar"] == "KUSIC DRAGAN"
    assert data["bricon_member_code"] == "C5397"
    assert "pakovanje : 3" in text
    assert any("golubove" in p for p in phases)


def test_corrupt_frame_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakePmClock(
        pigeons=[_pigeon_record("C507F0C6", "SR-23-0000464835", "100526", "120435")],
        corrupt_first_pigeon_frame=True,
    )
    _install(monkeypatch, clock)
    ro = pm.read_readout_via_print_manager_protocol("COM7", listen_sec=2.0)
    assert len(ro.pigeons) == 1
    get_pigeon0 = pm.pm_encode_frame(pm.cmd_get_pigeon(0x82, 0))
    assert sum(1 for w in clock.written if w == get_pigeon0) == 2, "pokvaren okvir -> komanda se ponavlja"


def test_silent_clock_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakePmClock(silent=True)
    _install(monkeypatch, clock)
    with pytest.raises(RuntimeError) as ei:
        pm.read_readout_via_print_manager_protocol("COM7", listen_sec=1.0)
    assert "9600 baud, 8N2" in str(ei.value)
    assert clock.closed
    # Otkrivanje je pokušano na svim adresama bar jednom
    for addr in (0x80, 0x85):
        assert pm.pm_encode_frame(pm.cmd_activate_ba(addr)) in clock.written


def test_try_wrapper_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from tauris_desktop.config import TaurisConnectionConfig

    clock = FakePmClock(silent=True)
    _install(monkeypatch, clock)
    assert pm.try_print_manager_protocol_readout(TaurisConnectionConfig(port="COM7", baudrate=9600), max_listen_sec=1.0) is None


def test_bricon_usb_readout_uses_pm_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_race_from_bricon_usb_readout: PM uspeh -> race_data bez pasivnog slušanja."""
    from tauris_desktop.config import TaurisConnectionConfig

    clock = FakePmClock(
        pigeons=[_pigeon_record("C507F0C6", "SR-23-0000464835", "100526", "120435")],
        timers=[_timer_record()],
    )
    _install(monkeypatch, clock)
    monkeypatch.setattr(br, "verify_com_port_available", lambda port: None)
    passive_opened: list[int] = []
    monkeypatch.setattr(br, "serial", FakeSerialModule(lambda **kw: passive_opened.append(1) or clock, []))

    data = br.read_race_from_bricon_usb_readout(
        TaurisConnectionConfig(port="COM7", baudrate=9600), max_listen_sec=2.0
    )
    assert data["bricon_capture_mode"] == "usb_pm_protocol"
    assert data["bricon_pm_device"]["model"] == "Bricon Speedy"
    assert len(data["bricon_pm_pigeons"]) == 1
    assert data["bricon_clockings"] or br.parse_bricon_speedy_arrival_rows(data["raw_com_text"])
    assert not passive_opened, "pasivni fallback se ne pokreće kad PM uspe"
