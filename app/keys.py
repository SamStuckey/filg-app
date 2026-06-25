#!/usr/bin/env python3
"""
Encrypted per-user BYOK key storage.

BYOK runs happen server-side in a background thread, so the user's API key must be available at
run time — which means storing it. We store it **encrypted at rest** (Fernet/AES) and decrypt only
in the worker, right before building the provider client. The plaintext key is never logged, never
returned to the client, and never written anywhere but the encrypted column.

Security model:
  - `FILG_KEY_SECRET` is the encryption secret, held only in the server env (Render dashboard
    secret). Any non-empty random string works (e.g. Render's "Generate" button) — we hash it into a
    valid Fernet key (see `_secret`). Rotating it invalidates stored keys (users re-enter).
  - With no `FILG_KEY_SECRET`, BYOK is OFF (enabled() is False) and the endpoints 503 — same
    graceful-degrade shape as auth/billing.
  - Reads expose only metadata (provider + last4), never the secret.

Shares the app's SQLite file (`FILG_DB`); stdlib sqlite3 + the (already-present) cryptography dep.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FILG_DB") or os.path.join(_HERE, "filg.db")

# Providers we accept a key for. OpenRouter (one key fronts any model + cited web search) or a user's
# own Anthropic key (Claude direct). The provider is auto-detected from the key prefix at save time.
PROVIDERS = ("openrouter", "anthropic")

_init_lock = threading.Lock()
_initialized = False
_fernet = None


def _secret():
    """The Fernet cipher built from FILG_KEY_SECRET, or None if BYOK isn't configured.

    Accepts ANY non-empty secret (including Render's "Generate" button, which doesn't emit a
    Fernet-formatted key): we derive a valid 32-byte url-safe base64 Fernet key by hashing it.
    Deterministic, so the same secret always decrypts what it encrypted. Keep the secret stable —
    changing it makes previously stored keys undecryptable (users re-enter)."""
    global _fernet
    if _fernet is not None:
        return _fernet
    raw = os.environ.get("FILG_KEY_SECRET")
    if not raw:
        return None
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    raw_bytes = raw.encode() if isinstance(raw, str) else raw
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw_bytes).digest()))
    return _fernet


def enabled() -> bool:
    """True iff a Fernet secret is configured — gates the whole BYOK feature on the server side."""
    return _secret() is not None


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
        con = _connect()
        try:
            with con:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS user_keys ("
                    "  email TEXT PRIMARY KEY,"
                    "  provider TEXT NOT NULL,"
                    "  ciphertext BLOB NOT NULL,"   # Fernet token of the API key
                    "  last4 TEXT,"                 # for masked display (sk-…last4) — not a secret
                    "  created_at TEXT NOT NULL,"
                    "  updated_at TEXT NOT NULL)")
        finally:
            con.close()
        _initialized = True


def save_key(email: str, provider: str, api_key: str) -> dict:
    """Encrypt and store (or replace) a user's key. Returns the masked metadata. Raises ValueError
    on a bad provider/key/missing-secret so the caller can 400."""
    f = _secret()
    if f is None:
        raise ValueError("BYOK is not configured on the server.")
    email = (email or "").strip().lower()
    api_key = (api_key or "").strip()
    if "@" not in email:
        raise ValueError("A signed-in email is required to save a key.")
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider {provider!r}.")
    if len(api_key) < 8:
        raise ValueError("That doesn't look like an API key.")
    init()
    token = f.encrypt(api_key.encode())
    last4 = api_key[-4:]
    now = datetime.now(timezone.utc).isoformat()
    con = _connect()
    try:
        with con:
            con.execute(
                "INSERT INTO user_keys (email, provider, ciphertext, last4, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET "
                "  provider=excluded.provider, ciphertext=excluded.ciphertext,"
                "  last4=excluded.last4, updated_at=excluded.updated_at",
                (email, provider, token, last4, now, now))
    finally:
        con.close()
    return {"provider": provider, "last4": last4}


def get_key(email: str) -> str | None:
    """Decrypt and return the plaintext key for a run. WORKER-ONLY — never send this to a client,
    never log it. Returns None if there's no key or BYOK isn't configured."""
    f = _secret()
    if f is None:
        return None
    init()
    con = _connect()
    try:
        row = con.execute("SELECT ciphertext FROM user_keys WHERE email=?",
                          ((email or "").strip().lower(),)).fetchone()
    finally:
        con.close()
    if not row:
        return None
    try:
        return f.decrypt(row["ciphertext"]).decode()
    except Exception:  # noqa: BLE001 — bad/rotated secret → treat as no key, force re-entry
        return None


def key_meta(email: str) -> dict | None:
    """Masked metadata for display (provider + last4). Never touches the ciphertext."""
    init()
    con = _connect()
    try:
        row = con.execute("SELECT provider, last4 FROM user_keys WHERE email=?",
                          ((email or "").strip().lower(),)).fetchone()
    finally:
        con.close()
    return {"provider": row["provider"], "last4": row["last4"]} if row else None


def has_key(email: str) -> bool:
    return key_meta(email) is not None


def delete_key(email: str) -> None:
    init()
    con = _connect()
    try:
        with con:
            con.execute("DELETE FROM user_keys WHERE email=?", ((email or "").strip().lower(),))
    finally:
        con.close()


if __name__ == "__main__":  # self-test (no API; generates an ephemeral secret)
    import tempfile
    from cryptography.fernet import Fernet
    DB = tempfile.mktemp(suffix=".db")
    os.environ["FILG_KEY_SECRET"] = Fernet.generate_key().decode()
    _fernet = None  # rebuild with the test secret
    assert enabled()
    meta = save_key("U@X.com", "openrouter", "sk-or-v1-abcd1234efgh")
    assert meta == {"provider": "openrouter", "last4": "efgh"}
    assert get_key("u@x.com") == "sk-or-v1-abcd1234efgh"     # case-insensitive email, round-trips
    assert key_meta("u@x.com") == {"provider": "openrouter", "last4": "efgh"}
    assert has_key("u@x.com") and not has_key("nobody@x.com")
    # ciphertext must NOT contain the plaintext
    con = _connect()
    blob = con.execute("SELECT ciphertext FROM user_keys WHERE email='u@x.com'").fetchone()["ciphertext"]
    con.close()
    assert b"sk-or-v1-abcd1234efgh" not in blob, "key stored in plaintext!"
    save_key("u@x.com", "openrouter", "sk-or-v1-NEWKEY99zzzz")  # replace
    assert get_key("u@x.com").endswith("zzzz")
    delete_key("u@x.com")
    assert not has_key("u@x.com") and get_key("u@x.com") is None
    # bad inputs
    for bad in (lambda: save_key("noemail", "openrouter", "sk-or-xyzlong"),
                lambda: save_key("u@x.com", "openai", "sk-xyzlong"),
                lambda: save_key("u@x.com", "openrouter", "x")):
        try:
            bad(); raise AssertionError("expected ValueError")
        except ValueError:
            pass
    print("keys.py self-test OK — encrypted at rest, masked reads, replace/delete")
