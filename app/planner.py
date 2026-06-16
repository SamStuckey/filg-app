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

# reuse the engine (prototype/ is a sibling of app/) and the app-side skill/persona layer (app/).
sys.path.insert(0, str(Path(__file__).resolve().parent))                       # app/  → skills, personas
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "prototype"))  # prototype/ → engine
import teardown  # noqa: E402
import intake  # noqa: E402
import personas  # noqa: E402
import board  # noqa: E402 — Board of Directors review, run inline so its takeaway can steer next draft
import skill_registry as skills  # noqa: E402

# The plan = an ordered set of files. Plain-language titles (operator voice), friendly filenames.
# `guide` (optional) is extra per-section instruction injected into synthesis — it forces the section
# to answer the questions a generic plan leaves vague (what am I actually selling? why me?).
SECTIONS = [
    {"key": "brief",    "file": "1-the-setup.md",            "title": "The setup",             "sub": "who it's for & why now"},
    {"key": "offer",    "file": "2-what-you-sell.md",        "title": "What you sell",         "sub": "the offer & business model",
     "guide": "State plainly WHAT the operator sells AND how it's produced — pick one and name it: a "
              "done-for-you build/service, a productized repeatable package, reselling/white-labeling "
              "an existing tool, or their own software. Make it unambiguous whether they're building "
              "it custom, productizing it, reselling someone else's, or selling a service — and say "
              "exactly what the buyer is paying for."},
    {"key": "why",      "file": "3-why-you-win.md",          "title": "Why you win",           "sub": "alternatives & your edge",
     "guide": "Name the REAL alternatives the buyer weighs — including doing nothing / DIY and the "
              "obvious competitor or substitute — then make the honest, specific case for why THIS "
              "operator wins anyway, led by their unfair advantage (founder edge). No 'we care more'; "
              "give a defensible reason a buyer picks them over the named alternatives."},
    {"key": "pricing",  "file": "4-what-you-charge.md",      "title": "What you charge",       "sub": "packaging & price"},
    {"key": "gtm",      "file": "5-how-you-get-customers.md","title": "How you get customers", "sub": "go-to-market"},
    {"key": "delivery", "file": "6-how-you-deliver.md",      "title": "How you deliver",       "sub": "delivery playbook"},
    {"key": "roadmap",  "file": "7-your-first-30-days.md",   "title": "Your first 30 days",    "sub": "the roadmap"},
]
N = len(SECTIONS)

CHOICES = {"yes_and", "not_quite", "okay_but"}

# "Ask an expert" + "Board of Directors" both draw on the persona registry (app/personas.py) — FILG-
# owned COMPOSITE ARCHETYPES, never real named people. Naming/impersonating a real person would
# trigger right-of-publicity / the ELVIS Act / the NO FAKES Act (commercial use of likeness/voice);
# composite archetypes carry none. The legal + AI-self-ID rules live once in the director_base skill.
# Names kept for back-compat with main.py; sourced from the registry so there's one list of advisors.
ARCHETYPES = personas.catalog()
ARCHETYPE_KEYS = personas.KEYS
_DISCLAIMER = personas.DISCLAIMER

