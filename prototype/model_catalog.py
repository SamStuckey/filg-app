#!/usr/bin/env python3
"""
Model catalog — one source of truth for model ids, prices, and OpenRouter slugs, plus a *cached*
availability check against Anthropic's Models API.

The problem this solves: the engine used to hard-code the model strings, the per-MTok prices, and the
OpenRouter slugs in three separate places (provider.py x2, pipeline.py), so every new Claude release
(Sonnet 5, Fable 5, …) meant editing three constants and redeploying. This module makes all three
data-driven and **env-overridable**, and adds a cached Models API lookup so we can (a) point a logical
slot at a newer model without a code change and (b) get a signal when Anthropic ships a model we don't
yet catalog.

What the Models API gives us vs. not:
  - AVAILABILITY (which ids your key can see): YES → that's what `available_ids()` caches.
  - PRICE (per-MTok in/out): NO → curated here, `FILG_PRICE_<ID>` override.
  - RELATIVE VALUE / rung (cost↔quality placement): NO → curated `rung`, a human judgment.

So the split is: discovery/availability is dynamic (API, cached); placement + price stay curated but are
env-overridable, so repointing a slot at a brand-new model is a Render-dashboard change, not a deploy.

No import of provider/pipeline (they import THIS) — the dependency stays one-directional.

Env overrides (all optional):
  FILG_MODEL_HAIKU / FILG_MODEL_SONNET / FILG_MODEL_OPUS   repoint a logical slot at a model id
  FILG_PRICE_<SANITIZED_ID>       "in,out" per-MTok, e.g. FILG_PRICE_CLAUDE_FABLE_5="10,50"
  FILG_OPENROUTER_<SANITIZED_ID>  the OpenRouter slug for an id
  FILG_MODELS_TTL                 seconds to cache the availability lookup (default 3600)
(<SANITIZED_ID> = the model id upper-cased with every non-alphanumeric char turned into "_".)
"""

from __future__ import annotations

import os
import time

# ── Curated catalog ───────────────────────────────────────────────────────────
# id -> {display, rung, price (in,out per 1M USD), openrouter}. `rung` is the cost↔quality placement
# (cheap→premium: fast < mid < high < frontier) — the one judgment the API can't make for us. Prices
# per Anthropic's published rates. Sonnet 5 / Fable 5 are catalogued (priced + placed) so they're ready
# to offer via an env repoint or a future stack, without a code edit.
CATALOG: dict[str, dict] = {
    "claude-haiku-4-5":  {"display": "Haiku 4.5",  "rung": "fast",     "price": (1.0, 5.0),   "openrouter": "anthropic/claude-haiku-4.5"},
    "claude-sonnet-4-6": {"display": "Sonnet 4.6", "rung": "mid",      "price": (3.0, 15.0),  "openrouter": "anthropic/claude-sonnet-4.6"},
    "claude-sonnet-5":   {"display": "Sonnet 5",   "rung": "mid",      "price": (3.0, 15.0),  "openrouter": "anthropic/claude-sonnet-5"},
    "claude-opus-4-8":   {"display": "Opus 4.8",   "rung": "high",     "price": (5.0, 25.0),  "openrouter": "anthropic/claude-opus-4.8"},
    "claude-fable-5":    {"display": "Fable 5",    "rung": "frontier", "price": (10.0, 50.0), "openrouter": "anthropic/claude-fable-5"},
}

# Rungs cheap → premium (used for ordering / placement of new ids).
RUNG_ORDER = ["fast", "mid", "high", "frontier"]

# Logical slots the engine's stacks reference (provider.HAIKU/SONNET/OPUS). Each resolves to a concrete
# model id: an env override wins, else the catalog default below. Defaults are the current live models,
# so behavior is unchanged until someone repoints a slot.
_SLOT_DEFAULTS = {"HAIKU": "claude-haiku-4-5", "SONNET": "claude-sonnet-4-6", "OPUS": "claude-opus-4-8"}


def _sanitize(model_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (model_id or "")).upper()


def model_id_for(slot: str) -> str:
    """Resolve a logical slot (HAIKU/SONNET/OPUS) to a concrete model id — env override, else default."""
    slot = slot.upper()
    return os.environ.get(f"FILG_MODEL_{slot}") or _SLOT_DEFAULTS.get(slot, slot)


def price(model_id: str) -> tuple[float, float]:
    """Per-MTok (input, output) USD for a model id: env override → catalog → (0.0, 0.0).
    The $0 fallback is deliberate — an unknown id (e.g. an OpenRouter slug on a BYOK run) resolves to
    $0 on FILG's ledger, keeping BYOK spend off FILG's budget (the caller reports the user's real cost)."""
    env = os.environ.get(f"FILG_PRICE_{_sanitize(model_id)}")
    if env:
        try:
            pin, pout = (float(x) for x in env.split(",", 1))
            return (pin, pout)
        except (ValueError, TypeError):
            pass
    entry = CATALOG.get(model_id)
    if entry and entry.get("price"):
        return tuple(entry["price"])  # type: ignore[return-value]
    return (0.0, 0.0)


