#!/usr/bin/env python3
"""
Access + entitlement service — the pure decision layer behind the routes.

Answers "who is this user, what may they do, whose key does their run bill, and how is that run
metered?" with no HTTP in sight: every function here takes/returns plain values, so the route layer
in main.py is a thin adapter over these and they're unit-testable in isolation. The load-bearing rule
is `_on_filg_key` (the single key-precedence decision) — `_provider_for` picks the provider from it and
`_meter`/`_meter_tokens` follow the SAME decision, so billing and metering can never diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine import usage     # free-taste + monthly fair-use metering
from engine import provider  # per-run LLM provider (FILG's hosted key vs a user's own)

from . import auth, billing, keys, store, tiers  # identity, purchases, BYOK keys, accounts, tier ladder

def _is_byok(user: str) -> bool:
    """True iff this user runs on their own key (BYOK configured + a key saved)."""
    return bool(user and keys.enabled() and keys.has_key(user))


def _acct(user: str) -> str:
    """Normalized account id (alias-collapsed) — the key billing, subscriptions, and monthly usage share."""
    return auth.normalize_email(user or "")


def _tier(user: str) -> str | None:
    """The active paid subscription tier for this user, or None (free / BYOK). Legacy tier ids from
    the retired 3-tier ladder fold onto the live ladder (tiers.canonical)."""
    return tiers.canonical(store.account_tier(_acct(user))) if user else None


def _is_subscriber(user: str) -> bool:
    return bool(_tier(user))


def _period(user: str) -> str:
    """The current fair-use window key for a subscriber: the subscription's period-end (so the cap
    resets exactly on renewal), falling back to the calendar month before Stripe fills the period in."""
    a = store.account_get(_acct(user)) or {}
    return a.get("current_period_end") or datetime.now(timezone.utc).strftime("%Y-%m")


def _budget(user: str, tier: str | None = None) -> dict | None:
    """A subscriber's fair-use status this period (None for non-subscribers). Enforced on COST — dollars
    bound margin identically across models, so a bounded cap makes any tier price safe regardless of
    which stack they pick; `tokens` is display-only."""
    tier = tier or _tier(user)
    if not tier:
        return None
    cap = tiers.monthly_cap_cents(tier)
    mu = usage.monthly_usage(_acct(user), _period(user))
    spent = int(round((mu.get("spend") or 0.0) * 100))
    a = store.account_get(_acct(user)) or {}
    return {"tier": tier, "cap_cents": cap, "spent_cents": spent,
            "remaining_cents": max(0, cap - spent), "tokens": mu.get("tokens") or 0,
            "reset_at": a.get("current_period_end"), "over": spent >= cap}


def _feature_ok(user: str, feature: str) -> bool:
    """Whether a premium engine feature (director_forge / custom_directors / skeptic) is available.
    Open in dev/local (no BYOK regime → no paywall at all, same as _needs_key). When the paid regime is
    on: available to BYOK users (their own key → everything) and to paid tiers that include it."""
    if not keys.enabled():
        return True
    if _is_byok(user):
        return True
    return tiers.has_feature(_tier(user), feature)


def _has_pdf_access(email: str, plan_key: str | None = None, verified: bool = False) -> bool:
    """READ-only: may this user download `plan_key`'s PDF for FREE right now — already unlocked this
    plan, or holds a comp grant — OR billing is off (dev/local → open)? Does NOT count available-but-
    unspent credits (the button checks those separately); does NOT consume one. Spending a credit on a
    new plan happens at download time via billing.claim_pdf."""
    if not billing.PDF_BILLING_ENABLED:
        return True
    if _is_subscriber(email):        # the subscription includes the polished PDF (no per-plan charge)
        return True
    return billing.has_purchased(email, plan_key)


def _key_provider_kind(api_key: str) -> str:
    """Which backend a pasted key is for, by prefix: sk-ant-… → Anthropic direct, else OpenRouter."""
    return "anthropic" if api_key.strip().startswith("sk-ant-") else "openrouter"


def _build_provider(kind: str, key: str):
    """Build the per-run provider for a stored user key — Anthropic direct or OpenRouter. Either way
    the user pays (bills_filg=False), so it never counts against FILG's daily budget."""
    if kind == "anthropic":
        return provider.anthropic_provider(key, bills_filg=False)
    return provider.openrouter_provider(key)


def _hosted():
    """FILG's hosted provider (the 'account key'), or None if no hosted key is configured (BYOK-only)."""
    return provider.anthropic_provider() if provider.hosted_key() else None


def _byok_provider(user: str):
    """The user's own saved key as a provider (bills_filg=False), or None if they have none."""
    if user and keys.enabled():
        key = keys.get_key(user)
        if key:
            kind = (keys.key_meta(user) or {}).get("provider") or "openrouter"
            return _build_provider(kind, key)
    return None


def _on_filg_key(user: str) -> bool:
    """Whether the user's next run bills FILG's key (vs their own). This is the SINGLE precedence rule —
    `_provider_for` picks the key from it, and metering follows the same decision so they never diverge:

      - SUBSCRIBER: spend the paid monthly allowance on OUR key FIRST; only once it's exhausted fall back
        to their own key (if they've added one) — never charge for credits and then quietly bill their
        key. Over the allowance with no key → still ours (the fair-use gate then prompts add-key/wait).
      - FREE / BYOK-only: their own key if they've saved one (they pay), else FILG's hosted free taste.

    Read pre-op — i.e. against the allowance state the provider was chosen under — so it's stable for the
    op's own metering (the op's spend isn't recorded until _meter runs)."""
    tier = _tier(user)
    if tier:
        b = _budget(user, tier)
        if not (b and b["over"]):
            return True                 # under the paid allowance → our key
        return not _is_byok(user)       # allowance spent → their key if they have one, else still ours
    return not _is_byok(user)           # free/BYOK: their own key if saved, else the hosted taste


def _provider_for(user: str):
    """The provider a run uses, per `_on_filg_key`. Falls back across sides when one is unavailable (a
    subscriber under allowance but no hosted key → their key; a free user with no key → the hosted
    taste, or None → they get walled)."""
    if _on_filg_key(user):
        return _hosted() or _byok_provider(user)
    return _byok_provider(user) or _hosted()


def _meter(user: str, cost: float) -> None:
    """Record a run's spend IFF it went on FILG's key (per `_on_filg_key`). A run on the user's own key
    is their spend, untracked here. A subscriber's hosted run counts against their monthly allowance
    (record_monthly also bumps the daily kill switch); a free-taste hosted run → the daily kill switch."""
    if not cost or not _on_filg_key(user):
        return
    if _is_subscriber(user):
        usage.record_monthly(_acct(user), _period(user), cost, 0)
    else:
        usage.record_spend(cost, taste=True)


def _meter_tokens(user: str, toks: int) -> None:
    """Fold an op's token count into a subscriber's monthly usage (tokens only — cost is metered by
    `_meter`) when the run went on FILG's key. No-op for runs on the user's own key."""
    if toks and user and _on_filg_key(user) and _is_subscriber(user):
        usage.record_monthly(_acct(user), _period(user), 0.0, int(toks))


def _needs_key(user: str) -> bool:
    """BYOK model: when BYOK is on and this user has neither a saved key NOR an active subscription,
    they're behind the wall — every API action requires their own key. A subscriber runs on FILG's key
    (bounded by their monthly fair-use cap), so they're never key-walled."""
    return bool(keys.enabled() and not _is_byok(user) and not _is_subscriber(user))
