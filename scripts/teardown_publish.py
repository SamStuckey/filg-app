#!/usr/bin/env python3
"""
Weekly "Cited Offer Teardown" publisher — the lead-magnet site generator.

Runs one teardown through the app's research layer (app/teardown.py, label-don't-chase mode)
and emits a publishable issue in three forms:
  teardowns/issue_NN_<slug>.md            markdown source / archive
  teardowns/issue_NN_<slug>.rows.json     structured rows (for rebuilds)
  landing/teardowns/issue-NN-<slug>.html  deploy-ready BRANDED page (filg.ai/teardowns/...)
and rebuilds landing/teardowns/index.html (the archive) from a manifest.

The labeling IS the marketing: showing the source-credibility gate flag a vendor stat live is
the differentiator. Cost ~$0.40-0.50/issue.

Run:  python3 scripts/teardown_publish.py "plain-text business idea"  [--headlines 3] [--mock]
      python3 scripts/teardown_publish.py --rebuild   # re-render pages + index from saved rows (no API)
Needs ANTHROPIC_API_KEY (except --rebuild).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> `app` + `engine`

ROOT = Path(__file__).resolve().parent.parent
MD_DIR = ROOT / "teardowns"
SITE_DIR = ROOT / "landing" / "teardowns"
MANIFEST = SITE_DIR / "_manifest.json"

DEFAULT_HEADLINES = 3   # label-don't-chase: re-source only the top N flagged claims


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "idea"


def host(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.") or url


# --- Shared brand CSS (kept in sync with landing/index.html tokens) ----------
BRAND_CSS = """
:root{--paper:#FBFAF8;--ink:#14110E;--muted:#6B655C;--line:#E7E2D8;--accent:#0F766E;
--accent-deep:#0B5751;--warn:#B45309;--warn-bg:#FBF3E6;--ok-bg:#EAF4F2}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);
font:17px/1.65 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:760px;margin:0 auto;padding:0 22px}
h1,h2,.logo{font-family:"Fraunces",Georgia,serif;font-weight:600;line-height:1.14;letter-spacing:-.01em}
a{color:var(--accent)}
nav{display:flex;justify-content:space-between;align-items:center;padding:22px 0;border-bottom:1px solid var(--line)}
.logo{font-size:21px;letter-spacing:-.02em;text-decoration:none;color:var(--ink)}
.logo span{color:var(--accent)}
.btn{display:inline-block;font-weight:600;padding:11px 18px;border-radius:9px;text-decoration:none;font-size:15px}
.btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:var(--accent-deep)}
.btn-ghost{border:1.5px solid var(--line);color:var(--ink)}
article{padding:34px 0}
.eyebrow{display:inline-block;font-size:12px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;
color:var(--accent);background:var(--ok-bg);padding:5px 11px;border-radius:999px;margin-bottom:16px}
h1{font-size:clamp(28px,5vw,42px);margin:0 0 6px}
.tag{color:var(--muted);font-size:15px;margin:0 0 24px}
h2{font-size:22px;margin:30px 0 8px}
.ev{list-style:none;padding:0;margin:14px 0 0}
.ev li{padding:12px 0;border-top:1px dashed var(--line);display:flex;gap:11px;font-size:15.5px}
.ev li:first-child{border-top:0}
.ev .ico{flex:0 0 auto;margin-top:1px}.ev .ok{color:var(--accent)}.ev .warn{color:var(--warn)}
.ev .note{color:var(--muted);font-size:14px}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:6px;margin-left:2px}
.badge.ok{background:var(--ok-bg);color:var(--accent)}.badge.warn{background:var(--warn-bg);color:var(--warn)}
.recpt{margin-top:22px;padding:16px 18px;background:var(--ok-bg);border-radius:11px;font-size:15px}
.cta{margin:28px 0}
footer{padding:30px 0;color:var(--muted);font-size:14px;border-top:1px solid var(--line)}
/* archive index */
.issue{display:block;padding:20px 0;border-top:1px solid var(--line);text-decoration:none;color:inherit}
.issue:hover h2{color:var(--accent)}
.issue h2{font-size:21px;margin:0 0 4px}.issue p{margin:6px 0 0;color:var(--muted);font-size:15px}
.issue .meta{font-size:13px;color:var(--accent);font-weight:600}
""".strip()

FAVICON = ('data:image/svg+xml,'
           '%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"%3E'
           '%3Crect width="32" height="32" rx="7" fill="%230F766E"/%3E'
           '%3Ctext x="16" y="22" font-family="Georgia,serif" font-size="16" font-weight="700" '
           'fill="white" text-anchor="middle"%3EF%3C/text%3E%3C/svg%3E')

HEAD = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&display=swap" rel="stylesheet">'
        f'<link rel="icon" href="{FAVICON}">')


def page_shell(title: str, desc: str, body: str) -> str:
    t = html.escape(title)
    d = html.escape(desc[:180])
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{t}, FILG</title><meta name="description" content="{d}">'
            f'<meta property="og:title" content="{t}"><meta property="og:description" content="{d}">'
            f'<meta property="og:type" content="article"><meta name="twitter:card" content="summary_large_image">'
            f'{HEAD}<style>{BRAND_CSS}</style></head><body><div class="wrap">'
            f'<nav><a class="logo" href="/">FI<span>LG</span></a>'
            f'<a class="btn btn-ghost" href="/#magnet">Get these weekly</a></nav>'
            f'{body}'
            f'<footer>FILG, fuck it, let\'s go. Every number above is graded by a source-credibility '
            f'gate. <a href="/teardowns/">More teardowns</a> · <a href="/">filg.ai</a></footer>'
            f'</div></body></html>')


def render_page(n: int, prose: dict, rows, stats, cost: float) -> str:
    from app.render import evidence_li  # the same graded-row renderer the app's share pages use
    body = (
        f'<article>'
        f'<span class="eyebrow">Cited Offer Teardown · Issue {n:02d}</span>'
        f'<h1>{html.escape(prose["title"])}</h1>'
        f'<p class="tag">Generated unattended for ~${cost:.2f}. Every number graded, '
        f'vendor stats labeled, not laundered.</p>'
        f'<p><strong>The idea:</strong> {html.escape(prose["idea_line"])}</p>'
        f'<h2>The offer</h2><p>{html.escape(prose["offer"])}</p>'
        f'<h2>How you\'d sell it</h2><p>{html.escape(prose["gtm"])}</p>'
        f'<h2>The evidence, graded</h2><ul class="ev">{evidence_li(rows)}</ul>'
        f'<div class="recpt"><strong>The credibility receipt:</strong> {stats["checked"]} quantitative '
        f'claims checked · <strong>{stats["cleared"]} cleared to a primary/neutral source</strong> · '
        f'{stats["flagged"]} flagged as vendor marketing and labeled. A naive tool prints all '
        f'{stats["checked"]} as fact.</div>'
        f'<div class="cta"><a class="btn btn-primary" href="/#start">Do this for my idea →</a></div>'
        f'</article>')
    return page_shell(prose["title"], prose["idea_line"], body)


def render_md(prose, rows, stats, cost) -> str:
    L = [f"# Cited Offer Teardown, {prose['title']}", "",
         "*Generated by FILG. Every number is graded by a source-credibility gate, vendor-marketing "
         "stats are **labeled, not laundered**.*", "",
         f"**The idea:** {prose['idea_line']}", "",
         "## The offer (what you'd sell)", prose["offer"], "",
         "## How you'd sell it", prose["gtm"], "", "## The evidence, graded", ""]
    for r in rows:
        icon = "✅" if r["mark"] == "ok" else "⚠️"
        L.append(f"- {icon} {r['text']}, [{host(r['url'])}]({r['url']}) *( {r['note']} )*")
    L += ["", "## The credibility receipt",
          f"- **{stats['checked']}** checked · **{stats['cleared']}** cleared to a primary/neutral "
          f"source · **{stats['flagged']}** flagged as vendor marketing and labeled.",
          f"- Produced unattended for **~${cost:.2f}**.", "",
          "---", "*Want this for **your** idea? Founding members get in first → "
          "[filg.ai](https://filg.ai/#start).*"]
    return "\n".join(L) + "\n"


def build_index(manifest) -> None:
    items = sorted(manifest, key=lambda e: e["n"], reverse=True)
    cards = "\n".join(
        f'<a class="issue" href="./{html.escape(e["file"])}">'
        f'<div class="meta">Issue {e["n"]:02d} · {e.get("date","")} · '
        f'{e["cleared"]}/{e["checked"]} cited, {e["flagged"]} flagged</div>'
        f'<h2>{html.escape(e["title"])}</h2><p>{html.escape(e.get("idea_line",""))}</p></a>'
        for e in items) or "<p>First issue coming soon.</p>"
    body = (f'<article><span class="eyebrow">The Cited Offer Teardown</span>'
            f'<h1>Every week: one idea, run through the engine.</h1>'
            f'<p class="tag">The offer, the go-to-market, and the research with the vendor spin called '
            f'out, every number graded, nothing laundered.</p>'
            f'<div class="cta"><a class="btn btn-primary" href="/#magnet">Get it weekly →</a></div>'
            f'{cards}</article>')
    (SITE_DIR / "index.html").write_text(
        page_shell("The Cited Offer Teardown", "Weekly graded offer teardowns from FILG.", body))


def load_manifest():
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    return []


def main() -> int:
    os.makedirs(MD_DIR, exist_ok=True)
    os.makedirs(SITE_DIR, exist_ok=True)

    if "--rebuild" in sys.argv:           # re-render pages + index from saved rows, no API
        manifest = load_manifest()
        for e in manifest:
            rows_path = MD_DIR / e["rows"]
            if rows_path.exists():
                saved = json.loads(rows_path.read_text())
                page = render_page(e["n"], saved["prose"], saved["rows"], saved["stats"], saved["cost"])
                (SITE_DIR / e["file"]).write_text(page)
        build_index(manifest)
        print(f"  rebuilt {len(manifest)} page(s) + index")
        return 0

    from app import teardown
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headlines = DEFAULT_HEADLINES
    if "--headlines" in sys.argv:
        headlines = int(sys.argv[sys.argv.index("--headlines") + 1])
    if not args:
        print("usage: teardown_publish.py \"plain-text business idea\" [--headlines N] [--mock]")
        return 2
    idea = args[0]

    print(f"\nGenerating teardown (label-don't-chase, re-source top {headlines})…")
    res = teardown.generate(idea, headlines, mock="--mock" in sys.argv)
    prose, rows, stats, cost = res["prose"], res["rows"], res["stats"], round(res["cost"], 2)
    print(f"  {stats['checked']} claims → {stats['cleared']} cleared / {stats['flagged']} labeled")

    manifest = load_manifest()
    n = (max((e["n"] for e in manifest), default=0)) + 1
    slug = slugify(prose["title"])
    md_name = f"issue_{n:02d}_{slug.replace('-', '_')}"
    site_name = f"issue-{n:02d}-{slug}.html"

    (MD_DIR / (md_name + ".md")).write_text(render_md(prose, rows, stats, cost))
    (MD_DIR / (md_name + ".rows.json")).write_text(
        json.dumps({"prose": prose, "rows": rows, "stats": stats, "cost": cost}, indent=2))
    (SITE_DIR / site_name).write_text(render_page(n, prose, rows, stats, cost))

    manifest.append({"n": n, "title": prose["title"], "slug": slug, "file": site_name,
                     "rows": md_name + ".rows.json", "idea_line": prose["idea_line"],
                     "date": date.today().isoformat(), **stats})
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    build_index(manifest)

    print(f"  cost: ${cost:.2f}")
    print(f"  → teardowns/{md_name}.md  +  landing/teardowns/{site_name}  +  index rebuilt\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
