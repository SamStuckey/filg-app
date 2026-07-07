#!/usr/bin/env python3
"""
Deterministic spine — the engine conductor (FILG).

The control flow of the research→grade engine lives HERE, in code, not in a model.
A conductor (`run_engine`) walks a fixed phase DAG and delegates to the existing
pipeline functions at the typed seams (deterministic / research / author / judge).
Every phase transition + verdict + cost slice is logged via `on_phase`, so a run is
reproducible and auditable. The model is a verified subroutine; the script owns the
sequence, the routing, and the side effects.

Design + the full target (validators on author seams, voted boolean gates on the
moat + qa, the VOICE copy-lint loop) live in `filg-docs/engine_spine_design.md`.

Migration step 1 (2026-06-30): wrap the already-deterministic engine chain
(`teardown.build_evidence`) in this conductor with ZERO behavior change — same
lanes, same fan-out concurrency, same `§LANES§`/`§LANEDONE§` progress sentinels,
same graded rows. Validators and voted gates land in later steps. Golden-output
verified against the legacy chain (tests/test_spine.py).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

# ── step kinds (the taxonomy) ────────────────────────────────────────────────
DETERMINISTIC = "deterministic"   # code only; runs or errors
RESEARCH = "research"             # model gathers from the open web, then we structure it
AUTHOR = "author"                 # model generates an artifact from inputs
JUDGE = "judge"                   # model answers atomic checks; the SCRIPT routes

# ── verdicts (the halt protocol) ─────────────────────────────────────────────
PASS = "PASS"     # continue
FIX = "FIX"       # bounded auto-fix loop (re-author)  [wired in a later step]
HALT = "HALT"     # existential — stop, surface verbatim
PAUSE = "PAUSE"   # uncertain / exhausted / human decides
ERROR = "ERROR"   # infra — never proceed


# Per-run cost guard (invariant #2/#3): if a run's cost passes this before the OPTIONAL re-search
# fan-out, skip re-search and label the remaining flagged claims instead of chasing them. Set well
# above a normal run (~$0.4–1) so only a runaway trips it; None disables the guard.
RUN_COST_CAP = 2.0


def _run_cost() -> float:
    from pipeline import LEDGER  # noqa: PLC0415
    try:
        return LEDGER.cost()
    except Exception:  # noqa: BLE001
        return 0.0


@dataclass
class Phase:
    id: str
    kind: str
    desc: str


@dataclass
class PhaseEvent:
    """One row of the run log. The phase id, what kind of seam it was, the verdict the
    conductor routed on, a short human detail, and the cost this phase added. This is the
    audit trail and (later) the showcase activity feed."""
    id: str
    kind: str
    verdict: str
    detail: str = ""
    cost: float = 0.0


# The engine spine as data — adding a phase is editing this list, not the loop.
PHASES: list[Phase] = [
    Phase("plan",      AUTHOR,        "decompose the idea into research lanes"),
    Phase("research",  RESEARCH,      "fan out web research, one lane per worker"),
    Phase("grade",     JUDGE,         "source-credibility gate (the moat)"),
    Phase("re-search", RESEARCH,      "re-source the top flagged claims (label-don't-chase)"),
    Phase("assemble",  DETERMINISTIC, "assemble graded rows + triangulation labels"),
]


# ── deterministic helpers (moved here from teardown; the engine's logic) ──────
def _row_host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return (m.group(1).replace("www.", "") if m else (url or "")).strip().lower()


def _label_triangulation(rows: list) -> None:
    """No extra research calls (label-don't-chase, invariant #2). A cleared claim is 'corroborated'
    only if another cleared claim in the SAME lane cites a DIFFERENT host; otherwise it rests on a
    single source. We just label it — we never go re-search to force a second cite."""
    by_lane: dict = {}
    for r in rows:
        if r["mark"] == "ok":
            by_lane.setdefault(r.get("lane", ""), []).append(_row_host(r["url"]))
    for r in rows:
        if r["mark"] != "ok":
            continue
        hosts = by_lane.get(r.get("lane", ""), [])
        r["sources"] = len(set(hosts))
        r["corroborated"] = len({h for h in hosts if h and h != _row_host(r["url"])}) >= 1


def _stale(v) -> bool:
    return bool(getattr(v, "stale", False))


def _warn_note(v) -> str:
    """Honest per-claim note: a stale-but-not-self-interested flag must NOT read as 'vendor marketing'."""
    checks = getattr(v, "checks", {}) or {}
    if _stale(v) and not checks.get("self_interested"):
        return f"stat is from {v.claim.as_of}, past the staleness window, treat as stale until re-verified"
    return "flagged self-interested/vendor source, unverified"


def _assemble_rows(cleared: list, rescues: list, to_label: list, claim_lane: dict) -> list:
    """Deterministic assembly. tier + judge + stale are the gate's per-claim reasoning — carried
    through so the UI can show HOW the moat graded each number (surfaced in 'see how it works' mode),
    not just the ok/warn outcome."""
    rows: list = []
    for v in cleared:
        rows.append({"mark": "ok", "text": v.claim.text, "url": v.claim.source_url,
                     "note": f"{v.tier.lower()} source, passed the gate",
                     "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of, "stale": _stale(v),
                     "lane": claim_lane.get(id(v.claim), "")})
    for r in rescues:
        if r.rescued and r.new_url:
            rows.append({"mark": "ok", "text": r.original.claim.text, "url": r.new_url,
                         "note": "re-sourced to a primary/neutral cite by the gate",
                         "tier": r.original.tier, "judge": r.original.judge,
                         "as_of": r.original.claim.as_of, "stale": _stale(r.original),
                         "lane": claim_lane.get(id(r.original.claim), "")})
        else:
            v = r.original
            rows.append({"mark": "warn", "text": v.claim.text, "url": v.claim.source_url,
                         "note": "no neutral source found, treat as a vendor marketing claim",
                         "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of, "stale": _stale(v),
                         "lane": claim_lane.get(id(v.claim), "")})
    for v in to_label:
        rows.append({"mark": "warn", "text": v.claim.text, "url": v.claim.source_url,
                     "note": _warn_note(v),
                     "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of, "stale": _stale(v),
                     "lane": claim_lane.get(id(v.claim), "")})
    return rows


# ── the author seam (generate → validate → reprompt) ─────────────────────────
def run_author(generate, validate, feedback_fn=None, max_fix: int = 3):
    """Author seam runner. `generate(feedback: str) -> str` produces the artifact (feedback="" on the
    first attempt); `validate(text) -> list` returns findings ([] = clean). Loop: generate → validate →
    on findings, reprompt with located feedback, bounded by `max_fix`. Accumulates unique findings
    across rounds (so a rewrite doesn't regress earlier fixes) and plateau-stops if a round repeats the
    previous round's finding set. Returns `(text, findings)`: findings is empty on success, or the
    residual on exhaustion/plateau — the CALLER surfaces it; we never strip the text (no post-processing).

    This is the spine's author contract: the model rewrites, the validator gates, the script bounds."""
    if feedback_fn is None:
        import voice_lint  # noqa: PLC0415
        feedback_fn = voice_lint.feedback

    def _sig(findings):
        return frozenset((getattr(f, "kind", str(f)), getattr(f, "context", "")) for f in findings)

    seen: list = []
    seen_keys: set = set()
    feedback = ""
    prev_sig = None
    text = ""
    for _ in range(max_fix):
        text = generate(feedback)
        findings = validate(text)
        if not findings:
            return text, []
        for f in findings:                       # accumulate unique offenders across rounds
            k = (getattr(f, "kind", str(f)), getattr(f, "context", ""))
            if k not in seen_keys:
                seen_keys.add(k)
                seen.append(f)
        sig = _sig(findings)
        if sig == prev_sig:                      # plateau: same offenders twice running → stop
            break
        prev_sig = sig
        feedback = feedback_fn(seen)
    return text, validate(text)


# ── the conductor ────────────────────────────────────────────────────────────
def run_engine(idea: str, headlines: int, on_progress=None, on_phase=None, cost_cap: float | None = -1.0,
               max_lanes: int | None = None, votes: int | None = None):
    """Walk the engine phase DAG, delegating to the pipeline at each seam, and return
    `(rows, stats, lanes)` — byte-identical to the legacy build_evidence chain. `on_progress(line)`
    streams the `§LANES§`/`§LANEDONE§` leaf sentinels for the live UI; `on_phase(PhaseEvent)`
    streams the typed run log (verdict + cost per phase). `max_lanes` caps the research fan-out
    (the funnel's light refine-stage skim researches fewer lanes than the deep run). `votes` sets how
    many times the moat's grade is voted (default = pipeline.JUDGE_VOTES = 3): the committed deep build
    keeps the full ×3 (invariant #1), the throwaway first-pass skim can drop to 1. Lazy import
    keeps `--rebuild` API-free and lets tests monkeypatch the pipeline functions."""
    from pipeline import (JUDGE_VOTES, LEDGER, bound, gate_claims, plan,  # noqa: PLC0415
                          research_lane, research_primary)
    if votes is None:
        votes = JUDGE_VOTES

    def emit(line: str) -> None:
        if on_progress:
            try:
                on_progress(line)
            except Exception:  # noqa: BLE001 — progress is best-effort, never break the run
                pass

    def _start() -> int:
        try:
            return len(LEDGER.rows)
        except Exception:  # noqa: BLE001 — cost logging is best-effort
            return 0

    def _cost(start: int) -> float:
        try:
            return round(LEDGER.cost_slice(start), 4)
        except Exception:  # noqa: BLE001
            return 0.0

    def log(pid: str, kind: str, verdict: str, detail: str, start: int) -> None:
        if on_phase:
            try:
                on_phase(PhaseEvent(pid, kind, verdict, detail, _cost(start)))
            except Exception:  # noqa: BLE001 — the log must never break the run
                pass

    # P1 · plan (author) — decompose the idea into research lanes
    s = _start()
    lanes = plan(idea)
    if max_lanes:
        lanes = lanes[:max_lanes]
    # Announce the fan-out shape so the UI can paint one leaf per research lane up front (grey), then
    # turn each leaf green as its §LANEDONE§ arrives. All emits run on THIS (the prepare) thread.
    emit("§LANES§" + json.dumps(lanes))
    log("plan", AUTHOR, PASS, f"{len(lanes)} research lanes", s)

    # P2 · research (research) — fan out, one lane per worker
    # bound() re-binds the active provider/stack/ledger inside each worker — threads don't inherit
    # contextvars, so without it the fan-out runs on FILG's default key, not the user's BYOK key.
    s = _start()
    lane_claims_map: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(bound(lambda ln=ln: research_lane(idea, ln))): li
                for li, ln in enumerate(lanes)}
        for f in as_completed(futs):
            li = futs[f]
            lane_claims_map[li] = f.result()
            emit("§LANEDONE§" + str(li))   # leaf li → green
    lane_claims = [lane_claims_map.get(li, []) for li in range(len(lanes))]
    # remember which lane each claim came from, so the UI can show who researched what.
    claim_lane = {id(c): lanes[li] for li, lane in enumerate(lane_claims) for c in lane}
    quant = [c for lane in lane_claims for c in lane if c.quantitative]
    log("research", RESEARCH, PASS, f"{len(quant)} quantitative claims", s)

    # P3 · grade (judge) — the source-credibility gate (the moat)
    s = _start()
    verdicts = gate_claims(quant, votes=votes)  # one batched judge call per vote (token win); votes-scaled
    cleared = [v for v in verdicts if not v.flagged]
    flagged = [v for v in verdicts if v.flagged]
    log("grade", JUDGE, PASS, f"{len(cleared)} cleared / {len(flagged)} flagged (×{votes})", s)

    # P4 · re-search (research) — re-source the top flagged claims; label the rest (invariant #2).
    # Cost guard: if the run already blew past the cap, skip the optional fan-out and label everything.
    s = _start()
    cap = RUN_COST_CAP if cost_cap == -1.0 else cost_cap
    rescues: list = []
    if cap is not None and _run_cost() > cap:
        to_chase, to_label = [], list(flagged)
        log("re-search", RESEARCH, PAUSE,
            f"cost cap ${cap:g} hit → labeled {len(flagged)} flagged, skipped re-search", s)
    else:
        to_chase, to_label = flagged[:headlines], flagged[headlines:]
        if to_chase:
            with ThreadPoolExecutor(max_workers=3) as ex:
                rescues = list(ex.map(bound(lambda v: research_primary(v.claim)), to_chase))
            for v, r in zip(to_chase, rescues):
                r.original = v
        log("re-search", RESEARCH, PASS, f"{len(to_chase)} re-sourced / {len(to_label)} labeled", s)

    # P5 · assemble (deterministic) — graded rows + triangulation labels
    rows = _assemble_rows(cleared, rescues, to_label, claim_lane)
    _label_triangulation(rows)
    n_clean = sum(1 for r in rows if r["mark"] == "ok")
    stats = {"checked": len(rows), "cleared": n_clean, "flagged": len(rows) - n_clean}
    log("assemble", DETERMINISTIC, PASS, f"{len(rows)} graded rows", _start())

    return rows, stats, lanes


if __name__ == "__main__":  # dev shakedown: run the engine and print the typed phase log
    import sys
    idea = " ".join(a for a in sys.argv[1:] if not a.startswith("--")) or \
        "a done-for-you AI receptionist for home-service contractors"
    print(f"\nengine spine · {len(PHASES)} phases · idea: {idea}\n")
    for p in PHASES:
        print(f"  · {p.id:10s} [{p.kind}]  {p.desc}")
    print("\n--- run (needs a bound provider / ANTHROPIC key for a real run) ---")
    try:
        rows, stats, lanes = run_engine(idea, headlines=3,
                                        on_phase=lambda e: print(f"  ⚙ {e.id:10s} {e.verdict:5s} {e.detail}"
                                                                 + (f"  ${e.cost:.3f}" if e.cost else "")))
        print(f"\n  {stats}")
    except Exception as e:  # noqa: BLE001 — dev tool: surface why it couldn't run, don't traceback-spam
        print(f"  (no live run: {type(e).__name__}: {e})")
