"""Subscription tiers — the paid ladder that runs on FILG's key.

Covers the load-bearing seams of the 2026-07-06 restructure (Pro $29 / Ultimate $99, everything
unlocked, tiers differ only in allowance): the stack unlock + clamp, the monthly fair-use budget
math, PDF-free-for-subscribers, the feature gate, legacy tier folding, the allowance-exhausted
upgrade prompt, and that a subscriber is not key-walled.
"""

from engine import provider
from engine import usage
from app import access, billing, keys, main, ops, store, tiers


def _sub(email, tier, period_end="2099-01-01T00:00:00Z"):
    store.set_subscription(email, tier=tier, status="active",
                           stripe_customer_id="cus_x", stripe_subscription_id="sub_x",
                           current_period_end=period_end)


def test_stack_ladder_and_clamp():
    # every live tier unlocks every stack — the ladder differs only in the monthly allowance
    assert tiers.allowed_stacks("pro") == provider.STACK_ORDER
    assert tiers.allowed_stacks("ultimate") == provider.STACK_ORDER
    assert tiers.monthly_cap_cents("ultimate") > tiers.monthly_cap_cents("pro")
    # subscribers keep what they picked; BYOK gets anything; free taste → Opus-free default
    assert tiers.clamp_stack("trust-fund-baby", tier="pro") == "trust-fund-baby"
    assert tiers.clamp_stack("the-wonder-kid", tier="ultimate") == "the-wonder-kid"
    assert tiers.clamp_stack("trust-fund-baby", byok=True) == "trust-fund-baby"
    assert tiers.clamp_stack("the-wonder-kid", tier=None) == provider.DEFAULT_STACK


def test_legacy_tiers_fold_onto_live_ladder():
    # a pre-restructure account row (starter/studio) never drops to free — it folds to the nearest tier
    assert tiers.canonical("starter") == "pro" and tiers.canonical("studio") == "ultimate"
    assert tiers.is_tier("starter") and tiers.label("studio") == "Ultimate"
    assert tiers.clamp_stack("trust-fund-baby", tier="starter") == "trust-fund-baby"
    email = "legacy@x.com"
    _sub(email, "studio")                         # stored as the retired id
    assert access._tier(email) == "ultimate"      # read back canonical


def test_budget_math_over_cap_and_upgrade_prompt():
    email = "budget@x.com"
    _sub(email, "pro")
    b = access._budget(email)
    assert b["tier"] == "pro" and b["cap_cents"] == 1600 and not b["over"]
    # spend past the $16 cap → over-limit, tokens tracked for the meter
    usage.record_monthly(access._acct(email), access._period(email), 17.00, 12345)
    b = access._budget(email)
    assert b["over"] and b["spent_cents"] >= 1600 and b["tokens"] == 12345
    assert access._budget("noone@x.com") is None   # non-subscriber has no budget
    # Ultimate is HIDDEN for launch: the allowance-exhausted response pitches only the BYOK fallback
    # (no upgrade rung on the public ladder), and checkout refuses the hidden tier.
    resp = ops.budget_response(ops.BudgetError(b))
    body = resp.body.decode()
    assert resp.status_code == 402 and "upgradeTier" not in body
    assert "fallback" in body
    assert tiers.next_tier("pro") is None and tiers.next_tier(None) == "pro"
    assert tiers.purchasable("pro") and not tiers.purchasable("ultimate")
    # …but an account already holding it keeps the full allowance + everything unlocked
    assert tiers.monthly_cap_cents("ultimate") == 5500
    assert tiers.allowed_stacks("ultimate") == provider.STACK_ORDER


def test_pdf_free_for_subscribers(monkeypatch):
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    email = "pdf-sub@x.com"
    assert main._has_pdf_access(email) is False   # billing on, no sub, no purchase → gated
    _sub(email, "pro")
    assert main._has_pdf_access(email) is True     # every subscription includes unlimited clean PDFs


def test_feature_gate(monkeypatch):
    monkeypatch.setattr(keys, "enabled", lambda: True)   # paid regime on
    email = "feat@x.com"
    assert main._feature_ok(email, "director_forge") is False    # free + keyless → gated
    _sub(email, "pro")
    assert main._feature_ok(email, "director_forge") is True     # the $29 tier includes everything
    assert main._feature_ok(email, "skeptic") is True
    monkeypatch.setattr(keys, "enabled", lambda: False)          # dev/local → no paywall, open
    assert main._feature_ok("anyone@x.com", "director_forge") is True


def test_subscriber_not_key_walled(monkeypatch):
    monkeypatch.setattr(keys, "enabled", lambda: True)
    monkeypatch.setattr(access, "_is_byok", lambda u: False)
    email = "wall@x.com"
    assert main._needs_key(email) is True     # no key, no sub → behind the BYOK wall
    _sub(email, "pro")
    assert main._needs_key(email) is False    # subscriber runs on FILG's key → not walled


