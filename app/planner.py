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

import json
import re
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
              "obvious competitor or substitute, then make the specific, defensible case for why THIS "
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
              "not your hours, not a tool they self-serve, not a custom one-off."),
    "why": ("## Why you win\n\nThe alternatives are doing nothing, a DIY tool, or a generalist "
            "competitor. You win on a specific unfair advantage, name it and make it the wedge, not a "
            "vague claim of caring more."),
    "pricing": ("## Packaging & pricing\n\nOne tier to start: a flat setup fee + a small monthly. "
                "Anchor on the outcome's value, not your hours. (Vendor 'leak/ROI' figures are "
                "*unverified*, model per client.)"),
    "gtm": ("## Go-to-market\n\nPost one specific offer in three communities your buyer already "
            "lives in this week. Take the first paying customer before building anything."),
    "delivery": ("## Delivery playbook\n\nDiscovery → build → test → go-live → a monthly "
                 "'what-you-got' report. Templatize each step so it runs the same every time."),
    "roadmap": ("## 30-day roadmap\n\nWk1 reference build · Wk2 list 50 + outreach · Wk3 demos + "
                "pilots · Wk4 convert + ask for one referral."),
}


# "Waste of time" mode — when the operator forces past the kill gate with no substance, each section is
# a self-aware comedic placeholder. NO research, NO board, NO API calls → ~$0, and by design never a
# credible-looking plan (protects invariant #1: a no-substance idea yields obvious comedy, not a laundered
# plan). The off-ramp is always open: add a real skill/asset/buyer and /revet turns this into a real build.
WOD = {
    "brief":    ("## The setup\n\n**Who it's for:** unclear. **Why now:** also unclear. We asked, you "
                 "clicked the button. Name one real skill or who'd pay and this becomes a real setup."),
    "offer":    ("## What you sell\n\nNo clue, you tell me. You're the one mashing the button. The moment "
                 "you name one real thing you can do, this turns into an actual offer."),
    "why":      ("## Why you win\n\nYou win because you out-clicked the gate. That is not a moat. Give us "
                 "one real edge and we'll write you a real one."),
    "pricing":  ("## What you charge\n\nCharge whatever you like for nothing. The market's counter-offer "
                 "is also nothing. Add a real deliverable and we'll price it."),
    "gtm":      ("## How you get customers\n\nStep one: have something to sell. We're still waiting on "
                 "step one."),
    "delivery": ("## How you deliver\n\nDeliver what, exactly? Name the thing and we'll build the playbook."),
    "roadmap":  ("## Your first 30 days\n\nDay 1 to 30: keep clicking this button. Results: the same as "
                 "now. Or give the gate something real and start over with an actual idea."),
}


def wod_forward(node: dict) -> tuple[dict, float]:
    """'Waste of time' forward: finalize the current section as a comedic placeholder and draft the next
    one the same way. Pure + zero-cost (no API, no research, no board). Returns (child, 0.0)."""
    step = node["step"]
    section = SECTIONS[step]
    files = dict(node["files"])
    files[section["file"]] = WOD[section["key"]]
    history = list(node["history"]) + [{"section": section["key"], "choice": "next", "note": None}]
    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        child = {"step": step + 1, "section": nxt["key"], "title": nxt["title"], "sub": nxt["sub"],
                 "draft": WOD[nxt["key"]], "files": files, "history": history,
                 "board": list(node["board"]), "feedback": None, "wod": True}
    else:
        child = {"step": N, "section": None, "title": "Plan complete", "sub": "", "draft": None,
                 "files": files, "history": history, "board": list(node["board"]),
                 "feedback": None, "wod": True}
    return child, 0.0


def research(idea: str, mock: bool = False, on_progress=None) -> dict:
    """Step 0 — run the teardown engine once. Returns {prose, rows, stats, cost}. `on_progress` streams
    the fan-out milestones (incl. the `§LANES§`/`§LANEDONE§` leaf events) up to the caller."""
    return teardown.generate(idea, mock=mock, on_progress=on_progress)


