"""Subscription tiers — the paid ladder that runs on FILG's key.

Covers the load-bearing seams: the model-stack unlock ladder + clamp, the monthly fair-use budget
math, PDF-free-for-subscribers, the premium-feature gate, and that a subscriber is not key-walled.
"""

import provider
import usage
from app import billing, keys, main, store, tiers


def _sub(email, tier, period_end="2099-01-01T00:00:00Z"):
    store.set_subscription(email, tier=tier, status="active",
                           stripe_customer_id="cus_x", stripe_subscription_id="sub_x",
                           current_period_end=period_end)


def test_stack_ladder_and_clamp():
    # starter unlocks the three Opus-free stacks; pro adds one Opus stack; studio adds them all
    assert tiers.allowed_stacks("starter") == provider.STACK_ORDER[:3]
    assert not any(provider.uses_opus(s) for s in tiers.allowed_stacks("starter"))
    assert "the-wonder-kid" in tiers.allowed_stacks("pro")
    assert tiers.allowed_stacks("studio") == provider.STACK_ORDER
    # clamp: a starter asking for Opus is pulled down to their best allowed (non-Opus) stack
    assert tiers.clamp_stack("trust-fund-baby", tier="starter") == provider.STACK_ORDER[2]
    assert not provider.uses_opus(tiers.clamp_stack("trust-fund-baby", tier="starter"))
    # entitled tiers keep what they picked; BYOK gets anything; free taste → Opus-free default
    assert tiers.clamp_stack("the-wonder-kid", tier="pro") == "the-wonder-kid"
    assert tiers.clamp_stack("trust-fund-baby", tier="studio") == "trust-fund-baby"
    assert tiers.clamp_stack("trust-fund-baby", byok=True) == "trust-fund-baby"
    assert tiers.clamp_stack("the-wonder-kid", tier=None) == provider.DEFAULT_STACK


def test_budget_math_and_over_cap():
    email = "budget@x.com"
    _sub(email, "starter")
    b = main._budget(email)
    assert b["tier"] == "starter" and b["cap_cents"] == 450 and not b["over"]
    # spend past the $4.50 cap → over-limit, tokens tracked for the meter
    usage.record_monthly(main._acct(email), main._period(email), 5.00, 12345)
    b = main._budget(email)
    assert b["over"] and b["spent_cents"] >= 450 and b["tokens"] == 12345
    assert main._budget("noone@x.com") is None   # non-subscriber has no budget


def test_pdf_free_for_subscribers(monkeypatch):
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    email = "pdf-sub@x.com"
    assert main._has_pdf_access(email) is False   # billing on, no sub, no purchase → gated
    _sub(email, "pro")
    assert main._has_pdf_access(email) is True     # subscription includes the polished PDF


def test_feature_gate(monkeypatch):
    monkeypatch.setattr(keys, "enabled", lambda: True)   # paid regime on
    email = "feat@x.com"
    _sub(email, "starter")
    assert main._feature_ok(email, "director_forge") is False   # Starter lacks the premium feature
    assert main._feature_ok(email, "skeptic") is False
    _sub(email, "pro")
    assert main._feature_ok(email, "director_forge") is True     # Pro unlocks it
    monkeypatch.setattr(keys, "enabled", lambda: False)          # dev/local → no paywall, open
    assert main._feature_ok("anyone@x.com", "director_forge") is True


def test_subscriber_not_key_walled(monkeypatch):
    monkeypatch.setattr(keys, "enabled", lambda: True)
    monkeypatch.setattr(main, "_is_byok", lambda u: False)
    email = "wall@x.com"
    assert main._needs_key(email) is True     # no key, no sub → behind the BYOK wall
    _sub(email, "starter")
    assert main._needs_key(email) is False    # subscriber runs on FILG's key → not walled


def test_me_logged_out_carries_catalog(client):
    d = client.get("/api/me").json()
    assert d["signed_in"] is False
    assert len(d["tiers"]) == 3 and {t["id"] for t in d["tiers"]} == {"starter", "pro", "studio"}
    starter = next(t for t in d["tiers"] if t["id"] == "starter")
    assert starter["price"] == 9.0 and all(not s["opus"] for s in starter["stacks"])


def test_subscribe_route_validates(client, monkeypatch):
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    # unauthenticated (no token, auth disabled in tests) → 401 before any Stripe call
    assert client.post("/api/subscribe", json={"tier": "pro"}).status_code == 401
