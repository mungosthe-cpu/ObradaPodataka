"""
Ulazna tačka (PyInstaller, ``python main.py``) i kompatibilnost sa ``import main`` u testovima.
Implementacija je u paketu ``tauris_desktop``.
"""

from __future__ import annotations

from tauris_desktop.lib import *
from tauris_desktop.gui import TaurisApp, main


if __name__ == "__main__":
    raise SystemExit(main())
