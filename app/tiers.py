#!/usr/bin/env python3
"""
Subscription tiers — the paid ladder that runs on FILG's own key.

FILG is free on your own key (BYOK) forever; the only BYOK purchase is the $13 clean-PDF unlock
(per plan, re-download free — billing.py). The subscription is for people who don't want to deal
with an API key at all: they pay monthly and every run goes on FILG's Anthropic key.

The ladder (2026-07-06 restructure — supersedes Starter $9 / Pro $29 / Studio $99):
    pro      $29/mo — ALL features and ALL model stacks (Opus included), unlimited clean PDFs,
                      bounded by a monthly usage allowance (the fair-use cost cap).
    ultimate $99/mo — the same everything; the difference is the allowance. For people who hit
                      Pro's ceiling and want more monthly credits instead of a BYOK fallback key.

When a subscriber hits their allowance they're prompted to (a) upgrade to Ultimate for more monthly
credits, or (b) add their own key as a BYOK fallback (runs overflow onto it, free) — or wait for the
allowance to reset next billing period. The allowance is enforced on COST (usage.usage_monthly), so
margin = price − (cap + Stripe fee) regardless of which models they run.

This module is the single source of truth for the ladder. It's imported by main.py (gating) only —
store.py/billing.py stay free of the provider dependency so their standalone self-tests still run.
Legacy tier ids from the retired 3-tier ladder (starter/studio) normalize to the nearest live tier
so a pre-restructure account row never falls back to free.
"""

from __future__ import annotations

import os

from engine import provider  # the model-stack ladder

FREE = "free"   # sentinel: no subscription (BYOK / free taste). Not a paid tier.

# Every paid tier includes every feature and every stack — the tiers differ ONLY in the monthly
# allowance. `features` is kept per-tier so has_feature stays the single gate if that ever changes.
ALL_FEATURES = {"director_forge", "custom_directors", "skeptic"}

# Paid tiers. `max_stack` indexes provider.STACK_ORDER (inclusive ceiling). Prices + caps in cents,
# env-overridable so they can be tuned in prod without a deploy.
TIERS: dict[str, dict] = {
    "pro": {
        "label": "Pro",
        "price_cents": int(os.environ.get("FILG_PRICE_PRO_CENTS", "2900")),          # $29/mo
        "max_stack": len(provider.STACK_ORDER) - 1,                                  # everything
        "monthly_cap_cents": int(os.environ.get("FILG_CAP_PRO_CENTS", "1600")),      # ~$16 API on our key
        "features": set(ALL_FEATURES),
    },
    "ultimate": {
        "label": "Ultimate",
        "price_cents": int(os.environ.get("FILG_PRICE_ULTIMATE_CENTS", "9900")),     # $99/mo
        "max_stack": len(provider.STACK_ORDER) - 1,                                  # everything
        "monthly_cap_cents": int(os.environ.get("FILG_CAP_ULTIMATE_CENTS", "5500")), # ~$55 API
        "features": set(ALL_FEATURES),
    },
}
TIER_ORDER = ["pro", "ultimate"]   # cheap → premium (the pricing table's left→right order)

# HIDDEN FOR LAUNCH (Sam, 2026-07-06): the public ladder is just BYOK (free) + Pro. Ultimate stays
# fully defined — an account already carrying it keeps its allowance — but it doesn't appear in the
# catalog, isn't purchasable, and the allowance-exhausted prompt stops pitching it. Re-light it from
# the Render dashboard (FILG_SHOW_ULTIMATE=1) when there are features worth a second rung (coaching,
# goal tracker, blocker-resolution helpers — the Phase-2 retention ideas).
HIDDEN_TIERS = set() if os.environ.get("FILG_SHOW_ULTIMATE") == "1" else {"ultimate"}
PUBLIC_TIER_ORDER = [t for t in TIER_ORDER if t not in HIDDEN_TIERS]

# The retired 3-tier ladder's ids, folded onto the live ladder — an account row written before the
# restructure keeps working (there were no live Stripe subscriptions, but dev grants + tests exist).
_LEGACY = {"starter": "pro", "studio": "ultimate"}


def canonical(tier: str | None) -> str | None:
    """The live tier id for any stored tier value (legacy ids fold in), or None."""
    if tier in TIERS:
        return tier
    return _LEGACY.get(tier or "")


def is_tier(tier: str | None) -> bool:
    return canonical(tier) in TIERS


def label(tier: str | None) -> str:
    return TIERS.get(canonical(tier), {}).get("label", "Free")


def price_cents(tier: str | None) -> int:
    return int(TIERS.get(canonical(tier), {}).get("price_cents", 0))


def monthly_cap_cents(tier: str | None) -> int:
    return int(TIERS.get(canonical(tier), {}).get("monthly_cap_cents", 0))


