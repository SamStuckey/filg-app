#!/usr/bin/env python3
"""
Stripe billing — two paid surfaces.

1. ONE-TIME PDF unlock (BYOK users): a $7 payment grants 3 plan-unlock credits. Free on your own key
   otherwise; the PDF is the paid artifact.
2. MONTHLY SUBSCRIPTION (paid tiers): runs on FILG's own key — Starter / Pro / Studio (see
   app/tiers.py). The subscription unlocks a model-stack ceiling + a monthly fair-use allowance, and
   the polished PDF is free. Tier prices/labels are passed in by the caller (main.py owns tiers.py) so
   this module stays free of the provider dependency.

stdlib-only: Checkout Sessions are created via Stripe's REST API over `urllib`, and webhook
signatures are verified with `hmac` — no `stripe` SDK / `cryptography` dependency, so it runs
anywhere. If STRIPE_SECRET_KEY is unset, billing is "off" and the endpoints say so; the app still
runs (the PDF is treated as unlocked in dev). State lives in the shared SQLite DB.

Env:
  STRIPE_SECRET_KEY      sk_live_… / sk_test_…
  STRIPE_WEBHOOK_SECRET  whsec_… (signing secret for the webhook endpoint)
  FILG_PDF_PRICE_CENTS   override the $7 PDF price (default 700)
  FILG_PUBLIC_URL        public base url for checkout success/cancel redirects
  (subscription tier prices are set in app/tiers.py, env-overridable there)
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
from datetime import datetime, timezone

from . import auth, store

SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PUBLIC_URL = os.environ.get("FILG_PUBLIC_URL", "http://localhost:8000").rstrip("/")

# The one paid action: a one-time $7 for the polished investor-grade PDF. Built inline (Stripe
# `price_data`) so no dashboard Price needs to exist — only the secret key. Override via env.
PDF_PRICE_CENTS = int(os.environ.get("FILG_PDF_PRICE_CENTS", "700"))
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


def create_pdf_checkout_url(email: str, *, user_id: str | None = None,
                            plan_id: str | None = None, plan_key: str | None = None) -> str:
    """Create a ONE-TIME ($7) Checkout Session for the polished PDF unlock and return its hosted URL.
    `plan_key` ("{sid}:{leaf-node-id}") scopes the unlock to ONE finished branch — a new branch is a
    new key and pays again. Inline `price_data` so no pre-made Stripe Price is needed. On success
    Stripe sends a `checkout.session.completed` event with `mode=payment` → handle_event records it."""
    if not PDF_BILLING_ENABLED:
        raise StripeError("billing not configured")
    ret = f"/plan/{plan_id}" if plan_id else "/"
    fields = {
        "mode": "payment",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": PDF_PRICE_CENTS,
        "line_items[0][price_data][product_data][name]": "FILG investor-grade plan PDF",
        "line_items[0][quantity]": 1,
        "customer_email": email,
        "client_reference_id": user_id or email,
        "metadata[kind]": "pdf",
        "allow_promotion_codes": "true",
        "success_url": f"{PUBLIC_URL}{ret}?pdf=1",
        "cancel_url": f"{PUBLIC_URL}{ret}?pdf_canceled=1",
    }
    if plan_key:
        fields["metadata[plan_key]"] = plan_key   # webhook scopes the unlock to this exact branch
    session = _post("/checkout/sessions", fields)
    return session["url"]


def create_subscription_checkout_url(email: str, *, tier: str, price_cents: int, label: str,
                                     user_id: str | None = None) -> str:
    """Create a MONTHLY subscription Checkout Session for a paid tier and return its hosted URL. Inline
    recurring `price_data` (no dashboard Price needed). `tier` rides on both the session metadata and
    `subscription_data[metadata]` so the subscription object carries it too — the webhook reads it back
    to set the account's tier. On success Stripe sends `checkout.session.completed` (mode=subscription)
    followed by `customer.subscription.created/updated` (which fills the billing-period boundary)."""
    if not PDF_BILLING_ENABLED:
        raise StripeError("billing not configured")
    fields = {
        "mode": "subscription",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": price_cents,
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]": f"FILG {label} (monthly)",
        "line_items[0][quantity]": 1,
        "customer_email": email,
        "client_reference_id": user_id or email,
        "metadata[kind]": "subscription",
        "metadata[tier]": tier,
        "subscription_data[metadata][kind]": "subscription",
        "subscription_data[metadata][tier]": tier,
        "allow_promotion_codes": "true",
        "success_url": f"{PUBLIC_URL}/account?sub=1",
        "cancel_url": f"{PUBLIC_URL}/?sub_canceled=1",
    }
    return _post("/checkout/sessions", fields)["url"]


def account_tier(email: str) -> str | None:
    """The active paid tier for an account (normalized), or None. Thin pass-through so main.py reads
    subscription state through the billing module like the rest of the money surface."""
    return store.account_tier(auth.normalize_email(email))


def has_purchased(email: str, plan_key: str | None = None) -> bool:
    """READ-only (no credit spent): may this email download `plan_key`'s PDF for free right now —
    because it's comped or already unlocked this plan? Normalized for alias dedup."""
    return store.has_purchased(auth.normalize_email(email), plan_key)


def credits_left(email: str) -> int:
    """Remaining paid plan-unlock credits for this account (normalized)."""
    return store.credits_left(auth.normalize_email(email))


