#!/usr/bin/env python3
"""
Weekly "Cited Offer Teardown" generator, the lead-magnet engine (FILG).

Runs the same stage chain as `pipeline.py` in **label-don't-chase** mode (re-source only the
top 2–3 headline stats; label the rest), then emits a publishable issue in three forms:
  ../teardowns/issue_NN_<slug>.md           , markdown source / archive
  ../teardowns/issue_NN_<slug>.rows.json    , structured rows (for rebuilds)
  ../landing/teardowns/issue-NN-<slug>.html , deploy-ready BRANDED page (filg.ai/teardowns/...)
and rebuilds ../landing/teardowns/index.html (the archive) from a manifest.

The labeling IS the marketing: showing the source-credibility gate flag a vendor stat live is the
differentiator. Cost ~$0.40–0.50/issue.

Run:  python3 teardown.py "plain-text business idea"   [--headlines 3]
      python3 teardown.py --rebuild        # re-render all pages + index from saved rows (no API)
Needs ANTHROPIC_API_KEY (except --rebuild).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlparse

HERE = os.path.dirname(__file__)
MD_DIR = os.path.join(HERE, "..", "teardowns")
SITE_DIR = os.path.join(HERE, "..", "landing", "teardowns")
MANIFEST = os.path.join(SITE_DIR, "_manifest.json")

HEADLINES_TO_RESEARCH = 3   # label-don't-chase: re-source only the top N flagged claims


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


# ─── Evidence assembly (deterministic, the gate's labels are authoritative) ──
def build_evidence(idea: str, headlines: int):
    from pipeline import plan, research_lane, gate_claims, research_primary  # lazy: --rebuild needs no API
    lanes = plan(idea)
    with ThreadPoolExecutor(max_workers=3) as ex:
        lane_claims = list(ex.map(lambda ln: research_lane(idea, ln), lanes))
    # remember which lane each claim came from, so the UI can show who researched what (persona-owned
    # lanes are assigned app-side; this just carries the provenance through the gate).
    claim_lane = {id(c): lanes[li] for li, lane in enumerate(lane_claims) for c in lane}
    quant = [c for lane in lane_claims for c in lane if c.quantitative]

    verdicts = gate_claims(quant)  # one batched judge call for all claims (token win)
    cleared = [v for v in verdicts if not v.flagged]
    flagged = [v for v in verdicts if v.flagged]

    to_chase, to_label = flagged[:headlines], flagged[headlines:]
    rescues = []
    if to_chase:
        with ThreadPoolExecutor(max_workers=3) as ex:
            rescues = list(ex.map(lambda v: research_primary(v.claim), to_chase))
        for v, r in zip(to_chase, rescues):
            r.original = v

    rows = []
    # tier + judge are the gate's per-claim reasoning — carried through so the UI can show HOW the
    # moat graded each number (surfaced in 'see how it works' mode), not just the ok/warn outcome.
    for v in cleared:
        rows.append({"mark": "ok", "text": v.claim.text, "url": v.claim.source_url,
                     "note": f"{v.tier.lower()} source, passed the gate",
                     "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of,
                     "lane": claim_lane.get(id(v.claim), "")})
    for r in rescues:
        if r.rescued and r.new_url:
            rows.append({"mark": "ok", "text": r.original.claim.text, "url": r.new_url,
                         "note": "re-sourced to a primary/neutral cite by the gate",
                         "tier": r.original.tier, "judge": r.original.judge,
                         "as_of": r.original.claim.as_of,
                         "lane": claim_lane.get(id(r.original.claim), "")})
        else:
            v = r.original
            rows.append({"mark": "warn", "text": v.claim.text, "url": v.claim.source_url,
                         "note": "no neutral source found, treat as a vendor marketing claim",
                         "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of,
                         "lane": claim_lane.get(id(v.claim), "")})
    for v in to_label:
        rows.append({"mark": "warn", "text": v.claim.text, "url": v.claim.source_url,
                     "note": "flagged self-interested/vendor source, unverified",
                     "tier": v.tier, "judge": v.judge, "as_of": v.claim.as_of,
                     "lane": claim_lane.get(id(v.claim), "")})

    _label_triangulation(rows)   # cheap surface-only: mark cleared claims single-source vs corroborated
    n_clean = sum(1 for r in rows if r["mark"] == "ok")
    stats = {"checked": len(rows), "cleared": n_clean, "flagged": len(rows) - n_clean}
    return rows, stats, lanes


def _row_host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return (m.group(1).replace("www.", "") if m else (url or "")).strip().lower()


def _label_triangulation(rows: list) -> None:
    """No extra research calls (label-don't-chase, invariant #2). A cleared claim is 'corroborated'
    only if another cleared claim in the SAME lane cites a DIFFERENT host; otherwise it rests on a
    single source. We just label it — we never go re-search to force a second cite."""
    by_lane: dict = {}
    for r in rows:
        if r["mark"] == "ok":
            by_lane.setdefault(r.get("lane", ""), []).append(_row_host(r["url"]))
    for r in rows:
        if r["mark"] != "ok":
            continue
        hosts = by_lane.get(r.get("lane", ""), [])
        r["sources"] = len(set(hosts))
        r["corroborated"] = len({h for h in hosts if h and h != _row_host(r["url"])}) >= 1


def write_prose(idea: str, rows) -> dict:
    from pipeline import call, extract_json, SONNET
    cleared_block = "\n".join(f"- {r['text']}" for r in rows if r["mark"] == "ok") or "- (none cleared)"
    out = call("teardown_synth", SONNET, max_tokens=700, prompt=(
        "You write a short, punchy 'Cited Offer Teardown' for an operator audience. From the "
        "plain-text idea and the gate-CLEARED evidence below, output strictly JSON:\n"
        '{"title": "<=8-word hook", "idea_line": "one sentence restating the idea", '
        '"offer": "2-3 sentences: the specific productized thing they would SELL", '
        '"gtm": "one sentence: the sharpest first go-to-market move"}\n\n'
        "Be concrete and specific. Do NOT invent statistics, only the evidence section carries numbers.\n\n"
        f"IDEA:\n{idea}\n\nGATE-CLEARED EVIDENCE (context only):\n{cleared_block}"
    ))
    data = extract_json(out)
    data = data if isinstance(data, dict) else {}
    return {"title": data.get("title") or "Cited Offer Teardown",
            "idea_line": data.get("idea_line") or idea[:160],
            "offer": data.get("offer") or "(offer synthesis unavailable)",
            "gtm": data.get("gtm") or "(gtm unavailable)"}


# ─── Public API (used by the CLI and the MVP app) ────────────────────────────
MOCK_RESULT = {
    "prose": {
        "title": "AI Front Desk for Home-Service Pros",
        "idea_line": "A done-for-you AI receptionist that answers every call and texts back every "
                     "missed lead so contractors stop losing jobs to whoever answers first.",
        "offer": "A fully installed AI front desk, answers 24/7, texts back missed calls in seconds, "
                 "books jobs to the calendar, live in 5 days, flat monthly retainer, no per-lead fees.",
        "gtm": "Cold-call 50 owner-operators in one trade + metro; demo by calling their own after-hours "
               "line, letting it ring out, then showing the AI handle the same call.",
    },
    "rows": [
        {"mark": "ok", "text": "~2.5M home-service businesses operate in the US", "url":
            "https://www.census.gov/", "note": "primary source, passed the gate",
            "tier": "PRIMARY", "judge": "TRUST", "as_of": 2022,
            "sources": 1, "corroborated": False,
            "lane": "What is the market size and number of target buyers?"},
        {"mark": "warn", "text": "62% of calls to small businesses go unanswered", "url":
            "https://www.getaira.io/blog/missed-business-calls-statistics", "note":
            "flagged self-interested/vendor source, unverified",
            "tier": "VENDOR", "judge": "FLAG_SELF_INTERESTED", "as_of": None,
            "lane": "What is the buyer's most acute, expensive pain point?"},
    ],
    "stats": {"checked": 2, "cleared": 1, "flagged": 1},
    "lanes": ["What is the market size and number of target buyers?",
              "Who are the competitors and what are the pricing norms?",
              "What is the buyer's most acute, expensive pain point?"],
    "cost": 0.0,
}


def generate(idea: str, headlines: int = HEADLINES_TO_RESEARCH, mock: bool = False) -> dict:
    """Run one teardown and return {prose, rows, stats, cost}. `mock=True` returns canned data with
    no API calls, for local/frontend dev and for testing the metering without spend."""
    if mock:
        return {**MOCK_RESULT, "prose": dict(MOCK_RESULT["prose"])}
    from pipeline import LEDGER
    start = len(LEDGER.rows)
    rows, stats, lanes = build_evidence(idea, headlines)
    prose = write_prose(idea, rows)
    return {"prose": prose, "rows": rows, "stats": stats, "lanes": lanes,
            "cost": round(LEDGER.cost_slice(start), 4)}


MOCK_FULL = {
    "artifacts_md": (
        "# AI Front Desk for Home-Service Pros\n\n## 1. Brief\nOwner-operators lose jobs to whoever "
        "answers first; ~2.5M US home-service businesses ([census.gov](https://www.census.gov/)).\n\n"
        "## 2. Offer\nDone-for-you AI receptionist + missed-call text-back, live in 5 days, flat "
        "retainer.\n\n## 3. Pricing\n$1,500 setup + $500/mo. (Leak figures from vendor blogs are "
        "*(unverified vendor claim)*, model per client.)\n\n## 4. Go-to-market\nCold-call one trade "
        "+ metro; after-hours-call demo.\n\n## 5. Delivery playbook\nDiscovery → build → test → go "
        "live → monthly 'jobs recovered' report.\n\n## 6. 30-day roadmap\nWk1 reference build · Wk2 "
        "list 50 + outreach · Wk3 demos + pilots · Wk4 convert + referral."),
    "rows": MOCK_RESULT["rows"],
    "stats": MOCK_RESULT["stats"],
    "cost": 0.0,
}


def generate_full(idea: str, headlines: int = HEADLINES_TO_RESEARCH, mock: bool = False) -> dict:
    """Paid-tier output: the complete artifact set (brief→offer→pricing→GTM→delivery→roadmap),
    synthesized ONLY over gate-graded research (label-don't-chase). Returns
    {artifacts_md, rows, stats, cost}."""
    if mock:
        return {**MOCK_FULL}
    from pipeline import LEDGER, call, SONNET
    start = len(LEDGER.rows)
    rows, stats, _lanes = build_evidence(idea, headlines)
    cited = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "ok") or "- (none)"
    flagged = "\n".join(f"- {r['text']} [{r['url']}]" for r in rows if r["mark"] == "warn") or "- (none)"
    artifacts = call("synth_full", SONNET, max_tokens=3500, prompt=(
        "You are the synthesis stage of FILG. Produce a sellable artifact set in markdown with these "
        "sections: (1) Structured brief, (2) Offer definition, (3) Packaging + pricing, "
        "(4) Go-to-market, (5) Delivery playbook, (6) 30-day roadmap. Be concrete and specific.\n\n"
        "Use the CITED research freely (cite it inline with its URL). You MAY reference a FLAGGED "
        "claim only if you append '(unverified vendor claim)' right after it. Never present a flagged "
        "number as established fact.\n\n"
        f"IDEA:\n{idea}\n\nCITED RESEARCH:\n{cited}\n\nFLAGGED (vendor) CLAIMS:\n{flagged}"))
    return {"artifacts_md": artifacts, "rows": rows, "stats": stats,
            "cost": round(LEDGER.cost_slice(start), 4)}


# ─── Render ──────────────────────────────────────────────────────────────────
def evidence_li(rows) -> str:
    out = []
    for r in rows:
        ok = r["mark"] == "ok"
        out.append(
            f'<li><span class="ico {"ok" if ok else "warn"}">{"✅" if ok else "⚠️"}</span>'
            f'<span>{html.escape(r["text"])} '
            f'<span class="badge {"ok" if ok else "warn"}">{"cited" if ok else "vendor, unverified"}</span>'
            f'<br><span class="note"><a href="{html.escape(r["url"])}">{html.escape(host(r["url"]))}</a>, '
            f'{html.escape(r["note"])}</span></span></li>')
    return "\n".join(out)


def render_page(n: int, prose: dict, rows, stats, cost: float) -> str:
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
    with open(os.path.join(SITE_DIR, "index.html"), "w") as f:
        f.write(page_shell("The Cited Offer Teardown", "Weekly graded offer teardowns from FILG.", body))


def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST) as f:
            return json.load(f)
    return []


def main() -> int:
    os.makedirs(MD_DIR, exist_ok=True)
    os.makedirs(SITE_DIR, exist_ok=True)

    if "--rebuild" in sys.argv:           # re-render pages + index from saved rows, no API
        manifest = load_manifest()
        for e in manifest:
            rows_path = os.path.join(MD_DIR, e["rows"])
            if os.path.exists(rows_path):
                with open(rows_path) as f:
                    saved = json.load(f)
                page = render_page(e["n"], saved["prose"], saved["rows"], saved["stats"], saved["cost"])
                with open(os.path.join(SITE_DIR, e["file"]), "w") as f:
                    f.write(page)
        build_index(manifest)
        print(f"  rebuilt {len(manifest)} page(s) + index")
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    headlines = HEADLINES_TO_RESEARCH
    if "--headlines" in sys.argv:
        headlines = int(sys.argv[sys.argv.index("--headlines") + 1])
    from pipeline import LEDGER, DEFAULT_IDEA
    idea = args[0] if args else DEFAULT_IDEA

    print(f"\nGenerating teardown (label-don't-chase, re-source top {headlines})…")
    res = generate(idea, headlines, mock="--mock" in sys.argv)
    prose, rows, stats, cost = res["prose"], res["rows"], res["stats"], round(res["cost"], 2)
    print(f"  {stats['checked']} claims → {stats['cleared']} cleared / {stats['flagged']} labeled")

    manifest = load_manifest()
    n = (max((e["n"] for e in manifest), default=0)) + 1
    slug = slugify(prose["title"])
    md_name = f"issue_{n:02d}_{slug.replace('-', '_')}"
    site_name = f"issue-{n:02d}-{slug}.html"

    with open(os.path.join(MD_DIR, md_name + ".md"), "w") as f:
        f.write(render_md(prose, rows, stats, cost))
    with open(os.path.join(MD_DIR, md_name + ".rows.json"), "w") as f:
        json.dump({"prose": prose, "rows": rows, "stats": stats, "cost": cost}, f, indent=2)
    with open(os.path.join(SITE_DIR, site_name), "w") as f:
        f.write(render_page(n, prose, rows, stats, cost))

    manifest.append({"n": n, "title": prose["title"], "slug": slug, "file": site_name,
                     "rows": md_name + ".rows.json", "idea_line": prose["idea_line"],
                     "date": date.today().isoformat(), **stats})
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    build_index(manifest)

    print(f"  cost: ${cost:.2f}")
    print(f"  → teardowns/{md_name}.md  +  landing/teardowns/{site_name}  +  index rebuilt\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
