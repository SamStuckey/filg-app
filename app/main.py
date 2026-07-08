#!/usr/bin/env python3
"""
FILG's route surface — the FastAPI app over the layered backend (see app/README.md).

This module holds ROUTES, WALLS, and BACKGROUND WORKERS only. The logic lives in its layers:
ops.py (how an engine op runs — slots, guards, metering), access.py (entitlements + provider
selection), views.py (session → frontend shaping), exports.py (free text exports), domain/
(the use-case plug), and the engine package (research, grading, the decision tree).

The funnel: /api/brainstorm (anonymous spread) → /merge (converge + light skim) → /refine →
/commit (THE WALL: account + key/subscription; the deep build) → /next /back /redraft /goto
(the staged tree build) — plus chat routing, the board, research lookups, exports, billing.

Run:
  FILG_MOCK=1 uvicorn app.main:app --reload        # dev: canned results, no spend
  uvicorn app.main:app                              # real runs, metered
Env: FILG_MOCK, FILG_FREE_RUNS, FILG_DAILY_BUDGET, FILG_ANTHROPIC_API_KEY (hosted key),
     FILG_KEY_SECRET (BYOK store), SUPABASE_URL/SUPABASE_PUBLISHABLE_KEY (auth),
     STRIPE_*/FILG_PUBLIC_URL (billing), FILG_ORPHAN_TTL_HOURS.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

import markdown
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool  # long engine calls must not block the event loop

# the engine package (research/grading/metering) + the app-side skill/persona/board layer.
_APP_DIR = Path(__file__).resolve().parent
from engine import usage     # noqa: E402
from app import personas  # noqa: E402 — advisor/director registry (shared by ask-an-expert + the board)
from app import board     # noqa: E402 — Board of Directors orchestration
from app import director_forge  # noqa: E402 — forge a custom Board director from a description (distill→draft→QA)
from app import gibberish  # noqa: E402 — pre-LLM "is this even an idea?" gate (saves a run, hands back a roast)
from app import intake     # noqa: E402 — shape + vet (the kill-gate); /revet re-runs it after added substance
from app import brainstorm # noqa: E402 — diverge/merge: the top of the funnel (1-3 directions → one refined idea)
from app import router     # noqa: E402 — the single prompt box (intent routing) + the pivot-fork integration gate
from app import plan_pdf   # noqa: E402 — styled PDF generation (synthesis + fpdf2 render)
from app import advisor    # noqa: E402 — "chat with your plan" (grounded advisory layer)
from app import context    # noqa: E402 — THE CONTEXT ENGINE: every model-facing view of session state
from app import decisions as decisions_mod  # noqa: E402 — standing decisions: axioms/non-negotiables (Summary tab)
from app.domain import tasks as tasks_mod  # noqa: E402 — execution layer: the Roadmap + Codex (tasks/milestones/goals/artifacts)
from app import skeptic    # noqa: E402 — adversarial assumption-checking on the live research path
from engine import provider   # noqa: E402 — BYOK: per-run LLM provider (FILG's key vs a user's OpenRouter key)
from engine import pipeline   # noqa: E402 — engine: per-run cost ledger (run_ledger) for safe concurrency
from engine import model_catalog  # noqa: E402 — model ids/prices/slugs + cached Models API availability
from engine import tree as dtree  # noqa: E402 — the decision tree: node wiring, kinds, attachments, run epochs

from . import auth, billing, exports, keys, ops, planner, render, store, tiers, views  # noqa: E402
from .domain import help as domain_help  # noqa: E402 — generated help copy (pricing facts) — persistence, auth, billing, keys, tiers, share-page HTML
# Entitlement/provider/metering decisions live in access.py (the pure service layer). Re-exported here
# so the route bodies call them as bare names and `main._budget` etc. stay importable by tests.
from .access import (  # noqa: E402,F401 — entitlement/provider/metering (the pure service layer)
    _acct, _tier, _is_subscriber, _budget, _feature_ok, _has_pdf_access,
    _key_provider_kind, _build_provider, _provider_for, _meter, _needs_key)

# One startup line so a local run never has to guess its wiring (the #1 source of confusing 500s is a
# hosted key that didn't reach the process env).
print(f"[filg] mock={'ON (canned, no spend)' if ops.MOCK else 'off (REAL runs)'}"
      f" · hosted key={'wired' if ops.HOSTED_FREE else 'MISSING (free/anon runs will fail in real mode)'}"
      f" · BYOK store={'on' if keys.enabled() else 'off (no FILG_KEY_SECRET)'}"
      f" · auth={'on' if auth.AUTH_ENABLED else ('dev as ' + os.environ.get('FILG_DEV_EMAIL', '(anonymous)'))}")


def _key_wall(session: dict):
    """402 if the session owner must bring a key before this (API-calling) action; else None.
    Past the free taste, every engine call needs the user's own key OR an active subscription."""
    if _needs_key(session.get("user")):
        return JSONResponse(
            {"error": "Keep building free on your own API key (OpenRouter or Anthropic), or "
                      "subscribe to run on ours.",
             "needKey": True}, status_code=402)
    return None


def _account_wall(request: Request, session: dict):
    """401 if auth is ON and this engine action has no verified account behind it; else None.

    THE WALL (2026-07-06): the free taste — initial prompt → direction spread → direction select →
    the merged first idea with its first-pass research — runs ANONYMOUS, on the house. The next click
    (commit = the deep build) and everything past it requires an account: sign up, then either bring
    your own key (free BYOK) or subscribe. A signed-in user touching an ownerless (anonymous-taste)
    plan CLAIMS it here, so the plan they tasted joins the account they just created; an unclaimed
    plan is purged after ~48h (store.purge_orphan_plans)."""
    if not auth.AUTH_ENABLED:
        return None
    authed = auth.user_from_request(request)
    if authed and authed["email"]:
        if not (session.get("user") or "").strip():   # their anonymous taste → claim it into the account
            store.plan_claim(session["id"], authed["email"])
            session["user"] = authed["email"]
        return None
    return JSONResponse(
        {"error": "Create a free account to keep building — this plan saves to it. Then bring your "
                  "own API key (free) or subscribe to run on ours.",
         "needAccount": True}, status_code=401)


def _kill_gate(session: dict):
    """422 if the session's idea was graded `kill` — the builder must not roll forward into a full
    plan built on nothing. The operator clears it via /revet (add a real skill/asset/buyer). Returns
    the gate payload (the vet's clarifying question + risk) or None when the verdict isn't a kill."""
    v = session.get("vetting") or {}
    if (v.get("verdict") or "pursue") != "kill":
        return None
    sh = session.get("shaped") or {}
    return JSONResponse({"needSubstance": True, "verdict": "kill",
        "question": sh.get("clarifying_question")
            or "Name one real skill, asset, or audience you already have, and who would pay for it.",
        "risk": v.get("biggest_risk") or "", "reaction": v.get("reaction") or ""}, status_code=422)


app = FastAPI(title="FILG")

# The SPA's CSS/JS are served as real static files from app/web/static (extracted out of this module
# so the display layer is editable with front-end tooling). index.html is read + templated in Python
# (see _render_page) because it carries the __FILG_HEAD__ config hook.
_WEB_DIR = _APP_DIR / "web"
app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

# Anonymous free-taste plans that never got an account are scrapped after ~48h — the wall's promised
# cleanup ("no account → the plan is lost"). One sweep at boot, then a slow background loop; both are
# best-effort (a missed sweep just waits for the next one). Env-tunable without a deploy.
ORPHAN_TTL_HOURS = int(os.environ.get("FILG_ORPHAN_TTL_HOURS", "48"))
_ORPHAN_SWEEP_SECS = 6 * 3600


def _orphan_sweep_loop() -> None:
    while True:
        try:
            n = store.purge_orphan_plans(ORPHAN_TTL_HOURS)
            if n:
                print(f"[filg] purged {n} account-less plan(s) older than {ORPHAN_TTL_HOURS}h")
        except Exception:  # noqa: BLE001 — a failed sweep must never take the app down
            traceback.print_exc()
        time.sleep(_ORPHAN_SWEEP_SECS)


threading.Thread(target=_orphan_sweep_loop, daemon=True).start()

@app.get("/api/me")
async def api_me(request: Request):
    """Tell the frontend who it is and whether the polished PDF is already unlocked."""
    authed = auth.user_from_request(request)
    if not authed:
        return {"signed_in": False, "auth_enabled": auth.AUTH_ENABLED,
                "pdf_billing": billing.PDF_BILLING_ENABLED,
                "pdf_price": billing.PDF_PRICE_CENTS,
                "sub_enabled": billing.PDF_BILLING_ENABLED, "tiers": tiers.catalog()}
    tier = _tier(authed["email"])
    return {"signed_in": True, "email": authed["email"],
            "pdf_unlocked": _has_pdf_access(authed["email"]),   # comp grant OR subscription → unlimited
            "pdf_credits": billing.credits_left(authed["email"]),   # paid plan-unlock credits remaining
            "pdf_billing": billing.PDF_BILLING_ENABLED, "pdf_price": billing.PDF_PRICE_CENTS,
            "auth_enabled": auth.AUTH_ENABLED,
            # subscription state for the fork UI + usage meter
            "sub_enabled": billing.PDF_BILLING_ENABLED, "tiers": tiers.catalog(),
            "tier": tier, "tier_label": tiers.label(tier) if tier else None,
            "subscription": _budget(authed["email"], tier)}


