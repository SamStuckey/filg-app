#!/usr/bin/env python3
"""
Thin unattended Idea → Offer pipeline (live).

This is the real version of the stage chain the dogfood runs did by hand:
    Haiku research fan-out  →  Sonnet synthesis  →  source-credibility gate
running with NO human steering. It exists to measure the one number Test #1 left
open (see ../test_01_results.md): POST-GATE survival quality — after the gate
flags self-interested claims and forces a re-search, how many flagged claims land
on a primary/neutral cite? — plus the MEASURED $/run vs the $0.21 model estimate.

Design (deliberately thin):
  1. PLAN     (Haiku)            : decompose the plain-text idea into 3 research lanes.
  2. RESEARCH (Haiku ×3, parallel): each lane runs real web_search and returns claims
                                    with the source URL it actually used.
  3. SYNTH    (Sonnet)          : one call over the research → the full artifact set
                                    (brief/offer/pricing/GTM/delivery/roadmap).
  4. GATE     (heuristic + Haiku judge): classify every quantitative claim; flag the
                                    self-interested / non-primary ones.
  5. RE-SEARCH(Haiku + web_search): for each flagged claim, try to find a primary or
                                    neutral source; re-judge → survival.

Every API call's token usage is metered against published per-MTok prices, so the
run prints a real $/run to compare against the $0.21 lean-routing estimate.

Run:  python3 pipeline.py            # uses the built-in fresh prompt
      python3 pipeline.py "your idea in plain text"
Needs ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

import anthropic

from source_credibility_gate import (
    classify_domain,
    TIER_PRIMARY, TIER_RESEARCH, TIER_VENDOR, TIER_FORUM, TIER_UNKNOWN,
)

# --- Models (lean routing: research=Haiku, synth=Sonnet) ---------------------
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"

# --- Prices: USD per 1M tokens (input, output) -------------------------------
PRICES = {HAIKU: (1.0, 5.0), SONNET: (3.0, 15.0)}
WEB_SEARCH_PRICE = 10.0 / 1000  # $10 per 1k searches (Anthropic server tool)

# web_search defaults to programmatic calling, which Haiku can't do — pin to direct.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search",
                   "max_uses": 4, "allowed_callers": ["direct"]}

client = anthropic.Anthropic()


# --- Cost ledger -------------------------------------------------------------
@dataclass
class Ledger:
    rows: list = field(default_factory=list)  # (stage, model, tin, tout, searches)

    def add(self, stage: str, model: str, usage) -> None:
        searches = getattr(getattr(usage, "server_tool_use", None),
                           "web_search_requests", 0) or 0
        self.rows.append((stage, model, usage.input_tokens, usage.output_tokens, searches))

    def cost(self) -> float:
        return self.cost_slice(0)

    def cost_slice(self, start: int) -> float:
        """Cost of rows added since index `start` — lets the server meter one run even though
        the ledger is process-global. (Production: use a per-request ledger.)"""
        total = 0.0
        for _stage, model, tin, tout, searches in self.rows[start:]:
            pin, pout = PRICES[model]
            total += tin / 1e6 * pin + tout / 1e6 * pout + searches * WEB_SEARCH_PRICE
        return total

    def breakdown(self) -> dict:
        agg: dict[str, float] = {}
        for stage, model, tin, tout, searches in self.rows:
            pin, pout = PRICES[model]
            c = tin / 1e6 * pin + tout / 1e6 * pout + searches * WEB_SEARCH_PRICE
            agg[stage] = agg.get(stage, 0.0) + c
        return agg

    def searches(self) -> int:
        return sum(r[4] for r in self.rows)


LEDGER = Ledger()


# --- Low-level call with server-tool resume ----------------------------------
def call(stage: str, model: str, prompt: str, *, max_tokens: int = 1500,
         tools: list | None = None, system: str | None = None) -> str:
    """One logical turn. Resumes server-tool loops on pause_turn. Returns text."""
    messages = [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    for _ in range(6):  # cap resume hops
        kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        LEDGER.add(stage, model, resp.usage)
        text_parts.extend(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return "\n".join(p for p in text_parts if p).strip()


def judge(c: "Claim") -> str:
    """Metered Haiku source-credibility judge (the gate's --judge path, but on our
    ledger so its tokens land in $/run). Returns TRUST / CROSS_CHECK / FLAG_SELF_INTERESTED."""
    prompt = (
        "You are a source-credibility auditor for a research tool. Given a claim and its "
        "source URL, judge whether the claim should be trusted as fact or flagged as a "
        "self-interested marketing claim.\n\n"
        f"CLAIM: {c.text}\nSOURCE: {c.source_url}\n\n"
        "Reply with exactly one token: TRUST, CROSS_CHECK, or FLAG_SELF_INTERESTED. "
        "Use FLAG_SELF_INTERESTED when the source is a company that profits if a reader "
        "believes the number."
    )
    raw = (call("judge", HAIKU, prompt, max_tokens=24) or "").upper()
    # The model doesn't always obey "one token" — normalize to the verdict it named.
    for tok in ("FLAG_SELF_INTERESTED", "CROSS_CHECK", "TRUST"):
        if tok in raw:
            return tok
    return "?"


def extract_json(text: str):
    """Pull the first JSON object/array out of a model response (handles fences)."""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = candidate.find(opener), candidate.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(candidate[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


# --- Stage 1: PLAN -----------------------------------------------------------
def plan(idea: str) -> list[str]:
    out = call("plan", HAIKU, max_tokens=400, prompt=(
        "You are the research planner for an Idea→Offer engine. Given a plain-text "
        "business idea from a solo operator, output the 3 most decision-relevant "
        "research lanes to investigate (e.g. market size, competitor/pricing norms, "
        "buyer pain). Each lane is one specific researchable question.\n\n"
        f"IDEA:\n{idea}\n\n"
        'Reply ONLY with JSON: {"lanes": ["question 1", "question 2", "question 3"]}'
    ))
    data = extract_json(out) or {}
    lanes = data.get("lanes") if isinstance(data, dict) else None
    if not lanes:
        lanes = ["What is the market size and number of target buyers?",
                 "Who are the competitors and what are the pricing norms?",
                 "What is the buyer's most acute, expensive pain point?"]
    return lanes[:3]


# --- Stage 2: RESEARCH fan-out ----------------------------------------------
@dataclass
class Claim:
    text: str
    source_url: str
    quantitative: bool
    promotes_category: str | None


def research_lane(idea: str, lane: str) -> list[Claim]:
    out = call("research", HAIKU, max_tokens=1600, tools=[WEB_SEARCH_TOOL], prompt=(
        "You are a research agent for an Idea→Offer engine. Use web_search to answer "
        "the question with SPECIFIC, sourced facts. Prefer hard numbers. For every "
        "claim, record the exact source URL you took it from.\n\n"
        f"BUSINESS IDEA:\n{idea}\n\nRESEARCH QUESTION:\n{lane}\n\n"
        "After researching, reply with ONLY a JSON array of up to 5 claims:\n"
        '[{"text": "the claim incl. the number", "source_url": "https://...", '
        '"quantitative": true, "promotes_category": "the thing this number makes look '
        'good, e.g. \'outsourced X\', or null if neutral"}]'
    ))
    data = extract_json(out) or []
    claims = []
    for c in data if isinstance(data, list) else []:
        if isinstance(c, dict) and c.get("text") and c.get("source_url"):
            claims.append(Claim(
                text=str(c["text"]).strip(),
                source_url=str(c["source_url"]).strip(),
                quantitative=bool(c.get("quantitative", True)),
                promotes_category=(c.get("promotes_category") or None),
            ))
    return claims


# --- Stage 3: SYNTH ----------------------------------------------------------
def synthesize(idea: str, claims: list[Claim]) -> str:
    research_block = "\n".join(
        f"- {c.text}  [{c.source_url}]" for c in claims)
    return call("synth", SONNET, max_tokens=3500, prompt=(
        "You are the synthesis stage of an Idea→Offer engine. From the operator's idea "
        "and the cited research below, produce a sellable artifact set in markdown: "
        "(1) structured brief, (2) offer definition, (3) packaging + pricing, "
        "(4) go-to-market, (5) delivery playbook, (6) 30-day roadmap. Cite the research "
        "inline with its URL. Where a number comes from a source that profits if you "
        "believe it, mark it '(unverified vendor claim)'. Be concrete and specific.\n\n"
        f"IDEA:\n{idea}\n\nCITED RESEARCH:\n{research_block}"
    ))


# --- Stage 4: GATE -----------------------------------------------------------
@dataclass
class Verdict:
    claim: Claim
    tier: str
    judge: str
    flagged: bool
    reason: str


def gate_claim(c: Claim) -> Verdict:
    """Heuristic tier + Haiku judge. Flag self-interested / non-primary quant claims."""
    tier, sells = classify_domain(c.source_url)
    jv = judge(c)
    flagged = (
        jv == "FLAG_SELF_INTERESTED"
        or (tier == TIER_VENDOR and c.quantitative and sells is not None
            and sells == c.promotes_category)
        or (c.quantitative and tier in (TIER_VENDOR, TIER_UNKNOWN, TIER_FORUM)
            and jv != "TRUST")
    )
    if tier == TIER_PRIMARY:
        reason = "primary/authoritative source"
    elif tier == TIER_RESEARCH:
        reason = "third-party research firm"
    elif flagged:
        reason = f"{tier.lower()} source + judge={jv} → needs a primary cite"
    else:
        reason = f"{tier.lower()} source, judge={jv}"
    return Verdict(c, tier, jv, flagged, reason)


def self_interested(tier: str, jv: str) -> bool:
    """The property the gate exists to kill: a claim resting on a source that profits
    if you believe it. Judge-led (the unattended mechanism) + the registry's known
    vendors. NOT registry-led — a fresh neutral domain the judge clears counts as safe,
    because the hand-built tier registry can't know every neutral source on a new niche."""
    return jv == "FLAG_SELF_INTERESTED" or tier == TIER_VENDOR


