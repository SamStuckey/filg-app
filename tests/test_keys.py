"""BYOK key storage + endpoints.

Storage encryption is covered by keys.py's own self-test; here we cover the API contract the
frontend relies on (feature gating, sign-in requirement) and one encrypt/round-trip via the module
the app actually imports (app.keys)."""

from cryptography.fernet import Fernet

from app import keys


# ── module: encrypt at rest + masked reads ────────────────────────────────────
def test_keys_encrypt_round_trip(monkeypatch):
    monkeypatch.setenv("FILG_KEY_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(keys, "_fernet", None)        # rebuild cipher from the test secret
    monkeypatch.setattr(keys, "_initialized", False)  # re-init the (test) table
    assert keys.enabled()
    meta = keys.save_key("Person@X.com", "openrouter", "sk-or-v1-secret-tail")
    assert meta == {"provider": "openrouter", "last4": "tail"}
    assert keys.get_key("person@x.com") == "sk-or-v1-secret-tail"   # case-insensitive
    assert keys.key_meta("person@x.com")["last4"] == "tail"
    keys.delete_key("person@x.com")
    assert not keys.has_key("person@x.com")


def test_keys_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("FILG_KEY_SECRET", raising=False)
    monkeypatch.setattr(keys, "_fernet", None)
    assert not keys.enabled()
    assert keys.get_key("x@y.com") is None   # no secret → no decryption, ever


# ── API: feature gating + sign-in requirement ─────────────────────────────────
def test_key_status_disabled_by_default(client):
    r = client.get("/api/key")
    assert r.status_code == 200 and r.json() == {"enabled": False}


def test_key_save_503_when_byok_unconfigured(client):
    r = client.post("/api/key", json={"provider": "openrouter", "key": "sk-or-12345678"})
    assert r.status_code == 503


def test_key_save_requires_signin_when_enabled(client, monkeypatch):
    monkeypatch.setattr(keys, "enabled", lambda: True)   # pretend BYOK is configured
    r = client.post("/api/key", json={"provider": "openrouter", "key": "sk-or-12345678"})
    assert r.status_code == 401   # auth is off in tests → no user → must sign in first


def test_key_remove_requires_signin(client):
    r = client.post("/api/key/remove", json={})
    assert r.status_code == 401
