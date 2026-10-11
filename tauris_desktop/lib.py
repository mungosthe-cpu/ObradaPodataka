from __future__ import annotations

import io
import json
import zipfile
import base64
import hashlib
import hmac
import logging
import errno
import os
import re
import shutil
import socket
import smtplib
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from queue import Empty, Queue
import stat
from typing import Any, Callable, Literal
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

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ImportError:  # pragma: no cover
    InvalidTag = ValueError  # type: ignore[assignment]
    hashes = None  # type: ignore[assignment]
    AESGCM = None  # type: ignore[assignment]
    PBKDF2HMAC = None  # type: ignore[assignment]



from tauris_desktop.config import *

LOG_PATH: Path | None = None

_https_ssl_context: ssl.SSLContext | None = None


def resolve_ca_bundle_path() -> str | None:
    """
    Pronađi cacert.pem za HTTPS (GitHub nadogradnja / SMTP).

    U PyInstaller onefile OpenSSL često NE koristi Windows store; mora da postoji
    stvarni CA fajl na disku. ``certifi.where()`` ponekad vrati putanju koja još
    nije izdvojena pored ``sys._MEIPASS``, pa proveravamo više kandidata.
    """
    candidates: list[Path] = []
    try:
        import certifi

        where = certifi.where()
        if where:
            candidates.append(Path(where))
        certifi_file = getattr(certifi, "__file__", None)
        if certifi_file:
            candidates.append(Path(certifi_file).resolve().parent / "cacert.pem")
    except ImportError:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(str(meipass))
        candidates.append(root / "certifi" / "cacert.pem")
        candidates.append(root / "cacert.pem")

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "certifi" / "cacert.pem")
        candidates.append(exe_dir / "cacert.pem")

    seen: set[str] = set()
    for cand in candidates:
        try:
            resolved = str(cand.resolve()) if cand.is_file() else ""
        except OSError:
            resolved = ""
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        return resolved
    return None


def configure_https_certificates() -> None:
    """
    PyInstaller .exe na nekim Windows računarima nema CA bundle za urllib/ssl.
    certifi se pakuje u instalaciju; ovde ga povezujemo pre GitHub/SMTP HTTPS poziva.
    """
    ca = resolve_ca_bundle_path()
    if not ca:
        logging.warning(
            "HTTPS CA bundle nije pronađen (certifi/cacert.pem) — "
            "provera nadogradnje može pasti na SSL."
        )
        return
    # Prepiši pogrešan SSL_CERT_FILE iz okruženja (setdefault bi ga zadržao).
    os.environ["SSL_CERT_FILE"] = ca
    os.environ["REQUESTS_CA_BUNDLE"] = ca
    logging.info("HTTPS CA bundle: %s", ca)


def https_ssl_context() -> ssl.SSLContext:
    global _https_ssl_context
    if _https_ssl_context is not None:
        return _https_ssl_context
    configure_https_certificates()
    ca = resolve_ca_bundle_path()
    if ca:
        # Windows store (ako ga OpenSSL vidi) + certifi kao pouzdan fallback.
        try:
            ctx = ssl.create_default_context()
            ctx.load_verify_locations(cafile=ca)
            _https_ssl_context = ctx
            return _https_ssl_context
        except ssl.SSLError:
            logging.exception("SSL: kombinovani CA kontekst nije uspeo, probam samo certifi.")
            _https_ssl_context = ssl.create_default_context(cafile=ca)
            return _https_ssl_context
    _https_ssl_context = ssl.create_default_context()
    return _https_ssl_context


def _urlopen_https(req: urllib.request.Request, *, timeout: float) -> Any:
    url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
    if str(url).lower().startswith("https:"):
        return urllib.request.urlopen(req, timeout=timeout, context=https_ssl_context())
    return urllib.request.urlopen(req, timeout=timeout)


