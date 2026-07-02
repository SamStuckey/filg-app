#!/usr/bin/env python3
"""
Subscription tiers — the paid ladder that runs on FILG's own key.

FILG is free on your own key (BYOK) forever. The subscription is for people who don't want to deal
with an API key at all: they pay monthly and every run goes on FILG's Anthropic key. Because that
spend is FILG's, each tier has a monthly cost ceiling — a fair-use backstop. When a subscriber hits
it, they're prompted to add their own key (free) or wait for the ceiling to reset next billing period.

Two dials, both already in the codebase:
  - MODELS: `provider.STACK_ORDER` is a cheap→premium ladder of per-stage model "stacks". A tier
    unlocks the stacks from the cheapest up to its ceiling index (inclusive):
        starter → idx 0..2  (Opus-free: "the-turd-polisher" .. "the-work-horse")
        pro     → idx 0..3  (+ "the-wonder-kid", Opus-heavy)
        studio  → idx 0..4  (+ "trust-fund-baby", all Opus)
  - USAGE: `usage.py` meters run cost. Each tier carries a `monthly_cap_cents` enforced against the
    subscriber's per-period spend on FILG's key.

FEATURES: a tier may gate premium engine features (Director Forge, custom directors, the skeptic).

The monthly cap is what makes any price safe: cost is bounded by the cap regardless of which model a
subscriber picks, so margin = price − (cap + Stripe fee). Prices/caps are env-overridable so they can
be tuned in prod without a deploy.

This module is the single source of truth for the ladder. It's imported by main.py (gating) only —
store.py/billing.py stay free of the provider dependency so their standalone self-tests still run.
"""

from __future__ import annotations

import os
import sys

# prototype/ holds provider.py (the model-stack ladder). main.py already puts it on sys.path before
# importing app submodules; add it defensively so tiers.py also imports cleanly on its own.
_PROTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prototype")
if _PROTO not in sys.path:
    sys.path.insert(0, _PROTO)

import provider  # noqa: E402 — after the sys.path bootstrap above

FREE = "free"   # sentinel: no subscription (BYOK / free taste). Not a paid tier.

# Paid tiers. `max_stack` indexes provider.STACK_ORDER (inclusive ceiling). Prices + caps in cents,
# env-overridable. `features` gates premium engine routes (see has_feature).
TIERS: dict[str, dict] = {
    "starter": {
        "label": "Starter",
        "price_cents": int(os.environ.get("FILG_PRICE_STARTER_CENTS", "900")),      # $9/mo
        "max_stack": 2,                                                              # ≤ the-work-horse (no Opus)
        "monthly_cap_cents": int(os.environ.get("FILG_CAP_STARTER_CENTS", "450")),  # ~$4.50 API on our key
        "features": set(),
    },
    "pro": {
        "label": "Pro",
        "price_cents": int(os.environ.get("FILG_PRICE_PRO_CENTS", "2900")),         # $29/mo
        "max_stack": 3,                                                             # + the-wonder-kid (Opus)
        "monthly_cap_cents": int(os.environ.get("FILG_CAP_PRO_CENTS", "1600")),     # ~$16 API
        "features": {"director_forge", "custom_directors", "skeptic"},
    },
    "studio": {
        "label": "Studio",
        "price_cents": int(os.environ.get("FILG_PRICE_STUDIO_CENTS", "9900")),      # $99/mo
        "max_stack": 4,                                                             # + trust-fund-baby (all Opus)
        "monthly_cap_cents": int(os.environ.get("FILG_CAP_STUDIO_CENTS", "5500")),  # ~$55 API
        "features": {"director_forge", "custom_directors", "skeptic"},
    },
}
TIER_ORDER = ["starter", "pro", "studio"]   # cheap → premium (the pricing table's left→right order)


def is_tier(tier: str | None) -> bool:
    return tier in TIERS


def label(tier: str | None) -> str:
    return TIERS.get(tier, {}).get("label", "Free")


def price_cents(tier: str | None) -> int:
    return int(TIERS.get(tier, {}).get("price_cents", 0))


def monthly_cap_cents(tier: str | None) -> int:
    return int(TIERS.get(tier, {}).get("monthly_cap_cents", 0))