@app.post("/api/plan/{sid}/buy-pdf")
async def api_buy_pdf(sid: str, request: Request):
    """Start the $13 Checkout that unlocks THIS plan's clean PDF (re-download free forever). Requires
    a signed-in owner of a finished plan. If the plan is already unlocked (or there's a comp credit
    to spend on it), no payment is needed — the client just downloads. Raw export stays free."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.PDF_BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    # Already accessible (comped or already unlocked), or there are credits left → no payment needed.
    if _has_pdf_access(authed["email"], ops.plan_key(s)) or billing.credits_left(authed["email"]) > 0:
        return JSONResponse({"error": "You can download this plan already.", "unlocked": True},
                            status_code=409)
    try:
        url = billing.create_pdf_checkout_url(authed["email"], user_id=authed["id"], plan_id=sid,
                                              plan_key=ops.plan_key(s))
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.post("/api/subscribe")
async def api_subscribe(request: Request):
    """Start a MONTHLY subscription Checkout for a paid tier (Starter/Pro/Studio). Signed-in only.
    Returns the hosted Stripe URL; the webhook activates the account on completion."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    if not billing.PDF_BILLING_ENABLED:
        return JSONResponse({"error": "Billing isn't configured yet."}, status_code=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    tier = tiers.canonical((body.get("tier") or "").strip())
    if not tiers.purchasable(tier):   # hidden tiers stay honored on accounts but are never sold
        return JSONResponse({"error": "Unknown plan."}, status_code=400)
    if _tier(authed["email"]) == tier:
        return JSONResponse({"error": f"You're already on {tiers.label(tier)}.", "current": True},
                            status_code=409)
    try:
        url = billing.create_subscription_checkout_url(
            authed["email"], tier=tier, price_cents=tiers.price_cents(tier),
            label=tiers.label(tier), user_id=authed["id"])
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.post("/api/subscription/portal")
async def api_subscription_portal(request: Request):
    """Open a Stripe billing-portal session (update card / cancel). Signed-in subscriber only."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    acct = store.account_get(_acct(authed["email"])) or {}
    cid = acct.get("stripe_customer_id")
    if not cid:
        return JSONResponse({"error": "No subscription to manage."}, status_code=400)
    try:
        url = billing.create_portal_url(cid)
    except billing.StripeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return {"url": url}


@app.get("/api/plan/{sid}/nudges")
async def api_plan_nudges(sid: str, request: Request):
    """Per-step quick-edit chips for the feedback modal — short, business + current-section specific.
    One cheap call on the owner's key; the frontend caches them per node."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    p = s.get("proposal") or {}
    draft, section = p.get("draft") or "", p.get("section")
    if not draft or not section:
        return {"chips": []}
    idea = planner._working_idea(s)
    try:
        with ops.run_slot(ops.slot_user(s), s.get("stack")):
            chips, cost = planner.nudges(idea, section, draft, mock=ops.MOCK)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return {"chips": []}   # nudges are a nicety — never block the modal on them
    nc, nt = ops.fold_usage(sid, s, cost, toks)
    return {"chips": chips, "cost": nc, "tokens": nt}


def _validate_key(provider_name: str, api_key: str) -> tuple[bool, str]:
    """One cheap call confirms a BYOK key works before we store it. Skipped in mock mode (no spend)."""
    if provider_name not in keys.PROVIDERS:
        return False, "Unsupported provider."
    if len(api_key) < 8:
        return False, "That doesn't look like an API key."
    if ops.MOCK:
        return True, "ok (mock)"
    try:
        from engine import provider as prov_mod
        from engine import pipeline
        with prov_mod.use(_build_provider(provider_name, api_key)):
            out = pipeline.call("key_validate", pipeline.HAIKU, "Reply with: OK", max_tokens=5)
        return (True, "ok") if out else (False, "The key didn't return a response.")
    except Exception as e:  # noqa: BLE001
        msg, _ = ops.humanize_error(e)
        if msg.startswith("Something went wrong"):
            # validation is the user debugging their OWN key — surface a trimmed real reason
            raw = " ".join(str(e).split())[:180]
            msg = f"That key didn't validate: {raw}"
        return False, msg


@app.get("/api/key")
async def api_key_get(request: Request):
    """BYOK status for the current user: whether the feature is on, plus any saved key (masked)."""
    if not keys.enabled():
        return {"enabled": False}
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return {"enabled": True, "signed_in": False, "key": None}
    return {"enabled": True, "signed_in": True, "key": keys.key_meta(authed["email"]),
            "providers": list(keys.PROVIDERS)}


@app.post("/api/key")
async def api_key_save(request: Request):
    """Validate a user's API key with one cheap call, then store it ENCRYPTED. Requires sign-in."""
    if not keys.enabled():
        return JSONResponse({"error": "BYOK isn't configured yet."}, status_code=503)
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    body = await request.json()
    api_key = (body.get("key") or "").strip()
    sel = (body.get("provider") or "").strip()    # the provider the user picked in the modal
    provider_name = sel if sel in keys.PROVIDERS else _key_provider_kind(api_key)  # fall back to prefix
    ok, why = _validate_key(provider_name, api_key)
    if not ok:
        return JSONResponse({"error": why}, status_code=400)
    try:
        meta = keys.save_key(authed["email"], provider_name, api_key)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "key": meta}


@app.post("/api/key/remove")
async def api_key_remove(request: Request):
    """Forget a user's stored key."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    keys.delete_key(authed["email"])
    return {"ok": True}


@app.delete("/api/account")
async def api_delete_account(request: Request):
    """Permanently delete the signed-in user's account: all their projects, PDF purchases/unlocks, and
    their stored BYOK key. Irreversible. (Supabase identity itself is managed by Supabase; this clears
    everything FILG holds for them.)"""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first."}, status_code=401)
    store.delete_account(authed["email"], auth.normalize_email(authed["email"]))
    try:
        keys.delete_key(authed["email"])
    except Exception:  # noqa: BLE001 — key store is best-effort; account data is already gone
        pass
    return {"ok": True}


@app.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request):
    """Stripe → us: flip subscription state. Verifies the signature before trusting the body."""
    payload = await request.body()
    event = billing.verify_webhook(payload, request.headers.get("stripe-signature", ""))
    if event is None:
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    billing.handle_event(event)
    return {"received": True}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mock": ops.MOCK, "auth_enabled": auth.AUTH_ENABLED,
            "pdf_billing": billing.PDF_BILLING_ENABLED, "sub_enabled": billing.PDF_BILLING_ENABLED,
            "models": model_catalog.snapshot()["slots"], **usage.snapshot()}


@app.get("/api/models")
async def api_models(request: Request):
    """The model catalog: each id with its rung / price / OpenRouter slug, the current logical-slot
    resolution, and — with ?check=1 — a cached Anthropic Models API availability pass plus any ids the
    API reports that we don't yet catalog (the 'a new model shipped, go place + price it' signal).
    Public + read-only; ids/prices/rungs aren't secret. Availability is cached (FILG_MODELS_TTL)."""
    check = request.query_params.get("check") in ("1", "true", "yes")
    return model_catalog.snapshot(check_availability=check)


@app.get("/p/{sid}", response_class=HTMLResponse)
async def share_plan(sid: str):
    """Public, read-only view of a plan the owner explicitly shared (private by default). Carries the
    two things nothing else in-market shows: the graded receipts and the decision path that led here —
    the share page IS the pitch, watermarked with the maker line."""
    s = store.plan_get(sid)
    if not s or not s.get("shared"):
        return HTMLResponse(render.not_found("This plan isn't shared or doesn't exist."), status_code=404)
    idea = planner._working_idea(s)
    inner = markdown.markdown(planner.bundle_markdown(idea, s.get("files") or {}), extensions=["extra"])
    title = ((s.get("shaped") or {}).get("thesis") or s["idea"] or "Shared business plan")[:120]
    rows = ((s.get("research") or {}).get("rows") or [])
    return HTMLResponse(render.shared_plan_page(title, inner, receipts=rows, path=views.share_path(s)))


# ── Interactive plan builder (idea → decision tree → downloadable file tree) ──
def _identity(request: Request, body_email: str | None = None) -> tuple[str, bool]:
    """Resolve (user, verified) — a verified Supabase user wins; else the body email (free tier)."""
    authed = auth.user_from_request(request)
    if authed and authed["email"]:
        return authed["email"], True
    return (body_email or "").strip().lower(), False


def _owns(request: Request, session: dict) -> bool:
    """Whether the requester may access this plan session. When auth is OFF (free/dev), the id is the
    capability and there's no identity to enforce, so allow. When auth is ON, a session owned by a
    verified email is private: require the requester's verified email to match. Ownerless legacy/anon
    sessions stay open. Callers return 404 (not 403) on failure so a plan's existence doesn't leak."""
    if not auth.AUTH_ENABLED:
        return True
    owner = (session.get("user") or "").strip().lower()
    if not owner:
        return True
    authed = auth.user_from_request(request)
    return bool(authed and (authed["email"] or "").strip().lower() == owner)


# Node bookkeeping (id/parent/children) and kind resolution each have one owner.
_new_node = dtree.new_node
_kind = context.kind


def _regrade_setup(s: dict, node: dict, cost: float) -> tuple[dict | None, float]:
    """When the SETUP (step-0) section is reframed, re-grade the idea against the new angle so the
    PURSUE/PIVOT verdict + reaction track it. Only the setup stage triggers a re-grade. Must run on the
    bound provider (call inside `_run_slot`). Returns (new_vetting_or_None, cost_including_revet). A
    re-grade failure never breaks the underlying redraft/back."""
    if (node or {}).get("step") != 0:
        return None, cost
    try:
        vetting, vc = intake.vet(planner._working_idea(s), s.get("shaped") or {}, s.get("research"),
                                 mock=ops.MOCK, angle=(node.get("draft") or ""))
    except Exception:  # noqa: BLE001
        return None, cost
    return vetting, round(cost + vc, 4)


# ── The diverge/converge funnel (brainstorm → merge → commit), layered on the same node tree ──
# Per-node history bounds (board=12, log=40) live in the attachment registry (app/domain/nodes.py).
_op_log = dtree.op_log   # one op's progress slice, sentinel-free + bounded, persisted on its node


def _attach_spread(tree: dict, diverge: dict, parent: str | None,
                   board: list | None = None) -> str:
    """Attach a `brainstorm` fork node + one `option` child per direction into `tree`, and land
    the active pointer on the fork (the node being decided). Returns the fork's id. `board` seeds
    the fork and its options with the pivot point's board history (a spread's options branch from
    the FORK, so they inherit through it)."""
    b = dtree.attach(tree, _new_node(
        {"kind": "brainstorm", "step": 0, "title": "A few directions",
         "spread": diverge.get("spread"), "draft": None, "files": {}, "history": [],
         # a declared set-aside (part of the ask the engine declined, with its reason) is
         # SURFACED, never silent — it rides the node so every view can show it
         "set_aside": diverge.get("set_aside"),
         "board": dtree.clip("board", board or [])}, parent))
    for d in (diverge.get("directions") or []):
        dtree.attach(tree, _new_node(
            {"kind": "option", "step": 0, "title": d.get("title") or "Direction",
             "direction": d, "draft": d.get("one_liner"), "files": {}, "history": []},
            b["id"]), activate=False, inherit=True)
    tree["active"] = b["id"]
    return b["id"]


def _refined_node(m: dict, selected: list, parent: str | None, feedback: str | None = None) -> dict:
    """The reconciled single idea (+ the adversarial cull + a light research skim + any clarifying
    questions) as one `refined` node, a child of the brainstorm fork — or, for a /refine re-merge,
    a child of the refined node it sharpens. `selected` records which option ids fed the merge;
    `feedback` is the operator's clarification a refine folded in (visible on the node forever)."""
    return _new_node({"kind": "refined", "step": 0, "title": "Refined idea", "thesis": m["thesis"],
                      "founder_edge": m.get("founder_edge"), "mold": m.get("mold"),
                      "kept": m.get("kept"), "dropped": m.get("dropped"), "research": m.get("research"),
                      "questions": m.get("questions") or [], "feedback": feedback or None,
                      "selected": selected, "draft": m["thesis"], "files": {}, "history": [],
                      "board": []}, parent)


def _fresh_tree_or_abandon(sid: str, tok: str | None):
    """Run-epoch check for a finishing background run: re-read the session and return
    (session, tree) to attach into — the FRESH tree, so nothing written mid-run is clobbered.
    If the user moved on (pivoted / started another run: the tree's `_run` token changed),
    return None and mark the abandonment in the progress log. The in-flight spend is already
    metered; only the RESULT is discarded — the user's newer state always wins."""
    s1 = store.plan_get(sid) or {}
    tree = s1.get("tree") or {}
    if not dtree.run_is_current(tree, tok):
        prog = list(s1.get("progress") or []) + ["✂ run abandoned — you moved on before it finished"]
        store.plan_save(sid, progress=prog)
        return None
    return s1, tree


