"""Engine tests: the source-credibility gate, the batched judge, and call() caching.

These cover the token-optimization work and the gate logic without spending on the
API (pipeline.call / the anthropic client are monkeypatched)."""

import types

import pipeline
from pipeline import Claim, gate_claim, judge_batch
from source_credibility_gate import TIER_VENDOR


def _claim(url, text="42% of X improves Y", quant=True, promotes=None):
    return Claim(text=text, source_url=url, quantitative=quant, promotes_category=promotes)


# ── batched judge ────────────────────────────────────────────────────────────
def test_judge_batch_parses_aligned_verdicts(patch_call):
    patch_call('[{"i": 1, "verdict": "TRUST"}, {"i": 2, "verdict": "FLAG_SELF_INTERESTED"}]')
    out = judge_batch([_claim("https://census.gov"), _claim("https://vendor.com")])
    assert out == ["TRUST", "FLAG_SELF_INTERESTED"]


def test_judge_batch_falls_back_when_malformed(patch_call):
    # Unparseable batch reply → must fall back to per-claim judging, not mis-align/crash.
    patch_call("totally not json")
    out = judge_batch([_claim("https://census.gov"), _claim("https://vendor.com")])
    assert len(out) == 2 and all(v in ("TRUST", "CROSS_CHECK", "FLAG_SELF_INTERESTED", "?") for v in out)


def test_judge_batch_empty():
    assert judge_batch([]) == []


# ── gate ─────────────────────────────────────────────────────────────────────
def test_gate_flags_self_interested_vendor():
    v = gate_claim(_claim("https://acmevendor.com"), jv="FLAG_SELF_INTERESTED")
    assert v.flagged is True


def test_gate_passes_primary_source():
    v = gate_claim(_claim("https://www.census.gov"), jv="TRUST")
    assert v.flagged is False and v.tier  # primary/authoritative isn't flagged


def test_gate_reuses_supplied_verdict_without_calling(monkeypatch):
    # If jv is provided, gate_claim must NOT call the single judge (that's the batch win).
    monkeypatch.setattr(pipeline, "judge", lambda c: (_ for _ in ()).throw(AssertionError("called")))
    v = gate_claim(_claim("https://example.com"), jv="TRUST")
    assert v.judge == "TRUST"


# ── call() prompt caching ────────────────────────────────────────────────────
def _fake_client(capture):
    def create(**kwargs):
        capture.update(kwargs)
        usage = types.SimpleNamespace(input_tokens=10, output_tokens=5, server_tool_use=None)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="ok")],
            usage=usage, stop_reason="end_turn")
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def test_call_marks_system_block_cacheable(monkeypatch):
    cap = {}
    monkeypatch.setattr(pipeline, "client", _fake_client(cap))
    pipeline.call("s", pipeline.SONNET, "hi", system="STABLE RULES", cache=True)
    assert isinstance(cap["system"], list)
    assert cap["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert cap["system"][0]["text"] == "STABLE RULES"


def test_call_plain_system_when_uncached(monkeypatch):
    cap = {}
    monkeypatch.setattr(pipeline, "client", _fake_client(cap))
    pipeline.call("s", pipeline.SONNET, "hi", system="RULES")
    assert cap["system"] == "RULES"