def allowed_stacks(tier: str | None) -> list[str]:
    """The stack keys this tier may run ON FILG'S KEY, cheapest → its ceiling. Unknown/free tiers get
    only the Opus-free default (a free-taste run must never spend Opus on FILG's dime — invariant #3)."""
    m = TIERS.get(tier, {}).get("max_stack")
    if m is None:
        return [provider.DEFAULT_STACK]
    return list(provider.STACK_ORDER[: m + 1])


def stack_allowed(tier: str | None, stack: str | None) -> bool:
    return provider.stack_name(stack) in allowed_stacks(tier)


def has_feature(tier: str | None, feature: str) -> bool:
    """Whether a paid tier includes a premium engine feature (director_forge / custom_directors /
    skeptic). BYOK users are handled separately by the caller (their own key → everything)."""
    return feature in TIERS.get(tier, {}).get("features", set())


def clamp_stack(stack: str | None, *, tier: str | None = None, byok: bool = False) -> str:
    """Resolve the stack a run may ACTUALLY use.
      - BYOK (the user's own key): they pay → any valid stack, Opus included, no downgrade.
      - Subscriber (paid tier, FILG's key): stacks up to the tier's ceiling; a request above the
        ceiling clamps down to the best stack the tier is entitled to.
      - Otherwise (free taste on FILG's key): the Opus-free default (invariant #3)."""
    name = provider.stack_name(stack)
    if byok:
        return name
    if tier in TIERS:
        allowed = allowed_stacks(tier)
        return name if name in allowed else allowed[-1]
    return provider.clamp_stack(name, byok=False)


def catalog() -> list[dict]:
    """Public tier catalog for the pricing UI: id, label, dollar price, unlocked stack keys (frontend
    maps keys → display names via its STACKS_UI), premium features, and the monthly cap (cents)."""
    out = []
    for t in TIER_ORDER:
        d = TIERS[t]
        out.append({
            "id": t,
            "label": d["label"],
            "price": d["price_cents"] / 100,
            "price_cents": d["price_cents"],
            "stacks": [{"key": k, "opus": provider.uses_opus(k)} for k in allowed_stacks(t)],
            "features": sorted(d["features"]),
            "cap_cents": d["monthly_cap_cents"],
        })
    return out


if __name__ == "__main__":  # self-test (imports provider; run from prototype-on-path context)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prototype"))
    import provider  # noqa: F811 — ensure importable when run standalone

    assert TIER_ORDER == ["starter", "pro", "studio"]
    # ladder: starter is Opus-free, pro adds one Opus stack, studio adds the all-Opus stack
    assert allowed_stacks("starter") == provider.STACK_ORDER[:3]
    assert not any(provider.uses_opus(s) for s in allowed_stacks("starter"))
    assert "the-wonder-kid" in allowed_stacks("pro") and provider.uses_opus("the-wonder-kid")
    assert allowed_stacks("studio") == provider.STACK_ORDER      # everything
    assert allowed_stacks(None) == [provider.DEFAULT_STACK]      # free → Opus-free default only
    # clamp: a starter asking for Opus gets clamped to their best allowed stack (not Opus)
    assert clamp_stack("trust-fund-baby", tier="starter") == provider.STACK_ORDER[2]
    assert not provider.uses_opus(clamp_stack("trust-fund-baby", tier="starter"))
    assert clamp_stack("trust-fund-baby", tier="studio") == "trust-fund-baby"   # studio is entitled
    assert clamp_stack("the-wonder-kid", tier="pro") == "the-wonder-kid"
    assert clamp_stack("trust-fund-baby", byok=True) == "trust-fund-baby"        # BYOK → anything
    assert clamp_stack("the-wonder-kid", tier=None) == provider.DEFAULT_STACK    # free taste → clamped
    # features
    assert has_feature("pro", "director_forge") and not has_feature("starter", "director_forge")
    assert monthly_cap_cents("starter") == 450 and price_cents("studio") == 9900
    assert len(catalog()) == 3 and catalog()[0]["id"] == "starter"
    print("tiers.py self-test OK — ladder:", *(f"{t}<=idx{TIERS[t]['max_stack']}" for t in TIER_ORDER))