def _run_merge(sid: str, option_ids: list, user: str, tok: str | None = None,
               refine_of: str | None = None, note: str | None = None) -> None:
    """Background: reconcile the chosen directions into one refined idea (+ light research skim), then
    attach a `refined` node under the brainstorm fork and advance the active pointer to it.
    `refine_of` + `note` = the /refine re-merge: fold the operator's clarification (answers to the
    gate's questions, a steer) into a re-reconcile of the SAME picks, and chain the new refined node
    under the one it sharpens — the refinement lineage stays visible in the graph."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        nodes0 = (s0.get("tree") or {}).get("nodes") or {}
        directions = [(nodes0.get(i) or {}).get("direction") for i in option_ids]
        directions = [d for d in directions if d]
        idea_in = s0["idea"]
        if refine_of and note:
            prev = ((nodes0.get(refine_of) or {}).get("thesis") or "").strip()
            idea_in = (f"{s0['idea']}\n\nTHE MERGED THESIS SO FAR (refine THIS, do not start over):\n"
                       f"{prev}\n\nTHE OPERATOR'S CLARIFICATION — it outweighs everything above; fold "
                       f"it in and do not re-ask what it answers:\n{note}")
        _decisions_receipt(s0, progress)          # the axioms this merge honors, in the receipts
        with ops.bind_session_run(user, s0.get("stack")) as prov:
            m, cost = brainstorm.merge(idea_in, directions, mock=ops.MOCK,
                                       on_progress=ops.bg_progress(sid, base_cost, base_tokens, progress),
                                       decisions=_decisions_block(s0))
            toks = pipeline.LEDGER.tokens()
        ops.meter_bg(user, prov, cost, toks)
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        # a refine chains under the refined node it sharpens (if it survived the wait); a first
        # merge joins its picks under the brainstorm fork
        parent = (refine_of if refine_of and nodes.get(refine_of) else tree.get("active"))
        refined = _refined_node(m, [] if refine_of else option_ids, parent, feedback=note)
        refined["log"] = _op_log(progress, len(s0.get("progress") or []))
        _stamp_decisions(s0, refined)
        dtree.attach(tree, refined, inherit=True)
        store.plan_save(sid, status="building", stage="refined", tree=tree, progress=progress,
                        cost=round(base_cost + cost, 4), tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=ops.humanize_error(e)[0])


def _deep_build(sid: str, thesis: str, user: str, tok: str | None = None,
                picks: list | None = None, prior: dict | None = None) -> None:
    """Background: the COMMIT step — the one deep research run + first section draft, attached to the
    tree under the active (refined) node so the funnel history is preserved. Same engine as the legacy
    welcome run, but it grows the existing tree instead of reseeding a fresh root. `picks` = option
    ids a direct brainstorm-commit chose: recorded as `selected` on the built node so the graph draws
    the join through them (the choice is part of the story, not just its text). `prior` (T2) = the
    refined node's carried merge-skim payload; when the committed thesis is unchanged, prepare reuses
    those already-fetched claims (re-graded at full strength) instead of re-running the web fan-out."""
    try:
        s0 = store.plan_get(sid) or {}
        base_cost, base_tokens = s0.get("cost") or 0, s0.get("tokens") or 0
        progress = list(s0.get("progress") or [])
        _decisions_receipt(s0, progress)          # the axioms this build honors, in the receipts
        with ops.bind_session_run(user, s0.get("stack")) as prov:
            prep = planner.prepare(thesis, mock=ops.MOCK, prior=prior,
                                   on_progress=ops.bg_progress(sid, base_cost, base_tokens, progress),
                                   decisions=_decisions_block(s0))
            toks = pipeline.LEDGER.tokens()
        ops.meter_bg(user, prov, prep["cost"], toks, is_run=True, research_cost=prep["research_cost"])
        fresh = _fresh_tree_or_abandon(sid, tok)
        if fresh is None:
            return                                # the user pivoted mid-run — their newer state wins
        _s1, tree = fresh
        nodes = tree.get("nodes") or {}
        root = _new_node(planner.root_node(prep["proposal"]), tree.get("active"))  # a plain section node (kind absent)
        if picks:
            root["selected"] = [i for i in picks if i in nodes]   # the join the graph rides through
        root["log"] = _op_log(progress, len(s0.get("progress") or []))
        _stamp_decisions(s0, root)
        dtree.attach(tree, root, inherit=True)
        store.plan_save(sid, status="building", stage="building", research=prep["research"], step=0,
                        proposal=prep["proposal"], shaped=prep["shaped"], vetting=prep["vetting"],
                        tree=tree, progress=progress, cost=round(base_cost + prep["cost"], 4),
                        tokens=base_tokens + toks)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, status="error", error=ops.humanize_error(e)[0])


# ── The context engine (app/context.py) owns every model-facing view of session state. These
# aliases keep main.py's historical names; DO NOT grow new context strings here — add to the
# engine's renderers/views so every consumer inherits the change (see tests/test_context.py).
_node_snippet = context.snippet
_journey_digest = context.journey
_route_context = context.screen


def _path_snippets(nodes: dict, at_id: str | None) -> list[str]:   # legacy signature shim
    return context.path({"tree": {"nodes": nodes}}, at_id)


# ── Standing decisions (app/decisions.py) — the operator's pinned axioms ──────
# One renderer (the context engine's view) feeds every injection site; `None` when nothing is pinned
# so downstream prompts stay byte-identical for sessions without decisions.
def _decisions_block(s: dict) -> str | None:
    return context.decisions_block(s) or None


def _stamp_decisions(s: dict, *nodes) -> None:
    """Record on each freshly built node which standing decisions were in force (ids only) — the
    reference trail decisions_mod.impact() walks when a decision is later edited/removed, and the
    frontend's 🧭 note. No decisions pinned → no field (wire compat)."""
    dids = [d["id"] for d in (s.get("decisions") or []) if d.get("id")]
    if not dids:
        return
    for n in nodes:
        if n is not None:
            n["decisions"] = dtree.clip("decisions", dids)


def _decisions_receipt(s: dict, progress: list) -> None:
    """One readable receipt line for a background op's node log: which axioms this build honored."""
    dd = decisions_mod.ordered(s.get("decisions"))
    if dd:
        names = "; ".join(d["text"][:48] for d in dd[:3]) + (" …" if len(dd) > 3 else "")
        progress.append(f"🧭 honoring {len(dd)} standing decision{'s' if len(dd) != 1 else ''}: {names}")


@app.post("/api/brainstorm")
async def api_brainstorm(request: Request):
    """Top of the funnel — ANONYMOUS, no email/login required. Spread a raw prompt into 1-3 loose,
    vetted-shape directions (pure LLM, no web, cheap) and seed the decision tree with a brainstorm fork
    + one option node per direction. This is the free, frictionless entry point."""
    body = await request.json()
    idea = (body.get("idea") or "").strip()
    if len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    if gibberish.looks_like_gibberish(idea):   # total nonsense → free roast, no run
        return JSONResponse({"gibberish": True, **gibberish.roast(idea)})
    user, _verified = _identity(request, body.get("email"))   # may be "" (anonymous) — that's allowed here
    directors = [k for k in (body.get("directors") or []) if k in personas.KEYS]
    sid = uuid.uuid4().hex[:12]
    store.plan_create(sid, user, idea, directors=directors)
    store.plan_save(sid, stack=provider.stack_name(body.get("stack")))
    def _work():   # off the event loop: other requests (node reads, polls) stay live while this thinks
        with ops.run_slot(user or f"anon:{sid}", body.get("stack")):
            d, cost = brainstorm.diverge(idea, mock=ops.MOCK)
            return d, cost, pipeline.LEDGER.tokens()
    try:
        d, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    ops.meter_bg(user, _provider_for(user), cost, toks)
    # The tree roots at a BASE `idea` node (the raw prompt). Every spread — including later pivots and
    # step-one rebuilds — branches beneath it, so alternate takes always share a common ancestor and
    # no branch is ever orphaned.
    base = _new_node({"kind": "idea", "step": 0, "title": "Your idea", "draft": idea,
                      "files": {}, "history": [], "board": []}, None)
    tree = dtree.seed(base)
    _attach_spread(tree, d, base["id"])
    store.plan_save(sid, status="building", stage="brainstorm", tree=tree,
                    cost=round(cost, 4), tokens=toks)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/rebrainstorm")
