#!/usr/bin/env python3
"""
Source-credibility + verify gate — the engine's source-tier registry + COI heuristics.

The dogfood runs proved the engine's moat (cited research) is fragile: on an
unfamiliar niche it laundered self-interested vendor-blog stats as fact
(e.g. "$201,600/yr from switching" from a billing vendor's marketing page).
This gate is the thing that protects the moat.

What it does, per research claim:
  1. Classify the source domain into a credibility tier.
  2. Detect conflict of interest — a quantitative claim that makes a category
     look good, sourced from a domain that SELLS in that category.
  3. Emit a verdict; FAIL the gate if any quantitative claim is sourced from a
     self-interested vendor and not backed by a primary/neutral source.

Two modes:
  - heuristic (default): pure-stdlib domain registry + rules. Runs with no API key.
  - --judge: also asks an LLM to classify each source (routed to Haiku — this is a
    cheap classify task per the token-optimization rule). Needs ANTHROPIC_API_KEY.

Run:
    python3 source_credibility_gate.py            # heuristic gate on the seeded claims
    python3 source_credibility_gate.py --judge     # + LLM cross-check (needs key)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from urllib.parse import urlparse

# --- Tiers -------------------------------------------------------------------
TIER_PRIMARY = "PRIMARY"        # gov / official stats / standards body / peer-reviewed
TIER_RESEARCH = "RESEARCH"      # third-party market-research / analytics firm
TIER_VENDOR = "VENDOR"          # a company that sells a product/service
TIER_FORUM = "FORUM"            # reddit / quora / medium / forum
TIER_UNKNOWN = "UNKNOWN"

# --- Domain registry ---------------------------------------------------------
# (tier, sells_category)  — sells_category is what the domain has a commercial
# interest in promoting; None for neutral sources.
DOMAIN_REGISTRY = {
    # primary / authoritative
    "ama-assn.org":            (TIER_PRIMARY, None),
    "census.gov":              (TIER_PRIMARY, None),
    "mgma.com":                (TIER_PRIMARY, None),   # industry benchmark body
    "hfma.org":                (TIER_PRIMARY, None),
    # third-party research firms
    "grandviewresearch.com":   (TIER_RESEARCH, None),
    "mordorintelligence.com":  (TIER_RESEARCH, None),
    "chartmogul.com":          (TIER_RESEARCH, None),
    "ibisworld.com":           (TIER_RESEARCH, None),
    "statista.com":            (TIER_RESEARCH, None),
    # vendors — sells_category is the conflict to watch for
    "carecloud.com":           (TIER_VENDOR, "outsourced_billing"),
    "neolytix.com":            (TIER_VENDOR, "outsourced_billing"),
    "listerventures.com":      (TIER_VENDOR, "outsourced_billing"),
    "aptarro.com":             (TIER_VENDOR, "denial_management"),
    "icsystem.com":            (TIER_VENDOR, "ar_collections"),
    "getaira.io":              (TIER_VENDOR, "ai_receptionist"),
    "caseyresponse.com":       (TIER_VENDOR, "lead_response"),
}


def classify_domain(url: str) -> tuple[str, str | None]:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host in DOMAIN_REGISTRY:
        return DOMAIN_REGISTRY[host]
    # pattern fallbacks
    if host.endswith((".gov", ".edu")) or host.endswith(".gov.uk"):
        return (TIER_PRIMARY, None)
    if any(host.endswith(d) or host == d for d in
           ("reddit.com", "quora.com", "medium.com")):
        return (TIER_FORUM, None)
    # unknown commercial domain — treat with caution, not trust
    return (TIER_UNKNOWN, None)


# --- Claim model -------------------------------------------------------------
@dataclass
class Claim:
    text: str
    source_url: str
    quantitative: bool          # is it a hard number / stat?
    promotes_category: str | None  # the category this claim makes look good (or None if neutral)
    run: str = ""               # which dogfood run it came from


# --- Verdicts ----------------------------------------------------------------
PASS_HIGH = "PASS_HIGH"             # primary source
PASS_OK = "PASS_OK"                 # research firm / neutral, acceptable
FLAG_WEAK = "FLAG_WEAK"             # quantitative claim from vendor/unknown, no COI — needs a neutral cross-cite
FLAG_SELF_INTERESTED = "FLAG_SELF_INTERESTED"  # COI: vendor profits if you believe the number


@dataclass
class Result:
    claim: Claim
    tier: str
    sells: str | None
    verdict: str
    reason: str


def evaluate(claim: Claim) -> Result:
    tier, sells = classify_domain(claim.source_url)
    conflict = (
        claim.quantitative
        and tier == TIER_VENDOR
        and sells is not None
        and sells == claim.promotes_category
    )
    if conflict:
        return Result(claim, tier, sells, FLAG_SELF_INTERESTED,
                      f"vendor sells '{sells}' and the number makes '{claim.promotes_category}' "
                      f"look good — treat as marketing, demand a primary cite")
    if tier == TIER_PRIMARY:
        return Result(claim, tier, sells, PASS_HIGH, "authoritative/primary source")
    if tier == TIER_RESEARCH and not (claim.quantitative and claim.promotes_category):
        return Result(claim, tier, sells, PASS_OK, "third-party research firm, no obvious COI")
    if claim.quantitative and tier in (TIER_VENDOR, TIER_UNKNOWN, TIER_FORUM, TIER_RESEARCH):
        return Result(claim, tier, sells, FLAG_WEAK,
                      "quantitative claim from a non-primary source — cross-check with a neutral cite")
    return Result(claim, tier, sells, PASS_OK, "qualitative / low-risk")


# --- Seeded claims (verbatim from the dogfood runs) --------------------------
CLAIMS = [
    # ---- run 02: medical billing (unfamiliar niche) ----
    Claim("22.4% of physicians are in independent practice (down from 37.8% in 2019)",
          "https://www.ama-assn.org/system/files/2024-prp-pp-characteristics.pdf",
          True, None, run="02"),
    Claim("US medical-billing-outsourcing market ~$15-17B (2024)",
          "https://www.grandviewresearch.com/industry-analysis/us-medical-billing-outsourcing-market",
          True, "outsourced_billing", run="02"),
    Claim("MGMA benchmark: outsourced billing ~5% of net collections",
          "https://www.mgma.com/", True, None, run="02"),
    Claim("First-pass claim denial ~12% overall; small practices 15-18%",
          "https://www.aptarro.com/insights/us-healthcare-denial-rates-reimbursement-statistics",
          True, "denial_management", run="02"),
    Claim("In-house billing costs $55-80K/yr per biller",
          "https://carecloud.com/cost-of-medical-billing-services/",
          True, "outsourced_billing", run="02"),
    Claim("A practice switching to outsourced billing gains avg $201,600/yr in revenue",
          "https://listerventures.com/addressing-5-key-concerns-you-may-have-while-changing-your-rcm-provider/",
          True, "outsourced_billing", run="02"),
    Claim("Outsourcing clinics are 30% more likely to hit net-revenue targets",
          "https://neolytix.com/articles/what-is-the-going-rate-for-medical-billing-services/",
          True, "outsourced_billing", run="02"),
    # ---- run 01: home-service AI receptionist (familiar niche) ----
    Claim("~62% of calls to small businesses go unanswered",
          "https://www.getaira.io/blog/missed-business-calls-statistics",
          True, "ai_receptionist", run="01"),
    Claim("78% of customers buy from the first business that responds",
          "https://www.getaira.io/blog/missed-business-calls-statistics",
          True, "ai_receptionist", run="01"),
]


# --- Optional LLM judge (Haiku — cheap classify task) ------------------------
def judge_llm(claim: Claim) -> str:
    """Cross-check one claim with an LLM. Routed to Haiku per token-optimization
    (source classification is a cheap classify task, not reasoning/strategy)."""
    import anthropic  # lazy import so the heuristic path needs no dependency

    client = anthropic.Anthropic()
    prompt = (
        "You are a source-credibility auditor for a research tool. Given a claim and its "
        "source URL, judge whether the claim should be trusted as fact or flagged as a "
        "self-interested marketing claim.\n\n"
        f"CLAIM: {claim.text}\n"
        f"SOURCE: {claim.source_url}\n\n"
        "Reply with exactly one token: TRUST, CROSS_CHECK, or FLAG_SELF_INTERESTED. "
        "Use FLAG_SELF_INTERESTED when the source is a company that profits if a reader "
        "believes the number."
    )
    resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=16,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text.strip() for b in resp.content if b.type == "text"), "?")


# --- Runner ------------------------------------------------------------------
def main() -> int:
    use_judge = "--judge" in sys.argv
    order = {FLAG_SELF_INTERESTED: 0, FLAG_WEAK: 1, PASS_OK: 2, PASS_HIGH: 3}
    results = sorted((evaluate(c) for c in CLAIMS), key=lambda r: order[r.verdict])

    print("\n=== SOURCE-CREDIBILITY + VERIFY GATE ===\n")
    for r in results:
        host = urlparse(r.claim.source_url).netloc.removeprefix("www.")
        print(f"[{r.verdict}]  (run {r.claim.run}, tier={r.tier})")
        print(f"   claim : {r.claim.text}")
        print(f"   source: {host} — {r.reason}")
        if use_judge:
            try:
                print(f"   judge : {judge_llm(r.claim)}  (Haiku cross-check)")
            except Exception as e:  # noqa: BLE001 — prototype: surface any judge failure, don't crash the gate
                print(f"   judge : (skipped: {e})")
        print()

    flagged = [r for r in results if r.verdict == FLAG_SELF_INTERESTED]
    weak = [r for r in results if r.verdict == FLAG_WEAK]
    print("--- SUMMARY ---")
    print(f"  {len(flagged)} self-interested (would be laundered as fact without this gate)")
    print(f"  {len(weak)} weak-source (need a neutral cross-cite)")
    print(f"  {len(results) - len(flagged) - len(weak)} passed")
    if flagged:
        print("\nGATE: FAIL — self-interested quantitative claims must be replaced with a primary")
        print("cite or down-ranked to 'marketing claim, unverified' before this ships to a user.")
        return 1
    print("\nGATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
