"""Dynamic model catalog — env-overridable ids/prices/slugs + the /api/models surface.

The point: a new or repointed model shouldn't need a code edit in three places. Prices flow from the
catalog into the cost ledger live (via _row_cost), so an env price override takes effect without a
deploy, and unknown ids resolve to $0 (BYOK stays off FILG's budget).
"""

from engine import model_catalog
from engine import pipeline


def test_resolution_and_fallbacks():
    assert model_catalog.model_id_for("OPUS") == "claude-opus-4-8"
    assert model_catalog.price("claude-opus-4-8") == (5.0, 25.0)
    assert model_catalog.price("claude-fable-5") == (10.0, 50.0)      # frontier catalogued + priced
    assert model_catalog.price("mystery-model") == (0.0, 0.0)          # unknown → $0 on FILG's ledger
    assert model_catalog.openrouter_slug("claude-opus-4-8") == "anthropic/claude-opus-4.8"
    assert model_catalog.openrouter_slug("claude-fable-7-2") == "anthropic/claude-fable-7.2"  # derived


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("FILG_MODEL_OPUS", "claude-fable-5")
    monkeypatch.setenv("FILG_PRICE_CLAUDE_NEWTHING_1", "12,60")
    monkeypatch.setenv("FILG_OPENROUTER_CLAUDE_NEWTHING_1", "anthropic/claude-newthing-1")
    assert model_catalog.model_id_for("OPUS") == "claude-fable-5"      # slot repointed, no code change
    assert model_catalog.price("claude-newthing-1") == (12.0, 60.0)
    assert model_catalog.openrouter_slug("claude-newthing-1") == "anthropic/claude-newthing-1"


def test_ledger_prices_from_catalog(monkeypatch):
    # 1M in + 1M out on Opus → $5 + $25 = $30 (+0 searches), sourced live from the catalog
    cost = pipeline.Ledger._row_cost("claude-opus-4-8", 1_000_000, 1_000_000, 0, None)
    assert round(cost, 2) == 30.0
    # a real (provider-reported) cost wins over the price table
    assert pipeline.Ledger._row_cost("claude-opus-4-8", 1_000_000, 1_000_000, 0, 1.23) == 1.23
    # an env price override flows straight into the ledger — no deploy needed
    monkeypatch.setenv("FILG_PRICE_CLAUDE_OPUS_4_8", "1,1")
    assert round(pipeline.Ledger._row_cost("claude-opus-4-8", 1_000_000, 1_000_000, 0, None), 2) == 2.0
    # unknown id → $0 (BYOK / OpenRouter slug never bills FILG)
    assert pipeline.Ledger._row_cost("anthropic/claude-opus-4.8", 1_000_000, 1_000_000, 0, None) == 0.0


def test_availability_fail_open():
    # no key / no network in tests → never raises, returns a set incl. the catalog
    ids = model_catalog.available_ids(force=True)
    assert isinstance(ids, set) and "claude-opus-4-8" in ids


def test_api_models_route(client):
    d = client.get("/api/models").json()
    assert len(d["models"]) == len(model_catalog.CATALOG)
    assert d["slots"]["OPUS"] == "claude-opus-4-8"
    assert d["rungs"] == ["fast", "mid", "high", "frontier"]
    ids = {m["id"] for m in d["models"]}
    assert {"claude-sonnet-5", "claude-fable-5"} <= ids          # newer models catalogued + ready
