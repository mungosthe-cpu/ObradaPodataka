"""
BRICON sat (X-treme, Speedy, klubski EC) — čitanje preko COM/USB-CDC i uvoz iz fajla.

Zvanični softver (Print Manager, Club Editor) koristi USB; na Windows-u to je često
virtuelni COM port. Protokol nije javan — podržani su pasivno slušanje (kao TAURIS),
više encodinga, JSON/CSV i tipični tekstualni ispisi sa oznakom BRICON / redovima dolaska.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tauris_desktop.config import (
    BRICON_READ_IDLE_EMPTY_READS,
    BRICON_READ_IDLE_EMPTY_READS_SLOW,
    BRICON_READ_MAX_ITERATIONS,
    BRICON_READ_MAX_ITERATIONS_SLOW,
    BRICON_READ_TIMEOUT_SEC,
    BRICON_READ_TIMEOUT_SEC_SLOW,
    BRICON_TAIL_EXTRA_MAX_ITER,
    BRICON_TAIL_EXTRA_MAX_ITER_SLOW,
    BRICON_TAIL_IDLE_EMPTY_READS,
    BRICON_TAIL_IDLE_EMPTY_READS_SLOW,
    BRICON_USB_BAUD_CANDIDATES,
    BRICON_USB_PER_BAUD_MIN_SEC,
    BRICON_USB_READOUT_MAX_SEC,
    BRICON_USB_READOUT_MIN_TOTAL_SEC,
    TaurisConnectionConfig,
)
from tauris_desktop.lib import (
    com_port_description_blob,
    prepare_serial_port_for_bricon,
    read_serial_bytes_until_idle,
    verify_com_port_available,
)

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover
    serial = None

BRICON_FILE_SUFFIXES = (".txt", ".csv", ".log", ".brc", ".bricon", ".dat", ".export")

# Opcioni bajtovi koje neki USB-CDC uređaji očekuju pre slanja (bez garancije).
BRICON_OPTIONAL_WAKE_BYTES: tuple[bytes, ...] = (
    b"\r\n",
    b"READ\r\n",
    b"READOUT\r\n",
    b"?\r\n",
)

_RE_FANCIER = re.compile(
    r"(?im)^\s*(?:fancier|owner|member|naam|name|takmi[cč]ar|duivenliefhebber|"
    r"klub|loft)\s*[:=]\s*(.+?)\s*$"
)
_RE_RACE_NAME = re.compile(
    r"(?im)^\s*(?:race|competition|wedstrijd|trka|event)\s*[:=]\s*(.+?)\s*$"
)
_RE_CLOCKING_CSV = re.compile(
    r"(?im)^\s*(\d+)\s*[,;]\s*([^,;]+?)\s*[,;]\s*(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)"
    r"(?:\s*[,;]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}))?"
)
_RE_CLOCKING_SPACED = re.compile(
    r"(?im)^\s*(\d{1,4})\s+([A-Z]{2,4}[\s\-]?\d{2,4}[\s\-]?\d{3,10})\s+"
    r"(\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)"
)
_RE_CLOCKING_SEMI = re.compile(
    r"(?im)ring\s*;\s*([^;]+?)\s*;\s*time\s*;\s*(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)"
    r"(?:\s*;\s*date\s*;\s*([^;]+))?",
    re.IGNORECASE,
)
_RE_RING_TIME_LOOSE = re.compile(
    r"(?im)([A-Z]{2,4}[\s\-]?\d{2,4}[\s\-]?\d{3,10}).{0,24}?(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)"
)

# BRICON Speedy — štampana / izvezena «lista doleta» (primer: C5397 KUSIC DRAGAN, chip + SR ring)
_RE_SPEEDY_DEVICE = re.compile(
    r"(?is)bricon\s+speedy\s*[-–]\s*(C\d{3,7})\s+V\s*(\d+)",
)
_RE_LISTA_DOLETA = re.compile(r"(?im)lista\s+doleta")
_RE_SPEEDY_MEMBER = re.compile(
    r"(?im)^\s*(C\d{3,7})\s+([A-ZČĆŽŠĐ][A-ZČĆŽŠĐa-zčćžšđ\s\-]{2,80})\s*$",
)
_RE_PAKOVANJE_COUNT = re.compile(r"(?im)pakovanje\s*:\s*(\d+)\s*$")
_RE_SPEEDY_ARRIVAL_ROW = re.compile(
    r"(?im)^\s*"
    r"(\d{1,4})\s+"
    r"([A-F0-9]{8})\s+"
    r"([A-Z]{2}-\d{2}-\d{6,14})\s+"
    r"([A-Z])\s+"
    r"([A-Z]{2,10})\s+"
    r"(\d{2}/\d{2})\s+"
    r"(\d{2}:\d{2}:\d{2})"
    r"(?:\s+:[0-9A-F]{2}:\s*\d:\s*)*"
    r"(?:\s+(OK|ERR|FAIL))?\s*$",
)


def _score_bricon_text(text: str, *, clocking_row_count: int | None = None) -> int:
    """
    ``clocking_row_count`` \u2014 prosledi ve\u0107 izra\u010dunat broj redova (npr. iz
    ``_bricon_payload_ready``) da se ceo tekst ne parsira regex-ima iznova; bez toga
    poziv sam ra\u010duna (isto pona\u0161anje kao ranije).
    """
    if not (text or "").strip():
        return -(10**9)
    low = text.lower()
    sc = 0
    for kw, pts in (
        ("bricon", 800),
        ("x-treme", 400),
        ("xtreme", 400),
        ("speedy", 350),
        ("unives", 300),
        ("clocking", 250),
        ("fancier", 200),
        ("ring", 120),
        ("arrival", 120),
        ("antenna", 100),
        ("bel", 80),
        ("lista doleta", 500),
        ("pakovanje", 150),
        ("sigurnosni kod", 120),
    ):
        if kw in low:
            sc += pts
    if _RE_SPEEDY_DEVICE.search(text):
        sc += 400
    rows = (
        clocking_row_count
        if clocking_row_count is not None
        else len(parse_bricon_clocking_rows(text))
    )
    sc += rows * 45
    sc -= text.count("\ufffd") * 120
    return sc


def decode_bricon_payload(raw: bytes, *, quiet: bool = False) -> str:
    """Izbor encodinga optimizovan za BRICON / belgijski ispis."""
    if not raw:
        return ""
    from tauris_desktop.lib import _DECODE_PRIORITY  # lokalni import da izbegnemo kružnu zavisnost pri učitavanju

    candidates: list[tuple[str, str]] = []
    for label, dec in (
        ("utf-8", lambda b: b.decode("utf-8")),
        ("cp1252", lambda b: b.decode("cp1252")),
        ("cp1250", lambda b: b.decode("cp1250")),
        ("latin-1", lambda b: b.decode("latin-1")),
    ):
        try:
            candidates.append((label, dec(raw)))
        except (LookupError, UnicodeDecodeError):
            pass
    candidates.append(("utf-8-replace", raw.decode("utf-8", errors="replace")))

    best_label, best_text = max(
        candidates,
        key=lambda p: (_score_bricon_text(p[1]), -_DECODE_PRIORITY.get(p[0], 99)),
    )
    if not quiet and len(raw) > 80 and _score_bricon_text(best_text) < 60:
        logging.warning(
            "COM tekst slabo liči na BRICON (encoding=%s). "
            "Na satu pokrenite Read out / slanje u Print Manageru; probajte drugi COM ili baud (9600–115200).",
            best_label,
        )
    return best_text


def looks_like_tauris_text(text: str) -> bool:
    low = (text or "").lower()
    return "tauris" in low or bool(re.search(r"\b\d{4}\s+SRB\b", text))


def looks_like_bricon_text(text: str) -> bool:
    if looks_like_tauris_text(text):
        return False
    if is_bricon_speedy_lista_doleta(text) and len(parse_bricon_speedy_arrival_rows(text)) >= 1:
        return True
    return _score_bricon_text(text) >= 120 or len(parse_bricon_clocking_rows(text)) >= 2


def is_bricon_speedy_lista_doleta(text: str) -> bool:
    return bool(_RE_LISTA_DOLETA.search(text or "")) or bool(_RE_SPEEDY_DEVICE.search(text or ""))


def infer_bricon_race_year(text: str) -> int | None:
    """Godina trke iz zaglavlja (DD/MM/YY) ili prstenova (SR-23-…)."""
    for m in re.finditer(r"\b(\d{2})/(\d{2})/(\d{2})\b", text or ""):
        yy = int(m.group(3))
        return 2000 + yy if yy < 100 else yy
    m2 = re.search(r"\b[A-Z]{2}-(\d{2})-\d{6,}", text or "")
    if m2:
        return 2000 + int(m2.group(1))
    return None


def _speedy_full_date(year: int | None, mm_dd: str) -> str:
    parts = (mm_dd or "").split("/")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return mm_dd
    dd, mm = int(parts[0]), int(parts[1])
    y = year if year is not None else datetime.now().year
    return f"{y:04d}-{mm:02d}-{dd:02d}"


def parse_bricon_speedy_arrival_rows(text: str) -> list[dict[str, str]]:
    """Redovi liste doleta: chip, prsten SR-YY-…, boja, DD/MM HH:MM:SS, status."""
    year = infer_bricon_race_year(text)
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in _RE_SPEEDY_ARRIVAL_ROW.finditer(text or ""):
        pos, chip, ring, gender, color, mm_dd, time_s = m.group(1, 2, 3, 4, 5, 6, 7)
        status_g = m.group(8)
        ring_n = ring.strip().upper()
        time_n = time_s.strip()
        key = (ring_n, time_n)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "position": pos.strip(),
                "chip_id": chip.strip().upper(),
                "ring": ring_n,
                "gender": gender.strip().upper(),
                "color": color.strip().upper(),
                "arrival_time": time_n,
                "arrival_date": _speedy_full_date(year, mm_dd),
                "arrival_mmdd": mm_dd.strip(),
                "status": (status_g or "OK").strip().upper(),
            }
        )
    return rows


def extract_bricon_speedy_meta(text: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    dm = _RE_SPEEDY_DEVICE.search(text or "")
    if dm:
        meta["bricon_device_id"] = dm.group(1).upper()
        meta["bricon_firmware"] = dm.group(2)
        meta["bricon_model"] = "Bricon Speedy"
    if _RE_LISTA_DOLETA.search(text or ""):
        meta["bricon_report_type"] = "lista doleta"
    pm = _RE_PAKOVANJE_COUNT.search(text or "")
    if pm:
        meta["bricon_basket_count"] = int(pm.group(1))
    yr = infer_bricon_race_year(text or "")
    if yr:
        meta["bricon_race_year"] = yr
    mm = _RE_SPEEDY_MEMBER.search(text or "")
    if mm:
        meta["bricon_member_code"] = mm.group(1).upper()
    return meta


def extract_bricon_fancier_name(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    mm = _RE_SPEEDY_MEMBER.search(text)
    if mm:
        name = re.sub(r"\s+", " ", mm.group(2).strip())
        if name and len(name) > 2:
            return name[:120]
    m = _RE_FANCIER.search(text)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        if name and not name.isdigit():
            return name[:120]
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in (
                    "fancier",
                    "owner",
                    "member",
                    "takmicar",
                    "takmičar",
                    "name",
                    "loft_name",
                ):
                    v = obj.get(key)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:120]
        except json.JSONDecodeError:
            pass
    return None


def parse_bricon_clocking_rows(text: str) -> list[dict[str, str]]:
    """Parsira redove dolazaka u normalizovanu listu."""
    speedy = parse_bricon_speedy_arrival_rows(text)
    if speedy:
        return speedy

    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(pos: str, ring: str, time_s: str, date_s: str = "", **extra: str) -> None:
        ring_n = re.sub(r"\s+", "", (ring or "").strip().upper())
        time_n = (time_s or "").strip()
        if not ring_n or not time_n:
            return
        key = (ring_n, time_n)
        if key in seen:
            return
        seen.add(key)
        row = {
            "position": pos,
            "ring": ring_n,
            "arrival_time": time_n,
            "arrival_date": (date_s or "").strip(),
        }
        row.update({k: v for k, v in extra.items() if v})
        rows.append(row)

    for m in _RE_CLOCKING_CSV.finditer(text):
        add(m.group(1), m.group(2), m.group(3), m.group(4) or "")
    for m in _RE_CLOCKING_SPACED.finditer(text):
        add(m.group(1), m.group(2), m.group(3))
    for m in _RE_CLOCKING_SEMI.finditer(text):
        add(str(len(rows) + 1), m.group(1), m.group(2), m.group(3) or "")
    if len(rows) < 2:
        for m in _RE_RING_TIME_LOOSE.finditer(text):
            add(str(len(rows) + 1), m.group(1), m.group(2))

    if text.strip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                items = obj.get("clockings") or obj.get("pigeons") or obj.get("birds") or []
                if isinstance(items, list):
                    for i, it in enumerate(items, start=1):
                        if not isinstance(it, dict):
                            continue
                        ring = str(
                            it.get("ring")
                            or it.get("ring_number")
                            or it.get("chip")
                            or ""
                        ).strip()
                        time_s = str(
                            it.get("time")
                            or it.get("arrival")
                            or it.get("arrival_time")
                            or ""
                        ).strip()
                        date_s = str(it.get("date") or it.get("arrival_date") or "").strip()
                        add(str(i), ring, time_s, date_s)
        except json.JSONDecodeError:
            pass

    return rows


def parse_bricon_payload_to_race_dict(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clockings = parse_bricon_clocking_rows(text)
    fancier = extract_bricon_fancier_name(text)
    race_name_m = _RE_RACE_NAME.search(text)
    race_label = race_name_m.group(1).strip() if race_name_m else ""

    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                out = dict(parsed)
                out.setdefault("race_id", f"BRICON-{stamp}")
                out.setdefault("start_time", datetime.now().isoformat(timespec="seconds"))
                out.setdefault("duration_sec", 0)
                out.setdefault("device_type", "bricon")
                out.setdefault("raw_com_text", text)
                if fancier and "takmicar" not in out:
                    out["takmicar"] = fancier
                if clockings and "bricon_clockings" not in out:
                    out["bricon_clockings"] = clockings
                return out
        except json.JSONDecodeError:
            pass

    speedy_meta = extract_bricon_speedy_meta(text)
    device_id = speedy_meta.get("bricon_device_id")
    race_id = f"BRICON-{device_id}-{stamp}" if device_id else f"BRICON-{stamp}"

    out: dict[str, Any] = {
        "race_id": race_id,
        "start_time": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": 0,
        "device_type": "bricon",
        "raw_com_text": text,
    }
    out.update(speedy_meta)
    if fancier:
        out["takmicar"] = fancier
    if race_label:
        out["bricon_race_name"] = race_label
    elif out.get("bricon_report_type"):
        out["bricon_race_name"] = str(out["bricon_report_type"])
    if clockings:
        out["bricon_clockings"] = clockings
    return out


def validate_bricon_payload(text: str) -> None:
    if not (text or "").strip():
        raise RuntimeError(
            "Nema podataka sa BRICON uređaja. Proverite USB/COM kabl, baud (često 9600), "
            "da li je na satu pokrenuto slanje (Read out) ili Print Manager «read out»."
        )
    if looks_like_tauris_text(text):
        raise RuntimeError(
            "Primljeni podaci liče na TAURIS, ne na BRICON. "
            "U programu izaberite «TAURIS» kao tip sata ili promenite port."
        )
    if not looks_like_bricon_text(text):
        raise RuntimeError(
            "Podaci sa porta ne liče na BRICON izlaz (nema prepoznatljivih redova dolaska). "
            "Pokrenite Read out na satu, probajte drugi COM port ili uvezite fajl iz Print Managera."
        )


def read_text_from_bricon_file(path: Path) -> str:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = path.read_bytes()
    if not raw.strip():
        raise RuntimeError(f"Fajl je prazan: {path}")
    return decode_bricon_payload(raw, quiet=True)


def read_race_from_bricon_file(path: Path) -> dict[str, Any]:
    text = read_text_from_bricon_file(path)
    validate_bricon_payload(text)
    data = parse_bricon_payload_to_race_dict(text)
    data["import_source"] = str(path)
    return data


def _bricon_connection_profile(
    config: TaurisConnectionConfig, *, slow: bool
) -> TaurisConnectionConfig:
    if slow:
        return TaurisConnectionConfig(
            port=config.port,
            baudrate=config.baudrate,
            read_timeout=BRICON_READ_TIMEOUT_SEC_SLOW,
            read_iterations=BRICON_READ_MAX_ITERATIONS_SLOW,
            read_idle_empty_reads=BRICON_READ_IDLE_EMPTY_READS_SLOW,
            tail_idle_empty_reads=BRICON_TAIL_IDLE_EMPTY_READS_SLOW,
            tail_extra_max_iterations=BRICON_TAIL_EXTRA_MAX_ITER_SLOW,
        )
    return TaurisConnectionConfig(
        port=config.port,
        baudrate=config.baudrate,
        read_timeout=BRICON_READ_TIMEOUT_SEC,
        read_iterations=BRICON_READ_MAX_ITERATIONS,
        read_idle_empty_reads=BRICON_READ_IDLE_EMPTY_READS,
        tail_idle_empty_reads=BRICON_TAIL_IDLE_EMPTY_READS,
        tail_extra_max_iterations=BRICON_TAIL_EXTRA_MAX_ITER,
    )


def com_port_looks_like_bricon_usb(port: str) -> bool:
    blob = com_port_description_blob(port)
    if not blob:
        return False
    return any(
        k in blob
        for k in (
            "bricon",
            "speedy",
            "x-treme",
            "xtreme",
            "print manager",
            "silicon labs",
            "ftdi",
            "cp210",
        )
    )


def _prepare_bricon_usb_serial(ser: Any) -> None:
    """Deljena implementacija — vidi lib.prepare_serial_port_for_bricon (mode='usb_passive')."""
    prepare_serial_port_for_bricon(ser, mode="usb_passive")


def _bricon_payload_ready(text: str, raw_len: int) -> bool:
    if raw_len < 40:
        return False
    speedy_rows = parse_bricon_speedy_arrival_rows(text)
    if len(speedy_rows) >= 1:
        return True
    clocking_rows = parse_bricon_clocking_rows(text)
    if len(clocking_rows) >= 2:
        return True
    return _score_bricon_text(text, clocking_row_count=len(clocking_rows)) >= 200


class BriconCancelled(RuntimeError):
    """Korisnik je otkazao BRICON preuzimanje dok je bilo u toku."""


def read_race_from_bricon_usb_readout(
    config: TaurisConnectionConfig,
    *,
    on_com_bytes: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    max_listen_sec: float = BRICON_USB_READOUT_MAX_SEC,
) -> dict[str, Any]:
    """
    Zamena za Print Manager «Uitlezen» preko USB:

    1) Print Manager binarni handshake (SendVersionInfo + baud 9600/38400).
    2) Program drži COM; korisnik na satu pokrene Read out (slave mode).
    3) Fallback: pasivno slušanje — SVAKA podržana baud vrednost dobija sopstveni
       vremenski budžet (``per_baud_budget``), tako da jedan neuspešan pokušaj ne
       potroši vreme predviđeno za ostale (ranije je ceo budžet trošio prvi baud iz
       liste i fallback na ostale se praktično nikad nije izvršavao).

    ``on_phase`` (opciono) dobija kratke tekstualne statuse za GUI (traženje porta,
    povezivanje, koji baud se proba, čitanje, završetak/greška) da korisnik vidi šta
    se dešava umesto zamrznutog prozora. ``should_cancel`` (opciono) se proverava
    između koraka da bi korisnik mogao bezbedno da otkaže preuzimanje.

    Ako ne uspe — uvezite fajl iz PM. Parcijalno primljeni podaci se UVEK čuvaju
    (najduži pokušaj preko svih baud-ova) i, ako se ništa ne parsira kao validna
    BRICON lista, snimaju se u dijagnostički fajl — nikad se tiho ne gube.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial paket. Instalirajte: pip install pyserial")

    def _check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise BriconCancelled("Otkazano od strane korisnika.")

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    _phase(f"traženje porta {config.port}…")
    verify_com_port_available(config.port)

    from tauris_desktop.bricon_capture import (
        active_capture,
        bricon_capture_enabled,
        end_capture_session,
        save_partial_payload_diagnostic,
        start_capture_session,
    )
    from tauris_desktop.bricon_pm import try_print_manager_protocol_readout

    from tauris_desktop.bricon_capture import wrap_serial_for_capture

    cap = active_capture()
    own_capture = False
    if cap is None and bricon_capture_enabled():
        cap = start_capture_session(config.port, note="automatski pri Preuzmi BRICON")
        own_capture = True

    try:
        _check_cancel()
        _phase("pokušavam Print Manager protokol (handshake)…")
        pm_raw = try_print_manager_protocol_readout(
            config,
            on_com_bytes=on_com_bytes,
            max_listen_sec=max_listen_sec,
            capture=cap,
            on_phase=_phase,
            should_cancel=should_cancel,
        )
        if pm_raw:
            text = decode_bricon_payload(pm_raw)
            try:
                validate_bricon_payload(text)
                data = parse_bricon_payload_to_race_dict(text)
                data["bricon_capture_mode"] = "usb_pm_protocol"
                _phase(f"završeno preko PM protokola — primljeno {len(pm_raw)} B.")
                return data
            except RuntimeError:
                logging.info(
                    "BRICON diag | phase=pm_fallback bytes=%s razlog=parser_ne_prepoznaje",
                    len(pm_raw),
                )

        _check_cancel()
        profile = _bricon_connection_profile(config, slow=True)
        overall_budget = max(BRICON_USB_READOUT_MIN_TOTAL_SEC, float(max_listen_sec))
        overall_deadline = time.monotonic() + overall_budget

        bauds: list[int] = []
        for b in (config.baudrate, *BRICON_USB_BAUD_CANDIDATES):
            if b not in bauds:
                bauds.append(b)

        per_baud_budget = max(BRICON_USB_PER_BAUD_MIN_SEC, overall_budget / len(bauds))

        raw = bytearray()
        last_error: str | None = None
        attempt_log: list[str] = []

        _phase(f"probam baud vrednosti: {', '.join(str(b) for b in bauds)}…")

        for baud in bauds:
            _check_cancel()
            remaining_overall = overall_deadline - time.monotonic()
            if remaining_overall <= 0:
                attempt_log.append(f"{baud}: preskočeno (isteklo ukupno vreme)")
                logging.info(
                    "BRICON diag | phase=baud_skip port=%s baud=%s razlog=isteklo_ukupno_vreme",
                    profile.port, baud,
                )
                break

            baud_budget = min(per_baud_budget, remaining_overall)
            baud_deadline = time.monotonic() + baud_budget
            attempt = bytearray()
            disconnect: list[BaseException] = []
            ser = None
            _phase(f"povezivanje na {profile.port} @ {baud} baud (do {baud_budget:.0f}s)…")
            logging.info(
                "BRICON diag | phase=baud_start port=%s baud=%s timeout=%.1fs",
                profile.port, baud, baud_budget,
            )
            try:
                ser = serial.Serial(
                    port=profile.port,
                    baudrate=baud,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=profile.read_timeout,
                )
                wser = wrap_serial_for_capture(ser, cap)
                _prepare_bricon_usb_serial(wser)
                for payload in BRICON_OPTIONAL_WAKE_BYTES:
                    try:
                        wser.write(payload)
                        wser.flush()
                    except Exception as exc:
                        logging.warning(
                            "BRICON diag | phase=wake_write port=%s baud=%s exc=%s: %s",
                            profile.port, baud, type(exc).__name__, exc,
                        )
                        continue

                def _notify(base: int) -> Callable[[int], None] | None:
                    if on_com_bytes is None:
                        return None
                    return lambda n: on_com_bytes(base + n)

                _phase(f"čitam @ {baud} baud…")
                last_checked_len = 0
                text_probe = ""
                while time.monotonic() < baud_deadline:
                    _check_cancel()
                    n_before = len(attempt)
                    chunk = read_serial_bytes_until_idle(
                        wser,
                        profile,
                        max_iterations=min(48, profile.read_iterations),
                        idle_empty_reads=profile.read_idle_empty_reads,
                        on_com_bytes=_notify(n_before),
                        on_error=disconnect.append,
                    )
                    if chunk:
                        attempt.extend(chunk)
                    if disconnect:
                        break
                    # Ponovo dekodiraj/parsiraj samo kad bafer STVARNO poraste — sprečava
                    # O(n^2) reparsiranje celog bafera na svaki prazan ciklus čekanja.
                    # ``text_probe``/``last_checked_len`` ostaju sinhronizovani sa ``attempt``
                    # i posle petlje se ponovo koriste umesto još jednog punog dekodiranja.
                    if len(attempt) != last_checked_len:
                        text_probe = decode_bricon_payload(bytes(attempt), quiet=True)
                        last_checked_len = len(attempt)
                        if _bricon_payload_ready(text_probe, len(attempt)) and not chunk:
                            break
                    if not chunk and n_before == len(attempt):
                        time.sleep(0.35)
                        if time.monotonic() >= baud_deadline:
                            break

                if disconnect:
                    exc0 = disconnect[0]
                    outcome = f"prekid konekcije ({type(exc0).__name__}: {exc0})"
                    attempt_log.append(f"{baud}: {outcome} — primljeno {len(attempt)} B")
                    logging.warning(
                        "BRICON diag | phase=disconnected port=%s baud=%s primljeno=%sB exc=%s: %s",
                        profile.port, baud, len(attempt), type(exc0).__name__, exc0,
                    )
                    if len(attempt) > len(raw):
                        raw = attempt
                    continue

                ready = _bricon_payload_ready(text_probe, len(attempt))
                if ready:
                    raw = attempt
                    attempt_log.append(f"{baud}: OK — primljeno {len(attempt)} B")
                    logging.info(
                        "BRICON diag | phase=baud_ok port=%s baud=%s bytes=%s",
                        profile.port, baud, len(attempt),
                    )
                    break
                attempt_log.append(f"{baud}: nema prepoznatljivog payload-a — primljeno {len(attempt)} B")
                logging.info(
                    "BRICON diag | phase=baud_result port=%s baud=%s rezultat=neprepoznato bytes=%s",
                    profile.port, baud, len(attempt),
                )
                if len(attempt) > len(raw):
                    raw = attempt
            except BriconCancelled:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempt_log.append(f"{baud}: greška — {last_error}")
                logging.warning(
                    "BRICON diag | phase=baud_error port=%s baud=%s exc=%s: %s",
                    profile.port, baud, type(exc).__name__, exc,
                )
                if len(attempt) > len(raw):
                    raw = attempt
            finally:
                try:
                    if ser is not None:
                        ser.close()
                except Exception:
                    pass

        attempts_text = "\n".join(f"  • {line}" for line in attempt_log) or "  (nijedan pokušaj)"

        if not raw:
            hint = (
                "Nije primljen nijedan podatak sa BRICON USB porta.\n\n"
                "Kao u Print Manageru (C:\\Bricon\\Print Manager):\n"
                "• Zatvorite Print Manager — ne deli COM sa ovim programom\n"
                "• USB kabl pre starta; FTDI drajver iz instalera Tauris\n"
                "• «Preuzmi BRICON» šalje StartReadout (10-E1-00) + drži COM\n"
                "• Zatim na Speedy: Read out / Uitlezen (slave, ne štampač)\n\n"
                f"Pokušani baud-ovi:\n{attempts_text}\n\n"
            )
            if last_error:
                hint += f"Poslednja greška: {last_error}\n\n"
            if cap is not None:
                hint += f"COM snimak: {cap.path}\n\n"
            hint += "Ako i dalje ne radi — «Uvezi BRICON fajl» ili «Snimi COM (dijagnostika)»."
            _phase("greška — nema podataka sa porta.")
            raise RuntimeError(hint)

        text = decode_bricon_payload(bytes(raw))
        try:
            validate_bricon_payload(text)
        except RuntimeError:
            diag_path = None
            try:
                diag_path = save_partial_payload_diagnostic(
                    profile.port, bytes(raw), reason="Payload primljen ali ne prepoznat kao BRICON lista"
                )
            except Exception:
                logging.exception("BRICON: čuvanje dijagnostičkog fajla nije uspelo.")
            hint2 = (
                "Podaci su stigli sa porta, ali ne liče na BRICON listu.\n"
                "Proverite da ste na satu pokrenuli Read out (USB), ne štampač.\n"
                "Ili uvezite sačuvani fajl iz Print Managera.\n\n"
                f"Pokušani baud-ovi:\n{attempts_text}"
            )
            if diag_path is not None:
                hint2 += f"\n\nParcijalni podaci ({len(raw)} B) sačuvani za dijagnostiku: {diag_path}"
            _phase("greška — primljeni podaci ne liče na BRICON listu.")
            raise RuntimeError(hint2) from None
        data = parse_bricon_payload_to_race_dict(text)
        data["bricon_capture_mode"] = "usb_readout"
        _phase(f"završeno — primljeno {len(raw)} B.")
        return data
    except BriconCancelled:
        _phase("otkazano od strane korisnika.")
        raise
    finally:
        if own_capture and cap is not None:
            end_capture_session(cap, summary="kraj Preuzmi BRICON")
