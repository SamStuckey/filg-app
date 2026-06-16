#!/usr/bin/env python3
"""
Per-run LLM provider selection — the BYOK seam.

The engine was hardcoded to Anthropic: a module-global `anthropic.Anthropic()`, the
Anthropic message format, and Anthropic's `web_search` server tool. BYOK needs the
SAME pipeline to run on a *user's* key. This module is the thin seam that makes that
possible without rewriting every stage.

Two ideas:
  - The DEFAULT path is unchanged Anthropic (FILG's key). When no provider is active,
    `pipeline.call()` behaves exactly as before, so the free "taste" run and every
    existing test are untouched.
  - A BYOK run sets an active `Provider` for its duration (a contextvar). `pipeline.call()`
    reads it and routes to the right SDK + message format + web-search mechanism. Because
    the research fan-out runs in worker threads (which do NOT inherit contextvars), wrap
    each worker with `bound()` so it re-binds the active provider.

v1 ships two providers:
  - anthropic : the hosted/free path (FILG's key) — native `web_search_20260209`.
  - openrouter: a user's OpenRouter key — OpenAI-compatible Chat Completions at
    https://openrouter.ai/api/v1, models mapped to Claude (so the skills behave
    identically), web search via OpenRouter's provider-agnostic web plugin.

Why this preserves the moat: the source-credibility gate grades the (claim, source_url)
pairs the research stage returns — it does not care which backend did the searching. So a
provider-agnostic search (OpenRouter's, Exa-backed, returns cited URLs) keeps the gate
working on any underlying model.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
from dataclasses import dataclass, field

# Logical model ids (canonical, lives here so pipeline and provider agree without a cycle).
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"

# OpenRouter slugs for the same Claude models — generation behaves identically to the hosted
# path, only the billing key changes. (Update if OpenRouter renames the slugs.)
OPENROUTER_MODELS = {
    HAIKU: "anthropic/claude-haiku-4.5",
    SONNET: "anthropic/claude-sonnet-4.6",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class Provider:
    """A bound LLM backend for one run. `kind` selects the wire format pipeline.call() speaks."""
    name: str                      # "anthropic" | "openrouter"  (for logs / cost attribution)
    kind: str                      # "anthropic" | "openai"      (request/response shape)
    client: object                 # an anthropic.Anthropic or openai.OpenAI instance
    models: dict = field(default_factory=dict)   # logical id -> provider id
    bills_filg: bool = True        # True = FILG pays (hosted key) → counts against the daily budget

    def model_id(self, logical: str) -> str:
        return self.models.get(logical, logical)


def anthropic_provider(api_key: str | None = None) -> Provider:
    """The hosted/free path. With no key, builds from ANTHROPIC_API_KEY (current behavior)."""
    import anthropic
    cl = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    return Provider("anthropic", "anthropic", cl, {HAIKU: HAIKU, SONNET: SONNET}, bills_filg=True)


def openrouter_provider(api_key: str) -> Provider:
    """A user's OpenRouter key. OpenAI-compatible client, Claude models, user pays (bills_filg=False)."""
    from openai import OpenAI
    cl = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)
    return Provider("openrouter", "openai", cl, dict(OPENROUTER_MODELS), bills_filg=False)


# ── Active-provider contextvar ────────────────────────────────────────────────
_active: contextvars.ContextVar[Provider | None] = contextvars.ContextVar("filg_provider", default=None)


def active() -> Provider | None:
    """The provider bound to this run, or None → pipeline uses the legacy Anthropic globals."""
    return _active.get()


@contextlib.contextmanager
def use(provider: Provider | None):
    """Bind `provider` for the duration of the block (and any synchronous calls within it)."""
    token = _active.set(provider)
    try:
        yield provider
    finally:
        _active.reset(token)


def bound(fn):
    """Wrap a worker so it re-binds the CURRENTLY active provider inside its own thread.

    Worker threads (ThreadPoolExecutor in the research fan-out) do not inherit contextvars, so a
    BYOK run's provider would be lost in the fan-out without this. Capture now, re-bind in the thread.
    """
    provider = active()

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        with use(provider):
            return fn(*args, **kwargs)

    return inner


if __name__ == "__main__":  # self-test (no API, no network)
    assert active() is None
    p = Provider("openrouter", "openai", object(), dict(OPENROUTER_MODELS), bills_filg=False)
    assert p.model_id(SONNET) == "anthropic/claude-sonnet-4.6"
    assert p.model_id("unknown-model") == "unknown-model"  # passthrough
    with use(p):
        assert active() is p
        captured = bound(lambda: active())   # capture inside the bound scope
    assert active() is None                  # use() reset cleanly
    assert captured() is p                   # bound re-binds even outside the original scope
    print("provider.py self-test OK — providers: anthropic, openrouter")
