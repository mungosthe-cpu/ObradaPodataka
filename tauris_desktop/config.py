"""Konstante i konfiguracioni tipovi — bez tajni u izvornom kodu (SMTP iz JSON-a ili env)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_JSON_DIR = Path("output")
RESULTS_DESKTOP_ROOT = Path.home() / "Desktop" / "REZULTATI_TRKE"
RESULTS_DESKTOP_FALLBACK_ROOT = Path.home() / "Desktop" / "REZULTATI_TRKE_RECOVERY"
GUI_SETTINGS_FILENAME = "tauris_gui_settings.json"
TAURIS_LOCK_PASSWORD_ENV = "TAURIS_LOCK_PASSWORD"
DEFAULT_TAURISLOCK_BUNDLE_PASSWORD = "TaurisDesktop"
LEGACY_PLAIN_TAURISLOCK_JSON_KEY = "taurislock_password"
TXT_EXPORT_DIRNAME = "txt dokumenti"
OPEN_PDF_FOLDER_NAME = "Open PDF"
OPEN_TXT_FOLDER_NAME = "Open TXT"
APP_VERSION = "2.0.1"
MAIN_WINDOW_SKY_BG = "#f3f9ff"
MAIN_WINDOW_BG_IMAGE_REL = "ICON/Pigeon in sky.png"
# Dugme «Preuzmi» — plava paleta u tonu pozadine (#f3f9ff, #6ba3d6)
DOWNLOAD_BTN_FONT = ("Segoe UI", 13, "bold")
DOWNLOAD_BTN_BG = "#3a7ec4"
DOWNLOAD_BTN_FG = "#ffffff"
DOWNLOAD_BTN_ACTIVE_BG = "#2d6aad"
DOWNLOAD_BTN_DISABLED_BG = "#b4cfe8"
DOWNLOAD_BTN_DISABLED_FG = "#5a7a9a"
DOWNLOAD_BTN_HIGHLIGHT_BG = "#9cc8ef"
DOWNLOAD_BTN_HIGHLIGHT_COLOR = "#5b9bd5"
DOWNLOAD_BTN_PADX = 25
DOWNLOAD_BTN_PADY = 14
DOWNLOAD_BTN_WIDTH = 32
DOWNLOAD_BTN_BORDER = 2
DOWNLOAD_BTN_MIN_HEIGHT_PX = 52
GITHUB_RELEASES_URL = "https://github.com/mungosthe-cpu/ObradaPodataka/releases"
GITHUB_LATEST_RELEASE_API = (
    "https://api.github.com/repos/mungosthe-cpu/ObradaPodataka/releases/latest"
)
# Imena fajlova na GitHub Releases — moraju odgovarati upload-u pri objavi verzije.
GITHUB_RELEASE_ASSET_FULL = "TaurisPdfApp_Setup.exe"
GITHUB_RELEASE_ASSET_LITE = "TaurisPdfApp_Lite_Setup.exe"
APP_EXPIRY_DATE = date(2027, 5, 1)
ACCESS_KEY_EXPECTED_SHA256 = "697d4c3cb2ff30a486a3a8b8e5886e760aeb681a23f2e0880bc6ef503781e841"
ACCESS_KEY_FILENAME = "tauris_access.key"
REQUIRED_RACE_FIELDS = ("race_id", "start_time", "duration_sec")
SUPPORTED_DELIVERY_MODES = {"none", "email"}
DEFAULT_TAURIS_MAIL_TO = "analizaletagolubova@gmail.com"
TAURIS_CLUB_EMAIL_FILENAME = "tauris_club_email.json"
CLUB_SMTP_HOST = "smtp.gmail.com"
CLUB_SMTP_PORT = 587
CLUB_SMTP_USER = "analizaletagolubova@gmail.com"
# Nikad u repou — samo tauris_club_email.json ili TAURIS_SMTP_* u okruženju.
CLUB_SMTP_PASSWORD = ""
CLUB_MAIL_FROM = "analizaletagolubova@gmail.com"
CLUB_MAIL_TO = ""
EMAIL_ZIP_MAX_ATTACHMENT_BYTES = 22 * 1024 * 1024
EMAIL_ZIP_BATCH_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
DEFAULT_TAURIS_DELIVERY_MODE = "email"
TAURIS_READ_CHUNK = 4096
TAURIS_READ_TIMEOUT_SEC = 0.13
TAURIS_READ_IDLE_EMPTY_READS = 22
TAURIS_READ_MAX_ITERATIONS = 128
TAURIS_CONNECT_MAX_ITERATIONS = 96
TAURIS_TAIL_IDLE_EMPTY_READS = 48
TAURIS_TAIL_EXTRA_MAX_ITER = 100
TAURIS_READ_TIMEOUT_SEC_SLOW = 0.25
TAURIS_READ_IDLE_EMPTY_READS_SLOW = 72
TAURIS_READ_MAX_ITERATIONS_SLOW = 320
TAURIS_TAIL_IDLE_EMPTY_READS_SLOW = 90
TAURIS_TAIL_EXTRA_MAX_ITER_SLOW = 220
TAURIS_CONNECT_MAX_ITERATIONS_SLOW = 200
# Otvaranje COM porta ponekad prolazno puca (Windows kratko drži port zauzet odmah posle
# priključenja USB-a) — jedan tihi ponovni pokušaj pre nego što se prijavi greška korisniku.
TAURIS_OPEN_RETRY_ATTEMPTS = 2
TAURIS_OPEN_RETRY_DELAY_SEC = 0.35

# BRICON (USB-CDC / virtuelni COM) — iste ideje kao TAURIS; često sporije slanje
BRICON_READ_TIMEOUT_SEC = 0.15
BRICON_READ_IDLE_EMPTY_READS = 28
BRICON_READ_MAX_ITERATIONS = 160
BRICON_TAIL_IDLE_EMPTY_READS = 55
BRICON_TAIL_EXTRA_MAX_ITER = 120
BRICON_READ_TIMEOUT_SEC_SLOW = 0.28
BRICON_READ_IDLE_EMPTY_READS_SLOW = 80
BRICON_READ_MAX_ITERATIONS_SLOW = 360
BRICON_TAIL_IDLE_EMPTY_READS_SLOW = 100
BRICON_TAIL_EXTRA_MAX_ITER_SLOW = 240
# USB uitlezen (Print Manager slave mode) — duže slušanje dok korisnik pokrene Read out na satu
BRICON_USB_READOUT_MAX_SEC = 180.0
# Baud vrednosti koje se probaju u pasivnom fallback rezimu (izabrani baud iz GUI-ja se dodaje ispred).
BRICON_USB_BAUD_CANDIDATES: tuple[int, ...] = (9600, 19200, 38400, 57600, 115200)
# Svaki baud dobija sopstveni deo ukupnog vremena, ali ne manje od ovoga — inace kratak
# max_listen_sec bi ostavio premalo vremena po baud-u da uredjaj stigne da posalje listu.
BRICON_USB_PER_BAUD_MIN_SEC = 12.0
# Donja granica ukupnog vremena za ceo pasivni fallback (svi baud-ovi zajedno).
BRICON_USB_READOUT_MIN_TOTAL_SEC = 30.0
BRICON_USB_DRIVER_URL = "https://www.bricon.be/download/software/Drivers/CDM_Setup.exe"
BRICON_USB_DRIVER_BUNDLED = True

CLOCK_DEVICE_TAURIS = "tauris"
CLOCK_DEVICE_BRICON = "bricon"
CLOCK_DEVICE_LABELS: tuple[str, str] = ("TAURIS", "BRICON")

# PDF izvoz — +20% u odnosu na prvobitne veličine (8/9.5/17/10 pt, red 8 mm)
PDF_FONT_SCALE = 1.2
PDF_LOG_FONT_SIZE_PT = 8.0 * PDF_FONT_SCALE
PDF_LOG_LEADING_PT = 9.5 * PDF_FONT_SCALE
PDF_REPORT_TITLE_FONT_PT = 17.0 * PDF_FONT_SCALE
PDF_REPORT_BODY_FONT_PT = 10.0 * PDF_FONT_SCALE
PDF_REPORT_ROW_HEIGHT_MM = 8.0 * PDF_FONT_SCALE


@dataclass
class TaurisConnectionConfig:
    port: str
    baudrate: int
    read_timeout: float = TAURIS_READ_TIMEOUT_SEC
    read_iterations: int = TAURIS_READ_MAX_ITERATIONS
    read_chunk: int = TAURIS_READ_CHUNK
    read_idle_empty_reads: int = TAURIS_READ_IDLE_EMPTY_READS
    tail_idle_empty_reads: int = TAURIS_TAIL_IDLE_EMPTY_READS
    tail_extra_max_iterations: int = TAURIS_TAIL_EXTRA_MAX_ITER


@dataclass
class DeliveryConfig:
    mode: str
    signing_key: str
    timeout_sec: float
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str
    mail_to: str
