#!/usr/bin/env python3
"""
plan_pdf.py — turn a finished plan session into a styled, branded PDF (the core paid artifact).

Two stages, kept separate so we can re-platform the renderer later without touching content:
  1. synthesize(session)  — reshape the builder's output into a plan structure: an AI-written
     executive summary (Sonnet), the section bodies (the active branch's finalized files), and the
     graded-research appendix (the moat, shown as an evidence exhibit). No invented numbers.
  2. render(plan)         — lay it out as a PDF with fpdf2 (pure-Python, zero system deps → runs on
     Render's native Python runtime as-is). Brand-but-serious: Fraunces display + Inter body, coral
     as a 10% accent, generous margins, cover + contents + running footer + page numbers.

Design numbers come from `filg-docs/pdf_plan_spec.md` (the deep-research spec): 11pt body, ~1.4
leading, ~28mm margins (~66–70 cpl), max 2 families, 60-30-10 colour, graded-evidence appendix.

This is v1 = the DEFAULT type (operator plan, investor-presentable). Other types (investor/Sequoia,
lender/SBA, one-page) and style presets get refactored on top once the content is dialed in.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

import planner  # section list + working idea
import skill_registry as skills  # the standing VOICE rule (no AI tells)

_FONTS = Path(__file__).resolve().parent / "assets" / "fonts"

# Brand palette (matches the app tokens). 60-30-10: paper/ink base, coral as the 10% accent.
INK = (27, 23, 38)
CORAL = (255, 107, 74)
MUTED = (110, 104, 120)
LINE = (224, 224, 224)
PAPER = (255, 253, 247)
OK = (30, 158, 90)
WARN = (201, 116, 11)
OK_BG = (231, 245, 236)
WARN_BG = (255, 243, 224)


# ── Stage 1: synthesize ──────────────────────────────────────────────────────
def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "")
    return (m.group(1).replace("www.", "") if m else (url or "")).strip()


def _title(thesis: str) -> str:
    t = (thesis or "Your business").strip().rstrip(".")
    return (t[:1].upper() + t[1:])[:90]


def _exec_summary(thesis: str, files: dict, vetting: dict, mock: bool = False) -> tuple[str, float]:
    """A one-page executive summary — written LAST, per every authority (SBA/HBR). Real mode asks
    Sonnet to synthesize one from the plan; mock returns a deterministic stand-in (no spend)."""
    if mock:
        return (f"**{_title(thesis)}.** A focused, sellable offer — built one decision at a time, "
                f"with the market research graded so vendor spin is labeled, not laundered.\n\n"
                f"This plan lays out who it's for and why now, what you sell, what you charge, how "
                f"you win customers, how you deliver repeatably, and your first 30 days. The biggest "
                f"risk and the cheapest first test are named up front, and every market claim is "
                f"sourced and graded in the evidence appendix."), 0.0
    from pipeline import LEDGER, call, SONNET  # heavy; real mode only
    start = len(LEDGER.rows)
    plan = "\n\n".join(f"## {s['title']}\n{files[s['file']]}" for s in planner.SECTIONS
                       if files.get(s["file"]))
    risk = (vetting or {}).get("biggest_risk") or ""
    test = (vetting or {}).get("first_test") or ""
    system = ("You write the executive summary of a business plan for a solo operator. One page, "
              "~220 words. Lead with the offer in one line, then cover the opportunity, how it makes "
              "money, the main risk, and the cheapest first test. Never invent numbers or cite a "
              "statistic as fact (the plan's research is graded elsewhere). Return markdown (a lead "
              "line in **bold**, then prose).\n\n" + skills.VOICE)
    body = call("plan_exec_summary", SONNET, max_tokens=600, system=system, cache=True, prompt=(
        f"BUSINESS: {thesis}\n\nPLAN:\n{plan}\n\nBIGGEST RISK: {risk}\nCHEAPEST FIRST TEST: {test}"))
    return body.strip(), round(LEDGER.cost_slice(start), 4)


def synthesize(session: dict, mock: bool = False) -> tuple[dict, float]:
    """Reshape a finished session into the plan structure the renderer consumes. Uses the ACTIVE
    branch's files (session['files'] is mirrored from the active node), so the PDF is the final
    decision set. Returns (plan, cost)."""
    thesis = planner._working_idea(session)
    files = session.get("files") or {}
    research = session.get("research") or {}
    vetting = session.get("vetting") or {}
    shaped = session.get("shaped") or {}
    rows = research.get("rows") or []

    sections = [{"n": i + 1, "title": s["title"], "sub": s.get("sub", ""), "body": files[s["file"]]}
                for i, s in enumerate(planner.SECTIONS) if files.get(s["file"])]
    evidence = [{"mark": r.get("mark"), "text": r.get("text", ""), "source": _host(r.get("url", "")),
                 "note": r.get("note", "")} for r in rows]
    exec_summary, cost = _exec_summary(thesis, files, vetting, mock=mock)
    plan = {
        "title": _title(thesis),
        "subtitle": (research.get("prose") or {}).get("offer") or shaped.get("thesis") or "",
        "date": datetime.now(timezone.utc).strftime("%B %Y"),
        "verdict": (vetting or {}).get("verdict"),
        "edge": shaped.get("founder_edge"),
        "first_test": (vetting or {}).get("first_test"),
        "exec_summary": exec_summary,
        "sections": sections,
        "evidence": evidence,
        "cited": sum(1 for e in evidence if e["mark"] == "ok"),
        "flagged": sum(1 for e in evidence if e["mark"] == "warn"),
    }
    return plan, cost


# ── Stage 2: render (fpdf2) ──────────────────────────────────────────────────
class _PDF(FPDF):
    plan_title = ""

    def header(self):
        if self.page_no() <= 2:   # no running header on cover (1) / contents (2)
            return
        self.set_y(10)
        self.set_font("Inter", "", 7.5)
        self.set_text_color(*MUTED)
        title = self.plan_title.upper()
        maxw = (self.w - self.l_margin - self.r_margin) - 34  # leave room for "BUILT WITH FILG"
        if self.get_string_width(title) > maxw:
            while title and self.get_string_width(title + "…") > maxw:
                title = title[:-1]
            title = title.rstrip() + "…"
        self.cell(maxw, 5, title, align="L")
        self.cell(0, 5, "BUILT WITH FILG", align="R")
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, 17, self.w - self.r_margin, 17)
        self.set_y(self.t_margin)

    def footer(self):
        if self.page_no() <= 2:
            return
        self.set_y(-13)
        self.set_font("Inter", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, f"{self.page_no() - 2}", align="C")


def _fonts(pdf: FPDF) -> None:
    pdf.add_font("Inter", "", str(_FONTS / "Inter-Regular.ttf"))
    pdf.add_font("Inter", "B", str(_FONTS / "Inter-SemiBold.ttf"))
    # No italic file vendored — map I/BI onto what we have so write_html never trips on <em>.
    pdf.add_font("Inter", "I", str(_FONTS / "Inter-Regular.ttf"))
    pdf.add_font("Inter", "BI", str(_FONTS / "Inter-SemiBold.ttf"))
    pdf.add_font("Fraunces", "", str(_FONTS / "Fraunces-Display.ttf"))


def _md_to_html(md: str) -> str:
    import markdown  # already a dependency
    # Demote body headings to bold run-in lines: keeps them visually distinct but stops fpdf2's
    # write_html from auto-registering every body sub-heading as a table-of-contents/outline entry.
    md = re.sub(r"(?m)^#{1,6}\s+(.*)$", r"**\1**", md or "")
    html = markdown.markdown(md, extensions=["tables", "sane_lists"])
    # fpdf2's write_html inherits multi_cell's JUSTIFY default, which strands ugly word gaps. Force
    # left alignment on the block tags.
    return (html.replace("<p>", '<p align="left">').replace("<li>", '<li align="left">')
                .replace("<td>", '<td align="left">'))


def _body_html(pdf: _PDF, md: str) -> None:
    pdf.set_font("Inter", "", 11)
    pdf.set_text_color(*INK)
    try:
        pdf.write_html(_md_to_html(md))
    except Exception:  # noqa: BLE001 — never let one section's markup kill the whole PDF
        pdf.set_font("Inter", "", 11)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 6.2, re.sub(r"[*#`>|_-]", "", md or "", align="L").strip())


def _cover(pdf: _PDF, plan: dict) -> None:
    pdf.add_page()
    pdf.set_fill_color(*PAPER)
    pdf.rect(0, 0, pdf.w, pdf.h, "F")
    pdf.set_fill_color(*CORAL)            # accent band, top-left
    pdf.rect(0, 0, 52, 7, "F")
    pdf.set_xy(pdf.l_margin, 64)
    pdf.set_font("Inter", "B", 11)
    pdf.set_text_color(*CORAL)
    pdf.cell(0, 7, "B U S I N E S S   P L A N")
    pdf.ln(16)
    pdf.set_x(pdf.l_margin)
    n = len(plan["title"])                       # adaptive size so long titles don't overflow/cramp
    size = 33 if n <= 42 else (27 if n <= 64 else 22)
    pdf.set_font("Fraunces", "", size)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, size * 0.42, plan["title"], align="L")
    pdf.ln(3)
    if plan.get("subtitle"):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Inter", "", 13)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 7, plan["subtitle"], align="L")
    pdf.ln(6)
    pdf.set_draw_color(*CORAL)
    pdf.set_line_width(1.1)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 34, pdf.get_y())

    # Evidence stat strip — the moat as a credibility cue, right on the cover.
    pdf.set_xy(pdf.l_margin, pdf.h - 64)
    pdf.set_font("Inter", "B", 9.5)
    pdf.set_text_color(*INK)
    pdf.cell(0, 6, f"EVIDENCE GRADED:  {plan['cited']} CITED   ·   {plan['flagged']} FLAGGED VENDOR")
    pdf.ln(7)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Inter", "", 10)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 5.5, "Every market claim in this plan is graded by a source-credibility gate. "
                           "Vendor-marketing stats are labeled, not laundered.", align="L")
    pdf.set_xy(pdf.l_margin, pdf.h - 26)
    pdf.set_font("Inter", "", 9)
    pdf.cell(0, 5, plan["date"])
    pdf.cell(0, 5, "Fuck it. Let's go.", align="R")


def _section_head(pdf: _PDF, eyebrow: str, title: str, sub: str = "") -> None:
    pdf.set_font("Inter", "B", 9)
    pdf.set_text_color(*CORAL)
    pdf.cell(0, 6, eyebrow.upper())
    pdf.ln(7)
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Fraunces", "", 21)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 9, title, align="L")
    if sub:
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Inter", "", 11)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 6, sub, align="L")
    pdf.ln(2)
    pdf.set_draw_color(*LINE)
    pdf.set_line_width(0.3)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)


def _evidence(pdf: _PDF, plan: dict) -> None:
    pdf.add_page()
    pdf.start_section("Appendix — Evidence, graded")
    _section_head(pdf, "Appendix", "Evidence, graded",
                  "Cited = a primary/neutral source cleared it. Vendor = self-interested; treat as a "
                  "marketing claim until you verify it.")
    if not plan["evidence"]:
        pdf.set_font("Inter", "", 11)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 6, "No graded claims captured for this plan.", align="L")
        return
    for e in plan["evidence"]:
        ok = e["mark"] == "ok"
        pdf.set_font("Inter", "B", 8)
        pdf.set_fill_color(*(OK_BG if ok else WARN_BG))
        pdf.set_text_color(*(OK if ok else WARN))
        label = "  CITED  " if ok else "  VENDOR  "
        pdf.cell(pdf.get_string_width(label) + 2, 6, label, fill=True)
        pdf.ln(8)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Inter", "", 11)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 6, e["text"], align="L")
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Inter", "", 9.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 5.2, f"{e['source']}  ·  {e['note']}", align="L")
        pdf.ln(4)


def _render_toc(pdf: _PDF, outline) -> None:
    pdf.set_font("Inter", "B", 9)
    pdf.set_text_color(*CORAL)
    pdf.cell(0, 7, "C O N T E N T S")
    pdf.ln(13)
    for e in outline:
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Fraunces", "", 13)
        pdf.set_text_color(*INK)
        pdf.cell(0, 9, e.name)
        pdf.set_font("Inter", "", 11)
        pdf.set_text_color(*MUTED)
        pdf.cell(0, 9, str(e.page_number - 2), align="R")
        pdf.ln(9)
        pdf.set_draw_color(*LINE)
        pdf.set_line_width(0.2)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(3)


def render(plan: dict, style: str = "filg") -> bytes:
    """Render the plan dict to PDF bytes. `style` is a hook for future presets (MVP = 'filg')."""
    pdf = _PDF(orientation="P", unit="mm", format="Letter")
    pdf.plan_title = plan["title"]
    pdf.set_margins(28, 22, 28)
    pdf.set_auto_page_break(True, margin=20)
    _fonts(pdf)
    pdf.set_title(f"{plan['title']} — business plan")

    _cover(pdf, plan)
    pdf.add_page()                                   # fresh contents page (so the ToC renders from top)
    pdf.insert_toc_placeholder(_render_toc, pages=1)  # ToC spans this page; breaks to the next for body

    # Body flows continuously — page-break only when a section won't fit — so short sections don't
    # each strand a near-empty page. Dense + well-presented reads more pro than padded whitespace.
    pdf.start_section("Executive summary")
    _section_head(pdf, "Overview", "Executive summary")
    _body_html(pdf, plan["exec_summary"])

    for s in plan["sections"]:
        if pdf.get_y() > pdf.h - pdf.b_margin - 55:   # not enough room for a header + a few lines
            pdf.add_page()
        else:
            pdf.ln(11)
        pdf.start_section(s["title"])
        _section_head(pdf, f"Part {s['n']}", s["title"], s.get("sub", ""))
        _body_html(pdf, s["body"])

    _evidence(pdf, plan)
    out = pdf.output()
    return bytes(out)


if __name__ == "__main__":  # self-test (mock, no API) — builds a real PDF and sanity-checks it
    r = planner.research("I play guitar and want to help people learn", mock=True)
    sess = {"idea": "guitar coaching", "research": r, "files": {}, "history": [], "step": 0,
            "cost": 0.0, "status": "building", "shaped": {"thesis": "guitar coaching for adults",
            "founder_edge": "10 years teaching"}, "vetting": {"verdict": "pursue",
            "biggest_risk": "thin pipeline", "first_test": "post in 3 communities"}}
    prop, _ = planner.first_proposal(sess["idea"], r, mock=True)
    sess["proposal"] = prop
    while sess.get("status") != "done":
        sess.update(planner.advance(sess, "yes_and", None, mock=True))
    plan, cost = synthesize(sess, mock=True)
    assert plan["sections"] and len(plan["sections"]) == planner.N
    assert plan["cited"] >= 1 and plan["evidence"]
    data = render(plan)
    assert data[:5] == b"%PDF-" and len(data) > 5000
    Path("/tmp/filg-example.pdf").write_bytes(data)
    print(f"plan_pdf.py self-test OK — {len(plan['sections'])} sections, "
          f"{plan['cited']} cited/{plan['flagged']} flagged, {len(data)} bytes → /tmp/filg-example.pdf")
