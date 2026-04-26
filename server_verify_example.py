from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request


app = Flask(__name__)
UPLOAD_DIR = Path("server_uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def verify_protected_payload(payload: dict[str, Any], signing_key: str) -> tuple[bool, str]:
    if "race_data" not in payload or "data_hash" not in payload or "signature_hmac_sha256" not in payload:
        return False, "Nedostaju polja race_data/data_hash/signature_hmac_sha256."

    race_data = payload["race_data"]
    declared_hash = str(payload["data_hash"])
    declared_signature = str(payload["signature_hmac_sha256"])

    canonical = json.dumps(race_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    real_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(real_hash, declared_hash):
        return False, "Hash se ne poklapa - podaci su izmenjeni."

    expected_signature = hmac.new(
        signing_key.encode("utf-8"),
        real_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, declared_signature):
        return False, "HMAC potpis nije validan."

    return True, "Validan potpis i neizmenjeni podaci."


@app.post("/api/upload")
def upload_results():
    signing_key = os.getenv("TAURIS_SIGNING_KEY", "CHANGE_ME_SECRET")
    if signing_key == "CHANGE_ME_SECRET":
        return jsonify({"ok": False, "error": "Podesite TAURIS_SIGNING_KEY na serveru."}), 500

    payload_file = request.files.get("payload")
    json_file = request.files.get("json_file")
    pdf_file = request.files.get("pdf_file")
    if not payload_file or not json_file or not pdf_file:
        return jsonify({"ok": False, "error": "Nedostaju fajlovi payload/json_file/pdf_file."}), 400

    try:
        payload = json.loads(payload_file.read().decode("utf-8"))
    except Exception:
        return jsonify({"ok": False, "error": "Payload nije validan JSON."}), 400

    valid, reason = verify_protected_payload(payload, signing_key)
    if not valid:
        return jsonify({"ok": False, "error": reason}), 400

    safe_name = payload.get("meta", {}).get("captured_at", "unknown").replace(":", "-")
    base = f"{safe_name}_{payload.get('meta', {}).get('port', 'PORT')}"
    json_target = UPLOAD_DIR / f"{base}.json"
    pdf_target = UPLOAD_DIR / f"{base}.pdf"
    json_target.write_bytes(json_file.read())
    pdf_target.write_bytes(pdf_file.read())

    return jsonify(
        {
            "ok": True,
            "message": reason,
            "json_saved": str(json_target),
            "pdf_saved": str(pdf_target),
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
