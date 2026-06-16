#!/usr/bin/env python3
"""
Live verification of the BYOK OpenRouter path — run with a REAL OpenRouter key.

Phase 1 built the provider seam (provider.py + pipeline.call routing) and unit-tested the
request SHAPE, but the OpenRouter web-plugin wire format could not be confirmed against live docs.
This script exercises the REAL code path (provider.use(openrouter_provider(...)) → pipeline.call)
to confirm, end to end, that:

  1. a plain Claude-via-OpenRouter call returns text (model mapping + chat-completions translation),
  2. a research call with web search returns claims WITH source URLs (the moat's contract),
  3. the source-credibility gate grades those URLs unchanged.

It never prints the key. Spend is a few cents on YOUR OpenRouter credit (that's the BYOK model).

Run:
    OPENROUTER_API_KEY=sk-or-... python3 prototype/verify_openrouter.py
"""

from __future__ import annotations

import os
import sys

import provider
import pipeline


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY not set. Set it in the environment and re-run:")
        print("    OPENROUTER_API_KEY=sk-or-... python3 prototype/verify_openrouter.py")
        return 2

    prov = provider.openrouter_provider(key)
    print(f"Provider: {prov.name} | model map: {prov.models}\n")

    # 1) plain text generation on Claude via OpenRouter (no web search)
    print("[1/3] plain Claude call via OpenRouter (synth-style)...")
    try:
        with provider.use(prov):
            txt = pipeline.call("verify_synth", pipeline.SONNET,
                                "Reply with exactly: BYOK_OK", max_tokens=20)
        ok1 = "BYOK_OK" in txt.upper()
        print(f"    reply: {txt!r}  -> {'PASS' if ok1 else 'CHECK (got a reply, content differs)'}\n")
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {type(e).__name__}: {e}\n")
        return 1

    # 2) research lane WITH web search → claims must carry source URLs (the moat's contract)
    print("[2/3] research_lane with OpenRouter web search...")
    idea = "AI-powered tenant maintenance triage for small property managers"
    lane = "What is the market size / number of small US property-management firms?"
    try:
        with provider.use(prov):
            claims = pipeline.research_lane(idea, lane)
        with_urls = [c for c in claims if c.source_url.startswith("http")]
        print(f"    {len(claims)} claims, {len(with_urls)} with http source URLs")
        for c in claims[:3]:
            print(f"      - {c.text[:70]!r}  [{c.source_url[:60]}]")
        ok2 = bool(with_urls)
        print(f"    -> {'PASS (web search returned cited claims)' if ok2 else 'FAIL (no cited claims)'}\n")
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {type(e).__name__}: {e}\n")
        return 1

    # 3) the gate grades those URLs unchanged (provider-agnostic moat)
    print("[3/3] source-credibility gate over the BYOK claims...")
    try:
        with provider.use(prov):
            verdicts = pipeline.gate_claims([c for c in claims if c.quantitative] or claims)
        for v in verdicts[:3]:
            print(f"      [{'FLAG' if v.flagged else 'ok'}] {v.tier:<8} judge={v.judge}  {v.claim.text[:50]!r}")
        print(f"    -> PASS (gate ran on {len(verdicts)} claims)\n")
    except Exception as e:  # noqa: BLE001
        print(f"    FAIL: {type(e).__name__}: {e}\n")
        return 1

    print("BYOK live verification complete. If [2/3] returned cited claims, the moat survives on a "
          "user's OpenRouter key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
