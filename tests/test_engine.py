"""Engine tests: the source-credibility gate, the batched judge, and call() caching.

These cover the token-optimization work and the gate logic without spending on the
API (pipeline.call / the anthropic client are monkeypatched)."""

import types

import pipeline
import provider
from pipeline import Claim, gate_claim, judge_batch
from source_credibility_gate import TIER_VENDOR


def _bound(cap):
    """A bound anthropic provider wrapping a fake client — FILG is user-key-only, so every real call
    routes through a bound provider (no global FILG client to monkeypatch)."""
    prov = provider.Provider("anthropic", "anthropic", _fake_client(cap),
                             {pipeline.SONNET: pipeline.SONNET})
    return provider.use(prov)


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


def test_call_marks_system_block_cacheable():
    cap = {}
    with _bound(cap):
        pipeline.call("s", pipeline.SONNET, "hi", system="STABLE RULES", cache=True)
    assert isinstance(cap["system"], list)
    assert cap["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert cap["system"][0]["text"] == "STABLE RULES"


def test_call_plain_system_when_uncached():
    cap = {}
    with _bound(cap):
        pipeline.call("s", pipeline.SONNET, "hi", system="RULES")
    assert cap["system"] == "RULES"


def test_call_without_a_bound_provider_raises():
    import pytest
    assert provider.active() is None
    with pytest.raises(RuntimeError):   # user-key-only: no provider bound is a hard error, not a fallback
        pipeline.call("s", pipeline.SONNET, "hi")


def test_web_search_tool_downgrades_on_haiku():
    # web_search_20260209 (dynamic filtering) 400s on Haiku 4.5 — the default stack's research model.
    # _call_anthropic must swap it for the basic web_search_20250305 when the resolved model is Haiku,
    # and leave it alone on Sonnet/Opus, so a real Anthropic key still returns cited research.
    haiku = pipeline._web_tools_for_model([dict(pipeline.WEB_SEARCH_TOOL)], pipeline.HAIKU)
    assert haiku[0]["type"] == "web_search_20250305"
    sonnet = pipeline._web_tools_for_model([dict(pipeline.WEB_SEARCH_TOOL)], pipeline.SONNET)
    assert sonnet[0]["type"] == "web_search_20260209"


def test_web_search_sent_to_client_matches_model():
    # End-to-end through the bound client: a research call resolving to Haiku sends the basic variant.
    cap = {}
    prov = provider.Provider("anthropic", "anthropic", _fake_client(cap),
                             {pipeline.HAIKU: pipeline.HAIKU})
    with provider.use(prov):
        pipeline.call("research", pipeline.HAIKU, "find facts", tools=[dict(pipeline.WEB_SEARCH_TOOL)])
    assert cap["tools"][0]["type"] == "web_search_20250305"


def test_as_year_coerces_and_bounds():
    assert pipeline._as_year(2024) == 2024
    assert pipeline._as_year("2021") == 2021
    assert pipeline._as_year("n/a") is None
    assert pipeline._as_year(1500) is None   # implausible year rejected


# ── voted moat gate + staleness ───────────────────────────────────────────────
def test_judge_batch_voted_resolves_by_majority(monkeypatch):
    rounds = iter([
        '[{"i": 1, "verdict": "TRUST"}, {"i": 2, "verdict": "TRUST"}]',
        '[{"i": 1, "verdict": "TRUST"}, {"i": 2, "verdict": "FLAG_SELF_INTERESTED"}]',
        '[{"i": 1, "verdict": "TRUST"}, {"i": 2, "verdict": "FLAG_SELF_INTERESTED"}]',
    ])
    monkeypatch.setattr(pipeline, "call", lambda *a, **k: next(rounds))
    out = pipeline.judge_batch_voted([_claim("https://census.gov"), _claim("https://v.com")], votes=3)
    assert out == ["TRUST", "FLAG_SELF_INTERESTED"]   # claim2: 2 flags beat 1 trust


def test_judge_batch_voted_defaults_to_flag_on_tie(monkeypatch):
    rounds = iter(['[{"i": 1, "verdict": "TRUST"}]', '[{"i": 1, "verdict": "FLAG_SELF_INTERESTED"}]'])
    monkeypatch.setattr(pipeline, "call", lambda *a, **k: next(rounds))
    # a 1-1 tie must route to the safe (flag) side — the moat defaults to flag on ambiguity.
    assert pipeline.judge_batch_voted([_claim("https://v.com")], votes=2) == ["FLAG_SELF_INTERESTED"]


def test_is_stale_window():
    assert pipeline._is_stale(Claim("x", "u", True, None, as_of=2020), now_year=2026)       # 6y > 36mo
    assert not pipeline._is_stale(Claim("x", "u", True, None, as_of=2024), now_year=2026)   # 2y < 36mo
    assert not pipeline._is_stale(Claim("x", "u", True, None, as_of=None), now_year=2026)   # unknown year
    assert not pipeline._is_stale(Claim("x", "u", False, None, as_of=2000), now_year=2026)  # not quantitative


def test_gate_flags_stale_even_when_primary():
    c = Claim("X grew 10%", "https://census.gov/x", True, None, as_of=2018)
    v = gate_claim(c, jv="TRUST", now_year=2026)
    assert v.flagged is True and v.stale is True and "stale" in v.reason


def test_gate_keeps_fresh_primary():
    c = Claim("X grew 10%", "https://census.gov/x", True, None, as_of=2025)
    v = gate_claim(c, jv="TRUST", now_year=2026)
    assert v.flagged is False and v.stale is False


def test_gate_claim_records_atomic_checks():
    v = gate_claim(_claim("https://vendor.io/x", promotes="cat"), jv="FLAG_SELF_INTERESTED", now_year=2026)
    assert v.checks["self_interested"] is True and v.checks["stale"] is False and v.checks["tier"]


def test_build_evidence_emits_leaf_events(monkeypatch):
    # The runner's leaf viz is driven by two sentinel progress lines: §LANES§<json> up front, then a
    # §LANEDONE§<index> as each lane future completes. Guard that build_evidence emits both, with one
    # done-event per lane, so the frontend can paint grey→green leaves.
    import teardown
    lanes = ["lane A?", "lane B?", "lane C?"]
    monkeypatch.setattr(pipeline, "plan", lambda idea: list(lanes))
    monkeypatch.setattr(pipeline, "research_lane", lambda idea, ln: [])
    monkeypatch.setattr(pipeline, "gate_claims", lambda claims: [])
    monkeypatch.setattr(pipeline, "research_primary", lambda c: None)
    seen = []
    teardown.build_evidence("an idea", 3, on_progress=seen.append)
    lanes_lines = [s for s in seen if s.startswith("§LANES§")]
    done_lines = [s for s in seen if s.startswith("§LANEDONE§")]
    assert len(lanes_lines) == 1 and __import__("json").loads(lanes_lines[0][len("§LANES§"):]) == lanes
    assert sorted(int(s[len("§LANEDONE§"):]) for s in done_lines) == [0, 1, 2]  # one green leaf per lane


def test_label_triangulation_marks_single_vs_corroborated():
    import teardown
    rows = [
        {"mark": "ok", "url": "https://census.gov/x", "lane": "market"},
        {"mark": "ok", "url": "https://bls.gov/y", "lane": "market"},      # different host, same lane
        {"mark": "ok", "url": "https://mgma.com/z", "lane": "pricing"},    # alone in its lane
        {"mark": "warn", "url": "https://vendor.com/w", "lane": "pricing"},
    ]
    teardown._label_triangulation(rows)
    assert rows[0]["corroborated"] is True and rows[1]["corroborated"] is True
    assert rows[2]["corroborated"] is False and rows[2]["sources"] == 1
    assert "corroborated" not in rows[3]   # flagged rows are not labeled (already unverified)
