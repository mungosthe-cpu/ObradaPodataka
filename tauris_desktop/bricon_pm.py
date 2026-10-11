"""
BRICON Print Manager — binarni USB protokol, rekonstruisan iz ``BriconLib.dll``
(klase ``BriconLib.Communications.Communication``, ``Command`` i naslednice).

Ranija verzija ovog modula slala je samo ``Command.ToBytes()`` (npr.
``10 70 13 02 01 01 00 "Print Manager U"``) i onda pasivno čekala tekst. Sat na to
NIKAD ne odgovara, jer Print Manager svaki paket uokviruje i otvara port drugačije:

Serijski port (``Communication.StartComm``)
    9600 baud, 8 bita podataka, bez pariteta, **2 stop-bita**; posle otvaranja
    ``RtsEnable = True`` i pauza 50 ms. U ovoj verziji DLL-a ne postoji 38400 — sve
    ide na 9600. Statusi 113/114 (pređi na 9600/38400) postoje u enumu i ovde se
    poštuju ako ih sat vrati.

Slanje (``Communication.SendCommand``)
    ``Sleep(10 ms)``; ``DiscardInBuffer``; ``DiscardOutBuffer``; ``ReadTimeout = t``
    TX okvir = ``0x6C`` | payload | ck,  ck = (0x6C + Σ payload) & 0xFF
    payload   = ``Command.CreateBytes(adresa, komanda, dužina)`` + podaci

Prijem
    RX okvir = ``0x33`` | adresa | komanda | len | status | data[len-1] | ck
    adresa/komanda su eho prva dva bajta našeg payload-a,
    ck = (0x33 + adresa + komanda + len + status + Σ data) & 0xFF.
    status 0 = OK, 5 = nema više zapisa (kraj liste), 32 = checksum na satu,
    113/114 = promena brzine, 252 = PC checksum, 253 = PC timeout.

Otkrivanje uređaja (``Communication.OnIdle`` / ``TryToActivateDevice``), 100 ms po pokušaju
    adresa 0x10 (Master)      -> ActivateMaster   payload ``10 61 00``
    adrese 0x80..0x85 (BA)    -> ActivateBA       payload ``AA C0 01 00``
    Odgovor sa statusom 0 = uređaj nađen; data = verzija (ASCII, npr. ``SPD...``).

Čitanje takmičara (``PrintManager.FancierReadOut``)
    GetFancier   ``AA C1 01 00``                       -> licenca, ime, adresa, pošt. broj, mesto
    GetPigeon    ``AA C8 0A idxH idxL club(4) flight(4)`` -> po jedan golub, idx od 0 dok status ≠ 0
    GetTimer     ``AA CD 05 idx '0000'``                -> vremena ukorpavanja/očitavanja, idx od 0

Ovaj modul sve to radi i na kraju renderuje tekst u istom obliku kao štampana
„lista doleta“ (vidi ``tests/fixtures/bricon_speedy_lista_doleta_sample.txt``), tako da
ostatak programa (parser, PDF, zaključavanje) radi nepromenjeno.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from tauris_desktop.bricon_capture import (
    BriconComCapture,
    bricon_capture_enabled,
    save_partial_payload_diagnostic,
    start_capture_session,
    wrap_serial_for_capture,
)
from tauris_desktop.config import TaurisConnectionConfig
from tauris_desktop.lib import prepare_serial_port_for_bricon

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    serial = None

# --- Konstante protokola -----------------------------------------------------------

PM_TX_START = 0x6C
PM_RX_START = 0x33

PM_BAUD_DEFAULT = 9600
# Zadržano zbog kompatibilnosti (stari kod/testovi su probali 38400 pa 9600). PM uvek
# kreće od 9600; 38400 se koristi SAMO ako sat vrati status 114.
PM_BAUD_INITIAL_CANDIDATES: tuple[int, ...] = (9600,)

# Odgovor sata (BriconLib.Communications.ResponseStatus)
RS_OK = 0
RS_NO_MORE_RECORDS = 5
RS_CHECKSUM_ERROR = 32
RS_SWITCH_TO_9600 = 113
RS_SWITCH_TO_38400 = 114
RS_PC_INVALID_BYTE = 251
RS_PC_CHECKSUM_ERROR = 252
RS_PC_TIMEOUT = 253
RS_PC_ERROR = 254

RS_NAMES = {
    0: "OK",
    1: "NoFancierData",
    2: "InvalidDataFormat",
    3: "FlightAlreadyBasketed",
    4: "FlightDataNotDeleted",
    5: "NoRecordThatCompliesTheRequest",
    6: "SynchronisationFailed",
    7: "TimerAlreadySynchronised",
    8: "NoPigeonTable",
    9: "UnknownPigeonID",
    10: "PigeonAlreadyBasketed",
    11: "ELRingAlreadyPresent",
    12: "OutOfRange",
    13: "NoTimerDataPresent",
    14: "FlightNotBasketed",
    15: "InvalidPincode",
    16: "SecretCodeNotChanged",
    17: "UnknownCommand",
    19: "NoVersionData",
    32: "ChecksumError",
    113: "SwitchAfterResponceTo9600",
    114: "SwitchAfterResponceTo38400",
    252: "PCChecksumError",
    253: "PCTimeOut",
}

ADDR_MASTER = 0x10
# Adresses enum: BA_First=0x80, BA_Benzing, BA_Bricon, BA_Tauris, BA_Tipes, BA_Unikon
BA_ADDRESSES: tuple[int, ...] = (0x80, 0x81, 0x82, 0x83, 0x84, 0x85)
BA_ADDRESS_NAMES = {
    0x80: "BA", 0x81: "Benzing", 0x82: "Bricon", 0x83: "Tauris", 0x84: "Tipes", 0x85: "Unikon",
}

PM_CMD_ACTIVATE_MASTER = bytes([0x10, 0x61, 0x00])
# SendVersionInfoCommand("Print Manager USB", 1, 1, 0).ToBytes() — komanda za MASTER (klupski
# uređaj); za čitanje takmičarskog sata nije potrebna, ostaje zbog kompatibilnosti/testova.
PM_SEND_VERSION_INFO = bytes.fromhex("10701302010100") + b"Print Manager U"

# Vremena kao u Print Manageru (Communication..ctor): 100 ms aktivacija, 2000 ms komande,
# 50 ms pauza posle svake komande.
PM_ACTIVATE_TIMEOUT_SEC = 0.15
PM_COMMAND_TIMEOUT_SEC = 2.0
PM_DELAY_AFTER_COMMAND_SEC = 0.05
PM_DISCOVERY_ROUND_PAUSE_SEC = 0.25
PM_MAX_PIGEON_RECORDS = 3000
PM_MAX_TIMER_RECORDS = 64
PM_FRAME_RETRIES = 2


# --- Okvir ------------------------------------------------------------------------


class PmFrameError(ValueError):
    """Neispravan RX okvir (start bajt, eho ili checksum)."""


class PmTimeout(RuntimeError):
    """Sat nije odgovorio u zadatom vremenu."""


class PmProtocolError(RuntimeError):
    """Sat je odgovorio statusom greške."""

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.payload = payload
        super().__init__(
            f"BRICON status {status} ({RS_NAMES.get(status, '?')}) na komandu {payload[:2].hex()}"
        )


@dataclass
class PmResponse:
    status: int
    data: bytes
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.status == RS_OK


def pm_checksum_tx(payload: bytes) -> int:
    return (PM_TX_START + sum(payload)) & 0xFF


def pm_encode_frame(payload: bytes) -> bytes:
    """Tačno ono što ``Communication.SendCommand`` upiše na port."""
    return bytes([PM_TX_START]) + bytes(payload) + bytes([pm_checksum_tx(payload)])


def pm_build_response_frame(addr: int, cmd: int, status: int, data: bytes = b"") -> bytes:
    """Odgovor sata (koristi se u testovima/simulatoru) — inverz od ``pm_parse_response``."""
    length = len(data) + 1
    body = bytes([PM_RX_START, addr & 0xFF, cmd & 0xFF, length & 0xFF, status & 0xFF]) + bytes(data)
    return body + bytes([sum(body) & 0xFF])


def pm_parse_response(buf: bytes, payload: bytes) -> tuple[PmResponse | None, int]:
    """
    Parsira jedan RX okvir sa početka ``buf``.
    Vraća ``(odgovor, potrošeno)``; ``(None, 0)`` dok okvir nije kompletan.
    Baca ``PmFrameError`` ako okvir nije validan.
    """
    if len(buf) < 1:
        return None, 0
    if buf[0] != PM_RX_START:
        raise PmFrameError(f"očekivan start 0x33, dobijen 0x{buf[0]:02X}")
    if len(buf) < 5:
        return None, 0
    if buf[1] != payload[0] or buf[2] != payload[1]:
        raise PmFrameError(
            f"eho ne odgovara: {buf[1]:02X} {buf[2]:02X} != {payload[0]:02X} {payload[1]:02X}"
        )
    length = buf[3]
    status = buf[4]
    n_data = max(0, length - 1)
    total = 5 + n_data + 1
    if len(buf) < total:
        return None, 0
    data = bytes(buf[5 : 5 + n_data])
    ck = buf[5 + n_data]
    expect = (PM_RX_START + buf[1] + buf[2] + length + status + sum(data)) & 0xFF
    if ck != expect:
        raise PmFrameError(f"checksum {ck:02X} != {expect:02X}")
    return PmResponse(status=status, data=data, raw=bytes(buf[:total])), total


# --- Komande (Command.ToBytes) --------------------------------------------------------


def pm_create_bytes(addr: int, cmd: int, data: bytes = b"") -> bytes:
    """``Command.CreateBytes(adresa, komanda, dužina)`` + podaci."""
    return bytes([addr & 0xFF, cmd & 0xFF, len(data) & 0xFF]) + bytes(data)


def cmd_activate_ba(addr: int) -> bytes:
    # ActivateBACommand.ToBytes: CreateBytes(addr, 0x50 ako addr==0 inače 0xC0, 1); data[0]=0
    return pm_create_bytes(addr, 0x50 if addr == 0 else 0xC0, b"\x00")


def cmd_get_fancier(addr: int) -> bytes:
    return pm_create_bytes(addr, 0x51 if addr == 0 else 0xC1, b"\x00")


def cmd_get_pigeon(addr: int, index: int, club_id: str = "0000", flight_id: str = "0000") -> bytes:
    data = bytes([(index >> 8) & 0xFF, index & 0xFF])
    data += club_id.ljust(4, "0")[:4].encode("ascii", "replace")
    data += flight_id.ljust(4, "0")[:4].encode("ascii", "replace")
    return pm_create_bytes(addr, 0x58 if addr == 0 else 0xC8, data)


def cmd_get_timer(addr: int, index: int) -> bytes:
    return pm_create_bytes(addr, 0x5D if addr == 0 else 0xCD, bytes([index & 0xFF]) + b"0000")


# --- Serijska veza ----------------------------------------------------------------------


def _prepare_pm_serial(ser: Any) -> None:
    """Deljena implementacija — vidi lib.prepare_serial_port_for_bricon (mode='pm_protocol')."""
    prepare_serial_port_for_bricon(ser, mode="pm_protocol")


def open_pm_serial(port: str, baud: int = PM_BAUD_DEFAULT) -> Any:
    """Isto kao ``Communication.StartComm``: 8N2, RTS=True, 50 ms pauza."""
    if serial is None:
        raise RuntimeError("Nedostaje pyserial")
    ser = serial.Serial(
        port=port,
        baudrate=baud,
        bytesize=8,
        parity="N",
        stopbits=2,
        timeout=0.05,
    )
    _prepare_pm_serial(ser)
    time.sleep(0.05)
    return ser


def _read_available(
    ser: Any,
    wait_sec: float,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> bytes:
    """Pokupi sve što stigne u ``wait_sec`` (pasivno slušanje / dijagnostika)."""
    deadline = time.monotonic() + max(0.05, wait_sec)
    buf = bytearray()
    while time.monotonic() < deadline:
        try:
            n = ser.in_waiting
        except Exception as exc:
            logging.warning(
                "BRICON PM diag | phase=poll_in_waiting exc=%s: %s", type(exc).__name__, exc
            )
            if on_error is not None:
                on_error(exc)
            break
        if n:
            try:
                buf.extend(ser.read(n))
            except Exception as exc:
                logging.warning(
                    "BRICON PM diag | phase=read exc=%s: %s bytes_primljeno=%s",
                    type(exc).__name__, exc, len(buf),
                )
                if on_error is not None:
                    on_error(exc)
                break
            time.sleep(0.02)
            continue
        time.sleep(0.04)
    try:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
    except Exception:
        pass
    return bytes(buf)


class PmLink:
    """Jedna otvorena veza sa satom — šalje komande i parsira okvire kao Print Manager."""

    def __init__(self, ser: Any, *, on_rx_bytes: Callable[[int], None] | None = None) -> None:
        self.ser = ser
        self.on_rx_bytes = on_rx_bytes
        self.total_rx = 0
        self.last_frame_error: str | None = None
        # Poslednjih ~64 KB primljenih bajtova — za dijagnostički fajl ako prenos pukne.
        self.rx_history = bytearray()

    def _apply_baud(self, baud: int) -> None:
        try:
            if int(self.ser.baudrate) == baud:
                return
        except Exception:
            pass
        logging.info("BRICON PM: sat traži promenu brzine na %s baud", baud)
        self.ser.baudrate = baud
        time.sleep(0.08)
        _prepare_pm_serial(self.ser)

    def send(self, payload: bytes, timeout_sec: float) -> PmResponse:
        """
        ``Communication.SendCommand``: očisti bafere, pošalji okvir, čekaj ceo odgovor.
        Baca ``PmTimeout`` ako ništa (validno) ne stigne, ``PmFrameError`` posle
        ``PM_FRAME_RETRIES`` pokvarenih okvira.
        """
        last_frame_exc: PmFrameError | None = None
        for attempt in range(PM_FRAME_RETRIES + 1):
            time.sleep(0.010)
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except Exception:
                pass
            self.ser.write(pm_encode_frame(payload))
            try:
                self.ser.flush()
            except Exception:
                pass
            deadline = time.monotonic() + timeout_sec
            buf = bytearray()
            got_any = False
            while time.monotonic() < deadline:
                try:
                    n = self.ser.in_waiting
                except Exception as exc:
                    raise PmTimeout(f"veza prekinuta ({type(exc).__name__}: {exc})") from exc
                if not n:
                    time.sleep(0.005)
                    continue
                chunk = self.ser.read(n)
                if not chunk:
                    continue
                got_any = True
                buf.extend(chunk)
                self.total_rx += len(chunk)
                self.rx_history.extend(chunk)
                if len(self.rx_history) > 65536:
                    del self.rx_history[: len(self.rx_history) - 65536]
                if self.on_rx_bytes is not None:
                    self.on_rx_bytes(self.total_rx)
                # Posle prvog bajta dozvoli satu još malo vremena da završi okvir.
                deadline = max(deadline, time.monotonic() + 0.25)
                while buf:
                    try:
                        resp, used = pm_parse_response(bytes(buf), payload)
                    except PmFrameError as exc:
                        last_frame_exc = exc
                        self.last_frame_error = str(exc)
                        nxt = buf.find(bytes([PM_RX_START]), 1)
                        if nxt < 0:
                            buf.clear()
                        else:
                            del buf[:nxt]
                        continue
                    if resp is None:
                        break
                    if resp.status in (RS_SWITCH_TO_9600, RS_SWITCH_TO_38400):
                        self._apply_baud(9600 if resp.status == RS_SWITCH_TO_9600 else 38400)
                        return self.send(payload, timeout_sec)
                    return resp
            if not got_any:
                raise PmTimeout("nema odgovora")
            logging.warning(
                "BRICON PM diag | phase=bad_frame attempt=%s cmd=%s rx=%s err=%s",
                attempt + 1, payload[:2].hex(), bytes(buf).hex() or "-", last_frame_exc,
            )
        raise PmFrameError(str(last_frame_exc) if last_frame_exc else "neispravan odgovor")

    def command(self, payload: bytes, timeout_sec: float = PM_COMMAND_TIMEOUT_SEC) -> PmResponse:
        """Komanda sa pauzom posle odgovora (``_delayAfterCommand``)."""
        resp = self.send(payload, timeout_sec)
        time.sleep(PM_DELAY_AFTER_COMMAND_SEC)
        return resp


# --- Otkrivanje i čitanje ------------------------------------------------------------------


def _ascii_field(data: bytes, start: int, length: int) -> str:
    """``Conversion.ByteArrayToString``: nule postaju razmaci, rezultat se trim-uje."""
    seg = data[start : start + length]
    if not seg:
        return ""
    return bytes(32 if b == 0 else b for b in seg).decode("latin-1").strip()


def pm_version_to_model(version: str, addr: int) -> str:
    """``Conversion.SerialFromVersion`` (skraćeno): SPD… -> Speedy, SPX -> Speedy-Xtreme, BRX -> X-treme."""
    up = (version or "").strip().upper()
    if up.startswith("SPX"):
        return "Bricon Speedy-Xtreme"
    if up.startswith("SPD"):
        return "Bricon Speedy"
    if up.startswith("BRX"):
        return "Bricon X-treme"
    if up.startswith("CMB"):
        return "Bricon Combo"
    if addr == 0x83 or up.startswith("TAU"):
        return "Tauris"
    if addr == 0x84:
        return "Tipes"
    if addr == 0x85 or up.startswith("UNI"):
        return "Unikon"
    if addr == 0x81 or up.startswith("BENZ"):
        return "Benzing"
    return "Bricon"


def pm_version_digits(version: str) -> str:
    """Brojčani deo verzije firmvera za zaglavlje liste (npr. ``SPD 2.07`` -> ``207``)."""
    digits = re.sub(r"\D", "", version or "")
    return digits or "0"


@dataclass
class PmDevice:
    addr: int
    version: str
    model: str

    @property
    def addr_name(self) -> str:
        return BA_ADDRESS_NAMES.get(self.addr, f"0x{self.addr:02X}")


@dataclass
class PmFancier:
    licence: str = ""
    name: str = ""
    address: str = ""
    postcode: str = ""
    town: str = ""
    raw: bytes = b""


@dataclass
class PmPigeon:
    index: int
    el_band: str
    fed_band: str
    club_id: str
    flight_id: str
    positions: tuple[int, int, int, int]
    arrival: datetime | None
    evaluation: str
    team: int | None
    raw: bytes

    @property
    def arrived(self) -> bool:
        return self.arrival is not None


@dataclass
class PmTimer:
    index: int
    club_id: str
    flight_id: str
    basketing_master: datetime | None
    basketing_internal: datetime | None
    basketing_diff: str
    readout_master: datetime | None
    readout_internal: datetime | None
    readout_diff: str
    diff: str
    raw: bytes


@dataclass
class PmReadout:
    device: PmDevice
    fancier: PmFancier
    pigeons: list[PmPigeon] = field(default_factory=list)
    timers: list[PmTimer] = field(default_factory=list)
    read_at: datetime = field(default_factory=datetime.now)
    warnings: list[str] = field(default_factory=list)
    port: str = ""


def _dt_from_strings(date6: str, time6: str) -> datetime | None:
    """``Conversion.DateTimeFromStrings``: ``ddMMyy`` + ``HHmmss``; prazno/000000 = nema."""
    d = (date6 or "").strip()
    t = (time6 or "").strip()
    if not d or not t or d == "000000" or t == "000000" or len(d) != 6 or len(t) != 6:
        return None
    try:
        return datetime(
            2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]), int(t[0:2]), int(t[2:4]), int(t[4:6])
        )
    except ValueError:
        return None


def parse_fancier_response(data: bytes) -> PmFancier:
    """``GetFancierCommand.HandleResponse`` (ne-UK grana): licenca 8, ime 25, adresa 25, pošt. 6, mesto 20."""
    f = PmFancier(raw=bytes(data))
    if not data:
        return f
    lic_len = 8
    f.licence = _ascii_field(data, 0, lic_len)
    f.name = _ascii_field(data, lic_len, 25)
    f.address = _ascii_field(data, lic_len + 25, 25)
    f.postcode = _ascii_field(data, lic_len + 50, 6)
    f.town = _ascii_field(data, lic_len + 56, 20)
    return f


def parse_pigeon_response(index: int, data: bytes) -> PmPigeon:
    """``GetPigeonCommand.HandleResponse`` — opšta (ne-UK) grana."""
    pos = 2
    el_band = _ascii_field(data, pos, 8)
    pos += 8
    fed_len = 14 if len(data) in (53, 54, 60, 61, 70, 71) else 22
    fed_band = _ascii_field(data, pos, fed_len)
    pos += fed_len
    club_id = _ascii_field(data, pos, 4)
    pos += 4
    flight_id = _ascii_field(data, pos, 4)
    pos += 4
    positions: list[int] = []
    for _ in range(4):
        if pos + 1 < len(data):
            positions.append((data[pos] << 8) | data[pos + 1])
        else:
            positions.append(0)
        pos += 2
    date6 = _ascii_field(data, pos, 6)
    pos += 6
    time6 = _ascii_field(data, pos, 6)
    pos += 6
    evaluation = _ascii_field(data, pos, 1)
    pos += 1
    team: int | None = None
    if pos < len(data):
        pos += 1
    if pos < len(data):
        pos += 1
    if pos < len(data):
        team = data[pos]
    return PmPigeon(
        index=index,
        el_band=el_band.upper(),
        fed_band=fed_band,
        club_id=club_id,
        flight_id=flight_id,
        positions=(positions[0], positions[1], positions[2], positions[3]),
        arrival=_dt_from_strings(date6, time6),
        evaluation=evaluation,
        team=team,
        raw=bytes(data),
    )


def parse_timer_response(index: int, data: bytes) -> PmTimer:
    """``GetTimerCommand.HandleResponse``."""
    pos = 1
    club_id = _ascii_field(data, pos, 4)
    pos += 4
    flight_id = _ascii_field(data, pos, 4)
    pos += 4
    b_date = _ascii_field(data, pos, 6)
    pos += 6
    b_master_t = _ascii_field(data, pos, 6)
    pos += 6
    b_internal_t = _ascii_field(data, pos, 6)
    pos += 6
    b_diff = _ascii_field(data, pos, 4)
    pos += 4
    r_date = _ascii_field(data, pos, 6)
    pos += 6
    r_master_t = _ascii_field(data, pos, 6)
    pos += 6
    r_internal_t = _ascii_field(data, pos, 6)
    pos += 6
    r_diff = _ascii_field(data, pos, 4)
    pos += 4
    diff = _ascii_field(data, pos, 4)
    return PmTimer(
        index=index,
        club_id=club_id,
        flight_id=flight_id,
        basketing_master=_dt_from_strings(b_date, b_master_t),
        basketing_internal=_dt_from_strings(b_date, b_internal_t),
        basketing_diff=b_diff,
        readout_master=_dt_from_strings(r_date, r_master_t),
        readout_internal=_dt_from_strings(r_date, r_internal_t),
        readout_diff=r_diff,
        diff=diff,
        raw=bytes(data),
    )


def pm_discover_once(link: PmLink) -> PmDevice | None:
    """Jedan krug kao ``Communication.OnIdle``: Master (0x10) pa BA adrese 0x80..0x85."""
    for addr in (ADDR_MASTER, *BA_ADDRESSES):
        payload = PM_CMD_ACTIVATE_MASTER if addr == ADDR_MASTER else cmd_activate_ba(addr)
        try:
            resp = link.send(payload, PM_ACTIVATE_TIMEOUT_SEC)
        except PmTimeout:
            continue
        except PmFrameError as exc:
            logging.info("BRICON PM diag | phase=discover addr=0x%02X bad_frame=%s", addr, exc)
            continue
        logging.info(
            "BRICON PM diag | phase=discover addr=0x%02X status=%s data=%r",
            addr, resp.status, resp.data[:40],
        )
        if resp.ok:
            version = _ascii_field(resp.data, 0, len(resp.data))
            return PmDevice(addr=addr, version=version, model=pm_version_to_model(version, addr))
    return None


def pm_discover(
    link: PmLink,
    *,
    max_wait_sec: float,
    should_cancel: Callable[[], bool] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> PmDevice | None:
    """Ponavlja krugove otkrivanja dok se sat ne javi ili ne istekne vreme."""
    deadline = time.monotonic() + max(1.0, max_wait_sec)
    rounds = 0
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            return None
        dev = pm_discover_once(link)
        if dev is not None:
            return dev
        rounds += 1
        if on_phase is not None and rounds % 4 == 0:
            left = max(0.0, deadline - time.monotonic())
            on_phase(f"čekam BRICON sat na USB kablu… (još {left:.0f}s)")
        time.sleep(PM_DISCOVERY_ROUND_PAUSE_SEC)
    return None


def pm_read_all(
    link: PmLink,
    device: PmDevice,
    *,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PmReadout:
    """GetFancier -> GetPigeon(0..) -> GetTimer(0..), kao ``FancierReadOut.frm_OnPostCommands``."""

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    def _cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise RuntimeError("Otkazano od strane korisnika.")

    _phase(f"sat nađen ({device.model}) — čitam takmičara…")
    resp = link.command(cmd_get_fancier(device.addr))
    fancier = parse_fancier_response(resp.data) if resp.ok else PmFancier(raw=resp.data)
    if not resp.ok:
        logging.warning("BRICON PM: GetFancier status %s (%s)", resp.status, RS_NAMES.get(resp.status, "?"))
    out = PmReadout(device=device, fancier=fancier)

    _phase("čitam golubove…")
    for idx in range(PM_MAX_PIGEON_RECORDS):
        _cancel()
        resp = link.command(cmd_get_pigeon(device.addr, idx))
        if resp.status == RS_NO_MORE_RECORDS:
            break
        if not resp.ok:
            if idx == 0:
                logging.info("BRICON PM: nema golubova (status %s)", resp.status)
                break
            # Već imamo deo liste — ne bacaj ga; zabeleži i nastavi na vremena.
            msg = f"GetPigeon #{idx}: status {resp.status} ({RS_NAMES.get(resp.status, '?')}) — lista možda nepotpuna"
            logging.warning("BRICON PM: %s", msg)
            out.warnings.append(msg)
            break
        out.pigeons.append(parse_pigeon_response(idx, resp.data))
        if idx % 5 == 4:
            _phase(f"čitam golubove… ({idx + 1})")

    _phase("čitam vremena ukorpavanja/očitavanja…")
    for idx in range(PM_MAX_TIMER_RECORDS):
        _cancel()
        resp = link.command(cmd_get_timer(device.addr, idx))
        if resp.status == RS_NO_MORE_RECORDS:
            break
        if not resp.ok:
            if idx > 0:
                msg = f"GetTimer #{idx}: status {resp.status} ({RS_NAMES.get(resp.status, '?')})"
                logging.warning("BRICON PM: %s", msg)
                out.warnings.append(msg)
            else:
                logging.info("BRICON PM: nema vremena (status %s)", resp.status)
            break
        out.timers.append(parse_timer_response(idx, resp.data))
    return out


# --- Render u tekst „lista doleta“ -------------------------------------------------------------


# --- Kontrola sata (odstupanje internog sata od mastera) --------------------------------

# Iznad ovoga se u izveštaju i PDF-u ističe upozorenje (sekunde, apsolutno).
PM_CLOCK_DEVIATION_WARN_SEC = 60


def _deviation_sec(master: datetime | None, internal: datetime | None) -> int | None:
    """Interni sat minus master (klupski) sat, u sekundama; None ako vreme nije upisano."""
    if master is None or internal is None:
        return None
    return int(round((internal - master).total_seconds()))


def _fmt_dev(sec: int | None) -> str:
    if sec is None:
        return "—"
    sign = "+" if sec >= 0 else "−"
    a = abs(sec)
    return f"{sign}{a // 60}:{a % 60:02d} ({sign}{a} s)" if a >= 60 else f"{sign}{a} s"


def pm_clock_check(ro: PmReadout, *, warn_sec: int = PM_CLOCK_DEVIATION_WARN_SEC) -> dict[str, Any]:
    """
    Kao „kontrola“ kolona u Print Manageru: za svaki let poređenje internog vremena sata sa
    master vremenom pri ukorpavanju i pri očitavanju. Vraća sažetak za JSON/PDF.
    """
    rows: list[dict[str, Any]] = []
    worst: int | None = None
    for t in ro.timers:
        b = _deviation_sec(t.basketing_master, t.basketing_internal)
        r = _deviation_sec(t.readout_master, t.readout_internal)
        drift = (r - b) if (r is not None and b is not None) else None
        for v in (b, r, drift):
            if v is not None and (worst is None or abs(v) > abs(worst)):
                worst = v
        rows.append(
            {
                "index": t.index,
                "flight_id": t.flight_id,
                "club_id": t.club_id,
                "basketing_deviation_sec": b,
                "readout_deviation_sec": r,
                "drift_sec": drift,
            }
        )
    if not rows or worst is None:
        status = "nepoznato"
        ok = None
        message = "Kontrola sata: nema podataka o ukorpavanju/očitavanju u satu."
    elif abs(worst) > warn_sec:
        status = "upozorenje"
        ok = False
        message = (
            f"UPOZORENJE — odstupanje sata {_fmt_dev(worst)} prelazi dozvoljenih {warn_sec} s. "
            "Proverite sat i ukorpavanje pre obračuna."
        )
    else:
        status = "ok"
        ok = True
        message = f"Kontrola sata OK — najveće odstupanje {_fmt_dev(worst)} (granica {warn_sec} s)."
    return {
        "status": status,
        "ok": ok,
        "max_deviation_sec": worst,
        "warn_threshold_sec": warn_sec,
        "message": message,
        "flights": rows,
    }


def _fmt_dt(dt: datetime | None, with_date: bool) -> str:
    if dt is None:
        return "--/--/-- --:--:--" if with_date else "--:--:--"
    return dt.strftime("%d/%m/%y %H:%M:%S") if with_date else dt.strftime("%H:%M:%S")


def render_lista_doleta(ro: PmReadout) -> str:
    """
    Tekst u obliku štampane Speedy liste (vidi fixture) da postojeći parser
    (``parse_bricon_speedy_arrival_rows`` i dr.) radi bez izmena.
    """
    lines: list[str] = []
    lines.append(ro.read_at.strftime("%d/%m/%y-%H:%M:%S"))
    lic = ro.fancier.licence or "-"
    lines.append(f"{ro.device.model} - {lic} V {pm_version_digits(ro.device.version)}")
    lines.append(f"verzija sata: {ro.device.version.strip() or '-'} (adresa {ro.device.addr_name})")
    lines.append("lista doleta")
    lines.append(f"{lic} {ro.fancier.name}".strip())
    if ro.fancier.town:
        lines.append(ro.fancier.town)
    lines.append("")
    lines.append("let pakovanje kontrola")
    for t in ro.timers:
        lines.append(
            f"{t.index + 1} {t.flight_id or '-'} {t.club_id or '-'} "
            f"{_fmt_dt(t.basketing_master, True)} {_fmt_dt(t.basketing_internal, False)} "
            f"{_fmt_dt(t.readout_master, True)} {_fmt_dt(t.readout_internal, False)} "
            f"{t.basketing_diff or ''} {t.readout_diff or ''} {t.diff or ''}".rstrip()
        )
    check = pm_clock_check(ro)
    for row in check["flights"]:
        lines.append(
            f"kontrola let {row['index'] + 1}: ukorpavanje {_fmt_dev(row['basketing_deviation_sec'])}, "
            f"očitavanje {_fmt_dev(row['readout_deviation_sec'])}, hod {_fmt_dev(row['drift_sec'])}"
        )
    lines.append(check["message"])
    lines.append("")
    arrived = [p for p in ro.pigeons if p.arrived]
    arrived.sort(key=lambda p: p.arrival or datetime.max)
    for n, p in enumerate(arrived, start=1):
        assert p.arrival is not None
        lines.append(
            f"{n} {p.el_band} {p.fed_band} {p.arrival.strftime('%d/%m %H:%M:%S')} "
            f"let:{p.flight_id or '-'} {p.evaluation or 'OK'}".rstrip()
        )
    not_arrived = [p for p in ro.pigeons if not p.arrived]
    if not_arrived:
        lines.append("")
        lines.append("ukorpani bez dolaska:")
        for p in not_arrived:
            lines.append(f"- {p.el_band} {p.fed_band} let:{p.flight_id or '-'}")
    lines.append(f"pakovanje : {len(ro.pigeons)}")
    lines.append(f"doleteli : {len(arrived)}")
    for w in ro.warnings:
        lines.append(f"UPOZORENJE: {w}")
    lines.append("Takmicara")
    return "\n".join(lines) + "\n"


def readout_to_structured(ro: PmReadout) -> dict[str, Any]:
    """Strukturirani podaci za JSON (pored renderovanog teksta)."""
    return {
        "bricon_pm_device": {
            "address": f"0x{ro.device.addr:02X}",
            "address_name": ro.device.addr_name,
            "version": ro.device.version,
            "model": ro.device.model,
        },
        "bricon_pm_fancier": {
            "licence": ro.fancier.licence,
            "name": ro.fancier.name,
            "address": ro.fancier.address,
            "postcode": ro.fancier.postcode,
            "town": ro.fancier.town,
        },
        "bricon_pm_pigeons": [
            {
                "index": p.index,
                "el_band": p.el_band,
                "fed_band": p.fed_band,
                "club_id": p.club_id,
                "flight_id": p.flight_id,
                "positions": list(p.positions),
                "arrival": p.arrival.isoformat(timespec="seconds") if p.arrival else None,
                "evaluation": p.evaluation,
                "team": p.team,
                "raw_hex": p.raw.hex(),
            }
            for p in ro.pigeons
        ],
        "bricon_pm_warnings": list(ro.warnings),
        "bricon_clock_check": pm_clock_check(ro),
        "bricon_port": ro.port,
        "bricon_pm_timers": [
            {
                "index": t.index,
                "club_id": t.club_id,
                "flight_id": t.flight_id,
                "basketing_master": t.basketing_master.isoformat(timespec="seconds") if t.basketing_master else None,
                "basketing_internal": t.basketing_internal.isoformat(timespec="seconds") if t.basketing_internal else None,
                "basketing_diff": t.basketing_diff,
                "readout_master": t.readout_master.isoformat(timespec="seconds") if t.readout_master else None,
                "readout_internal": t.readout_internal.isoformat(timespec="seconds") if t.readout_internal else None,
                "readout_diff": t.readout_diff,
                "diff": t.diff,
                "raw_hex": t.raw.hex(),
            }
            for t in ro.timers
        ],
    }


# --- Javni ulaz -----------------------------------------------------------------------


def _ordered_unique_ports(port: str, candidate_ports: list[str] | tuple[str, ...] | None) -> list[str]:
    out: list[str] = []
    for p in (port, *(candidate_ports or ())):
        p = (p or "").strip().upper()
        if p and p not in out:
            out.append(p)
    return out


def pm_scan_ports(
    ports: list[str],
    *,
    max_wait_sec: float,
    on_bytes: Callable[[int], None] | None = None,
    capture: BriconComCapture | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> tuple[str, Any, PmLink, PmDevice] | None:
    """
    „COM: Auto“ kao u Print Manageru (``Communication.NextComport``): redom na svakom portu
    jedan krug otkrivanja (Master + BA adrese, ~1 s), pa sledeći port, dok se sat ne javi
    ili ne istekne vreme. Vraća (port, otvoren serijski port, link, uređaj) — port ostaje
    otvoren za čitanje. Port koji ne može da se otvori (zauzet / nestao) se preskače.
    """
    deadline = time.monotonic() + max(1.0, max_wait_sec)
    dead: set[str] = set()
    rounds = 0
    while time.monotonic() < deadline:
        live = [p for p in ports if p not in dead]
        if not live:
            return None
        for p in live:
            if should_cancel is not None and should_cancel():
                return None
            try:
                ser = open_pm_serial(p, PM_BAUD_DEFAULT)
            except Exception as exc:
                logging.info("BRICON PM auto-port: %s se ne otvara (%s: %s) — preskačem", p, type(exc).__name__, exc)
                dead.add(p)
                continue
            link = PmLink(wrap_serial_for_capture(ser, capture), on_rx_bytes=on_bytes)
            try:
                dev = pm_discover_once(link)
            except Exception as exc:
                logging.info("BRICON PM auto-port: %s greška (%s: %s)", p, type(exc).__name__, exc)
                dev = None
            if dev is not None:
                logging.info("BRICON PM auto-port: sat nađen na %s (%s)", p, dev.model)
                return p, ser, link, dev
            try:
                ser.close()
            except Exception:
                pass
        rounds += 1
        if on_phase is not None:
            left = max(0.0, deadline - time.monotonic())
            on_phase(
                f"tražim BRICON sat na portovima {', '.join(live)}… (krug {rounds}, još {left:.0f}s)"
            )
        time.sleep(PM_DISCOVERY_ROUND_PAUSE_SEC)
    return None


def read_readout_via_print_manager_protocol(
    port: str,
    *,
    listen_sec: float = 120.0,
    on_bytes: Callable[[int], None] | None = None,
    capture: BriconComCapture | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    candidate_ports: list[str] | tuple[str, ...] | None = None,
) -> PmReadout:
    """
    Ceo PM tok: otvori port (9600 8N2), čekaj da se sat javi (do ``listen_sec``), pročitaj
    takmičara, golubove i vremena. Ako je zadato više portova (``candidate_ports``), radi
    kao „COM: Auto“ u Print Manageru — sat se traži redom na svim portovima, počevši od
    ``port``. Baca ``RuntimeError`` sa jasnim objašnjenjem ako sat nije nađen ili je prenos
    pukao; delimični binarni odgovori se čuvaju u dijagnostici.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial")

    ports = _ordered_unique_ports(port, candidate_ports)
    if not ports:
        raise RuntimeError("Nije izabran nijedan COM port.")

    if capture is None and bricon_capture_enabled():
        capture = start_capture_session(ports[0], note="TAURIS_BRICON_CAPTURE=1")

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    ser: Any = None
    link: PmLink | None = None
    device: PmDevice | None = None
    found_port = ports[0]
    if len(ports) == 1:
        _phase(f"BRICON PM: otvaram {found_port} (9600 baud, 8N2)…")
        ser = open_pm_serial(found_port, PM_BAUD_DEFAULT)
        link = PmLink(wrap_serial_for_capture(ser, capture), on_rx_bytes=on_bytes)
        _phase("BRICON PM: tražim sat (Master 0x10, BA 0x80–0x85)…")
        try:
            device = pm_discover(
                link, max_wait_sec=listen_sec, should_cancel=should_cancel, on_phase=on_phase
            )
        except Exception:
            try:
                ser.close()
            except Exception:
                pass
            raise
    else:
        _phase(f"BRICON PM: automatski tražim sat na {len(ports)} porta ({', '.join(ports)})…")
        hit = pm_scan_ports(
            ports,
            max_wait_sec=listen_sec,
            on_bytes=on_bytes,
            capture=capture,
            should_cancel=should_cancel,
            on_phase=on_phase,
        )
        if hit is not None:
            found_port, ser, link, device = hit

    if device is None or link is None:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass
        if should_cancel is not None and should_cancel():
            raise RuntimeError("Otkazano od strane korisnika.")
        where = f"Port: {ports[0]}" if len(ports) == 1 else f"Probani portovi: {', '.join(ports)}"
        last_err = link.last_frame_error if link is not None else None
        raise RuntimeError(
            "BRICON sat se nije javio na Print Manager protokol (9600 baud, 8N2).\n"
            f"{where}. Poslednja greška okvira: {last_err or '-'}.\n"
            "Proverite: USB kabl i FTDI drajver, da je Print Manager zatvoren, "
            "da je sat uključen i priključen pre klika na «Preuzmi BRICON»."
        )

    failed = False
    try:
        logging.info(
            "BRICON PM diag | phase=device_found port=%s addr=0x%02X version=%r model=%s",
            found_port, device.addr, device.version, device.model,
        )
        if found_port != ports[0]:
            _phase(f"sat nađen na {found_port} (umesto izabranog {ports[0]})…")
        try:
            readout = pm_read_all(link, device, on_phase=on_phase, should_cancel=should_cancel)
        except (PmTimeout, PmFrameError, PmProtocolError) as exc:
            failed = True
            raise RuntimeError(
                f"BRICON prenos je prekinut usred čitanja: {exc}.\n"
                "Ostavite sat priključen i pokušajte ponovo."
            ) from exc
        readout.port = found_port
        logging.info(
            "BRICON PM diag | phase=readout_ok port=%s pigeons=%s timers=%s rx=%sB",
            found_port, len(readout.pigeons), len(readout.timers), link.total_rx,
        )
        return readout
    finally:
        try:
            ser.close()
        except Exception:
            pass
        if failed and link.rx_history:
            try:
                save_partial_payload_diagnostic(
                    found_port, bytes(link.rx_history), reason="PM prenos prekinut — sirovi odgovori sata"
                )
            except Exception:
                logging.exception("BRICON PM: čuvanje dijagnostike nije uspelo.")