def allowed_stacks(tier: str | None) -> list[str]:
    """The stack keys this tier may run ON FILG'S KEY, cheapest → its ceiling. Unknown/free tiers get
    only the Opus-free default (a free-taste run must never spend Opus on FILG's dime — invariant #3)."""
    m = TIERS.get(canonical(tier), {}).get("max_stack")
    if m is None:
        return [provider.DEFAULT_STACK]
    return list(provider.STACK_ORDER[: m + 1])


def stack_allowed(tier: str | None, stack: str | None) -> bool:
    return provider.stack_name(stack) in allowed_stacks(tier)


def has_feature(tier: str | None, feature: str) -> bool:
    """Whether a paid tier includes a premium engine feature (director_forge / custom_directors /
    skeptic). Every live tier includes every feature; the gate stays so that can change later.
    BYOK users are handled separately by the caller (their own key → everything)."""
    return feature in TIERS.get(canonical(tier), {}).get("features", set())


def purchasable(tier: str | None) -> bool:
    """Whether this tier may be BOUGHT right now (on the public ladder). Hidden tiers stay valid for
    accounts that already hold them, but checkout refuses them."""
    return canonical(tier) in PUBLIC_TIER_ORDER


def next_tier(tier: str | None) -> str | None:
    """The upgrade target from a tier (the allowance-exhausted prompt offers it), or None at the top.
    Walks the PUBLIC ladder only — a hidden tier is never pitched."""
    t = canonical(tier)
    if t is None:
        return PUBLIC_TIER_ORDER[0] if PUBLIC_TIER_ORDER else None
    later = [x for x in PUBLIC_TIER_ORDER if TIER_ORDER.index(x) > TIER_ORDER.index(t)]
    return later[0] if later else None


def clamp_stack(stack: str | None, *, tier: str | None = None, byok: bool = False) -> str:
    """Resolve the stack a run may ACTUALLY use.
      - BYOK (the user's own key): they pay → any valid stack, Opus included, no downgrade.
      - Subscriber (paid tier, FILG's key): every stack (the allowance bounds cost, not the model).
      - Otherwise (free taste on FILG's key): the Opus-free default (invariant #3)."""
    name = provider.stack_name(stack)
    if byok:
        return name
    t = canonical(tier)
    if t in TIERS:
        allowed = allowed_stacks(t)
        return name if name in allowed else allowed[-1]
    return provider.clamp_stack(name, byok=False)


def catalog() -> list[dict]:
    """PUBLIC tier catalog for the pricing UI (hidden tiers excluded): id, label, dollar price,
    unlocked stack keys (frontend maps keys → display names via its STACKS_UI), premium features,
    and the monthly cap (cents)."""
    out = []
    for t in PUBLIC_TIER_ORDER:
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


if __name__ == "__main__":  # self-test: python -m app.tiers
    assert TIER_ORDER == ["pro", "ultimate"]
    # both defined tiers unlock EVERYTHING — the ladder differs only in the monthly allowance
    assert allowed_stacks("pro") == provider.STACK_ORDER
    assert allowed_stacks("ultimate") == provider.STACK_ORDER
    assert allowed_stacks(None) == [provider.DEFAULT_STACK]      # free → Opus-free default only
    assert monthly_cap_cents("ultimate") > monthly_cap_cents("pro")
    # clamp: a subscriber keeps what they picked; free taste clamps; BYOK gets anything
    assert clamp_stack("trust-fund-baby", tier="pro") == "trust-fund-baby"
    assert clamp_stack("trust-fund-baby", byok=True) == "trust-fund-baby"
    assert clamp_stack("the-wonder-kid", tier=None) == provider.DEFAULT_STACK
    # legacy ids fold onto the live ladder (a pre-restructure account row never drops to free)
    assert canonical("starter") == "pro" and canonical("studio") == "ultimate"
    assert is_tier("starter") and label("studio") == "Ultimate"
    assert clamp_stack("trust-fund-baby", tier="starter") == "trust-fund-baby"
    assert has_feature("pro", "director_forge") and has_feature("pro", "skeptic")
    # ultimate is HIDDEN for launch: defined + honored on accounts, but not sold or pitched
    if "ultimate" in HIDDEN_TIERS:
        assert PUBLIC_TIER_ORDER == ["pro"]
        assert purchasable("pro") and not purchasable("ultimate") and not purchasable("studio")
        assert next_tier(None) == "pro" and next_tier("pro") is None and next_tier("ultimate") is None
        assert len(catalog()) == 1 and catalog()[0]["id"] == "pro"
        assert allowed_stacks("ultimate") == provider.STACK_ORDER   # an existing holder keeps it all
    else:   # FILG_SHOW_ULTIMATE=1 — the second rung is live
        assert PUBLIC_TIER_ORDER == ["pro", "ultimate"] and purchasable("ultimate")
        assert next_tier("pro") == "ultimate" and next_tier("ultimate") is None
        assert len(catalog()) == 2
    assert price_cents("pro") == 2900 and price_cents("ultimate") == 9900
    print("tiers.py self-test OK — public ladder:",
          *(f"{t}=${TIERS[t]['price_cents']/100:g}" for t in PUBLIC_TIER_ORDER))
