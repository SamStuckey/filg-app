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


# ── phase 3: gating + metering routing ────────────────────────────────────────
from app import main  # noqa: E402

_IDEA = {"idea": "a real idea about coaching small dental practices", "email": "x@y.com"}


def test_start_walls_without_hosted_key(client, monkeypatch):
    # BYOK on, no user key, and NO hosted FILG key → must bring a key from the very first submit.
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: False)
    monkeypatch.setattr(main, "HOSTED_FREE", False)
    r = client.post("/api/plan/start", json=_IDEA)
    assert r.status_code == 402 and r.json()["needKey"] is True


def test_first_query_free_on_hosted_key(client, monkeypatch):
    # BYOK on, no user key, hosted FILG key present, free taste available → the first query is on us.
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: False)
    monkeypatch.setattr(main, "HOSTED_FREE", True)
    monkeypatch.setattr(main.usage, "can_run", lambda *a, **k: (True, "ok"))
    r = client.post("/api/plan/start", json=_IDEA)
    assert r.status_code == 200 and "id" in r.json()


def test_free_taste_used_degrades_to_needkey(client, monkeypatch):
    # Free taste used up (or the daily pool tapped) → degrade to a key prompt, not a dead end.
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: False)
    monkeypatch.setattr(main, "HOSTED_FREE", True)
    monkeypatch.setattr(main.usage, "can_run", lambda *a, **k: (False, "free limit reached"))
    r = client.post("/api/plan/start", json=_IDEA)
    assert r.status_code == 402 and r.json()["needKey"] is True


def test_byok_user_has_no_cap(client, monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: True)            # user has a saved key
    r = client.post("/api/plan/start", json=_IDEA)
    assert r.status_code == 200 and "id" in r.json()                     # key holders are unlimited


def test_start_legacy_when_byok_off(client, monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: False)             # no FILG_KEY_SECRET (dev)
    monkeypatch.setattr(main.usage, "can_run", lambda *a, **k: (False, "free limit reached"))
    r = client.post("/api/plan/start", json=_IDEA)
    assert r.status_code == 402 and "limit" in r.json()["error"].lower()  # legacy dev free-cap preserved


# ── the section wall: no free API actions past the welcome ─────────────────────
def test_key_wall_blocks_without_key(monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: False)
    resp = main._key_wall({"user": "x@y.com"})
    assert resp is not None and resp.status_code == 402


def test_key_wall_passes_with_key(monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    monkeypatch.setattr(main.keys, "has_key", lambda u: True)
    assert main._key_wall({"user": "x@y.com"}) is None


def test_key_wall_off_when_byok_disabled(monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: False)
    assert main._key_wall({"user": "x@y.com"}) is None


def test_next_is_walled_without_key(client, monkeypatch):
    monkeypatch.setattr(main.keys, "enabled", lambda: True)
    has = {"v": True}
    monkeypatch.setattr(main.keys, "has_key", lambda u: has["v"])
    sid = client.post("/api/plan/start", json=_IDEA).json()["id"]   # create the plan as a key holder
    has["v"] = False                                                # key gone → wall closes
    r = client.post("/api/plan/" + sid + "/next", json={})          # building needs a key
    assert r.status_code == 402 and r.json().get("needKey") is True


def test_meter_skips_byok_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(main.usage, "record_spend", lambda c: calls.append(c))
    monkeypatch.setattr(main, "_is_byok", lambda u: True)
    main._meter("x@y.com", 0.5)
    assert calls == []                                              # BYOK = user's spend, not metered
    monkeypatch.setattr(main, "_is_byok", lambda u: False)
    main._meter("x@y.com", 0.5)
    assert calls == [0.5]                                           # FILG-key run still metered


def test_save_and_read_anthropic_key(monkeypatch):
    monkeypatch.setenv("FILG_KEY_SECRET", Fernet.generate_key().decode())
    monkeypatch.setattr(keys, "_fernet", None)
    monkeypatch.setattr(keys, "_initialized", False)
    keys.save_key("dev@x.com", "anthropic", "sk-ant-api03-abcd1234wxyz")
    assert keys.get_key("dev@x.com") == "sk-ant-api03-abcd1234wxyz"
    assert keys.key_meta("dev@x.com") == {"provider": "anthropic", "last4": "wxyz"}


def test_main_auto_detects_provider_from_key_prefix():
    from app import main
    assert main._key_provider_kind("sk-ant-api03-foo") == "anthropic"
    assert main._key_provider_kind("sk-or-v1-foo") == "openrouter"
    assert main._key_provider_kind("  sk-ant-bar  ") == "anthropic"   # tolerates whitespace