_MOCK_DRAFT = {
    "brief": ("## Structured brief\n\n**Problem:** the operator can do the work but is stuck on "
              "*what to sell*.\n**Wedge:** a single, specific, outcome-named offer.\n**Who it's "
              "for:** people already trying to solve this and failing.\n**Why now:** demand is "
              "visible and unmet."),
    "offer": ("## Offer\n\nA productized service: you build and run one named outcome for the client "
              "(done-for-you), fixed scope, flat price, short timeline. The buyer pays for the outcome, "
              "not your hours — not a tool they self-serve, not a custom one-off."),
    "why": ("## Why you win\n\nThe alternatives are doing nothing, a DIY tool, or a generalist "
            "competitor. You win on a specific unfair advantage — name it and make it the wedge, not a "
            "vague claim of caring more."),
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


def _working_idea(session: dict) -> str:
    """The focused thesis (from intake) is what synthesis runs on; fall back to the raw idea for
    sessions created before intake existed."""
    return (session.get("shaped") or {}).get("thesis") or session["idea"]


def _founder(session: dict) -> str | None:
    """The operator's unfair advantage (from intake) — fed into synthesis so 'Why you win' and the
    offer can lead with it instead of a generic pitch."""
    return (session.get("shaped") or {}).get("founder_edge")


def prepare(idea: str, mock: bool = False) -> dict:
    """Full pre-build pass for a new session: intake (shape the grab-bag into one thesis) → research
    the thesis → vet it (kill-gate) → draft section 0. Returns everything the session needs to start
    building, plus a `cost` total. The raw `idea` is kept by the caller for display; everything
    downstream runs on the focused `shaped['thesis']`."""
    shaped, c_shape = intake.shape(idea, mock=mock)
    thesis = shaped["thesis"]
    research_data = research(thesis, mock=mock)
    vetting, c_vet = intake.vet(idea, shaped, research_data, mock=mock)
    proposal, c_prop = first_proposal(thesis, research_data, founder=shaped.get("founder_edge"),
                                      mock=mock)
    return {"shaped": shaped, "research": research_data, "vetting": vetting, "proposal": proposal,
            "research_cost": research_data["cost"],
            "cost": round(research_data["cost"] + c_shape + c_vet + c_prop, 4)}


def _cited_flagged(rows: list) -> tuple[str, str]:
    cited = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "ok") or "- (none)"
    flagged = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "warn") or "- (none)"
    return cited, flagged


def propose(idea: str, section_key: str, research_data: dict, history: list,
            steer: str | None = None, board_notes: str | None = None,
            founder: str | None = None, mock: bool = False) -> tuple[str, float]:
    """Draft one section. `steer` is a branch instruction (set when re-drafting after a branch with
    a note). `board_notes` are the board's net takeaways on earlier sections — injected so the
    directors actually shape what gets written next, not just comment after the fact. `founder` is the
    operator's unfair advantage (so positioning leads with it). Per-section `guide` (from SECTIONS)
    forces the section to answer what it must. All go into the prompt so the operator's choices, edge,
    and board genuinely steer the output. Returns (draft, cost)."""
    if mock:
        draft = _MOCK_DRAFT[section_key]
        if steer:
            draft += f"\n\n*(revised — {steer})*"
        if board_notes:
            draft += "\n\n*(board-guided)*"
        return draft, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    section = next(s for s in SECTIONS if s["key"] == section_key)
    cited, flagged = _cited_flagged(research_data["rows"])
    prior = "\n".join(f"- {h['section']}: {h['choice']}" + (f" — “{h['note']}”" if h.get("note") else "")
                      for h in history) or "- (none yet)"
    guide_block = f"\n\nWHAT THIS SECTION MUST DO: {section['guide']}" if section.get("guide") else ""
    founder_block = (f"\n\nOPERATOR'S UNFAIR ADVANTAGE (lead positioning with this): {founder}"
                     if founder else "")
    steer_block = f"\n\nOPERATOR DIRECTION (honor this): {steer}" if steer else ""
    board_block = (f"\n\nBOARD GUIDANCE (your directors' net takeaways on earlier sections — honor "
                   f"them):\n{board_notes}") if board_notes else ""
    # Durable instruction (the IP) lives in the synth_section skill → cached system block; only the
    # runtime data (which section, the idea, decisions, graded research, board) goes in the user message.
    draft = call(f"plan_{section_key}", SONNET, max_tokens=1100,
                 system=skills.system("synth_section"), cache=True, prompt=(
        f"SECTION TO WRITE: **{section['title']}** ({section['sub']}).{guide_block}\n\n"
        f"IDEA:\n{idea}\n\nDECISIONS SO FAR:\n{prior}{founder_block}{steer_block}{board_block}\n\n"
        f"CITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}"))
    return draft, round(LEDGER.cost_slice(start), 4)


