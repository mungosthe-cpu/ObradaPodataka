"""
TAURIS dijagnostika — čuva sirove/već pročitane podatke kada nešto ne uspe do kraja.

Namerno odvojeno od bricon_capture.py (TAURIS i BRICON imaju odvojenu logiku; objedinjavanje
ima smisla samo kada ponašanje mora biti identično). Format snimka je isti kao kod BRICON-a
jer rešava isti problem (terenska dijagnostika + oporavak već pročitanih podataka), ali je
namerno u zasebnom fajlu/folderu da izmena kod jednog uređaja ne može slučajno pokvariti
dijagnostiku drugog.

Nikad ne upisuje lozinke, ključeve ili email kredencijale — samo sirove bajtove primljene sa
serijskog porta i već parsirani race_data rečnik (koji ne sadrži tajne).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tauris_desktop.lib import app_data_dir


def tauris_capture_dir() -> Path:
    d = app_data_dir() / "logs" / "tauris_capture"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_port_tag(port: str) -> str:
    return re.sub(r"[^\w\-]+", "_", (port or "?").strip().upper())


def _hex_and_ascii(raw: bytes, limit: int = 512) -> tuple[str, str]:
    data = raw[:limit]
    hex_line = " ".join(f"{b:02x}" for b in data)
    ascii_line = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return hex_line, ascii_line


def save_tauris_partial_payload_diagnostic(port: str, raw: bytes, *, reason: str = "") -> Path:
    """
    Snima primljene bajtove kada TAURIS prenos ne uspe do kraja (prekid veze, nepotpuna
    lista, neuspešno parsiranje) — terenska dijagnostika uvek ima trag onoga što je stiglo
    sa uređaja, čak i kad korisnik nije prijavio tačan problem.
    """
    safe = _safe_port_tag(port)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = tauris_capture_dir() / f"tauris_partial_{safe}_{stamp}.log"
    hex_line, ascii_line = _hex_and_ascii(raw)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"# TAURIS parcijalni podaci — port {port}\n"
            f"# Vreme: {datetime.now().isoformat(timespec='seconds')}\n"
            f"# Razlog: {reason}\n"
            f"# Dužina: {len(raw)} B\n"
            f"# Format: hex | ascii (prvih {min(len(raw), 512)} B)\n"
        )
        fh.write(hex_line + "\n")
        fh.write(f"ASCII: {ascii_line}\n")
    logging.info("TAURIS parcijalni podaci sačuvani: %s (%s B)", path, len(raw))
    return path


def save_tauris_race_data_recovery_snapshot(
    race_data: dict[str, Any], *, port: str, reason: str = ""
) -> Path:
    """
    Snima već pročitane TAURIS podatke (race_data) kao JSON ako sledeći korak posle
    uspešnog čitanja (PDF/zaključavanje/slanje) padne — preuzimanje sa uređaja često
    zahteva da korisnik ponovo pokrene slanje na satu, pa se već primljena lista ne sme
    izgubiti samo zato što je kasniji korak u obradi pukao.
    """
    safe = _safe_port_tag(port)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = tauris_capture_dir() / f"tauris_racedata_recovery_{safe}_{stamp}.json"
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
    logging.info("TAURIS race_data recovery snimak sačuvan: %s", path)
    return path
