from __future__ import annotations

import io
import json
import base64
import hashlib
import hmac
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from queue import Empty, Queue
import stat
from typing import Any
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

try:
    from reportlab.graphics.barcode.qr import QrCodeWidget
except Exception:  # pragma: no cover
    QrCodeWidget = None


DEFAULT_JSON_DIR = Path("output")
RESULTS_DESKTOP_ROOT = Path.home() / "Desktop" / "REZULTATI_TRKE"
RESULTS_DESKTOP_FALLBACK_ROOT = Path.home() / "Desktop" / "REZULTATI_TRKE_RECOVERY"
GUI_SETTINGS_FILENAME = "tauris_gui_settings.json"
RESULTS_LOCK_PASSWORD = "2249987"
APP_EXPIRY_DATE = date(2027, 5, 1)
ACCESS_KEY_EXPECTED_SHA256 = "697d4c3cb2ff30a486a3a8b8e5886e760aeb681a23f2e0880bc6ef503781e841"
ACCESS_KEY_FILENAME = "tauris_access.key"
REQUIRED_RACE_FIELDS = ("race_id", "start_time", "duration_sec")
SUPPORTED_DELIVERY_MODES = {"none", "server", "email"}
# COM: kratki timeout + raniji kraj kad uređaj prestane da šalje (idle), umesto fiksnog 30×0.5 s
TAURIS_READ_CHUNK = 4096
TAURIS_READ_TIMEOUT_SEC = 0.13
# Duže pauze između blokova na UART-u — premali broj je prekidao transfer oko reda 030–031
TAURIS_READ_IDLE_EMPTY_READS = 22
TAURIS_READ_MAX_ITERATIONS = 128
TAURIS_CONNECT_MAX_ITERATIONS = 96
# Dopunsko čitanje ako kontrol lista kaže 0040 a još nema reda 0040 SRB (strpljiviji idle)
TAURIS_TAIL_IDLE_EMPTY_READS = 48
TAURIS_TAIL_EXTRA_MAX_ITER = 100


@dataclass
class TaurisConnectionConfig:
    port: str
    baudrate: int
    read_timeout: float = TAURIS_READ_TIMEOUT_SEC
    read_iterations: int = TAURIS_READ_MAX_ITERATIONS
    read_chunk: int = TAURIS_READ_CHUNK
    # uzastopna prazna čitanja posle što je bar jedan bajt stigao = transfer završen
    read_idle_empty_reads: int = TAURIS_READ_IDLE_EMPTY_READS


@dataclass
class DeliveryConfig:
    mode: str
    signing_key: str
    timeout_sec: float
    server_url: str
    server_token: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str
    mail_to: str


def read_serial_bytes_until_idle(
    ser: Any,
    config: TaurisConnectionConfig,
    *,
    max_iterations: int | None = None,
    idle_empty_reads: int | None = None,
) -> bytes:
    """
    Skladišti bajtove sa COM-a. Nakon što je nešto primljeno, n uzastopnih praznih read-ova
    (timeout bez podataka) znači da je TAURIS završio slanje — ne čekamo fiksno 30 ciklusa.
    """
    acc = bytearray()
    idle = 0
    limit = config.read_idle_empty_reads if idle_empty_reads is None else idle_empty_reads
    max_loops = config.read_iterations if max_iterations is None else max_iterations
    for _ in range(max_loops):
        try:
            data = ser.read(config.read_chunk)
        except Exception:
            break
        if data:
            acc.extend(data)
            idle = 0
        elif acc:
            idle += 1
            if idle >= limit:
                break
    return bytes(acc)


def setup_logging() -> None:
    log_path = app_data_dir() / "logs" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # i dalje izlažemo putanju kroz globalu zbog postojećih log poruka/debug očekivanja
    global LOG_PATH
    LOG_PATH = log_path
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def get_resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path / relative_path


