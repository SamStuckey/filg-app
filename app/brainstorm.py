#!/usr/bin/env python3
"""
Brainstorm — the diverge/converge FRONT of the funnel, before deep research.

The old engine was strictly linear: one raw idea -> intake.shape picks ONE thesis
-> deep research -> build. The new UX opens the top of the funnel:

  diverge(idea)            -> 1-3 loose-but-vetted-SHAPE directions (pure LLM, no web)
  merge(idea, directions)  -> ONE reconciled thesis + an adversarial cull + a LIGHT
                              first-pass research skim

Both are deliberately cheaper than the deep run. `diverge` is a single LLM call, no
web at all. `merge` reconciles (one LLM call) then runs the teardown engine with the
re-search chase OFF (`headlines=0`) for a quick graded skim, not the full deep pass.
The deep run (teardown headlines=3, in planner.prepare) still happens later, at commit.

`mock=True` returns canned data with no API calls, for local/frontend dev and tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))                       # app/  -> skills
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))  # prototype/ -> engine
import skill_registry as skills  # noqa: E402
import teardown  # noqa: E402

# How light the merge-stage research skim is: grade the lanes, skip the re-source chase.
MERGE_RESEARCH_HEADLINES = 0

_MOCK_DIVERGE = {
    "spread": "loose",
    "directions": [
        {"title": "Muffins-on-demand for local offices",
         "one_liner": "A standing weekly pastry drop for nearby small offices and cafes.",
         "mold": "Local B2B recurring", "leans_on": "your baking"},
        {"title": "Far-from-home care boxes",
         "one_liner": "A monthly subscription box of homemade goods for people missing home.",
         "mold": "Subscription box / membership", "leans_on": "your baking"},
        {"title": "The wandering baker",
         "one_liner": "A lifestyle account documenting cross-country baking, monetized by an audience.",
         "mold": "Creator / lifestyle audience", "leans_on": "your baking + the story"},
    ],
}

_MOCK_MERGE = {
    "thesis": "A monthly subscription box of regional homemade baked goods, sold to people far "
              "from home, with the cross-country sourcing story used as the marketing engine.",
    "founder_edge": "You actually bake well and can tell the story of where each thing comes from.",
    "mold": "Subscription box / membership",
    "kept": ["the homemade subscription box as the revenue core",
             "the cross-country story as the audience/marketing engine"],
    "dropped": [{"thread": "the standing weekly office drop",
                 "why": "it needs a fixed local route, which fights the on-the-road model"}],
}


def diverge(idea: str, mock: bool = False) -> tuple[dict, float]:
    """Spread a raw prompt into 1-3 distinct, vetted-SHAPE directions. Pure LLM, no web, cheap.
    Returns (result, cost) where result = {spread, directions:[{title, one_liner, mold, leans_on}]}."""
    if mock:
        dirs = [dict(d) for d in _MOCK_DIVERGE["directions"]]
        # echo what we heard, so mock UX runs SHOW the input plumbing working. A pivot input leads
        # with the preamble line — echo the FEEDBACK line (line 2), not the preamble.
        lines = idea.splitlines()
        head = (lines[1] if idea.startswith("THE OPERATOR IS PIVOTING") and len(lines) > 1 else idea)[:56]
        dirs[0]["one_liner"] = f"[mock — heard: \u201c{head}\u201d] " + dirs[0]["one_liner"]
        return {"spread": _MOCK_DIVERGE["spread"], "directions": dirs}, 0.0
    from pipeline import LEDGER, call, extract_json, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    is_pivot = idea.startswith("THE OPERATOR IS PIVOTING")
    feedback = ""
    directions, spread = [], "loose"
    for _ in range(2):   # generate -> validate -> reprompt once (the research-lane seam contract)
        out = call("diverge", SONNET, max_tokens=700, system=skills.system("diverge"), cache=True,
                   prompt=f"The operator typed this in plain text:\n\n{idea}\n\n"
                          f"Spread it into directions now.{feedback}")
        data = extract_json(out)
        data = data if isinstance(data, dict) else {}
        s = str(data.get("spread", "loose")).lower().strip()
        spread = s if s in ("tight", "loose") else "loose"
        directions = _clean_directions(data.get("directions"))
        if directions:
            break
        feedback = ("\n\nYour previous reply had no usable directions — it either echoed the "
                    "instructions/scaffolding back as a direction, or returned nothing parseable. "
                    "Reply with ONLY the JSON described, each direction a REAL business direction.")
    if not directions:
        if is_pivot:   # never launder scaffold or an unusable pivot into a rendered fork — fail LOUD
            raise RuntimeError("That pivot didn't spread into real directions — try rephrasing what "
                               "should change.")
        directions = [{"title": idea.strip()[:60] or "Your idea", "one_liner": idea.strip()[:160],
                       "mold": "", "leans_on": ""}]   # a plain idea may pass through; scaffold may not
    return {"spread": spread, "directions": directions}, round(LEDGER.cost_slice(start), 4)


# prompt scaffolding must NEVER render as product — a direction echoing it is invalid at the seam
# (2026-07-04: a garbage pivot produced an option card titled "THE OPERATOR IS PIVOTING...")
_SCAFFOLD = ("operator is pivoting", "pivot instruction", "outweighs everything", "committed path",
             "steered by:", "spread it into directions")


def _clean_directions(raw) -> list[dict]:
    """The diverge seam's schema validator: real titles, no scaffold echo, max 3."""
    directions = []
    for d in (raw or [])[:3]:
        if not isinstance(d, dict) or not (d.get("title") or "").strip():
            continue
        blob = ((d.get("title") or "") + " " + (d.get("one_liner") or "")).lower()
        if any(s in blob for s in _SCAFFOLD):
            continue
        directions.append({
            "title": (d.get("title") or "").strip(),
            "one_liner": (d.get("one_liner") or "").strip(),
            "mold": (d.get("mold") or "").strip(),
            "leans_on": (d.get("leans_on") or "").strip(),
        })
    return directions