def survives(tier: str, jv: str) -> bool:
    return not self_interested(tier, jv)


# --- Stage 5: RE-SEARCH flagged ---------------------------------------------
@dataclass
class Rescue:
    original: Verdict
    new_url: str | None
    new_tier: str
    new_judge: str
    rescued: bool


def research_primary(c: Claim) -> Rescue | None:
    out = call("research2", HAIKU, max_tokens=900, tools=[WEB_SEARCH_TOOL], prompt=(
        "A claim in our research came from a source that may be self-interested. Use "
        "web_search to find the SAME fact stated by a PRIMARY or NEUTRAL source "
        "(government / official statistics / standards body / independent research "
        "firm), not a vendor that profits from the claim.\n\n"
        f"CLAIM: {c.text}\nORIGINAL SOURCE: {c.source_url}\n\n"
        'Reply ONLY with JSON: {"found": true/false, "source_url": "https://...", '
        '"note": "what the neutral source says"}'
    ))
    data = extract_json(out) or {}
    if not isinstance(data, dict) or not data.get("source_url"):
        return Rescue(None, None, "NONE", "?", False)  # placeholder, fixed by caller
    new_url = str(data["source_url"]).strip()
    new_tier, _ = classify_domain(new_url)
    new_claim = Claim(c.text, new_url, c.quantitative, c.promotes_category)
    new_judge = judge(new_claim)
    return Rescue(None, new_url, new_tier, new_judge, survives(new_tier, new_judge))


