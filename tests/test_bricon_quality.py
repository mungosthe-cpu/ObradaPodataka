"""Testovi za BRICON parser i prepoznavanje formata."""

from __future__ import annotations

import json

import pytest

from tauris_desktop import bricon as br


SAMPLE_BRICON_TEXT = """BRICON X-TREME
Fancier: Jan Janssen
Race: Oldbird 2024
001  BEL24-1234567  14:23:45.123  Ant:1
002  BEL24-7654321  14:25:01.456  Ant:2
003  BEL24-1111111  14:26:10.000  Ant:1
"""

SAMPLE_BRICON_CSV = """Position,Ring,Arrival,Date
1,BEL241234567,14:23:45,2024-06-15
2,BEL247654321,14:25:01,2024-06-15
"""

SAMPLE_TAURIS_SNIPPET = "TauRIS kontrol lista\n001.001.049.066  Dragan  0040 0001-0040\n0001 SRB 12:34:56\n"


def test_looks_like_bricon_and_not_tauris() -> None:
    assert br.looks_like_bricon_text(SAMPLE_BRICON_TEXT)
    assert not br.looks_like_bricon_text(SAMPLE_TAURIS_SNIPPET)
    assert br.looks_like_tauris_text(SAMPLE_TAURIS_SNIPPET)


def test_parse_bricon_clocking_rows() -> None:
    rows = br.parse_bricon_clocking_rows(SAMPLE_BRICON_TEXT)
    assert len(rows) == 3
    assert rows[0]["ring"] == "BEL24-1234567"
    assert rows[0]["arrival_time"] == "14:23:45.123"
    rows_csv = br.parse_bricon_clocking_rows(SAMPLE_BRICON_CSV)
    assert len(rows_csv) == 2
    assert "BEL241234567" in rows_csv[0]["ring"]


def test_extract_bricon_fancier() -> None:
    assert br.extract_bricon_fancier_name(SAMPLE_BRICON_TEXT) == "Jan Janssen"


def test_parse_bricon_json() -> None:
    payload = {
        "device": "bricon",
        "fancier": "Pera Perić",
        "clockings": [
            {"ring": "SRB24-9999999", "time": "10:11:12", "date": "2024-07-01"},
        ],
    }
    text = json.dumps(payload)
    data = br.parse_bricon_payload_to_race_dict(text)
    assert data["takmicar"] == "Pera Perić"
    assert len(data["bricon_clockings"]) == 1


def test_validate_bricon_rejects_tauris() -> None:
    with pytest.raises(RuntimeError, match="TAURIS"):
        br.validate_bricon_payload(SAMPLE_TAURIS_SNIPPET)


def test_validate_bricon_requires_content() -> None:
    with pytest.raises(RuntimeError):
        br.validate_bricon_payload("prazan ili nepoznat sadržaj bez redova")


def test_com_port_looks_like_bricon_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blob(port: str) -> str:
        if port == "COM3":
            return "bricon speedy usb serial"
        return "generic uart"

    monkeypatch.setattr(br, "com_port_description_blob", _blob)
    assert br.com_port_looks_like_bricon_usb("COM3")
    assert not br.com_port_looks_like_bricon_usb("COM9")


SAMPLE_SPEEDY_LISTA = """\
10/05/26-18:03:37
Bricon Speedy - C5397 V 007
lista doleta
C5397 KUSIC DRAGAN
1 C507F0C6 SR-23-0000464835 Z PLAV 10/05 12:04:35 :4B: 7: OK
2 C507F033 SR-23-0000464840 M FAKO 10/05 12:05:12 :A2: 1: OK
pakovanje : 16
"""


def test_bricon_payload_ready_speedy() -> None:
    assert br._bricon_payload_ready(SAMPLE_SPEEDY_LISTA, len(SAMPLE_SPEEDY_LISTA))


def test_bricon_speedy_lista_doleta_from_scan() -> None:
    assert br.is_bricon_speedy_lista_doleta(SAMPLE_SPEEDY_LISTA)
    assert br.extract_bricon_fancier_name(SAMPLE_SPEEDY_LISTA) == "KUSIC DRAGAN"
    rows = br.parse_bricon_speedy_arrival_rows(SAMPLE_SPEEDY_LISTA)
    assert len(rows) == 2
    assert rows[0]["chip_id"] == "C507F0C6"
    assert rows[0]["ring"] == "SR-23-0000464835"
    assert rows[0]["color"] == "PLAV"
    assert rows[0]["arrival_time"] == "12:04:35"
    assert rows[0]["arrival_date"] == "2026-05-10"
    assert rows[0]["status"] == "OK"
    data = br.parse_bricon_payload_to_race_dict(SAMPLE_SPEEDY_LISTA)
    assert data["takmicar"] == "KUSIC DRAGAN"
    assert data["bricon_device_id"] == "C5397"
    assert data["bricon_basket_count"] == 16
    assert len(data["bricon_clockings"]) == 2


def test_bricon_speedy_fixture_file() -> None:
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "bricon_speedy_lista_doleta_sample.txt"
    text = path.read_text(encoding="utf-8")
    rows = br.parse_bricon_speedy_arrival_rows(text)
    assert len(rows) == 3
    assert br.looks_like_bricon_text(text)
