#!/usr/bin/env python3
"""
The execution layer — the Roadmap (tasks / milestones / goals) and the Codex (artifacts).

FILG builds a plan; this is the layer that MANAGES and EXECUTES it. A task is one step of the
roadmap; a milestone is a dated marker that groups tasks; a goal is an outcome with a measure;
an artifact is a POINTER (URL + the operator's note) to a real thing they made — the ad, the
site, the sales sheet. MVP stores pointers, never files.

This module is plan-scoped state modeled EXACTLY like app/decisions.py: bounded lists, a clean()
validator per entity, node stamps for provenance, free-side CRUD (the routes are pure state, no
model call), and one AI seam (`extract`) that PROPOSES a roadmap from the finished plan. The moat
carries through: every generated task records the plan node, graded claims, and standing decisions
that produced it — "why is this on my list?" has a receipt, and a changed decision surfaces the
WORK it touched (the decisions.impact() pattern, one layer down).

`extract` parses the built plan (section 7 + GTM + delivery) into a starter roadmap. Mock mode is
deterministic (a real parser over the section markdown, with a sensible canned fallback so the
surface is always populated in dev); real mode is one cheap model call, schema-validated with the
deterministic parse as the fallback. `replan` proposes the ONE next action + adjustments — it never
mutates; the operator confirms, same offer-don't-autopin rule as decisions.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone

# ── enums + bounds ──────────────────────────────────────────────────────────
TASK_STATUS = ("todo", "doing", "done", "dropped")
MILESTONE_STATUS = ("pending", "hit", "slipped")
GOAL_STATUS = ("on_track", "at_risk", "met", "dropped")
ARTIFACT_KINDS = ("sheet", "doc", "site", "pdf", "image", "ad", "repo", "other")

MAX_TASKS = 150
MAX_MILESTONES = 20
MAX_GOALS = 10
MAX_ARTIFACTS = 100
MAX_TEXT = 240
MAX_DETAIL = 800
MAX_NOTE = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nid(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:10]


def _oneline(v, cap: int) -> str:
    return " ".join(str(v or "").split()).strip()[:cap]


# ── entity constructors / validators ─────────────────────────────────────────
def clean_task(raw: dict | None) -> dict | None:
    """Validate/normalize a task payload; None when there's no usable text."""
    raw = raw or {}
    text = _oneline(raw.get("text"), MAX_TEXT)
    if len(text) < 3:
        return None
    status = raw.get("status") if raw.get("status") in TASK_STATUS else "todo"
    due = _clean_date(raw.get("due"))
    span = raw.get("span_days")
    try:
        span = max(1, min(60, int(span))) if span not in (None, "") else None
    except (TypeError, ValueError):
        span = None
    return {
        "text": text,
        "detail": _oneline(raw.get("detail"), MAX_DETAIL),
        "status": status,
        "due": due,
        "span_days": span,
        "milestone": raw.get("milestone") or None,
        "blocked_by": [b for b in (raw.get("blocked_by") or []) if isinstance(b, str)][:20],
        "blocker_note": _clean_blocker(raw.get("blocker_note")),
        "artifacts": [a for a in (raw.get("artifacts") or []) if isinstance(a, str)][:MAX_ARTIFACTS],
        "source": raw.get("source") if raw.get("source") in
        ("generated", "chat", "manual", "board") else "manual",
        # provenance — the moat carry-through
        "node": raw.get("node") or None,
        "section": raw.get("section") or None,
        "claims": [c for c in (raw.get("claims") or []) if isinstance(c, str)][:12],
        "decisions": [d for d in (raw.get("decisions") or []) if isinstance(d, str)][:30],
        "feedback": [f for f in (raw.get("feedback") or []) if isinstance(f, dict)][-12:],
    }


def _clean_blocker(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    what = _oneline(raw.get("what"), MAX_TEXT)
    if not what:
        return None
    return {"what": what, "since": raw.get("since") or _now()}


def _clean_date(v) -> str | None:
    """Accept 'YYYY-MM-DD' (the only shape the calendar uses); drop anything else."""
    if not v:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(v))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def new_task(text: str, order: int = 0, **fields) -> dict | None:
    t = clean_task({"text": text, **fields})
    if t is None:
        return None
    t["id"] = _nid("tk")
    t["order"] = order
    t["created_at"] = _now()
    return t