def _board_notes(reviews: list) -> str | None:
    """Condense per-section board reviews into a guidance block for the next section's synthesis —
    this is how the board's takeaway actually influences the output, not just narrates it."""
    if not reviews:
        return None
    return "\n".join(f"- on “{r.get('title', r.get('section'))}”: {r.get('verdict', '')}"
                     for r in reviews) or None


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


def advance(session: dict, choice: str, note: str | None, mock: bool = False,
            directors: list | None = None) -> dict:
    """Apply a branch to the current node and return the fields to persist
    ({files, step, proposal, history, status, cost, board}).

    - not_quite → re-draft THIS section with a different angle (stay on the node).
    - yes_and / okay_but with a note → re-synthesize THIS section honoring the note, then finalize.
    - yes_and / okay_but with no note → accept the current draft as-is, then finalize.
    The choice+note are appended to history, which feeds every later section's prompt.

    If `directors` is set (the operator built a Board of Directors), each finalized section is vetted
    by the board (board.review_section) — each director's take + a synthesized takeaway — and that
    takeaway is fed into the NEXT section's synthesis, so the board genuinely steers the output."""
    if choice not in CHOICES:
        raise ValueError(f"bad choice {choice!r}")
    step = session["step"]
    section = SECTIONS[step]
    files = dict(session.get("files") or {})
    history = list(session.get("history") or [])
    reviews = list(session.get("board") or [])
    cost = session.get("cost") or 0.0
    note = (note or "").strip() or None
    idea = _working_idea(session)  # synthesize on the focused thesis, not the raw grab-bag
    founder = _founder(session)
    history.append({"section": section["key"], "choice": choice, "note": note})

    if choice == "not_quite":
        draft, c = propose(idea, section["key"], session["research"], history,
                           steer=_steer(choice, note), board_notes=_board_notes(reviews),
                           founder=founder, mock=mock)
        return {"proposal": {"section": section["key"], "title": section["title"], "draft": draft},
                "history": history, "cost": round(cost + c, 4)}

    # yes_and / okay_but → finalize this file (re-synthesizing if the note steers it), then advance
    steer = _steer(choice, note)
    if steer:
        draft, c = propose(idea, section["key"], session["research"], history,
                           steer=steer, board_notes=_board_notes(reviews), founder=founder, mock=mock)
        cost = round(cost + c, 4)
    else:
        draft = session["proposal"]["draft"]
    files[section["file"]] = draft

    # The board reviews the section just finalized; its takeaway then steers the next draft.
    if directors:
        review, bc = board.review_section(idea, section["title"], draft,
                                          bundle_markdown(idea, files), directors, mock=mock)
        reviews.append({"section": section["file"], "title": section["title"], **review})
        cost = round(cost + bc, 4)

    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        draft, c = propose(idea, nxt["key"], session["research"], history,
                           board_notes=_board_notes(reviews), founder=founder, mock=mock)
        upd = {"files": files, "step": step + 1, "history": history, "cost": round(cost + c, 4),
               "proposal": {"section": nxt["key"], "title": nxt["title"], "draft": draft}}
    else:
        upd = {"files": files, "step": N, "history": history, "status": "done",
               "proposal": None, "cost": round(cost, 4)}
    if directors:
        upd["board"] = reviews
    return upd


# ── Branching decision tree (Next / Back / navigate between branches) ─────────
# The builder is a *tree* of section-proposals, not one straight line. Each node is a proposal for a
# single section, carrying the files its ancestors finalized. Going forward (Next) finalizes this
# node's section and drafts the next one as a child; going back (Back) re-drafts the PREVIOUS section
# as a new sibling branch. These functions are pure — node content in, node content out — so the tree
# wiring (ids, parent/child links, the active pointer) lives in app/main.py and stays testable here.

