#!/usr/bin/env python3
"""
FILG local dev/test harness — be any user-type against a local SQLite DB (no staging, no prod backdoor).

Everything that distinguishes a user-type (fresh taste / post-taste / kill-gated / budget-exhausted /
BYOK / entitlement) is just config + DB state, so locally you can set it directly instead of baking a
test-mode into production code. Point the app AND this CLI at the same DB via FILG_DB (see TESTING.md),
then drive states from here.

  python scripts/dev.py serve                 # run the app locally (mock, local DB)
  python scripts/dev.py serve --real          # run the app for REAL (BYOK key in the UI drives the engine)
  python scripts/dev.py engine "<idea>"       # REAL engine smoke test (key from env), prints the spine
                                              #   phase log + graded rows; --full for the artifact synth
  python scripts/dev.py reset                 # wipe the local DB → fresh everything
  python scripts/dev.py state <sid>           # inspect a plan's gating-relevant state
  python scripts/dev.py kill <sid>            # force verdict=kill → exercise the coaching ladder
  python scripts/dev.py unkill <sid>          # back to 'pursue'
  python scripts/dev.py spend <amount>        # add to today's spend → test the daily kill-switch / degrade
  python scripts/dev.py usage                 # show today_spend / daily_budget / free-run counters
  python scripts/dev.py plan <email> <tier>   # set a subscription tier (pro|ultimate|free)
  python scripts/dev.py account <email>       # why am I (not) walled? tier / saved key / monthly usage
  python scripts/dev.py key <email> [--clear] # show or clear a saved BYOK key (flip free ↔ BYOK)
  python scripts/dev.py models [--check]      # show the model catalog (--check hits the cached Models API)
  python scripts/dev.py buy <email>           # grant an account-wide PDF comp → test the export
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # repo root -> `app` + `engine` packages
os.environ.setdefault("FILG_DB", str(ROOT / ".dev" / "filg.db"))


def _store():
    from app import store
    store.init()
    return store


def _usage():
    from engine import usage
    return usage


def _norm(email):
    """Normalize an email the way the app dedupes the free taste + PDF unlock (alias collapse)."""
    from app import auth
    return auth.normalize_email(email)


def cmd_serve(args):
    # Default to mock (no spend). `serve --real` runs the actual engine — then the UI's BYOK key drives
    # real model calls (set FILG_KEY_SECRET so the key modal/wall is active; see TESTING.md).
    if not getattr(args, "real", False):
        os.environ.setdefault("FILG_MOCK", "1")
    else:
        os.environ["FILG_MOCK"] = "0"
    Path(os.environ["FILG_DB"]).parent.mkdir(parents=True, exist_ok=True)
    print(f"FILG_DB={os.environ['FILG_DB']}  FILG_MOCK={os.environ.get('FILG_MOCK', '0')}")
    print("→ http://localhost:8000  (set FILG_KEY_SECRET to exercise the BYOK wall; see TESTING.md)")
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--reload", "--app-dir", str(ROOT), "--port", "8000"])


def cmd_engine(args):
    """Run the REAL engine (no mock) against YOUR OWN key, printing the live phase log + graded rows.
    The single end-to-end smoke test of the deterministic spine: conductor, research validator, voted
    moat gate, staleness, and the ⚙ activity feed, with no server and no DB. Key comes from the env
    (never the CLI): ANTHROPIC_API_KEY (sk-ant-...) or OPENROUTER_API_KEY (sk-or-...)."""
    from engine import provider
    from engine import pipeline
    from app import teardown
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("set ANTHROPIC_API_KEY (sk-ant-...) or OPENROUTER_API_KEY (sk-or-...) in your env first "
              "(e.g. `export ANTHROPIC_API_KEY=...` — do NOT pass it as an argument)")
        return
    prov = provider.openrouter_provider(key) if key.startswith("sk-or") \
        else provider.anthropic_provider(key, bills_filg=False)
    stack = provider.clamp_stack(args.stack, byok=True) if args.stack else provider.DEFAULT_STACK
    print(f"engine: REAL mode · provider={prov.kind} · stack={stack}\n")
    with provider.use(prov), provider.use_stack(stack), pipeline.run_ledger():
        if args.full:
            res = teardown.generate_full(args.idea, mock=False)
            print("\n=== ARTIFACTS ===\n" + res["artifacts_md"])
        else:
            res = teardown.generate(args.idea, mock=False, on_progress=lambda ln: print("  " + ln))
            print("\n=== GRADED ROWS ===")
            for r in res["rows"]:
                tag = " [STALE]" if r.get("stale") else ""
                print(f"  [{r['mark']}]{tag} {r['text']}  <{r['url']}>\n      ({r['note']})")
            print(f"\n  stats={res['stats']}")
        print(f"  cost=${res['cost']:.4f}")


def cmd_reset(args):
    db = Path(os.environ["FILG_DB"])
    if db.exists():
        db.unlink()
        print(f"wiped {db}")
    else:
        print(f"(no DB at {db})")


def cmd_state(args):
    store, usage = _store(), _usage()
    s = store.plan_get(args.sid)
    if not s:
        print(f"no session {args.sid}")
        return
    verdict = (s.get("vetting") or {}).get("verdict")
    tree = s.get("tree") or {}
    user = s.get("user")
    snap = usage.snapshot()
    print(f"sid={args.sid}")
    print(f"  status={s.get('status')} step={s.get('step')} verdict={verdict} files={len(s.get('files') or {})}")
    print(f"  tree.active={tree.get('active')} nodes={len(tree.get('nodes') or {})}")
    print(f"  user={user} tier={store.account_tier(_norm(user or '')) or 'free'} "
          f"free_used={usage.free_used(_norm(user or ''))}")
    print(f"  pdf_unlocked={store.has_purchased(_norm(user or ''))}")
    print(f"  today_spend=${snap['today_spend']:.2f} / ${snap['daily_budget']:.0f} budget")


def cmd_kill(args):
    store = _store()
    s = store.plan_get(args.sid)
    if not s:
        print(f"no session {args.sid}")
        return
    store.plan_save(args.sid, vetting={**(s.get("vetting") or {}), "verdict": "kill",
                                       "biggest_risk": "no real skill, asset, or buyer named",
                                       "reaction": "There isn't an idea here to build on yet."})
    print(f"{args.sid} → verdict=kill (the coaching ladder + 'Build it anyway' will show)")


def cmd_unkill(args):
    store = _store()
    s = store.plan_get(args.sid)
    if not s:
        print(f"no session {args.sid}")
        return
    store.plan_save(args.sid, vetting={**(s.get("vetting") or {}), "verdict": "pursue"})
    print(f"{args.sid} → verdict=pursue")


def cmd_spend(args):
    usage = _usage()
    usage.record_spend(float(args.amount))
    snap = usage.snapshot()
    print(f"today_spend now ${snap['today_spend']:.2f} / ${snap['daily_budget']:.0f} "
          f"(kill_switch_tripped={usage.kill_switch_tripped()})")


def cmd_usage(args):
    import json
    print(json.dumps(_usage().snapshot(), indent=2))


def cmd_plan(args):
    """Set (or clear) an account's subscription tier locally — test the tier gating / fair-use meter /
    PDF-free-for-subscribers without Stripe. `plan` = pro|ultimate (legacy starter/studio fold in), or free|none to cancel."""
    store = _store()
    norm = _norm(args.email)
    if args.plan in ("free", "none", "cancel"):
        store.cancel_subscription(norm)
    else:
        store.set_subscription(norm, tier=args.plan, status="active",
                               stripe_customer_id="dev", stripe_subscription_id="dev",
                               current_period_end=None)
    print(f"{args.email} (→ {norm}) → tier={store.account_tier(norm)}")


def cmd_models(args):
    """Show the model catalog (ids/prices/rungs/slugs + logical-slot resolution). --check does the
    cached Anthropic Models API availability pass + lists any uncatalogued (newly-shipped) ids."""
    import json
    from engine import model_catalog
    print(json.dumps(model_catalog.snapshot(check_availability=args.check), indent=2))


def cmd_account(args):
    """Inspect ONE identity's gating state — the 'why am I (not) being walled?' answer. The key wall
    lifts for a subscriber OR a saved BYOK key OR when keys are off (dev). A free user (keys on, no tier,
    no key) gets one welcome plan then a key prompt on every later step."""
    from datetime import datetime, timezone
    store, usage = _store(), _usage()
    from app import keys
    norm = _norm(args.email)
    raw = (args.email or "").strip().lower()   # keys are stored on the raw-lower email; subs on the alias-norm
    tier = store.account_tier(norm)
    acct = store.account_get(norm) or {}
    meta = keys.key_meta(raw)
    key_state = f"{meta['provider']} …{meta['last4']}" if meta else "none"
    period = acct.get("current_period_end") or datetime.now(timezone.utc).strftime("%Y-%m")
    mu = usage.monthly_usage(norm, period)
    walled = keys.enabled() and not meta and not tier
    print(f"{args.email} (→ {norm})")
    print(f"  tier={tier or 'free'} status={acct.get('status')} period_end={acct.get('current_period_end')}")
    print(f"  saved_key={key_state}  keys.enabled={keys.enabled()}")
    print(f"  monthly_usage=${(mu.get('spend') or 0):.3f} / {mu.get('tokens') or 0} tok  (period={period})")
    print(f"  wall on next step? {'YES — free user, key-prompted after the welcome plan' if walled else 'NO — subscriber, saved key, or keys off (dev)'}")


def cmd_key(args):
    """Inspect or clear a saved BYOK key for an identity (so you can flip one email between
    free / BYOK without juggling addresses). `key <email>` shows it; `key <email> --clear` removes it."""
    from app import keys
    raw = (args.email or "").strip().lower()
    if args.clear:
        keys.delete_key(raw)
        print(f"{args.email} → saved key cleared (has_key={keys.has_key(raw)})")
    else:
        meta = keys.key_meta(raw)
        print(f"{args.email} → {('%s …%s' % (meta['provider'], meta['last4'])) if meta else 'no saved key'}")


def cmd_buy(args):
    store = _store()
    norm = _norm(args.email)
    store.record_purchase(norm, stripe_session="dev", amount_cents=3500)
    print(f"{args.email} (→ {norm}) → pdf_unlocked={store.has_purchased(norm)} "
          "(the polished PDF now downloads; raw export was always free)")


def main():
    import argparse
    p = argparse.ArgumentParser(description="FILG local dev/test harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve")
    sp.add_argument("--real", action="store_true", help="run the actual engine (BYOK key in the UI), not mock")
    sp.set_defaults(fn=cmd_serve)
    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    sub.add_parser("usage").set_defaults(fn=cmd_usage)
    a = sub.add_parser("engine", help="run the REAL engine on an idea against your own env key")
    a.add_argument("idea")
    a.add_argument("--full", action="store_true", help="run the full artifact synth (generate_full)")
    a.add_argument("--stack", default=None, help="model stack key (default the-work-horse)")
    a.set_defaults(fn=cmd_engine)
    for name in ("state", "kill", "unkill"):
        a = sub.add_parser(name)
        a.add_argument("sid")
        a.set_defaults(fn={"state": cmd_state, "kill": cmd_kill, "unkill": cmd_unkill}[name])
    a = sub.add_parser("spend")
    a.add_argument("amount")
    a.set_defaults(fn=cmd_spend)
    a = sub.add_parser("plan", help="set an account's subscription tier (pro|ultimate|free)")
    a.add_argument("email")
    a.add_argument("plan")
    a.set_defaults(fn=cmd_plan)
    a = sub.add_parser("models", help="show the model catalog (--check hits the cached Models API)")
    a.add_argument("--check", action="store_true", help="cached Anthropic Models API availability pass")
    a.set_defaults(fn=cmd_models)
    a = sub.add_parser("account", help="inspect an identity's gating state (tier / saved key / monthly usage)")
    a.add_argument("email")
    a.set_defaults(fn=cmd_account)
    a = sub.add_parser("key", help="show or clear an identity's saved BYOK key")
    a.add_argument("email")
    a.add_argument("--clear", action="store_true", help="remove the saved key")
    a.set_defaults(fn=cmd_key)
    a = sub.add_parser("buy")
    a.add_argument("email")
    a.set_defaults(fn=cmd_buy)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
