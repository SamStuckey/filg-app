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
OPUS = "claude-opus-4-8"

# OpenRouter slugs for the same Claude models — generation behaves identically to the hosted
# path, only the billing key changes. (Update if OpenRouter renames the slugs.)
OPENROUTER_MODELS = {
    HAIKU: "anthropic/claude-haiku-4.5",
    SONNET: "anthropic/claude-sonnet-4.6",
    OPUS: "anthropic/claude-opus-4.8",
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ── Model stacks ──────────────────────────────────────────────────────────────
# A "stack" is the per-stage model assignment for a run, chosen by the user. The engine has four
# stages: `plan` (the research planner / orchestrator), `research` (the web_search fan-out), `grade`
# (the source-credibility gate — invariant #1, the moat), and `synth` (everything the user reads).
# Historically ALL of these ran on Haiku, so the moat + research foundation ran on the cheapest model
# while only the prose was Sonnet. The stacks let the user pick the quality/cost trade-off (BYOK = their
# spend, the meter shows it). `web_search` works on all three Claude tiers; Opus/Sonnet add dynamic
# result filtering, so upgrading research is a quality bonus, not a compatibility risk.
STACKS = {
    "trust-fund":    {"plan": OPUS,   "research": OPUS,   "grade": OPUS,   "synth": OPUS},    # all top-of-the-line
    "damn-good":     {"plan": SONNET, "research": HAIKU,  "grade": SONNET, "synth": SONNET},  # Sonnet brains, Haiku legs
    "polished-turd": {"plan": HAIKU,  "research": HAIKU,  "grade": HAIKU,  "synth": HAIKU},   # Haiku only (cheapest)
}
DEFAULT_STACK = "damn-good"
PREMIUM_STACK = "trust-fund"   # not allowed on FILG's free key (see clamp_stack) — BYOK only


def _role(stage: str) -> str:
    """Map a call's stage name to its stack role. Stage names come from pipeline.call()'s first arg."""
    if stage == "judge":
        return "grade"
    if stage == "plan":                 # the research planner exactly; section drafts are "plan_<key>"
        return "plan"
    if stage.startswith("research"):    # research, research2
        return "research"
    return "synth"                      # synth, plan_<section>, teardown_synth, expert_*, intake, board…


def stack_name(name: str | None) -> str:
    return name if name in STACKS else DEFAULT_STACK


def clamp_stack(name: str | None, *, byok: bool) -> str:
    """The premium stack runs Opus on every stage — never on FILG's free key. Off BYOK, clamp it down so
    a free run can't spend Opus money on FILG's dime (invariant #3: meter before you open the tap)."""
    name = stack_name(name)
    if not byok and name == PREMIUM_STACK:
        return DEFAULT_STACK
    return name


_stack: contextvars.ContextVar[str] = contextvars.ContextVar("filg_stack", default=DEFAULT_STACK)


def active_stack() -> str:
    return _stack.get()


@contextlib.contextmanager
def use_stack(name: str | None):
    """Bind the model stack for the duration of the block (and any synchronous calls within it)."""
    token = _stack.set(stack_name(name))
    try:
        yield
    finally:
        _stack.reset(token)


def resolve_model(stage: str, fallback: str) -> str:
    """The logical model the ACTIVE stack assigns to `stage`'s role (fallback if the role is unknown).
    Called by pipeline.call() so each call site keeps passing its own model as a sane default."""
    return STACKS.get(active_stack(), STACKS[DEFAULT_STACK]).get(_role(stage), fallback)


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
    assert p.model_id(OPUS) == "anthropic/claude-opus-4.8"
    # stacks: same call stage resolves to different models per active stack
    assert active_stack() == DEFAULT_STACK
    with use_stack("trust-fund"):
        assert resolve_model("judge", HAIKU) == OPUS and resolve_model("research", HAIKU) == OPUS
    with use_stack("damn-good"):
        assert resolve_model("judge", HAIKU) == SONNET      # the moat upgraded
        assert resolve_model("research", SONNET) == HAIKU   # bulk reads stay cheap
        assert resolve_model("plan_brief", HAIKU) == SONNET  # section draft = synth role
    with use_stack("polished-turd"):
        assert resolve_model("judge", SONNET) == HAIKU
    assert clamp_stack("trust-fund", byok=False) == DEFAULT_STACK   # no Opus on FILG's free key
    assert clamp_stack("trust-fund", byok=True) == "trust-fund"
    print("provider.py self-test OK — providers: anthropic, openrouter; stacks:", *STACKS)