# --- Orchestrator ------------------------------------------------------------
DEFAULT_IDEA = (
    "I'm good with automation and AI tools. I've noticed that small, independent "
    "property-management companies (the ones managing a few hundred rental units) are "
    "slow to respond to tenant maintenance requests and leasing inquiries, and they "
    "lose tenants and prospective renters because of it. I think I could sell them "
    "something AI-powered, but I don't know exactly what the offer is or how I'd sell it."
)


def main() -> int:
    idea = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IDEA
    t0 = time.time()
    print("\n=== LIVE PIPELINE: Idea → Offer (unattended) ===")
    print(f"\nPROMPT (fresh niche):\n{idea}\n")

    # 1. PLAN
    lanes = plan(idea)
    print("PLAN — research lanes:")
    for ln in lanes:
        print(f"  • {ln}")

    # 2. RESEARCH fan-out (parallel)
    print("\nRESEARCH — Haiku fan-out (live web_search, parallel)…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        lane_claims = list(ex.map(lambda ln: research_lane(idea, ln), lanes))
    claims = [c for lane in lane_claims for c in lane]
    quant = [c for c in claims if c.quantitative]
    print(f"  {len(claims)} claims gathered ({len(quant)} quantitative).")

    # 3. SYNTH
    print("\nSYNTH — Sonnet artifact set…")
    artifacts = synthesize(idea, claims)
    with open("live_run_artifacts.md", "w") as f:
        f.write(f"# Live run — artifact set\n\n**Prompt:** {idea}\n\n---\n\n{artifacts}\n")
    print(f"  artifact set written ({len(artifacts)} chars) → live_run_artifacts.md")

    # 4. GATE
    print("\nGATE — credibility check on quantitative claims (heuristic + Haiku judge)…")
    verdicts = [gate_claim(c) for c in quant]
    flagged = [v for v in verdicts if v.flagged]
    clean = [v for v in verdicts if not v.flagged]
    for v in verdicts:
        mark = "FLAG" if v.flagged else ("OK  " if v in clean else "weak")
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        print(f"  [{mark}] {host:<28} {v.claim.text[:70]}")
    n = len(verdicts) or 1
    print(f"\n  pre-re-search: {len(clean)}/{n} clean ({len(clean)/n:.0%}), "
          f"{len(flagged)} flagged ({len(flagged)/n:.0%})")

    # 5. RE-SEARCH flagged → survival
    print("\nRE-SEARCH — forcing a primary/neutral cite for each flagged claim…")
    rescues: list[Rescue] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda v: research_primary(v.claim), flagged))
    for v, r in zip(flagged, results):
        r.original = v
        rescues.append(r)
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none found)"
        print(f"  [{'RESCUED' if r.rescued else 'still weak'}] "
              f"{v.claim.text[:55]} → {host} (tier={r.new_tier}, judge={r.new_judge})")

    rescued = [r for r in rescues if r.rescued]
    survived = len(clean) + len(rescued)
    survival_q = survived / n
    rescue_rate = (len(rescued) / len(flagged)) if flagged else 0.0

    # --- Report ---
    cost = LEDGER.cost()
    elapsed = time.time() - t0
    print("\n=== RESULTS ===")
    print(f"(a) POST-GATE SURVIVAL QUALITY")
    print(f"    initial clean         : {len(clean)}/{n} ({len(clean)/n:.0%})")
    print(f"    flagged → re-searched : {len(flagged)}")
    print(f"    rescued to primary    : {len(rescued)}/{len(flagged) or 0} "
          f"(rescue rate {rescue_rate:.0%})")
    print(f"    >> SURVIVAL after gate: {survived}/{n} = {survival_q:.0%} "
          f"land on a primary/neutral cite")
    print(f"\n(b) MEASURED $/RUN")
    for stage, c in sorted(LEDGER.breakdown().items(), key=lambda x: -x[1]):
        print(f"    {stage:<10} ${c:.4f}")
    print(f"    web searches: {LEDGER.searches()}  (${LEDGER.searches()*WEB_SEARCH_PRICE:.4f})")
    print(f"    >> TOTAL    : ${cost:.4f}/run   (model estimate was $0.21)")
    print(f"\n    wall-clock: {elapsed:.0f}s")

    write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed)
    print("\n  results written → test_01_live_results_auto.md\n")
    return 0