def root_node(proposal: dict) -> dict:
    """The tree's first node — the section-0 proposal, nothing finalized upstream yet."""
    s0 = SECTIONS[0]
    return {"step": 0, "section": s0["key"], "title": s0["title"], "sub": s0["sub"],
            "draft": proposal["draft"], "files": {}, "history": [], "board": [], "feedback": None}


def forward(idea: str, research_data: dict, node: dict, feedback: str | None,
            directors: list | None = None, founder: str | None = None,
            mock: bool = False) -> tuple[dict, float]:
    """Finalize `node`'s section (re-synthesizing if `feedback` steers it), optionally let the board
    review it, then draft the next section. Returns (child_node_content, cost). When the section just
    finalized is the last one, the child is a terminal 'done' node (no draft)."""
    step = node["step"]
    section = SECTIONS[step]
    files = dict(node["files"])
    reviews = list(node["board"])
    fb = (feedback or "").strip() or None
    history = list(node["history"]) + [{"section": section["key"], "choice": "next", "note": fb}]
    cost = 0.0
    if fb:  # a forward note adds/extends — re-synthesize this section honoring it, then finalize
        final, c = propose(idea, section["key"], research_data, history,
                           steer=_steer("yes_and", fb), board_notes=_board_notes(node["board"]),
                           founder=founder, mock=mock)
        cost += c
    else:
        final = node["draft"]
    files[section["file"]] = final
    if directors:
        review, bc = board.review_section(idea, section["title"], final,
                                          bundle_markdown(idea, files), directors, mock=mock)
        reviews = reviews + [{"section": section["file"], "title": section["title"], **review}]
        cost += bc
    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        draft, c = propose(idea, nxt["key"], research_data, history,
                           board_notes=_board_notes(reviews), founder=founder, mock=mock)
        cost += c
        child = {"step": step + 1, "section": nxt["key"], "title": nxt["title"], "sub": nxt["sub"],
                 "draft": draft, "files": files, "history": history, "board": reviews, "feedback": fb}
    else:
        child = {"step": N, "section": None, "title": "Plan complete", "sub": "", "draft": None,
                 "files": files, "history": history, "board": reviews, "feedback": fb}
    return child, round(cost, 4)


def rebranch(idea: str, research_data: dict, node: dict, feedback: str,
             founder: str | None = None, mock: bool = False) -> tuple[dict, float]:
    """Re-draft `node`'s section taking `feedback` as a redirect — a fresh sibling branch of `node`.
    Used by Back: the operator revises a previous step, spawning a new branch from that point. The
    section isn't finalized (it becomes the live proposal again), so no board review runs here."""
    section = SECTIONS[node["step"]]
    draft, c = propose(idea, section["key"], research_data, node["history"],
                       steer=_steer("not_quite", feedback), board_notes=_board_notes(node["board"]),
                       founder=founder, mock=mock)
    sib = {"step": node["step"], "section": section["key"], "title": section["title"],
           "sub": section["sub"], "draft": draft, "files": dict(node["files"]),
           "history": list(node["history"]), "board": list(node["board"]), "feedback": feedback}
    return sib, round(c, 4)


def first_proposal(idea: str, research_data: dict, founder: str | None = None,
                   mock: bool = False) -> tuple[dict, float]:
    """Draft section 0's proposal right after research completes."""
    s0 = SECTIONS[0]
    draft, cost = propose(idea, s0["key"], research_data, [], founder=founder, mock=mock)
    return {"section": s0["key"], "title": s0["title"], "draft": draft}, cost


