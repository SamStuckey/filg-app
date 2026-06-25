#!/usr/bin/env python3
"""
Account plans + entitlements.

An account (one email) has ONE plan; a plan carries **limits** (numbers, e.g. how many AI
operations can run at once) and **features** (flags, e.g. whether the board is available). Plans are
defined here in code — a small, deploy-versioned set — while the account→plan *assignment* lives in
the DB (`store.account_plan`). Everything that gates behavior reads it through here, so when we start
feature-gating models/tiers there's exactly one place to do it.

v1 plans:
  - free : the pre-key state (no covered runs — a key is required to build; minimal concurrency).
  - byok : the default once a user brings a key (unlimited plans; N concurrent ops).
  - pro  : reserved for a future paid/comp tier (higher limits).

Limits fall back to DEFAULTS when a plan doesn't set them, so adding a new limit doesn't mean
editing every plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_CONCURRENCY = 3   # per-user concurrent AI operations, unless the plan overrides it

# Fallback limits for any plan that doesn't specify its own.
DEFAULTS = {"max_concurrent": DEFAULT_CONCURRENCY}


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    limits: dict = field(default_factory=dict)
    features: frozenset = field(default_factory=frozenset)


PLANS = {
    "free": Plan("free", "Free", {"max_concurrent": 1}, frozenset({"chat", "pdf"})),
    "byok": Plan("byok", "Bring your own key", {"max_concurrent": 3},
                 frozenset({"board", "chat", "pdf"})),
    "pro":  Plan("pro", "Operator", {"max_concurrent": 5},
                 frozenset({"board", "chat", "pdf", "priority"})),
}

# A resolved account with no explicit plan lands here. Mandatory-BYOK world: a keyed account is
# effectively "byok", so that's the sensible default for the concurrency limit.
DEFAULT_PLAN = "byok"


def get(plan_key: str | None) -> Plan:
    """The Plan for a key, falling back to the default plan for unknown/None keys."""
    return PLANS.get(plan_key or "", PLANS[DEFAULT_PLAN])


def limit(plan_key: str | None, name: str, default=None):
    """A numeric/limit entitlement: the plan's value, else the shared DEFAULTS, else `default`."""
    return get(plan_key).limits.get(name, DEFAULTS.get(name, default))


def max_concurrent(plan_key: str | None) -> int:
    """How many AI operations this plan may run at once."""
    return int(limit(plan_key, "max_concurrent", DEFAULT_CONCURRENCY))


def has_feature(plan_key: str | None, feature: str) -> bool:
    """Whether a feature flag is enabled for this plan (for future model/feature gating)."""
    return feature in get(plan_key).features


if __name__ == "__main__":  # self-test (no DB/API)
    assert get("byok").key == "byok"
    assert get(None).key == DEFAULT_PLAN and get("nonexistent").key == DEFAULT_PLAN
    assert max_concurrent("free") == 1 and max_concurrent("byok") == 3 and max_concurrent("pro") == 5
    assert max_concurrent(None) == PLANS[DEFAULT_PLAN].limits["max_concurrent"]
    assert has_feature("byok", "board") and not has_feature("free", "board")
    assert limit("free", "made_up_limit", 99) == 99   # unknown limit falls back
    print("plans.py self-test OK —", {k: v.limits for k, v in PLANS.items()})
