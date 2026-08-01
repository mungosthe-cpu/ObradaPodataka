#!/usr/bin/env python3
"""CLI: snimi BRICON COM saobracaj (zatvorite Print Manager pre pokretanja)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tauris_desktop.bricon_capture import analyze_capture_file, run_com_capture_session
from tauris_desktop.lib import setup_logging


def main() -> int:
    p = argparse.ArgumentParser(description="Snimi BRICON COM (PM protokol + slusanje)")
    p.add_argument("port", help="npr. COM3")
    p.add_argument("--seconds", type=float, default=180.0, help="trajanje slusanja (default 180)")
    p.add_argument(
        "--no-pm-protocol",
        action="store_true",
        help="samo pasivno slusanje bez PM handshake",
    )
    p.add_argument("--analyze", action="store_true", help="ispisi analizu posle snimanja")
    args = p.parse_args()

    setup_logging()
    print(f"Snimam {args.port} (~{args.seconds:.0f}s). Zatvorite Print Manager.")
    print("Posle poruke na ekranu pokrenite Read out na satu.\n")

    path, summary = run_com_capture_session(
        args.port.upper(),
        duration_sec=args.seconds,
        use_pm_protocol=not args.no_pm_protocol,
    )
    print(f"\nSacuvano: {path}")
    print(summary)
    if args.analyze:
        print("\n--- Analiza ---\n")
        print(analyze_capture_file(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