def read_raw_via_print_manager_protocol(
    port: str,
    *,
    listen_sec: float = 120.0,
    on_bytes: Callable[[int], None] | None = None,
    capture: BriconComCapture | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bytes:
    """Kompatibilan ulaz: vraća renderovanu „lista doleta“ kao UTF-8 bajtove."""
    ro = read_readout_via_print_manager_protocol(
        port,
        listen_sec=listen_sec,
        on_bytes=on_bytes,
        capture=capture,
        on_phase=on_phase,
        should_cancel=should_cancel,
    )
    return render_lista_doleta(ro).encode("utf-8")


def try_print_manager_protocol_readout(
    config: TaurisConnectionConfig,
    *,
    on_com_bytes: Callable[[int], None] | None = None,
    max_listen_sec: float = 120.0,
    capture: BriconComCapture | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> PmReadout | None:
    """Vraća ``PmReadout`` ako PM protokol uspe, inače ``None`` (poziva se pasivni fallback)."""
    try:
        return read_readout_via_print_manager_protocol(
            config.port,
            listen_sec=max_listen_sec,
            on_bytes=on_com_bytes,
            capture=capture,
            on_phase=on_phase,
            should_cancel=should_cancel,
            candidate_ports=getattr(config, "candidate_ports", None) or None,
        )
    except Exception as exc:
        if should_cancel is not None and should_cancel():
            from tauris_desktop.bricon import BriconCancelled

            raise BriconCancelled("Otkazano od strane korisnika.") from exc
        logging.info("BRICON PM protokol nije uspeo: %s: %s", type(exc).__name__, exc)
        if on_phase is not None:
            on_phase(f"PM protokol nije uspeo ({exc}) — probam pasivno slušanje…")
        return None