def test_me_logged_out_carries_catalog(client):
    d = client.get("/api/me").json()
    assert d["signed_in"] is False
    # the public ladder for launch: just Pro (Ultimate defined but hidden until it earns features)
    assert [t["id"] for t in d["tiers"]] == ["pro"]
    pro = d["tiers"][0]
    assert pro["price"] == 29.0 and any(s["opus"] for s in pro["stacks"])   # $29 includes Opus stacks
    assert d["pdf_price"] == 1300                                            # the $13 per-plan unlock


def test_subscribe_route_validates(client, monkeypatch):
    monkeypatch.setattr(billing, "PDF_BILLING_ENABLED", True)
    # unauthenticated (no token, auth disabled in tests) → 401 before any Stripe call
    assert client.post("/api/subscribe", json={"tier": "pro"}).status_code == 401
    # a hidden tier is never sold, even to a signed-in user — 400 before any Stripe call
    import app.auth as app_auth
    monkeypatch.setattr(app_auth, "user_from_request",
                        lambda req: {"id": "u", "email": "buyer@x.com"})
    assert client.post("/api/subscribe", json={"tier": "ultimate"}).status_code == 400
    assert client.post("/api/subscribe", json={"tier": "studio"}).status_code == 400


def test_key_precedence_paid_allowance_first(monkeypatch):
    """A subscriber spends their paid allowance on OUR key first, THEN falls back to their own key —
    we never charge for credits and then quietly bill their key. Free/BYOK users run on their key."""
    from engine import usage
    email = "prec@x.com"
    # free user: no key → hosted taste; with a key → their key
    monkeypatch.setattr(access, "_is_byok", lambda u: False)
    assert access._on_filg_key(email) is True
    monkeypatch.setattr(access, "_is_byok", lambda u: True)
    assert access._on_filg_key(email) is False
    # subscriber UNDER allowance → OUR key even though they have a key (spend paid credits first)
    _sub(email, "pro")
    assert access._on_filg_key(email) is True
    # exhaust the allowance → fall back to their own key (the BYOK fallback)
    usage.record_monthly(access._acct(email), access._period(email), 100.0, 0)
    assert access._on_filg_key(email) is False
    # over allowance but NO key → still ours (the fair-use gate then prompts upgrade/add-key/wait)
    monkeypatch.setattr(access, "_is_byok", lambda u: False)
    assert access._on_filg_key(email) is True


def test_meter_follows_actual_key(monkeypatch):
    """Metering follows the key the run ACTUALLY used: a subscriber under allowance (on our key) is
    metered monthly even though they have a key saved — the bug was skipping it because they had a key."""
    from engine import usage
    email = "mfollow@x.com"
    _sub(email, "pro")
    monkeypatch.setattr(access, "_is_byok", lambda u: True)   # has a key, but under allowance → our key
    rec = []
    monkeypatch.setattr(usage, "record_monthly", lambda e, p, c, t: rec.append((c, t)))
    main._meter(email, 0.5)
    assert rec == [(0.5, 0)]                                 # metered against the monthly allowance


def test_subscriber_runs_bill_the_subscription_account(monkeypatch):
    """Paid runs bill SUBSCRIBER_ANTHROPIC_KEY; the free taste bills the general hosted key. Two
    separate Anthropic accounts, so a drained taste pool can't stall runs people paid for (and a
    runaway taste day can't spend the subscription float)."""
    seen = {}

    def _fake_provider(api_key=None, bills_filg=True):
        seen["key"], seen["bills_filg"] = api_key, bills_filg
        return type("P", (), {"bills_filg": bills_filg, "name": "anthropic"})()

    monkeypatch.setattr(provider, "anthropic_provider", _fake_provider)
    monkeypatch.setattr(provider, "hosted_key", lambda: "sk-ant-taste")
    monkeypatch.setenv("SUBSCRIBER_ANTHROPIC_KEY", "sk-ant-subs")
    monkeypatch.setattr(access, "_is_byok", lambda u: False)

    # free / anonymous taste → the taste account
    access._provider_for("freekey@x.com")
    assert seen["key"] == "sk-ant-taste"

    # a subscriber under allowance → the subscription account
    email = "subskey@x.com"
    _sub(email, "pro")
    access._provider_for(email)
    assert seen["key"] == "sk-ant-subs"
    # ...and it is still FILG's spend, so metering is unchanged by WHICH of our accounts funded it
    assert seen["bills_filg"] is True
    assert access._on_filg_key(email) is True


def test_subscriber_key_falls_back_when_unset(monkeypatch):
    """An unset SUBSCRIBER_ANTHROPIC_KEY degrades to the old single-account behavior — never key-wall
    a paying subscriber just because the split isn't configured yet."""
    monkeypatch.setattr(provider, "hosted_key", lambda: "sk-ant-taste")
    monkeypatch.delenv("SUBSCRIBER_ANTHROPIC_KEY", raising=False)
    assert access._subscriber_key() == "sk-ant-taste"
    assert access.hosted_key_status() == {"taste_key": True, "subscriber_key": False}
    monkeypatch.setenv("SUBSCRIBER_ANTHROPIC_KEY", "sk-ant-subs")
    assert access._subscriber_key() == "sk-ant-subs"
    assert access.hosted_key_status() == {"taste_key": True, "subscriber_key": True}