def app_data_dir() -> Path:
    """
    Upisive lokacije:
    - instaliran EXE -> %LOCALAPPDATA%\\TaurisPdfApp
    - dev run (main.py) -> folder projekta
    """
    if getattr(sys, "frozen", False):
        base = Path(os.getenv("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        target = base / "TaurisPdfApp"
        target.mkdir(parents=True, exist_ok=True)
        return target
    return Path(__file__).resolve().parent


def gui_settings_path() -> Path:
    """Fajl za trajno čuvanje GUI podešavanja (uvek u upisivoj lokaciji)."""
    return app_data_dir() / GUI_SETTINGS_FILENAME


def load_gui_settings_dict() -> dict[str, Any]:
    path = gui_settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("Neuspelo učitavanje %s", path)
        return {}


def save_gui_settings_dict(data: dict[str, Any]) -> None:
    path = gui_settings_path()
    try:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        logging.exception("Neuspelo snimanje %s", path)


_BAUD_CHOICES = ("9600", "19200", "38400", "57600", "115200")


def load_delivery_config() -> DeliveryConfig:
    mode = os.getenv("TAURIS_DELIVERY_MODE", "none").strip().lower()
    if mode not in SUPPORTED_DELIVERY_MODES:
        logging.warning("Nepoznat TAURIS_DELIVERY_MODE=%s, koristim 'none'.", mode)
        mode = "none"

    timeout_text = os.getenv("TAURIS_DELIVERY_TIMEOUT", "15").strip()
    try:
        timeout_sec = float(timeout_text)
    except ValueError:
        timeout_sec = 15.0

    smtp_port_text = os.getenv("TAURIS_SMTP_PORT", "587").strip()
    try:
        smtp_port = int(smtp_port_text)
    except ValueError:
        smtp_port = 587

    return DeliveryConfig(
        mode=mode,
        signing_key=os.getenv("TAURIS_SIGNING_KEY", "CHANGE_ME_SECRET"),
        timeout_sec=timeout_sec,
        server_url=os.getenv("TAURIS_SERVER_URL", "").strip(),
        server_token=os.getenv("TAURIS_SERVER_TOKEN", "").strip(),
        smtp_host=os.getenv("TAURIS_SMTP_HOST", "").strip(),
        smtp_port=smtp_port,
        smtp_user=os.getenv("TAURIS_SMTP_USER", "").strip(),
        smtp_password=os.getenv("TAURIS_SMTP_PASSWORD", "").strip(),
        mail_from=os.getenv("TAURIS_MAIL_FROM", "").strip(),
        mail_to=os.getenv("TAURIS_MAIL_TO", "").strip(),
    )


def _clean_takmicar_display(name: str) -> str:
    txt = " ".join(str(name or "").replace("\xa0", " ").split()).strip(" ,")
    if "," in txt:
        txt = txt.split(",", 1)[0].strip()
    return txt.strip(" ,")


def extract_takmicar_from_tauris_kontrol_list(text: str) -> str | None:
    """
    TauRIS „kontrol list“: linija tipa
      001.001.049.066  Dragan Todorovic      0040 0001-0040
    (četiri grupe cifara tačkom, ime, četvorocifren broj, opseg xxxx-xxxx).
    """
    if not (text or "").strip():
        return None
    normalized = text.replace("\r\n", "\n")
    m = re.search(
        r"(?m)^\s*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\s+(.+?)\s+\d{4}\s+\d{4}-\d{4}\s*$",
        normalized,
    )
    if not m:
        return None
    cand = _clean_takmicar_display(m.group(1))
    if not cand or cand[0].isdigit():
        return None
    return cand


def parse_tauris_expected_last_lag(text: str) -> int | None:
    """
    Iz linije kontrol liste tipa „001.001.049.066  Ime  0040 0001-0040“ uzima poslednji Lag.
    Prvo traži isti obrazac kao extract_takmicar_from_tauris_kontrol_list da nema lažnog pogotka.
    """
    normalized = (text or "").replace("\r\n", "\n")
    m = re.search(
        r"(?m)^\s*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\s+.+\s+(\d{4})\s+(\d{4})-(\d{4})\s*$",
        normalized,
    )
    if m:
        try:
            lo = int(m.group(2))
            hi = int(m.group(3))
            if lo <= hi <= 9999:
                return hi
        except ValueError:
            pass
    for line in normalized.splitlines():
        m2 = re.search(r"\b(\d{4})\s+(\d{4})-(\d{4})\s*$", line.rstrip())
        if not m2:
            continue
        try:
            lo = int(m2.group(2))
            hi = int(m2.group(3))
        except ValueError:
            continue
        if lo <= hi <= 9999:
            return hi
    return None


def tauris_text_has_lag_row(text: str, last_lag: int) -> bool:
    """True ako postoji red koji počinje sa „NNNN SRB“ (npr. 0040 SRB)."""
    lag = f"{last_lag:04d}"
    return bool(re.search(rf"(?m)^{re.escape(lag)}\s+SRB\b", text))


def extract_takmicar_name_from_raw_text(text: str) -> str | None:
    """
    Ime takmičara iz TAURIS teksta (isti obrazac kao u Analiza leta / _savez_parse_takmicar_name_broj_from_line):
    linija „Takmičar:“ / „Takmicar:“ + ime, opciono zarez i članski broj na kraju.
    Takođe probaju uobičajeni ključevi u JSON-u.
    """
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                for key in (
                    "takmicar",
                    "Takmicar",
                    "TAKMICAR",
                    "takmičar",
                    "ime_takmicara",
                    "pilot",
                    "competitor",
                    "name",
                    "vlasnik",
                ):
                    v = obj.get(key)
                    if isinstance(v, str) and v.strip():
                        return _clean_takmicar_display(v)
        except json.JSONDecodeError:
            pass

    for line in text.replace("\r\n", "\n").split("\n"):
        line = " ".join(line.replace("\xa0", " ").split()).strip()
        m = re.match(r"(?is)Takmi\S*\s*:\s*(.+)$", line)
        if not m:
            continue
        rest = m.group(1).strip()
        m2 = re.search(r",\s*([0-9]{2,6})\s*$", rest)
        if m2:
            name = rest[: m2.start()].strip().strip(",")
        else:
            m3 = re.search(r"\s+([0-9]{4,6})\s*$", rest)
            if m3:
                name = rest[: m3.start()].strip()
            else:
                m4 = re.search(r"\s+([0-9]{2,6})\s*$", rest)
                if m4:
                    name = rest[: m4.start()].strip()
                else:
                    name = rest
        name = _clean_takmicar_display(name)
        if name:
            return name

    m0 = re.search(r"(?is)Takmi\S*\s*:\s*(.+?)(?:\r?\n|$)", text)
    if m0:
        rest = m0.group(1).strip()
        m2 = re.search(r",\s*([0-9]{2,6})\s*$", rest)
        if m2:
            name = _clean_takmicar_display(rest[: m2.start()].strip().strip(","))
        else:
            name = _clean_takmicar_display(rest)
        if name:
            return name

    name_kl = extract_takmicar_from_tauris_kontrol_list(text)
    if name_kl:
        return name_kl
    return None


def read_com_snippet_for_takmicar(config: TaurisConnectionConfig, iterations: int) -> str:
    """Čitanje sa COM-a do max_iterations blokova."""
    txt, _ = read_com_until_takmicar_or_done(config, max_iterations=iterations, stop_on_name=False)
    return txt


_DECODE_PRIORITY = {"utf-8": 0, "cp1250": 1, "cp1252": 2, "latin-1": 3, "utf-8-replace": 4}


def decode_com_payload(raw: bytes, *, quiet: bool = False) -> str:
    """
    Izbor encodinga za sirove bajtove sa COM-a.

    Ranije je korišćen UTF-8 sa ``errors="replace"``: to *nikad* ne pada na latin-1/cp1250,
    a uređaj često šalje CP1250 (srpski Windows) ili 8-bit ASCII — rezultat su � i „hijeroglifi“ u PDF-u.
    Biramo kandidata sa najboljim skorom (ključne reči TauRIS / kontrol list / redovi „NNNN SRB“).
    """
    if not raw:
        return ""
    candidates: list[tuple[str, str]] = []

    try:
        candidates.append(("utf-8", raw.decode("utf-8")))
    except UnicodeDecodeError:
        pass

    for label, enc in (
        ("cp1250", "cp1250"),
        ("cp1252", "cp1252"),
        ("latin-1", "latin-1"),
    ):
        try:
            candidates.append((label, raw.decode(enc)))
        except (LookupError, UnicodeDecodeError):
            pass

    candidates.append(("utf-8-replace", raw.decode("utf-8", errors="replace")))

    def score_text(s: str) -> int:
        if not s.strip():
            return -(10**9)
        low = s.lower()
        sc = 0
        if "tauris" in low:
            sc += 1200
        if "kontrol" in low:
            sc += 600
        if re.search(r"\b\d{4}\s+SRB\b", s):
            sc += 900
        sc -= s.count("\ufffd") * 150
        bad_ctrl = sum(1 for c in s if ord(c) < 32 and c not in "\r\n\t")
        sc -= bad_ctrl * 30
        printable = sum(1 for c in s if c.isprintable() or c in "\n\r\t")
        sc += int(100 * printable / max(len(s), 1))
        return sc

    best_label, best_text = max(
        candidates,
        key=lambda p: (score_text(p[1]), -_DECODE_PRIORITY.get(p[0], 99)),
    )
    sc = score_text(best_text)
    if not quiet:
        if sc < 80 and len(raw) > 120:
            logging.warning(
                "COM tekst ne liči na TAURIS (encoding=%s, score=%s). "
                "Proverite baud rate u programu i u podešavanjima uređaja (često 9600 ili 19200) "
                "i ponovo pokrenite slanje liste.",
                best_label,
                sc,
            )
        else:
            logging.debug("COM dekodiranje: %s (score=%s)", best_label, sc)
    return best_text


def read_com_until_takmicar_or_done(
    config: TaurisConnectionConfig,
    max_iterations: int,
    stop_on_name: bool = True,
) -> tuple[str, str | None]:
    """
    Čita COM i spaja bajtove do kraja petlje (max_iterations).

    Nikada ne prekidamo čitanje čim se nađe ime: TAURIS šalje prvo liniju sa takmičarem,
    pa tek onda redove tabele (0001 SRB …). Prerano zatvaranje porta je držalo uređaj
    „usred“ slanja — sledeće čitanje je počinjalo od reda 013 umesto od 001.

    Ako je stop_on_name True, ime se ažurira čim se pojavi u baferu, ali čitanje traje u punom obimu.
    """
    if serial is None:
        return "", None
    ser = serial.Serial(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=config.read_timeout,
    )
    acc = bytearray()
    extracted: str | None = None
    idle = 0
    idle_limit = config.read_idle_empty_reads
    try:
        for _ in range(max_iterations):
            try:
                data = ser.read(config.read_chunk)
            except Exception:
                break
            if data:
                acc.extend(data)
                idle = 0
                if stop_on_name and acc:
                    text_try = decode_com_payload(bytes(acc), quiet=True)
                    maybe_name = extract_takmicar_name_from_raw_text(text_try)
                    if maybe_name:
                        extracted = maybe_name
            elif acc:
                idle += 1
                if idle >= idle_limit:
                    break
    finally:
        try:
            ser.close()
        except Exception:
            pass

    if not acc.strip():
        return "", extracted

    text = decode_com_payload(bytes(acc))
    if extracted is None:
        extracted = extract_takmicar_name_from_raw_text(text)
    return text, extracted


def parse_tauris_payload_to_race_dict(text: str) -> dict[str, Any]:
    """
    Uređaj šalje tekst (ili JSON). Uvek vraćamo dict sa obaveznim poljima za pipeline.
    """
    text = (text or "").strip()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                out = dict(parsed)
                out.setdefault("race_id", f"TAURIS-{stamp}")
                out.setdefault("start_time", datetime.now().isoformat(timespec="seconds"))
                if "duration_sec" not in out:
                    out["duration_sec"] = 0
                if not any(
                    k in out for k in ("takmicar", "takmičar", "Takmicar", "ime_takmicara")
                ):
                    tn = extract_takmicar_name_from_raw_text(text)
                    if tn:
                        out["takmicar"] = tn
                return out
        except json.JSONDecodeError:
            pass
    out = {
        "race_id": f"TAURIS-{stamp}",
        "start_time": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": 0,
        "raw_com_text": text,
    }
    tn = extract_takmicar_name_from_raw_text(text)
    if tn:
        out["takmicar"] = tn
    return out


def read_race_from_device(config: TaurisConnectionConfig) -> dict[str, Any]:
    """
    Čitanje kao u programu Analiza leta (qt_app_ui_common._do_read):
    otvori COM, u petlji čitaj bajtove (ne šalje se GET_RACE); uređaj šalje podatke kada korisnik pokrene slanje.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial paket. Instalirajte: pip install pyserial")

    ser = None
    try:
        ser = serial.Serial(
            port=config.port,
            baudrate=config.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=config.read_timeout,
        )
    except Exception as exc:
        logging.exception("Greska pri otvaranju COM porta.")
        raise RuntimeError(f"Greska pri otvaranju porta: {exc}") from exc

    try:
        raw = read_serial_bytes_until_idle(ser, config)
        for _ in range(4):
            probe = decode_com_payload(raw)
            expected_last = parse_tauris_expected_last_lag(probe)
            if expected_last is None or tauris_text_has_lag_row(probe, expected_last):
                break
            n_before = len(raw)
            more = read_serial_bytes_until_idle(
                ser,
                config,
                max_iterations=TAURIS_TAIL_EXTRA_MAX_ITER,
                idle_empty_reads=TAURIS_TAIL_IDLE_EMPTY_READS,
            )
            raw = raw + more
            if len(raw) == n_before:
                logging.warning(
                    "TAURIS: u tekstu nema reda %04d SRB iako kontrol lista traži do tog broja; "
                    "nema novih bajtova — proverite kabl/baud ili ponovo pokrenite slanje.",
                    expected_last,
                )
                break
            logging.info(
                "TAURIS: dopunjeno čitanje COM-a (još %s bajtova), očekivan poslednji Lag=%04d.",
                len(raw) - n_before,
                expected_last,
            )
    finally:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass

    if not raw.strip():
        raise RuntimeError(
            "Nema podataka sa TAURIS uređaja. Proverite: pravi COM port, baud rate "
            "(probajte 9600 ili kao u Analiza leta), da li je uređaj poslao podatke posle klika na preuzimanje."
        )

    text = decode_com_payload(raw)
    return parse_tauris_payload_to_race_dict(text)


# Isto kao podrazumevano u GUI — ako je još uvek ovo, ime uzimamo iz preuzetih podataka
UNKNOWN_PILOT_LABEL = "Nepoznat takmičar"

GRUPACIJA_LABELS = tuple(f"Grupacija {i}" for i in range(1, 19))
# Broj trke uvek tri cifre (001 … 999) — i u padajućem meniju i u izvozu
BROJ_TRKE_LABELS = tuple(f"{i:03d}" for i in range(1, 1000))


def apply_staza_ui_to_race_data(
    race_data: dict[str, Any],
    *,
    grupacija_label: str,
    broj_trke_num: int,
    mesto_pustanja: str,
) -> dict[str, Any]:
    """Uključi izbor staze iz GUI u race_data (JSON potpis, PDF zaglavlje, izvoz)."""
    out = dict(race_data)
    g = (grupacija_label or "").strip() or GRUPACIJA_LABELS[0]
    out["grupacija"] = g
    m_gr = re.search(r"(\d+)\s*$", g)
    if m_gr:
        try:
            gn = int(m_gr.group(1))
            if 1 <= gn <= 18:
                out["grupacija_broj"] = gn
        except ValueError:
            pass
    try:
        n = int(broj_trke_num)
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(999, n))
    out["broj_trke"] = f"{n:03d}"
    mp = (mesto_pustanja or "").strip()
    out["mesto_pustanja"] = mp
    if mp:
        out["start_grad"] = mp
    else:
        out.pop("start_grad", None)
    return out


# Windows: zabranjeni znaci u imenu fajla; ostalo dozvoljeno (uklj. č/ć/š/đ/ž)
_WIN_FILENAME_FORBIDDEN = frozenset('<>:"/\\|?*')


def sanitize_filename(part: str) -> str:
    """Bezbedno ime fajla; čuva srpska slova (ne pretvara č u _)."""
    s = (part or "").strip()
    if not s:
        return "nepoznato"
    out: list[str] = []
    for ch in s:
        if ch in _WIN_FILENAME_FORBIDDEN or ord(ch) < 32:
            out.append("_")
        elif ch.isspace():
            out.append("_")
        else:
            out.append(ch)
    cleaned = "".join(out)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "nepoznato"


def clear_export_file_readonly(path: Path) -> None:
    """Ukloni read-only pre nego što aplikacija ponovo upiše fajl."""
    try:
        if path.is_file():
            os.chmod(path, stat.S_IWRITE)
    except Exception:
        logging.exception("Neuspesno uklanjanje read-only sa %s", path)


def set_export_file_readonly(path: Path) -> None:
    """Read-only u Explorer-u — sprečava slučajno menjanje; ne sprečava administratora."""
    try:
        if path.is_file():
            os.chmod(path, stat.S_IREAD)
    except Exception:
        logging.exception("Neuspesno postavljanje read-only na %s", path)


def is_app_expired(now: date | None = None) -> bool:
    today = now or datetime.now().date()
    return today > APP_EXPIRY_DATE


def _show_block_message(message: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Program zaključan", message)
        root.destroy()
    except Exception:
        print(message)


def _validate_access_key_file(path: Path) -> bool:
    try:
        key_bytes = path.read_bytes()
    except Exception:
        return False
    digest = hashlib.sha256(key_bytes).hexdigest()
    return digest == ACCESS_KEY_EXPECTED_SHA256


def request_access_key_file(parent: tk.Misc | None = None) -> Path | None:
    desktop_key = (Path.home() / "Desktop" / ACCESS_KEY_FILENAME).resolve()
    if desktop_key.is_file() and _validate_access_key_file(desktop_key):
        return desktop_key
    initial_dir = str(Path.home() / "Desktop")
    selected = filedialog.askopenfilename(
        parent=parent,
        title="Izaberite pristupni ključ",
        initialdir=initial_dir,
        initialfile=ACCESS_KEY_FILENAME,
        filetypes=[("Access key", "*.key"), ("Svi fajlovi", "*.*")],
    )
    if not selected:
        return None
    key_path = Path(selected)
    if not key_path.is_file() or not _validate_access_key_file(key_path):
        messagebox.showerror("Greška", "Nevažeća ključna datoteka.")
        return None
    return key_path


def lock_results_folder(folder_path: Path) -> None:
    """
    Folder lock je isključen jer je pravio konfuziju sa prikazom/pravima u Explorer-u.
    Sigurnost ostaje kroz šifrovani .taurislock paket i lozinku pri otvaranju.
    """
    _ = folder_path
    return


def remove_plain_exports(*paths: Path) -> None:
    """Obriši otvorene izvoze; ostavlja samo .taurislock paket."""
    for p in paths:
        try:
            clear_export_file_readonly(p)
            p.unlink(missing_ok=True)
        except Exception:
            logging.exception("Neuspešno brisanje otvorenog fajla: %s", p)


def write_preview_pdf_temp(pdf_bytes: bytes) -> Path:
    """Upiši privremeni PDF za ugrađeni preview nakon zaključanog čuvanja."""
    fd, tmp_name = tempfile.mkstemp(prefix="tauris_preview_", suffix=".pdf")
    os.close(fd)
    tmp_path = Path(tmp_name)
    tmp_path.write_bytes(pdf_bytes)
    return tmp_path


def ensure_results_folder_writable(folder_path: Path) -> None:
    """
    Ako je folder ranije zaključan pogrešnim ACL-om, pokušaj oporavak prava
    za trenutnog korisnika kako bi snimanje fajlova ponovo radilo.
    """
    folder_path.mkdir(parents=True, exist_ok=True)
    probe = folder_path / ".__write_probe__.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return
    except PermissionError:
        if sys.platform != "win32":
            raise
    except Exception:
        return

    user = ""
    try:
        whoami = subprocess.run(
            ["whoami"],
            check=False,
            capture_output=True,
            text=True,
        )
        user = (whoami.stdout or "").strip()
    except Exception:
        user = ""
    if not user:
        user = os.environ.get("USERNAME", "").strip()
    if not user:
        return
    fp = str(folder_path)
    try:
        # Agresivniji oporavak ACL-a za već zaključane foldere.
        subprocess.run(["icacls", fp, "/reset", "/T", "/C"], check=False, capture_output=True, text=True)
        subprocess.run(["icacls", fp, "/inheritance:e", "/T", "/C"], check=False, capture_output=True, text=True)
        subprocess.run(["icacls", fp, "/grant:r", f"{user}:(OI)(CI)F", "/T", "/C"], check=False, capture_output=True, text=True)
    except Exception:
        logging.exception("Neuspešan oporavak dozvola za folder: %s", folder_path)


def resolve_output_directory(race_data: dict[str, Any]) -> Path:
    broj_trke = sanitize_filename(str(race_data.get("broj_trke") or ""))
    grupacija = sanitize_filename(str(race_data.get("grupacija") or ""))
    target_dir = RESULTS_DESKTOP_ROOT / f"{broj_trke}-{grupacija}"
    ensure_results_folder_writable(target_dir)
    probe = target_dir / ".__write_probe__.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return target_dir
    except Exception:
        logging.exception("Primarni folder nije upisiv, koristim fallback: %s", target_dir)
    fallback_dir = RESULTS_DESKTOP_FALLBACK_ROOT / f"{broj_trke}-{grupacija}"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    return fallback_dir


def _xor_stream_encrypt(data: bytes, key: bytes) -> bytes:
    out = bytearray(len(data))
    counter = 0
    offset = 0
    while offset < len(data):
        block = hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        chunk = data[offset : offset + len(block)]
        out[offset : offset + len(chunk)] = bytes(a ^ b for a, b in zip(chunk, block))
        offset += len(chunk)
        counter += 1
    return bytes(out)


def create_locked_results_bundle(
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    password: str,
) -> Path:
    """
    Pravi šifrovani paket (.taurislock) koji zahteva lozinku za čitanje.
    Format: TAURISLOCKv1 + salt + hmac + ciphertext.
    """
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": [
            {"name": json_path.name, "data_b64": base64.b64encode(json_path.read_bytes()).decode("ascii")},
            {"name": pdf_path.name, "data_b64": base64.b64encode(pdf_path.read_bytes()).decode("ascii")},
        ],
    }
    if txt_path.is_file():
        payload["files"].append(
            {"name": txt_path.name, "data_b64": base64.b64encode(txt_path.read_bytes()).decode("ascii")}
        )
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    salt = os.urandom(16)
    key_material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000, dklen=64)
    enc_key = key_material[:32]
    mac_key = key_material[32:]
    cipher = _xor_stream_encrypt(plain, enc_key)
    tag = hmac.new(mac_key, cipher, hashlib.sha256).digest()
    out_path = json_path.with_suffix(".taurislock")
    clear_export_file_readonly(out_path)
    out_path.write_bytes(b"TAURISLOCKv1" + salt + tag + cipher)
    set_export_file_readonly(out_path)
    return out_path


def unlock_results_bundle(bundle_path: Path, password: str, target_dir: Path) -> list[Path]:
    raw = bundle_path.read_bytes()
    magic = b"TAURISLOCKv1"
    if len(raw) < len(magic) + 16 + 32:
        raise ValueError("Zaključan paket je neispravan.")
    if not raw.startswith(magic):
        raise ValueError("Nepoznat format zaključanog paketa.")
    i = len(magic)
    salt = raw[i : i + 16]
    i += 16
    tag = raw[i : i + 32]
    i += 32
    cipher = raw[i:]
    key_material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000, dklen=64)
    enc_key = key_material[:32]
    mac_key = key_material[32:]
    expected = hmac.new(mac_key, cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise ValueError("Pogrešna šifra.")
    plain = _xor_stream_encrypt(cipher, enc_key)
    try:
        payload = json.loads(plain.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Neuspešno čitanje zaključanog paketa.") from exc
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Zaključan paket nema fajlove.")
    target_dir.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = sanitize_filename(str(item.get("name") or "rezultat.bin"))
        if not name:
            continue
        data_b64 = item.get("data_b64")
        if not isinstance(data_b64, str):
            continue
        out = target_dir / name
        out.write_bytes(base64.b64decode(data_b64.encode("ascii")))
        out_paths.append(out)
    if not out_paths:
        raise ValueError("Nijedan fajl nije raspakovan.")
    return out_paths


def resolve_pilot_name_for_output(
    race_data: dict[str, Any],
    pilot_from_ui: str,
) -> str:
    """
    Ako korisnik nije uneo drugo ime, koristi takmičara iz payload-a / sirovog COM teksta
    da JSON/PDF budu imenovani pravilno (npr. Dragan_Todorovic, ne Nepoznat_takmi_ar).
    """
    ui = pilot_from_ui.strip()
    if ui and ui != UNKNOWN_PILOT_LABEL:
        return ui
    for k in ("takmicar", "takmičar", "Takmicar", "ime_takmicara"):
        v = race_data.get(k)
        if isinstance(v, str) and v.strip():
            return _clean_takmicar_display(v)
    raw = race_data.get("raw_com_text")
    if isinstance(raw, str) and raw.strip():
        ext = extract_takmicar_name_from_raw_text(raw)
        if ext:
            return ext
    return ui or UNKNOWN_PILOT_LABEL


def build_output_paths(race_data: dict[str, Any], pilot_name: str) -> tuple[Path, Path, Path]:
    output_dir = resolve_output_directory(race_data)
    datum = datetime.now().strftime("%Y%m%d")
    broj_trke = sanitize_filename(str(race_data.get("broj_trke") or ""))
    mesto_pustanja = sanitize_filename(str(race_data.get("mesto_pustanja") or ""))
    ime_staze = sanitize_filename(
        str(
            race_data.get("ime_staze")
            or race_data.get("staza")
            or race_data.get("grupacija")
            or ""
        )
    )
    pilot = sanitize_filename(pilot_name)
    base_name = f"{broj_trke}_{mesto_pustanja}_{ime_staze}_{pilot}_{datum}"
    json_path = output_dir / f"{base_name}.json"
    pdf_path = output_dir / f"{base_name}.pdf"
    txt_path = output_dir / f"{base_name}.txt"
    if json_path.exists() or pdf_path.exists() or txt_path.exists():
        suffix = datetime.now().strftime("%H%M%S")
        base_name = f"{base_name}_{suffix}"
    return (
        output_dir / f"{base_name}.json",
        output_dir / f"{base_name}.pdf",
        output_dir / f"{base_name}.txt",
    )


def save_raw_com_text_export(race_data: dict[str, Any], target_path: Path) -> None:
    """
    Isti sadržaj kao sa COM-a (raw_com_text), UTF-8, LF — za uvoz u Analizu leta ili druge alate
    bez PDF preloma redova.
    """
    raw = race_data.get("raw_com_text")
    body = raw if isinstance(raw, str) else ""
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    clear_export_file_readonly(target_path)
    target_path.write_text(normalized, encoding="utf-8", newline="\n")
    set_export_file_readonly(target_path)


def validate_race_data(payload: dict[str, Any]) -> None:
    if not payload:
        raise ValueError("Podaci trke su prazni.")
    missing = [field for field in REQUIRED_RACE_FIELDS if field not in payload]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"Nedostaju obavezna polja: {missing_text}")


def format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def register_font_if_available() -> str:
    font_candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    for font_path in font_candidates:
        if font_path.exists():
            pdfmetrics.registerFont(TTFont("CustomSans", str(font_path)))
            return "CustomSans"
    return "Helvetica"


_tauris_mono_pdf_font: str | None = None


def register_mono_font_for_pdf() -> str:
    """Courier u PDF-u često nema č/ć — koristi Consolas ili cour.ttf sa Windows-a."""
    global _tauris_mono_pdf_font
    if _tauris_mono_pdf_font is not None:
        return _tauris_mono_pdf_font
    for font_path in (
        Path("C:/Windows/Fonts/consola.ttf"),
        Path("C:/Windows/Fonts/lucon.ttf"),
        Path("C:/Windows/Fonts/cour.ttf"),
    ):
        if font_path.exists():
            try:
                pdfmetrics.registerFont(TTFont("TaurisMono", str(font_path)))
                _tauris_mono_pdf_font = "TaurisMono"
                return _tauris_mono_pdf_font
            except Exception:
                logging.exception("Registracija monospace fonta za PDF.")
    _tauris_mono_pdf_font = "Courier"
    return _tauris_mono_pdf_font


def _wrap_long_lines(text: str, max_chars: int = 102) -> str:
    """Prelomi jako duge linije da ne izlaze iz širine strane u PDF-u."""
    out: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if len(line) <= max_chars:
            out.append(line)
            continue
        for i in range(0, len(line), max_chars):
            out.append(line[i : i + max_chars])
    return "\n".join(out)


def generate_pdf_log_style(
    race_data: dict[str, Any],
    output_path: Path,
    pilot_name: str,
    port_name: str,
) -> None:
    """
    PDF u stilu izlaza iz Analize leta: naslov + monospaced ceo tekst liste sa uređaja.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer

    raw = str(race_data.get("raw_com_text") or "").strip()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    head = (
        "TAURIS — izlazni dokument\n"
        f"Takmičar: {pilot_name}\n"
        f"Grupacija: {race_data.get('grupacija', '')}\n"
        f"Broj trke: {race_data.get('broj_trke', '')}\n"
        f"Mesto puštanja: {race_data.get('mesto_pustanja', '')}\n"
        f"Port: {port_name}\n"
        f"Generisano: {generated_at}\n"
        f"race_id: {race_data.get('race_id', '')}\n"
        f"start_time: {race_data.get('start_time', '')}\n"
        + ("-" * 78 + "\n\n")
    )

    body = _wrap_long_lines(head + raw, max_chars=102)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=11 * mm,
        rightMargin=11 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    mono_face = register_mono_font_for_pdf()
    title_style = ParagraphStyle(
        "TaurisLogTitle",
        parent=styles["Normal"],
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=4 * mm,
        fontName="Helvetica-Bold",
        alignment=TA_LEFT,
    )

    mono_style = ParagraphStyle(
        "TaurisLogMono",
        parent=styles["Code"],
        fontName=mono_face,
        fontSize=8,
        leading=9.5,
        textColor=colors.black,
        alignment=TA_LEFT,
        leftIndent=0,
        rightIndent=0,
    )
    story = [
        Paragraph("TAURIS / Analiza leta — izlazni dokument", title_style),
        Spacer(1, 2 * mm),
        Preformatted(body, mono_style),
    ]
    doc.build(story)


def draw_qr(doc: canvas.Canvas, text: str, x: float, y: float, size: float) -> None:
    if QrCodeWidget is None:
        return
    qr = QrCodeWidget(text)
    bounds = qr.getBounds()
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    if width <= 0 or height <= 0:
        return
    scale_x = size / width
    scale_y = size / height
    # QrCodeWidget.draw() vraća Group, a renderPDF.draw() traži Drawing (sa renderScale).
    group = qr.draw()
    group.scale(scale_x, scale_y)
    dw = float(width * scale_x)
    dh = float(height * scale_y)
    drawing = Drawing(dw, dh)
    drawing.add(group)
    renderPDF.draw(drawing, doc, x, y)


def generate_pdf(
    race_data: dict[str, Any],
    output_path: Path,
    pilot_name: str,
    port_name: str,
) -> None:
    clear_export_file_readonly(output_path)
    raw_txt = race_data.get("raw_com_text")
    if isinstance(raw_txt, str) and len(raw_txt.strip()) > 40:
        generate_pdf_log_style(race_data, output_path, pilot_name, port_name)
        set_export_file_readonly(output_path)
        return

    doc = canvas.Canvas(str(output_path), pagesize=A4)
    width, height = A4
    font_name = register_font_if_available()

    left = 18 * mm
    right = width - (18 * mm)
    top = height - (20 * mm)

    doc.setLineWidth(1)
    doc.rect(left, 15 * mm, right - left, height - (35 * mm))

    doc.setFont(font_name, 17)
    doc.drawString(left + 6, top, "TAURIS - Izvestaj trke")

    doc.setFont(font_name, 10)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc.drawString(left + 6, top - 16, f"Takmičar: {pilot_name}")
    doc.drawString(left + 220, top - 16, f"Port: {port_name}")
    doc.drawString(left + 6, top - 30, f"Datum generisanja: {generated_at}")

    qr_payload = json.dumps(
        {
            "pilot": pilot_name,
            "port": port_name,
            "generated_at": generated_at,
            "race_id": race_data.get("race_id", ""),
            "grupacija": race_data.get("grupacija", ""),
            "broj_trke": race_data.get("broj_trke", ""),
            "mesto_pustanja": race_data.get("mesto_pustanja", ""),
        },
        ensure_ascii=True,
    )
    draw_qr(doc, qr_payload, right - (35 * mm), top - (20 * mm), 22 * mm)

    y = top - 50
    row_height = 8 * mm
    key_width = 55 * mm

    doc.setFont(font_name, 10)
    for key, value in race_data.items():
        if y < 30 * mm:
            doc.showPage()
            doc.setFont(font_name, 10)
            y = height - (25 * mm)
        doc.rect(left + 6, y - 2, key_width, row_height, stroke=1, fill=0)
        doc.rect(left + 6 + key_width, y - 2, (right - left) - key_width - 12, row_height, stroke=1, fill=0)
        doc.drawString(left + 10, y + 3, str(key)[:28])
        doc.drawString(left + 12 + key_width, y + 3, format_value(value)[:75])
        y -= row_height

    doc.save()
    set_export_file_readonly(output_path)


def build_protected_package(
    race_data: dict[str, Any],
    pilot_name: str,
    port_name: str,
    signing_key: str,
    pdf_bytes: bytes,
) -> dict[str, Any]:
    """
    Potpis pokriva i kanonski hash podataka trke i SHA-256 celog PDF fajla (bajt-po-bajt).
    Izmena PDF-a ili JSON-a bez tajnog ključa menja potpis / hash i otkriva se pri proveri.
    """
    canonical = json.dumps(race_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    data_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_hmac_sha256 = hmac.new(signing_key.encode("utf-8"), pdf_bytes, hashlib.sha256).hexdigest()
    sign_payload = f"{data_hash}\n{pdf_sha256}".encode("utf-8")
    signature = hmac.new(signing_key.encode("utf-8"), sign_payload, hashlib.sha256).hexdigest()
    return {
        "meta": {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "takmicar": pilot_name,
            "port": port_name,
            "pdf_sha256": pdf_sha256,
            "pdf_hmac_sha256": pdf_hmac_sha256,
            "integrity": "Potpis HMAC pokriva data_hash + pdf_sha256; pdf_hmac_sha256 je HMAC celog PDF-a.",
        },
        "race_data": race_data,
        "data_hash": data_hash,
        "signature_hmac_sha256": signature,
    }


def verify_tauris_export(json_path: Path, pdf_path: Path, signing_key: str) -> tuple[bool, str]:
    """
    Provera za primaoca: da li se PDF poklapa sa JSON-om i da li je paket potpisan datim ključem.
    Poziv iz drugog skripta ili REPL: verify_tauris_export(Path('a.json'), Path('a.pdf'), os.environ['TAURIS_SIGNING_KEY'])
    """
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Neispravan JSON: {exc}"

    if not pdf_path.is_file():
        return False, f"Nema PDF fajla: {pdf_path}"

    pdf_bytes = pdf_path.read_bytes()
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return False, "Nedostaje meta u JSON-u."

    expected_pdf_sha = meta.get("pdf_sha256")
    if not expected_pdf_sha:
        return False, "Stari JSON bez meta.pdf_sha256 — ponovo generišite iz programa."

    actual_pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    if actual_pdf_sha != expected_pdf_sha:
        return False, "PDF je menjan ili nije par koji odgovara ovom JSON-u (SHA-256 se ne poklapa)."

    race_data = payload.get("race_data")
    if not isinstance(race_data, dict):
        return False, "Nedostaje race_data."

    canonical = json.dumps(race_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    recalc_data_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if recalc_data_hash != payload.get("data_hash"):
        return False, "Podaci trke u JSON-u su menjani (data_hash ne odgovara)."

    sign_payload = f"{recalc_data_hash}\n{actual_pdf_sha}".encode("utf-8")
    sig_ok = hmac.new(signing_key.encode("utf-8"), sign_payload, hashlib.sha256).hexdigest()
    if sig_ok != payload.get("signature_hmac_sha256"):
        return False, "HMAC potpis nije ispravan (pogrešan TAURIS_SIGNING_KEY ili izmenjen sadržaj)."

    pdf_hmac = hmac.new(signing_key.encode("utf-8"), pdf_bytes, hashlib.sha256).hexdigest()
    if pdf_hmac != meta.get("pdf_hmac_sha256"):
        return False, "meta.pdf_hmac_sha256 ne odgovara PDF-u i ključu."

    return True, "OK: PDF i JSON su autentični i nisu menjani od potpisivanja."


def save_protected_json(payload: dict[str, Any], target_path: Path) -> None:
    clear_export_file_readonly(target_path)
    target_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    set_export_file_readonly(target_path)


def send_to_server(
    protected_payload: dict[str, Any],
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    config: DeliveryConfig,
) -> str:
    if not config.server_url:
        raise RuntimeError("TAURIS_SERVER_URL nije podesena.")

    try:
        import requests  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Nedostaje requests paket. Instalirajte: pip install requests") from exc

    headers: dict[str, str] = {}
    if config.server_token:
        headers["Authorization"] = f"Bearer {config.server_token}"

    files: dict[str, tuple[str, bytes, str]] = {
        "json_file": (json_path.name, json_path.read_bytes(), "application/json"),
        "pdf_file": (pdf_path.name, pdf_path.read_bytes(), "application/pdf"),
        "payload": ("payload.json", json.dumps(protected_payload).encode("utf-8"), "application/json"),
    }
    if txt_path.is_file() and txt_path.stat().st_size > 0:
        files["txt_file"] = (txt_path.name, txt_path.read_bytes(), "text/plain; charset=utf-8")
    response = requests.post(
        config.server_url,
        headers=headers,
        files=files,
        timeout=config.timeout_sec,
    )
    response.raise_for_status()
    return f"Poslato na server ({config.server_url})"


def send_email(
    protected_payload: dict[str, Any],
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    config: DeliveryConfig,
) -> str:
    required = [
        config.smtp_host,
        config.smtp_user,
        config.smtp_password,
        config.mail_from,
        config.mail_to,
    ]
    if not all(required):
        raise RuntimeError("SMTP podešavanja nisu kompletna (host/user/pass/from/to).")

    message = EmailMessage()
    message["Subject"] = "TAURIS rezultati trke"
    message["From"] = config.mail_from
    message["To"] = config.mail_to
    meta = protected_payload.get("meta") or {}
    pdf_sha = meta.get("pdf_sha256", "")
    _txt_note = ""
    if txt_path.is_file() and txt_path.stat().st_size > 0:
        _txt_note = (
            "Prilog .txt je ista raw lista sa COM-a (UTF-8), pogodna za uvoz u Analizu leta bez PDF preloma.\n\n"
        )
    message.set_content(
        "Automatski poslati rezultati sa TAURIS uređaja.\n\n"
        + _txt_note
        + "Provera integriteta PDF-a: izračunajte SHA-256 PDF fajla i uporedite sa meta.pdf_sha256 u JSON-u.\n"
        "Potpis HMAC u JSON-u pokriva i podatke trke i SHA-256 PDF-a (isti TAURIS_SIGNING_KEY kao kod pošiljaoca).\n\n"
        f"Hash podataka trke (data_hash): {protected_payload['data_hash']}\n"
        f"SHA-256 PDF: {pdf_sha}\n"
        f"Potpis (signature_hmac_sha256): {protected_payload['signature_hmac_sha256']}\n"
    )
    message.add_attachment(
        json_path.read_bytes(),
        maintype="application",
        subtype="json",
        filename=json_path.name,
    )
    message.add_attachment(
        pdf_path.read_bytes(),
        maintype="application",
        subtype="pdf",
        filename=pdf_path.name,
    )
    if txt_path.is_file() and txt_path.stat().st_size > 0:
        message.add_attachment(
            txt_path.read_bytes(),
            maintype="text",
            subtype="plain",
            filename=txt_path.name,
        )

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_sec) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)
    return f"Poslato na email ({config.mail_to})"


def transmit_results(
    protected_payload: dict[str, Any],
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    delivery_config: DeliveryConfig,
) -> str:
    if delivery_config.mode == "none":
        return "Lokalno sačuvano (slanje nije uključeno)."
    if delivery_config.mode == "server":
        return send_to_server(protected_payload, json_path, pdf_path, txt_path, delivery_config)
    if delivery_config.mode == "email":
        return send_email(protected_payload, json_path, pdf_path, txt_path, delivery_config)
    return "Lokalno sačuvano (nepoznat mode slanja)."


def run_pipeline(
    race_data: dict[str, Any],
    pilot_name: str,
    port_name: str,
    delivery_config: DeliveryConfig,
) -> tuple[Path, str, bytes]:
    validate_race_data(race_data)
    pilot_effective = resolve_pilot_name_for_output(race_data, pilot_name)
    json_path, pdf_path, txt_path = build_output_paths(race_data, pilot_effective)
    save_raw_com_text_export(race_data, txt_path)
    generate_pdf(race_data, pdf_path, pilot_effective, port_name)
    pdf_bytes = pdf_path.read_bytes()
    protected_payload = build_protected_package(
        race_data=race_data,
        pilot_name=pilot_effective,
        port_name=port_name,
        signing_key=delivery_config.signing_key,
        pdf_bytes=pdf_bytes,
    )
    save_protected_json(protected_payload, json_path)
    locked_bundle_path = create_locked_results_bundle(
        json_path=json_path,
        pdf_path=pdf_path,
        txt_path=txt_path,
        password=RESULTS_LOCK_PASSWORD,
    )
    lock_results_folder(json_path.parent)
    logging.info(
        "Integritet PDF: SHA256=%s… (ceo hash u JSON meta.pdf_sha256)",
        protected_payload["meta"]["pdf_sha256"][:16],
    )
    logging.info("Kreiran zaključan paket: %s", locked_bundle_path)
    delivery_note = transmit_results(
        protected_payload, json_path, pdf_path, txt_path, delivery_config
    )
    remove_plain_exports(json_path, pdf_path, txt_path)
    return locked_bundle_path, delivery_note, pdf_bytes


def open_file_default_viewer(path: Path, parent: tk.Misc | None = None) -> None:
    """Otvara fajl podrazumevanim programom (npr. PDF čitač)."""
    try:
        p = path.resolve()
        if not p.is_file():
            logging.warning("Fajl za pregled ne postoji: %s", p)
            return
        key_file = request_access_key_file(parent)
        if key_file is None:
            return
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", str(p)], start_new_session=True)
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(p)], start_new_session=True)
    except Exception:
        logging.exception("Neuspesno otvaranje fajla u pregledu: %s", path)


