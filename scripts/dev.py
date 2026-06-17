#!/usr/bin/env python3
"""
FILG local dev/test harness — be any user-type against a local SQLite DB (no staging, no prod backdoor).

Everything that distinguishes a user-type (fresh taste / post-taste / kill-gated / budget-exhausted /
BYOK / entitlement) is just config + DB state, so locally you can set it directly instead of baking a
test-mode into production code. Point the app AND this CLI at the same DB via FILG_DB (see TESTING.md),
then drive states from here.

  python scripts/dev.py serve                 # run the app locally (mock, local DB)
  python scripts/dev.py reset                 # wipe the local DB → fresh everything
  python scripts/dev.py state <sid>           # inspect a plan's gating-relevant state
  python scripts/dev.py kill <sid>            # force verdict=kill → exercise the coaching ladder
  python scripts/dev.py unkill <sid>          # back to 'pursue'
  python scripts/dev.py spend <amount>        # add to today's spend → test the daily kill-switch / degrade
  python scripts/dev.py usage                 # show today_spend / daily_budget / free-run counters
  python scripts/dev.py plan <email> <plan>   # set an account's entitlement (free|byok|pro)
  python scripts/dev.py buy <email>           # grant the one-time $35 PDF unlock → test the polished export
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("FILG_DB", str(ROOT / ".dev" / "filg.db"))
sys.path.insert(0, str(ROOT / "app"))         # store.py
sys.path.insert(0, str(ROOT / "prototype"))   # usage.py


def _store():
    import store
    store.init()
    return store


def _usage():
    import usage
    return usage


def _norm(email):
    """Normalize an email the way the app dedupes the free taste + PDF unlock (alias collapse)."""
    import auth
    return auth.normalize_email(email)


def cmd_serve(args):
    os.environ.setdefault("FILG_MOCK", "1")
    Path(os.environ["FILG_DB"]).parent.mkdir(parents=True, exist_ok=True)
    print(f"FILG_DB={os.environ['FILG_DB']}  FILG_MOCK={os.environ['FILG_MOCK']}")
    print("→ http://localhost:8000  (set FILG_KEY_SECRET to exercise the BYOK wall; see TESTING.md)")
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--reload", "--app-dir", str(ROOT), "--port", "8000"])


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
    print(f"  user={user} plan={store.account_plan(user)} free_used={usage.free_used(_norm(user or ''))}")
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
    store = _store()
    store.set_account_plan(args.email, args.plan)
    print(f"{args.email} → plan={store.account_plan(args.email)}")


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
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("reset").set_defaults(fn=cmd_reset)
    sub.add_parser("usage").set_defaults(fn=cmd_usage)
    for name in ("state", "kill", "unkill"):
        a = sub.add_parser(name)
        a.add_argument("sid")
        a.set_defaults(fn={"state": cmd_state, "kill": cmd_kill, "unkill": cmd_unkill}[name])
    a = sub.add_parser("spend")
    a.add_argument("amount")
    a.set_defaults(fn=cmd_spend)
    a = sub.add_parser("plan")
    a.add_argument("email")
    a.add_argument("plan")
    a.set_defaults(fn=cmd_plan)
    a = sub.add_parser("buy")
    a.add_argument("email")
    a.set_defaults(fn=cmd_buy)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
