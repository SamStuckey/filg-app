#!/usr/bin/env python3
"""
Server-rendered HTML for the app's shared/served pages — the display layer for the public plan
share (`/p/{sid}`) and the small error shells.

Kept separate from `main.py` so no markup lives in the route layer: the routes do the data work (load
from the store, extract fields) and hand primitives to these pure functions, which return HTML strings.
Matches the stripped-down "Craigslist-plain" app — no decorative brand chrome, no lead-magnet link.
"""

from __future__ import annotations

import html
from urllib.parse import urlparse


CTA = ('<div class="cta"><a class="btn btn-primary" href="https://filg.ai/#start">'
       'Run your own idea →</a></div>')

# Plain shell for /p (the plan share). The old teardown.page_shell used the
# teal Fraunces brand + a "Get these weekly" lead-magnet link; the app is now stripped plain, so the
# share pages match it (no decorative brand, no weekly link).
SHARE_CSS = (
    ":root{--ink:#222;--muted:#666;--line:#ccc;--link:#1a0dab;--ok:#067d2f;--ok-bg:#eef6ef;"
    "--warn:#a85b00;--warn-bg:#f7f1e8}"
    "*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);"
    "font:15px/1.55 Arial,Helvetica,sans-serif}"
    ".wrap{max-width:760px;margin:0 auto;padding:0 18px}"
    "a{color:var(--link)}"
    "nav{display:flex;justify-content:space-between;align-items:center;padding:14px 0;"
    "border-bottom:1px solid var(--line)}"
    ".logo{font-weight:700;font-size:16px;text-decoration:none;color:var(--ink)}"
    "article{padding:24px 0}h1{font-size:24px;margin:0 0 6px}h2{font-size:18px;margin:24px 0 8px}"
    ".eyebrow{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;"
    "letter-spacing:.05em;color:var(--muted);margin-bottom:12px}"
    ".tag{color:var(--muted);font-size:14px;margin:0 0 18px}"
    ".ev{list-style:none;padding:0;margin:12px 0 0}"
    ".ev li{padding:10px 0;border-top:1px solid var(--line);display:flex;gap:10px;font-size:14px}"
    ".ev li:first-child{border-top:0}.ev .ok{color:var(--ok)}.ev .warn{color:var(--warn)}"
    ".ev .note{color:var(--muted);font-size:13px}"
    ".badge{display:inline-block;font-size:11px;font-weight:700;padding:1px 6px;border-radius:4px;margin-left:2px}"
    ".badge.ok{background:var(--ok-bg);color:var(--ok)}.badge.warn{background:var(--warn-bg);color:var(--warn)}"
    ".recpt{margin-top:18px;padding:12px 14px;background:var(--ok-bg);border-radius:6px;font-size:14px}"
    ".trail{margin:12px 0 0;padding-left:20px}.trail li{padding:4px 0;font-size:14px}"
    ".trail .note{color:var(--muted);font-size:13px}"
    ".cta{margin:24px 0}.btn{display:inline-block;font-weight:700;text-decoration:underline;color:var(--link)}"
    "footer{padding:24px 0;color:var(--muted);font-size:13px;border-top:1px solid var(--line)}")


def share_shell(title: str, desc: str, body: str, safe: bool = False) -> str:
    """A plain HTML page for the app's shared views — matches the stripped-down app: no brand chrome,
    no lead-magnet link. `safe=True` = a CLIENT-FACING page (a plan a user sends a customer or
    investor): the header reads FILG, not the long-form name — the standing profanity gate for
    shared plans (branding note, quick_restart)."""
    t, d = html.escape(title), html.escape((desc or "")[:180])
    logo = "FILG" if safe else "fuck it, let's go"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{t}, FILG</title><meta name="description" content="{d}">'
            f'<meta property="og:title" content="{t}"><meta property="og:description" content="{d}">'
            f'<meta property="og:type" content="article">'
            f'<style>{SHARE_CSS}</style></head><body><div class="wrap">'
            f'<nav><a class="logo" href="/">{html.escape(logo)}</a></nav>'
            f'{body}'
            f"<footer>Built with FILG. Every number above is graded by a source-credibility gate. "
            f'<a href="/">filg.ai</a></footer>'
            f'</div></body></html>')