def openrouter_slug(model_id: str) -> str:
    """The OpenRouter slug for a model id: env override → catalog → a best-effort derivation
    (`anthropic/<id with the last "-N-M" turned into ".N.M">`). The derivation is a fallback for a
    brand-new id not yet catalogued; set FILG_OPENROUTER_<ID> if OpenRouter names it differently."""
    env = os.environ.get(f"FILG_OPENROUTER_{_sanitize(model_id)}")
    if env:
        return env
    entry = CATALOG.get(model_id)
    if entry and entry.get("openrouter"):
        return entry["openrouter"]
    # derive: claude-opus-4-8 -> anthropic/claude-opus-4.8 (version dashes → dots, best effort)
    import re
    slug = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", model_id)
    return f"anthropic/{slug}"


def rung(model_id: str) -> str:
    return CATALOG.get(model_id, {}).get("rung", "")


def display(model_id: str) -> str:
    return CATALOG.get(model_id, {}).get("display", model_id)


# ── Cached availability (Anthropic Models API) ────────────────────────────────
_avail_cache: dict = {"ids": None, "ts": 0.0, "error": None}


def _ttl() -> int:
    try:
        return int(os.environ.get("FILG_MODELS_TTL", "3600"))
    except ValueError:
        return 3600


def available_ids(*, force: bool = False, client=None) -> set[str]:
    """The set of model ids the (hosted) Anthropic key can see, cached for FILG_MODELS_TTL seconds so
    we don't hit the API on the hot path. Fail-open: on any error (no key, network) it returns the last
    good cache, else the catalog keys — availability is a *signal*, never a gate on real runs."""
    now = time.time()
    if not force and _avail_cache["ids"] is not None and (now - _avail_cache["ts"]) < _ttl():
        return _avail_cache["ids"]
    try:
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        ids = {m.id for m in client.models.list()}
        _avail_cache.update(ids=ids, ts=now, error=None)
        return ids
    except Exception as e:  # noqa: BLE001 — never let a discovery lookup break a request
        _avail_cache["ts"] = now                       # back off for the TTL even on failure
        _avail_cache["error"] = f"{type(e).__name__}: {e}"[:200]
        return _avail_cache["ids"] if _avail_cache["ids"] is not None else set(CATALOG)


def uncatalogued(*, client=None) -> list[str]:
    """Model ids the API reports that we don't yet catalog — the 'Anthropic shipped a new model, go
    place + price it' signal. Empty if the availability lookup failed (nothing to compare against)."""
    ids = available_ids(client=client)
    if not ids or ids == set(CATALOG):
        return sorted(i for i in ids if i not in CATALOG)
    return sorted(i for i in ids if i not in CATALOG)


def snapshot(*, client=None, check_availability: bool = False) -> dict:
    """A view of the catalog for the health/admin surface: each id with rung/price/slug and (optionally)
    whether the live key can see it, plus the current logical-slot resolution and any uncatalogued ids."""
    avail = available_ids(client=client) if check_availability else None
    models = []
    for mid, e in sorted(CATALOG.items(), key=lambda kv: (RUNG_ORDER.index(kv[1]["rung"])
                                                           if kv[1]["rung"] in RUNG_ORDER else 99, kv[0])):
        pin, pout = price(mid)
        row = {"id": mid, "display": e["display"], "rung": e["rung"],
               "price_in": pin, "price_out": pout, "openrouter": openrouter_slug(mid)}
        if avail is not None:
            row["available"] = mid in avail
        models.append(row)
    out = {"models": models,
           "slots": {s: model_id_for(s) for s in _SLOT_DEFAULTS},
           "rungs": RUNG_ORDER}
    if avail is not None:
        out["uncatalogued"] = sorted(i for i in avail if i not in CATALOG)
        out["availability_error"] = _avail_cache.get("error")
    return out


if __name__ == "__main__":  # self-test (no network — availability check is not exercised here)
    assert model_id_for("HAIKU") == "claude-haiku-4-5"
    assert model_id_for("OPUS") == "claude-opus-4-8"
    assert price("claude-opus-4-8") == (5.0, 25.0)
    assert price("claude-fable-5") == (10.0, 50.0)
    assert price("some-unknown-model") == (0.0, 0.0)          # BYOK/unknown → $0 on FILG's ledger
    assert openrouter_slug("claude-sonnet-4-6") == "anthropic/claude-sonnet-4.6"
    assert openrouter_slug("claude-opus-4-8") == "anthropic/claude-opus-4.8"
    assert openrouter_slug("claude-fable-9-1") == "anthropic/claude-fable-9.1"   # derivation fallback
    # env overrides
    os.environ["FILG_MODEL_OPUS"] = "claude-fable-5"
    os.environ["FILG_PRICE_CLAUDE_FANCY_9"] = "12,60"
    os.environ["FILG_OPENROUTER_CLAUDE_FANCY_9"] = "anthropic/claude-fancy-9"
    assert model_id_for("OPUS") == "claude-fable-5"            # slot repointed via env, no code change
    assert price("claude-fancy-9") == (12.0, 60.0) and openrouter_slug("claude-fancy-9") == "anthropic/claude-fancy-9"
    del os.environ["FILG_MODEL_OPUS"]; del os.environ["FILG_PRICE_CLAUDE_FANCY_9"]; del os.environ["FILG_OPENROUTER_CLAUDE_FANCY_9"]
    # availability fail-open (no key / no network) → catalog keys, never raises
    ids = available_ids(force=True)
    assert isinstance(ids, set) and "claude-opus-4-8" in ids
    snap = snapshot()
    assert snap["slots"]["OPUS"] == "claude-opus-4-8" and len(snap["models"]) == len(CATALOG)
    print("model_catalog.py self-test OK — catalog:", *[m["id"] for m in snap["models"]])