def _working_idea(session: dict) -> str:
    """The focused thesis (from intake) is what synthesis runs on; fall back to the raw idea for
    sessions created before intake existed."""
    return (session.get("shaped") or {}).get("thesis") or session["idea"]


def _founder(session: dict) -> str | None:
    """The operator's unfair advantage (from intake) — fed into synthesis so 'Why you win' and the
    offer can lead with it instead of a generic pitch."""
    return (session.get("shaped") or {}).get("founder_edge")


def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return (m.group(1).replace("www.", "") if m else (url or "")).strip() or "a source"


def _own_lanes(lanes: list[str]) -> list[dict]:
    """Assign each auto-planned research lane a persona OWNER (who 'asks' that question), so the
    fan-out surfaces as 'Nia is digging into distribution' instead of an anonymous web search. Uses
    the persona router (skill-set overlap); the skeptic is a standing seat and never owns a lane."""
    pool = [k for k in personas.KEYS if not (personas.get(k) or {}).get("standing")]
    owned, used = [], set()
    for ln in lanes:
        ranked = personas.route(ln, k=len(pool))          # full ranking, best fit first
        key = next((k for k in ranked if k not in used), ranked[0])  # distinct owners when possible
        used.add(key)
        p = personas.get(key) or {}
        owned.append({"lane": ln, "owner": key, "owner_name": p.get("name"),
                      "owner_first": p.get("first")})
    return owned


def prepare(idea: str, mock: bool = False, on_progress=None) -> dict:
    """Full pre-build pass for a new session: intake (shape the grab-bag into one thesis) → research
    the thesis → vet it (kill-gate) → draft section 0. Returns everything the session needs to start
    building, plus a `cost` total. The raw `idea` is kept by the caller for display; everything
    downstream runs on the focused `shaped['thesis']`. `on_progress(line)` (optional) is called at each
    real milestone, including a receipt per graded source, so the UI can spew live progress."""
    def emit(line: str) -> None:
        if on_progress:
            try:
                on_progress(line)
            except Exception:  # noqa: BLE001 — progress is best-effort, never break the run
                pass

    emit("Focusing your idea into one sharp thesis")
    shaped, c_shape = intake.shape(idea, mock=mock)
    thesis = shaped["thesis"]
    emit("Planning the research fan-out")
    research_data = research(thesis, mock=mock, on_progress=on_progress)
    owned = _own_lanes(research_data.get("lanes") or [])
    research_data["owned_lanes"] = owned                # who researched what — surfaced in the UI
    if mock and owned:   # real mode streams the leaf events live from build_evidence as each lane returns;
        emit("§LANES§" + json.dumps([o["lane"] for o in owned]))   # mock skips that path, so paint them here
        for i in range(len(owned)):
            emit("§LANEDONE§" + str(i))
    for o in owned:                                     # the fan-out, as persona-owned lanes
        who = o.get("owner_first") or o.get("owner_name") or "A researcher"
        emit(f"🔎 {who} ({o.get('owner_name')}) is digging into: {o['lane']}")
    rows = research_data.get("rows") or []
    for r in rows:                                   # the receipts — the gate's verdict per source
        emit((f"✓ cited {_host(r.get('url',''))}" if r.get("mark") == "ok"
              else f"⚠ flagged {_host(r.get('url',''))} (vendor)"))
    emit(f"Graded {len(rows)} source" + ("" if len(rows) == 1 else "s"))
    vetting, c_vet = intake.vet(idea, shaped, research_data, mock=mock)
    emit(f"Verdict: {vetting.get('verdict', 'pursue')}")
    emit("Pressure-testing the assumptions your plan rests on")
    pm, c_pm = intake.premortem(idea, shaped, research_data, mock=mock)
    vetting["premortem"] = pm                          # rides along in the persisted vetting JSON
    for a in pm:
        emit(f"• assumption [{a['status']}]: {a['assumption']}")
    emit("Drafting your first offer")
    proposal, c_prop = first_proposal(thesis, research_data, founder=shaped.get("founder_edge"),
                                      mock=mock)
    return {"shaped": shaped, "research": research_data, "vetting": vetting, "proposal": proposal,
            "research_cost": research_data["cost"],
            "cost": round(research_data["cost"] + c_shape + c_vet + c_pm + c_prop, 4)}


