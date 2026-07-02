#!/usr/bin/env python3
"""
Supabase auth — verify user JWTs against the project's ASYMMETRIC signing keys (JWKS).

Supabase's current system signs user tokens with an asymmetric key (ES256/RS256) and publishes the
PUBLIC half at the project's JWKS endpoint. We fetch that public key and verify the signature with it,
so this server never holds a forge-able signing secret — it only needs the public `SUPABASE_URL`.
(We deliberately moved off the legacy HS256 shared-secret path, which Supabase is deprecating.)

Identity is the token's email — the same key we bill on. Verification also requires `aud=authenticated`,
so only real signed-in users pass (not anon tokens).

If `SUPABASE_URL` is unset (local / mock / dev), auth is "off": callers fall back to the email typed
in the request body as an UNVERIFIED identity. That keeps the free tier frictionless while the paid
tier is always gated on a verified user (see main.py).
"""

from __future__ import annotations

import os

import jwt  # PyJWT (+ cryptography for ES256/RS256) — see requirements.txt
from jwt import PyJWKClient

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
AUTH_ENABLED = bool(SUPABASE_URL)

_ALGORITHMS = ["ES256", "RS256"]   # Supabase asymmetric signing keys (ECC P-256 / RSA)
_AUDIENCE = "authenticated"        # signed-in users; rejects anon tokens
_JWKS_PATH = "/auth/v1/.well-known/jwks.json"

_jwk_client: PyJWKClient | None = None


def _client() -> PyJWKClient | None:
    """Lazily build + cache the JWKS client (it caches fetched public keys internally)."""
    global _jwk_client
    if _jwk_client is None and SUPABASE_URL:
        _jwk_client = PyJWKClient(f"{SUPABASE_URL}{_JWKS_PATH}")
    return _jwk_client


def verify_token(token: str) -> dict | None:
    """Return the claims of a valid, unexpired Supabase token (signature checked against the project's
    public JWKS, aud=authenticated), else None."""
    if not AUTH_ENABLED or not token or token.count(".") != 2:
        return None
    client = _client()
    if client is None:
        return None
    try:
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=_ALGORITHMS,
                          audience=_AUDIENCE, options={"require": ["exp"]})
    except Exception:  # noqa: BLE001 — any verification failure ⇒ not authenticated
        return None


def user_from_request(request) -> dict | None:
    """Extract a verified {id, email} from an `Authorization: Bearer <jwt>` header, or None.

    Local dev only: if auth is OFF (no SUPABASE_URL) and FILG_DEV_EMAIL is set, act as that signed-in
    account — so `/api/me`, subscriptions, and the PDF gate reflect a `dev.py plan`-granted tier without
    standing up Supabase. Never active when auth is on (prod), so it can't be used to spoof identity."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        dev = os.environ.get("FILG_DEV_EMAIL")
        if dev and not AUTH_ENABLED:
            e = dev.strip().lower()
            return {"id": e, "email": e}
        return None
    claims = verify_token(header[7:].strip())
    if not claims:
        return None
    return {"id": claims.get("sub"), "email": (claims.get("email") or "").strip().lower()}


_GMAIL = ("gmail.com", "googlemail.com")


def normalize_email(email: str) -> str:
    """Collapse provider-equivalent addresses to ONE identity (anti-abuse, business_plan §16.1):
    lowercase, drop a `+suffix` from the local part, and for gmail/googlemail also strip dots and
    fold googlemail → gmail. Supabase treats `me+1@`/`me.e@`/`me@` as distinct accounts, so we dedupe
    them ourselves for the free-taste counter (and the $13 PDF unlock). Imperfect by design — separate
    real accounts slip through; the daily free-taste budget is the real ceiling."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


if __name__ == "__main__":  # self-test: sign an ES256 token like Supabase would, verify via a fake JWKS
    import time
    import types
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())

    def _mint(key, claims):
        return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "test"})

    # Force auth on and point the verifier at our in-memory public key (no network).
    AUTH_ENABLED = True
    _jwk_client = types.SimpleNamespace(
        get_signing_key_from_jwt=lambda tok: types.SimpleNamespace(key=priv.public_key()))

    good = _mint(priv, {"sub": "u1", "email": "A@X.com", "aud": "authenticated", "exp": time.time() + 60})
    assert verify_token(good)["email"] == "A@X.com"
    # wrong key → rejected
    other = ec.generate_private_key(ec.SECP256R1())
    assert verify_token(_mint(other, {"sub": "u1", "aud": "authenticated", "exp": time.time() + 60})) is None
    # expired → rejected
    assert verify_token(_mint(priv, {"sub": "u1", "aud": "authenticated", "exp": time.time() - 1})) is None
    # wrong audience → rejected
    assert verify_token(_mint(priv, {"sub": "u1", "aud": "anon", "exp": time.time() + 60})) is None
    assert verify_token("not.a.jwt") is None

    class _Req:
        headers = {"authorization": f"Bearer {good}"}
    assert user_from_request(_Req()) == {"id": "u1", "email": "a@x.com"}
    # email normalization (free-taste dedup)
    assert normalize_email("Me+filg@Gmail.com") == "me@gmail.com"
    assert normalize_email("m.e.123@googlemail.com") == "me123@gmail.com"
    assert normalize_email("me+a@fastmail.com") == "me@fastmail.com"   # plus stripped everywhere
    assert normalize_email("a.b@fastmail.com") == "a.b@fastmail.com"   # dots kept for non-gmail
    assert normalize_email("  Plain@X.com ") == "plain@x.com"
    print("auth.py self-test OK (ES256/JWKS + normalize_email)")
