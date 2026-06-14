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

# The plan = an ordered set of files. Plain-language titles (operator voice), friendly filenames.
SECTIONS = [
    {"key": "brief",    "file": "1-the-setup.md",            "title": "The setup",             "sub": "who it's for & why now"},
    {"key": "offer",    "file": "2-what-you-sell.md",        "title": "What you sell",         "sub": "the offer"},
    {"key": "pricing",  "file": "3-what-you-charge.md",      "title": "What you charge",       "sub": "packaging & price"},
    {"key": "gtm",      "file": "4-how-you-get-customers.md","title": "How you get customers", "sub": "go-to-market"},
    {"key": "delivery", "file": "5-how-you-deliver.md",      "title": "How you deliver",       "sub": "delivery playbook"},
    {"key": "roadmap",  "file": "6-your-first-30-days.md",   "title": "Your first 30 days",    "sub": "the roadmap"},
]
N = len(SECTIONS)

CHOICES = {"yes_and", "not_quite", "okay_but"}

# "Ask an expert" ships as FILG-owned COMPOSITE ARCHETYPES — never real named people. Naming a real
# person would trigger right-of-publicity / the ELVIS Act / the NO FAKES Act (commercial use of
# likeness/voice). Archetypes carry no such exposure. Advisors always self-ID as AI and never give
# professional (legal/tax/financial) advice.
ARCHETYPES = [
    {"key": "closer",       "name": "The Closer",        "blurb": "direct-response sales & pricing nerve"},
    {"key": "bootstrapper", "name": "The Bootstrapper",  "blurb": "ship lean, get to revenue fast"},
    {"key": "brand",        "name": "The Brand Builder", "blurb": "positioning & audience"},
    {"key": "cfo",          "name": "The Skeptical CFO", "blurb": "unit economics & risk"},
]
ARCHETYPE_KEYS = {a["key"] for a in ARCHETYPES}
_DISCLAIMER = ("Heads up — I'm an AI composite advisor (not a real person), and this is general "
               "business thinking, not legal, tax, or financial advice.")

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
            steer: str | None = None, mock: bool = False) -> tuple[str, float]:
    """Draft one section. `steer` is a branch instruction (set when re-drafting after a branch with
    a note) that is injected into the prompt so the operator's choice actually shapes the output.
    Returns (draft, cost)."""
    if mock:
        draft = _MOCK_DRAFT[section_key]
        if steer:
            draft += f"\n\n*(revised — {steer})*"
        return draft, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    section = next(s for s in SECTIONS if s["key"] == section_key)
    cited, flagged = _cited_flagged(research_data["rows"])
    prior = "\n".join(f"- {h['section']}: {h['choice']}" + (f" — “{h['note']}”" if h.get("note") else "")
                      for h in history) or "- (none yet)"
    steer_block = f"\n\nOPERATOR DIRECTION (honor this): {steer}" if steer else ""
    draft = call(f"plan_{section_key}", SONNET, max_tokens=1100, prompt=(
        f"You are the synthesis stage of FILG, building ONE section of a sellable business plan: "
        f"**{section['title']}**. Be concrete and specific; markdown; no preamble.\n\n"
        "Use the CITED research freely (cite inline with its URL). You MAY reference a FLAGGED "
        "claim only if you append '(unverified vendor claim)'. Never present a flagged number as "
        f"fact.\n\nIDEA:\n{idea}\n\nDECISIONS SO FAR:\n{prior}{steer_block}\n\n"
        f"CITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}"))
    return draft, round(LEDGER.cost_slice(start), 4)


# How each branch steers the next draft. The operator's choice + note become a prompt instruction,
# so the buttons genuinely change what the LLM generates (not just a cosmetic footnote).
def _steer(choice: str, note: str | None) -> str | None:
    if choice == "not_quite":
        return (f"The operator rejected the previous draft — redirect: “{note}”. Take a clearly "
                f"different approach." if note
                else "The operator rejected the previous draft. Take a clearly different angle.")
    if not note:
        return None  # plain acceptance — keep the draft as-is, no re-gen
    if choice == "yes_and":
        return f"The operator approves and wants to ADD/extend: “{note}”. Revise to weave this in."
    if choice == "okay_but":
        return f"The operator accepts but with this constraint/objection: “{note}”. Revise to honor it."
    return None