def _cited_flagged(rows: list) -> tuple[str, str]:
    cited = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "ok") or "- (none)"
    flagged = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "warn") or "- (none)"
    return cited, flagged


def propose(idea: str, section_key: str, research_data: dict, history: list,
            steer: str | None = None, board_notes: str | None = None,
            founder: str | None = None, mock: bool = False,
            plan_so_far: str | None = None) -> tuple[str, float]:
    """Draft one section. `steer` is a branch instruction (set when re-drafting after a branch with
    a note). `board_notes` are the board's net takeaways on earlier sections — injected so the
    directors actually shape what gets written next, not just comment after the fact. `founder` is the
    operator's unfair advantage (so positioning leads with it). Per-section `guide` (from SECTIONS)
    forces the section to answer what it must. All go into the prompt so the operator's choices, edge,
    and board genuinely steer the output. Returns (draft, cost)."""
    if mock:
        draft = _MOCK_DRAFT[section_key]
        if steer:
            draft += f"\n\n*(revised, {steer})*"
        if board_notes:
            draft += "\n\n*(board-guided)*"
        return draft, 0.0

    from pipeline import LEDGER, call, SONNET  # heavy; only in real mode
    start = len(LEDGER.rows)
    section = next(s for s in SECTIONS if s["key"] == section_key)
    cited, flagged = _cited_flagged(research_data["rows"])
    # Optional METHOD grounding (app/rag): when a curated method corpus is ingested, inject the cited
    # passages relevant to this section. Self-disabling — no corpus → empty block → prompt unchanged.
    # Wrapped so grounding can NEVER break plan building (a bad key / missing dep just skips it).
    method_block = ""
    try:
        from rag import grounding  # noqa: PLC0415 — additive integration, lazy so it's optional
        method_block, _msrc, _mcost = grounding.method_grounding(
            f"{section['title']}: {section.get('guide', '')}\nBUSINESS: {idea}", mock=mock)
    except Exception:
        method_block = ""
    prior = "\n".join(f"- {h['section']}: {h['choice']}" + (f" — “{h['note']}”" if h.get("note") else "")
                      for h in history) or "- (none yet)"
    guide_block = f"\n\nWHAT THIS SECTION MUST DO: {section['guide']}" if section.get("guide") else ""
    founder_block = (f"\n\nOPERATOR'S UNFAIR ADVANTAGE (lead positioning with this): {founder}"
                     if founder else "")
    steer_block = f"\n\nOPERATOR DIRECTION (honor this): {steer}" if steer else ""
    board_block = (f"\n\nBOARD GUIDANCE (your directors' net takeaways on earlier sections — honor "
                   f"them):\n{board_notes}") if board_notes else ""
    # The sections already written — so this one BUILDS ON them (connect, stay consistent, don't repeat)
    # rather than reading as an unrelated blob.
    plan_block = (f"\n\nPLAN SO FAR (the sections already written — connect to these and build on them, "
                  f"do NOT repeat them):\n{plan_so_far}") if plan_so_far else ""
    # Durable instruction (the IP) lives in the synth_section skill → cached system block; only the
    # runtime data (which section, the idea, decisions, graded research, board) goes in the user message.
    draft = call(f"plan_{section_key}", SONNET, max_tokens=800,
                 system=skills.system("synth_section"), cache=True, prompt=(
        f"SECTION TO WRITE: **{section['title']}** ({section['sub']}).{guide_block}\n\n"
        f"IDEA:\n{idea}\n\nDECISIONS SO FAR:\n{prior}{plan_block}{founder_block}{steer_block}{board_block}"
        f"{method_block}\n\n"
        f"CITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}"))
    return draft, round(LEDGER.cost_slice(start), 4)


