"""
BRICON Print Manager binarni protokol (iz analize BriconLib.dll).

Print Manager ne šalje običan tekst — koristi okvire sa kontrolnim bajtovima,
SendVersionInfo rukovanje i prebacivanje baud brzine (9600 / 38400) prema odgovoru sata.
"""

from __future__ import annotations

import logging
import time
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

# Odgovor sata (BriconLib.Communications.ResponseStatus)
RS_SWITCH_TO_9600 = 113
RS_SWITCH_TO_38400 = 114
RS_OK = 0
RS_CHECKSUM_ERROR = 32
RS_PC_TIMEOUT = 253

# Paket iz SendVersionInfoCommand.ToBytes("Print Manager USB", 1, 1, 0)
PM_SEND_VERSION_INFO = bytes(
    [
        0x10,
        0x70,
        0x13,
        0x02,
        0x01,
        0x01,
        0x00,
        0x50,
        0x72,
        0x69,
        0x6E,
        0x74,
        0x20,
        0x4D,
        0x61,
        0x6E,
        0x61,
        0x67,
        0x65,
        0x72,
        0x20,
        0x55,
    ]
)

# Komande bez parametara (ToBytes iz BriconLib.dll — instalirani Print Manager)
PM_CMD_START_READOUT = bytes([0x10, 0xE1, 0x00])
PM_CMD_START_TEMP_READOUT = bytes([0x10, 0xE2, 0x00])
PM_CMD_GET_FANCIER_COUNT = bytes([0xFF, 0xFA, 0x02, 0x0A, 0x00])
PM_CMD_GET_FLIGHT_NAMES = bytes([0xFF, 0xFA, 0x02, 0x0C, 0x00])
PM_CMD_GET_FANCIER = bytes([0xFF, 0x51, 0x01, 0x00])

PM_BAUD_INITIAL_CANDIDATES = (38400, 9600)
PM_POST_COMMAND_DELAY_SEC = 0.12
PM_RESPONSE_WAIT_SEC = 2.5
PM_COMMAND_RESPONSE_WAIT_SEC = 0.55
PM_CAPTURE_MIN_LISTEN_SEC = 5.0


def _prepare_pm_serial(ser: Any) -> None:
    """Deljena implementacija — vidi lib.prepare_serial_port_for_bricon (mode='pm_protocol')."""
    prepare_serial_port_for_bricon(ser, mode="pm_protocol")