def claim_pdf(email: str, plan_key: str) -> bool:
    """Claim `plan_key`'s PDF: free if comped/already-unlocked, else spend one credit. False if there's
    no access and no credit (caller asks for payment). Normalized for alias dedup."""
    return store.claim_pdf(auth.normalize_email(email), plan_key)


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


def _iso(unix_ts) -> str | None:
    """Stripe unix timestamp → ISO string (used for the subscription's current_period_end)."""
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _sub_status(stripe_status: str | None) -> str:
    """Map a Stripe subscription status to our vocabulary. active/trialing/past_due keep access
    (past_due = Stripe's dunning window); everything else (canceled, unpaid, incomplete*) drops it."""
    s = (stripe_status or "").lower()
    if s == "trialing":
        return "trialing"
    if s == "active":
        return "active"
    if s == "past_due":
        return "past_due"
    return "canceled"


def _event_email(obj: dict) -> str:
    return (obj.get("customer_email")
            or (obj.get("customer_details") or {}).get("email") or "").strip().lower()


def handle_event(event: dict) -> None:
    """Route a verified Stripe webhook to account/credit state. Handles the one-time PDF payment (+3
    credits, idempotent on session id) and the subscription lifecycle (checkout → created/updated →
    deleted). All account writes key on the NORMALIZED email so aliases resolve to one account."""
    etype = event.get("type")
    obj = event.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        mode = obj.get("mode")
        if mode == "payment":                                   # one-time PDF unlock → 3 credits
            email = _event_email(obj)
            if email:
                store.credit_for_session(auth.normalize_email(email), obj.get("id"))
        elif mode == "subscription":                            # a tier went live
            email = _event_email(obj)
            tier = (obj.get("metadata") or {}).get("tier")
            if email:
                store.set_subscription(
                    auth.normalize_email(email), tier=tier, status="active",
                    stripe_customer_id=obj.get("customer"),
                    stripe_subscription_id=obj.get("subscription"),
                    current_period_end=None)                    # filled by the subscription event below
        return

    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        acct = store.account_by_stripe(subscription_id=obj.get("id"), customer_id=obj.get("customer"))
        if not acct:
            return   # checkout.session.completed hasn't created the row yet; a later event will catch up
        status = _sub_status(obj.get("status"))
        if status == "canceled":
            store.cancel_subscription(acct["email"])
            return
        store.set_subscription(
            acct["email"], tier=(obj.get("metadata") or {}).get("tier") or acct.get("tier"),
            status=status, stripe_customer_id=obj.get("customer"), stripe_subscription_id=obj.get("id"),
            current_period_end=_iso(obj.get("current_period_end")))   # advances the fair-use window on renewal
        return

    if etype == "customer.subscription.deleted":
        acct = store.account_by_stripe(subscription_id=obj.get("id"), customer_id=obj.get("customer"))
        if acct:
            store.cancel_subscription(acct["email"])
        return


if __name__ == "__main__":  # self-test (no network): webhook verification + event handling
    import tempfile

    store.DB = tempfile.mktemp(suffix=".db")
    WEBHOOK_SECRET = "whsec_test"

    def _signed(payload: dict):
        raw = json.dumps(payload).encode()
        ts = str(int(time.time()))
        sig = hmac.new(WEBHOOK_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        return raw, f"t={ts},v1={sig}"

    # a $7 payment (mode=payment) → 3 plan-unlock credits on the normalized account
    pdf_ev = {"type": "checkout.session.completed",
              "data": {"object": {"mode": "payment", "id": "cs_77", "amount_total": 700,
                                  "metadata": {"kind": "pdf"},
                                  "customer_details": {"email": "Pdf+x@Gmail.com"}}}}
    raw, header = _signed(pdf_ev)
    assert verify_webhook(raw, header) == pdf_ev
    assert verify_webhook(raw, header.replace("v1=", "v1=dead")) is None   # bad signature
    assert verify_webhook(raw, "garbage") is None
    handle_event(verify_webhook(raw, header))
    assert credits_left("pdf@gmail.com") == 3              # normalized alias gets the credits
    handle_event(verify_webhook(raw, header))              # Stripe retries the SAME session → no double grant
    assert credits_left("pdf@gmail.com") == 3
    assert claim_pdf("pdf@gmail.com", "sid:leaf") is True and credits_left("pdf@gmail.com") == 2

    # subscription lifecycle: checkout → tier live; subscription.updated fills the period; deleted cancels
    assert account_tier("subby@x.com") is None
    handle_event({"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "id": "cs_sub1", "customer": "cus_A", "subscription": "sub_A",
        "metadata": {"kind": "subscription", "tier": "pro"},
        "customer_details": {"email": "Subby@x.com"}}}})
    assert account_tier("subby@x.com") == "pro"
    handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_A", "customer": "cus_A", "status": "active",
        "current_period_end": 1790000000, "metadata": {"tier": "pro"}}}})
    assert store.account_get("subby@x.com")["current_period_end"] is not None   # window boundary set
    handle_event({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_A", "customer": "cus_A", "status": "past_due"}}})
    assert account_tier("subby@x.com") == "pro"                                 # dunning keeps access
    handle_event({"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_A", "customer": "cus_A", "status": "canceled"}}})
    assert account_tier("subby@x.com") is None                                  # canceled → free
    print("billing.py self-test OK — $7 PDF credits + subscription lifecycle")