def shared_plan_page(title: str, inner_html: str, receipts: list | None = None,
                     path: list | None = None) -> str:
    """The /p public plan share: the plan markdown + the two proof surfaces nothing else in-market
    shows — the graded receipts and the decision path — wrapped in the plain share shell with the
    maker line. (Client-facing page: brand copy stays profanity-free, filg.ai not the long domain.)"""
    ev = ""
    rows = [r for r in (receipts or []) if r.get("text")]
    if rows:
        lis = "".join(
            f'<li><span class="{"ok" if r.get("mark") == "ok" else "warn"}">'
            f'{"✅" if r.get("mark") == "ok" else "⚠️"}</span>'
            f'<span>{html.escape(r["text"])}'
            + (f' <a href="{html.escape(r["url"])}" rel="nofollow noopener">src</a>' if r.get("url") else "")
            + f'<span class="badge {"ok" if r.get("mark") == "ok" else "warn"}">'
              f'{"cited" if r.get("mark") == "ok" else "vendor"}</span></span></li>'
            for r in rows[:12])
        ok = sum(1 for r in rows if r.get("mark") == "ok")
        ev = (f'<h2>The receipts</h2><p class="tag">Every market claim graded by a source-credibility '
              f'gate — {ok} cited, {len(rows) - ok} flagged as vendor marketing. Labeled, not laundered.</p>'
              f'<ul class="ev">{lis}</ul>')
    trail = ""
    steps = [p for p in (path or []) if p.get("label")]
    if len(steps) > 1:
        lis = "".join(
            f'<li><b>{html.escape(p["label"])}</b>'
            + (f' <span class="note">↳ {html.escape(p["note"][:140])}</span>' if p.get("note") else "")
            + "</li>" for p in steps)
        trail = (f'<h2>How it got here</h2><p class="tag">The decision path — {len(steps)} steps from '
                 f'raw idea to this plan, pivots included.</p><ol class="trail">{lis}</ol>')
    article = (f'<article><span class="eyebrow">Shared business plan · built with FILG</span>'
               f'<h1>{html.escape(title)}</h1>{ev}{trail}<h2>The plan</h2>{inner_html}{CTA}'
               f'<footer>Built with <a href="https://filg.ai">FILG</a> — an idea in, a graded plan out. '
               f'Free to try, receipts included.</footer></article>')
    return share_shell("Shared plan — FILG", title, article, safe=True)


def not_found(message: str) -> str:
    """A bare, styled 404 body for the share routes (result/plan not ready, private, or missing)."""
    return (f"<p style='font-family:sans-serif;max-width:520px;margin:60px auto;padding:0 22px'>"
            f"{html.escape(message)}</p>")


# ─── Graded evidence rows (moved from the retired engine/teardown.py) ─────────
def _host(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") or url


def evidence_li(rows) -> str:
    """The graded rows as share-page <li> items — the ✅/⚠️ receipts with source + note."""
    out = []
    for r in rows:
        ok = r["mark"] == "ok"
        out.append(
            f'<li><span class="ico {"ok" if ok else "warn"}">{"✅" if ok else "⚠️"}</span>'
            f'<span>{html.escape(r["text"])} '
            f'<span class="badge {"ok" if ok else "warn"}">{"cited" if ok else "vendor, unverified"}</span>'
            f'<br><span class="note"><a href="{html.escape(r["url"])}">{html.escape(_host(r["url"]))}</a>, '
            f'{html.escape(r["note"])}</span></span></li>')
    return "\n".join(out)