def _read_available(
    ser: Any,
    wait_sec: float,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> bytes:
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


def _detect_baud_switch_status(data: bytes) -> int | None:
    if not data:
        return None
    if RS_SWITCH_TO_9600 in data:
        return 9600
    if RS_SWITCH_TO_38400 in data:
        return 38400
    return None


def _apply_baud(ser: Any, baud: int) -> None:
    ser.baudrate = baud
    time.sleep(0.08)
    _prepare_pm_serial(ser)


def pm_handshake(ser: Any, *, initial_baud: int) -> int | None:
    """
    PM StartComm + SendVersionInfo.
    Vraća baud na kom treba nastaviti (9600 ili 38400), ili None ako nema odgovora.
    """
    _apply_baud(ser, initial_baud)
    try:
        ser.write(PM_SEND_VERSION_INFO)
        ser.flush()
    except Exception as exc:
        logging.warning(
            "BRICON PM diag | phase=handshake_write baud=%s exc=%s: %s",
            initial_baud, type(exc).__name__, exc,
        )
        raise
    resp = _read_available(ser, PM_RESPONSE_WAIT_SEC)
    if resp:
        logging.info("BRICON PM handshake @ %s: primljeno %s bajtova", initial_baud, len(resp))
        logging.debug("BRICON PM handshake odgovor: %s", resp[:64].hex())
    switch = _detect_baud_switch_status(resp)
    if switch:
        _apply_baud(ser, switch)
        return switch
    if resp:
        return initial_baud
    return None


def pm_send_readout_commands(ser: Any) -> None:
    """
    Niz komandi kao u Print Manageru «Uitlezen» / Read out.

    StartReadoutCommand (10-E1-00) iz instaliranog BriconLib.dll — bez nje sat
    često ne prelazi u slave režim za prenos liste.
    """
    for payload in (
        PM_CMD_START_READOUT,
        PM_CMD_GET_FANCIER_COUNT,
        PM_CMD_GET_FLIGHT_NAMES,
        PM_CMD_GET_FANCIER,
    ):
        try:
            ser.write(payload)
            ser.flush()
        except Exception as exc:
            logging.warning(
                "BRICON PM diag | phase=send_command cmd=%s exc=%s: %s",
                payload.hex(), type(exc).__name__, exc,
            )
            raise
        time.sleep(PM_POST_COMMAND_DELAY_SEC)
        resp = _read_available(ser, PM_COMMAND_RESPONSE_WAIT_SEC)
        if payload == PM_CMD_START_READOUT and resp:
            logging.info("BRICON PM StartReadout odgovor: %s bajtova", len(resp))


def capture_pm_readout_session(
    ser: Any,
    *,
    listen_sec: float,
    on_bytes: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bytes:
    """Slušanje dok korisnik na satu pokrene Read out (slave mode).

    Prekid konekcije usred slušanja se ODMAH prekida (ne čeka se pun idle prag) ali
    se već primljeni bajtovi uvek vraćaju — pozivalac ih koristi za dijagnostiku
    čak i kad prenos nije kompletan.
    """
    raw = bytearray()
    deadline = time.monotonic() + max(PM_CAPTURE_MIN_LISTEN_SEC, listen_sec)
    idle_rounds = 0
    disconnect: list[BaseException] = []
    while time.monotonic() < deadline:
        if should_cancel is not None and should_cancel():
            logging.info(
                "BRICON PM diag | phase=cancelled bytes=%s", len(raw)
            )
            break
        chunk = _read_available(ser, 0.45, on_error=disconnect.append)
        if disconnect:
            logging.warning(
                "BRICON PM diag | phase=disconnected bytes=%s exc=%s: %s",
                len(raw), type(disconnect[0]).__name__, disconnect[0],
            )
            break
        if chunk:
            raw.extend(chunk)
            idle_rounds = 0
            if on_bytes:
                on_bytes(len(raw))
        else:
            idle_rounds += 1
            if len(raw) > 80 and idle_rounds >= 8:
                break
    return bytes(raw)


def read_raw_via_print_manager_protocol(
    port: str,
    *,
    listen_sec: float = 120.0,
    on_bytes: Callable[[int], None] | None = None,
    capture: BriconComCapture | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bytes:
    """
    Pokušaj čitanja kao Print Manager: handshake + komande + slušanje.

    Čuva najduži parcijalni prijem preko svih probanih baud-ova (``best_partial``) i,
    ako protokol na kraju ne uspe, snima ga u dijagnostički fajl umesto da ga
    jednostavno odbaci — terenska dijagnoza (npr. sat se javio ali prenos nije
    završen) ne sme da ostane bez traga.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial")

    if capture is None and bricon_capture_enabled():
        capture = start_capture_session(port, note="TAURIS_BRICON_CAPTURE=1")

    last_error: str | None = None
    best_partial = bytearray()
    for initial in PM_BAUD_INITIAL_CANDIDATES:
        if should_cancel is not None and should_cancel():
            raise RuntimeError("Otkazano od strane korisnika.")
        ser = None
        try:
            if on_phase:
                on_phase(f"BRICON PM: povezivanje na {port} @ {initial} baud…")
            ser = serial.Serial(
                port=port,
                baudrate=initial,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.2,
            )
            wser = wrap_serial_for_capture(ser, capture)
            _prepare_pm_serial(wser)
            if on_phase:
                on_phase(f"BRICON PM: handshake @ {initial} baud…")
            active_baud = pm_handshake(wser, initial_baud=initial)
            if active_baud is None:
                logging.warning(
                    "BRICON PM diag | phase=handshake port=%s baud=%s rezultat=nema_odgovora",
                    port, initial,
                )
                continue
            pm_send_readout_commands(wser)
            if on_phase:
                on_phase(f"BRICON PM: čekam Read out na satu (aktivan baud {active_baud})…")
            raw = capture_pm_readout_session(
                wser,
                listen_sec=listen_sec,
                on_bytes=on_bytes,
                should_cancel=should_cancel,
            )
            if len(raw) > len(best_partial):
                best_partial = bytearray(raw)
            if len(raw) >= 40:
                logging.info(
                    "BRICON PM diag | phase=readout_ok port=%s baud_start=%s baud_active=%s bytes=%s",
                    port, initial, active_baud, len(raw),
                )
                return raw
            last_error = f"Premalo podataka posle PM protokola @ {initial} baud ({len(raw)} B)"
            logging.info(
                "BRICON PM diag | phase=readout_incomplete port=%s baud=%s bytes=%s",
                port, initial, len(raw),
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logging.warning(
                "BRICON PM diag | phase=attempt_failed port=%s baud=%s exc=%s: %s",
                port, initial, type(exc).__name__, exc,
            )
        finally:
            try:
                if ser is not None:
                    ser.close()
            except Exception:
                pass

    if best_partial:
        diag_path = save_partial_payload_diagnostic(
            port, bytes(best_partial), reason=last_error or "PM protokol nije uspeo"
        )
        raise RuntimeError(
            f"{last_error or 'BRICON Print Manager protokol nije uspeo'}\n"
            f"Parcijalni podaci ({len(best_partial)} B) sačuvani za dijagnostiku: {diag_path}"
        )
    raise RuntimeError(last_error or "BRICON Print Manager protokol nije uspeo")


def try_print_manager_protocol_readout(
    config: TaurisConnectionConfig,
    *,
    on_com_bytes: Callable[[int], None] | None = None,
    max_listen_sec: float = 120.0,
    capture: BriconComCapture | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> bytes | None:
    """Vraća sirove bajtove ako PM protokol uspe, inače None (fallback na pasivno slušanje)."""
    try:
        return read_raw_via_print_manager_protocol(
            config.port,
            listen_sec=max_listen_sec,
            on_bytes=on_com_bytes,
            capture=capture,
            on_phase=on_phase,
            should_cancel=should_cancel,
        )
    except Exception as exc:
        logging.info("BRICON PM protokol preskočen: %s: %s", type(exc).__name__, exc)
        return None