def write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed) -> None:
    bd = LEDGER.breakdown()
    n_rescued = len([r for r in rescues if r.rescued])
    lines = [
        "# Test #1 (live) — post-gate survival quality + measured $/run",
        "",
        "Ran the thin unattended pipeline (Haiku research fan-out → Sonnet synthesis → "
        "source-credibility gate) end-to-end on one fresh prompt, with real `web_search`. "
        "This closes the open number from `test_01_results.md`.",
        "",
        f"**Prompt (fresh niche — not seen in either dogfood run):** {idea}",
        "",
        "**Research lanes (auto-planned):**",
        *[f"- {ln}" for ln in lanes],
        "",
        "## (a) Post-gate survival quality — THE open number",
        "",
        f"- Quantitative claims gated: **{n}**",
        f"- Clean on first pass: **{len(clean)}/{n} ({len(clean)/n:.0%})**",
        f"- Flagged (self-interested / non-primary) → forced re-search: **{len(flagged)}**",
        f"- Rescued to a primary/neutral cite: **{n_rescued}/{len(flagged)}** "
        f"(rescue rate **{rescue_rate:.0%}**)",
        f"- **Survival after the gate: {survived}/{n} = {survival_q:.0%}** of cited "
        "quantitative claims land on a primary/neutral source.",
        "",
        "### Per-claim",
        "",
        "| verdict | source | claim | judge |",
        "|---|---|---|---|",
    ]
    for v in verdicts:
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        mark = "FLAG" if v.flagged else ("clean" if v in clean else "weak")
        lines.append(f"| {mark} | {host} | {v.claim.text[:80]} | {v.judge} |")
    lines += ["", "### Re-search outcomes", "",
              "| original claim | new source | tier | judge | rescued |",
              "|---|---|---|---|---|"]
    for r in rescues:
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none)"
        lines.append(f"| {r.original.claim.text[:60]} | {host} | {r.new_tier} | "
                     f"{r.new_judge} | {'yes' if r.rescued else 'no'} |")
    lines += [
        "",
        "## (b) Measured $/run vs the $0.21 estimate",
        "",
        "| stage | cost |",
        "|---|--:|",
        *[f"| {s} | ${c:.4f} |" for s, c in sorted(bd.items(), key=lambda x: -x[1])],
        f"| **total** | **${cost:.4f}** |",
        "",
        f"- Model estimate (`pipeline_economics.py`, lean routing): **$0.21/run**.",
        f"- Measured live: **${cost:.4f}/run** ({LEDGER.searches()} web searches "
        f"@ $0.01 included).",
        f"- Wall-clock: {elapsed:.0f}s.",
        "",
        "## Caveats",
        "- One run, one niche — survival % is indicative, not a distribution. Re-run "
        "across niches to get a confidence interval.",
        "- Web-search server-tool cost ($10/1k searches) is included here but was NOT in "
        "the $0.21 token-only model — see the breakdown above for the split.",
        "- Re-search success depends on whether a neutral source actually publishes the "
        "fact; a 'still weak' outcome can mean the number only exists in vendor marketing "
        "(itself a useful signal to down-rank the claim).",
    ]
    # Auto-generated machine output. The curated file of record (with interpretation)
    # is test_01_live_results.md — kept separate so a re-run doesn't wipe the analysis.
    with open("test_01_live_results_auto.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
