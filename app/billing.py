#!/usr/bin/env python3
"""
Stripe billing — the $39/mo "Operator" subscription.

Per the invariant in CLAUDE.md, `is_paid` is derived from the live subscription (this module +
app/store.py), NOT from the FILG_PAID_EMAILS allowlist (that stays only as a manual comp override).

stdlib-only: Checkout Sessions are created via Stripe's REST API over `urllib`, and webhook
signatures are verified with `hmac` — no `stripe` SDK / `cryptography` dependency, so it runs
anywhere. If STRIPE_SECRET_KEY / STRIPE_PRICE_ID are unset, billing is "off" and the endpoints say
so; the free tier still runs. Subscription state lives in the shared SQLite DB.

Env:
  STRIPE_SECRET_KEY      sk_live_… / sk_test_…
  STRIPE_PRICE_ID        price_… for the $39/mo recurring price
  STRIPE_WEBHOOK_SECRET  whsec_… (signing secret for the webhook endpoint)
  FILG_PUBLIC_URL        public base url for checkout success/cancel redirects
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth, store

SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("FILG_PUBLIC_URL", "http://localhost:8000").rstrip("/")
BILLING_ENABLED = bool(SECRET_KEY and PRICE_ID)   # the (dormant) $39/mo subscription

# The LOCKED monetization model (business_plan §16.1): one-time $35 for the polished investor-grade
# PDF, no subscription yet. The price is built inline (Stripe `price_data`) so no dashboard Price needs
# to exist — only the secret key. Override the amount with FILG_PDF_PRICE_CENTS.
PDF_PRICE_CENTS = int(os.environ.get("FILG_PDF_PRICE_CENTS", "3500"))
PDF_BILLING_ENABLED = bool(SECRET_KEY)

_API = "https://api.stripe.com/v1"


class StripeError(Exception):
    pass


def _post(path: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(
        f"{_API}{path}", data=body, method="POST",
        headers={"Authorization": f"Bearer {SECRET_KEY}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 — fixed https Stripe host
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise StripeError(f"Stripe API {e.code}: {e.read().decode()[:200]}") from e
    except urllib.error.URLError as e:  # network failure
        raise StripeError(f"Stripe unreachable: {e.reason}") from e


def create_checkout_url(email: str, user_id: str | None = None) -> str:
    """Create a subscription Checkout Session and return its hosted URL."""
    if not BILLING_ENABLED:
        raise StripeError("billing not configured")
    session = _post("/checkout/sessions", {
        "mode": "subscription",
        "line_items[0][price]": PRICE_ID,
        "line_items[0][quantity]": 1,
        "customer_email": email,
        "client_reference_id": user_id or email,
        "allow_promotion_codes": "true",
        "success_url": f"{PUBLIC_URL}/?upgraded=1",
        "cancel_url": f"{PUBLIC_URL}/?canceled=1",
    })
    return session["url"]


def create_pdf_checkout_url(email: str, *, user_id: str | None = None,
                            plan_id: str | None = None) -> str:
    """Create a ONE-TIME ($35) Checkout Session for the polished PDF unlock and return its hosted URL.
    Inline `price_data` so no pre-made Stripe Price is needed. On success Stripe sends a
    `checkout.session.completed` event with `mode=payment` → handle_event records the purchase."""
    if not PDF_BILLING_ENABLED:
        raise StripeError("billing not configured")
    ret = f"/plan/{plan_id}" if plan_id else "/"
    session = _post("/checkout/sessions", {
        "mode": "payment",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": PDF_PRICE_CENTS,
        "line_items[0][price_data][product_data][name]": "FILG investor-grade plan PDF",
        "line_items[0][quantity]": 1,
        "customer_email": email,
        "client_reference_id": user_id or email,
        "allow_promotion_codes": "true",
        "success_url": f"{PUBLIC_URL}{ret}?pdf=1",
        "cancel_url": f"{PUBLIC_URL}{ret}?pdf_canceled=1",
    })
    return session["url"]


def has_purchased(email: str) -> bool:
    """True iff this email has bought the one-time $35 PDF unlock (normalized for alias dedup)."""
    return store.has_purchased(auth.normalize_email(email))


def verify_webhook(payload: bytes, sig_header: str, tolerance: int = 300) -> dict | None:
    """Verify a Stripe webhook signature (`Stripe-Signature: t=…,v1=…`) and return the parsed event,
    or None if the secret is unset, the header is malformed, the timestamp is stale, or it fails."""
    if not WEBHOOK_SECRET or not sig_header:
        return None
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    ts, v1 = parts.get("t"), parts.get("v1")
    if not ts or not v1:
        return None
    try:
        if tolerance and abs(time.time() - int(ts)) > tolerance:
            return None
    except ValueError:
        return None
    expected = hmac.new(WEBHOOK_SECRET.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def handle_event(event: dict) -> None:
    """Translate a Stripe subscription lifecycle event into our subscription state."""
    typ = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    if typ == "checkout.session.completed":
        email = (obj.get("customer_email")
                 or (obj.get("customer_details") or {}).get("email") or "").strip().lower()
        if not email:
            return
        if obj.get("mode") == "payment":   # the one-time $35 PDF unlock (the live model)
            store.record_purchase(auth.normalize_email(email),
                                  stripe_session=obj.get("id"),
                                  amount_cents=obj.get("amount_total"))
        else:                              # subscription checkout (dormant $39/mo path)
            store.upsert_subscription(email, customer=obj.get("customer"),
                                      subscription=obj.get("subscription"), status="active")
    elif typ.startswith("customer.subscription."):
        email = store.email_for_customer(obj.get("customer"))
        if email:
            status = "canceled" if typ.endswith(".deleted") else obj.get("status")
            store.upsert_subscription(email, customer=obj.get("customer"),
                                      subscription=obj.get("id"), status=status,
                                      current_period_end=obj.get("current_period_end"))


def is_paid(email: str) -> bool:
    return store.is_paid(email)


if __name__ == "__main__":  # self-test (no network): webhook verification + event handling
    import tempfile

    store.DB = tempfile.mktemp(suffix=".db")
    WEBHOOK_SECRET = "whsec_test"

    def _signed(payload: dict):
        raw = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = hmac.new(WEBHOOK_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return raw, f"t={ts},v1={sig}"

    ev = {"type": "checkout.session.completed",
          "data": {"object": {"customer_email": "Buyer@X.com", "customer": "cus_9",
                              "subscription": "sub_9"}}}
    raw, header = _signed(ev)
    assert verify_webhook(raw, header) == ev
    assert verify_webhook(raw, header.replace("v1=", "v1=dead")) is None   # bad signature
    assert verify_webhook(raw, "garbage") is None

    handle_event(verify_webhook(raw, header))
    assert is_paid("buyer@x.com") is True
    cancel = {"type": "customer.subscription.deleted",
              "data": {"object": {"customer": "cus_9", "id": "sub_9"}}}
    handle_event(cancel)
    assert is_paid("buyer@x.com") is False

    # one-time $35 PDF unlock (mode=payment) → recorded as a purchase, not a subscription
    pdf_ev = {"type": "checkout.session.completed",
              "data": {"object": {"mode": "payment", "id": "cs_77", "amount_total": 3500,
                                  "customer_details": {"email": "Pdf+x@Gmail.com"}}}}
    raw, header = _signed(pdf_ev)
    handle_event(verify_webhook(raw, header))
    assert has_purchased("pdf@gmail.com") is True          # normalized alias unlocks
    assert is_paid("pdf@gmail.com") is False               # not a subscription
    print("billing.py self-test OK")