def read_serial_bytes_until_idle(
    ser: Any,
    config: TaurisConnectionConfig,
    *,
    max_iterations: int | None = None,
    idle_empty_reads: int | None = None,
    on_com_bytes: Callable[[int], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> bytes:
    """
    Skladišti bajtove sa COM-a. Nakon što je nešto primljeno, n uzastopnih praznih read-ova
    (timeout bez podataka) znači da je TAURIS/BRICON završio slanje — ne čekamo fiksno 30 ciklusa.

    Greška pri čitanju (npr. uređaj fizički isključen usred prenosa) prekida petlju i vraća
    ono što je do tada primljeno — pozivalac ne sme izgubiti već primljene bajtove — ali se
    UVEK loguje (nikad tiho) i, ako je prosleđen, prijavljuje pozivaocu preko ``on_error`` da bi
    korisnik dobio jasnu poruku umesto generičkog "nema podataka".
    """
    acc = bytearray()
    idle = 0
    limit = config.read_idle_empty_reads if idle_empty_reads is None else idle_empty_reads
    max_loops = config.read_iterations if max_iterations is None else max_iterations
    for _ in range(max_loops):
        try:
            data = ser.read(config.read_chunk)
        except Exception as exc:
            port_name = getattr(ser, "port", None) or getattr(config, "port", "?")
            logging.warning(
                "Serijsko čitanje prekinuto (port=%s baud=%s primljeno=%sB): %s: %s",
                port_name, getattr(config, "baudrate", "?"), len(acc), type(exc).__name__, exc,
            )
            if on_error is not None:
                on_error(exc)
            break
        if data:
            acc.extend(data)
            idle = 0
            if on_com_bytes is not None:
                on_com_bytes(len(acc))
        elif acc:
            idle += 1
            if idle >= limit:
                break
    return bytes(acc)


def prepare_serial_port_for_passive_listen(ser: Any, *, device_label: str = "uređaj") -> None:
    """
    DTR/RTS za pasivno USB-CDC slušanje (program ne šalje nikakve komande, samo čeka da
    uređaj počne slanje) — deljeno između TAURIS-a (nikad ne šalje komande) i BRICON-ovog
    usb_passive režima, jer je ponašanje ovde stvarno identično po dizajnu.

    RTS=False jer neki jeftini USB-CDC mostovi (isti čipovi kao na Arduino pločama) tumače
    RTS tranziciju kao reset/boot signal i prekidaju vezu — zato se namerno NE diže RTS.

    Ne menjaj ove vrednosti bez testa na stvarnom uređaju.
    """
    try:
        ser.setDTR(True)
        ser.setRTS(False)
    except Exception as exc:
        logging.debug(
            "%s: DTR/RTS podešavanje nije uspelo: %s: %s", device_label, type(exc).__name__, exc
        )
    try:
        ser.reset_input_buffer()
    except Exception as exc:
        logging.debug(
            "%s: reset ulaznog bafera nije uspeo: %s: %s", device_label, type(exc).__name__, exc
        )


def prepare_serial_port_for_bricon(ser: Any, *, mode: str) -> None:
    """
    Postavlja DTR/RTS pre BRICON komunikacije. Jedino mesto koje to radi.

    mode="usb_passive": pasivno slušanje (bez PM komandi, kao TAURIS slušanje) —
        deli logiku sa prepare_serial_port_for_passive_listen.
    mode="pm_protocol": Print Manager binarni protokol. Isto kao
        ``Communication.StartComm`` u BriconLib.dll: ``RtsEnable = True``; .NET SerialPort
        ima ``DtrEnable = false`` podrazumevano, pa ni mi ne dižemo DTR. Baferi se čiste
        pre svake komande (``DiscardInBuffer``/``DiscardOutBuffer``) — i ovde.

    Ne menjaj ove vrednosti bez testa na stvarnom uređaju.
    """
    if mode == "usb_passive":
        prepare_serial_port_for_passive_listen(ser, device_label="BRICON")
        return
    try:
        ser.setDTR(False)
        ser.setRTS(True)
    except Exception as exc:
        logging.debug("BRICON: DTR/RTS podešavanje nije uspelo (mode=%s): %s: %s", mode, type(exc).__name__, exc)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except Exception as exc:
        logging.debug("BRICON: reset bafera nije uspeo (mode=%s): %s: %s", mode, type(exc).__name__, exc)


def bricon_import_initial_dir(last_used: str | None = None) -> str:
    """
    Folder koji «Uvezi BRICON fajl» otvara: poslednji korišćeni (ako još postoji), zatim
    folderi Print Managera (``BRICON_PM_IMPORT_DIRS`` — tamo PM snima očitavanja), na kraju
    Preuzimanja korisnika.
    """
    candidates: list[str] = []
    if last_used:
        candidates.append(str(last_used))
    candidates.extend(BRICON_PM_IMPORT_DIRS)
    for c in candidates:
        try:
            if c and Path(c).is_dir():
                return str(Path(c))
        except OSError:
            continue
    return str(Path.home() / "Downloads")


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


def _dev_project_root() -> Path:
    """Koren repozitorijuma (tauris_desktop/ je podfolder)."""
    return Path(__file__).resolve().parent.parent


def get_resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False):
        base_path = Path(getattr(sys, "_MEIPASS"))
    else:
        base_path = _dev_project_root()
    return base_path / relative_path


def tauris_is_lite_edition() -> bool:
    """Lite instalacija: bez menija za zaključan folder / zaključan paket (marker u PyInstaller paketu)."""
    env = os.environ.get("TAURIS_LITE_EDITION", "").strip().lower()
    if env in ("1", "true", "yes", "lite"):
        return True
    if not getattr(sys, "frozen", False):
        return False
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return False
    return (Path(meipass) / "TAURIS_LITE").is_file()


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
    return _dev_project_root()


def distribution_dir() -> Path:
    """Folder gde je .exe ili koren projekta — ovde tražimo tauris_club_email.json."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _dev_project_root()


# Opciono: stavite tauris_connected.wav (npr. iz YouTube snimka) pored .exe ili u ICON/ u projektu.
TAURIS_CONNECTED_WAV_NAME = "tauris_connected.wav"


def play_tauris_connected_sound() -> None:
    """Kratak znak kad je TAURIS uspešno povezan (Windows; bez blokiranja GUI)."""
    if sys.platform != "win32":
        return
    try:
        import winsound
    except ImportError:
        return
    for cand in (
        distribution_dir() / TAURIS_CONNECTED_WAV_NAME,
        get_resource_path(f"ICON/{TAURIS_CONNECTED_WAV_NAME}"),
    ):
        if not cand.is_file():
            continue
        try:
            winsound.PlaySound(str(cand), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception:
            logging.exception("Neuspešna reprodukcija zvuka povezivanja: %s", cand)
    try:
        winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        logging.exception("Podrazumevani zvuk povezivanja (SystemAsterisk).")


def load_club_email_file() -> dict[str, Any]:
    path = distribution_dir() / TAURIS_CLUB_EMAIL_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            logging.info("Učitani klupski email podaci iz %s", path)
            return data
    except Exception:
        logging.exception("Neuspelo učitavanje %s", path)
    return {}


def _first_str(*candidates: object) -> str:
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            return s
    return ""


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


def _cleanup_obsolete_taurislock_local_secrets() -> None:
    """Briše zastarelo: plain ključ u JSON podešavanjima i stari DPAPI fajl u AppData."""
    data = load_gui_settings_dict()
    if LEGACY_PLAIN_TAURISLOCK_JSON_KEY in data:
        data = dict(data)
        data.pop(LEGACY_PLAIN_TAURISLOCK_JSON_KEY, None)
        try:
            save_gui_settings_dict(data)
            logging.info("Uklonjen zastareli plain ključ iz GUI podešavanja.")
        except Exception:
            logging.exception("Neuspešno snimanje podešavanja posle uklanjanja zastarelog ključa.")
    dpapi_path = app_data_dir() / "taurislock_secret.dpapi"
    try:
        dpapi_path.unlink(missing_ok=True)
    except Exception:
        logging.exception("Neuspešno brisanje zastarelog fajla %s", dpapi_path)


def get_configured_taurislock_password() -> str | None:
    """Samo promenljiva okruženja TAURIS_LOCK_PASSWORD (lokalno čuvanje u programu više ne postoji)."""
    env_pw = (os.getenv(TAURIS_LOCK_PASSWORD_ENV) or "").strip()
    return env_pw or None


BAUD_CHOICES = ("9600", "19200", "38400", "57600", "115200")


def load_delivery_config() -> DeliveryConfig:
    mode = os.getenv("TAURIS_DELIVERY_MODE", DEFAULT_TAURIS_DELIVERY_MODE).strip().lower()
    if mode in ("server", "both"):
        logging.warning(
            "TAURIS_DELIVERY_MODE=%s više nije podržan (HTTP uklonjen); koristim 'email'.",
            mode,
        )
        mode = "email"
    if mode not in SUPPORTED_DELIVERY_MODES:
        logging.warning("Nepoznat TAURIS_DELIVERY_MODE=%s, koristim 'none'.", mode)
        mode = "none"

    timeout_text = os.getenv("TAURIS_DELIVERY_TIMEOUT", "15").strip()
    try:
        timeout_sec = float(timeout_text)
    except ValueError:
        timeout_sec = 15.0

    club = load_club_email_file()

    smtp_host = _first_str(
        os.getenv("TAURIS_SMTP_HOST"),
        club.get("smtp_host"),
        CLUB_SMTP_HOST,
    )
    smtp_user = _first_str(
        os.getenv("TAURIS_SMTP_USER"),
        club.get("smtp_user"),
        CLUB_SMTP_USER,
    )
    smtp_password = _first_str(
        os.getenv("TAURIS_SMTP_PASSWORD"),
        club.get("smtp_password"),
        CLUB_SMTP_PASSWORD,
    )
    mail_from = _first_str(
        os.getenv("TAURIS_MAIL_FROM"),
        club.get("mail_from"),
        CLUB_MAIL_FROM,
    )
    mail_to = _first_str(
        os.getenv("TAURIS_MAIL_TO"),
        club.get("mail_to"),
        CLUB_MAIL_TO,
        DEFAULT_TAURIS_MAIL_TO,
    )

    smtp_port_env = os.getenv("TAURIS_SMTP_PORT", "").strip()
    if smtp_port_env:
        try:
            smtp_port = int(smtp_port_env)
        except ValueError:
            smtp_port = int(CLUB_SMTP_PORT) if CLUB_SMTP_PORT else 587
    elif club.get("smtp_port") is not None:
        try:
            smtp_port = int(club["smtp_port"])
        except (TypeError, ValueError):
            smtp_port = int(CLUB_SMTP_PORT) if CLUB_SMTP_PORT else 587
    else:
        smtp_port = int(CLUB_SMTP_PORT) if CLUB_SMTP_PORT else 587

    return DeliveryConfig(
        mode=mode,
        signing_key=os.getenv("TAURIS_SIGNING_KEY", "CHANGE_ME_SECRET"),
        timeout_sec=timeout_sec,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        mail_from=mail_from,
        mail_to=mail_to,
    )


def _clean_takmicar_display(name: str) -> str:
    txt = " ".join(str(name or "").replace("\xa0", " ").split()).strip(" ,")
    if "," in txt:
        txt = txt.split(",", 1)[0].strip()
    return txt.strip(" ,")


# Linija kontrol liste: četiri grupe cifara, ime (pohlepno do kolone lag + opseg), opseg xxxx-xxxx.
_KONTROL_LIST_LINE_RE = re.compile(
    r"(?m)^\s*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\s+(.+)\s+(\d{4})\s+(\d{4})-(\d{4})\s*$",
)


def extract_takmicar_from_tauris_kontrol_list(text: str) -> str | None:
    """
    TauRIS „kontrol list“: linija tipa
      001.001.049.066  Dragan Todorovic      0040 0001-0040
    (četiri grupe cifara tačkom, ime, četvorocifren broj, opseg xxxx-xxxx).
    """
    if not (text or "").strip():
        return None
    normalized = text.replace("\r\n", "\n")
    m = _KONTROL_LIST_LINE_RE.search(normalized)
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
    """
    True ako postoji red „NNNN SRB“ (npr. 0040 SRB). Stvarni ispis sata uvlači SVAKI red sa
    nekoliko razmaka (vidi logs/tauris_capture), zato ``^\\s*`` — bez toga provera nikad nije
    prolazila i svako čitanje je završavalo lažnim upozorenjem.
    """
    lag = f"{last_lag:04d}"
    return bool(re.search(rf"(?m)^\s*{re.escape(lag)}\s+SRB\b", text))


def assess_tauris_transfer_quality(raw: bytes) -> tuple[bool, str]:
    """
    Da li primljeni bajtovi liče na ispravan TAURIS tekst ili na oštećen prenos (npr. klizanje
    takta UART-a: crtice postaju 0xD6, razmaci 0x02/0x06 — primer 23.08.2026).
    Vraća (ok, opis). Ne pada zbog imena sa č/ć/š — dozvoljeno je do 15 % bajtova ≥ 0x80.
    """
    if not raw:
        return False, "nema podataka"
    n = len(raw)
    ctrl = sum(1 for b in raw if b < 32 and b not in (9, 10, 13))
    high = sum(1 for b in raw if b >= 128)
    ctrl_ratio = ctrl / n
    high_ratio = high / n
    desc = f"kontrolnih bajtova {ctrl_ratio:.1%}, bajtova ≥0x80 {high_ratio:.1%} (ukupno {n} B)"
    if ctrl_ratio > TAURIS_MAX_CONTROL_BYTE_RATIO or high_ratio > TAURIS_MAX_HIGH_BYTE_RATIO:
        return False, desc
    return True, desc


def _parse_version_parts(version_text: str) -> tuple[int, ...]:
    cleaned = (version_text or "").strip().lower()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        m = re.match(r"(\d+)", chunk)
        parts.append(int(m.group(1)) if m else 0)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts or [0])


def compare_version_strings(current: str, latest: str) -> int:
    """-1 ako je current stariji od latest, 0 jednako, 1 ako je current noviji."""
    cur = _parse_version_parts(current)
    lat = _parse_version_parts(latest)
    if cur < lat:
        return -1
    if cur > lat:
        return 1
    return 0


def pick_github_release_installer_asset(
    assets: list[dict[str, Any]],
    *,
    lite: bool,
) -> dict[str, Any] | None:
    """Izaberi Setup.exe sa release-a (Lite ili puni installer)."""
    if not assets:
        return None
    preferred = GITHUB_RELEASE_ASSET_LITE if lite else GITHUB_RELEASE_ASSET_FULL
    fallback = GITHUB_RELEASE_ASSET_FULL if lite else GITHUB_RELEASE_ASSET_LITE
    by_name = {
        str(a.get("name") or "").strip(): a
        for a in assets
        if isinstance(a, dict) and str(a.get("name") or "").strip()
    }
    for name in (preferred, fallback):
        hit = by_name.get(name)
        if hit and str(hit.get("browser_download_url") or "").strip():
            return hit
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip().lower()
        if not str(asset.get("browser_download_url") or "").strip():
            continue
        if name.endswith("_setup.exe"):
            return asset
        # Česta greška pri uploadu: TaurisPdfApp_Lite_Setup.1.7.exe
        if lite and name.startswith("taurispdfapp_lite_setup") and name.endswith(".exe"):
            return asset
        if not lite and name.startswith("taurispdfapp_setup") and name.endswith(".exe"):
            return asset
    return None


def _message_looks_like_ssl_error(msg: str) -> bool:
    low = (msg or "").lower()
    return (
        "certificate_verify_failed" in low
        or "certificate verify failed" in low
        or "unable to get local issuer certificate" in low
        or "sslcertverificationerror" in low
    )


def _is_ssl_certificate_error(exc: BaseException | str) -> bool:
    if isinstance(exc, str):
        return _message_looks_like_ssl_error(exc)
    if not isinstance(exc, BaseException):
        return _message_looks_like_ssl_error(str(exc))
    for e in _walk_exception_graph(exc):
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        if _message_looks_like_ssl_error(str(e)):
            return True
    return False


def format_github_update_error(exc: BaseException | str) -> str:
    """Poruka za korisnika kada provera nadogradnje ne uspe."""
    if _is_ssl_certificate_error(exc):
        return (
            "Računar ne može da proveri HTTPS sertifikat za GitHub "
            "(čest problem kod .exe paketa bez CA fajla ili sa antivirus SSL pregledom).\n\n"
            "• Preuzmite nadogradnju ručno u pregledaču (Chrome/Edge):\n"
            f"  {GITHUB_RELEASES_URL}\n"
            "• Instalirajte fajl TaurisPdfApp_Lite_Setup.exe sa poslednjeg release-a\n"
            "• Od verzije 2.0.1 program traži ugrađeni cacert.pem i Windows store\n\n"
            "Ako koristite antivirus/proxy sa SSL pregledom, dodajte program u izuzetke."
        )
    if isinstance(exc, BaseException) and _is_network_unreachable_error(exc):
        return (
            "Nema internet veze ili računar ne može da pronađe GitHub (DNS).\n\n"
            "• Proverite Wi‑Fi ili mrežni kabl\n"
            "• Isključite VPN/proxy ako blokira GitHub\n"
            "• Pokušajte ponovo: meni Baza → Provera Nadogradnje\n\n"
            f"Ručno preuzimanje (pregledač):\n{GITHUB_RELEASES_URL}"
        )
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return (
                "Na GitHub-u još nema objavljenog release-a za ovaj program.\n\n"
                f"Kada bude objavljen, biće ovde:\n{GITHUB_RELEASES_URL}"
            )
        if exc.code == 403:
            return (
                "GitHub je odbio zahtev (previše provera — limit API-ja).\n"
                "Sačekajte nekoliko minuta ili otvorite release-ove ručno:\n"
                f"{GITHUB_RELEASES_URL}"
            )
        return f"GitHub je vratio grešku HTTP {exc.code}.\n\n{exc}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (BaseException, str)) and reason is not exc:
            return format_github_update_error(reason)
        return (
            "Nije moguće pristupiti GitHub-u.\n\n"
            f"Proverite internet i firewall, pa ponovo.\n\n"
            f"Ručno: {GITHUB_RELEASES_URL}"
        )
    return f"Provera nadogradnje nije uspela.\n\n{exc}"


def fetch_github_latest_release(
    api_url: str = GITHUB_LATEST_RELEASE_API,
    *,
    app_version: str = APP_VERSION,
    timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """Poslednji GitHub release (tag_name, assets, html_url, body)."""
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"TaurisPdfApp-Updater/{app_version}",
        },
    )
    try:
        with _urlopen_https(req, timeout=timeout_sec) as resp:
            raw = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(format_github_update_error(exc)) from exc
    except urllib.error.HTTPError as exc:
        raise RuntimeError(format_github_update_error(exc)) from exc
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Neočekivan odgovor sa GitHub-a.")
    tag_name = str(payload.get("tag_name") or "").strip()
    latest_version = tag_name or str(payload.get("name") or "").strip()
    raw_assets = payload.get("assets")
    assets: list[dict[str, Any]] = []
    if isinstance(raw_assets, list):
        assets = [a for a in raw_assets if isinstance(a, dict)]
    html_url = str(payload.get("html_url") or GITHUB_RELEASES_URL).strip() or GITHUB_RELEASES_URL
    return {
        "latest_version": latest_version,
        "html_url": html_url,
        "body": str(payload.get("body") or "").strip(),
        "assets": assets,
    }


def download_release_asset(
    download_url: str,
    target: Path,
    *,
    app_version: str = APP_VERSION,
    timeout_sec: float = 300.0,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": f"TaurisPdfApp-Updater/{app_version}"},
    )
    with _urlopen_https(req, timeout=timeout_sec) as resp:
        data = resp.read()
    target.write_bytes(data)


def automatic_update_installer_target(version_tag: str, asset_name: str) -> Path:
    safe_ver = re.sub(r"[^\w.\-]+", "_", (version_tag or "new").strip()) or "new"
    folder = Path(tempfile.gettempdir()) / "TaurisPdfApp_update"
    folder.mkdir(parents=True, exist_ok=True)
    name = Path(asset_name or "TaurisPdfApp_Setup.exe").name
    return folder / f"{safe_ver}_{name}"


def launch_automatic_windows_upgrade(installer_path: Path) -> None:
    """
  Pokrene pomoćni .cmd: sačeka zatvaranje aplikacije, ugasi TaurisPdfApp.exe,
  tiha instalacija (/VERYSILENT) zamenjuje staru verziju, briše preuzeti installer.
  """
    if sys.platform != "win32":
        raise OSError("Automatska nadogradnja je podržana samo na Windows-u.")
    inst = installer_path.resolve()
    if not inst.is_file():
        raise FileNotFoundError(str(inst))
    helper = Path(tempfile.gettempdir()) / f"TaurisPdfApp_upgrade_{inst.stem}.cmd"
    inst_q = str(inst).replace('"', '""')
    helper_q = str(helper).replace('"', '""')
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "timeout /t 2 /nobreak >nul\r\n"
        "taskkill /F /IM TaurisPdfApp.exe /T >nul 2>&1\r\n"
        "ping 127.0.0.1 -n 2 >nul\r\n"
        f'"{inst_q}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART '
        f"/CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS\r\n"
        f'del /F /Q "{inst_q}" >nul 2>&1\r\n'
        f'del /F /Q "{helper_q}" >nul 2>&1\r\n'
    )
    helper.write_text(script, encoding="utf-8")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
    subprocess.Popen(
        ["cmd.exe", "/c", str(helper)],
        close_fds=True,
        creationflags=flags,
        shell=False,
    )


def launch_windows_installer(installer_path: Path, *, silent: bool = False) -> None:
    if sys.platform != "win32":
        raise OSError("Instalacioni program se pokreće samo na Windows-u.")
    path = installer_path.resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if silent:
        launch_automatic_windows_upgrade(path)
        return
    os.startfile(str(path))  # type: ignore[attr-defined]


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
    *,
    on_com_bytes: Callable[[int], None] | None = None,
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
                if on_com_bytes is not None:
                    on_com_bytes(len(acc))
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


def list_live_com_port_names() -> list[str]:
    if list_ports is None:
        return []
    return [str(p.device).strip() for p in list_ports.comports() if str(p.device or "").strip()]


def com_port_description_blob(port: str) -> str:
    if list_ports is None:
        return ""
    want = (port or "").strip()
    for info in list_ports.comports():
        if (info.device or "").strip() != want:
            continue
        return " ".join(
            str(x or "")
            for x in (info.description, info.manufacturer, info.product, info.interface)
        ).lower()
    return ""


def com_port_usability_issue(port: str) -> str | None:
    """
    Windows ponekad prikazuje COM koji ne može da se otvori (npr. PL2303 „phased out“).
    """
    blob = com_port_description_blob(port)
    if not blob:
        return None
    if "phased out" in blob or ("please use" in blob and "pl2303" in blob):
        return (
            f"Port {port} je zastareli/neispravan USB-Serial adapter (PL2303).\n"
            "Windows ga prikazuje ali ne radi — zamenite kabl/adapter ili izaberite drugi port (F5)."
        )
    if "marked as inactive" in blob or "already in use" in blob:
        return f"Port {port} nije slobodan ili je neaktivan u Windows-u."
    return None


def verify_com_port_available(port: str) -> None:
    """Provera pre otvaranja — izbegava „COM7 ne postoji“ ako je port uklonjen ili pogrešan."""
    name = (port or "").strip()
    if not name:
        raise RuntimeError("Nije izabran COM port. Pritisnite F5 (Osveži) i izaberite port iz liste.")
    alive = set(list_live_com_port_names())
    if name not in alive:
        hint = (
            f"Port {name} trenutno ne postoji u Windows-u "
            f"(priključen sat / USB, zatim F5).\n\n"
        )
        if alive:
            hint += f"Dostupni portovi: {', '.join(sorted(alive))}"
        else:
            hint += "Nema nijednog COM porta — za BRICON koristite «Uvezi BRICON fajl»."
        raise RuntimeError(hint)
    issue = com_port_usability_issue(name)
    if issue:
        raise RuntimeError(issue)


def format_com_open_error(port: str, exc: BaseException) -> str:
    port = (port or "").strip() or "?"
    msg = str(exc).lower()
    if "does not exist" in msg or "could not open port" in msg or getattr(exc, "errno", None) == 22:
        return (
            f"Port {port} nije dostupan.\n\n"
            "Proverite da li je sat priključen, pritisnite F5 (Osveži) "
            "i izaberite port koji Windows zaista prikazuje u listi."
        )
    return f"Greška pri otvaranju porta {port}: {exc}"


class TaurisCancelled(RuntimeError):
    """Korisnik je otkazao TAURIS preuzimanje dok je bilo u toku."""


def _is_transient_com_open_error(exc: BaseException) -> bool:
    """
    Windows ponekad kratko drži port „zauzetim" odmah posle priključenja USB-a ili posle
    zatvaranja drugog programa — sledeći pokušaj otvaranja često prođe. Razlikuje se od
    „port ne postoji"/„pogrešan port", gde ponavljanje ne pomaže i samo odlaže jasnu poruku.
    """
    if isinstance(exc, PermissionError):
        return True
    msg = str(exc).lower()
    return "access is denied" in msg or "access denied" in msg or "permissionerror" in msg


def read_race_from_device(
    config: TaurisConnectionConfig,
    *,
    on_com_bytes: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """
    Čitanje kao u programu Analiza leta (qt_app_ui_common._do_read):
    otvori COM, u petlji čitaj bajtove (ne šalje se GET_RACE); uređaj šalje podatke kada korisnik pokrene slanje.

    TAURIS nema aktivni handshake — uređaj počinje da šalje tek kad korisnik pokrene slanje
    na satu, zato faza koja bi kod BRICON-a bila „handshake" ovde znači „čekanje da uređaj
    počne slanje". ``on_phase``/``should_cancel`` su opcioni (isti oblik kao BRICON put) da bi
    GUI mogao da prikaže napredak i bezbedno otkaže preuzimanje dok je u toku.
    """
    if serial is None:
        raise RuntimeError("Nedostaje pyserial paket. Instalirajte: pip install pyserial")

    def _check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise TaurisCancelled("Otkazano od strane korisnika.")

    def _phase(msg: str) -> None:
        if on_phase is not None:
            on_phase(msg)

    t_start = time.monotonic()
    _phase(f"traženje uređaja na portu {config.port}…")
    verify_com_port_available(config.port)
    _check_cancel()

    _phase(f"povezivanje na {config.port} @ {config.baudrate} baud…")
    ser = None
    last_open_exc: BaseException | None = None
    attempts = max(1, int(TAURIS_OPEN_RETRY_ATTEMPTS))
    for attempt in range(1, attempts + 1):
        try:
            ser = serial.Serial(
                port=config.port,
                baudrate=config.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=config.read_timeout,
            )
            last_open_exc = None
            break
        except Exception as exc:
            last_open_exc = exc
            if attempt >= attempts or not _is_transient_com_open_error(exc):
                break
            logging.warning(
                "TAURIS diag | port=%s baud=%s phase=open_retry pokusaj=%s tip_greske=%s: %s",
                config.port, config.baudrate, attempt, type(exc).__name__, exc,
            )
            _phase(f"port {config.port} privremeno nedostupan, ponovni pokušaj…")
            _check_cancel()
            time.sleep(TAURIS_OPEN_RETRY_DELAY_SEC)
    if last_open_exc is not None:
        logging.exception("Greska pri otvaranju COM porta.")
        logging.info(
            "TAURIS diag | port=%s baud=%s phase=open_failed trajanje=%.2fs tip_greske=%s",
            config.port, config.baudrate, time.monotonic() - t_start, type(last_open_exc).__name__,
        )
        raise RuntimeError(format_com_open_error(config.port, last_open_exc)) from last_open_exc

    prepare_serial_port_for_passive_listen(ser, device_label="TAURIS")
    logging.info(
        "TAURIS diag | port=%s baud=%s dtr=True rts=False phase=open_ok",
        config.port, config.baudrate,
    )

    def _notify(base: int) -> Callable[[int], None] | None:
        if on_com_bytes is None:
            return None
        return lambda n: on_com_bytes(base + n)

    read_errors: list[BaseException] = []

    def _on_read_error(exc: BaseException) -> None:
        read_errors.append(exc)
        _phase(f"veza prekinuta tokom čitanja ({type(exc).__name__}) — čuvam primljene podatke…")

    raw = b""
    try:
        _check_cancel()
        _phase("čekanje da TAURIS počne slanje podataka…")
        raw = read_serial_bytes_until_idle(
            ser, config, on_com_bytes=_notify(0), on_error=_on_read_error
        )
        _phase(f"preuzimanje u toku — primljeno {len(raw)} B…")
        # „Dopuna“: sat pravi pauze usred liste duže od idle praga. Ako kontrol-lista u zaglavlju
        # kaže do kog rednog broja ide, čekamo još malo dok se taj red ne pojavi. Rezult.list ne
        # mora da sadrži sve ukorpane golubove, pa izostanak reda NIJE greška — samo kraj čitanja.
        for _ in range(4):
            _check_cancel()
            if read_errors:
                break
            probe = decode_com_payload(raw, quiet=True)
            expected_last = parse_tauris_expected_last_lag(probe)
            if expected_last is None or tauris_text_has_lag_row(probe, expected_last):
                break
            n_before = len(raw)
            _phase(f"primljeno {len(raw)} B — proveravam da li sat šalje još…")
            more = read_serial_bytes_until_idle(
                ser,
                config,
                max_iterations=config.tail_extra_max_iterations,
                idle_empty_reads=config.tail_idle_empty_reads,
                on_com_bytes=_notify(n_before),
                on_error=_on_read_error,
            )
            raw = raw + more
            if len(raw) == n_before:
                logging.info(
                    "TAURIS: kraj prenosa posle %s B (zaglavlje navodi do Lag=%04d, red nije u listi — "
                    "uobičajeno za Rezult.list).",
                    len(raw),
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

    duration = time.monotonic() - t_start

    if not raw.strip():
        logging.info(
            "TAURIS diag | port=%s baud=%s phase=empty trajanje=%.2fs primljeno=%sB parsiranje=n/a",
            config.port, config.baudrate, duration, len(raw),
        )
        if read_errors:
            _phase("veza prekinuta pre nego što je bilo šta primljeno.")
            raise RuntimeError(
                f"Veza sa TAURIS uređajem je prekinuta tokom čitanja "
                f"({type(read_errors[0]).__name__}: {read_errors[0]}).\n\n"
                "Proverite USB kabl/priključak i pokušajte ponovo."
            ) from read_errors[0]
        _phase("nema primljenih podataka.")
        raise RuntimeError(
            "Nema podataka sa TAURIS uređaja. Proverite: pravi COM port, baud rate "
            "(probajte 9600 ili kao u Analiza leta), da li je uređaj poslao podatke posle klika na preuzimanje."
        )

    if read_errors:
        logging.warning(
            "TAURIS diag | port=%s baud=%s phase=read_error trajanje=%.2fs primljeno=%sB tip_greske=%s "
            "— parcijalni podaci se ipak obrađuju.",
            config.port, config.baudrate, duration, len(raw), type(read_errors[0]).__name__,
        )

    quality_ok, quality_desc = assess_tauris_transfer_quality(raw)
    if not quality_ok:
        diag_path = _save_tauris_incomplete_diagnostic(
            config.port, raw, reason=f"oštećen prenos: {quality_desc}"
        )
        logging.warning(
            "TAURIS diag | port=%s baud=%s phase=corrupted trajanje=%.2fs primljeno=%sB %s diag=%s",
            config.port, config.baudrate, duration, len(raw), quality_desc, diag_path,
        )
        _phase("greška — primljeni podaci su oštećeni.")
        raise RuntimeError(
            "Prenos sa TAURIS sata je OŠTEĆEN — tekst je nečitljiv "
            f"({quality_desc}).\n\n"
            "Ovo je greška na vezi (brzina/takt UART-a, kabl, adapter, baterija sata), ne u programu. "
            "Rezultat NIJE sačuvan ni poslat.\n\n"
            "Uradite: proverite baud u programu i na satu (obično 9600), promenite USB-serijski "
            "adapter ili kabl, pa ponovo pokrenite slanje na satu.\n\n"
            + (f"Sirovi bajtovi sačuvani za dijagnostiku: {diag_path}" if diag_path else "")
        )

    _phase("parsiranje primljenih podataka…")
    text = decode_com_payload(raw)
    try:
        data = parse_tauris_payload_to_race_dict(text)
    except Exception as exc:
        logging.exception("TAURIS: parsiranje primljenih podataka nije uspelo.")
        diag_path = _save_tauris_incomplete_diagnostic(
            config.port, raw, reason=f"parsiranje nije uspelo: {type(exc).__name__}: {exc}"
        )
        logging.info(
            "TAURIS diag | port=%s baud=%s phase=parse_failed trajanje=%.2fs primljeno=%sB diag=%s",
            config.port, config.baudrate, duration, len(raw), diag_path,
        )
        raise

    logging.info(
        "TAURIS diag | port=%s baud=%s phase=done trajanje=%.2fs primljeno=%sB parsiranje=ok",
        config.port, config.baudrate, duration, len(raw),
    )
    _phase("čuvanje rezultata…")
    return data


def _save_tauris_incomplete_diagnostic(port: str, raw: bytes, *, reason: str) -> Path | None:
    """Snimi sirove bajtove za terensku dijagnostiku kad TAURIS prenos ne uspe do kraja."""
    if not raw:
        return None
    try:
        from tauris_desktop.tauris_capture import save_tauris_partial_payload_diagnostic

        return save_tauris_partial_payload_diagnostic(port, bytes(raw), reason=reason)
    except Exception:
        logging.exception("Snimanje TAURIS dijagnostičkog fajla nije uspelo.")
        return None


def read_race_from_clock(
    config: TaurisConnectionConfig,
    device_type: str,
    *,
    on_com_bytes: Callable[[int], None] | None = None,
    on_phase: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    slow_uart: bool = False,
) -> dict[str, Any]:
    """Čitanje sa TAURIS ili BRICON uređaja preko COM porta.

    ``on_phase``/``should_cancel`` rade na oba puta (status faze u GUI-ju + bezbedno
    otkazivanje) — TAURIS put baca ``TaurisCancelled``, BRICON put ``BriconCancelled``.
    """
    kind = (device_type or CLOCK_DEVICE_TAURIS).strip().lower()
    if kind == CLOCK_DEVICE_BRICON:
        from tauris_desktop.bricon import read_race_from_bricon_usb_readout

        return read_race_from_bricon_usb_readout(
            config,
            on_com_bytes=on_com_bytes,
            on_phase=on_phase,
            should_cancel=should_cancel,
        )
    return read_race_from_device(
        config, on_com_bytes=on_com_bytes, on_phase=on_phase, should_cancel=should_cancel
    )


# Isto kao podrazumevano u GUI — ako je još uvek ovo, ime uzimamo iz preuzetih podataka
UNKNOWN_PILOT_LABEL = "Nepoznat takmičar"

GRUPACIJA_LABELS = tuple(f"Grupacija {i}" for i in range(1, 19))
MAX_BROJ_TRKE = 20
# Broj trke uvek tri cifre (001 … 020) — i u padajućem meniju i u izvozu
BROJ_TRKE_LABELS = tuple(f"{i:03d}" for i in range(1, MAX_BROJ_TRKE + 1))


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
    n = max(1, min(MAX_BROJ_TRKE, n))
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
    _ = parent
    desktop_key = (Path.home() / "Desktop" / ACCESS_KEY_FILENAME).resolve()
    if desktop_key.is_file() and _validate_access_key_file(desktop_key):
        return desktop_key
    messagebox.showerror(
        "Greška",
        "Traženu radnju trenutno nije moguće izvršiti.",
    )
    return None


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


TAURISLOCK_V1_MAGIC = b"TAURISLOCKv1"
TAURISLOCK_V2_MAGIC = b"TAURISLOCKv2"
TAURISLOCK_V2_SALT_BYTES = 16
TAURISLOCK_V2_NONCE_BYTES = 12
TAURISLOCK_V2_KEY_BYTES = 32
TAURISLOCK_V2_KDF_ITERATIONS = 390_000


def _xor_stream_encrypt(data: bytes, key: bytes) -> bytes:
    """Legacy v1 stream used only for opening old .taurislock packages."""
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


def _build_taurislock_plain_payload(json_path: Path, pdf_path: Path, txt_path: Path) -> bytes:
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
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _derive_taurislock_v2_key(password: str, salt: bytes) -> bytes:
    if hashes is None or PBKDF2HMAC is None:
        raise RuntimeError(
            "Za jače zaključavanje potreban je paket 'cryptography'. "
            "Instalirajte ga komandom: pip install cryptography"
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=TAURISLOCK_V2_KEY_BYTES,
        salt=salt,
        iterations=TAURISLOCK_V2_KDF_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def create_locked_results_bundle(
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    password: str,
) -> Path:
    """
    Pravi šifrovani paket (.taurislock) koji zahteva lozinku za čitanje.
    Format v2: TAURISLOCKv2 + salt + nonce + AES-GCM(ciphertext+tag).
    Stari v1 format ostaje podržan samo za otključavanje postojećih paketa.
    """
    if AESGCM is None:
        raise RuntimeError(
            "Za jače zaključavanje potreban je paket 'cryptography'. "
            "Instalirajte ga komandom: pip install cryptography"
        )
    plain = _build_taurislock_plain_payload(json_path, pdf_path, txt_path)
    salt = os.urandom(TAURISLOCK_V2_SALT_BYTES)
    nonce = os.urandom(TAURISLOCK_V2_NONCE_BYTES)
    key = _derive_taurislock_v2_key(password, salt)
    header = TAURISLOCK_V2_MAGIC + salt + nonce
    cipher = AESGCM(key).encrypt(nonce, plain, header)
    out_path = json_path.with_suffix(".taurislock")
    clear_export_file_readonly(out_path)
    out_path.write_bytes(header + cipher)
    set_export_file_readonly(out_path)
    return out_path


def _decrypt_taurislock_v1(raw: bytes, password: str) -> bytes:
    magic = TAURISLOCK_V1_MAGIC
    if len(raw) < len(magic) + 16 + 32:
        raise ValueError("Zaključan paket je neispravan.")
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
    return _xor_stream_encrypt(cipher, enc_key)


def _decrypt_taurislock_v2(raw: bytes, password: str) -> bytes:
    if AESGCM is None:
        raise RuntimeError(
            "Za otključavanje ovog paketa potreban je paket 'cryptography'. "
            "Instalirajte ga komandom: pip install cryptography"
        )
    magic = TAURISLOCK_V2_MAGIC
    min_len = len(magic) + TAURISLOCK_V2_SALT_BYTES + TAURISLOCK_V2_NONCE_BYTES + 16
    if len(raw) < min_len:
        raise ValueError("Zaključan paket je neispravan.")
    i = len(magic)
    salt = raw[i : i + TAURISLOCK_V2_SALT_BYTES]
    i += TAURISLOCK_V2_SALT_BYTES
    nonce = raw[i : i + TAURISLOCK_V2_NONCE_BYTES]
    i += TAURISLOCK_V2_NONCE_BYTES
    cipher = raw[i:]
    header = raw[:i]
    key = _derive_taurislock_v2_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, cipher, header)
    except InvalidTag as exc:
        raise ValueError("Pogrešna šifra ili je paket izmenjen.") from exc


def _write_unlocked_payload_files(plain: bytes, target_dir: Path) -> list[Path]:
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


def unlock_results_bundle(bundle_path: Path, password: str, target_dir: Path) -> list[Path]:
    raw = bundle_path.read_bytes()
    if raw.startswith(TAURISLOCK_V2_MAGIC):
        plain = _decrypt_taurislock_v2(raw, password)
    elif raw.startswith(TAURISLOCK_V1_MAGIC):
        plain = _decrypt_taurislock_v1(raw, password)
    else:
        raise ValueError("Nepoznat format zaključanog paketa.")
    return _write_unlocked_payload_files(plain, target_dir)


def extract_all_pdfs_from_taurislocks_in_dir(
    source_dir: Path,
    password: str,
    *,
    dest_dir: Path | None = None,
) -> tuple[int, list[str]]:
    """
    Iz svih *.taurislock direktno u ``source_dir`` kopira PDF-ove u ``dest_dir``.
    Podrazumevano: ``source_dir.resolve().parent / OPEN_PDF_FOLDER_NAME`` (folder „pored“ izabranog).
    Vraća (ukupno_iskopiranih_pdf, lista_tekstova_grešaka).
    """
    dest = dest_dir if dest_dir is not None else source_dir.resolve().parent / OPEN_PDF_FOLDER_NAME
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    copied = 0
    for bundle in sorted(source_dir.glob("*.taurislock")):
        if not bundle.is_file():
            continue
        tmp = Path(tempfile.mkdtemp(prefix="tauris_baza_"))
        try:
            unlock_results_bundle(bundle, password, tmp)
        except Exception as exc:
            errors.append(f"{bundle.name}: {exc}")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        pdfs = sorted(tmp.glob("*.pdf"))
        if not pdfs:
            errors.append(f"{bundle.name}: u paketu nema PDF fajla.")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        for i, pdf in enumerate(pdfs):
            stem = bundle.stem if len(pdfs) == 1 else f"{bundle.stem}_{i + 1}"
            target = dest / f"{stem}.pdf"
            suf = 2
            while target.exists():
                target = dest / f"{stem}_{suf}.pdf"
                suf += 1
            try:
                shutil.copy2(pdf, target)
                copied += 1
            except Exception as exc:
                errors.append(f"{bundle.name} → {pdf.name}: {exc}")
        shutil.rmtree(tmp, ignore_errors=True)
    return copied, errors


def extract_all_txt_from_taurislocks_in_dir(
    source_dir: Path,
    password: str,
    *,
    dest_dir: Path | None = None,
) -> tuple[int, list[str]]:
    """
    Iz svih *.taurislock direktno u ``source_dir`` izvlači .txt segmente po takmičarima u ``dest_dir``.
    Podrazumevano: ``source_dir.resolve().parent / OPEN_TXT_FOLDER_NAME`` (folder „pored“ izabranog).
    Vraća (ukupno_snimljenih_txt, lista_tekstova_grešaka).
    """
    dest = dest_dir if dest_dir is not None else source_dir.resolve().parent / OPEN_TXT_FOLDER_NAME
    dest.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    written = 0
    for bundle in sorted(source_dir.glob("*.taurislock")):
        if not bundle.is_file():
            continue
        tmp = Path(tempfile.mkdtemp(prefix="tauris_baza_txt_"))
        try:
            unlock_results_bundle(bundle, password, tmp)
        except Exception as exc:
            errors.append(f"{bundle.name}: {exc}")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        json_paths = sorted(tmp.glob("*.json"))
        if not json_paths:
            errors.append(f"{bundle.name}: u paketu nema JSON fajla.")
            shutil.rmtree(tmp, ignore_errors=True)
            continue
        try:
            race_data = load_race_data_from_bundle_json(json_paths[0])
        except Exception as exc:
            errors.append(f"{bundle.name}: {exc}")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        raw = race_data.get("raw_com_text")
        if not isinstance(raw, str) or not raw.strip():
            txts = sorted(tmp.glob("*.txt"))
            if txts:
                try:
                    raw = txts[0].read_text(encoding="utf-8")
                except Exception:
                    raw = ""
            else:
                raw = ""
        if not (raw or "").strip():
            errors.append(f"{bundle.name}: nema sirovog COM teksta za .txt.")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        segments = discover_takmicar_segments_from_raw_com_text(
            raw,
            race_data=race_data,
            bundle_json_path=json_paths[0],
        )
        if not segments:
            errors.append(f"{bundle.name}: nije moguće prepoznati takmičare.")
            shutil.rmtree(tmp, ignore_errors=True)
            continue

        for i, (_name, body) in enumerate(segments):
            stem = bundle.stem if len(segments) == 1 else f"{bundle.stem}_{i + 1}"
            dest_txt = dest / f"{stem}.txt"
            suf = 2
            while dest_txt.exists():
                dest_txt = dest / f"{stem}_{suf}.txt"
                suf += 1
            normalized = body.replace("\r\n", "\n").replace("\r", "\n")
            try:
                clear_export_file_readonly(dest_txt)
                dest_txt.write_text(normalized, encoding="utf-8", newline="\n")
                set_export_file_readonly(dest_txt)
                written += 1
            except Exception as exc:
                errors.append(f"{bundle.name} → {dest_txt.name}: {exc}")
        shutil.rmtree(tmp, ignore_errors=True)
    return written, errors


def extract_takmicar_from_race_data(race_data: dict[str, Any]) -> str | None:
    """Ime i prezime takmičara iz sveže pročitanih podataka (JSON polja ili raw COM tekst)."""
    if not race_data:
        return None
    for k in ("takmicar", "takmičar", "Takmicar", "ime_takmicara"):
        v = race_data.get(k)
        if isinstance(v, str) and v.strip():
            name = _clean_takmicar_display(v)
            if name:
                return name
    raw = race_data.get("raw_com_text")
    if isinstance(raw, str) and raw.strip():
        return extract_takmicar_name_from_raw_text(raw)
    return None


def resolve_pilot_name_for_output(
    race_data: dict[str, Any],
    pilot_from_ui: str,
) -> str:
    """
    Pri preuzimanju: uvek prvo ime iz pročitanih podataka (COM), pa tek onda ručni unos u polju.
    Tako se polje Takmičar i nazivi fajlova poklapaju sa onim što je stiglo sa sata.
    """
    from_device = extract_takmicar_from_race_data(race_data)
    if from_device:
        return from_device
    ui = pilot_from_ui.strip()
    if ui and ui != UNKNOWN_PILOT_LABEL:
        return ui
    return UNKNOWN_PILOT_LABEL


def _hint_takmicar_from_race_data(race_data: dict[str, Any] | None) -> str | None:
    """Ime takmičara iz JSON race_data (ključevi takmicar/…) kada COM tekst ne sadrži prepoznatljiv obrazac."""
    if not race_data:
        return None
    n = resolve_pilot_name_for_output(race_data, "")
    if n and n != UNKNOWN_PILOT_LABEL:
        return n
    return None


def _hint_takmicar_from_export_json_stem(json_path: Path, race_data: dict[str, Any]) -> str | None:
    """
    Ime iz imena izvoznog JSON-a (isto pravilo kao build_output_paths: broj_mesto_staza_pilot_datum).
    """
    stem = json_path.stem
    bt = sanitize_filename(str(race_data.get("broj_trke") or ""))
    mp = sanitize_filename(str(race_data.get("mesto_pustanja") or ""))
    iz = sanitize_filename(
        str(race_data.get("ime_staze") or race_data.get("staza") or race_data.get("grupacija") or "")
    )
    if not bt:
        return None
    if iz:
        prefix = f"{bt}_{mp}_{iz}_"
    elif mp:
        prefix = f"{bt}_{mp}_"
    else:
        prefix = f"{bt}_"
    if not stem.startswith(prefix):
        return None
    tail = stem[len(prefix) :]
    m = re.match(r"(.+)_(\d{8})(?:_(\d+))?$", tail)
    if not m:
        return None
    pilot_part = m.group(1).replace("_", " ").strip()
    pilot_part = _clean_takmicar_display(pilot_part)
    return pilot_part if pilot_part else None


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


def _split_norm_into_tauris_header_blocks(norm: str) -> list[str]:
    """
    Više takmičara u jednom COM tekstu često dolazi kao više uzastavnih TauRIS izlaza
    (ponovo crte + red sa „TauRIS“). Svaki takav blok vraća poseban string.
    """
    rx = re.compile(r"(?ms)^\s*-{10,}\s*\n\s*TauRIS\b", re.IGNORECASE)
    matches = list(rx.finditer(norm))
    if len(matches) <= 1:
        return []
    pre = norm[: matches[0].start()].rstrip()
    out: list[str] = []
    for i, m in enumerate(matches):
        a = m.start()
        b = matches[i + 1].start() if i + 1 < len(matches) else len(norm)
        chunk = norm[a:b].rstrip()
        if i == 0 and pre:
            chunk = (pre + "\n\n" + chunk).rstrip()
        if chunk:
            out.append(chunk)
    return out


def _name_from_single_takmicar_line(line: str) -> str | None:
    line = " ".join(line.replace("\xa0", " ").split()).strip()
    m = re.match(r"(?is)^Takmi\S*\s*:\s*(.+)$", line)
    if not m:
        return None
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
    return name if name else None


def discover_takmicar_segments_from_raw_com_text(
    raw: str,
    *,
    race_data: dict[str, Any] | None = None,
    bundle_json_path: Path | None = None,
) -> list[tuple[str, str]]:
    """
    Podela sirovog COM teksta po takmičarima: više linija „Takmičar:“ ili više redova kontrol liste.
    Ako iz COM-a ime ne pročita, koristi race_data / meta ili ime iz naziva JSON fajla izvoza.
    """
    norm = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not norm.strip():
        return []

    def _name_hint() -> str | None:
        h = _hint_takmicar_from_race_data(race_data)
        if h:
            return h
        if bundle_json_path is not None and race_data is not None:
            return _hint_takmicar_from_export_json_stem(bundle_json_path, race_data)
        return None

    header_blocks = _split_norm_into_tauris_header_blocks(norm)
    if len(header_blocks) >= 2:
        out_h: list[tuple[str, str]] = []
        for i, chunk in enumerate(header_blocks):
            nm = (
                extract_takmicar_from_tauris_kontrol_list(chunk)
                or extract_takmicar_name_from_raw_text(chunk)
                or (_name_hint() if i == 0 else None)
                or f"Takmičar_{i + 1}"
            )
            nm = _clean_takmicar_display(str(nm)) or f"Takmičar_{i + 1}"
            out_h.append((nm, chunk.rstrip()))
        return out_h

    lines = norm.split("\n")
    tak_idx: list[int] = []
    for i, line in enumerate(lines):
        stripped = " ".join(line.replace("\xa0", " ").split()).strip()
        if re.match(r"(?is)^Takmi\S*\s*:\s*.+", stripped):
            tak_idx.append(i)

    if len(tak_idx) >= 2:
        preamble = "\n".join(lines[: tak_idx[0]]).rstrip()
        out: list[tuple[str, str]] = []
        for j, start in enumerate(tak_idx):
            nm = _name_from_single_takmicar_line(lines[start]) or f"Takmičar_{j + 1}"
            end = tak_idx[j + 1] if j + 1 < len(tak_idx) else len(lines)
            body = "\n".join(lines[start:end]).rstrip()
            text_out = (preamble + "\n\n" + body).strip() if preamble else body
            out.append((nm, text_out))
        return out

    if len(tak_idx) == 1:
        start = tak_idx[0]
        nm = (
            _name_from_single_takmicar_line(lines[start])
            or extract_takmicar_name_from_raw_text(norm)
            or _name_hint()
            or UNKNOWN_PILOT_LABEL
        )
        return [(nm, norm.rstrip())]

    matches = list(_KONTROL_LIST_LINE_RE.finditer(norm))
    if len(matches) >= 2:
        header = norm[: matches[0].start()].rstrip()
        out2: list[tuple[str, str]] = []
        for i, m in enumerate(matches):
            nm = _clean_takmicar_display(m.group(1))
            if not nm or nm[0].isdigit():
                nm = f"Takmičar_{i + 1}"
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(norm)
            seg = norm[start:end].rstrip()
            if header:
                seg = (header + "\n\n" + seg).strip()
            out2.append((nm, seg))
        return out2

    name = extract_takmicar_from_tauris_kontrol_list(norm)
    if not name:
        name = extract_takmicar_name_from_raw_text(norm)
    if not name:
        name = _name_hint()
    if not name:
        name = UNKNOWN_PILOT_LABEL
    return [(name, norm.rstrip())]


def load_race_data_from_bundle_json(json_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"JSON se ne može učitati: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON nije objekat.")
    rd = payload.get("race_data")
    if isinstance(rd, dict):
        out = dict(rd)
        meta = payload.get("meta")
        if isinstance(meta, dict):
            mt = meta.get("takmicar")
            if isinstance(mt, str) and mt.strip():
                has_t = any(
                    isinstance(out.get(k), str) and str(out.get(k) or "").strip()
                    for k in ("takmicar", "takmičar", "Takmicar", "ime_takmicara")
                )
                if not has_t:
                    out["takmicar"] = _clean_takmicar_display(mt)
        return out
    if isinstance(payload.get("raw_com_text"), str):
        return payload
    raise ValueError("U JSON-u nema race_data (očekuje se potpisani izvoz).")


def show_locked_bundle_action_choice(parent: tk.Misc) -> Literal["prikaz", "txt"] | None:
    """Izbor: prikaz kao ranije (PDF + folder) ili raspakovanje .txt po takmičarima."""
    chosen: list[Literal["prikaz", "txt"] | None] = [None]

    win = tk.Toplevel(parent)
    win.title("Zaključan paket")
    win.transient(parent)
    win.resizable(False, False)
    win.grab_set()
    frm = tk.Frame(win, padx=16, pady=14)
    frm.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        frm,
        text="Kako želite da otvorite izabrane pakete?",
        font=("Segoe UI", 10, "bold"),
    ).pack(anchor="w", pady=(0, 10))

    def pick(val: Literal["prikaz", "txt"]) -> None:
        chosen[0] = val
        win.destroy()

    tk.Button(
        frm,
        text="Prikaz dokumenta",
        width=28,
        command=lambda: pick("prikaz"),
    ).pack(fill=tk.X, pady=(0, 6))
    tk.Label(
        frm,
        text="Otključava privremeni folder, otvara PDF i Explorer.",
        fg="#4b5563",
        font=("Segoe UI", 9),
        wraplength=360,
        justify="left",
    ).pack(anchor="w", pady=(0, 10))

    tk.Button(
        frm,
        text="Raspakovanje .txt dokumenta",
        width=28,
        command=lambda: pick("txt"),
    ).pack(fill=tk.X, pady=(0, 6))
    tk.Label(
        frm,
        text=f'Pravi folder „{TXT_EXPORT_DIRNAME}“ pored .taurislock fajla i snima\n'
        "po jedan .txt za svakog izabranog takmičara (više izbora u sledećem koraku).",
        fg="#4b5563",
        font=("Segoe UI", 9),
        wraplength=360,
        justify="left",
    ).pack(anchor="w", pady=(0, 12))

    tk.Button(frm, text="Otkaži", width=12, command=win.destroy).pack(anchor="e")

    try:
        px = parent.winfo_rootx() + max(20, (parent.winfo_width() // 2) - 200)
        py = parent.winfo_rooty() + max(20, (parent.winfo_height() // 2) - 120)
        win.geometry(f"+{px}+{py}")
    except Exception:
        pass

    parent.wait_window(win)
    return chosen[0]


def show_takmicar_multi_select_dialog(
    parent: tk.Misc,
    segments: list[tuple[str, str]],
    *,
    bundle_label: str | None = None,
) -> list[int] | None:
    """
    Listbox sa višestrukim izborom (MULTIPLE: klik na red uključuje/isključuje bez Ctrl).
    Vraća indekse u ``segments`` ili None ako je Otkaži. Za svakog izabranog — poseban .txt.
    """
    counts: dict[str, int] = {}
    labels: list[str] = []
    for nm, _ in segments:
        counts[nm] = counts.get(nm, 0) + 1
        c = counts[nm]
        labels.append(nm if c == 1 else f"{nm} ({c})")

    state: dict[str, Any] = {"ok": False, "indices": []}

    win = tk.Toplevel(parent)
    win.title("Izbor takmičara za .txt" + (f" — {bundle_label}" if bundle_label else ""))
    win.transient(parent)
    win.grab_set()
    body = tk.Frame(win, padx=14, pady=12)
    body.pack(fill=tk.BOTH, expand=True)

    intro = (
        "Klik na red bira ili skida takmičara. Možete više odjednom — za svakog izabranog "
        "dobijate poseban .txt fajl."
    )
    if bundle_label:
        intro = f"Paket: {bundle_label}\n\n{intro}"
    tk.Label(body, text=intro, wraplength=440, justify="left").pack(anchor="w", pady=(0, 6))

    quick = tk.Frame(body)
    quick.pack(anchor="w", pady=(0, 6))

    list_fr = tk.Frame(body)
    list_fr.pack(fill=tk.BOTH, expand=True)
    scroll = tk.Scrollbar(list_fr)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    lb = tk.Listbox(
        list_fr,
        selectmode=tk.MULTIPLE,
        height=min(14, max(4, len(labels))),
        width=52,
        yscrollcommand=scroll.set,
        exportselection=False,
    )
    lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.config(command=lb.yview)
    for i, lab in enumerate(labels):
        lb.insert(tk.END, f"{i + 1}. {lab}")

    if len(labels) == 1:
        tk.Label(
            body,
            text="Napomena: u ovom paketu je u COM tekstu prepoznata samo jedna lista. Ako očekujete "
            "više takmičara, TAURIS mora u jednom slanju poslati više TauRIS zaglavlja (blokova) ili "
            "više redova kontrol liste; inače izaberite više .taurislock paketa u prvom koraku.",
            fg="#6b7280",
            font=("Segoe UI", 9),
            wraplength=440,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    def select_all() -> None:
        lb.selection_set(0, tk.END)

    def select_none() -> None:
        lb.selection_clear(0, tk.END)

    tk.Button(quick, text="Izaberi sve", width=12, command=select_all).pack(side=tk.LEFT, padx=(0, 6))
    tk.Button(quick, text="Poništi izbor", width=12, command=select_none).pack(side=tk.LEFT)

    btn_row = tk.Frame(body)
    btn_row.pack(fill=tk.X, pady=(12, 0))

    def on_ok() -> None:
        sel = sorted(int(x) for x in lb.curselection())
        if not sel:
            messagebox.showwarning("Izbor", "Izaberite bar jednog takmičara.", parent=win)
            return
        state["ok"] = True
        state["indices"] = sel
        win.destroy()

    def on_cancel() -> None:
        win.destroy()

    tk.Button(btn_row, text="OK", width=10, command=on_ok).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(btn_row, text="Otkaži", width=10, command=on_cancel).pack(side=tk.LEFT)

    try:
        px = parent.winfo_rootx() + max(20, (parent.winfo_width() // 2) - 220)
        py = parent.winfo_rooty() + max(20, (parent.winfo_height() // 2) - 160)
        win.geometry(f"+{px}+{py}")
    except Exception:
        pass

    parent.wait_window(win)
    if not state["ok"]:
        return None
    return list(state["indices"])


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
    PDF u stilu izlaza iz Analize leta: kratak blok takmičar/staza, zatim monospaced lista sa uređaja.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Preformatted, SimpleDocTemplate

    raw = str(race_data.get("raw_com_text") or "").strip()
    _ = port_name  # ostaje u potpisu funkcije radi kompatibilnosti; ne štampa se u PDF

    head = (
        f"Takmičar: {pilot_name}\n"
        f"Grupacija: {race_data.get('grupacija', '')}\n"
        f"Broj trke: {race_data.get('broj_trke', '')}\n"
        f"Mesto puštanja: {race_data.get('mesto_pustanja', '')}\n"
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

    mono_style = ParagraphStyle(
        "TaurisLogMono",
        parent=styles["Code"],
        fontName=mono_face,
        fontSize=PDF_LOG_FONT_SIZE_PT,
        leading=PDF_LOG_LEADING_PT,
        textColor=colors.black,
        alignment=TA_LEFT,
        leftIndent=0,
        rightIndent=0,
    )
    story: list[Any] = []
    check = race_data.get("bricon_clock_check")
    if isinstance(check, dict) and check.get("message"):
        from reportlab.platypus import Paragraph, Spacer
        from xml.sax.saxutils import escape as _xml_escape

        ok = check.get("ok")
        color = "#1b6e2a" if ok is True else ("#b00020" if ok is False else "#555555")
        bg = colors.HexColor("#e8f5e9") if ok is True else (
            colors.HexColor("#fdecea") if ok is False else colors.HexColor("#f2f2f2")
        )
        banner_style = ParagraphStyle(
            "BriconClockCheck",
            parent=styles["Normal"],
            fontName=register_font_if_available(),
            fontSize=PDF_REPORT_BODY_FONT_PT + 1,
            leading=(PDF_REPORT_BODY_FONT_PT + 1) * 1.3,
            textColor=colors.HexColor(color),
            backColor=bg,
            borderColor=colors.HexColor(color),
            borderWidth=0.8,
            borderPadding=5,
        )
        # Bez <b>: na Windows-u je font registrovan kao jedan TTF (Arial) bez bold porodice —
        # reportlab bi tada bacio grešku „Can't map determine family/bold“.
        lines = [_xml_escape(str(check["message"]))]
        for row in check.get("flights") or []:
            def _f(v: Any) -> str:
                return "—" if v is None else f"{'+' if v >= 0 else '−'}{abs(int(v))} s"
            lines.append(
                _xml_escape(
                    f"Let {int(row.get('index', 0)) + 1} ({row.get('flight_id') or '-'}): "
                    f"ukorpavanje {_f(row.get('basketing_deviation_sec'))}, "
                    f"očitavanje {_f(row.get('readout_deviation_sec'))}, "
                    f"hod sata {_f(row.get('drift_sec'))}"
                )
            )
        story.append(Paragraph("<br/>".join(lines), banner_style))
        story.append(Spacer(1, 4 * mm))
    story.append(Preformatted(body, mono_style))
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

    doc.setFont(font_name, PDF_REPORT_TITLE_FONT_PT)
    doc.drawString(left + 6, top, "TAURIS - Izvestaj trke")

    doc.setFont(font_name, PDF_REPORT_BODY_FONT_PT)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hdr_gap = 16 * PDF_FONT_SCALE
    hdr_gap2 = 30 * PDF_FONT_SCALE
    doc.drawString(left + 6, top - hdr_gap, f"Takmičar: {pilot_name}")
    doc.drawString(left + 220, top - hdr_gap, f"Port: {port_name}")
    doc.drawString(left + 6, top - hdr_gap2, f"Datum generisanja: {generated_at}")

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

    y = top - (50 * PDF_FONT_SCALE)
    row_height = PDF_REPORT_ROW_HEIGHT_MM * mm
    key_width = 55 * mm
    row_text_dy = 3 * PDF_FONT_SCALE

    doc.setFont(font_name, PDF_REPORT_BODY_FONT_PT)
    for key, value in race_data.items():
        if y < 30 * mm:
            doc.showPage()
            doc.setFont(font_name, PDF_REPORT_BODY_FONT_PT)
            y = height - (25 * mm)
        doc.rect(left + 6, y - 2, key_width, row_height, stroke=1, fill=0)
        doc.rect(left + 6 + key_width, y - 2, (right - left) - key_width - 12, row_height, stroke=1, fill=0)
        doc.drawString(left + 10, y + row_text_dy, str(key)[:28])
        doc.drawString(left + 12 + key_width, y + row_text_dy, format_value(value)[:75])
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


def iter_protected_export_sets_in_folder(folder: Path) -> list[tuple[dict[str, Any], Path, Path, Path]]:
    """
    Svi zašticeni JSON izvozi u folderu (uključujući podfoldere), uz PDF istog osnova;
    TXT istog osnova ako postoji (npr. za ZIP slanja).
    """
    folder = folder.resolve()
    out: list[tuple[dict[str, Any], Path, Path, Path]] = []
    if not folder.is_dir():
        return out
    for json_path in sorted(folder.rglob("*.json")):
        try:
            raw = json_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not all(k in data for k in ("race_data", "data_hash", "signature_hmac_sha256")):
            continue
        pdf_path = json_path.with_suffix(".pdf")
        if not pdf_path.is_file():
            continue
        txt_path = json_path.with_suffix(".txt")
        out.append((data, json_path, pdf_path, txt_path))
    return out


def save_protected_json(payload: dict[str, Any], target_path: Path) -> None:
    clear_export_file_readonly(target_path)
    target_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    set_export_file_readonly(target_path)


def smtp_settings_complete(config: DeliveryConfig) -> bool:
    return all(
        [
            config.smtp_host,
            config.smtp_user,
            config.smtp_password,
            config.mail_from,
            config.mail_to,
        ]
    )


def _walk_exception_graph(exc: BaseException) -> list[BaseException]:
    """__cause__ / __context__ — za prepoznavanje mrežnih grešaka u lancu."""
    seen: set[int] = set()
    out: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        out.append(e)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None and e.__context__ is not e.__cause__:
            stack.append(e.__context__)
    return out


def _is_network_unreachable_error(exc: BaseException) -> bool:
    """Heuristika: nema veze / DNS (npr. Windows [Errno 11001] getaddrinfo failed)."""
    for e in _walk_exception_graph(exc):
        if isinstance(e, socket.gaierror):
            return True
        if isinstance(e, OSError):
            winerr = int(getattr(e, "winerror", 0) or 0)
            n = int(e.errno or 0)
            if winerr in (11001, 11002, 10050, 10051):
                return True
            if n in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN):
                return True
        msg = str(e).lower()
        if "getaddrinfo" in msg and "failed" in msg:
            return True
        if "name or service not known" in msg:
            return True
        if "temporary failure in name resolution" in msg:
            return True
    return False


def _offline_send_instructions_for_data_folder(data_folder: Path) -> str:
    p = str(data_folder.resolve())
    return (
        "Nema internet veze — slanje podataka na obradu preko programa trenutno nije moguće.\n\n"
        "Pošaljite administratoru podatke na obradu ručno (npr. kada bude veza).\n\n"
        "Lokacija podataka na ovom računaru:\n"
        f"{p}"
    )


def _offline_send_instructions_for_locked_bundle(locked_bundle_path: Path) -> str:
    lock_f = locked_bundle_path.resolve()
    folder = lock_f.parent
    return (
        "Nema internet veze — automatsko slanje podataka na obradu nije uspelo.\n\n"
        "Pošaljite administratoru podatke na obradu ručno.\n\n"
        f"Zaključan paket:\n{lock_f}\n\n"
        f"Folder sa rezultatima:\n{folder}"
    )


def _smtp_send_email_message(message: EmailMessage, config: DeliveryConfig) -> None:
    if not smtp_settings_complete(config):
        raise RuntimeError("Podešavanja slanja nisu kompletna (host/user/pass/from/to).")
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_sec) as smtp:
        smtp.starttls()
        smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)


def _results_email_subject_from_payload(protected_payload: dict[str, Any]) -> str:
    """Naslov mejla: broj_trke-Grupacija_N-mesto_pustanja-takmičar-datum."""
    meta = protected_payload.get("meta") or {}
    rd = protected_payload.get("race_data") or {}
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(rd, dict):
        rd = {}

    bt = sanitize_filename(str(rd.get("broj_trke") or "").strip() or "000")
    g_raw = str(rd.get("grupacija") or "").strip()
    if not g_raw:
        g_raw = GRUPACIJA_LABELS[0]
    gr = sanitize_filename(g_raw)
    mp = sanitize_filename(str(rd.get("mesto_pustanja") or "").strip() or "nepoznato")
    tak = meta.get("takmicar") or rd.get("takmicar") or rd.get("takmičar") or rd.get("Takmicar")
    tak = sanitize_filename(str(tak or "").strip() or "nepoznato")

    cap = meta.get("captured_at")
    datum = datetime.now().strftime("%d.%m.%Y")
    if isinstance(cap, str) and cap.strip():
        try:
            iso = cap.strip().replace("Z", "+00:00")
            datum = datetime.fromisoformat(iso).strftime("%d.%m.%Y")
        except ValueError:
            pass

    subject = f"{bt}-{gr}-{mp}-{tak}-{datum}"
    if len(subject) > 220:
        subject = subject[:217] + "..."
    return subject


def send_email(
    protected_payload: dict[str, Any],
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    config: DeliveryConfig,
) -> str:
    if not smtp_settings_complete(config):
        raise RuntimeError("Podešavanja slanja nisu kompletna (host/user/pass/from/to).")

    message = EmailMessage()
    message["Subject"] = _results_email_subject_from_payload(protected_payload)
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

    _smtp_send_email_message(message, config)
    return "Podaci poslati na obradu."


def _safe_zip_stub(folder_name: str) -> str:
    raw = re.sub(r"[^\w\-.]+", "_", folder_name.strip())[:48].strip("_")
    return raw if raw else "tauris_rezultati"


def _folder_entries_for_zip(folder: Path) -> list[tuple[Path, str]]:
    """Puni ZIP: .taurislock + JSON+PDF(+TXT) isto pravilo kao ranije (bez duplog stem-a)."""
    folder = folder.resolve()
    json_sets = iter_protected_export_sets_in_folder(folder)
    bundles = sorted(folder.rglob("*.taurislock"))
    sent_stems: set[str] = set()
    out: list[tuple[Path, str]] = []
    for bundle in bundles:
        arc = bundle.resolve().relative_to(folder).as_posix()
        out.append((bundle, arc))
        sent_stems.add(bundle.stem)
    for _payload, jp, pp, tp in json_sets:
        if jp.stem in sent_stems:
            continue
        for p in (jp, pp):
            if p.is_file():
                arc = p.resolve().relative_to(folder).as_posix()
                out.append((p, arc))
        if tp.is_file() and tp.stat().st_size > 0:
            arc = tp.resolve().relative_to(folder).as_posix()
            out.append((tp, arc))
    return out


def expected_results_subfolder_path(grupacija_label: str, broj_trke_label: str) -> Path:
    """Isto pravilo imena kao kod snimanja (resolve_output_directory), bez kreiranja foldera."""
    broj_trke = sanitize_filename(str(broj_trke_label or "").strip())
    grupacija = sanitize_filename(str(grupacija_label or "").strip())
    return RESULTS_DESKTOP_ROOT / f"{broj_trke}-{grupacija}"


def fallback_results_subfolder_path(grupacija_label: str, broj_trke_label: str) -> Path:
    broj_trke = sanitize_filename(str(broj_trke_label or "").strip())
    grupacija = sanitize_filename(str(grupacija_label or "").strip())
    return RESULTS_DESKTOP_FALLBACK_ROOT / f"{broj_trke}-{grupacija}"


def resolve_upload_folder_for_current_staza(grupacija_label: str, broj_trke_label: str) -> Path | None:
    """
    Podfolder na Desktopu (REZULTATI_TRKE ili REZULTATI_TRKE_RECOVERY) za grupaciju i broj trke,
    ako postoji i sadrži .taurislock ili parove JSON+PDF za slanje.
    """
    for folder in (
        expected_results_subfolder_path(grupacija_label, broj_trke_label),
        fallback_results_subfolder_path(grupacija_label, broj_trke_label),
    ):
        if not folder.is_dir():
            continue
        try:
            resolved = folder.resolve()
            if _folder_entries_for_zip(resolved):
                return resolved
        except Exception:
            logging.exception("Provera foldera za slanje: %s", folder)
    return None


def _build_zip_bytes(paths_and_arcs: list[tuple[Path, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, arc in paths_and_arcs:
            zf.write(path, arc)
    return buf.getvalue()


def _batch_entries_by_uncompressed_size(
    entries: list[tuple[Path, str]],
    target_batch_bytes: int,
) -> list[list[tuple[Path, str]]]:
    batches: list[list[tuple[Path, str]]] = []
    cur: list[tuple[Path, str]] = []
    acc = 0
    for path, arc in entries:
        sz = path.stat().st_size if path.is_file() else 0
        if sz >= EMAIL_ZIP_MAX_ATTACHMENT_BYTES:
            if cur:
                batches.append(cur)
                cur = []
                acc = 0
            batches.append([(path, arc)])
            continue
        if cur and acc + sz > target_batch_bytes:
            batches.append(cur)
            cur = []
            acc = 0
        cur.append((path, arc))
        acc += sz
    if cur:
        batches.append(cur)
    return batches


def _split_group_until_zip_fits(
    group: list[tuple[Path, str]],
    max_zip_bytes: int,
) -> list[list[tuple[Path, str]]]:
    if not group:
        return []
    zb = _build_zip_bytes(group)
    if len(zb) <= max_zip_bytes:
        return [group]
    if len(group) == 1:
        return [group]
    mid = len(group) // 2
    return _split_group_until_zip_fits(group[:mid], max_zip_bytes) + _split_group_until_zip_fits(
        group[mid:], max_zip_bytes
    )


def _refine_zip_batches(
    batches: list[list[tuple[Path, str]]],
    max_zip_bytes: int,
) -> list[list[tuple[Path, str]]]:
    out: list[list[tuple[Path, str]]] = []
    for group in batches:
        out.extend(_split_group_until_zip_fits(group, max_zip_bytes))
    return out


def _obrada_email_subject(
    broj_trke: str,
    grupacija: str,
    *,
    part: int = 1,
    total_parts: int = 1,
) -> str:
    """Naslov mejla za slanje foldera na obradu: npr. 001-Grupacija_2-KOMPLET."""
    bt = sanitize_filename(str(broj_trke or "").strip() or "000")
    gr = sanitize_filename(str(grupacija or "").strip() or GRUPACIJA_LABELS[0])
    subject = f"{bt}-{gr}-KOMPLET"
    if total_parts > 1:
        subject = f"{subject} ({part}/{total_parts})"
    if len(subject) > 220:
        subject = subject[:217] + "..."
    return subject


def _parse_staza_from_results_folder_name(folder_name: str) -> tuple[str, str] | None:
    """Iz imena podfoldera ``001-Grupacija_2`` vrati broj trke i grupaciju."""
    name = (folder_name or "").strip()
    if "-" not in name:
        return None
    broj, grupa = name.split("-", 1)
    broj = broj.strip()
    grupa = grupa.strip()
    if not broj:
        return None
    return broj, grupa or GRUPACIJA_LABELS[0]


def _staza_labels_for_folder_email(folder: Path, broj_trke: str, grupacija: str) -> tuple[str, str]:
    bt = str(broj_trke or "").strip()
    gr = str(grupacija or "").strip()
    if bt and gr:
        return bt, gr
    parsed = _parse_staza_from_results_folder_name(folder.name)
    if parsed:
        return parsed
    return "000", GRUPACIJA_LABELS[0]


def _attachment_mime_type(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "application", "pdf"
    if ext == ".json":
        return "application", "json"
    if ext == ".txt":
        return "text", "plain"
    return "application", "octet-stream"


def _batch_paths_by_total_size(paths: list[Path], target_batch_bytes: int) -> list[list[Path]]:
    batches: list[list[Path]] = []
    cur: list[Path] = []
    acc = 0
    for path in paths:
        sz = path.stat().st_size if path.is_file() else 0
        if sz >= EMAIL_ZIP_MAX_ATTACHMENT_BYTES:
            if cur:
                batches.append(cur)
                cur = []
                acc = 0
            batches.append([path])
            continue
        if cur and acc + sz > target_batch_bytes:
            batches.append(cur)
            cur = []
            acc = 0
        cur.append(path)
        acc += sz
    if cur:
        batches.append(cur)
    return batches


def _refine_file_batches_for_email(
    batches: list[list[Path]],
    max_bytes: int,
) -> list[list[Path]]:
    out: list[list[Path]] = []
    for group in batches:
        total = sum(p.stat().st_size for p in group if p.is_file())
        if total <= max_bytes or len(group) <= 1:
            out.append(group)
        else:
            mid = len(group) // 2
            out.extend(_refine_file_batches_for_email([group[:mid], group[mid:]], max_bytes))
    return out


def send_email_folder_file_batch(
    paths: list[Path],
    *,
    broj_trke: str,
    grupacija: str,
    part: int,
    total_parts: int,
    config: DeliveryConfig,
) -> str:
    """Pošalji listu fajlova kao zasebne priloge (bez ZIP arhive)."""
    if not smtp_settings_complete(config):
        raise RuntimeError("Podešavanja slanja nisu kompletna (host/user/pass/from/to).")
    if not paths:
        raise ValueError("Nema fajlova za slanje.")

    message = EmailMessage()
    message["Subject"] = _obrada_email_subject(
        broj_trke, grupacija, part=part, total_parts=total_parts
    )
    message["From"] = config.mail_from
    message["To"] = config.mail_to

    lines = [f"- {p.name}" for p in paths]
    warn = ""
    for p in paths:
        if p.is_file() and p.stat().st_size > EMAIL_ZIP_MAX_ATTACHMENT_BYTES:
            warn = (
                "\n\nUPOZORENJE: neki prilozi su veći od preporučenog limita — "
                "proverite da li su svi fajlovi stigli primaocu."
            )
            break
    body = (
        "Rezultati sa TAURIS uređaja — prilozi su poslati bez ZIP arhive "
        "(otvaraju se direktno u emailu).\n\n"
        f"Fajlovi u ovom mejlu ({len(paths)}):\n"
        + "\n".join(lines[:200])
    )
    if len(lines) > 200:
        body += f"\n… (+ još {len(lines) - 200} fajlova)"
    message.set_content((body + warn)[:12000])

    for path in paths:
        maintype, subtype = _attachment_mime_type(path)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    _smtp_send_email_message(message, config)
    if total_parts <= 1:
        return f"Podaci poslati na obradu ({len(paths)} priloga)."
    return f"Podaci poslati na obradu — deo {part}/{total_parts} ({len(paths)} priloga)."


def send_folder_to_obrada_by_email(
    folder: Path,
    *,
    broj_trke: str,
    grupacija: str,
    config: DeliveryConfig,
) -> list[str]:
    """
    Pošalji sadržaj foldera na obradu: .taurislock i/ili JSON+PDF kao zasebni prilozi.
    Ako ne stane u jedan mejl, deli na više poruka (isti naslov + deo N/M).
    """
    folder = folder.resolve()
    entries = _folder_entries_for_zip(folder)
    if not entries:
        raise ValueError(
            "U folderu nema šta da se pošalje: parovi JSON+PDF ili fajlovi .taurislock."
        )

    paths = [p.resolve() for p, _arc in entries]
    bt, gr = _staza_labels_for_folder_email(folder, broj_trke, grupacija)
    rough = _batch_paths_by_total_size(paths, EMAIL_ZIP_BATCH_UNCOMPRESSED_BYTES)
    batches = _refine_file_batches_for_email(rough, EMAIL_ZIP_MAX_ATTACHMENT_BYTES)
    notes: list[str] = []
    total_parts = len(batches)
    ok_count = 0

    for i, batch in enumerate(batches, start=1):
        try:
            notes.append(
                send_email_folder_file_batch(
                    batch,
                    broj_trke=bt,
                    grupacija=gr,
                    part=i,
                    total_parts=total_parts,
                    config=config,
                )
            )
            ok_count += 1
        except Exception as exc:
            notes.append(f"Deo {i}/{total_parts}: {exc}")

    if ok_count == 0:
        raise ValueError("\n".join(notes))
    return notes


def send_email_zip_archive(
    zip_bytes: bytes,
    attachment_filename: str,
    *,
    part: int,
    total_parts: int,
    member_summary: str,
    config: DeliveryConfig,
) -> str:
    if not smtp_settings_complete(config):
        raise RuntimeError("Podešavanja slanja nisu kompletna (host/user/pass/from/to).")

    message = EmailMessage()
    if total_parts <= 1:
        message["Subject"] = "TAURIS rezultati (ZIP)"
    else:
        message["Subject"] = f"TAURIS rezultati (ZIP deo {part}/{total_parts})"
    message["From"] = config.mail_from
    message["To"] = config.mail_to
    body = (
        "ZIP prilog sadrži .taurislock i/ili potpisane JSON+PDF iz izabranog foldera "
        "(isti sadržaj kao ranije — samo spakovano u jedan prilog).\n\n"
        f"{member_summary}"
    )
    message.set_content(body[:12000])
    message.add_attachment(
        zip_bytes,
        maintype="application",
        subtype="zip",
        filename=attachment_filename,
    )
    _smtp_send_email_message(message, config)
    return f"Podaci poslati na obradu ({attachment_filename})."


def transmit_results(
    protected_payload: dict[str, Any],
    json_path: Path,
    pdf_path: Path,
    txt_path: Path,
    delivery_config: DeliveryConfig,
) -> str:
    if delivery_config.mode == "none":
        return "Lokalno sačuvano (slanje nije uključeno)."
    if delivery_config.mode == "email":
        return send_email(protected_payload, json_path, pdf_path, txt_path, delivery_config)
    return "Lokalno sačuvano (nepoznat mode slanja)."


def run_pipeline(
    race_data: dict[str, Any],
    pilot_name: str,
    port_name: str,
    delivery_config: DeliveryConfig,
    *,
    lock_password: str,
) -> tuple[Path, str, bytes]:
    validate_race_data(race_data)
    lp = (lock_password or "").strip()
    if not lp:
        raise ValueError("Nedostaje lozinka za zaključavanje (.taurislock).")
    pilot_effective = resolve_pilot_name_for_output(race_data, pilot_name)
    if pilot_effective != UNKNOWN_PILOT_LABEL:
        race_data = dict(race_data)
        race_data["takmicar"] = pilot_effective
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
        password=lp,
    )
    lock_results_folder(json_path.parent)
    logging.info(
        "Integritet PDF: SHA256=%s… (ceo hash u JSON meta.pdf_sha256)",
        protected_payload["meta"]["pdf_sha256"][:16],
    )
    logging.info("Kreiran zaključan paket: %s", locked_bundle_path)
    try:
        delivery_note = transmit_results(
            protected_payload, json_path, pdf_path, txt_path, delivery_config
        )
    except Exception as exc:
        from tauris_desktop.pending_sends import enqueue_pending_send

        staza = _parse_staza_from_results_folder_name(locked_bundle_path.parent.name) or ("", "")
        n_pending = enqueue_pending_send(
            locked_bundle_path, broj_trke=staza[0], grupacija=staza[1], error=f"{type(exc).__name__}: {exc}"
        )
        if _is_network_unreachable_error(exc):
            # Očekivana situacija na terenu — jedna linija u logu, bez traceback-a.
            logging.warning(
                "Nema internet veze — paket nije poslat, čeka u redu (%s u redu): %s",
                n_pending, locked_bundle_path.name,
            )
            delivery_note = (
                "Nema internet veze — paket je sačuvan i stavljen u red za slanje.\n\n"
                "Program će ga sam poslati kad veza bude dostupna (pri sledećem pokretanju ili "
                "iz menija Baza → «Pošalji neposlate pakete»).\n\n"
                f"Neposlatih paketa u redu: {n_pending}\n"
                f"Zaključan paket: {locked_bundle_path}"
            )
        else:
            logging.exception("Slanje rezultata nije uspelo; paket je sačuvan i stavljen u red.")
            delivery_note = (
                f"Upozorenje: automatsko slanje nije uspelo ({exc}). "
                "Zaključan paket je sačuvan lokalno i stavljen u red za naknadno slanje "
                f"(u redu: {n_pending}) — proverite podešavanja slanja na obradu."
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


def _tk_winfo_id_to_hwnd_int(wid: object) -> int:
    """
    Tk ``winfo_id()`` na Windows-u je obično ceo broj (HWND). Na nekim građama
    (npr. Python 3.14 + Tk) može vratiti raw bajtove pokazivača — ``int()`` nad
    bytes pada; ovde čitamo LE uintptr (do 64 bita).
    """
    if isinstance(wid, int):
        return wid & 0xFFFFFFFFFFFFFFFF
    if isinstance(wid, bytes):
        if not wid:
            return 0
        chunk = wid[:8].ljust(8, b"\x00")
        return int.from_bytes(chunk, "little", signed=False)
    try:
        return int(wid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _windows_build_devmode_a4_portrait(hwnd_owner: int) -> int:
    """GlobalAlloc(HGLOBAL) sa DEVMODE: A4, portret (za početak PrintDlg-a)."""
    import ctypes
    from ctypes import wintypes

    import pywintypes  # type: ignore
    import win32con as wcon  # type: ignore
    import win32print  # type: ignore

    GMEM_MOVEABLE = 0x0002
    GMEM_ZEROINIT = 0x0040
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    pn = win32print.GetDefaultPrinter()
    if not pn:
        return 0
    hp = win32print.OpenPrinter(pn)
    try:
        sz = int(win32print.DocumentProperties(hwnd_owner, hp, pn, None, None, 0))
        if sz <= 0:
            return 0
        hmem = int(k32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, sz))
        if not hmem:
            return 0
        ptr = k32.GlobalLock(hmem)
        if not ptr:
            k32.GlobalFree(wintypes.HGLOBAL(hmem))
            return 0
        try:
            dm = pywintypes.DEVMODEType(ptr)
            win32print.DocumentProperties(hwnd_owner, hp, pn, dm, None, wcon.DM_OUT_BUFFER)
            dm.Fields = int(dm.Fields) | wcon.DM_ORIENTATION | wcon.DM_PAPERSIZE
            dm.Orientation = wcon.DMORIENT_PORTRAIT
            dm.PaperSize = wcon.DMPAPER_A4
            win32print.DocumentProperties(
                hwnd_owner,
                hp,
                pn,
                dm,
                dm,
                wcon.DM_IN_BUFFER | wcon.DM_OUT_BUFFER,
            )
        finally:
            k32.GlobalUnlock(wintypes.HGLOBAL(hmem))
    finally:
        win32print.ClosePrinter(hp)
    return hmem


def print_pdf_via_system_dialog(pdf_path: Path, parent: tk.Misc) -> None:
    """
    Otvara sistemski dijalog za izbor štampača i podešavanja.
    Windows: PrintDlg + DEVMODE podrazumevano A4 portret; štampa preko PyMuPDF + GDI.
    Ostalo: lp na podrazumevani štampač (bez dijaloga).
    """
    path = pdf_path.resolve()
    if not path.is_file():
        messagebox.showwarning("Štampanje", f"Fajl ne postoji:\n{path}", parent=parent)
        return

    if sys.platform != "win32":
        try:
            r = subprocess.run(
                ["lp", str(path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip() or f"izlazni kod {r.returncode}"
                raise RuntimeError(err)
        except FileNotFoundError:
            messagebox.showwarning(
                "Štampanje",
                "Na ovom sistemu nije dostupan Windows dijalog štampača.\n"
                "Instalirajte CUPS / lp ili otvorite PDF u pregledaču i odštampajte odatle.",
                parent=parent,
            )
        except Exception as exc:
            logging.exception("Štampanje (lp): %s", path)
            messagebox.showerror("Štampanje", str(exc), parent=parent)
        return

    try:
        import ctypes
        from ctypes import wintypes

        import fitz  # type: ignore
        import win32con as wcon  # type: ignore
        import win32ui  # type: ignore
        from PIL import Image  # type: ignore
        from PIL import ImageWin  # type: ignore
    except ImportError as exc:
        logging.exception("Štampanje: nedostaju pywin32 / PyMuPDF / Pillow.")
        messagebox.showerror(
            "Štampanje",
            "Za dijalog štampača potrebni su paketi: pywin32, PyMuPDF, Pillow.\n\n"
            f"Detalj: {exc}",
            parent=parent,
        )
        return

    class PRINTDLGW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hDevMode", wintypes.HGLOBAL),
            ("hDevNames", wintypes.HGLOBAL),
            ("hDC", wintypes.HDC),
            ("Flags", wintypes.DWORD),
            ("nFromPage", wintypes.WORD),
            ("nToPage", wintypes.WORD),
            ("nMinPage", wintypes.WORD),
            ("nMaxPage", wintypes.WORD),
            ("nCopies", wintypes.WORD),
            ("hInstance", wintypes.HINSTANCE),
            ("lCustData", wintypes.LPARAM),
            ("lpfnPrintHook", ctypes.c_void_p),
            ("lpfnSetupHook", ctypes.c_void_p),
            ("lpPrintTemplateName", wintypes.LPCWSTR),
            ("lpSetupTemplateName", wintypes.LPCWSTR),
            ("hPrintTemplate", wintypes.HGLOBAL),
            ("hSetupTemplate", wintypes.HGLOBAL),
        ]

    PD_RETURNDC = 0x100
    PD_NOPAGENUMS = 0x8
    PD_NOSELECTION = 0x4
    PD_HIDEPRINTTOFILE = 0x100000
    PD_USEDEVMODECOPIES = 0x40000

    k32 = ctypes.windll.kernel32
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL

    comdlg = ctypes.windll.comdlg32
    comdlg.PrintDlgW.argtypes = [ctypes.POINTER(PRINTDLGW)]
    comdlg.PrintDlgW.restype = wintypes.BOOL

    try:
        hwnd_int = _tk_winfo_id_to_hwnd_int(parent.winfo_id())
    except Exception:
        hwnd_int = 0
    hwnd = wintypes.HWND(hwnd_int)

    preset_devmode = 0
    try:
        preset_devmode = _windows_build_devmode_a4_portrait(hwnd_int)
    except Exception:
        logging.exception("Početni DEVMODE (A4) — nastavi bez njega.")

    pd = PRINTDLGW()
    pd.lStructSize = ctypes.sizeof(PRINTDLGW)
    pd.hwndOwner = hwnd
    pd.hDevMode = wintypes.HGLOBAL(preset_devmode) if preset_devmode else wintypes.HGLOBAL(0)
    pd.hDevNames = wintypes.HGLOBAL(0)
    pd.Flags = (
        PD_RETURNDC
        | PD_NOPAGENUMS
        | PD_NOSELECTION
        | PD_HIDEPRINTTOFILE
        | PD_USEDEVMODECOPIES
    )

    if not comdlg.PrintDlgW(ctypes.byref(pd)):
        if preset_devmode:
            k32.GlobalFree(wintypes.HGLOBAL(preset_devmode))
        return

    hdc_print = int(pd.hDC)
    if not hdc_print:
        if pd.hDevMode:
            k32.GlobalFree(pd.hDevMode)
        if pd.hDevNames:
            k32.GlobalFree(pd.hDevNames)
        messagebox.showerror("Štampanje", "Štampač nije izabran (prazan DC).", parent=parent)
        return

    dc = None
    doc = None
    try:
        dc = win32ui.CreateDCFromHandle(hdc_print)
        doc = fitz.open(str(path))
        pw = dc.GetDeviceCaps(wcon.HORZRES)
        ph = dc.GetDeviceCaps(wcon.VERTRES)
        dc.StartDoc("Tauris PDF")
        try:
            for i in range(len(doc)):
                page = doc.load_page(i)
                rect = page.rect
                zw = pw / rect.width
                zh = ph / rect.height
                zoom = min(zw, zh)
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                dib = ImageWin.Dib(img)
                off_x = max(0, (pw - pix.width) // 2)
                off_y = max(0, (ph - pix.height) // 2)
                dc.StartPage()
                dib.draw(
                    dc.GetHandleOutput(),
                    (off_x, off_y, off_x + pix.width, off_y + pix.height),
                )
                dc.EndPage()
        finally:
            dc.EndDoc()
    except Exception as exc:
        logging.exception("Greška tokom štampanja: %s", path)
        messagebox.showerror("Štampanje", str(exc), parent=parent)
    finally:
        if doc is not None:
            doc.close()
        if dc is not None:
            dc.DeleteDC()
        if pd.hDevMode:
            k32.GlobalFree(pd.hDevMode)
        if pd.hDevNames:
            k32.GlobalFree(pd.hDevNames)


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

    def _print_pdf() -> None:
        print_pdf_via_system_dialog(p, pdf_win)

    bottom = tk.Frame(pdf_win)
    bottom.pack(fill=tk.X, pady=(0, 8))
    tk.Button(bottom, text="Zatvori pregled", command=pdf_win.destroy, width=16).pack(side=tk.RIGHT, padx=10)
    tk.Button(
        bottom,
        text="Štampaj…",
        command=_print_pdf,
        width=14,
    ).pack(side=tk.RIGHT, padx=6)

    pdf_win.update_idletasks()
    pdf_win.lift()
    pdf_win.attributes("-topmost", True)
    pdf_win.after(450, lambda w=pdf_win: w.attributes("-topmost", False))


