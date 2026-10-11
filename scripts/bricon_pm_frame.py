#!/usr/bin/env python3
"""
CLI proba BRICON USB čitanja (Print Manager protokol) — bez GUI-ja.

    .venv\\Scripts\\python.exe scripts\\bricon_pm_frame.py COM7 [--seconds 30] [--save lista.txt]

Radi isto što i dugme «Preuzmi BRICON»: otvori port 9600 8N2, pronađe sat
(ActivateMaster/ActivateBA), pročita takmičara, golubove i vremena i ispiše listu.
Sa ``TAURIS_BRICON_CAPTURE=1`` u okruženju snima i ceo COM saobraćaj u logs/bricon_capture/.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tauris_desktop.bricon_pm import (  # noqa: E402
    PM_CMD_ACTIVATE_MASTER,
    cmd_activate_ba,
    pm_encode_frame,
    read_readout_via_print_manager_protocol,
    readout_to_structured,
    render_lista_doleta,
)
from tauris_desktop.lib import setup_logging  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="BRICON USB (Print Manager protokol) — proba čitanja")
    p.add_argument("port", help="npr. COM7")
    p.add_argument("--seconds", type=float, default=30.0, help="koliko dugo tražiti sat (default 30)")
    p.add_argument("--save", help="snimi renderovanu listu u fajl")
    p.add_argument("--json", help="snimi strukturirane podatke (JSON) u fajl")
    args = p.parse_args()

    setup_logging()
    print("Okvir ActivateMaster na žici:", pm_encode_frame(PM_CMD_ACTIVATE_MASTER).hex(" "))
    print("Okvir ActivateBA(0x82)      :", pm_encode_frame(cmd_activate_ba(0x82)).hex(" "))
    print(f"Tražim sat na {args.port.upper()} (9600 8N2) do {args.seconds:.0f}s… Print Manager mora biti zatvoren.\n")

    ro = read_readout_via_print_manager_protocol(
        args.port.upper(), listen_sec=args.seconds, on_phase=lambda m: print("  ·", m)
    )
    text = render_lista_doleta(ro)
    print("\n" + text)
    if args.save:
        Path(args.save).write_text(text, encoding="utf-8")
        print("Lista snimljena:", args.save)
    if args.json:
        import json

        Path(args.json).write_text(
            json.dumps(readout_to_structured(ro), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("JSON snimljen:", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
