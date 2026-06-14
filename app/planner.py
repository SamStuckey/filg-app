#!/usr/bin/env python3
"""
The interactive plan-builder — idea → live decision tree → structured, downloadable file tree.

Flow (driven by app/main.py over /api/plan/*):
  1. `research(idea)` runs the teardown engine once → the graded offer + cited evidence (the free
     "answer" shown in the left sidebar).
  2. The builder then walks the plan SECTIONS. For each, `propose()` drafts that file, and the
     operator reacts with a branch — "yes_and" / "not_quite" / "okay_but" (+ an optional note):
       - not_quite → re-draft this section taking the note into account (stay on the node)
       - yes_and / okay_but → finalize the file (folding in the note) and advance
     That choice path *is* the decision tree, and each finalize drops a file into the tree.
  3. When every section is finalized the tree is downloadable (the paid artifact).

`mock=True` returns canned drafts with no API calls (dev/testing, no spend). Real mode synthesizes
each section on Sonnet over ONLY the gate-graded research (label-don't-chase), same as teardown.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

# reuse the engine (prototype/ is a sibling of app/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))
import teardown  # noqa: E402

# The plan = an ordered set of files. Mirrors generate_full()'s artifact set, one file per node.
SECTIONS = [
    {"key": "brief",    "file": "01_brief.md",         "title": "Structured brief"},
    {"key": "offer",    "file": "02_offer.md",          "title": "Offer definition"},
    {"key": "pricing",  "file": "03_pricing.md",        "title": "Packaging & pricing"},
    {"key": "gtm",      "file": "04_go_to_market.md",   "title": "Go-to-market"},
    {"key": "delivery", "file": "05_delivery.md",       "title": "Delivery playbook"},
    {"key": "roadmap",  "file": "06_roadmap.md",        "title": "30-day roadmap"},
]
N = len(SECTIONS)

CHOICES = {"yes_and", "not_quite", "okay_but"}

_MOCK_DRAFT = {
    "brief": ("## Structured brief\n\n**Problem:** the operator can do the work but is stuck on "
              "*what to sell*.\n**Wedge:** a single, specific, outcome-named offer.\n**Who it's "
              "for:** people already trying to solve this and failing.\n**Why now:** demand is "
              "visible and unmet."),
    "offer": ("## Offer\n\nA productized package with one named outcome, fixed scope, flat price, "
              "delivered on a short timeline. No custom quotes, no inventory."),
    "pricing": ("## Packaging & pricing\n\nOne tier to start: a flat setup fee + a small monthly. "
                "Anchor on the outcome's value, not your hours. (Vendor 'leak/ROI' figures are "
                "*unverified* — model per client.)"),
    "gtm": ("## Go-to-market\n\nPost one specific offer in three communities your buyer already "
            "lives in this week. Take the first paying customer before building anything."),
    "delivery": ("## Delivery playbook\n\nDiscovery → build → test → go-live → a monthly "
                 "'what-you-got' report. Templatize each step so it runs the same every time."),
    "roadmap": ("## 30-day roadmap\n\nWk1 reference build · Wk2 list 50 + outreach · Wk3 demos + "
                "pilots · Wk4 convert + ask for one referral."),
}


def research(idea: str, mock: bool = False) -> dict:
    """Step 0 — run the teardown engine once. Returns {prose, rows, stats, cost}."""
    return teardown.generate(idea, mock=mock)


def _cited_flagged(rows: list) -> tuple[str, str]:
    cited = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "ok") or "- (none)"
    flagged = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "warn") or "- (none)"
    return cited, flagged


def propose(idea: str, section_key: str, research_data: dict, history: list,
            note: str | None = None, mock: bool = False) -> tuple[str, float]:
    """Draft one section. `note` is set when re-drafting after a 'not quite'. Returns (draft, cost)."""
    if mock:
        draft = _MOCK_DRAFT[section_key]
        if note:
            draft += f"\n\n*(revised — you said: “{note}”)*"
        return draft, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    section = next(s for s in SECTIONS if s["key"] == section_key)
    cited, flagged = _cited_flagged(research_data["rows"])
    prior = "\n".join(f"- {h['section']}: {h['choice']}" + (f" — “{h['note']}”" if h.get("note") else "")
                      for h in history) or "- (none yet)"
    redirect = f"\nThe operator pushed back on your last draft: “{note}”. Address it." if note else ""
    draft = call(f"plan_{section_key}", SONNET, max_tokens=1100, prompt=(
        f"You are the synthesis stage of FILG, building ONE section of a sellable business plan: "
        f"**{section['title']}**. Be concrete and specific; markdown; no preamble.\n\n"
        "Use the CITED research freely (cite inline with its URL). You MAY reference a FLAGGED "
        "claim only if you append '(unverified vendor claim)'. Never present a flagged number as "
        f"fact.\n\nIDEA:\n{idea}\n\nDECISIONS SO FAR:\n{prior}{redirect}\n\n"
        f"CITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}"))
    return draft, round(LEDGER.cost_slice(start), 4)


def _finalize(section: dict, draft: str, choice: str, note: str | None) -> str:
    """Compose the file content from the accepted draft + the operator's branch."""
    out = draft
    if choice == "okay_but" and note:
        out += f"\n\n> **Operator caveat:** {note}"
    elif choice == "yes_and" and note:
        out += f"\n\n> **Operator add:** {note}"
    return out


