#!/usr/bin/env python3
"""
skeptic.py — adversarial assumption-checking on the LIVE research path.

`intake.premortem` names a plan's load-bearing assumptions and judges each from the model's own
reasoning over evidence already gathered — an OPINION. This stage turns that opinion into an EVIDENCED
verdict: for each assumption it runs an adversarial research pass that actively hunts DISCONFIRMING
evidence, grades that evidence through FILG's existing source-credibility gate (so a vendor blog
"refuting" an assumption doesn't count like a primary source), and returns survives / weakened / broken
with the cited counter-evidence.

Composition, not new machinery:
  intake.premortem       → the load-bearing assumptions (reused)
  pipeline.research_lane  → inverted into a REFUTATION search per assumption (adversarial prompt)
  pipeline.gate_claims    → grades the disconfirming evidence (the moat composes on the counter-evidence)
  one batched verdict call → weighs the graded counter-evidence per assumption

Ported from the profile skills' hardened patterns (biz-skeptic; business-manager G1/G4/G7): the verdict
runs in FRESH CONTEXT (it sees only the assumption + its graded counter-evidence, never the optimistic
plan, so it can't be anchored), a STRUCTURED verdict schema, and broken assumptions are surfaced
verbatim rather than written around.

WHY THE LIVE PATH (not a corpus): it operates on the time-sensitive, plan-specific specifics — does
THIS buyer pay, is THIS channel saturated NOW — which is exactly the gradeable live content that drives
plan quality and where FILG's moat already lives.

Cost: N assumptions × (one refutation search) + one gate batch + one verdict batch. Real spend — gate
it to high-stakes moments (the vet/kill decision, the first proposal, an explicit "stress-test"), not
every redraft. `mock=True` returns a canned assessment with no API spend.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor


_VERDICTS = ("survives", "weakened", "broken")

_MOCK = {
    "assessments": [
        {"assumption": "Small property managers will pay a monthly fee for AI tenant response",
         "prior_status": "shaky", "verdict": "weakened", "confidence": 0.5,
         "why": "Credible signals of budget resistance in the segment, but not disqualifying.",
         "evidence": [{"text": "Comparable proptech tools show high SMB churn in year one",
                       "url": "https://example.org/report", "tier": "RESEARCH", "judge": "TRUST",
                       "flagged": False}]},
        {"assumption": "Cold email reaches these operators",
         "prior_status": "holds", "verdict": "survives", "confidence": 0.7,
         "why": "No credible disconfirming evidence found.", "evidence": []},
    ],
    "summary": {"survives": 1, "weakened": 1, "broken": 0},
}


def _emit(on_progress, msg: str) -> None:
    if on_progress:
        on_progress(msg)


def _clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _refute(idea: str, assumption: str):
    """One adversarial research pass: web_search for the STRONGEST evidence this assumption is FALSE.
    Returns pipeline.Claim objects (the disconfirming findings, with the source URL each came from)."""
    from engine.pipeline import call, extract_json, Claim, HAIKU, WEB_SEARCH_TOOL, _as_year
    out = call("research_refute", HAIKU, max_tokens=1200, tools=[WEB_SEARCH_TOOL], prompt=(
        "You are a red-team researcher. An operator's business plan DEPENDS on the assumption below "
        "being true. Use web_search to find the STRONGEST real evidence that it is FALSE or overstated: "
        "saturated or shrinking markets, buyers who won't pay, failed comparable attempts, data that "
        "undercuts it. Do NOT confirm it — try to break it. Record the exact source URL for each point.\n\n"
        f"ASSUMPTION (try to break this):\n{assumption}\n\nBUSINESS CONTEXT:\n{idea}\n\n"
        "Reply ONLY with a JSON array of up to 4 disconfirming findings (empty array if you genuinely "
        'find none):\n[{"text": "the disconfirming finding", "source_url": "https://...", '
        '"quantitative": true, "as_of": 2024}]'))
    data = extract_json(out) or []
    claims = []
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict) and c.get("text") and c.get("source_url"):
            claims.append(Claim(text=str(c["text"]).strip(), source_url=str(c["source_url"]).strip(),
                                quantitative=bool(c.get("quantitative", True)), promotes_category=None,
                                as_of=_as_year(c.get("as_of"))))
    return claims


def _verdicts(assumptions: list[str], ev_lists: list[list[dict]], priors: dict) -> list[dict]:
    """One batched call weighing each assumption's GRADED disconfirming evidence. Fresh context: the
    verdict sees only the assumptions + graded counter-evidence, never the optimistic plan (G1)."""
    from engine.pipeline import call, extract_json, SONNET
    blocks = []
    for i, (a, evs) in enumerate(zip(assumptions, ev_lists)):
        eb = "\n".join(f'   - [tier={e["tier"]}, judge={e["judge"]}] {e["text"]} [{e["url"]}]'
                       for e in evs) or "   - (no disconfirming evidence found)"
        blocks.append(f"{i + 1}. ASSUMPTION: {a}\n{eb}")
    out = call("judge", SONNET, max_tokens=60 + 44 * len(assumptions), prompt=(
        "You are a skeptic delivering a verdict on each assumption a business plan depends on. For EACH, "
        "weigh ONLY the DISCONFIRMING evidence listed and how CREDIBLE it is — tier/judge are already "
        "graded, so PRIMARY/TRUST counts fully and VENDOR/FLAG barely counts. Do not use outside "
        "knowledge; judge from the evidence shown.\n\n"
        "survives = no credible disconfirming evidence; the assumption stands.\n"
        "weakened = some credible disconfirming evidence; a real risk to flag.\n"
        "broken = strong, credible disconfirming evidence; building on this is likely a mistake.\n\n"
        + "\n\n".join(blocks) +
        '\n\nReply ONLY with a JSON array: [{"i": 1, "verdict": "survives|weakened|broken", '
        '"confidence": 0.0-1.0, "why": "one blunt sentence citing the evidence"}].'))
    data = extract_json(out)
    by_i = {}
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and "i" in it:
                try:
                    by_i[int(it["i"])] = it
                except (TypeError, ValueError):
                    continue
    out_items = []
    for i, a in enumerate(assumptions):
        it = by_i.get(i + 1, {})
        verdict = str(it.get("verdict", "weakened")).strip().lower()
        if verdict not in _VERDICTS:
            verdict = "weakened"
        out_items.append({"assumption": a, "prior_status": priors.get(a), "verdict": verdict,
                          "confidence": _clamp01(it.get("confidence", 0.5)),
                          "why": (it.get("why") or "").strip(), "evidence": ev_lists[i]})
    return out_items


def _summary(assessments: list[dict]) -> dict:
    s = {v: 0 for v in _VERDICTS}
    for a in assessments:
        s[a["verdict"]] = s.get(a["verdict"], 0) + 1
    return s


def stress_test(idea: str, shaped: dict, research: dict | None = None, mock: bool = False,
                on_progress=None) -> tuple[dict, float]:
    """Adversarially stress-test the plan's load-bearing assumptions with live, gate-graded refutation
    research. Returns ({assessments:[{assumption, prior_status, verdict, confidence, why, evidence}],
    summary:{survives, weakened, broken}}, cost)."""
    if mock:
        _emit(on_progress, "§LANES§" + json.dumps([a["assumption"][:40] for a in _MOCK["assessments"]]))
        for i in range(len(_MOCK["assessments"])):
            _emit(on_progress, f"§LANEDONE§{i}")
        return {k: [dict(x) for x in v] if isinstance(v, list) else dict(v)
                for k, v in _MOCK.items()}, 0.0

    from app import intake  # noqa: PLC0415 — reuse premortem for assumption extraction
    from engine.pipeline import LEDGER, gate_claims, bound
    start = len(LEDGER.rows)

    pre, _c = intake.premortem(idea, shaped, research, mock=False)
    assumptions = [a["assumption"] for a in pre][:4]
    priors = {a["assumption"]: a["status"] for a in pre}
    if not assumptions:
        return {"assessments": [], "summary": _summary([])}, round(LEDGER.cost_slice(start), 4)

    # Paint one live leaf per assumption; green each as its refutation search returns (runner panel).
    _emit(on_progress, "§LANES§" + json.dumps([a[:40] for a in assumptions]))

    def one(idx_assumption):
        i, a = idx_assumption
        claims = _refute(idea, a)
        _emit(on_progress, f"§LANEDONE§{i}")
        return claims

    with ThreadPoolExecutor(max_workers=min(4, len(assumptions))) as ex:
        per = list(ex.map(bound(one), list(enumerate(assumptions))))

    # Grade ALL disconfirming claims in one gate batch, then regroup the graded evidence per assumption.
    flat = [c for lane in per for c in lane]
    verdicts = gate_claims(flat) if flat else []
    ev_lists: list[list[dict]] = [[] for _ in assumptions]
    k = 0
    for i, lane in enumerate(per):
        for c in lane:
            v = verdicts[k]
            k += 1
            ev_lists[i].append({"text": c.text, "url": c.source_url, "tier": v.tier,
                                "judge": v.judge, "flagged": v.flagged})

    assessments = _verdicts(assumptions, ev_lists, priors)
    return {"assessments": assessments, "summary": _summary(assessments)}, \
        round(LEDGER.cost_slice(start), 4)


if __name__ == "__main__":  # self-test (mock, no API)
    res, cost = stress_test("AI tenant-response tool for small property managers",
                            {"thesis": "done-for-you AI tenant response", "founder_edge": "automation"},
                            mock=True)
    assert cost == 0.0 and res["assessments"] and res["summary"]["weakened"] == 1
    assert {a["verdict"] for a in res["assessments"]} <= set(_VERDICTS)
    seen = []
    stress_test("x", {"thesis": "y"}, mock=True, on_progress=seen.append)
    assert any(m.startswith("§LANES§") for m in seen) and any(m.startswith("§LANEDONE§") for m in seen)
    print(f"skeptic.py self-test OK — {len(res['assessments'])} assessments, "
          f"summary={res['summary']}, runner sentinels emitted")