def ask_expert(idea: str, files: dict, archetype_key: str, question: str,
               mock: bool = False) -> tuple[dict, float]:
    """An add-on: get a take on the plan in a composite persona's voice (the one-off version of the
    board). Returns ({archetype, answer}, cost). Always prepends the AI / not-pro-advice disclosure.
    The persona's instruction = the shared director_base skill + that persona's voice (personas.py)."""
    p = personas.get(archetype_key)
    q = (question or "").strip() or "What would you change to make this actually work?"
    if mock:
        return {"archetype": p["name"],
                "answer": f"{_DISCLAIMER}\n\n**{p['name']}** on “{q}”: tighten the offer to one "
                          f"outcome, charge for it up front, and go get one yes this week. (mock)"}, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    plan = "\n\n".join(f"## {f}\n{c}" for f, c in files.items()) or "(plan still in progress)"
    ans = call(f"expert_{archetype_key}", SONNET, max_tokens=700,
               system=personas.system_for(archetype_key), cache=True, prompt=(
        f"IDEA: {idea}\n\nPLAN SO FAR:\n{plan}\n\nOPERATOR'S QUESTION: {q}"))
    return {"archetype": p["name"], "answer": f"{_DISCLAIMER}\n\n{ans}"}, round(LEDGER.cost_slice(start), 4)


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
    assert "revised" not in sess["files"]["7-your-first-30-days.md"]  # plain-accepted kept as-is
    md = bundle_markdown(sess["idea"], sess["files"])
    assert "Business plan" in md
    exp, _ = ask_expert(sess["idea"], sess["files"], "closer", "is the price right?", mock=True)
    assert exp["archetype"] == "The Closer" and "AI composite" in exp["answer"]
    # prepare(): intake shapes a grab-bag → thesis drives synthesis; vet returns a verdict
    prep = prepare("I like basketball, MTG, food, and I'm good at sales", mock=True)
    assert prep["shaped"]["thesis"] and prep["vetting"]["verdict"] in ("pursue", "pivot", "kill")
    sess2 = {"idea": "raw grab-bag", "shaped": prep["shaped"], "research": prep["research"],
             "files": {}, "history": [], "step": 0, "cost": prep["cost"], "proposal": prep["proposal"]}
    assert _working_idea(sess2) == prep["shaped"]["thesis"]  # builds on the focused thesis
    assert _working_idea({"idea": "x"}) == "x"               # back-compat: no shaped → raw idea
    # board-driven advance: each finalized section gets a board review, takeaway steers the next draft
    sess2.update({"history": [], "step": 0, "cost": 0.0, "board": []})
    upd = advance(sess2, "yes_and", None, mock=True, directors=["closer", "cfo"])
    assert len(upd["board"]) == 1 and len(upd["board"][0]["directors"]) == 2  # per-director takes
    assert upd["board"][0]["verdict"]                                          # synthesized takeaway
    assert "board-guided" in upd["proposal"]["draft"]                          # takeaway steered next
    assert _board_notes(upd["board"]).startswith("- on")
    # branching tree: root → forward (finalize + next) → rebranch (re-draft previous as a sibling)
    r3 = research("guitar coaching", mock=True)
    prop3, _ = first_proposal("guitar coaching", r3, mock=True)
    root = root_node(prop3)
    assert root["step"] == 0 and root["files"] == {}
    child, _ = forward("guitar coaching", r3, root, "go bolder", mock=True)
    assert child["step"] == 1 and len(child["files"]) == 1            # section 0 finalized
    assert "revised" in child["files"]["1-the-setup.md"]             # forward note steered it
    sib, _ = rebranch("guitar coaching", r3, root, "narrower niche", mock=True)
    assert sib["step"] == 0 and "revised" in sib["draft"] and sib["files"] == {}  # re-draft, no finalize
    # forward to the end → terminal node
    node = child
    while node["step"] < N:
        node, _ = forward("guitar coaching", r3, node, None, mock=True)
    assert node["step"] == N and node["draft"] is None and len(node["files"]) == N
    print("planner.py self-test OK —", N, "sections,", len(sess["files"]),
          "files, expert ok, prepare ok, board ok, tree ok")