def _change_note(idea: str, feedback: str | None, section: dict, mock: bool = False) -> tuple[str | None, float]:
    """A one-line, plain-language flag of HOW the operator's note was folded into the plan — shown as
    a callout at the top of the affected section so the change is visible, not silent. (feedback empty
    → no note.)"""
    fb = (feedback or "").strip()
    if not fb:
        return None, 0.0
    if mock:
        return (f"Folded in your note (“{fb[:60]}”): this part now takes it into account."), 0.0
    from pipeline import LEDGER, call, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    note = call("plan_change_note", SONNET, max_tokens=120, system=skills.VOICE, cache=True, prompt=(
        f"The operator is building a business plan for: {idea}.\nThey just added this note: \"{fb}\".\n"
        f"In ONE short sentence (no preamble, no quotes), tell them how you folded that note into the "
        f"\"{section['title']}\" section. Be specific about what you actually did with it."))
    return note.strip(), round(LEDGER.cost_slice(start), 4)


_MOCK_QA = {"notes": ["Read all seven sections as one plan — same buyer, offer, and price throughout.",
                      "Tightened a few wordy lines so each part stays skimmable.",
                      "Confirmed every cited link is a real source, no placeholders."],
            "fixed": []}


def qa_plan(idea: str, files: dict, mock: bool = False) -> tuple[dict, dict, float]:
    """Final QA pass over the WHOLE assembled plan, run once right before it's marked complete. The
    facts/numbers were graded earlier and are assumed settled — this never touches a statistic. It
    checks the PLAN reads as ONE coherent piece: consistent buyer/offer/price/channel across sections,
    no contradictions, tight on-voice prose, no broken/placeholder links. Returns (files, report, cost)
    where report = {notes:[...], fixed:[file,...]}. Only sections the editor actually rewrote (keyed by
    exact file path) are applied; everything else is left byte-for-byte unchanged."""
    if not files:
        return files, {"notes": [], "fixed": []}, 0.0
    if mock:
        return dict(files), {"notes": list(_MOCK_QA["notes"]), "fixed": []}, 0.0
    from pipeline import LEDGER, call, extract_json, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    paths = list(files.keys())
    out = call("plan_qa", SONNET, max_tokens=1800, system=skills.VOICE, prompt=(
        "You are the final editor of a finished business plan, doing ONE last QA pass before it ships. "
        "The FACTS and numbers are already graded and settled — do NOT add, remove, or change any "
        "statistic, and never invent one. Your job is to make the whole thing read as ONE coherent "
        "plan:\n"
        "- Fix contradictions across sections — the same buyer, offer, price, and channel everywhere.\n"
        "- Cut wordiness and repetition; every line earns its place.\n"
        "- Keep the voice plain and human (no AI tells).\n"
        "- Fix any broken or placeholder link; leave real cited links exactly as written.\n"
        "Only rewrite a section if it genuinely needs it. Output STRICTLY this JSON, no preamble:\n"
        '{"notes": ["short bullet on what you checked or fixed", ...], '
        '"fixes": {"<exact file path>": "<full corrected markdown for that one section>"}}\n'
        "Leave \"fixes\" empty for any section you did not change; file-path keys must match exactly.\n\n"
        f"IDEA:\n{idea}\n\nVALID FILE PATHS (use these exact strings as fixes keys):\n{paths}\n\n"
        f"THE PLAN:\n{bundle_markdown(idea, files)}"))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    notes = [str(n).strip() for n in (data.get("notes") or []) if str(n).strip()][:6]
    fixes = data.get("fixes") if isinstance(data.get("fixes"), dict) else {}
    revised, fixed = dict(files), []
    for path, body in (fixes or {}).items():
        if path in revised and isinstance(body, str) and len(body.strip()) > 40:
            revised[path] = body.strip()
            fixed.append(path)
    return revised, {"notes": notes, "fixed": fixed}, round(LEDGER.cost_slice(start), 4)


def _plan_so_far(files: dict) -> str | None:
    """The sections already written (in order), as context so the NEXT section connects to and builds on
    them instead of reading as an unrelated blob. Returns None for the first section."""
    parts = [files[s["file"]] for s in SECTIONS if s.get("file") in (files or {})]
    return "\n\n---\n\n".join(parts) if parts else None


