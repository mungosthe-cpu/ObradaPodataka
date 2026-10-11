"""
Red neposlatih paketa (.taurislock) — kad na terenu nema interneta.

Ranije: svaki neuspeo mejl je ostavljao ERROR + traceback u logu, a korisnik je morao
ručno da pošalje pakete iz Baze. Sada se paket upiše u ``pending_sends.json`` (u folderu
aplikacije), a program ga sam pošalje kad veza bude dostupna — pri sledećem startu,
posle svakog uspešnog preuzimanja, ili na klik Baza → «Pošalji neposlate pakete».

Fajl je prost JSON (lista zapisa) da bi se po potrebi mogao i ručno pregledati/ispraviti.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tauris_desktop.config import DeliveryConfig

PENDING_SENDS_FILENAME = "pending_sends.json"
PENDING_MAX_ATTEMPTS_PER_RUN = 20

_LOCK = threading.Lock()


@dataclass
class PendingSend:
    bundle_path: str
    broj_trke: str = ""
    grupacija: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    last_error: str = ""
    last_attempt_at: str = ""

    @property
    def path(self) -> Path:
        return Path(self.bundle_path)


def pending_sends_path() -> Path:
    from tauris_desktop.lib import app_data_dir

    return app_data_dir() / PENDING_SENDS_FILENAME


def load_pending_sends() -> list[PendingSend]:
    p = pending_sends_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Red neposlatih paketa je neispravan — ignorišem: %s", p)
        return []
    out: list[PendingSend] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict) or not item.get("bundle_path"):
                continue
            known = {k: item.get(k) for k in PendingSend.__dataclass_fields__ if k in item}
            try:
                out.append(PendingSend(**known))  # type: ignore[arg-type]
            except TypeError:
                continue
    return out


def save_pending_sends(items: list[PendingSend]) -> None:
    p = pending_sends_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logging.exception("Upis reda neposlatih paketa nije uspeo: %s", p)


def enqueue_pending_send(
    bundle_path: Path, *, broj_trke: str = "", grupacija: str = "", error: str = ""
) -> int:
    """Dodaje paket u red (bez duplikata). Vraća ukupan broj paketa u redu."""
    with _LOCK:
        items = load_pending_sends()
        key = str(Path(bundle_path).resolve())
        for it in items:
            if str(Path(it.bundle_path).resolve()) == key:
                it.attempts += 1
                it.last_error = error[:300]
                it.last_attempt_at = datetime.now().isoformat(timespec="seconds")
                save_pending_sends(items)
                return len(items)
        items.append(
            PendingSend(
                bundle_path=key,
                broj_trke=broj_trke,
                grupacija=grupacija,
                attempts=1,
                last_error=error[:300],
                last_attempt_at=datetime.now().isoformat(timespec="seconds"),
            )
        )
        save_pending_sends(items)
        logging.warning(
            "Paket stavljen u red za naknadno slanje (%s u redu): %s", len(items), bundle_path
        )
        return len(items)


def pending_count() -> int:
    return len(load_pending_sends())


def _default_sender(item: PendingSend, config: DeliveryConfig) -> str:
    from tauris_desktop.lib import _staza_labels_for_folder_email, send_email_folder_file_batch

    bundle = item.path
    bt, gr = _staza_labels_for_folder_email(bundle.parent, item.broj_trke, item.grupacija)
    return send_email_folder_file_batch(
        [bundle], broj_trke=bt, grupacija=gr, part=1, total_parts=1, config=config
    )


def retry_pending_sends(
    config: DeliveryConfig,
    *,
    sender: Callable[[PendingSend, DeliveryConfig], str] | None = None,
    stop_on_network_error: bool = True,
) -> tuple[int, int, list[str]]:
    """
    Pokušaj slanja svih paketa iz reda. Vraća (poslato, preostalo, poruke).
    Paketi čiji fajl više ne postoji se izbacuju iz reda uz poruku.
    Na prvu mrežnu grešku (nema interneta) prekida — nema smisla probati ostale.
    """
    from tauris_desktop.lib import _is_network_unreachable_error, smtp_settings_complete

    send = sender or _default_sender
    with _LOCK:
        items = load_pending_sends()
        if not items:
            return 0, 0, []
        if not smtp_settings_complete(config):
            return 0, len(items), ["Podešavanja slanja nisu kompletna — paketi ostaju u redu."]
        notes: list[str] = []
        sent = 0
        remaining: list[PendingSend] = []
        attempts = 0
        network_down = False
        for it in items:
            if network_down or attempts >= PENDING_MAX_ATTEMPTS_PER_RUN:
                remaining.append(it)
                continue
            if not it.path.is_file():
                notes.append(f"Izbačen iz reda (fajl ne postoji): {it.bundle_path}")
                continue
            attempts += 1
            try:
                note = send(it, config)
                sent += 1
                notes.append(f"Poslato: {it.path.name} — {note}")
                logging.info("Naknadno poslat paket iz reda: %s", it.bundle_path)
            except Exception as exc:
                it.attempts += 1
                it.last_error = str(exc)[:300]
                it.last_attempt_at = datetime.now().isoformat(timespec="seconds")
                remaining.append(it)
                if _is_network_unreachable_error(exc):
                    network_down = True
                    notes.append("Nema internet veze — ostali paketi ostaju u redu.")
                    logging.warning("Naknadno slanje: nema mreže, %s paketa čeka.", len(items) - sent)
                    if not stop_on_network_error:
                        network_down = False
                else:
                    notes.append(f"Nije poslato ({it.path.name}): {exc}")
                    logging.warning("Naknadno slanje nije uspelo za %s: %s", it.bundle_path, exc)
        save_pending_sends(remaining)
        return sent, len(remaining), notes


def summarize_pending_for_user(items: list[PendingSend] | None = None) -> str:
    items = load_pending_sends() if items is None else items
    if not items:
        return "Nema neposlatih paketa."
    lines = [f"Neposlatih paketa: {len(items)}"]
    for it in items[:15]:
        lines.append(f"• {it.path.name}  (pokušaja: {it.attempts}, poslednja greška: {it.last_error or '-'})")
    if len(items) > 15:
        lines.append(f"… i još {len(items) - 15}")
    return "\n".join(lines)


def json_safe(obj: Any) -> Any:  # pragma: no cover — pomoćno za debug
    return json.loads(json.dumps(obj, default=str))
