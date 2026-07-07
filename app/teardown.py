#!/usr/bin/env python3
"""
The offer teardown — FILG's research-run product layer over the engine's graded evidence.

`generate` runs one graded research pass (engine.evidence.build_evidence, in label-don't-chase
mode) and synthesizes the short offer summary (title / idea line / offer / GTM) the funnel
shows; `generate_full` synthesizes the complete artifact set over the same graded rows. Both
speak the business-planning language, so they live in the app — the engine below them carries
no product vocabulary (it gets its subject wording via app/domain/research.FRAMING).

Consumed by the plan builder (planner.research), the merge skim (brainstorm.merge), and the
weekly lead-magnet publisher (scripts/teardown_publish.py).
"""

from __future__ import annotations

from engine import evidence

from app.domain.research import FRAMING

HEADLINES_TO_RESEARCH = 3   # label-don't-chase: re-source only the top N flagged claims


def build_evidence(idea: str, headlines: int, on_progress=None, on_phase=None, max_lanes=None,
                   votes=None, sink=None):
    """The engine's evidence chain, framed for this product. Kept as the app-wide entry point
    (advisor's deep research mode calls it too) so the framing is applied in exactly one place."""
    return evidence.build_evidence(idea, headlines, on_progress=on_progress, on_phase=on_phase,
                                   max_lanes=max_lanes, votes=votes, sink=sink, framing=FRAMING)


def write_prose(idea: str, rows) -> dict:
    # lazy imports: mock mode never touches the pipeline, and tests monkeypatch pipeline.call
    from engine import spine, voice_lint  # noqa: PLC0415
    from engine.pipeline import SONNET, call, extract_json  # noqa: PLC0415
    cleared_block = "\n".join(f"- {r['text']}" for r in rows if r["mark"] == "ok") or "- (none cleared)"
    base = (
        "You write a short, punchy 'Cited Offer Teardown' for an operator audience. From the "
        "plain-text idea and the gate-CLEARED evidence below, output strictly JSON:\n"
        '{"title": "<=8-word hook", "idea_line": "one sentence restating the idea", '
        '"offer": "2-3 sentences: the specific productized thing they would SELL", '
        '"gtm": "one sentence: the sharpest first go-to-market move"}\n\n'
        "Be concrete and specific. Do NOT invent statistics, only the evidence section carries numbers.\n\n"
        f"IDEA:\n{idea}\n\nGATE-CLEARED EVIDENCE (context only):\n{cleared_block}"
    )
    # VOICE author seam: generate → voice-lint → reprompt until clean (bounded). max_fix=2 (T3): the
    # teardown offer summary is a non-final draft (runs on every research pass incl. the throwaway
    # skim); the final artifact set (generate_full) keeps the full 3.
    out, _residual = spine.run_author(
        lambda fb: call("teardown_synth", SONNET, max_tokens=700, prompt=base + (f"\n\n{fb}" if fb else "")),
        voice_lint.lint, max_fix=2)
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    return {"title": data.get("title") or "Cited Offer Teardown",
            "idea_line": data.get("idea_line") or idea[:160],
            "offer": data.get("offer") or "(offer synthesis unavailable)",
            "gtm": data.get("gtm") or "(gtm unavailable)"}


# ─── Public API (used by the app and the publish CLI) ────────────────────────
MOCK_RESULT = {
    "prose": {
        "title": "AI Front Desk for Home-Service Pros",
        "idea_line": "A done-for-you AI receptionist that answers every call and texts back every "
                     "missed lead so contractors stop losing jobs to whoever answers first.",
        "offer": "A fully installed AI front desk, answers 24/7, texts back missed calls in seconds, "
                 "books jobs to the calendar, live in 5 days, flat monthly retainer, no per-lead fees.",
        "gtm": "Cold-call 50 owner-operators in one trade + metro; demo by calling their own after-hours "
               "line, letting it ring out, then showing the AI handle the same call.",
    },
    "rows": [
        {"mark": "ok", "text": "~2.5M home-service businesses operate in the US", "url":
            "https://www.census.gov/", "note": "primary source, passed the gate",
            "tier": "PRIMARY", "judge": "TRUST", "as_of": 2022, "stale": False,
            "sources": 1, "corroborated": False,
            "lane": "What is the market size and number of target buyers?"},
        {"mark": "warn", "text": "62% of calls to small businesses go unanswered", "url":
            "https://www.getaira.io/blog/missed-business-calls-statistics", "note":
            "flagged self-interested/vendor source, unverified",
            "tier": "VENDOR", "judge": "FLAG_SELF_INTERESTED", "as_of": None, "stale": False,
            "lane": "What is the buyer's most acute, expensive pain point?"},
    ],
    "stats": {"checked": 2, "cleared": 1, "flagged": 1},
    "lanes": ["What is the market size and number of target buyers?",
              "Who are the competitors and what are the pricing norms?",
              "What is the buyer's most acute, expensive pain point?"],
    "cost": 0.0,
}


