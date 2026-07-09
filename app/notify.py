#!/usr/bin/env python3
"""
Transactional email — the cadence digest + "email me my plan" sender.

Resend was chosen over the Gmail API deliberately (integrations_research.md §Google): reaching the
operator's inbox needs their address, not a restricted Gmail scope + annual CASA audit. Plain REST,
one API token, pure-Python (stdlib urllib) so it fits Render's native runtime.

Inert without a key, exactly like billing/Stripe: unset RESEND_API_KEY → `enabled()` is False and
`send()` is a no-op that reports back so callers (the digest scheduler, a preview route) can run in
dev/test with zero spend and zero external dependency. The compose functions are pure and always
available, so the digest can be PREVIEWED even when sending is off.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API_KEY = os.environ.get("RESEND_API_KEY", "")
# From address — must live on a domain verified in Resend. Default is hello@filg.ai; override with
# FILG_MAIL_FROM (e.g. a fuckitletsgo.ai sender) once that domain is verified too.
FROM = os.environ.get("FILG_MAIL_FROM", "FILG <hello@filg.ai>")
_ENDPOINT = "https://api.resend.com/emails"


def enabled() -> bool:
    return bool(API_KEY)


def send(to: str, subject: str, html: str, text: str = "") -> dict:
    """Send one email. No-op (reported, not raised) when disabled or `to` is empty — the caller
    treats {'sent': False} as 'skipped', never an error, so a keyless dev run is quiet."""
    if not enabled():
        return {"sent": False, "reason": "mail disabled (no RESEND_API_KEY)"}
    if not to:
        return {"sent": False, "reason": "no recipient"}
    body = json.dumps({"from": FROM, "to": [to], "subject": subject, "html": html,
                       **({"text": text} if text else {})}).encode()
    # A real User-Agent + Accept are required: Resend's API is Cloudflare-fronted and blocks the
    # default `Python-urllib/x.y` signature with a 1010 "browser signature" challenge (an HTML block
    # page, not JSON). A named client UA passes it. (The Resend SDK sends `python-requests`, which is
    # why the SDK works and a raw urllib call didn't.)
    req = urllib.request.Request(_ENDPOINT, data=body, method="POST", headers={
        "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "FILG-mailer/1.0 (+https://filg.ai)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = json.loads(r.read().decode() or "{}")
        return {"sent": True, "id": out.get("id")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        # Cloudflare/WAF blocks return HTML, not JSON — surface a clean hint instead of a page dump.
        reason = _err_reason(raw)
        return {"sent": False, "reason": f"http {e.code}: {reason}"}
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": str(e)[:200]}


def _err_reason(raw: str) -> str:
    """Pull a readable reason out of an error body: Resend's JSON `message`, or a WAF hint."""
    try:
        j = json.loads(raw)
        if isinstance(j, dict) and (j.get("message") or j.get("error")):
            return str(j.get("message") or j.get("error"))[:200]
    except Exception:  # noqa: BLE001
        pass
    low = raw.lower()
    if "error code: 1010" in low or "cloudflare" in low:
        return "blocked by Resend's WAF (Cloudflare 1010) — client signature rejected"
    return " ".join(raw.split())[:200]


# ── compose (pure — always available, no key needed) ─────────────────────────
def digest(session: dict, base_url: str = "") -> dict:
    """Compose the weekly roadmap digest: what got done, THE next action, slipped/blocked items.
    Sunday-evening re-engagement framing (integrations_research.md: Friday is where streaks die).
    Returns {subject, html, text} so it can be sent OR previewed. Pure — no model call."""
    from app.domain import tasks as _tk  # noqa: PLC0415
    rm = (session or {}).get("roadmap") or {}
    idea = (session or {}).get("idea") or "your business"
    prog = _tk.progress(rm)
    nxt = _tk.next_action(rm)
    today = _tk._today()
    tasks = _tk.ordered_tasks(rm)
    done = [t for t in tasks if t.get("status") == "done"]
    blocked = [t for t in tasks if _tk.is_blocked(t, rm) and t.get("status") not in ("done", "dropped")]
    slipped = [t for t in tasks if _tk._overdue(t, today) and not _tk.is_blocked(t, rm)]
    link = (base_url.rstrip("/") + f"/plan/{session.get('id')}/roadmap") if base_url and session.get("id") else ""

    subject = (f"Your week on {idea[:40]} — "
               + (f"next: {nxt['text'][:50]}" if nxt else "roadmap complete 🎉"))

    def _li(items):
        return "".join(f"<li>{_esc(t['text'])}</li>" for t in items)
    parts = [f"<h2 style='font-family:sans-serif'>This week on {_esc(idea)}</h2>",
             f"<p style='color:#555'>You've cleared <b>{prog['done']} of {prog['total']}</b> "
             f"steps ({prog['pct']}%).</p>"]
    if nxt:
        parts.append(f"<p style='font-size:16px'><b>Your one next move:</b><br>{_esc(nxt['text'])}</p>")
    if blocked:
        parts.append("<p><b>🚧 Blocked — worth clearing:</b></p><ul>" + _li(blocked[:5]) + "</ul>")
    if slipped:
        parts.append("<p><b>⏰ Slipped past their date:</b></p><ul>" + _li(slipped[:5]) + "</ul>")
    if done:
        parts.append(f"<p><b>✓ Done recently:</b> {len(done)} step(s). Keep going.</p>")
    if link:
        parts.append(f"<p><a href='{link}' style='color:#FF6B4A'>Open your roadmap →</a></p>")
    html = "<div style='max-width:560px;margin:0 auto'>" + "".join(parts) + "</div>"

    tlines = [f"This week on {idea}", f"{prog['done']}/{prog['total']} done ({prog['pct']}%)", ""]
    if nxt:
        tlines += [f"Your one next move: {nxt['text']}", ""]
    if blocked:
        tlines += ["Blocked:"] + [f"  - {t['text']}" for t in blocked[:5]] + [""]
    if slipped:
        tlines += ["Slipped:"] + [f"  - {t['text']}" for t in slipped[:5]] + [""]
    if link:
        tlines.append(f"Open your roadmap: {link}")
    return {"subject": subject, "html": html, "text": "\n".join(tlines)}


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


if __name__ == "__main__":  # self-test (no key, no send)
    assert enabled() is False and send("a@b.com", "x", "<p>y</p>")["sent"] is False
    s = {"id": "sid1", "idea": "cookies",
         "roadmap": {"tasks": [
             {"id": "t1", "text": "Post the offer", "status": "todo", "order": 0},
             {"id": "t2", "text": "File LLC", "status": "todo", "order": 1,
              "blocker_note": {"what": "EIN"}},
             {"id": "t3", "text": "Ship v1", "status": "done", "order": 2}], "milestones": [], "goals": []}}
    d = digest(s, base_url="https://fuckitletsgo.ai")
    assert "Post the offer" in d["subject"] and "cookies" in d["html"]
    assert "File LLC" in d["html"] and "roadmap" in d["text"]
    assert "/plan/sid1/roadmap" in d["html"]
    print("notify.py self-test OK — compose works, sending inert without a key")