def advance(session: dict, choice: str, note: str | None, mock: bool = False) -> dict:
    """Apply a branch to the current node. Mutates and returns a dict of fields to persist:
    {files, step, proposal, history, status, cost}. `not_quite` re-drafts in place; the others
    finalize the file and move on."""
    if choice not in CHOICES:
        raise ValueError(f"bad choice {choice!r}")
    step = session["step"]
    section = SECTIONS[step]
    files = dict(session.get("files") or {})
    history = list(session.get("history") or [])
    cost = session.get("cost") or 0.0
    note = (note or "").strip() or None

    if choice == "not_quite":
        draft, c = propose(session["idea"], section["key"], session["research"], history,
                           note=note, mock=mock)
        history.append({"section": section["key"], "choice": choice, "note": note})
        return {"proposal": {"section": section["key"], "title": section["title"], "draft": draft},
                "history": history, "cost": round(cost + c, 4)}

    # yes_and / okay_but → finalize this file and advance
    files[section["file"]] = _finalize(section, session["proposal"]["draft"], choice, note)
    history.append({"section": section["key"], "choice": choice, "note": note})
    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        draft, c = propose(session["idea"], nxt["key"], session["research"], history, mock=mock)
        return {"files": files, "step": step + 1, "history": history, "cost": round(cost + c, 4),
                "proposal": {"section": nxt["key"], "title": nxt["title"], "draft": draft}}
    return {"files": files, "step": N, "history": history, "status": "done", "proposal": None}


def first_proposal(idea: str, research_data: dict, mock: bool = False) -> tuple[dict, float]:
    """Draft section 0's proposal right after research completes."""
    s0 = SECTIONS[0]
    draft, cost = propose(idea, s0["key"], research_data, [], mock=mock)
    return {"section": s0["key"], "title": s0["title"], "draft": draft}, cost


def bundle_markdown(idea: str, files: dict) -> str:
    """Combine the file tree into one README-style markdown (used for the .md inside the zip)."""
    L = [f"# Business plan — {idea.strip()[:80]}", "",
         "*Built with FILG. Every number is graded by a source-credibility gate — vendor-marketing "
         "stats are labeled, not laundered.*", ""]
    for s in SECTIONS:
        if s["file"] in files:
            L += [files[s["file"]], "", "---", ""]
    return "\n".join(L)


if __name__ == "__main__":  # self-test (mock, no API)
    r = research("I play guitar and want to help people learn", mock=True)
    assert r["prose"]["title"]
    sess = {"idea": "guitar coaching", "research": r, "files": {}, "history": [], "step": 0,
            "cost": 0.0}
    prop, _ = first_proposal(sess["idea"], r, mock=True)
    sess["proposal"] = prop
    assert prop["section"] == "brief"
    # not_quite stays on the same node
    upd = advance(sess, "not_quite", "make it punchier", mock=True)
    assert "revised" in upd["proposal"]["draft"] and "step" not in upd
    sess.update(upd)
    # walk all sections with yes_and
    for _ in range(N):
        sess.update(advance(sess, "yes_and", None, mock=True))
    assert sess["status"] == "done"
    assert len(sess["files"]) == N
    md = bundle_markdown(sess["idea"], sess["files"])
    assert "Business plan" in md and "30-day roadmap" in md
    print("planner.py self-test OK —", N, "sections,", len(sess["files"]), "files")