def advance(session: dict, choice: str, note: str | None, mock: bool = False) -> dict:
    """Apply a branch to the current node and return the fields to persist
    ({files, step, proposal, history, status, cost}).

    - not_quite → re-draft THIS section with a different angle (stay on the node).
    - yes_and / okay_but with a note → re-synthesize THIS section honoring the note, then finalize.
    - yes_and / okay_but with no note → accept the current draft as-is, then finalize.
    Either way the choice+note are appended to history, which feeds every later section's prompt."""
    if choice not in CHOICES:
        raise ValueError(f"bad choice {choice!r}")
    step = session["step"]
    section = SECTIONS[step]
    files = dict(session.get("files") or {})
    history = list(session.get("history") or [])
    cost = session.get("cost") or 0.0
    note = (note or "").strip() or None
    history.append({"section": section["key"], "choice": choice, "note": note})

    if choice == "not_quite":
        draft, c = propose(session["idea"], section["key"], session["research"], history,
                           steer=_steer(choice, note), mock=mock)
        return {"proposal": {"section": section["key"], "title": section["title"], "draft": draft},
                "history": history, "cost": round(cost + c, 4)}

    # yes_and / okay_but → finalize this file (re-synthesizing if the note steers it), then advance
    steer = _steer(choice, note)
    if steer:
        draft, c = propose(session["idea"], section["key"], session["research"], history,
                           steer=steer, mock=mock)
        cost = round(cost + c, 4)
    else:
        draft = session["proposal"]["draft"]
    files[section["file"]] = draft
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


def ask_expert(idea: str, files: dict, archetype_key: str, question: str,
               mock: bool = False) -> tuple[dict, float]:
    """An add-on: get a take on the plan in a composite ARCHETYPE's voice. Returns
    ({archetype, answer}, cost). Always prepends the AI / not-professional-advice disclosure."""
    arch = next(a for a in ARCHETYPES if a["key"] == archetype_key)
    q = (question or "").strip() or "What would you change to make this actually work?"
    if mock:
        return {"archetype": arch["name"],
                "answer": f"{_DISCLAIMER}\n\n**{arch['name']}** on “{q}”: tighten the offer to one "
                          f"outcome, charge for it up front, and go get one yes this week. (mock)"}, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    plan = "\n\n".join(f"## {p}\n{c}" for p, c in files.items()) or "(plan still in progress)"
    ans = call(f"expert_{archetype_key}", SONNET, max_tokens=700, prompt=(
        f"You are '{arch['name']}', a FICTIONAL composite business advisor ({arch['blurb']}). You are "
        "NOT a real person and never claim to be; never give legal, tax, or financial advice. Give "
        "punchy, specific, encouraging operator advice in your archetype's distinct voice — react to "
        f"THIS plan, don't speak in generalities.\n\nIDEA: {idea}\n\nPLAN SO FAR:\n{plan}\n\n"
        f"OPERATOR'S QUESTION: {q}"))
    return {"archetype": arch["name"], "answer": f"{_DISCLAIMER}\n\n{ans}"}, round(LEDGER.cost_slice(start), 4)


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
            "cost": 0.0, "status": "building"}
    prop, _ = first_proposal(sess["idea"], r, mock=True)
    sess["proposal"] = prop
    assert prop["section"] == "brief"
    # not_quite stays on the same node and re-drafts
    upd = advance(sess, "not_quite", "make it punchier", mock=True)
    assert "revised" in upd["proposal"]["draft"] and "step" not in upd
    sess.update(upd)
    # yes_and WITH a note re-synthesizes the section (the note steers it, not just a footnote)
    sess.update(advance(sess, "yes_and", "add a freemium hook", mock=True))
    assert "revised" in sess["files"]["1-the-setup.md"] and sess["step"] == 1
    # finish the rest with plain acceptance (no re-gen)
    while sess.get("status") != "done":
        sess.update(advance(sess, "yes_and", None, mock=True))
    assert sess["status"] == "done" and len(sess["files"]) == N
    assert "revised" not in sess["files"]["6-your-first-30-days.md"]  # plain-accepted kept as-is
    md = bundle_markdown(sess["idea"], sess["files"])
    assert "Business plan" in md
    exp, _ = ask_expert(sess["idea"], sess["files"], "closer", "is the price right?", mock=True)
    assert exp["archetype"] == "The Closer" and "AI composite" in exp["answer"]
    print("planner.py self-test OK —", N, "sections,", len(sess["files"]), "files, expert ok")
