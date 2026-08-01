#!/usr/bin/env python3
"""Analiza BRICON COM snimka (.log iz logs/bricon_capture/)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tauris_desktop.bricon_capture import analyze_capture_file


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("logfile", type=Path, help="putanja do bricon_COMx_*.log")
    args = p.parse_args()
    if not args.logfile.is_file():
        print(f"Fajl ne postoji: {args.logfile}", file=sys.stderr)
        return 1
    print(analyze_capture_file(args.logfile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