def generate(idea: str, headlines: int = HEADLINES_TO_RESEARCH, mock: bool = False,
             on_progress=None, max_lanes: int | None = None, votes: int | None = None,
             prior_claims: list | None = None) -> dict:
    """Run one teardown and return {prose, rows, stats, cost, claims}. `mock=True` returns canned data
    with no API calls, for local/frontend dev and for testing the metering without spend.
    `on_progress(line)` streams milestones (incl. `§LANES§`/`§LANEDONE§` leaf events) so the UI can
    paint the fan-out live. `max_lanes` caps the research fan-out (the funnel's light skim vs the full
    deep run). `votes` scales the moat's grade vote count (1 on the throwaway skim, default 3 on the
    committed build). `claims` in the result is the fetched (Claim-dict, lane) list, serialized so a
    later run can re-grade WITHOUT re-fetching. `prior_claims` (T2 reuse path) = that carried list; when
    given, the web fan-out is SKIPPED and the claims are re-graded (at full `votes`) + re-source-chased,
    roughly halving the deep build's web wait when the thesis is unchanged from the skim."""
    if mock:
        out = {**MOCK_RESULT, "prose": dict(MOCK_RESULT["prose"]), "claims": []}
        if max_lanes:
            out["lanes"] = list(MOCK_RESULT["lanes"])[:max_lanes]   # mock paints the capped fan-out too
        return out
    from engine import spine  # noqa: PLC0415
    from engine.pipeline import Claim, LEDGER  # noqa: PLC0415
    start = len(LEDGER.rows)
    if prior_claims:
        # Reuse path: re-grade the skim's already-fetched claims at full strength; no plan, no re-fan.
        claim_lanes = [(Claim.from_dict(c), c.get("lane", "")) for c in prior_claims]
        rows, stats = spine.regrade_engine(claim_lanes, headlines, on_progress=on_progress, votes=votes)
        lanes = list(dict.fromkeys(ln for _c, ln in claim_lanes if ln))   # distinct carried lanes
        sink = {"claims": claim_lanes}
    else:
        sink = {}
        rows, stats, lanes = build_evidence(idea, headlines, on_progress=on_progress,
                                            max_lanes=max_lanes, votes=votes, sink=sink)
    prose = write_prose(idea, rows)
    claims_out = [{**c.to_dict(), "lane": ln} for c, ln in sink.get("claims", [])]
    return {"prose": prose, "rows": rows, "stats": stats, "lanes": lanes,
            "claims": claims_out, "cost": round(LEDGER.cost_slice(start), 4)}


MOCK_FULL = {
    "artifacts_md": (
        "# AI Front Desk for Home-Service Pros\n\n## 1. Brief\nOwner-operators lose jobs to whoever "
        "answers first; ~2.5M US home-service businesses ([census.gov](https://www.census.gov/)).\n\n"
        "## 2. Offer\nDone-for-you AI receptionist + missed-call text-back, live in 5 days, flat "
        "retainer.\n\n## 3. Pricing\n$1,500 setup + $500/mo. (Leak figures from vendor blogs are "
        "*(unverified vendor claim)*, model per client.)\n\n## 4. Go-to-market\nCold-call one trade "
        "+ metro; after-hours-call demo.\n\n## 5. Delivery playbook\nDiscovery → build → test → go "
        "live → monthly 'jobs recovered' report.\n\n## 6. 30-day roadmap\nWk1 reference build · Wk2 "
        "list 50 + outreach · Wk3 demos + pilots · Wk4 convert + referral."),
    "rows": MOCK_RESULT["rows"],
    "stats": MOCK_RESULT["stats"],
    "cost": 0.0,
}


def generate_full(idea: str, headlines: int = HEADLINES_TO_RESEARCH, mock: bool = False) -> dict:
    """Paid-tier output: the complete artifact set (brief→offer→pricing→GTM→delivery→roadmap),
    synthesized ONLY over gate-graded research (label-don't-chase). Returns
    {artifacts_md, rows, stats, cost}."""
    if mock:
        return {**MOCK_FULL}
    from engine import spine, voice_lint  # noqa: PLC0415
    from engine.pipeline import LEDGER, SONNET, call  # noqa: PLC0415
    start = len(LEDGER.rows)
    rows, stats, _lanes = build_evidence(idea, headlines)
    cited = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "ok") or "- (none)"
    flagged = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "warn") or "- (none)"
    base = (
        "You are the synthesis stage of FILG. Produce a sellable artifact set in markdown with these "
        "sections: (1) Structured brief, (2) Offer definition, (3) Packaging + pricing, "
        "(4) Go-to-market, (5) Delivery playbook, (6) 30-day roadmap. Be concrete and specific.\n\n"
        "Use the CITED research freely (cite it inline with its URL). You MAY reference a FLAGGED "
        "claim only if you append '(unverified vendor claim)' right after it. Never present a flagged "
        "number as established fact.\n\n"
        f"IDEA:\n{idea}\n\nCITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}")
    # VOICE author seam: generate → voice-lint → reprompt until clean. This is the FINAL assembled
    # artifact set (the paid deliverable), so it keeps the full max_fix=3 (default) — non-final section
    # drafts drop to 2 (T3).
    artifacts, _residual = spine.run_author(
        lambda fb: call("synth_full", SONNET, max_tokens=3500, prompt=base + (f"\n\n{fb}" if fb else "")),
        voice_lint.lint)
    return {"artifacts_md": artifacts, "rows": rows, "stats": stats,
            "cost": round(LEDGER.cost_slice(start), 4)}
