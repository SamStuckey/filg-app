#!/usr/bin/env python3
"""
Supabase auth — verify the JWT Supabase issues, with a dev fallback.

Supabase signs user JWTs HS256 with the project's JWT secret (Dashboard → Project Settings → API →
JWT Secret). We verify signature + expiry with stdlib `hmac` — no PyJWT/`cryptography` dependency, so
it runs anywhere. Identity is the token's email: the same key we bill on.

If `SUPABASE_JWT_SECRET` is unset (local / mock / dev), auth is "off": callers fall back to the email
typed in the request body as an UNVERIFIED identity. That keeps the free tier frictionless (try it
with just an email) while the paid tier is always gated on a verified user (see main.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "")
AUTH_ENABLED = bool(JWT_SECRET)


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def verify_token(token: str) -> dict | None:
    """Return the claims of a valid, unexpired Supabase HS256 token, else None."""
    if not JWT_SECRET or not token or token.count(".") != 2:
        return None
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        if json.loads(_b64url_decode(header_b64)).get("alg") != "HS256":
            return None
        expected = hmac.new(JWT_SECRET.encode(), f"{header_b64}.{payload_b64}".encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return None
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    if exp and time.time() > float(exp):
        return None
    return claims


def user_from_request(request) -> dict | None:
    """Extract a verified {id, email} from an `Authorization: Bearer <jwt>` header, or None."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    claims = verify_token(header[7:].strip())
    if not claims:
        return None
    return {"id": claims.get("sub"), "email": (claims.get("email") or "").strip().lower()}


if __name__ == "__main__":  # self-test: mint a token the same way Supabase would, then verify
    import os as _os

    def _mint(secret, claims):
        def seg(d):
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        head, pay = seg({"alg": "HS256", "typ": "JWT"}), seg(claims)
        sig = base64.urlsafe_b64encode(
            hmac.new(secret.encode(), f"{head}.{pay}".encode(), hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        return f"{head}.{pay}.{sig}"

    JWT_SECRET = "test-secret"
    good = _mint("test-secret", {"sub": "u1", "email": "A@X.com", "exp": time.time() + 60})
    assert verify_token(good)["email"] == "A@X.com"
    assert verify_token(_mint("WRONG", {"sub": "u1", "exp": time.time() + 60})) is None
    assert verify_token(_mint("test-secret", {"sub": "u1", "exp": time.time() - 1})) is None
    assert verify_token("not.a.jwt") is None

    class _Req:
        headers = {"authorization": f"Bearer {good}"}
    assert user_from_request(_Req()) == {"id": "u1", "email": "a@x.com"}
    print("auth.py self-test OK")