async def api_plan_rebrainstorm(sid: str, request: Request):
    """Re-spread WITHIN the same tree: a pivot, a 'show me other directions', or a 'start over but
    keep X'. Runs diverge and attaches a new brainstorm fork off the PIVOT POINT (the active node; a
    re-spread while already on a fork lands as its sibling), so the old branch stays in the graph."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    idea = (body.get("idea") or s.get("idea") or "").strip()
    feedback = (body.get("feedback") or "").strip()
    if not feedback and len(idea) < 12:
        return JSONResponse({"error": "Tell me a bit more about the idea."}, status_code=400)
    # keyboard-mash pivots get the same free roast as a mash at the landing box (no run, no LLM) —
    # the v2 chat renders it as a bot bubble instead of quietly spreading nonsense into the tree
    if gibberish.looks_like_gibberish(feedback or idea):
        return JSONResponse({"gibberish": True, **gibberish.roast(feedback or idea)})
    tree = s.get("tree") or {"nodes": {}, "active": None}
    nodes = tree.get("nodes") or {}
    # the pivot point: an explicitly named node (the one the user had open) beats the active one
    at = nodes.get((body.get("node") or "").strip()) or nodes.get(tree.get("active")) or {}
    # THE PIVOT CONTRACT: context = the pivot node + its ancestors (root → node, last item = chosen);
    # siblings/descendants dropped; the pivot feedback OUTWEIGHS all of it.
    path = _path_snippets(nodes, at.get("id")) if at else []
    path_block = "\n".join(f"- {p}" for p in path) or f"- the original idea: {s.get('idea', '')[:160]}"
    if feedback:
        div_input = (
            f"THE OPERATOR IS PIVOTING. Their pivot instruction OUTWEIGHS everything below — the new "
            f"directions must be a genuine change of course that honors it:\n{feedback}\n\n"
            f"COMMITTED PATH (root \u2192 the pivot point; treat each item, especially the LAST, as "
            f"chosen context — nothing outside this path applies):\n{path_block}")
    else:
        div_input = (f"{idea}\n\nCOMMITTED PATH (root \u2192 the pivot point; treat each item as "
                     f"chosen context):\n{path_block}")
    def _work():
        with ops.run_slot(ops.slot_user(s), s.get("stack")):
            d, cost = brainstorm.diverge(div_input, mock=ops.MOCK, decisions=_decisions_block(s))
            return d, cost, pipeline.LEDGER.tokens()
    try:
        d, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    ops.meter_bg(s.get("user"), _provider_for(s.get("user")), cost, toks)
    # every node is branchable: the new spread is a CHILD of the pivot node itself, always
    parent = at.get("id")
    # permanent evidence line — pivots are core IP, every hop must be verifiable in the server log
    print(f"[pivot] sid={sid} node_in={(body.get('node') or None)!r} resolved={at.get('id')}/"
          f"{_kind(at) if at else None} parent={parent} feedback={feedback[:80]!r}")
    tree["nodes"] = nodes
    bid = _attach_spread(tree, d, parent, board=(at or {}).get("board"))
    nodes[bid]["feedback"] = feedback or idea[:120]   # the pivot ask, visible on the fork forever
    _stamp_decisions(s, nodes[bid])                   # the axioms this spread was constrained by
    dtree.begin_run(tree)                    # pivoting abandons any run still in flight — you moved on
    ops.fold_usage(sid, s, cost, toks, tree=tree, stage="brainstorm", **views.mirror(tree))
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/merge")
async def api_plan_merge(sid: str, request: Request):
    """Converge: reconcile the checked directions into one refined idea (+ adversarial cull + a light
    research skim). Runs in the background (the skim hits the web); the frontend polls /api/plan/{sid}."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    nodes = (s.get("tree") or {}).get("nodes") or {}
    valid = [i for i in (body.get("options") or [])
             if isinstance(i, str) and _kind(nodes.get(i) or {}) == "option"]
    if not valid:
        return JSONResponse({"error": "Pick at least one direction to try."}, status_code=400)
    # The merge is the free taste's one web-touching step and runs in a background thread WITHOUT
    # _run_slot — so the daily kill switch must be read here, before the spawn (invariant #3: the
    # funnel feeds the meter, it must also read it). Subscribers are bounded by their monthly cap.
    if ops.free_pool_tapped(s.get("user")):
        return ops.engine_error(ops.DailyCapError(), 402)
    tree = s.get("tree") or {"nodes": {}, "active": None}
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="merging", tree=tree)
    threading.Thread(target=_run_merge, args=(sid, valid, s.get("user"), tok), daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/refine")
async def api_plan_refine(sid: str, request: Request):
    """Sharpen the refined idea IN PLACE: fold the operator's clarification (answers to the gate's
    questions, a steer) into a re-merge of the same picks, chained under the refined node it sharpens.
    Part of the pre-commit funnel, so it sits on the free side of the wall like /merge — and reads
    the same kill switch, for the same reason. Background; the frontend polls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    note = (body.get("note") or "").strip()
    if len(note) < 4:
        return JSONResponse({"error": "Tell me what to fold in."}, status_code=400)
    # a keyboard-mash "answer" gets the same free roast as a mash pivot (no run, no LLM)
    if gibberish.looks_like_gibberish(note):
        return JSONResponse({"gibberish": True, **gibberish.roast(note)})
    tree = s.get("tree") or {"nodes": {}, "active": None}
    nodes = tree.get("nodes") or {}
    at = nodes.get((body.get("node") or "").strip()) or nodes.get(tree.get("active")) or {}
    if _kind(at) != "refined":
        return JSONResponse({"error": "Nothing here to refine yet — pick directions and merge first."},
                            status_code=400)
    # the picks that fed this refined idea: a refine-of-a-refine walks up to the original merge's
    sel, cur = [], at
    while cur and not sel:
        sel = [i for i in (cur.get("selected") or []) if _kind(nodes.get(i) or {}) == "option"]
        cur = nodes.get(cur.get("parent")) if cur.get("parent") else None
    if ops.free_pool_tapped(s.get("user")):
        return ops.engine_error(ops.DailyCapError(), 402)
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="merging", tree=tree)
    threading.Thread(target=_run_merge, args=(sid, sel, s.get("user"), tok),
                     kwargs={"refine_of": at.get("id"), "note": note}, daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/commit")
async def api_plan_commit(sid: str, request: Request):
    """'I'm sold, build the plan' — the one deep research run + first plan page. Uses the active refined
    node's thesis (or a direction/thesis passed in the body). Background; the frontend polls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    # THE WALL lives here: the free taste ends at this click (brainstorm → merge ran on the house).
    # The deep build requires an account, then a key or a subscription (see _account_wall).
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    thesis = (body.get("thesis") or "").strip()
    tree = s.get("tree") or {}
    nodes = tree.get("nodes") or {}
    # Building from an explicitly named node (the one the user had open) grows the build out of THAT
    # node — but VALIDATE FIRST, MUTATE LAST. This used to jump the active pointer before checking
    # the thesis: a commit from a browsed brainstorm fork 400'd AND stranded `active` on the fork,
    # so every surface then described two different nodes and every retry re-failed (Sam's
    # roll-forward freeze, 2026-07-06). A rejected request must leave the plan untouched.
    # picks riding a direct commit ("I'm sold" straight off the brainstorm, skipping the merge):
    # the CHOICE must be recorded, not just its text — without `selected` on the built node the
    # graph drew the checked option as passed-over and the pivot read as abandoned (Sam's QA,
    # 2026-07-06), even though the thesis carried it.
    picks = [i for i in (body.get("options") or [])
             if isinstance(i, str) and _kind(nodes.get(i) or {}) == "option"]
    if not thesis and picks:   # derive the joined thesis server-side if the client didn't
        thesis = " + ".join(
            ((nodes[i].get("direction") or {}).get("one_liner")
             or (nodes[i].get("direction") or {}).get("title") or "") for i in picks).strip(" +")

    at_id = (body.get("node") or "").strip()
    target = nodes.get(at_id) if at_id else None
    a = target or nodes.get(tree.get("active")) or {}
    build_from = a

    def _thesis_of(n: dict) -> str:
        k = _kind(n or {})
        if k == "refined":
            return (n.get("thesis") or "").strip()
        if k == "option":
            dr = n.get("direction") or {}
            return (dr.get("one_liner") or dr.get("title") or "").strip()
        return ""

    if not thesis:
        thesis = _thesis_of(a)
        # POINTER-DRIFT RESILIENCE (2026-07-06): "build the plan" must mean the nearest buildable
        # idea, not "hope the active pointer is exactly right". A drifted/stranded pointer (the old
        # mutate-before-validate bug corrupted live sessions) landed commits on forks/sections and
        # every retry re-failed. Resolve instead: walk UP the ancestors for a refined/option node,
        # then fall back to the NEWEST buildable node anywhere. Only refuse when the tree genuinely
        # has nothing to build from (a raw brainstorm with no picks).
        cur = a
        while not thesis and cur is not None:
            cur = nodes.get(cur.get("parent")) if cur.get("parent") else None
            if cur is not None:
                t = _thesis_of(cur)
                if t:
                    thesis, build_from = t, cur
        if not thesis:
            # anywhere-fallback targets REFINED nodes only: a refined idea is a converged CHOICE;
            # an option is an unchosen candidate — never build one the operator didn't pick
            for n in reversed(list(nodes.values())):   # dict order = creation order → newest first
                if _kind(n) == "refined":
                    t = _thesis_of(n)
                    if t:
                        thesis, build_from = t, n
                        break
    if len(thesis) < 8:
        return JSONResponse(
            {"error": "Refine an idea or pick a direction to build first "
                      f"(the current step is a {_kind(a) or 'missing'} node and there is no refined "
                      "idea to build from yet)."}, status_code=400)
    if build_from.get("id") and build_from["id"] != tree.get("active"):
        tree["active"] = build_from["id"]    # the jump happens only when the build actually starts
    # T2 reuse: if we're building the EXACT refined node the merge skim already researched (same
    # thesis, skim claims present), carry those claims into the deep build so it re-grades them at full
    # strength instead of re-running the web fan-out. A steered/explicit-thesis or direct-picks commit
    # (thesis ≠ the refined node's, or no refined node) falls through to a full re-research.
    prior = None
    if _kind(build_from) == "refined":
        skim = build_from.get("research") or {}
        if skim.get("claims") and (build_from.get("thesis") or "").strip() == thesis.strip():
            prior = {"thesis": build_from.get("thesis"), "founder_edge": build_from.get("founder_edge"),
                     "claims": skim["claims"]}
    tok = dtree.begin_run(tree)              # this run's epoch — a pivot mid-run invalidates it
    store.plan_save(sid, status="researching", stage="researching", tree=tree)
    threading.Thread(target=_deep_build, args=(sid, thesis, s.get("user"), tok, picks, prior),
                     daemon=True).start()
    return {"id": sid}


@app.post("/api/plan/{sid}/route")
async def api_plan_route(sid: str, request: Request):
    """The single prompt box. Classify a free-text prompt against the funnel stage + active tool mode
    into one action (steer/commit/diverge/restart_keep/restart_hard/ask). A plan-stage steer that hard-
    clashes with the committed idea returns a `fork` (discard vs pivot) instead of applying. Returns the
    decision; the frontend acts on it (calls /merge, /commit, /next, /redraft, a tool, etc.)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "Type something."}, status_code=400)
    mode = body.get("mode") or "build"
    node_id = (body.get("node") or "").strip() or None   # the node the user has open (browse context)
    # Standing-decision detection runs on EVERY chat message, in every mode (Sam, 2026-07-07): a
    # declarative statement about the business gets a "pin it?" OFFER before any routing (the
    # operator confirms in the chat; nothing saves itself; declining falls through to a normal
    # route). The Summary tab is EAGER (the model reads most messages); everywhere else the
    # deterministic declarative pre-filter gates the model call so steers/questions cost nothing.
    from_summary = bool(body.get("summary"))
    stage = s.get("stage") or ("building" if s.get("proposal") else "plan")
    rstage = {"brainstorm": "brainstorm", "merging": "merge", "refined": "refined",
              "building": "plan", "done": "plan"}.get(stage, "plan")
    def _work():
        with ops.run_slot(ops.slot_user(s), s.get("stack")):
            offer, d_cost = decisions_mod.detect(prompt, mock=ops.MOCK, eager=from_summary)
            if offer:
                return None, None, offer, round(d_cost, 4), pipeline.LEDGER.tokens()
            decision, cost = router.route(prompt, stage=rstage, mode=mode,
                                          context=_route_context(s, node_id), mock=ops.MOCK)
            cost = round(cost + d_cost, 4)   # a ran-but-declined detect still costs its call
            fork = None
            if decision["intent"] == "steer" and rstage == "plan" and decision.get("steer"):
                idea = planner._working_idea(s)
                ic, ic_cost = router.check_integration(
                    decision["steer"], idea, planner.bundle_markdown(idea, s.get("files") or {}), mock=ops.MOCK)
                cost = round(cost + ic_cost, 4)
                if not ic["integrable"]:
                    fork = {"clash": ic["clash"], "skeptic_say": ic["skeptic_say"],
                            "steer": decision["steer"]}
            return decision, fork, None, cost, pipeline.LEDGER.tokens()
    try:
        decision, fork, offer, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    ops.fold_usage(sid, s, cost, toks)
    if offer:
        return {"offer": offer, "cost": cost, "tokens": toks}
    out = {"decision": decision, "cost": cost, "tokens": toks}
    if fork:
        out["fork"] = fork
    return out


_MOCK_LOOKUP = [
    {"text": "Companies spend an average of $75 per employee per month on office snacks",
     "url": "https://snackvendor.example.com/report", "tier": "vendor", "flagged": True,
     "reason": "vendor-published stat promoting its own category"},
    {"text": "U.S. office food-service spending grew 4% year over year",
     "url": "https://bls.gov/example", "tier": "primary", "flagged": False, "reason": ""},
]


@app.post("/api/plan/{sid}/lookup")
async def api_plan_lookup(sid: str, request: Request):
    """A fast, GRADED research lookup from the chat's research mode: one web-search lane on the
    question, every claim through the source-credibility gate (the moat), labeled — never laundered."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    question = (body.get("message") or "").strip()
    if not question:
        return JSONResponse({"error": "Ask a research question."}, status_code=400)
    if len(question) > 500:
        return JSONResponse({"error": "Keep it under 500 characters."}, status_code=400)
    if ops.MOCK:
        claims = [dict(c) for c in _MOCK_LOOKUP]
        _save_lookups(sid, s, claims)
        return {"claims": claims, "cost": 0, "tokens": 0}

    def _work():
        with ops.run_slot(s.get("user"), s.get("stack")):
            claims = pipeline.research_lane(planner._working_idea(s), question)
            verdicts = pipeline.gate_claims(claims)
            return verdicts, round(pipeline.LEDGER.cost(), 4), pipeline.LEDGER.tokens()
    try:
        verdicts, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = ops.fold_usage(sid, s, cost, toks)
    claims = [{"text": v.claim.text, "url": v.claim.source_url, "tier": v.tier,
               "flagged": bool(v.flagged), "reason": v.reason or ""} for v in verdicts]
    _save_lookups(sid, s, claims)
    return {"claims": claims, "cost": nc, "tokens": nt}


def _save_lookups(sid: str, s: dict, claims: list) -> None:
    """Persist chat-lookup claims on the session (deduped by text|url, bounded) so the research stack
    survives a reload — they used to live only in the client's memory."""
    if not claims:
        return
    have = list(s.get("lookups") or [])
    seen = {f"{c.get('text')}|{c.get('url')}" for c in have}
    have += [c for c in claims if f"{c.get('text')}|{c.get('url')}" not in seen]
    store.plan_save(sid, lookups=have[-60:])


# ── Standing decisions — the Summary tab's editable index (no AI, no spend, owner only) ──────────
@app.post("/api/plan/{sid}/decisions")
async def api_decisions_add(sid: str, request: Request):
    """Pin a standing decision (axiom / non-negotiable / preference). From the Summary tab's + Add,
    or the chat's 'pin it?' offer after a declarative message. Pure state — no model call."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    have = list(s.get("decisions") or [])
    if len(have) >= decisions_mod.MAX_DECISIONS:
        return JSONResponse({"error": "That's a full slate of decisions — retire one before "
                                      "pinning another."}, status_code=409)
    d = decisions_mod.new(body.get("text") or "", why=body.get("why") or "",
                          weight=body.get("weight") or "firm")
    if d is None:
        return JSONResponse({"error": "Give the decision a few real words."}, status_code=400)
    have.append(d)
    store.plan_save(sid, decisions=have)
    return {"decisions": have, "added": d}


@app.patch("/api/plan/{sid}/decisions/{did}")
async def api_decisions_edit(sid: str, did: str, request: Request):
    """Edit a decision's text / context / weight. Returns `impact` — the nodes built while it was
    in force — so the frontend can ask 'want to revisit any of these steps?' and pivot from them."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    have = list(s.get("decisions") or [])
    cur = next((d for d in have if d.get("id") == did), None)
    if cur is None:
        return JSONResponse({"error": "unknown decision"}, status_code=404)
    body = await request.json()
    before = dict(cur)
    upd = decisions_mod.clean({"text": body.get("text", cur["text"]),
                               "why": body.get("why", cur.get("why") or ""),
                               "weight": body.get("weight", cur.get("weight"))})
    if upd is None:
        return JSONResponse({"error": "Give the decision a few real words."}, status_code=400)
    cur.update(upd)
    store.plan_save(sid, decisions=have)
    changed = any(before.get(k) != cur.get(k) for k in ("text", "why", "weight"))
    return {"decisions": have,
            "changed": {"before": {k: before.get(k) for k in ("text", "why", "weight")},
                        "after": {k: cur.get(k) for k in ("text", "why", "weight")}},
            # impact only when something material moved — a no-op save shouldn't open the modal
            "impact": (decisions_mod.impact(s, did) if changed else [])}


@app.delete("/api/plan/{sid}/decisions/{did}")
async def api_decisions_remove(sid: str, did: str, request: Request):
    """Remove a decision. The nodes it shaped keep their stamp (the record is history, not a live
    pointer); `impact` names them so the frontend can offer the revisit-and-pivot pass."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    have = list(s.get("decisions") or [])
    cur = next((d for d in have if d.get("id") == did), None)
    if cur is None:
        return JSONResponse({"error": "unknown decision"}, status_code=404)
    have = [d for d in have if d.get("id") != did]
    store.plan_save(sid, decisions=have)
    return {"decisions": have, "removed": cur, "impact": decisions_mod.impact(s, did)}


# ── The Roadmap + the Codex (execution layer, app/domain/tasks.py) ────────────────────────────────
# The roadmap (tasks/milestones/goals) and codex (artifact pointers) are plan-scoped state, modeled
# on decisions: CRUD is pure state on the FREE side of the wall; the ONE AI seam is `extract` (plan →
# starter roadmap). A task carries provenance (source node + decisions) — the moat, extended to work.
def _roadmap_payload(s: dict) -> dict:
    """The roadmap surface's read model: the stored roadmap + codex, plus everything the frontend
    would otherwise recompute — the ONE next action, progress, per-task blocked flag, and the
    node→tasks counts that back a graph node's '⚒ N tasks' chip."""
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    codex = s.get("codex") or []
    tasks = tasks_mod.ordered_tasks(rm)
    blocked = {t["id"]: tasks_mod.is_blocked(t, rm) for t in tasks}
    node_counts: dict[str, int] = {}
    for t in tasks:
        if t.get("node"):
            node_counts[t["node"]] = node_counts.get(t["node"], 0) + 1
    nxt = tasks_mod.next_action(rm)
    return {"roadmap": {**rm, "tasks": tasks}, "codex": codex,
            "idea": (s.get("idea") or ""),
            "blocked": blocked, "next_action": nxt["id"] if nxt else None,
            "progress": tasks_mod.progress(rm), "node_counts": node_counts,
            "extracted": bool(rm.get("extracted_at"))}


@app.get("/api/plan/{sid}/roadmap")
async def api_roadmap_get(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return _roadmap_payload(s)


@app.post("/api/plan/{sid}/roadmap/extract")
async def api_roadmap_extract(sid: str, request: Request):
    """Turn the finished plan into a starter roadmap (the ONE AI seam). Idempotent-ish: re-running
    replaces the generated roadmap, so a `force` is required once tasks exist to avoid clobbering
    manual edits."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await (request.json() if request.headers.get("content-type", "").startswith("application/json")
                  else _empty())
    existing = (s.get("roadmap") or {}).get("tasks")
    if existing and not body.get("force"):
        return JSONResponse({"error": "This roadmap already has tasks. Pass force to regenerate.",
                             "have": len(existing)}, status_code=409)
    def _work():
        with ops.run_slot(ops.slot_user(s), s.get("stack")):
            return tasks_mod.extract(s, mock=ops.MOCK)
    try:
        roadmap, cost = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    store.plan_save(sid, roadmap=roadmap)
    ops.fold_usage(sid, s, cost, pipeline.LEDGER.tokens())
    s["roadmap"] = roadmap
    return {**_roadmap_payload(s), "cost": cost}


async def _empty():
    return {}


@app.post("/api/plan/{sid}/tasks")
async def api_task_add(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    if len(rm.get("tasks") or []) >= tasks_mod.MAX_TASKS:
        return JSONResponse({"error": "That's a full roadmap — clear a task before adding another."},
                            status_code=409)
    order = max((t.get("order", 0) for t in rm.get("tasks") or []), default=-1) + 1
    t = tasks_mod.new_task(body.get("text") or "", order=order,
                           detail=body.get("detail") or "", due=body.get("due"),
                           milestone=body.get("milestone"),
                           source=body.get("source") or "manual",
                           decisions=[d["id"] for d in (s.get("decisions") or []) if d.get("id")],
                           node=(s.get("tree") or {}).get("active"))
    if t is None:
        return JSONResponse({"error": "Give the task a few real words."}, status_code=400)
    rm.setdefault("tasks", []).append(t)
    store.plan_save(sid, roadmap=rm)
    s["roadmap"] = rm
    return {**_roadmap_payload(s), "added": t}


# The mutable fields a PATCH may set (pure state — validated by clean_task on save).
_TASK_PATCH = ("text", "detail", "status", "due", "span_days", "milestone", "blocked_by",
               "blocker_note", "artifacts", "order")


@app.patch("/api/plan/{sid}/tasks/{tid}")
async def api_task_edit(sid: str, tid: str, request: Request):
    """Edit a task: status (check-off), schedule (due/span), order (drag), dependencies (blocked_by),
    a blocker note, milestone, or an appended feedback note. Pure state, no model call. Returns the
    impacted downstream tasks when a status change unblocks/blocks others."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    cur = next((t for t in rm.get("tasks") or [] if t.get("id") == tid), None)
    if cur is None:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    merged = {**cur, **{k: body[k] for k in _TASK_PATCH if k in body}}
    upd = tasks_mod.clean_task(merged)
    if upd is None:
        return JSONResponse({"error": "Give the task a few real words."}, status_code=400)
    cur.update(upd)
    if body.get("feedback"):   # append an operator note (folds into the next replan / task chat)
        cur.setdefault("feedback", []).append({"text": tasks_mod._oneline(body["feedback"], 240),
                                               "at": tasks_mod._now()})
        cur["feedback"] = cur["feedback"][-12:]
    store.plan_save(sid, roadmap=rm)
    s["roadmap"] = rm
    return _roadmap_payload(s)


@app.delete("/api/plan/{sid}/tasks/{tid}")
async def api_task_remove(sid: str, tid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    rm["tasks"] = [t for t in rm.get("tasks") or [] if t.get("id") != tid]
    # drop dangling dependency edges so nothing is blocked by a deleted task
    for t in rm["tasks"]:
        if tid in (t.get("blocked_by") or []):
            t["blocked_by"] = [b for b in t["blocked_by"] if b != tid]
    store.plan_save(sid, roadmap=rm)
    s["roadmap"] = rm
    return _roadmap_payload(s)


@app.post("/api/plan/{sid}/tasks/reorder")
async def api_task_reorder(sid: str, request: Request):
    """Persist a drag-reorder: an ordered list of task ids becomes their new `order`."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    order = {tid: i for i, tid in enumerate(body.get("ids") or [])}
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    for t in rm.get("tasks") or []:
        if t["id"] in order:
            t["order"] = order[t["id"]]
    store.plan_save(sid, roadmap=rm)
    s["roadmap"] = rm
    return _roadmap_payload(s)


@app.post("/api/plan/{sid}/milestones")
async def api_milestone_add(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    m = tasks_mod.new_milestone(body.get("title") or "", due=body.get("due"))
    if m is None:
        return JSONResponse({"error": "Give the milestone a title."}, status_code=400)
    rm.setdefault("milestones", []).append(m)
    store.plan_save(sid, roadmap=rm)
    s["roadmap"] = rm
    return {**_roadmap_payload(s), "added": m}


# ── The Codex — artifact pointers (URL + note; no doc storage in MVP) ──────────
@app.post("/api/plan/{sid}/codex")
async def api_codex_add(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    codex = list(s.get("codex") or [])
    if len(codex) >= tasks_mod.MAX_ARTIFACTS:
        return JSONResponse({"error": "The codex is full — remove one before adding another."},
                            status_code=409)
    a = tasks_mod.new_artifact(title=body.get("title") or "", url=body.get("url") or "",
                               note=body.get("note") or "", kind=body.get("kind"),
                               node=(s.get("tree") or {}).get("active"))
    if a is None:
        return JSONResponse({"error": "An artifact needs at least a title or a note."}, status_code=400)
    codex.append(a)
    store.plan_save(sid, codex=codex)
    # link to a task if asked
    tid = body.get("task")
    if tid:
        rm = s.get("roadmap") or tasks_mod.empty_roadmap()
        t = next((x for x in rm.get("tasks") or [] if x.get("id") == tid), None)
        if t is not None:
            t.setdefault("artifacts", []).append(a["id"])
            a.setdefault("tasks", []).append(tid)
            store.plan_save(sid, roadmap=rm, codex=codex)
            s["roadmap"] = rm
    s["codex"] = codex
    return {**_roadmap_payload(s), "added": a}


@app.patch("/api/plan/{sid}/codex/{aid}")
async def api_codex_edit(sid: str, aid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    codex = list(s.get("codex") or [])
    cur = next((a for a in codex if a.get("id") == aid), None)
    if cur is None:
        return JSONResponse({"error": "unknown artifact"}, status_code=404)
    upd = tasks_mod.clean_artifact({**cur, **{k: body[k] for k in ("title", "url", "note", "kind",
                                                                   "tasks") if k in body}})
    if upd is None:
        return JSONResponse({"error": "An artifact needs at least a title or a note."}, status_code=400)
    cur.update(upd)
    store.plan_save(sid, codex=codex)
    s["codex"] = codex
    return _roadmap_payload(s)


@app.delete("/api/plan/{sid}/codex/{aid}")
async def api_codex_remove(sid: str, aid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    codex = [a for a in (s.get("codex") or []) if a.get("id") != aid]
    rm = s.get("roadmap") or tasks_mod.empty_roadmap()
    for t in rm.get("tasks") or []:   # unlink from any task that referenced it
        if aid in (t.get("artifacts") or []):
            t["artifacts"] = [x for x in t["artifacts"] if x != aid]
    store.plan_save(sid, codex=codex, roadmap=rm)
    s["codex"] = codex
    s["roadmap"] = rm
    return _roadmap_payload(s)


@app.post("/api/plan/{sid}/tasks/{tid}/chat")
async def api_task_chat(sid: str, tid: str, request: Request):
    """Task-grounded chat — the SAME advisor, focused on one task ('what does this mean?', 'how do I
    do this?'). Grounded via context.task_view so the reply knows the task, its provenance, and its
    linked artifacts. Account-walled like every engine verb; free-side CRUD is elsewhere."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    wall = _account_wall(request, s)
    if wall:
        return wall
    body = await request.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        return JSONResponse({"error": "Type something."}, status_code=400)
    tv = context.task_view(s, tid)
    if not tv:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    def _work():
        with ops.run_slot(ops.slot_user(s), s.get("stack")):
            return advisor.chat_reply(s, msg, history=(s.get("chat") or [])[-8:],
                                      mock=ops.MOCK, situation=tv)
    try:
        reply, cost = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    ops.fold_usage(sid, s, cost, pipeline.LEDGER.tokens())
    return {"reply": reply, "cost": cost}


@app.get("/api/plan/{sid}/roadmap.ics")
async def api_roadmap_ics(sid: str, request: Request):
    """A zero-OAuth calendar feed of scheduled tasks + dated milestones (integrations_research.md
    §Google). Subscribe to it from Google/Apple/Outlook; refreshes on their cadence."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    ics = tasks_mod.to_ics(s.get("roadmap"), plan_title=(s.get("idea") or "FILG roadmap")[:60])
    return Response(ics, media_type="text/calendar",
                    headers={"Content-Disposition": f'inline; filename="filg-{sid}.ics"'})


@app.post("/api/plan/{sid}/chatlog")
async def api_plan_chatlog(sid: str, request: Request):
    """Append one message to the session's conversation record (the v2 left-panel chat). Pure logging,
    no AI call — the client posts what it rendered so the conversation survives a reload. Distinct from
    /chat (the v1 advisor, which generates a reply)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    role = body.get("role")
    content = str(body.get("content") or "").strip()[:2000]
    if role not in ("user", "bot", "status") or not content:
        return JSONResponse({"error": "role must be user/bot/status, content required"}, status_code=400)
    chat = list(s.get("chat") or [])
    chat.append({"role": role, "content": content})
    store.plan_save(sid, chat=chat[-400:])   # a long session stays bounded
    return {"ok": True}


@app.get("/api/plan/{sid}/node/{nid}")
async def api_plan_node(sid: str, nid: str, request: Request):
    """Lazy node content for the decision-graph zoom: given a node id, return its full page (the
    section's built content, the option's direction, the refined idea, or a fork's options)."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    n = ((s.get("tree") or {}).get("nodes") or {}).get(nid)
    if not n:
        return JSONResponse({"error": "unknown node"}, status_code=404)
    k = _kind(n)
    out = {"id": n["id"], "kind": k, "title": n.get("title"), "parent": n.get("parent"),
           "children": n.get("children") or [], "feedback": n.get("feedback"), "step": n.get("step", 0),
           "log": n.get("log") or [],   # the persisted build receipts — survive a reload (§v2 #10)
           "board": n.get("board") or []}   # this step's board history — the node view shows the convenes
    if k == "section":
        sec = next((x for x in planner.SECTIONS if x["key"] == n.get("section")), None)
        content = (n.get("files") or {}).get(sec["file"]) if sec else None
        out.update({"section": n.get("section"), "sub": (sec or {}).get("sub"),
                    "draft": n.get("draft"), "content": content})
    elif k == "option":
        out["direction"] = n.get("direction")
    elif k == "refined":
        for f in ("thesis", "founder_edge", "mold", "kept", "dropped", "research", "selected"):
            out[f] = n.get(f)
    elif k == "brainstorm":
        out["spread"] = n.get("spread")
        out["feedback"] = n.get("feedback")   # the pivot ask this spread was answering (if any)
        out["set_aside"] = n.get("set_aside")  # declined-out-loud part of the ask
        # the fork's story, self-contained: every direction offered + which were picked (a pick =
        # named in any join's `selected` anywhere in the tree)
        nodes = (s.get("tree") or {}).get("nodes") or {}
        chosen = set()
        for x in nodes.values():
            chosen.update(x.get("selected") or [])
        out["options"] = [{"id": c["id"], "direction": c.get("direction"), "picked": c["id"] in chosen}
                          for c in (nodes.get(i) for i in (n.get("children") or []))
                          if c and _kind(c) == "option"]
    elif k == "idea":
        out["draft"] = n.get("draft")   # the raw prompt the whole tree grew from
    elif k == "fork":
        out.update({"question": n.get("question"), "options": n.get("options")})
    return out


@app.get("/api/plans")
async def api_plans(request: Request):
    """The signed-in user's plans — for the profile / 'My plans' view."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in to see your plans."}, status_code=401)
    plans = []
    for p in store.plan_list(authed["email"]):
        done = p["status"] == "done"
        # per-plan PDF unlock (for the "My files" tab → re-download purchased items). Only finished
        # plans can have a polished PDF; the unlock is per the plan's active branch.
        unlocked = False
        if done:
            full = store.plan_get(p["id"]) or {}
            unlocked = _has_pdf_access(authed["email"], ops.plan_key(full), verified=True)
        plans.append({"id": p["id"], "idea": p["idea"], "status": p["status"], "step": p["step"],
                      "created_at": p["created_at"], "updated_at": p.get("updated_at") or p["created_at"],
                      "done": done, "shared": bool(p.get("shared")), "pdf_unlocked": unlocked})
    return {"email": authed["email"], "total": planner.N, "plans": plans}


# ── In-app product help (a standard website help chat; runs on the user's key) ──
_FEATURE_WORDS = {"director_forge": "Director Forge", "custom_directors": "custom directors",
                  "skeptic": "the assumption stress-test"}


@app.post("/api/help")
async def api_help(request: Request):
    """A standard website-style help chat for using the product. Runs on the user's own key (BYOK),
    same as every other engine call; no plan/session required."""
    if ops.MOCK:
        return {"reply": domain_help.MOCK_REPLY}
    authed = auth.user_from_request(request)
    user = authed["email"] if authed else None
    prov = _provider_for(user)   # the user's own key, else FILG's hosted free key (if configured)
    if prov is None:
        return JSONResponse({"error": "Add your API key to use help (it runs on your own key).",
                             "needKey": True}, status_code=402)
    if prov.bills_filg and usage.kill_switch_tripped():   # help on FILG's key respects the daily budget
        return JSONResponse({"error": "Today's free pool is tapped. Add your own API key to keep going.",
                             "needKey": True}, status_code=402)
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Ask a question."}, status_code=400)
    if len(message) > 1000:
        return JSONResponse({"error": "Keep it under 1000 characters."}, status_code=400)
    convo = ""
    for m in (body.get("history") or [])[-6:]:
        who = "User" if m.get("role") == "user" else "Help"
        convo += f"\n{who}: {str(m.get('content', ''))[:600]}"
    try:
        with provider.use(prov), provider.use_stack(provider.DEFAULT_STACK), pipeline.run_ledger():
            reply = pipeline.call("help", pipeline.SONNET, max_tokens=400, system=domain_help.system(), cache=True,
                                  prompt=f"Conversation so far:{convo or ' (none)'}\n\nUser: {message}\n\n"
                                         "Reply as the FILG help assistant.")
            _meter(user, round(pipeline.LEDGER.cost(), 4))   # FILG-key help → daily + a subscriber's monthly cap
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    return {"reply": (reply or "").strip() or "Sorry, I couldn't generate a reply, try rephrasing."}


@app.get("/api/plan/{sid}")
async def api_plan_get(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if request.query_params.get("touch"):   # explicit open (not a status poll) → bump recency for the profile sort
        store.plan_touch(sid)
    return views.plan_state(s)


@app.post("/api/plan/{sid}/claim")
async def api_plan_claim(sid: str, request: Request):
    """Attach an anonymous free-taste plan to the account that just signed in (the wall's happy path:
    taste → sign up → the plan follows you). Idempotent; refuses a plan someone else owns."""
    authed = auth.user_from_request(request)
    if not authed or not authed["email"]:
        return JSONResponse({"error": "Sign in first.", "needAccount": True}, status_code=401)
    s = store.plan_get(sid)
    if not s:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not store.plan_claim(sid, authed["email"]):
        return JSONResponse({"error": "unknown session"}, status_code=404)   # owned by someone else — don't leak
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/next")
async def api_plan_next(sid: str, request: Request):
    """Roll forward: finalize the active node's section (a note steers it) and draft the next one.
    Going forward from a node that already has children naturally creates a new branch."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip() or None
    force = bool(body.get("force"))   # operator chose "build it anyway" past the kill gate (no substance)
    killed = (s.get("vetting") or {}).get("verdict") == "kill"
    if killed and not force:   # soft gate: a non-forced advance still gets the advisement (off-ramp = /revet)
        return _kill_gate(s)
    tree = views.ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if active["step"] >= planner.N:
        return JSONResponse({"error": "This plan is already complete."}, status_code=409)
    def _work():
        with ops.run_slot(s.get("user"), s.get("stack")):
            if killed:   # forced past the gate with no substance → waste-of-time mode (comedic, skips research → ~$0)
                child, cost = planner.wod_forward(active)
            else:
                child, cost = planner.forward(planner._working_idea(s), s["research"], active, feedback,
                                              directors=s.get("directors") or None,
                                              founder=planner._founder(s), mock=ops.MOCK,
                                              extra_personas=s.get("custom_directors"),
                                              decisions=_decisions_block(s))
            return child, cost, pipeline.LEDGER.tokens()
    try:
        child, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    node = _new_node(child, active["id"])
    _stamp_decisions(s, node)
    dtree.attach(tree, node)
    _meter(s.get("user"), cost)
    ops.fold_usage(sid, s, cost, toks, tree=tree, **views.mirror(tree))
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/revet")
async def api_plan_revet(sid: str, request: Request):
    """Kill-gate rescue: the operator adds real substance (a skill / asset / who'd pay); we re-shape +
    re-vet. If the verdict clears the kill, we redraft part 1 from the enriched thesis and the builder
    unlocks. Still `kill` → the new (sharper) diagnostic comes back and the gate holds."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    more = (body.get("more") or "").strip()
    if len(more) < 8:
        return JSONResponse(
            {"error": "Give me a bit more — a real skill or asset, and who'd pay for it."}, status_code=400)
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            shaped, vetting, cost = intake.revet(s["idea"], more, s.get("research"), mock=ops.MOCK)
            proposal = None
            if vetting["verdict"] != "kill":   # cleared → redraft part 1 from the now-substantive thesis
                proposal, c2 = planner.first_proposal(
                    shaped["thesis"], s["research"], founder=shaped.get("founder_edge"), mock=ops.MOCK,
                    decisions=_decisions_block(s))
                cost = round(cost + c2, 4)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    updates = {"shaped": shaped, "vetting": vetting}
    if proposal is not None:
        root = _new_node(planner.root_node(proposal), None)   # no branches exist yet on a kill, so reseed
        _stamp_decisions(s, root)
        tree = dtree.seed(root)
        updates.update({"proposal": proposal, "step": 0, "tree": tree, **views.mirror(tree)})
    _meter(s.get("user"), cost)
    ops.fold_usage(sid, s, cost, toks, **updates)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/back")
async def api_plan_back(sid: str, request: Request):
    """Roll back a step: re-draft the PREVIOUS section taking the (required) note as a redirect — a
    new sibling branch from the node before this one. Feedback is required: backing up means you want
    something changed, so an empty note is a form error."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        return JSONResponse(
            {"error": "Add a quick note on what to change — feedback's required to go back a step."},
            status_code=400)
    tree = views.ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if not active.get("parent"):
        return JSONResponse({"error": "You're on the first part — nothing to go back to."},
                            status_code=400)
    prev = tree["nodes"][active["parent"]]   # the previous step's node — the one we re-draft
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], prev, feedback,
                                         founder=planner._founder(s), mock=ops.MOCK)
            regrade, cost = _regrade_setup(s, sib, cost)   # back onto the setup → re-grade the verdict
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    dtree.attach(tree, _new_node(sib, prev.get("parent")))   # sibling of `prev` → branches from prev's parent
    _meter(s.get("user"), cost)
    updates = dict(tree=tree, **views.mirror(tree))
    if regrade:
        updates["vetting"] = regrade
    ops.fold_usage(sid, s, cost, toks, **updates)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/redraft")
async def api_plan_redraft(sid: str, request: Request):
    """Regenerate the CURRENT part with the operator's feedback applied — a fresh sibling of the active
    node at the SAME step (not a step back; backing up is done via the tree). Feedback required: a
    rework needs a note to steer it."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        return JSONResponse(
            {"error": "Add a quick note on what's not landing — a rework needs something to steer it."},
            status_code=400)
    tree = views.ensure_tree(s)
    active = tree["nodes"][tree["active"]]
    if active["step"] >= planner.N:
        return JSONResponse({"error": "This plan is already complete."}, status_code=409)
    def _work():
        with ops.run_slot(s.get("user"), s.get("stack")):
            sib, cost = planner.rebranch(planner._working_idea(s), s["research"], active, feedback,
                                         founder=planner._founder(s), mock=ops.MOCK,
                                         decisions=_decisions_block(s))
            regrade, cost2 = _regrade_setup(s, sib, cost)   # setup reframed → re-grade the verdict on the new angle
            return sib, regrade, cost2, pipeline.LEDGER.tokens()
    try:
        sib, regrade, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    sib_node = _new_node(sib, active.get("parent"))   # sibling of the active node → same step, new branch
    _stamp_decisions(s, sib_node)
    dtree.attach(tree, sib_node)
    _meter(s.get("user"), cost)
    updates = dict(tree=tree, **views.mirror(tree))
    if regrade:
        updates["vetting"] = regrade
    ops.fold_usage(sid, s, cost, toks, **updates)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/stack")
async def api_plan_stack(sid: str, request: Request):
    """Choose the model stack for this plan (trust-fund / damn-good / polished-turd). Pure setting — no
    spend. The premium stack is only honored on a BYOK key; on FILG's free key it clamps at run time."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    want = provider.stack_name(body.get("stack"))
    store.plan_save(sid, stack=want)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/goto")
async def api_plan_goto(sid: str, request: Request):
    """Hop to an existing node in the decision tree (pure navigation — no new branch, no spend).
    Rolling forward from there is what spawns a new branch."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    node_id = body.get("node")
    tree = views.ensure_tree(s)
    if node_id not in (tree.get("nodes") or {}):
        return JSONResponse({"error": "unknown node"}, status_code=404)
    tree["active"] = node_id
    store.plan_save(sid, tree=tree, **views.mirror(tree))
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/ask")
async def api_plan_ask(sid: str, request: Request):
    """Add-on: ask a composite archetype advisor about the plan-in-progress."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    archetype = body.get("archetype")
    if archetype not in planner.ARCHETYPE_KEYS:
        return JSONResponse({"error": "pick an advisor"}, status_code=400)
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            res, cost = planner.ask_expert(s["idea"], s.get("files") or {}, archetype,
                                           body.get("question") or "", mock=ops.MOCK)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = ops.fold_usage(sid, s, cost, toks)
    return {**res, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/board")
async def api_plan_board(sid: str, request: Request):
    """Convene the Board of Directors on the plan-so-far. The standing, multi-advisor version of
    'ask an expert': several composite directors weigh in and FILG synthesizes the collaboration
    matrix (agreement / conflict / net verdict). `directors` in the body re-picks + persists the
    board; otherwise the session's board (or the default starter board) is used."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    customs = s.get("custom_directors") or []
    custom_keys = {p["key"] for p in customs}
    picked = body.get("directors")
    directors = [k for k in (picked or s.get("directors") or personas.DEFAULT_BOARD)
                 if k in personas.KEYS or k in custom_keys] or personas.DEFAULT_BOARD
    question = (body.get("question") or "").strip() or \
        "Vet the plan so far — what's the one thing I should change before continuing?"
    work_idea = planner._working_idea(s)
    plan_text = planner.bundle_markdown(work_idea, s.get("files") or {})
    def _work():
        with ops.run_slot(s.get("user"), s.get("stack")):
            res, cost = board.convene(work_idea, plan_text, question, directors, mock=ops.MOCK,
                                      extra_personas=customs, decisions=_decisions_block(s))
            return res, cost, pipeline.LEDGER.tokens()
    try:
        res, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    _meter(s.get("user"), cost)
    extra = {"directors": directors} if picked else {}   # persist a freshly chosen board for later steps
    # Persist the convene on the ACTIVE NODE's board history (bounded), so it survives tree navigation
    # (the flat `board` column is a mirror of the active node — writing only there gets clobbered),
    # steers later drafts via planner._board_notes, and shows up in the exports/handoff.
    tree = views.ensure_tree(s)
    a = (tree.get("nodes") or {}).get(tree.get("active"))
    if a is not None:
        entry = {"section": "convene", "title": f"Board convened: “{question[:90]}”", **res}
        # pre-tree sessions advanced the flat mirror without the tree — trust whichever is ahead
        base = max((a.get("board") or []), (s.get("board") or []), key=len)
        reviews = dtree.clip("board", list(base) + [entry])
        a["board"] = reviews
        extra.update(tree=tree, board=reviews)
    nc, nt = ops.fold_usage(sid, s, cost, toks, **extra)
    return {**res, "cost": nc, "tokens": nt}


_MAX_CUSTOM_DIRECTORS = 8


@app.post("/api/plan/{sid}/director/forge")
async def api_director_forge(sid: str, request: Request):
    """Forge a CUSTOM board director from a description (distill → draft → QA). Returns a DRAFT persona
    (with its trace) that the operator can approve (save) or retry; nothing is seated until they save.
    Runs on the owner's bound key, same as every engine op."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    if not _feature_ok(s.get("user"), "director_forge"):   # premium feature: Pro/Studio, or BYOK
        return JSONResponse(
            {"error": "Forging a custom board director is a Pro feature. Upgrade to Pro, or add your "
                      "own API key to use it free.", "upgrade": True, "feature": "director_forge"},
            status_code=402)
    body = await request.json()
    desc = (body.get("description") or "").strip()
    if len(desc) < 4:
        return JSONResponse({"error": "Describe the director you want."}, status_code=400)
    if len(desc) > 600:
        return JSONResponse({"error": "Keep it under 600 characters."}, status_code=400)
    if len(s.get("custom_directors") or []) >= _MAX_CUSTOM_DIRECTORS:
        return JSONResponse({"error": "You've already forged a full bench of custom directors."},
                            status_code=409)
    existing = list(personas.KEYS) + [p["key"] for p in (s.get("custom_directors") or [])]
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            persona, cost = director_forge.forge(desc, existing_keys=existing, mock=ops.MOCK)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        return ops.engine_error(e)
    _meter(s.get("user"), cost)
    nc, nt = ops.fold_usage(sid, s, cost, toks)
    return {"persona": persona, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/director/save")
async def api_director_save(sid: str, request: Request):
    """Seat a forged director on the board: persist it to the session's custom_directors registry (so
    board.convene can resolve it) and add it to the active board. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    p = (await request.json()).get("persona") or {}
    if not (p.get("key") and p.get("name") and p.get("voice")):
        return JSONResponse({"error": "Forge a director first."}, status_code=400)
    customs = list(s.get("custom_directors") or [])
    if len(customs) >= _MAX_CUSTOM_DIRECTORS:
        return JSONResponse({"error": "You've already forged a full bench of custom directors."},
                            status_code=409)
    clean = {k: p.get(k) for k in ("key", "name", "first", "blurb", "domains", "voice")}
    clean["custom"] = True
    if not any(c["key"] == clean["key"] for c in customs):   # idempotent on re-save
        customs.append(clean)
    directors = list(s.get("directors") or [])
    if clean["key"] not in directors:                        # seat them on the active board
        directors.append(clean["key"])
    store.plan_save(sid, custom_directors=customs, directors=directors)
    return views.plan_state(store.plan_get(sid))


@app.post("/api/plan/{sid}/research/query")
async def api_research_query(sid: str, request: Request):
    """Query the graded research. mode='quick' reads what's already there (and may say it's not sure);
    mode='deep' spawns a fresh, bounded research pass on the question. Owner only, on their bound key."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    q = (body.get("question") or "").strip()
    if len(q) < 3:
        return JSONResponse({"error": "Ask a question about your research."}, status_code=400)
    if len(q) > 1000:
        return JSONResponse({"error": "Keep it under 1000 characters."}, status_code=400)
    mode = "deep" if body.get("mode") == "deep" else "quick"
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            res, cost = advisor.research_answer(s, q, mode=mode, mock=ops.MOCK)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
    _meter(s.get("user"), cost)
    nc, nt = ops.fold_usage(sid, s, cost, toks)
    return {**res, "cost": nc, "tokens": nt}


def _stress_worker(sid: str, idea: str, shaped: dict, research: dict | None,
                   user: str, stack: str | None) -> None:
    """Background worker: run the adversarial stress-test on the user's bound key, streaming the
    §LANES§/§LANEDONE§ runner sentinels into the session's `skeptic.progress` live (so the client's
    runner panel paints the assumption→attack→verdict tree grey→green), then persist the result.
    Mirrors _plan_research's threading + on_progress + provider/ledger binding."""
    progress: list[str] = []
    try:
        def on_progress(line: str) -> None:
            progress.append(line)
            store.plan_save(sid, skeptic={"status": "running", "progress": list(progress),
                                          "result": None})

        with ops.bind_session_run(user, stack):
            res, cost = skeptic.stress_test(idea, shaped, research, mock=ops.MOCK, on_progress=on_progress)
            toks = pipeline.LEDGER.tokens()
        _meter(user, cost)
        s = store.plan_get(sid) or {}
        ops.fold_usage(sid, s, cost, toks,
                    skeptic={"status": "done", "progress": progress, "result": res, "cost": cost})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        store.plan_save(sid, skeptic={"status": "error", "progress": progress, "result": None,
                                      "error": ops.humanize_error(e)[0]})


@app.post("/api/plan/{sid}/stress-test")
async def api_plan_stress_test(sid: str, request: Request):
    """Kick off the adversarial assumption stress-test (skeptic.stress_test) in the background so its
    runner tree streams live. High-stakes, real spend — an explicit action, owner only, behind the key
    wall. Returns {started:true}; the client polls GET /api/plan/{sid}/stress-test for progress+result."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    if not _feature_ok(s.get("user"), "skeptic"):   # premium feature: Pro/Studio, or BYOK
        return JSONResponse(
            {"error": "The adversarial stress-test is a Pro feature. Upgrade to Pro, or add your own "
                      "API key to use it free.", "upgrade": True, "feature": "skeptic"}, status_code=402)
    shaped = s.get("shaped")
    if not shaped:
        return JSONResponse({"error": "Shape the idea first, then stress-test it."}, status_code=409)
    if (s.get("skeptic") or {}).get("status") == "running":
        return JSONResponse({"error": "A stress-test is already running.", "running": True},
                            status_code=409)
    store.plan_save(sid, skeptic={"status": "running", "progress": [], "result": None})
    threading.Thread(target=_stress_worker,
                     args=(sid, planner._working_idea(s), shaped, s.get("research"),
                           s.get("user"), s.get("stack")), daemon=True).start()
    return {"started": True}


@app.get("/api/plan/{sid}/stress-test")
async def api_plan_stress_test_state(sid: str, request: Request):
    """Poll the stress-test: {status: idle|running|done|error, progress:[…sentinels…], result, cost}."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return s.get("skeptic") or {"status": "idle", "progress": [], "result": None}


@app.post("/api/plan/{sid}/chat")
async def api_plan_chat(sid: str, request: Request):
    """Chat with your plan — a standing advisor grounded in the plan, graded research, decisions, and
    the chosen board. Persists the thread on the session so it lives with the plan. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] == "error":
        return JSONResponse({"error": "This plan hit an error — start over or re-run it first."},
                            status_code=409)
    if (wall := _account_wall(request, s) or _key_wall(s)):
        return wall
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Ask a question."}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"error": "Keep it under 2000 characters."}, status_code=400)
    # v2 logs the user's bubble itself via /chatlog before routing here — log_user=false stops the
    # double entry, and the trailing duplicate is trimmed from what the model sees
    echo_user = bool(body.get("log_user", True))
    history = list(s.get("chat") or [])
    hist_model = (history[:-1] if (not echo_user and history
                                   and (history[-1].get("content") or "") == message) else history)

    journey = _journey_digest(s)   # the decision tree — options, picks, pivots — else the advisor is blind to it
    situation = context.situation(s, (body.get("node") or "").strip() or None,
                                  (str(body.get("working") or "").strip() or None))

    def _work():
        with ops.run_slot(s.get("user"), s.get("stack")):
            reply, cost = advisor.chat_reply(s, message, history=hist_model, mock=ops.MOCK,
                                             journey=journey, situation=situation)
            return reply, cost, pipeline.LEDGER.tokens()
    try:
        reply, cost, toks = await run_in_threadpool(_work)
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)
    history = list(store.plan_get(sid).get("chat") or [])   # re-read: a run may have logged mid-flight
    history += (([{"role": "user", "content": message}] if echo_user else [])
                + [{"role": "assistant", "content": reply}])
    _meter(s.get("user"), cost)  # FILG-key chat counts toward the daily kill switch; BYOK is the user's spend
    nc, nt = ops.fold_usage(sid, s, cost, toks, chat=history)
    return {"reply": reply, "messages": history, "cost": nc, "tokens": nt}


@app.post("/api/plan/{sid}/delete")
async def api_plan_delete(sid: str, request: Request):
    """Permanently delete a plan (owner only). Note: deleting does NOT refund a free-tier run — each
    build already cost compute (cost guardrail), so delete is for tidiness, not free re-rolls."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    store.plan_delete(sid)
    return {"ok": True}


@app.post("/api/plan/{sid}/share")
async def api_plan_share(sid: str, request: Request):
    """Toggle a plan's public read-only share link (owner only). Body {shared: bool} (default true).
    Returns the shareable URL. Plans are private by default — this opts one in."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    body = await request.json()
    shared = body.get("shared", True)
    store.plan_set_shared(sid, shared)
    base = (os.environ.get("FILG_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/"))
    return {"shared": bool(shared), "url": f"{base}/p/{sid}" if shared else None}


@app.get("/api/plan/{sid}/download")
async def api_plan_download(sid: str, request: Request):
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    # No paywall (monetization model in flux — own-your-docs is the wedge). `s["files"]` mirrors the
    # ACTIVE decision-tree branch, so the download is exactly the final decision set the user landed on.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", planner.bundle_markdown(s["idea"], s["files"]))
        for path, content in s["files"].items():
            z.writestr(path, content)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="filg-business-plan.zip"'})


@app.get("/api/plan/{sid}/handoff.txt")
async def api_plan_handoff(sid: str, request: Request):
    """The LLM-handoff prompt (copy-paste into any model to continue). Free, owner only, any point."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not (s.get("research") or s.get("files") or s.get("shaped") or s.get("vetting")):
        return JSONResponse({"error": "Nothing to hand off yet."}, status_code=400)
    return Response(exports.handoff_prompt(s), media_type="text/plain; charset=utf-8")


@app.get("/api/plan/{sid}/export.txt")
async def api_plan_export_txt(sid: str, request: Request):
    """Get-your-data-out: a single plain-text dump of everything generated so far (idea, straight read,
    plan parts, graded research, board takes). Available the moment there's data — no 'done' gate, no
    paywall. Owner only."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not (s.get("research") or s.get("files") or s.get("shaped") or s.get("vetting")):
        return JSONResponse({"error": "Nothing to export yet."}, status_code=400)
    fn = f"{exports.slug(s.get('idea'))}-filg-export.txt"
    return Response(exports.export_text(s), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.get("/api/plan/{sid}/plan.pdf")
async def api_plan_pdf(sid: str, request: Request):
    """The core artifact: a styled, branded PDF of the finished plan. Synthesizes an exec summary,
    lays out the active branch's sections, and appends the graded-research evidence exhibit. Clean
    copy paid via the $13 per-plan unlock (or a comp credit); re-downloading an unlocked plan is
    free. The synthesis runs on the OWNER'S bound key (`_run_slot` binds their provider). Builds from
    `s["files"]` = the active branch's final decision set."""
    s = store.plan_get(sid)
    if not s or not _owns(request, s):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if s["status"] != "done":
        return JSONResponse({"error": "plan isn't finished yet"}, status_code=400)
    authed = auth.user_from_request(request)
    email = (authed or {}).get("email", "")
    # Subscribers get the polished PDF free (it's part of the plan). Otherwise claim it: free if comped
    # or already unlocked ($13 bought this plan), else spend a comp credit. No access → a BYOK user
    # still gets a FREE WATERMARKED copy (synth runs on their own key — the share loop needs an
    # artifact that circulates; the $13 unlock removes the line). No access and no key → payment.
    watermark = False
    if (billing.PDF_BILLING_ENABLED and not _is_subscriber(email)
            and not billing.claim_pdf(email, ops.plan_key(s))):
        owner = (s.get("user") or email or "").strip().lower()
        if keys.enabled() and owner and keys.has_key(owner):
            watermark = True
        else:
            return JSONResponse(
                {"error": f"The clean PDF for this plan is a one-time "
                          f"${billing.PDF_PRICE_CENTS // 100} (re-downloads are free). Or add your "
                          "own API key for a free watermarked copy. Your raw export is always free.",
                 "needPurchase": True, "price": billing.PDF_PRICE_CENTS}, status_code=402)
    try:
        with ops.run_slot(s.get("user"), s.get("stack")):
            plan, cost = plan_pdf.synthesize(s, mock=ops.MOCK)
            data = plan_pdf.render(plan, style=(request.query_params.get("style") or "filg"),
                                   watermark=watermark)
            toks = pipeline.LEDGER.tokens()
    except ops.BusyError as be:
        return ops.busy_response(be)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse({"error": f"Could not build the PDF: {e}"}, status_code=500)
    # BYOK → the owner's own key (never touches FILG's budget). A subscriber → FILG's key, so meter the
    # synth against their monthly fair-use cap. The per-session meter reflects it either way.
    _meter(s.get("user"), cost)
    nc, nt = ops.fold_usage(sid, s, cost, toks)   # binary response → echo usage via headers for the meter
    fn = f"{exports.slug(s.get('idea'))}-business-plan.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"',
                             "X-FILG-Cost": str(nc), "X-FILG-Tokens": str(nt),
                             "X-FILG-Watermark": "1" if watermark else "0"})


def _page_head() -> str:
    """The `__FILG_HEAD__` block: the window.FILG config + the Supabase script when auth is on."""
    cfg = json.dumps({"authEnabled": auth.AUTH_ENABLED,
                      "pdfBilling": billing.PDF_BILLING_ENABLED, "pdfPrice": billing.PDF_PRICE_CENTS,
                      "byokEnabled": keys.enabled(),
                      "freeTaste": bool(ops.HOSTED_FREE and keys.enabled()),   # first query on FILG's key
                      "subEnabled": billing.PDF_BILLING_ENABLED,           # monthly tiers available (Stripe on)
                      "tiers": tiers.catalog(),                            # pricing table / fork UI
                      # dev-only: with auth off, act as this account so the subscriber UX reflects locally
                      "devEmail": (os.environ.get("FILG_DEV_EMAIL", "") if not auth.AUTH_ENABLED else ""),
                      "supabaseUrl": os.environ.get("SUPABASE_URL", ""),
                      "supabaseAnon": (os.environ.get("SUPABASE_PUBLISHABLE_KEY")
                                       or os.environ.get("SUPABASE_ANON_KEY", "")),
                      "archetypes": personas.catalog(), "defaultBoard": personas.DEFAULT_BOARD})
    head = f"<script>window.FILG={cfg}</script>"
    if auth.AUTH_ENABLED:
        head += '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
    return head


def _shell() -> str:
    """THE app shell (the former v2 surface, promoted to the root namespace 2026-07-06 — v1 retired).
    Served at `/` and every clean deep-link path (`/plan/{id}`, `/account/<tab>`) so real History-API
    URLs direct-load and refresh."""
    return _page_text("index.html").replace("__FILG_HEAD__", _page_head())


@app.get("/", response_class=HTMLResponse)
async def index():
    """The unified two-panel build surface: LEFT = the chat (research/board/help/summary displays),
    RIGHT = the decision graph. The landing IS the workspace with the drawer expanded."""
    return _shell()


@app.get("/plan/{sid}", response_class=HTMLResponse)
async def plan_page(sid: str):
    """Deep link into a plan: same shell, the frontend reads the id from the path and restores the
    session — graph, documents, and the conversation log. (Distinct from `/p/{id}` — the public
    share — and `/r/{id}` teardowns.)"""
    return _shell()


@app.get("/plan/{sid}/roadmap", response_class=HTMLResponse)
async def roadmap_page(sid: str):
    """The Roadmap + Codex surface — a first-class execution view (list + calendar + codex) that
    stands beside the decision graph. A self-contained prototype page wired to the real /roadmap
    API; it reads the sid from the path. (Slated to fold into the main shell as a right-panel
    surface — execution_layer.md §5.)"""
    return HTMLResponse(_page_text("roadmap.html").replace("__FILG_HEAD__", _page_head()))


@app.get("/roadmap-demo")
async def roadmap_demo():
    """DEV/DEMO only (mock mode): seed a plan with a mock 7-section build, a standing decision, and a
    tree, then jump to its Roadmap so the surface is clickable without running a full build. Never
    exists in prod (real runs cost money and start from a real idea)."""
    if not ops.MOCK:
        return RedirectResponse("/", status_code=302)
    from app.domain import copy as _copy  # noqa: PLC0415
    from app.domain import sections as _sec  # noqa: PLC0415
    sid = uuid.uuid4().hex[:12]
    idea = "a done-for-you AI automation service for local HVAC companies"
    store.plan_create(sid, "", idea)
    files = {s["file"]: _copy.MOCK_DRAFT.get(s["key"], "") for s in _sec.SECTIONS}
    root = dtree.new_node({"kind": "refined", "thesis": idea,
                           "title": "Refined idea"}, None)
    tree = dtree.seed(root)
    decisions = [decisions_mod.new("No cold-call marketing", why="I hate phones",
                                   weight="non_negotiable"),
                 decisions_mod.new("Stay solo — no employees in year one", weight="firm")]
    store.plan_save(sid, files=files, tree=tree, status="done", stage="done",
                    decisions=decisions)
    return RedirectResponse(f"/plan/{sid}/roadmap", status_code=302)


@app.get("/account", response_class=HTMLResponse)
@app.get("/account/{tab}", response_class=HTMLResponse)
async def account_page(tab: str = ""):
    """The account surface (projects / files / API key / account) — same shell, the frontend reads
    the tab from the path. Directly visitable so a refresh or a Stripe return lands on the right tab."""
    return _shell()


# v1 is RETIRED (2026-07-06) and the /v2 namespace folded into the root. Old /v2 links — shares,
# bookmarks, Stripe return URLs minted before the move — redirect permanently to the clean paths.
@app.get("/v2")
@app.get("/v2/{rest:path}")
async def v2_redirect(rest: str = ""):
    return RedirectResponse(f"/{rest}" if rest else "/", status_code=301)


# ── Single-page plan-builder frontend ──────────────────────────────────────
# The shell lives in app/web/index.html (CSS/JS split into app/web/static, served via the /static
# mount above). Cached on mtime, NOT read-once: uvicorn --reload only watches .py files, so a
# read-once shell kept serving YESTERDAY'S HTML against today's fresh /static JS/CSS after a pull —
# the JS then broke on elements the stale page didn't have (found via an empty research pane,
# 2026-07-05). A stat() per request buys shell edits that always propagate.
_PAGE_CACHE: dict = {}


def _page_text(name: str) -> str:
    f = _WEB_DIR / name
    mt = f.stat().st_mtime
    hit = _PAGE_CACHE.get(name)
    if not hit or hit[0] != mt:
        hit = (mt, f.read_text(encoding="utf-8"))
        _PAGE_CACHE[name] = hit
    return hit[1]
