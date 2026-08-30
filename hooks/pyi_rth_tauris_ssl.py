# PyInstaller runtime hook: OpenSSL u frozen .exe ne vidi Windows CA store.
# Postavi SSL_CERT_FILE na izdvojeni certifi/cacert.pem pre bilo kog HTTPS poziva.
import os
import sys


def _set_ssl_cert_file() -> None:
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "certifi", "cacert.pem"))
        candidates.append(os.path.join(meipass, "cacert.pem"))
    try:
        import certifi

        candidates.insert(0, certifi.where())
    except Exception:
        pass
    for path in candidates:
        if path and os.path.isfile(path):
            os.environ["SSL_CERT_FILE"] = path
            os.environ["REQUESTS_CA_BUNDLE"] = path
            return


_set_ssl_cert_file()
