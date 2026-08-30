"""Regresioni testovi za parsere COM teksta i .taurislock paket."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

import main
import tauris_desktop.lib as tdl


def test_extract_takmicar_from_kontrol_list() -> None:
    sample = (
        "001.001.049.066  Dragan Todorovic      0040 0001-0040\n"
        "0001 SRB 12:34:56\n"
    )
    assert main.extract_takmicar_from_tauris_kontrol_list(sample) == "Dragan Todorovic"


def test_parse_tauris_expected_last_lag() -> None:
    sample = "001.001.049.066  Ime Prezime      0040 0001-0040\n"
    assert main.parse_tauris_expected_last_lag(sample) == 40


def test_extract_takmicar_name_takmicar_line() -> None:
    raw = "Takmičar: Marko Marković , 12345\n0001 SRB\n"
    assert main.extract_takmicar_name_from_raw_text(raw) == "Marko Marković"


def test_extract_takmicar_from_json_dict() -> None:
    j = json.dumps({"takmicar": "  Ana Anić  "}, ensure_ascii=False)
    assert main.extract_takmicar_name_from_raw_text(j) == "Ana Anić"


def test_tauris_text_has_lag_row() -> None:
    text = "0039 SRB x\n0040 SRB y\n"
    assert main.tauris_text_has_lag_row(text, 40) is True
    assert main.tauris_text_has_lag_row(text, 41) is False


def test_decode_com_payload_prefers_tauris_keywords() -> None:
    raw = "TauRIS kontrol lista\r\n0001 SRB".encode("cp1250")
    out = main.decode_com_payload(raw, quiet=True)
    assert "TauRIS" in out or "kontrol" in out.lower()


def test_taurislock_roundtrip(tmp_path: Path) -> None:
    j = tmp_path / "race.json"
    p = tmp_path / "race.pdf"
    t = tmp_path / "race.txt"
    j.write_text('{"x": 1}', encoding="utf-8")
    p.write_bytes(b"%PDF-1.4 test")
    t.write_text("com", encoding="utf-8")
    secret = "moja_tajna_za_test"
    lock = main.create_locked_results_bundle(j, p, t, secret)
    assert lock.suffix == ".taurislock"
    assert lock.read_bytes().startswith(b"TAURISLOCKv2")
    out_dir = tmp_path / "out"
    paths = main.unlock_results_bundle(lock, secret, out_dir)
    names = {x.name for x in paths}
    assert "race.json" in names
    assert "race.pdf" in names
    assert "race.txt" in names
    assert (out_dir / "race.pdf").read_bytes() == b"%PDF-1.4 test"


def test_taurislock_legacy_v1_still_unlocks(tmp_path: Path) -> None:
    payload = {
        "created_at": "2026-01-01T12:00:00",
        "files": [
            {
                "name": "legacy.json",
                "data_b64": base64.b64encode(b'{"legacy": true}').decode("ascii"),
            }
        ],
    }
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    password = "stara_sifra"
    salt = b"legacy-v1-salt!!"
    key_material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000, dklen=64)
    enc_key = key_material[:32]
    mac_key = key_material[32:]
    cipher = tdl._xor_stream_encrypt(plain, enc_key)
    tag = hmac.new(mac_key, cipher, hashlib.sha256).digest()
    lock = tmp_path / "legacy.taurislock"
    lock.write_bytes(b"TAURISLOCKv1" + salt + tag + cipher)

    out = main.unlock_results_bundle(lock, password, tmp_path / "out")

    assert {p.name for p in out} == {"legacy.json"}
    assert (tmp_path / "out" / "legacy.json").read_text(encoding="utf-8") == '{"legacy": true}'


def test_taurislock_wrong_password(tmp_path: Path) -> None:
    j = tmp_path / "a.json"
    p = tmp_path / "a.pdf"
    t = tmp_path / "a.txt"
    j.write_text("{}", encoding="utf-8")
    p.write_bytes(b"x")
    t.write_text("y", encoding="utf-8")
    lock = main.create_locked_results_bundle(j, p, t, "good")
    with pytest.raises(ValueError, match="Pogrešna"):
        main.unlock_results_bundle(lock, "bad", tmp_path / "o")


def test_resolve_pilot_prefers_fresh_com_over_stale_ui() -> None:
    race = {
        "race_id": "T-1",
        "start_time": "2026-01-01T12:00:00",
        "duration_sec": 0,
        "raw_com_text": "001.001.049.066  Marko Markovic      0040 0001-0040\n",
    }
    assert tdl.resolve_pilot_name_for_output(race, "Dragan Todorovic") == "Marko Markovic"


def test_extract_takmicar_from_race_data() -> None:
    race = {
        "takmicar": "  Ana Anić  ",
        "raw_com_text": "Takmičar: Drugi Ime\n",
    }
    assert tdl.extract_takmicar_from_race_data(race) == "Ana Anić"


def test_obrada_email_subject_format() -> None:
    assert tdl._obrada_email_subject("001", "Grupacija 2") == "001-Grupacija_2-KOMPLET"
    assert (
        tdl._obrada_email_subject("001", "Grupacija 2", part=2, total_parts=3)
        == "001-Grupacija_2-KOMPLET (2/3)"
    )


def test_parse_staza_from_results_folder_name() -> None:
    assert tdl._parse_staza_from_results_folder_name("001-Grupacija_2") == (
        "001",
        "Grupacija_2",
    )


def test_run_pipeline_requires_lock_password(sample_race_dict: dict) -> None:
    with pytest.raises(ValueError, match="lozinka"):
        main.run_pipeline(
            sample_race_dict,
            "Pilot",
            "COM1",
            main.DeliveryConfig(
                mode="none",
                signing_key="k",
                timeout_sec=5.0,
                smtp_host="",
                smtp_port=587,
                smtp_user="",
                smtp_password="",
                mail_from="",
                mail_to="",
            ),
            lock_password="",
        )


def test_compare_version_strings() -> None:
    assert tdl.compare_version_strings("1.3", "1.1") == 1
    assert tdl.compare_version_strings("1.1", "1.3") == -1
    assert tdl.compare_version_strings("v1.4", "1.4") == 0
    assert tdl.compare_version_strings("1.3", "1.4") == -1


def test_format_github_update_error_dns() -> None:
    import socket
    import urllib.error

    err = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
    msg = tdl.format_github_update_error(err)
    assert "internet" in msg.lower() or "dns" in msg.lower()
    assert "github.com" in msg.lower() or "releases" in msg.lower()


def test_automatic_update_installer_target() -> None:
    path = tdl.automatic_update_installer_target("1.5", "TaurisPdfApp_Lite_Setup.exe")
    assert path.name == "1.5_TaurisPdfApp_Lite_Setup.exe"
    assert path.parent.name == "TaurisPdfApp_update"


def test_pick_github_release_installer_asset() -> None:
    assets = [
        {"name": "Source code.zip", "browser_download_url": "https://example.com/a.zip"},
        {
            "name": "TaurisPdfApp_Lite_Setup.exe",
            "browser_download_url": "https://example.com/lite.exe",
        },
        {
            "name": "TaurisPdfApp_Setup.exe",
            "browser_download_url": "https://example.com/full.exe",
        },
    ]
    lite = tdl.pick_github_release_installer_asset(assets, lite=True)
    assert lite is not None
    assert lite["name"] == "TaurisPdfApp_Lite_Setup.exe"
    full = tdl.pick_github_release_installer_asset(assets, lite=False)
    assert full is not None
    assert full["name"] == "TaurisPdfApp_Setup.exe"

    versioned = tdl.pick_github_release_installer_asset(
        [
            {
                "name": "TaurisPdfApp_Lite_Setup.1.7.exe",
                "browser_download_url": "https://example.com/v.exe",
            }
        ],
        lite=True,
    )
    assert versioned is not None
    assert versioned["name"] == "TaurisPdfApp_Lite_Setup.1.7.exe"


def test_format_github_update_error_ssl() -> None:
    import ssl
    import urllib.error

    msg = tdl.format_github_update_error(
        ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )
    assert "HTTPS sertifikat" in msg
    assert "GitHub" in msg

    # URLError sa string reason (često na Windows / urllib) ne sme da padne
    wrapped = urllib.error.URLError(
        "<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "unable to get local issuer certificate (_ssl.c:1081)>"
    )
    msg2 = tdl.format_github_update_error(wrapped)
    assert "HTTPS sertifikat" in msg2
    assert "releases" in msg2.lower()


def test_resolve_ca_bundle_path_finds_certifi() -> None:
    path = tdl.resolve_ca_bundle_path()
    assert path is not None
    assert path.lower().endswith("cacert.pem")
    assert Path(path).is_file()


def test_https_ssl_context_loads() -> None:
    ctx = tdl.https_ssl_context()
    assert ctx is not None
    assert ctx.verify_mode != 0


@pytest.fixture
def sample_race_dict() -> dict:
    return {
        "race_id": "T-1",
        "start_time": "2026-01-01T12:00:00",
        "duration_sec": 3600,
        "raw_com_text": "Takmičar: X\n",
    }