def _reconcile(idea: str, directions: list[dict], mock: bool = False) -> tuple[dict, float]:
    """The LLM reconcile half of merge: chosen directions -> one thesis + kept/dropped cull.
    Split out so merge() can add the light research on top. Returns (reconciled, cost)."""
    if mock:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _MOCK_MERGE.items()}, 0.0
    from pipeline import LEDGER, call, extract_json, SONNET
    start = len(LEDGER.rows)
    chosen = "\n".join(
        f"- {d.get('title', '').strip()}: {d.get('one_liner', '').strip()}"
        + (f" [mold: {d.get('mold', '').strip()}]" if d.get("mold") else "")
        for d in directions) or "- (none)"
    out = call("merge", SONNET, max_tokens=700, system=skills.system("merge"), cache=True, prompt=(
        f"OPERATOR'S ORIGINAL INPUT:\n{idea}\n\nTHE DIRECTIONS THEY CHECKED (reconcile these):\n"
        f"{chosen}\n\nReconcile them now."))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    dropped = []
    for x in (data.get("dropped") or []):
        if isinstance(x, dict) and (x.get("thread") or "").strip():
            dropped.append({"thread": x["thread"].strip(), "why": (x.get("why") or "").strip()})
    reconciled = {
        "thesis": (data.get("thesis") or idea).strip(),
        "founder_edge": (data.get("founder_edge") or "").strip(),
        "mold": (data.get("mold") or "").strip(),
        "kept": [str(k).strip() for k in (data.get("kept") or []) if str(k).strip()],
        "dropped": dropped,
    }
    return reconciled, round(LEDGER.cost_slice(start), 4)


def merge(idea: str, directions: list[dict], mock: bool = False,
          on_progress=None, research: bool = True) -> tuple[dict, float]:
    """Converge the checked directions into ONE thesis (adversarial cull) + a LIGHT first-pass
    research skim. `directions` is the list of chosen direction dicts (from diverge). `research=False`
    skips the skim (pure reconcile, for a fast re-merge). Returns (result, cost) where
    result = {thesis, founder_edge, mold, kept, dropped, research?}."""
    def emit(line: str) -> None:
        if on_progress:
            try:
                on_progress(line)
            except Exception:  # noqa: BLE001 — progress is best-effort, never break the run
                pass

    emit("Reconciling the directions you picked")
    reconciled, c_rec = _reconcile(idea, directions, mock=mock)
    cost = c_rec
    for d in reconciled["dropped"]:
        emit(f"✂ dropped: {d['thread']}")
    if research:
        emit("Running a first-pass market skim")
        res = teardown.generate(reconciled["thesis"], headlines=MERGE_RESEARCH_HEADLINES,
                                mock=mock, on_progress=on_progress)
        reconciled["research"] = res
        cost = round(cost + res.get("cost", 0.0), 4)
    return reconciled, round(cost, 4)


if __name__ == "__main__":  # self-test (mock, no API)
    d, c = diverge("I'm a baker in the middle of nowhere, how do I sell what I make?", mock=True)
    assert c == 0.0 and d["spread"] in ("tight", "loose")
    assert 1 <= len(d["directions"]) <= 3 and all(x["title"] for x in d["directions"])
    assert all("mold" in x for x in d["directions"])
    # real diverge never returns empty — the fallback pass-through
    empty_like, _ = diverge("x", mock=True)
    assert empty_like["directions"]
    # merge: reconcile + cull + a light research skim rides along
    m, c2 = merge("baker in the middle of nowhere", d["directions"], mock=True)
    assert c2 == 0.0 and m["thesis"] and m["founder_edge"]
    assert isinstance(m["kept"], list) and isinstance(m["dropped"], list)
    assert m["dropped"] and m["dropped"][0]["thread"] and m["dropped"][0]["why"]  # honest cut, with a reason
    assert m.get("research") and m["research"]["prose"]["title"]                  # light skim attached
    # research=False → fast re-merge, no skim
    m2, _ = merge("baker", d["directions"], mock=True, research=False)
    assert "research" not in m2
    # skills load and carry the VOICE rule
    assert "mold" in skills.system("diverge").lower() and "reconcile" in skills.system("merge").lower()
    assert "em-dash" in skills.system("diverge")   # VOICE appended
    print("brainstorm.py self-test OK — diverge:", len(d["directions"]), "directions; merge thesis set")
