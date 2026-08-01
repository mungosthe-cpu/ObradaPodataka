"""
Snimanje BRICON COM saobraćaja (TX/RX) za dijagnostiku i upoređivanje sa Print Managerom.

Logovi: <app_data>/logs/bricon_capture/bricon_COM3_YYYYMMDD_HHMMSS.log
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tauris_desktop.lib import app_data_dir

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    serial = None

_CAPTURE_ACTIVE: BriconComCapture | None = None


def bricon_capture_enabled() -> bool:
    v = os.environ.get("TAURIS_BRICON_CAPTURE", "").strip().lower()
    return v in ("1", "true", "yes", "da", "on")


def bricon_capture_dir() -> Path:
    d = app_data_dir() / "logs" / "bricon_capture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_capture() -> BriconComCapture | None:
    return _CAPTURE_ACTIVE


class BriconComCapture:
    """Upisuje svaki TX/RX sa vremenom, baud-om i HEX/ASCII prikazom."""

    def __init__(self, port: str, *, note: str = "") -> None:
        self.port = (port or "").strip().upper()
        safe = re.sub(r"[^\w\-]+", "_", self.port)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = bricon_capture_dir() / f"bricon_{safe}_{stamp}.log"
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")
        self._t0 = time.monotonic()
        self.total_tx = 0
        self.total_rx = 0
        self._write_header(note)

    def _elapsed(self) -> str:
        return f"{time.monotonic() - self._t0:8.3f}s"

    def _write_header(self, note: str) -> None:
        self._fh.write(
            f"# BRICON COM snimak — port {self.port}\n"
            f"# Pocetak: {datetime.now().isoformat(timespec='seconds')}\n"
        )
        if note:
            self._fh.write(f"# {note}\n")
        self._fh.write("# Format: [elapsed] DIR len @baud | hex | ascii\n")
        self._fh.flush()

    @staticmethod
    def _ascii_preview(data: bytes, limit: int = 96) -> str:
        out: list[str] = []
        for b in data[:limit]:
            out.append(chr(b) if 32 <= b < 127 else ".")
        s = "".join(out)
        if len(data) > limit:
            s += "…"
        return s

    @staticmethod
    def _hex_line(data: bytes, width: int = 32) -> str:
        if not data:
            return ""
        lines: list[str] = []
        for i in range(0, len(data), width):
            chunk = data[i : i + width]
            lines.append(chunk.hex(" "))
        return "\n".join(f"    {ln}" for ln in lines)

    def log_note(self, message: str) -> None:
        self._fh.write(f"[{self._elapsed()}] NOTE {message}\n")
        self._fh.flush()
        logging.info("BRICON capture: %s", message)

    def log_tx(self, data: bytes, baud: int = 0) -> None:
        if not data:
            return
        self.total_tx += len(data)
        self._fh.write(
            f"[{self._elapsed()}] TX {len(data)} B @ {baud or '?'} baud\n"
            f"{self._hex_line(data)}\n"
            f"    ASCII: {self._ascii_preview(data)}\n"
        )
        self._fh.flush()

    def log_rx(self, data: bytes, baud: int = 0) -> None:
        if not data:
            return
        self.total_rx += len(data)
        self._fh.write(
            f"[{self._elapsed()}] RX {len(data)} B @ {baud or '?'} baud\n"
            f"{self._hex_line(data)}\n"
            f"    ASCII: {self._ascii_preview(data)}\n"
        )
        self._fh.flush()

    def close(self, *, summary: str = "") -> Path:
        self._fh.write(
            f"\n# Kraj: {datetime.now().isoformat(timespec='seconds')}\n"
            f"# Ukupno TX={self.total_tx} RX={self.total_rx} B\n"
        )
        if summary:
            self._fh.write(f"# {summary}\n")
        self._fh.close()
        logging.info("BRICON COM snimak sacuvan: %s (TX=%s RX=%s)", self.path, self.total_tx, self.total_rx)
        return self.path

    def __enter__(self) -> BriconComCapture:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class CapturingSerialWrapper:
    """Omotac oko pyserial — loguje svaki write/read."""

    def __init__(self, ser: Any, capture: BriconComCapture) -> None:
        object.__setattr__(self, "_ser", ser)
        object.__setattr__(self, "_cap", capture)

    @property
    def baudrate(self) -> int:
        return int(self._ser.baudrate)

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        old = getattr(self._ser, "baudrate", 0)
        self._ser.baudrate = value
        if old != value:
            self._cap.log_note(f"baudrate {old} -> {value}")

    def write(self, data: bytes | bytearray) -> int:
        payload = bytes(data)
        self._cap.log_tx(payload, self.baudrate)
        return self._ser.write(payload)

    def read(self, size: int = 1) -> bytes:
        data = self._ser.read(size)
        if data:
            self._cap.log_rx(bytes(data), self.baudrate)
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ser, name)


def wrap_serial_for_capture(ser: Any, capture: BriconComCapture | None) -> Any:
    if capture is None:
        return ser
    return CapturingSerialWrapper(ser, capture)


def start_capture_session(port: str, *, note: str = "") -> BriconComCapture:
    global _CAPTURE_ACTIVE
    cap = BriconComCapture(port, note=note)
    _CAPTURE_ACTIVE = cap
    return cap


def end_capture_session(cap: BriconComCapture | None, *, summary: str = "") -> Path | None:
    global _CAPTURE_ACTIVE
    if cap is None:
        return None
    path = cap.close(summary=summary)
    if _CAPTURE_ACTIVE is cap:
        _CAPTURE_ACTIVE = None
    return path


def save_partial_payload_diagnostic(port: str, raw: bytes, *, reason: str = "") -> Path:
    """
    Snima parcijalno primljene bajtove kada BRICON prenos ne uspe do kraja — nezavisno od
    opcione ``TAURIS_BRICON_CAPTURE`` snimalice, da terenska dijagnostika uvek ima trag
    onoga što je stiglo sa uređaja, čak i kad korisnik nije uključio detaljni COM snimak.
    Ne sadrži lozinke/tajne — samo sirove bajtove primljene sa serijskog porta.
    """
    safe = re.sub(r"[^\w\-]+", "_", (port or "?").strip().upper())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = bricon_capture_dir() / f"bricon_partial_{safe}_{stamp}.log"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"# BRICON parcijalni podaci — port {port}\n"
            f"# Vreme: {datetime.now().isoformat(timespec='seconds')}\n"
            f"# Razlog neuspeha: {reason}\n"
            f"# Duzina: {len(raw)} B\n"
            f"# Format: hex | ascii\n"
        )
        fh.write(BriconComCapture._hex_line(raw) + "\n")
        fh.write(f"    ASCII: {BriconComCapture._ascii_preview(raw, limit=len(raw) or 1)}\n")
    logging.info("BRICON parcijalni podaci sacuvani: %s (%s B)", path, len(raw))
    return path


def save_race_data_recovery_snapshot(race_data: dict, *, port: str, reason: str = "") -> Path:
    """
    Snima već pročitane BRICON podatke (race_data) kao JSON ako sledeći korak posle
    uspešnog čitanja (PDF/zaključavanje/slanje) padne — preuzimanje sa uređaja je često
    jednokratno (korisnik mora ponovo da pokrene Read out na satu), pa se već primljena
    lista ne sme izgubiti samo zato što je kasniji korak u obradi pukao.
    """
    safe = re.sub(r"[^\w\-]+", "_", (port or "?").strip().upper())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = bricon_capture_dir() / f"bricon_racedata_recovery_{safe}_{stamp}.json"
    payload = {
        "_recovery_reason": reason,
        "_recovery_time": datetime.now().isoformat(timespec="seconds"),
        "_recovery_port": port,
        "race_data": race_data,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    logging.info("BRICON race_data recovery snimak sacuvan: %s", path)
    return path


def run_com_capture_session(
    port: str,
    *,
    duration_sec: float = 180.0,
    use_pm_protocol: bool = True,
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[Path, str]:
    """
    Snima COM sesiju: PM handshake + komande + slusanje (kao «Preuzmi BRICON»).
    Print Manager mora biti zatvoren — isti port ne moze dva programa.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial")

    verify_com_port_available(port)
    cap = start_capture_session(
        port,
        note="Dijagnostika — zatvorite Print Manager pre snimanja",
    )
    cap.log_note(
        "Posle starta pokrenite Read out / Uitlezen na satu (slave USB, ne stampac)"
    )
    raw = bytearray()
    try:
        if use_pm_protocol:
            from tauris_desktop.bricon_pm import read_raw_via_print_manager_protocol

            def _notify(n: int) -> None:
                if on_bytes:
                    on_bytes(n)

            chunk = read_raw_via_print_manager_protocol(
                port,
                listen_sec=duration_sec,
                on_bytes=_notify,
                capture=cap,
            )
            raw.extend(chunk)
        else:
            from tauris_desktop.bricon_pm import PM_BAUD_INITIAL_CANDIDATES, _prepare_pm_serial, _read_available

            for baud in PM_BAUD_INITIAL_CANDIDATES:
                ser = serial.Serial(
                    port=port,
                    baudrate=baud,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=0.2,
                )
                wser = wrap_serial_for_capture(ser, cap)
                try:
                    _prepare_pm_serial(wser)
                    cap.log_note(f"pasivno slusanje @ {baud}")
                    deadline = time.monotonic() + duration_sec
                    while time.monotonic() < deadline:
                        part = _read_available(wser, 0.5)
                        if part:
                            raw.extend(part)
                            if on_bytes:
                                on_bytes(len(raw))
                finally:
                    ser.close()

        summary = (
            f"TX={cap.total_tx} RX={cap.total_rx} payload={len(raw)} B; "
            f"trajanje ~{duration_sec:.0f}s"
        )
        if len(raw) >= 40:
            cap.log_note(f"ukupno primljeno {len(raw)} B korisnog payload-a")
        else:
            cap.log_note("malo ili nema podataka — proverite Read out na satu i port")
        path = end_capture_session(cap, summary=summary) or cap.path
        return path, summary
    except Exception:
        end_capture_session(cap, summary="prekinuto greskom")
        raise