def _board_notes(reviews: list) -> str | None:
    """Condense per-section board reviews into a guidance block for the next section's synthesis —
    this is how the board's takeaway actually influences the output, not just narrates it."""
    if not reviews:
        return None
    lines = []
    for r in reviews:
        lines.append(f"- on “{r.get('title', r.get('section'))}”: {r.get('verdict', '')}")
        # the standing skeptic's objection steers the next draft too (not just the net verdict) — but
        # only when it actually pushed back (concern/dissent/non-starter), so an 'agree' adds no noise.
        sk = r.get("skeptic") or {}
        if sk.get("rationale") and sk.get("verdict") in ("concern", "dissent", "non-starter"):
            lines.append(f"  · the skeptic ({sk['verdict']}): {sk['rationale']}")
    return "\n".join(lines) or None


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
                           founder=founder, mock=mock, plan_so_far=_plan_so_far(files))
        return {"proposal": {"section": section["key"], "title": section["title"], "draft": draft},
                "history": history, "cost": round(cost + c, 4)}

    # yes_and / okay_but → finalize this file (re-synthesizing if the note steers it), then advance
    steer = _steer(choice, note)
    if steer:
        draft, c = propose(idea, section["key"], session["research"], history,
                           steer=steer, board_notes=_board_notes(reviews), founder=founder, mock=mock,
                           plan_so_far=_plan_so_far(files))
        cost = round(cost + c, 4)
    else:
        draft = session["proposal"]["draft"]
    files[section["file"]] = draft

    # The board reviews the section just finalized; its takeaway then steers the next draft.
    if directors:
        review, bc = board.review_section(idea, section["title"], draft,
                                          bundle_markdown(idea, files), directors, mock=mock,
                                          extra_personas=session.get("custom_directors"))
        reviews.append({"section": section["file"], "title": section["title"], **review})
        cost = round(cost + bc, 4)

    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        draft, c = propose(idea, nxt["key"], session["research"], history,
                           board_notes=_board_notes(reviews), founder=founder, mock=mock,
                           plan_so_far=_plan_so_far(files))
        upd = {"files": files, "step": step + 1, "history": history, "cost": round(cost + c, 4),
               "proposal": {"section": nxt["key"], "title": nxt["title"], "draft": draft}}
    else:
        files, qa, qc = qa_plan(idea, files, mock=mock)   # final QA pass before the plan is complete
        upd = {"files": files, "step": N, "history": history, "status": "done",
               "proposal": None, "qa": qa, "cost": round(cost + qc, 4)}
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
            mock: bool = False, extra_personas=None) -> tuple[dict, float]:
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
                           founder=founder, mock=mock, plan_so_far=_plan_so_far(files))
        cost += c
    else:
        final = node["draft"]
    files[section["file"]] = final
    if directors:
        review, bc = board.review_section(idea, section["title"], final,
                                          bundle_markdown(idea, files), directors, mock=mock,
                                          extra_personas=extra_personas)
        reviews = reviews + [{"section": section["file"], "title": section["title"], **review}]
        cost += bc
    if step + 1 < N:
        nxt = SECTIONS[step + 1]
        draft, c = propose(idea, nxt["key"], research_data, history,
                           board_notes=_board_notes(reviews), founder=founder, mock=mock,
                           plan_so_far=_plan_so_far(files))
        cost += c
        change, cc = _change_note(idea, fb, nxt, mock=mock)   # flag how the note shaped the next part
        cost += cc
        child = {"step": step + 1, "section": nxt["key"], "title": nxt["title"], "sub": nxt["sub"],
                 "draft": draft, "files": files, "history": history, "board": reviews, "feedback": fb,
                 "change": change}
    else:
        files, qa, qc = qa_plan(idea, files, mock=mock)   # final QA pass before the plan is complete
        cost += qc
        child = {"step": N, "section": None, "title": "Plan complete", "sub": "", "draft": None,
                 "files": files, "history": history, "board": reviews, "feedback": fb, "qa": qa}
    return child, round(cost, 4)


