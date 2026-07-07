"""BYOK provider seam: pipeline.call() routes to the active provider.

No network: we monkeypatch fake clients and assert the request SHAPE per provider. The default
(no active provider) must stay byte-for-byte the legacy Anthropic path so the free run + every
existing test are untouched."""

import types

from engine import pipeline
from engine import provider


# ── fake clients ──────────────────────────────────────────────────────────────
def _fake_anthropic(capture):
    def create(**kwargs):
        capture.update(kwargs)
        usage = types.SimpleNamespace(input_tokens=10, output_tokens=5, server_tool_use=None)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="anthropic-reply")],
            usage=usage, stop_reason="end_turn")
    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def _fake_openai(capture):
    def create(**kwargs):
        capture.update(kwargs)
        usage = types.SimpleNamespace(prompt_tokens=20, completion_tokens=8)
        msg = types.SimpleNamespace(content="openrouter-reply")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    return types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))


# ── Anthropic path routes through the BOUND user provider (no FILG fallback) ───
def test_anthropic_byok_routes_through_bound_client():
    cap = {}
    prov = provider.Provider("anthropic", "anthropic", _fake_anthropic(cap),
                             {pipeline.SONNET: pipeline.SONNET}, bills_filg=False)
    with provider.use(prov):
        out = pipeline.call("s", pipeline.SONNET, "hi", system="RULES")
    assert out == "anthropic-reply"
    assert cap["system"] == "RULES" and cap["model"] == pipeline.SONNET


def test_no_bound_provider_raises():
    import pytest
    assert provider.active() is None   # user-key-only: there is no FILG fallback client
    with pytest.raises(RuntimeError):
        pipeline.call("s", pipeline.SONNET, "hi", system="RULES")


# ── BYOK / OpenRouter path ────────────────────────────────────────────────────
def test_openrouter_translates_to_chat_completions(monkeypatch):
    cap = {}
    prov = provider.Provider("openrouter", "openai", _fake_openai(cap),
                             dict(provider.OPENROUTER_MODELS), bills_filg=False)
    with provider.use(prov):
        out = pipeline.call("synth", pipeline.SONNET, "draft this", system="VOICE RULES")
    assert out == "openrouter-reply"
    # system → leading system message; model → OpenRouter slug
    assert cap["messages"][0] == {"role": "system", "content": "VOICE RULES"}
    assert cap["messages"][1]["role"] == "user"
    assert cap["model"] == provider.OPENROUTER_MODELS[pipeline.SONNET]


def test_openrouter_web_search_becomes_web_plugin(monkeypatch):
    cap = {}
    prov = provider.Provider("openrouter", "openai", _fake_openai(cap),
                             dict(provider.OPENROUTER_MODELS))
    with provider.use(prov):
        pipeline.call("research", pipeline.HAIKU, "find facts", tools=[pipeline.WEB_SEARCH_TOOL])
    plugins = cap.get("extra_body", {}).get("plugins")
    assert plugins and plugins[0]["id"] == "web"


def test_byok_run_costs_filg_nothing(monkeypatch):
    # A BYOK model id isn't in PRICES → resolves to $0 against FILG's budget (user pays).
    cap = {}
    prov = provider.Provider("openrouter", "openai", _fake_openai(cap),
                             dict(provider.OPENROUTER_MODELS), bills_filg=False)
    start = len(pipeline.LEDGER.rows)
    with provider.use(prov):
        pipeline.call("synth", pipeline.SONNET, "hi")
    assert pipeline.LEDGER.cost_slice(start) == 0.0


def _fake_openai_with_cost(capture, cost):
    def create(**kwargs):
        capture.update(kwargs)
        usage = types.SimpleNamespace(prompt_tokens=20, completion_tokens=8, cost=cost)
        msg = types.SimpleNamespace(content="openrouter-reply")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    return types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))


def test_openrouter_real_cost_and_tokens(monkeypatch):
    # OpenRouter reports the actual USD cost in usage.cost (we send usage.include=true). The ledger
    # uses that real cost for BYOK $ (not $0), and counts the real tokens for the session meter.
    cap = {}
    prov = provider.Provider("openrouter", "openai", _fake_openai_with_cost(cap, 0.0123),
                             dict(provider.OPENROUTER_MODELS), bills_filg=False)
    start = len(pipeline.LEDGER.rows)
    t0 = pipeline.LEDGER.tokens()
    with provider.use(prov):
        pipeline.call("synth", pipeline.SONNET, "hi")
    assert cap.get("extra_body", {}).get("usage") == {"include": True}     # we asked for the real cost
    assert abs(pipeline.LEDGER.cost_slice(start) - 0.0123) < 1e-9          # real BYOK cost, not $0
    assert pipeline.LEDGER.tokens() - t0 == 28                             # 20 + 8 real tokens counted


# ── contextvar plumbing ───────────────────────────────────────────────────────
def test_bound_rebinds_provider_in_worker_thread():
    from concurrent.futures import ThreadPoolExecutor
    prov = provider.Provider("openrouter", "openai", object(), {})
    with provider.use(prov):
        worker = provider.bound(lambda _: provider.active())
        with ThreadPoolExecutor(max_workers=2) as ex:
            seen = list(ex.map(worker, [1, 2]))
    assert all(p is prov for p in seen)  # provider survived the thread hop


def test_use_resets_to_prior_provider():
    assert provider.active() is None
    with provider.use(provider.Provider("openrouter", "openai", object(), {})):
        assert provider.active() is not None
    assert provider.active() is None  # cleanly reset


def test_anthropic_provider_user_key_supports_opus_and_bills_user(monkeypatch):
    import types, sys
    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=lambda **k: object()))
    p = provider.anthropic_provider("sk-ant-xyz", bills_filg=False)
    assert p.name == "anthropic" and p.kind == "anthropic" and p.bills_filg is False
    assert p.model_id(provider.OPUS) == provider.OPUS        # Opus available on a direct key
    assert p.model_id(provider.SONNET) == provider.SONNET    # identity map (logical id IS anthropic id)