def clean_milestone(raw: dict | None) -> dict | None:
    raw = raw or {}
    title = _oneline(raw.get("title"), MAX_TEXT)
    if len(title) < 3:
        return None
    return {"title": title,
            "due": _clean_date(raw.get("due")),
            "status": raw.get("status") if raw.get("status") in MILESTONE_STATUS else "pending",
            "goal": raw.get("goal") or None,
            "node": raw.get("node") or None,
            "decisions": [d for d in (raw.get("decisions") or []) if isinstance(d, str)][:30]}


def new_milestone(title: str, **fields) -> dict | None:
    m = clean_milestone({"title": title, **fields})
    if m is None:
        return None
    m["id"] = _nid("ms")
    m["created_at"] = _now()
    return m


def clean_goal(raw: dict | None) -> dict | None:
    raw = raw or {}
    text = _oneline(raw.get("text"), MAX_TEXT)
    if len(text) < 3:
        return None
    metric = raw.get("metric") if isinstance(raw.get("metric"), dict) else None
    if metric:
        metric = {"kind": metric.get("kind") if metric.get("kind") in
                  ("count", "dollars", "manual") else "manual",
                  "target": metric.get("target"), "current": metric.get("current") or 0,
                  "source": metric.get("source") or "manual"}
    return {"text": text, "metric": metric, "due": _clean_date(raw.get("due")),
            "status": raw.get("status") if raw.get("status") in GOAL_STATUS else "on_track",
            "node": raw.get("node") or None,
            "decisions": [d for d in (raw.get("decisions") or []) if isinstance(d, str)][:30]}


def new_goal(text: str, **fields) -> dict | None:
    g = clean_goal({"text": text, **fields})
    if g is None:
        return None
    g["id"] = _nid("gl")
    g["created_at"] = _now()
    return g


def clean_artifact(raw: dict | None) -> dict | None:
    """An artifact is a pointer: a title + optional URL + the operator's note (which carries the
    meaning even when the URL is private/unreadable). Kind is inferred from the URL, editable."""
    raw = raw or {}
    title = _oneline(raw.get("title"), MAX_TEXT)
    url = _oneline(raw.get("url"), 600)
    note = _oneline(raw.get("note"), MAX_NOTE)
    if not title and url:
        title = _title_from_url(url)
    if len(title) < 2 and not note:
        return None
    kind = raw.get("kind")
    if kind not in ARTIFACT_KINDS:
        kind = _kind_from_url(url)
    return {"title": title or "Untitled", "url": url, "note": note, "kind": kind,
            "tasks": [t for t in (raw.get("tasks") or []) if isinstance(t, str)][:MAX_TASKS],
            "node": raw.get("node") or None}


def new_artifact(**fields) -> dict | None:
    a = clean_artifact(fields)
    if a is None:
        return None
    a["id"] = _nid("ar")
    a["added_at"] = _now()
    return a


def _title_from_url(url: str) -> str:
    m = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0]
    return m[:MAX_TEXT] if m else ""


def _kind_from_url(url: str) -> str:
    u = (url or "").lower()
    if "docs.google.com/spreadsheets" in u or "airtable" in u:
        return "sheet"
    if "docs.google.com/document" in u or "notion.so" in u:
        return "doc"
    if u.endswith(".pdf"):
        return "pdf"
    if re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", u):
        return "image"
    if "github.com" in u or "gitlab.com" in u:
        return "repo"
    if u.startswith("http"):
        return "site"
    return "other"


# ── roadmap container ─────────────────────────────────────────────────────────
def empty_roadmap() -> dict:
    return {"tasks": [], "milestones": [], "goals": [], "extracted_at": None}


def ordered_tasks(roadmap: dict | None) -> list[dict]:
    """List order: manual `order`, then creation. This is the drag order the list view persists."""
    tasks = [t for t in ((roadmap or {}).get("tasks") or []) if isinstance(t, dict)]
    return sorted(tasks, key=lambda t: (t.get("order", 0), t.get("created_at", "")))


def is_blocked(task: dict, roadmap: dict) -> bool:
    """A task is blocked if it has an external blocker note OR any incomplete blocked_by dependency."""
    if task.get("blocker_note"):
        return True
    by = {t["id"]: t for t in (roadmap.get("tasks") or [])}
    return any(by.get(b, {}).get("status") not in ("done", "dropped")
               for b in (task.get("blocked_by") or []))