def analyze_capture_file(path: Path) -> str:
    """Kratka analiza snimka — PM paketi, baud status, tekst."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tx_count = sum(1 for ln in lines if "] TX " in ln)
    rx_count = sum(1 for ln in lines if "] RX " in ln)
    raw_hex = re.findall(r"^    ([0-9a-f ]+)$", text, re.MULTILINE)
    all_bytes = bytearray()
    for hx in raw_hex[:200]:
        try:
            all_bytes.extend(bytes.fromhex(hx.replace(" ", "")))
        except ValueError:
            pass
    blob = bytes(all_bytes)
    notes: list[str] = [
        f"Fajl: {path}",
        f"TX blokova: {tx_count}, RX blokova: {rx_count}",
    ]
    if bytes([0x10, 0x70, 0x13]) in blob:
        notes.append("[OK] Prepoznat PM SendVersionInfo (10 70 13 …)")
    if 113 in blob:
        notes.append("[OK] Sat trazi prelazak na 9600 baud (status 113)")
    if 114 in blob:
        notes.append("[OK] Sat trazi prelazak na 38400 baud (status 114)")
    if b"\xff\xfa" in blob.lower():
        notes.append("[OK] FF FA komande (PM GetFancier/Flight)")
    if b"lista doleta" in blob.lower() or b"bricon speedy" in blob.lower():
        notes.append("[OK] Tekst liste doleta u RX")
    try:
        from tauris_desktop.bricon import decode_bricon_payload, validate_bricon_payload

        decoded = decode_bricon_payload(blob, quiet=True)
        validate_bricon_payload(decoded)
        notes.append("[OK] Payload bi se parsirao kao BRICON lista")
    except Exception as exc:
        notes.append(f"Parser: {exc}")
    return "\n".join(notes)


def verify_com_port_available(port: str) -> None:
    from tauris_desktop.lib import verify_com_port_available as _v

    _v(port)
