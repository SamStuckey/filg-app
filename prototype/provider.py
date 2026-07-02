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

import model_catalog  # noqa: E402 — id/price/slug source of truth (one-directional: provider → catalog)

# Logical model ids (canonical; pipeline imports these). Resolved from the catalog so each slot is
# env-repointable (FILG_MODEL_HAIKU/SONNET/OPUS) — defaults are the current live models, so behavior is
# unchanged until a slot is repointed. Read once at import (a repoint is a restart, i.e. a config change).
HAIKU = model_catalog.model_id_for("HAIKU")
SONNET = model_catalog.model_id_for("SONNET")
OPUS = model_catalog.model_id_for("OPUS")

# OpenRouter slugs for the same models — generation behaves identically to the hosted path, only the
# billing key changes. Sourced from the catalog (env-overridable via FILG_OPENROUTER_<ID>).
OPENROUTER_MODELS = {
    HAIKU: model_catalog.openrouter_slug(HAIKU),
    SONNET: model_catalog.openrouter_slug(SONNET),
    OPUS: model_catalog.openrouter_slug(OPUS),
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ── Model stacks ──────────────────────────────────────────────────────────────
# A "stack" is the per-stage model assignment for a run, chosen by the user (a cost↔quality dial). The
# engine has four stages: `plan` (the research planner / orchestrator), `research` (the web_search
# fan-out), `grade` (the source-credibility gate — invariant #1, the moat), and `synth` (everything the
# user reads). Historically ALL ran on Haiku, so the moat + research foundation used the cheapest model
# while only the prose was Sonnet. The ladder is MONOTONIC (slide toward premium → a stage never
# downgrades). `web_search` works on all three Claude tiers; Opus/Sonnet add dynamic result filtering, so
# upgrading research is a quality bonus, not a compatibility risk.
STACKS = {
    "trust-fund-baby":   {"plan": OPUS,   "research": OPUS,   "grade": OPUS,   "synth": OPUS},
    "the-wonder-kid":        {"plan": OPUS,   "research": SONNET, "grade": OPUS,   "synth": OPUS},
    "the-work-horse":    {"plan": SONNET, "research": HAIKU,  "grade": SONNET, "synth": SONNET},
    "the-capable-intern":{"plan": HAIKU,  "research": HAIKU,  "grade": SONNET, "synth": SONNET},
    "the-turd-polisher": {"plan": HAIKU,  "research": HAIKU,  "grade": HAIKU,  "synth": HAIKU},
}
# Cheap → premium order (the slider runs left→right along this), the default, and the BYOK-recommended pick.
STACK_ORDER = ["the-turd-polisher", "the-capable-intern", "the-work-horse", "the-wonder-kid", "trust-fund-baby"]
DEFAULT_STACK = "the-work-horse"      # the best Opus-free tier — safe for FILG's free taste
RECOMMENDED_STACK = "the-wonder-kid"      # best results without the full capital burn (BYOK)

# Back-compat: the first cut shipped 3 differently-named stacks; map them so old session rows resolve.
_ALIASES = {"damn-good": "the-work-horse", "trust-fund": "trust-fund-baby", "polished-turd": "the-turd-polisher"}


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
    name = _ALIASES.get(name, name)
    return name if name in STACKS else DEFAULT_STACK


def uses_opus(name: str | None) -> bool:
    return OPUS in STACKS.get(stack_name(name), {}).values()


def clamp_stack(name: str | None, *, byok: bool) -> str:
    """Any Opus-using stack is BYOK-only — never run Opus on FILG's free key (invariant #3: meter before
    you open the tap). Off BYOK, clamp such a stack down to the best Opus-free tier (the default)."""
    name = stack_name(name)
    if not byok and uses_opus(name):
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


def anthropic_provider(api_key: str | None = None, bills_filg: bool = True) -> Provider:
    """Anthropic direct. No key → FILG's hosted ANTHROPIC_API_KEY (bills_filg=True, the free path).
    A user's OWN Anthropic key → pass bills_filg=False (they pay; all Claude tiers incl. Opus). The
    logical ids ARE the Anthropic ids, so the model map is identity."""
    import anthropic
    cl = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    return Provider("anthropic", "anthropic", cl, {HAIKU: HAIKU, SONNET: SONNET, OPUS: OPUS},
                    bills_filg=bills_filg)


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
    assert active_stack() == DEFAULT_STACK == "the-work-horse" and len(STACK_ORDER) == len(STACKS) == 5
    with use_stack("trust-fund-baby"):
        assert resolve_model("judge", HAIKU) == OPUS and resolve_model("research", HAIKU) == OPUS
    with use_stack("the-wonder-kid"):
        assert resolve_model("judge", HAIKU) == OPUS         # moat + orchestration + synth on Opus
        assert resolve_model("research", HAIKU) == SONNET    # ...but research stays Sonnet (no capital burn)
    with use_stack("the-work-horse"):
        assert resolve_model("judge", HAIKU) == SONNET       # the moat upgraded
        assert resolve_model("research", SONNET) == HAIKU    # bulk reads stay cheap
        assert resolve_model("plan_brief", HAIKU) == SONNET  # section draft = synth role
    with use_stack("the-turd-polisher"):
        assert resolve_model("judge", SONNET) == HAIKU
    assert stack_name("damn-good") == "the-work-horse"       # legacy alias still resolves
    assert clamp_stack("the-wonder-kid", byok=False) == DEFAULT_STACK   # Opus tiers off FILG's free key
    assert clamp_stack("the-wonder-kid", byok=True) == "the-wonder-kid"
    assert not uses_opus("the-work-horse") and uses_opus("the-wonder-kid")
    print("provider.py self-test OK — providers: anthropic, openrouter; stacks:", *STACK_ORDER)