def next_action(roadmap: dict | None) -> dict | None:
    """THE one next action (the cadence's spine): the first unblocked, not-done task in list order.
    A deterministic pick so the surface always has a 'do this next' even before `replan` runs."""
    rm = roadmap or {}
    for t in ordered_tasks(rm):
        if t.get("status") in ("todo", "doing") and not is_blocked(t, rm):
            return t
    return None


def progress(roadmap: dict | None) -> dict:
    tasks = [t for t in ((roadmap or {}).get("tasks") or []) if t.get("status") != "dropped"]
    done = sum(1 for t in tasks if t.get("status") == "done")
    return {"total": len(tasks), "done": done,
            "pct": round(100 * done / len(tasks)) if tasks else 0}


def impact_of_decision(roadmap: dict | None, decision_id: str) -> list[dict]:
    """Tasks generated while a decision was in force — the revisit list when that decision changes
    (the decisions.impact() pattern, extended to work). Mirrors app/decisions.impact()."""
    return [{"id": t["id"], "text": t["text"], "status": t.get("status")}
            for t in ((roadmap or {}).get("tasks") or [])
            if decision_id in (t.get("decisions") or [])]


# ── ICS export (zero-OAuth calendar presence — integrations_research.md §Google) ──
def to_ics(roadmap: dict | None, plan_title: str = "FILG roadmap") -> str:
    """A minimal RFC-5545 VCALENDAR of scheduled tasks + dated milestones as all-day VEVENTs. Served
    as a per-plan feed a calendar app subscribes to — no OAuth, works in Google/Apple/Outlook."""
    rm = roadmap or {}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FILG//Roadmap//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_ics_text(plan_title)}"]

    def _event(uid: str, start: str, summary: str, span: int = 1):
        d0 = start.replace("-", "")
        try:
            end = (date.fromisoformat(start) + timedelta(days=max(1, span))).isoformat().replace("-", "")
        except ValueError:
            end = d0
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}@filg", f"DTSTART;VALUE=DATE:{d0}",
                      f"DTEND;VALUE=DATE:{end}", f"SUMMARY:{_ics_text(summary)}", "END:VEVENT"])

    for t in rm.get("tasks") or []:
        if t.get("due") and t.get("status") != "dropped":
            _event(t["id"], t["due"], t["text"], t.get("span_days") or 1)
    for m in rm.get("milestones") or []:
        if m.get("due"):
            _event(m["id"], m["due"], "◆ " + m["title"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _ics_text(v: str) -> str:
    return re.sub(r"([,;\\])", r"\\\1", str(v or "")).replace("\n", "\\n")


# ── extract — the plan → roadmap seam ────────────────────────────────────────
_WEEK = re.compile(r"\b(wk|week)\s*(\d+)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)]|•)\s+(.*\S)")


def _split_actions(text: str) -> list[str]:
    """Pull candidate action lines out of a section's markdown: bullets, numbered items, and
    'Wk1 do X · do Y'-style dotted clauses. FILG writes its own markdown, so this dialect is known."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#") or ln.startswith(">"):
            continue
        m = _BULLET.match(ln)
        body = m.group(1) if m else ln
        # split "Wk1 reference build · list 50 · outreach" into separate actions
        for piece in re.split(r"\s+[·•]\s+|\s+→\s+", body):
            piece = re.sub(r"^\s*(wk|week)\s*\d+\s*[:.\-]?\s*", "", piece, flags=re.I).strip(" .")
            piece = re.sub(r"\*\*|__|`", "", piece)
            if 3 <= len(piece) <= MAX_TEXT and not piece.lower().startswith(("problem", "wedge", "who it")):
                out.append(piece[0].upper() + piece[1:])
    # de-dupe preserving order
    seen, uniq = set(), []
    for a in out:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(a)
    return uniq


def _weeks_in(text: str) -> list[int]:
    return sorted({int(m.group(2)) for m in _WEEK.finditer(text or "")}) or [1, 2, 3, 4]


_CANNED = [
    ("Write your one-line offer and post it where your buyer already is", 0, "gtm"),
    ("Build one reference version of the deliverable", 2, "delivery"),
    ("List 50 real prospects and send the first 10 outreach messages", 4, "gtm"),
    ("Run 3 discovery calls; book 1 pilot", 9, "gtm"),
    ("Set your price and write the one-page proposal", 11, "pricing"),
    ("Deliver the pilot and ask for one referral", 20, "delivery"),
]


def _canned_roadmap(node: str | None, decisions: list[str], today: date) -> dict:
    """A sensible starter roadmap when the plan has no parseable section-7 (fresh/mock sessions).
    Keeps the surface populated so the operator always has something to act on."""
    rm = empty_roadmap()
    ms = new_milestone("First paying customer", due=(today + timedelta(days=30)).isoformat(),
                       node=node, decisions=decisions)
    rm["milestones"] = [ms]
    rm["goals"] = [new_goal("3 paying clients in the first 30 days",
                            metric={"kind": "count", "target": 3, "current": 0, "source": "manual"},
                            due=(today + timedelta(days=30)).isoformat(), node=node,
                            decisions=decisions)]
    for i, (text, offset, section) in enumerate(_CANNED):
        rm["tasks"].append(new_task(text, order=i, due=(today + timedelta(days=offset)).isoformat(),
                                    milestone=ms["id"], source="generated", node=node,
                                    section=section, decisions=decisions))
    rm["extracted_at"] = _now()
    return rm


def extract(session: dict, mock: bool = False) -> tuple[dict, float]:
    """Turn the built plan into a starter roadmap {tasks, milestones, goals}, each item stamped with
    its provenance (source node + standing decisions in force). Returns (roadmap, cost).

    Mock/parse path: read section 7 (+ GTM + delivery) markdown, pull action lines, schedule them
    across the weeks the roadmap names. Real path: one cheap model call for a sharper, better-ordered
    roadmap, schema-validated with the parse as the fallback."""
    files = (session or {}).get("files") or {}
    decisions = [d["id"] for d in ((session or {}).get("decisions") or []) if d.get("id")]
    tree = (session or {}).get("tree") or {}
    node = tree.get("active")
    today = _today()

    roadmap_text = files.get("7-your-first-30-days.md") or ""
    gtm = files.get("5-how-you-get-customers.md") or ""
    delivery = files.get("6-how-you-deliver.md") or ""
    actions = _split_actions(roadmap_text) or (_split_actions(gtm) + _split_actions(delivery))

    if not actions:
        return _canned_roadmap(node, decisions, today), 0.0

    if not mock:
        real = _extract_real(session, actions, node, decisions, today)
        if real is not None:
            return real

    # deterministic parse → tasks spread across the plan's named weeks
    weeks = _weeks_in(roadmap_text)
    rm = empty_roadmap()
    end = new_milestone("First paying customer", due=(today + timedelta(days=30)).isoformat(),
                        node=node, decisions=decisions)
    rm["milestones"] = [end]
    rm["goals"] = [new_goal("Land the first paying customer in 30 days",
                            metric={"kind": "count", "target": 1, "current": 0, "source": "manual"},
                            due=(today + timedelta(days=30)).isoformat(), node=node,
                            decisions=decisions)]
    per = max(1, len(actions) // max(1, len(weeks)))
    for i, text in enumerate(actions[:MAX_TASKS]):
        wk = weeks[min(i // per, len(weeks) - 1)]
        due = (today + timedelta(days=(wk - 1) * 7 + (i % per))).isoformat()
        rm["tasks"].append(new_task(text, order=i, due=due, milestone=end["id"],
                                    source="generated", node=node, section="roadmap",
                                    decisions=decisions))
    rm["extracted_at"] = _now()
    return rm, 0.0


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _extract_real(session, actions, node, decisions, today):
    """One cheap model call to sharpen + order + schedule the parsed actions. Returns a roadmap dict
    or None to fall back to the deterministic parse (unparseable verdict / call failure)."""
    try:
        from engine.pipeline import LEDGER, call, extract_json, HAIKU  # noqa: PLC0415
    except Exception:
        return None
    try:
        raw = call("roadmap_extract", HAIKU, max_tokens=1200, cache=True, prompt=(
            "You are turning a solo operator's finished business plan into a 30-day working roadmap. "
            "Here are the candidate action lines pulled from their plan:\n\n"
            + "\n".join(f"- {a}" for a in actions[:40])
            + "\n\nReturn STRICTLY this JSON, no preamble — concrete, sequenced, each a single "
            "do-able action, day offsets 0-29 from today:\n"
            '{"tasks": [{"text": "<=15 words, imperative", "day": 0}], '
            '"milestone": "the 30-day marker in <=8 words"}'))
        cost = round(LEDGER.cost_slice(len(LEDGER.rows)) if False else 0.0, 4)
    except Exception:
        return None
    data = extract_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return None
    rm = empty_roadmap()
    end = new_milestone(_oneline(data.get("milestone"), 80) or "First paying customer",
                        due=(today + timedelta(days=30)).isoformat(), node=node, decisions=decisions)
    rm["milestones"] = [end]
    rm["goals"] = [new_goal("Land the first paying customer in 30 days",
                            metric={"kind": "count", "target": 1, "current": 0, "source": "manual"},
                            due=(today + timedelta(days=30)).isoformat(), node=node,
                            decisions=decisions)]
    order = 0
    for row in data["tasks"][:MAX_TASKS]:
        if not isinstance(row, dict):
            continue
        try:
            day = max(0, min(29, int(row.get("day", order))))
        except (TypeError, ValueError):
            day = order
        t = new_task(row.get("text") or "", order=order, due=(today + timedelta(days=day)).isoformat(),
                     milestone=end["id"], source="generated", node=node, section="roadmap",
                     decisions=decisions)
        if t:
            rm["tasks"].append(t)
            order += 1
    if not rm["tasks"]:
        return None
    rm["extracted_at"] = _now()
    return rm, 0.0


if __name__ == "__main__":  # self-test (mock, no API)
    # entity validators
    assert clean_task({"text": "x"}) is None and clean_task({"text": "Do the thing"})
    t = new_task("Post the offer", order=3, due="2026-08-01", section="gtm", decisions=["dc1"])
    assert t["id"].startswith("tk") and t["order"] == 3 and t["due"] == "2026-08-01"
    assert clean_task({"text": "t", "status": "bogus"}) is None
    assert new_task("Ship it", status="doing")["status"] == "doing"
    assert new_task("Ship it", due="not-a-date")["due"] is None
    assert new_milestone("Site live", due="2026-09-01")["id"].startswith("ms")
    g = new_goal("3 clients", metric={"kind": "count", "target": 3})
    assert g["metric"]["target"] == 3 and g["metric"]["current"] == 0
    # artifact kind inference from URL
    assert new_artifact(url="https://docs.google.com/spreadsheets/d/abc")["kind"] == "sheet"
    assert new_artifact(url="https://github.com/me/site")["kind"] == "repo"
    assert new_artifact(title="Sales", note="week 1 numbers")["kind"] == "other"
    assert clean_artifact({}) is None
    # extract from a real section-7 markdown
    sess = {"files": {"7-your-first-30-days.md":
            "## 30-day roadmap\n\nWk1 reference build · list 50 · outreach\nWk2 demos · pilots\n"
            "Wk3 convert · ask for a referral"},
            "tree": {"active": "n1"}, "decisions": [{"id": "dc1"}]}
    rm, c = extract(sess, mock=True)
    assert c == 0.0 and len(rm["tasks"]) >= 5 and rm["milestones"]
    assert all(t["source"] == "generated" and t["node"] == "n1" and t["decisions"] == ["dc1"]
               for t in rm["tasks"])
    assert all(t["due"] for t in rm["tasks"])   # every generated task is scheduled
    # canned fallback when there's no parseable plan
    rm2, _ = extract({"files": {}, "tree": {"active": "z"}}, mock=True)
    assert len(rm2["tasks"]) == len(_CANNED) and rm2["goals"]
    # next-action skips blocked + done
    rm3 = empty_roadmap()
    a = new_task("Task A", order=0); b = new_task("Task B", order=1)
    a["status"] = "done"; b["blocker_note"] = {"what": "waiting on LLC", "since": _now()}
    c2 = new_task("Task C", order=2)
    rm3["tasks"] = [a, b, c2]
    assert next_action(rm3)["text"] == "Task C"    # A done, B blocked → C
    assert is_blocked(b, rm3) and not is_blocked(c2, rm3)
    # blocked_by dependency
    c2["blocked_by"] = [a["id"]]
    assert not is_blocked(c2, rm3)            # A is done → not blocked
    c2["blocked_by"] = [b["id"]]
    assert is_blocked(c2, rm3)                # B is not done → blocked
    # progress + impact
    assert progress(rm3)["done"] == 1
    rm3["tasks"][0]["decisions"] = ["dcx"]
    assert impact_of_decision(rm3, "dcx") and not impact_of_decision(rm3, "dcz")
    # ICS export
    ics = to_ics(rm, "My plan")
    assert "BEGIN:VCALENDAR" in ics and "BEGIN:VEVENT" in ics and "DTSTART;VALUE=DATE:" in ics
    print("tasks.py self-test OK —", len(rm["tasks"]), "parsed,", len(rm2["tasks"]), "canned")