def notify_download_complete(parent: tk.Tk, message: str = "Preuzimanje gotovo.") -> None:
    """Zvučni signal + kratak toast da se ne promasi završetak ako je PDF iza drugih prozora."""
    try:
        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass
    try:
        parent.bell()
    except Exception:
        pass

    toast = tk.Toplevel(parent)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    sw = toast.winfo_screenwidth()
    sh = toast.winfo_screenheight()
    tw, th = 300, 58
    toast.geometry(f"{tw}x{th}+{sw - tw - 20}+{sh - th - 72}")
    frame = tk.Frame(toast, bg="#1e3a5f", padx=16, pady=12)
    frame.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        frame,
        text=message,
        fg="#f8fafc",
        bg="#1e3a5f",
        font=("Segoe UI", 11),
    ).pack(anchor="w")
    toast.after(3200, toast.destroy)


def show_download_result_preview(
    parent: tk.Tk,
    locked_bundle_path: Path,
    race_data: dict[str, Any],
    delivery_note: str,
) -> None:
    """Kratak pregled šta je sačuvano nakon preuzimanja."""
    info_win = tk.Toplevel(parent)
    info_win.title("Pregled preuzimanja")
    info_win.transient(parent)
    info_win.resizable(False, False)

    sw = info_win.winfo_screenwidth()
    sh = info_win.winfo_screenheight()
    ww = min(760, max(560, int(sw * 0.50)))
    wh = min(430, max(320, int(sh * 0.42)))
    try:
        px = parent.winfo_rootx() + (parent.winfo_width() // 2) - ww // 2
        py = parent.winfo_rooty() + (parent.winfo_height() // 2) - wh // 2
        info_win.geometry(f"{ww}x{wh}+{max(0, px)}+{max(0, py)}")
    except Exception:
        info_win.geometry(f"{ww}x{wh}")

    wrap = ww - 42
    frame = tk.Frame(info_win, padx=14, pady=12)
    frame.pack(fill=tk.BOTH, expand=True)

    pilot = resolve_pilot_name_for_output(race_data, "")
    grupacija = str(race_data.get("grupacija") or "")
    broj = str(race_data.get("broj_trke") or "")
    mesto = str(race_data.get("mesto_pustanja") or "")
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    lines = [
        f"Vreme završetka: {stamp}",
        f"Takmičar: {pilot}",
        f"Grupacija / broj trke: {grupacija} / {broj}",
        f"Mesto puštanja: {mesto}",
        f"Sačuvano: {locked_bundle_path.name}",
        f"Lokacija: {locked_bundle_path.parent}",
        f"Status slanja: {delivery_note}",
    ]
    tk.Label(
        frame,
        text="Preuzimanje završeno (zaključan paket)",
        font=("Segoe UI", 11, "bold"),
        anchor="w",
        justify="left",
    ).pack(fill=tk.X, pady=(0, 8))
    tk.Label(
        frame,
        text="\n".join(lines),
        anchor="w",
        justify="left",
        wraplength=wrap,
        fg="#1f2937",
    ).pack(fill=tk.BOTH, expand=True)

    btns = tk.Frame(frame)
    btns.pack(fill=tk.X, pady=(10, 0))
    tk.Button(
        btns,
        text="Otvori folder",
        width=16,
        command=lambda p=locked_bundle_path.parent: os.startfile(str(p)),  # type: ignore[attr-defined]
    ).pack(side=tk.LEFT)
    tk.Button(btns, text="Zatvori", width=12, command=info_win.destroy).pack(side=tk.RIGHT)

    info_win.lift()
    info_win.attributes("-topmost", True)
    info_win.after(450, lambda w=info_win: w.attributes("-topmost", False))


def show_pdf_embedded_preview(parent: tk.Tk, pdf_path: Path) -> None:
    """
    U posebnom prozoru srednje veličine (ne pun ekran) prikaže stranice PDF-a sa skrolovanjem.
    Ako nema PyMuPDF/Pillow, pada na spoljni pregledač.
    """
    try:
        import fitz  # type: ignore  # PyMuPDF
        from PIL import Image, ImageTk  # type: ignore
    except ImportError:
        logging.warning("Ugradjeni PDF pregled zahteva: pip install PyMuPDF Pillow")
        open_file_default_viewer(pdf_path, parent=parent)
        return

    p = pdf_path.resolve()
    if not p.is_file():
        logging.warning("PDF ne postoji: %s", p)
        return

    pdf_win = tk.Toplevel(parent)
    pdf_win.title(f"PDF — {pdf_path.name}")
    pdf_win.transient(parent)

    sw = pdf_win.winfo_screenwidth()
    sh = pdf_win.winfo_screenheight()
    ww = min(880, max(540, int(sw * 0.52)))
    wh = min(780, max(420, int(sh * 0.68)))
    try:
        px = parent.winfo_rootx() + (parent.winfo_width() // 2) - ww // 2
        py = parent.winfo_rooty() + (parent.winfo_height() // 2) - wh // 2
        pdf_win.geometry(f"{ww}x{wh}+{max(0, px)}+{max(0, py)}")
    except Exception:
        pdf_win.geometry(f"{ww}x{wh}")

    outer = tk.Frame(pdf_win)
    outer.pack(fill=tk.BOTH, expand=True)
    canvas = tk.Canvas(outer, bg="#4a4a4a", highlightthickness=0)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    inner = tk.Frame(canvas, bg="#4a4a4a")
    canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

    photos: list[Any] = []
    doc = fitz.open(str(p))
    scale = min(1.55, max(1.0, ww / 600))
    mat = fitz.Matrix(scale, scale)
    try:
        for i in range(len(doc)):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            im = Image.open(io.BytesIO(pix.tobytes("png")))
            photo = ImageTk.PhotoImage(im)
            photos.append(photo)
            tk.Label(inner, image=photo, bg="white").pack(pady=(0, 6))
    finally:
        doc.close()

    pdf_win._photo_refs = photos  # zadrži slike od GC

    def _scroll_region(_event: Any = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def _fit_inner_width(event: Any) -> None:
        canvas.itemconfigure(canvas_window, width=event.width)

    inner.bind("<Configure>", lambda e: _scroll_region())
    canvas.bind("<Configure>", _fit_inner_width)

    def _wheel(event: Any) -> None:
        if sys.platform == "darwin":
            canvas.yview_scroll(int(-1 * event.delta), "units")
        else:
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    pdf_win.bind("<MouseWheel>", _wheel)
    canvas.bind("<MouseWheel>", _wheel)
    try:
        pdf_win.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        pdf_win.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
    except Exception:
        pass

    bottom = tk.Frame(pdf_win)
    bottom.pack(fill=tk.X, pady=(0, 8))
    tk.Button(bottom, text="Zatvori pregled", command=pdf_win.destroy, width=16).pack(side=tk.RIGHT, padx=10)
    tk.Button(
        bottom,
        text="Otvori u spoljnom programu…",
        command=lambda: open_file_default_viewer(p, parent=pdf_win),
        width=26,
    ).pack(
        side=tk.RIGHT, padx=6
    )

    pdf_win.update_idletasks()
    pdf_win.lift()
    pdf_win.attributes("-topmost", True)
    pdf_win.after(450, lambda w=pdf_win: w.attributes("-topmost", False))


class TaurisApp:
    def __init__(self, delivery_config: DeliveryConfig) -> None:
        self.delivery_config = delivery_config
        self.connected = False
        self.busy = False
        self.worker_queue: Queue[tuple[str, Any]] = Queue()
        self.root = tk.Tk()
        self.root.title("Tauris Povezivanje ver1.0")
        self.window_width = 520
        self.window_height = 485
        self._set_window_icon()
        self._center_window()
        self.root.resizable(False, False)

        self.port_var = tk.StringVar(value="")
        self.baud_var = tk.StringVar(value="9600")
        self.grupacija_var = tk.StringVar(value=GRUPACIJA_LABELS[0])
        self.broj_trke_var = tk.StringVar(value=BROJ_TRKE_LABELS[0])
        self.mesto_var = tk.StringVar(value="")
        self.pilot_var = tk.StringVar(value=UNKNOWN_PILOT_LABEL)
        self.status_var = tk.StringVar(value="Status: cekam povezivanje na TAURIS sat.")
        self.last_locked_bundle_path: Path | None = None

        self._build_ui()
        self._load_gui_settings_into_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._poll_worker_queue)

    def _load_gui_settings_into_ui(self) -> None:
        d = load_gui_settings_dict()
        if not d:
            return
        baud = str(d.get("baud") or "").strip()
        if baud in _BAUD_CHOICES:
            self.baud_var.set(baud)
        g = str(d.get("grupacija") or "").strip()
        if g in GRUPACIJA_LABELS:
            self.grupacija_var.set(g)
        b = str(d.get("broj_trke") or "").strip()
        if b in BROJ_TRKE_LABELS:
            self.broj_trke_var.set(b)
        self.mesto_var.set(str(d.get("mesto_pustanja") or ""))
        pilot = str(d.get("pilot") or "").strip()
        if pilot:
            self.pilot_var.set(pilot)
        port = str(d.get("port") or "").strip()
        ports = list(self.port_combo["values"] or [])
        if port and ports and port in ports:
            self.port_var.set(port)

    def _persist_gui_settings(self) -> None:
        save_gui_settings_dict(
            {
                "port": (self.port_var.get() or "").strip(),
                "baud": (self.baud_var.get() or "").strip(),
                "grupacija": (self.grupacija_var.get() or "").strip(),
                "broj_trke": (self.broj_trke_var.get() or "").strip(),
                "mesto_pustanja": (self.mesto_var.get() or "").strip(),
                "pilot": (self.pilot_var.get() or "").strip(),
            }
        )

    def _on_close(self) -> None:
        try:
            self._persist_gui_settings()
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _set_window_icon(self) -> None:
        icon_path = get_resource_path("ICON/PIGEONp.png")
        if not icon_path.exists():
            icon_path = get_resource_path("ICON/pigeon.png")
        if not icon_path.exists():
            logging.warning("Ikonica nije pronadjena: %s", icon_path)
            return
        try:
            self.window_icon_image = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.window_icon_image)
        except Exception:
            logging.exception("Neuspesno postavljanje ikonice prozora.")

    def _center_window(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - self.window_width) // 2
        y = (screen_height - self.window_height) // 2
        self.root.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")

    def _build_ui(self) -> None:
        frame = tk.Frame(self.root, padx=16, pady=16)
        frame.pack(fill="both", expand=True)

        content = tk.Frame(frame)
        content.place(relx=0.5, rely=0.48, anchor="center")

        title = tk.Label(
            content,
            text="Povezi se sa TAURIS satom",
            font=("Segoe UI", 12, "bold"),
        )
        title.pack(anchor="center", pady=(0, 12))

        port_row = tk.Frame(content)
        port_row.pack(anchor="center", pady=(0, 8))
        tk.Label(port_row, text="Port:", width=18, anchor="w").pack(side="left")
        self.port_combo = ttk.Combobox(
            port_row,
            textvariable=self.port_var,
            width=18,
            state="readonly",
        )
        self.port_combo.pack(side="left", padx=(0, 8))
        self.refresh_btn = tk.Button(
            port_row,
            text="Osvezi",
            width=9,
            command=self.refresh_ports,
        )
        self.refresh_btn.pack(side="left")

        baud_row = tk.Frame(content)
        baud_row.pack(anchor="center", pady=(0, 8))
        tk.Label(baud_row, text="Baud rate:", width=18, anchor="w").pack(side="left")
        self.baud_combo = ttk.Combobox(
            baud_row,
            textvariable=self.baud_var,
            width=16,
            state="readonly",
            values=_BAUD_CHOICES,
        )
        self.baud_combo.pack(side="left")

        grupacija_row = tk.Frame(content)
        grupacija_row.pack(anchor="center", pady=(0, 8))
        tk.Label(grupacija_row, text="Grupacija:", width=18, anchor="w").pack(side="left")
        self.grupacija_combo = ttk.Combobox(
            grupacija_row,
            textvariable=self.grupacija_var,
            values=GRUPACIJA_LABELS,
            width=27,
            state="readonly",
        )
        self.grupacija_combo.pack(side="left")

        broj_trke_row = tk.Frame(content)
        broj_trke_row.pack(anchor="center", pady=(0, 8))
        tk.Label(broj_trke_row, text="Broj trke:", width=18, anchor="w").pack(side="left")
        self.broj_trke_combo = ttk.Combobox(
            broj_trke_row,
            textvariable=self.broj_trke_var,
            values=BROJ_TRKE_LABELS,
            width=7,
            state="readonly",
        )
        self.broj_trke_combo.pack(side="left")

        mesto_row = tk.Frame(content)
        mesto_row.pack(anchor="center", pady=(0, 8))
        tk.Label(mesto_row, text="Mesto puštanja:", width=18, anchor="w").pack(side="left")
        self.mesto_entry = tk.Entry(mesto_row, textvariable=self.mesto_var, width=30)
        self.mesto_entry.pack(side="left")

        pilot_row = tk.Frame(content)
        pilot_row.pack(anchor="center", pady=(0, 12))
        tk.Label(pilot_row, text="Takmičar:", width=18, anchor="w").pack(side="left")
        tk.Entry(pilot_row, textvariable=self.pilot_var, width=30).pack(side="left")

        button_row = tk.Frame(content)
        button_row.pack(anchor="center", pady=(4, 10))

        self.connect_btn = tk.Button(
            button_row,
            text="Povezi se",
            width=15,
            command=self.connect_device,
        )
        self.connect_btn.pack(side="left", padx=(0, 8))

        self.download_btn = tk.Button(
            button_row,
            text="Preuzmi podatke",
            width=15,
            state="disabled",
            command=self.download_data,
        )
        self.download_btn.pack(side="left", padx=(0, 8))

        self.open_locked_btn = tk.Button(
            button_row,
            text="Otvori zaključan paket",
            width=21,
            command=self.open_locked_bundle,
        )
        self.open_locked_btn.pack(side="left")

        status_label = tk.Label(
            content,
            textvariable=self.status_var,
            fg="#1f2937",
            wraplength=470,
            justify="center",
        )
        status_label.pack(anchor="center", pady=(4, 0))

        footer_label = tk.Label(
            self.root,
            text="by Dragan Todorović",
            fg="#6b7280",
            font=("Segoe UI", 9, "italic"),
        )
        footer_label.place(relx=1.0, rely=1.0, anchor="se", x=-10, y=-8)
        self.refresh_ports()

    def _set_busy(self, is_busy: bool) -> None:
        self.busy = is_busy
        common_state = "disabled" if is_busy else "normal"
        self.connect_btn.config(state=common_state)
        self.refresh_btn.config(state=common_state)
        self.baud_combo.config(state=common_state)
        self.open_locked_btn.config(state=common_state)
        self.grupacija_combo.config(state="disabled" if is_busy else "readonly")
        self.broj_trke_combo.config(state="disabled" if is_busy else "readonly")
        self.mesto_entry.config(state=common_state)
        if is_busy:
            self.download_btn.config(state="disabled")
            return
        self.download_btn.config(state="normal" if self.connected else "disabled")

    def refresh_ports(self) -> None:
        ports = self._list_available_ports()
        self.port_combo["values"] = ports
        if not ports:
            self.port_var.set("")
            self.status_var.set("Status: nema dostupnih COM portova. Proverite kabl/uredjaj.")
            return
        current = self.port_var.get().strip()
        if current in ports:
            return
        self.port_var.set(ports[0])
        self.status_var.set("Status: izaberite port i kliknite 'Povezi se'.")

    def _list_available_ports(self) -> list[str]:
        if list_ports is None:
            return []
        return [port.device for port in list_ports.comports()]

    def _capture_staza_from_ui(self) -> tuple[str, int, str]:
        """Čitanje staze sa glavne niti pre pozadinskog preuzimanja."""
        g = (self.grupacija_var.get() or GRUPACIJA_LABELS[0]).strip()
        if g not in GRUPACIJA_LABELS:
            g = GRUPACIJA_LABELS[0]
        b = (self.broj_trke_var.get() or BROJ_TRKE_LABELS[0]).strip()
        if b not in BROJ_TRKE_LABELS:
            b = BROJ_TRKE_LABELS[0]
        try:
            n = int(b, 10)
        except ValueError:
            n = 1
        m = (self.mesto_var.get() or "").strip()
        return g, n, m

    def _get_config(self) -> TaurisConnectionConfig:
        try:
            baud = int(self.baud_var.get().strip() or "9600")
        except ValueError:
            baud = 9600
        return TaurisConnectionConfig(
            port=self.port_var.get().strip(),
            baudrate=baud,
        )

    def connect_device(self) -> None:
        if serial is None:
            messagebox.showerror("Greska", "Nedostaje pyserial paket. Instalirajte: pip install pyserial")
            return
        if self.busy:
            return

        self.refresh_ports()
        config = self._get_config()
        if not config.port:
            messagebox.showwarning("Upozorenje", "Izaberite serijski port sa liste.")
            return

        self._set_busy(True)
        self.status_var.set(
            "Status: povezujem se… Čitam COM (brzo završava kad uređaj prestane slanje; max ~12 s). "
            "Na TAURIS-u odmah pokrenite slanje liste ako ime ne stiže samo od sebe."
        )
        self.root.update_idletasks()

        def worker() -> None:
            exc: Exception | None = None
            ime: str | None = None
            try:
                _text, ime = read_com_until_takmicar_or_done(
                    config,
                    max_iterations=TAURIS_CONNECT_MAX_ITERATIONS,
                    stop_on_name=True,
                )
                if ime is None and _text.strip():
                    ime = extract_takmicar_name_from_raw_text(_text)
            except Exception as e:
                exc = e
                logging.exception("Greska pri povezivanju / citanju takmicara.")

            exc_captured = exc
            ime_captured = ime

            def done() -> None:
                self._set_busy(False)
                if exc_captured is not None:
                    self.connected = False
                    self.download_btn.config(state="disabled")
                    self.status_var.set("Status: povezivanje neuspesno.")
                    messagebox.showerror("Greska pri povezivanju", str(exc_captured))
                    return
                if ime_captured:
                    self.pilot_var.set(ime_captured)
                    logging.info("Takmicar iz uređaja (pri povezivanju): %s", ime_captured)
                self.connected = True
                self.download_btn.config(state="normal")
                if ime_captured:
                    self.status_var.set(
                        f"Status: povezano. Takmičar: {ime_captured}. "
                        "Za puno preuzimanje liste kliknite 'Preuzmi podatke' i ponovo pokrenite slanje na uređaju."
                    )
                else:
                    self.status_var.set(
                        "Status: uređaj povezan. Ime nije pročitano — pokrenite slanje na TAURIS-u pa "
                        "kliknite 'Preuzmi podatke', ili ponovo 'Poveži se' dok uređaj šalje podatke."
                    )
                logging.info("Uspesno povezivanje na port %s", config.port)
                messagebox.showinfo("Uspesno", "TAURIS uređaj je povezan.")

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def download_data(self) -> None:
        if not self.connected:
            messagebox.showwarning("Upozorenje", "Prvo kliknite na 'Povezi se'.")
            return
        if self.busy:
            return

        self._set_busy(True)
        self.status_var.set(
            "Status: čitam COM (uglavnom nekoliko sekundi posle slanja). Na TAURIS-u pokrenite slanje podataka."
        )
        config = self._get_config()
        pilot_name = self.pilot_var.get().strip() or UNKNOWN_PILOT_LABEL
        staza = self._capture_staza_from_ui()

        thread = threading.Thread(
            target=self._worker_live_download,
            args=(config, pilot_name, staza),
            daemon=True,
        )
        thread.start()

    def _worker_live_download(
        self,
        config: TaurisConnectionConfig,
        pilot_name: str,
        staza: tuple[str, int, str],
    ) -> None:
        try:
            race_data = read_race_from_device(config)
            g, n, m = staza
            race_data = apply_staza_ui_to_race_data(
                race_data,
                grupacija_label=g,
                broj_trke_num=n,
                mesto_pustanja=m,
            )
            locked_bundle_path, delivery_note, preview_pdf_bytes = run_pipeline(
                race_data,
                pilot_name,
                config.port,
                self.delivery_config,
            )
            self.worker_queue.put(
                ("ok", (locked_bundle_path, delivery_note, race_data, preview_pdf_bytes))
            )
            logging.info("Preuzimanje uspesno sa porta %s", config.port)
        except Exception as exc:
            logging.exception("Greska tokom live preuzimanja.")
            self.worker_queue.put(("error", exc))

    def _poll_worker_queue(self) -> None:
        try:
            event_type, payload = self.worker_queue.get_nowait()
        except Empty:
            self.root.after(200, self._poll_worker_queue)
            return

        self._set_busy(False)
        if event_type == "ok":
            locked_bundle_path, delivery_note, race_data, preview_pdf_bytes = payload
            self.last_locked_bundle_path = locked_bundle_path
            self.status_var.set("Status: gotovo. Sačuvan je samo zaključan paket (.taurislock).")
            if isinstance(race_data, dict):
                ime = None
                for k in ("takmicar", "takmičar", "Takmicar", "ime_takmicara"):
                    v = race_data.get(k)
                    if isinstance(v, str) and v.strip():
                        ime = _clean_takmicar_display(v)
                        break
                if not ime:
                    raw = race_data.get("raw_com_text")
                    if isinstance(raw, str) and raw.strip():
                        ime = extract_takmicar_name_from_raw_text(raw)
                if ime:
                    self.pilot_var.set(ime)
            logging.info(
                "Zaključan paket sačuvan — %s | %s",
                locked_bundle_path,
                delivery_note,
            )
            self.root.after(80, lambda: notify_download_complete(self.root))
            self.root.after(
                180,
                lambda p=locked_bundle_path, rd=race_data, dn=delivery_note: show_download_result_preview(
                    self.root, p, rd if isinstance(rd, dict) else {}, dn
                ),
            )
            try:
                preview_path = write_preview_pdf_temp(preview_pdf_bytes)
                self.root.after(260, lambda p=preview_path: show_pdf_embedded_preview(self.root, p))
                self.root.after(240000, lambda p=preview_path: p.unlink(missing_ok=True))
            except Exception:
                logging.exception("Neuspešna priprema PDF preview-a iz privremenog fajla.")
        else:
            self.status_var.set("Status: greska tokom obrade.")
            messagebox.showerror("Greska", str(payload))
        self.root.after(200, self._poll_worker_queue)

    def open_locked_bundle(self) -> None:
        initial_dir = RESULTS_DESKTOP_ROOT if RESULTS_DESKTOP_ROOT.exists() else Path.home() / "Desktop"
        initial_file = str(self.last_locked_bundle_path) if self.last_locked_bundle_path else ""
        selected = filedialog.askopenfilename(
            title="Izaberite .taurislock paket",
            initialdir=str(initial_dir),
            initialfile=initial_file,
            filetypes=[("Tauris zaključan paket", "*.taurislock"), ("Svi fajlovi", "*.*")],
        )
        if not selected:
            return
        bundle = Path(selected)
        if not bundle.is_file():
            messagebox.showerror("Greška", "Izabrani fajl ne postoji.")
            return
        entered = simpledialog.askstring(
            "Lozinka",
            "Unesite šifru za otključavanje paketa:",
            show="*",
        )
        if entered is None:
            return
        if entered != RESULTS_LOCK_PASSWORD:
            messagebox.showerror("Greška", "Pogrešna šifra.")
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="tauris_unlocked_"))
        try:
            files = unlock_results_bundle(bundle, entered, tmp_dir)
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            messagebox.showerror("Greška", str(exc))
            return
        self.status_var.set(f"Status: paket otključan privremeno ({tmp_dir}).")
        for p in files:
            if p.suffix.lower() == ".pdf":
                try:
                    os.startfile(str(p))  # type: ignore[attr-defined]
                except Exception:
                    logging.exception("Neuspešno otvaranje PDF-a iz paketa: %s", p)
                break
        try:
            if sys.platform == "win32":
                os.startfile(str(tmp_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(tmp_dir)], start_new_session=True)
            else:
                subprocess.Popen(["xdg-open", str(tmp_dir)], start_new_session=True)
        except Exception:
            logging.exception("Neuspešno otvaranje privremenog foldera: %s", tmp_dir)

        def _cleanup_temp() -> None:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                logging.exception("Neuspešno brisanje privremenog foldera: %s", tmp_dir)

        self.root.after(180000, _cleanup_temp)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    setup_logging()
    if is_app_expired():
        _show_block_message(
            "Program je istekao i zaključan posle 01.05.2027. Kontaktirajte autora za novu verziju."
        )
        return 3
    if len(sys.argv) >= 4 and sys.argv[1].lower() == "verify":
        key = os.getenv("TAURIS_SIGNING_KEY", "").strip()
        if not key or key == "CHANGE_ME_SECRET":
            print(
                "Postavite TAURIS_SIGNING_KEY u okruženju na istu tajnu kao kod pošiljaoca programa "
                "(ne koristite CHANGE_ME_SECRET u produkciji)."
            )
            return 2
        ok, msg = verify_tauris_export(Path(sys.argv[2]), Path(sys.argv[3]), key)
        print(msg)
        return 0 if ok else 1

    delivery_config = load_delivery_config()
    app = TaurisApp(delivery_config)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