def rebranch(idea: str, research_data: dict, node: dict, feedback: str,
             founder: str | None = None, mock: bool = False) -> tuple[dict, float]:
    """Re-draft `node`'s section taking `feedback` as a redirect — a fresh sibling branch of `node`.
    Used by Back: the operator revises a previous step, spawning a new branch from that point. The
    section isn't finalized (it becomes the live proposal again), so no board review runs here."""
    section = SECTIONS[node["step"]]
    draft, c = propose(idea, section["key"], research_data, node["history"],
                       steer=_steer("not_quite", feedback), board_notes=_board_notes(node["board"]),
                       plan_so_far=_plan_so_far(node.get("files") or {}),
                       founder=founder, mock=mock)
    change, cc = _change_note(idea, feedback, section, mock=mock)
    sib = {"step": node["step"], "section": section["key"], "title": section["title"],
           "sub": section["sub"], "draft": draft, "files": dict(node["files"]),
           "history": list(node["history"]), "board": list(node["board"]), "feedback": feedback,
           "change": change}
    return sib, round(c + cc, 4)


_MOCK_NUDGES = ["go bolder", "narrower niche", "cheaper entry", "more specific", "add an upsell"]


def nudges(idea: str, section_key: str, draft: str, mock: bool = False) -> tuple[list[str], float]:
    """A handful of SHORT (2-4 word) quick-edit chips a founder might click to revise THIS section of
    THIS business — specific to where the plan stands, not generic. One cheap Haiku call. Returns
    (chips, cost)."""
    if mock:
        return list(_MOCK_NUDGES), 0.0
    section = next((s for s in SECTIONS if s["key"] == section_key), None)
    if not section or not (draft or "").strip():
        return list(_MOCK_NUDGES), 0.0
    from pipeline import LEDGER, call, extract_json, HAIKU
    start = len(LEDGER.rows)
    out = call("nudges", HAIKU, max_tokens=140, system=skills.VOICE, cache=True, prompt=(
        "Suggest 5 SHORT feedback nudges (2-4 words each, lowercase, no punctuation) that this founder "
        "might click to revise this part of their plan. Make them SPECIFIC to this business and this "
        "section — the kind of concrete redirection that fits where the plan actually stands, not "
        "generic advice. Output strictly JSON: {\"chips\": [\"...\", \"...\", \"...\", \"...\", \"...\"]}\n\n"
        f"BUSINESS:\n{idea}\n\nSECTION: {section['title']} ({section['sub']})\n\nCURRENT DRAFT:\n{draft[:900]}"))
    data = extract_json(out)
    chips = data.get("chips") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    chips = [str(c).strip() for c in (chips or []) if str(c).strip()][:6]
    return (chips or list(_MOCK_NUDGES)), round(LEDGER.cost_slice(start), 4)


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
    L = [f"# Business plan: {idea.strip()[:80]}", "",
         "*Built with FILG. Every number is graded by a source-credibility gate, vendor-marketing "
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
    assert child["change"] and "go bolder" in child["change"]        # the fold-in is flagged
    sib, _ = rebranch("guitar coaching", r3, root, "narrower niche", mock=True)
    assert sib["step"] == 0 and "revised" in sib["draft"] and sib["files"] == {}  # re-draft, no finalize
    assert sib["change"] and "narrower niche" in sib["change"]
    assert forward("guitar coaching", r3, root, None, mock=True)[0]["change"] is None  # no note, no flag
    # forward to the end → terminal node
    node = child
    while node["step"] < N:
        node, _ = forward("guitar coaching", r3, node, None, mock=True)
    assert node["step"] == N and node["draft"] is None and len(node["files"]) == N
    # waste-of-time mode: force past a kill → comedic placeholder, zero spend, still advances the tree
    wod, wc = wod_forward(root)
    assert wc == 0.0 and wod["step"] == 1 and wod.get("wod") and "button" in wod["files"]["1-the-setup.md"].lower()
    print("planner.py self-test OK —", N, "sections,", len(sess["files"]),
          "files, expert ok, prepare ok, board ok, tree ok")
