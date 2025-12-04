import hashlib
import hmac
import os
from typing import Optional

SECRET_PATH = "/app/.internal/secret.bin"


def load_shared_secret() -> str:
    """
    Load the shared HMAC secret from the internal secret file.
    Exits immediately if the secret is missing or empty.
    """
    try:
        with open(SECRET_PATH, "r", encoding="utf-8") as f:
            secret = f.read().strip()
    except FileNotFoundError:
        raise SystemExit("Fatal: SHARED_SECRET missing. Provide it at build time using Docker --build-arg.")
    if not secret:
        raise SystemExit("Fatal: SHARED_SECRET missing. Provide it at build time using Docker --build-arg.")
    return secret


def compute_hmac(secret: str, message: str) -> str:
    """Return an HMAC-SHA256 hex digest."""
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def build_registration_signature(secret: str, device_name: str, nonce: str) -> str:
    """Build the registration signature using device_name + nonce."""
    return compute_hmac(secret, f"{device_name}{nonce}")


def build_ingest_signature(secret: str, device_id: str, timestamp_iso: str) -> str:
    """Build the ingest signature using device_id + timestamp."""
    return compute_hmac(secret, f"{device_id}{timestamp_iso}")
