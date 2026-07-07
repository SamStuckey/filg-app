#!/usr/bin/env python3
"""
Thin unattended Idea → Offer pipeline (live).

This is the real version of the stage chain the dogfood runs did by hand:
    Haiku research fan-out  →  Sonnet synthesis  →  source-credibility gate
running with NO human steering. It exists to measure the one number Test #1 left
open (see ../test_01_results.md): POST-GATE survival quality — after the gate
flags self-interested claims and forces a re-search, how many flagged claims land
on a primary/neutral cite? — plus the MEASURED $/run vs the $0.21 model estimate.

Design (deliberately thin):
  1. PLAN     (Haiku)            : decompose the plain-text idea into 3 research lanes.
  2. RESEARCH (Haiku ×3, parallel): each lane runs real web_search and returns claims
                                    with the source URL it actually used.
  3. SYNTH    (Sonnet)          : one call over the research → the full artifact set
                                    (brief/offer/pricing/GTM/delivery/roadmap).
  4. GATE     (heuristic + Haiku judge): classify every quantitative claim; flag the
                                    self-interested / non-primary ones.
  5. RE-SEARCH(Haiku + web_search): for each flagged claim, try to find a primary or
                                    neutral source; re-judge → survival.

Every API call's token usage is metered against published per-MTok prices, so the
run prints a real $/run to compare against the $0.21 lean-routing estimate.

Run:  python3 pipeline.py            # uses the built-in fresh prompt
      python3 pipeline.py "your idea in plain text"
Needs ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlparse

import anthropic

import model_catalog
import provider
from provider import HAIKU, SONNET, OPUS  # canonical model ids (defined in provider to avoid a cycle)

from source_credibility_gate import (
    classify_domain,
    TIER_PRIMARY, TIER_RESEARCH, TIER_VENDOR, TIER_FORUM, TIER_UNKNOWN,
)

# --- Prices: USD per 1M tokens (input, output) -------------------------------
# Sourced from model_catalog (env-overridable) so a new/repointed model doesn't need a code edit here.
# BYOK runs use a user's key (provider.bills_filg=False), so their tokens are the user's spend — an
# unknown model id resolves to $0 (model_catalog.price fallback), keeping BYOK off FILG's kill switch.
# Kept as a dict for back-compat; `_row_cost` consults the catalog directly so ANY resolved id prices.
PRICES = {m: model_catalog.price(m) for m in (HAIKU, SONNET, OPUS)}
WEB_SEARCH_PRICE = 10.0 / 1000  # $10 per 1k searches (Anthropic server tool)
OPENROUTER_WEB_MAX = 4          # results per request for OpenRouter's web plugin

# web_search defaults to programmatic calling, which Haiku can't do — pin to direct.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search",
                   "max_uses": 4, "allowed_callers": ["direct"]}
# The dynamic-filtering `web_search_20260209` variant requires Opus 4.6+/Sonnet 4.6 — it is NOT
# supported on Haiku 4.5 (the default stack's research model), where it 400s. Haiku must use the
# basic `web_search_20250305` variant. `_web_tools_for_model` swaps per RESOLVED model so a real
# Anthropic key on the default "The closer" stack still returns cited results from the fan-out.
WEB_SEARCH_TOOL_BASIC = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}

# FILG is user-key-only: there is NO hosted FILG client. Every real call runs on a provider bound
# from the user's BYOK key (provider.use(...) on the run thread + pipeline.bound() into fan-outs).


# --- Cost ledger -------------------------------------------------------------
@dataclass
class Ledger:
    rows: list = field(default_factory=list)  # (stage, model, tin, tout, searches)

    def add(self, stage: str, model: str, usage) -> None:
        searches = getattr(getattr(usage, "server_tool_use", None),
                           "web_search_requests", 0) or 0
        real = getattr(usage, "cost", None)   # OpenRouter reports the actual USD cost; Anthropic → None
        self.rows.append((stage, model, usage.input_tokens, usage.output_tokens, searches, real))

    @staticmethod
    def _row_cost(model, tin, tout, searches, real) -> float:
        """Real provider-reported cost when we have it (OpenRouter/BYOK), else FILG's price table
        (Anthropic). An unknown model with no reported cost resolves to $0."""
        if real is not None:
            return float(real)
        pin, pout = model_catalog.price(model)   # catalog + env overrides; unknown id → (0,0)
        return tin / 1e6 * pin + tout / 1e6 * pout + searches * WEB_SEARCH_PRICE

    def cost(self) -> float:
        return self.cost_slice(0)

    def cost_slice(self, start: int) -> float:
        """Cost of rows added since index `start` — lets the server meter one run even though
        the ledger is process-global. (Production: use a per-request ledger.)"""
        return sum(self._row_cost(m, tin, tout, s, real)
                   for _stg, m, tin, tout, s, real in self.rows[start:])

    def breakdown(self) -> dict:
        agg: dict[str, float] = {}
        for stage, model, tin, tout, searches, real in self.rows:
            agg[stage] = agg.get(stage, 0.0) + self._row_cost(model, tin, tout, searches, real)
        return agg

    def searches(self) -> int:
        return sum(r[4] for r in self.rows)

    def tokens(self) -> int:
        """Total input+output tokens across all rows (for the live session usage meter)."""
        return sum(r[2] + r[3] for r in self.rows)


# Per-run ledger: a fresh Ledger is bound per request so concurrent operations don't interleave
# their rows (cost_slice would otherwise mis-bill one run with another's tokens). Outside a bound
# run (tests, standalone, teardown) calls fall back to the process-global ledger.
_GLOBAL_LEDGER = Ledger()
_ledger_var: contextvars.ContextVar[Ledger | None] = contextvars.ContextVar("filg_ledger", default=None)


def _active_ledger() -> Ledger:
    return _ledger_var.get() or _GLOBAL_LEDGER


@contextlib.contextmanager
def run_ledger():
    """Bind a fresh per-run ledger for the duration of one request, so its cost is isolated from any
    concurrent run. `bound()` carries it into the research fan-out's worker threads."""
    token = _ledger_var.set(Ledger())
    try:
        yield _ledger_var.get()
    finally:
        _ledger_var.reset(token)


class _LedgerProxy:
    """Delegates to the active per-run ledger (or the global one). Keeps `from pipeline import LEDGER`
    working everywhere while making the ledger per-run under concurrency."""
    @property
    def rows(self):
        return _active_ledger().rows

    def add(self, *a, **k):
        return _active_ledger().add(*a, **k)

    def cost(self):
        return _active_ledger().cost()

    def cost_slice(self, start):
        return _active_ledger().cost_slice(start)

    def breakdown(self):
        return _active_ledger().breakdown()

    def searches(self):
        return _active_ledger().searches()

    def tokens(self):
        return _active_ledger().tokens()


LEDGER = _LedgerProxy()


def bound(fn):
    """Wrap a fan-out worker so it re-binds BOTH the active provider and the per-run ledger inside its
    own thread (ThreadPoolExecutor workers don't inherit contextvars). Captured at submit time."""
    prov = provider.active()
    stk = provider.active_stack()   # carry the model stack into the fan-out too (threads don't inherit it)
    led = _ledger_var.get()

    @functools.wraps(fn)
    def inner(*args, **kwargs):
        with provider.use(prov), provider.use_stack(stk):
            token = _ledger_var.set(led)
            try:
                return fn(*args, **kwargs)
            finally:
                _ledger_var.reset(token)

    return inner


# --- Low-level call with server-tool resume ----------------------------------
def call(stage: str, model: str, prompt: str, *, max_tokens: int = 1500,
         tools: list | None = None, system: str | None = None, cache: bool = False) -> str:
    """One logical turn. Returns text.

    Routes to the active provider (provider.active()): the default/None path is the unchanged
    Anthropic SDK (FILG's hosted key); a BYOK run binds an OpenRouter provider and we speak the
    OpenAI-compatible Chat Completions format instead. The (claim, source_url) contract the gate
    grades is identical either way.

    `system` is the stable instruction block (a skill body). Pass `cache=True` to mark it
    cache-eligible (prompt caching): the system prefix is identical across every run of a stage,
    so caching it reads at ~0.1× input price after the first call — the cheapest token win we have.
    """
    model = provider.resolve_model(stage, model)   # the active model stack picks the model for this stage
    prov = provider.active()
    if prov is not None and prov.kind == "openai":
        return _call_openai(prov, stage, model, prompt, max_tokens=max_tokens,
                            tools=tools, system=system)
    return _call_anthropic(prov, stage, model, prompt, max_tokens=max_tokens,
                           tools=tools, system=system, cache=cache)


def _call_anthropic(prov, stage: str, model: str, prompt: str, *, max_tokens: int,
                    tools: list | None, system: str | None, cache: bool) -> str:
    """The Anthropic path (Messages API). FILG is user-key-only — there is no FILG fallback key, so a
    real call MUST have a provider bound. A None here means a fan-out worker lost the provider
    contextvar (it needs pipeline.bound()); fail loudly rather than silently using an unfunded key."""
    if prov is None:
        raise RuntimeError("No API key bound for this call. Every run uses the user's own key — "
                           "fan-out workers must be wrapped with pipeline.bound().")
    cl = prov.client
    model = prov.model_id(model)
    if tools:
        tools = _web_tools_for_model(tools, model)   # Haiku needs the basic web_search variant
    messages = [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    for _ in range(6):  # cap resume hops
        kwargs = dict(model=model, max_tokens=max_tokens, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if system:
            kwargs["system"] = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if cache else system)
        resp = cl.messages.create(**kwargs)
        LEDGER.add(stage, model, resp.usage)
        text_parts.extend(b.text for b in resp.content if b.type == "text")
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return "\n".join(p for p in text_parts if p).strip()


def _is_web_search_tool(t) -> bool:
    return isinstance(t, dict) and str(t.get("type", "")).startswith("web_search")


def _web_tools_for_model(tools: list, model: str) -> list:
    """Match the web_search tool VERSION to the resolved Anthropic model. The dynamic-filtering
    `web_search_20260209` needs Opus 4.6+/Sonnet 4.6; on Haiku 4.5 it 400s, so swap it for the basic
    `web_search_20250305`. Non-Haiku models keep the richer variant. No-op when there's no web tool."""
    if HAIKU not in (model or ""):
        return tools
    return [dict(WEB_SEARCH_TOOL_BASIC)
            if (_is_web_search_tool(t) and "20260209" in str(t.get("type", ""))) else t
            for t in tools]


def _call_openai(prov, stage: str, model: str, prompt: str, *, max_tokens: int,
                 tools: list | None, system: str | None) -> str:
    """The OpenAI-compatible path (OpenRouter). Translates the Anthropic-shaped call:

    - `system` becomes a leading system message.
    - an Anthropic `web_search` tool in `tools` becomes OpenRouter's provider-agnostic web plugin
      (Exa-backed, returns cited URLs the model folds into its JSON). Prompt caching is implicit on
      OpenRouter, so there's no explicit cache flag.

    NOTE: validate the web-plugin wire format against OpenRouter live before trusting the live BYOK
    path — their docs were not fetchable when this was written; this follows the documented shape.
    """
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    kwargs = dict(model=prov.model_id(model), max_tokens=max_tokens, messages=msgs)
    # usage.include → OpenRouter returns the real USD cost in resp.usage.cost (so BYOK $ is accurate,
    # not a price-table guess). Plugins (web search) merge into the same extra_body.
    extra_body: dict = {"usage": {"include": True}}
    if tools and any(_is_web_search_tool(t) for t in tools):
        # OpenRouter web plugin: real-time, cited search for any model. The model still emits the
        # source_url JSON our research prompts ask for; the gate grades those URLs unchanged.
        extra_body["plugins"] = [{"id": "web", "max_results": OPENROUTER_WEB_MAX}]
    kwargs["extra_body"] = extra_body
    resp = prov.client.chat.completions.create(**kwargs)
    LEDGER.add(stage, prov.model_id(model), _openai_usage(resp))
    choice = resp.choices[0] if resp.choices else None
    text = (getattr(getattr(choice, "message", None), "content", None) or "") if choice else ""
    return text.strip()


def _openai_usage(resp):
    """Adapt an OpenAI/OpenRouter usage object to what Ledger.add expects (input/output tokens +
    server_tool_use.web_search_requests + the real USD `cost`). OpenRouter returns the actual cost in
    `usage.cost` when we send usage.include=true; we read it (attr or pydantic model_extra) so the BYOK
    $ is real, not a price-table guess. None → fall back to the price table."""
    u = getattr(resp, "usage", None)
    cost = None
    if u is not None:
        cost = getattr(u, "cost", None)
        if cost is None:
            extra = getattr(u, "model_extra", None) or {}
            cost = extra.get("cost") if isinstance(extra, dict) else None
    return SimpleNamespace(
        input_tokens=getattr(u, "prompt_tokens", 0) or 0,
        output_tokens=getattr(u, "completion_tokens", 0) or 0,
        server_tool_use=None,
        cost=cost,
    )


def judge(c: "Claim") -> str:
    """Metered Haiku source-credibility judge (the gate's --judge path, but on our
    ledger so its tokens land in $/run). Returns TRUST / CROSS_CHECK / FLAG_SELF_INTERESTED."""
    prompt = (
        "You are a source-credibility auditor for a research tool. Given a claim and its "
        "source URL, judge whether the claim should be trusted as fact or flagged as a "
        "self-interested marketing claim.\n\n"
        f"CLAIM: {c.text}\nSOURCE: {c.source_url}\n\n"
        "Reply with exactly one token: TRUST, CROSS_CHECK, or FLAG_SELF_INTERESTED. "
        "Use FLAG_SELF_INTERESTED when the source is a company that profits if a reader "
        "believes the number."
    )
    raw = (call("judge", HAIKU, prompt, max_tokens=24) or "").upper()
    return _normalize_verdict(raw)


def _normalize_verdict(raw: str) -> str:
    # The model doesn't always obey "one token" — normalize to the verdict it named.
    raw = (raw or "").upper()
    for tok in ("FLAG_SELF_INTERESTED", "CROSS_CHECK", "TRUST"):
        if tok in raw:
            return tok
    return "?"


def judge_batch(claims: list["Claim"]) -> list[str]:
    """Judge every claim's source credibility in ONE Haiku call instead of one call per claim.
    A 14-claim run drops from 14 judge calls to 1 — the gate's biggest avoidable token + latency
    cost. Returns verdicts aligned to `claims`; falls back to per-claim judging if the batch reply
    can't be parsed (so a malformed batch never silently mis-grades)."""
    if not claims:
        return []
    listing = "\n".join(f'{i + 1}. CLAIM: {c.text}\n   SOURCE: {c.source_url}'
                        for i, c in enumerate(claims))
    out = call("judge", HAIKU, max_tokens=40 + 12 * len(claims), prompt=(
        "You are a source-credibility auditor. For EACH numbered claim below, judge whether it "
        "should be trusted as fact or flagged as a self-interested marketing claim. Use "
        "FLAG_SELF_INTERESTED when the source is a company that profits if a reader believes the "
        "number; TRUST for primary/neutral sources (government, official stats, standards bodies, "
        "independent research); CROSS_CHECK otherwise.\n\n"
        f"{listing}\n\n"
        'Reply ONLY with a JSON array, same order: [{"i": 1, "verdict": "TRUST"}, ...]. '
        "verdict ∈ {TRUST, CROSS_CHECK, FLAG_SELF_INTERESTED}."))
    data = extract_json(out)
    if isinstance(data, list) and len(data) == len(claims):
        by_i = {}
        for item in data:
            if isinstance(item, dict) and "i" in item:
                by_i[int(item["i"])] = _normalize_verdict(str(item.get("verdict", "")))
        if len(by_i) == len(claims):
            return [by_i.get(i + 1, "?") for i in range(len(claims))]
    return [judge(c) for c in claims]  # fallback: never silently mis-grade


# The gate's one model judgment — "is this source self-interested about this number?" — is the moat's
# load-bearing call, so we VOTE it. Default-to-flag framing: ambiguity (a non-TRUST vote) routes to the
# safe side. Three batch calls (not 3× per claim) keep the cost bounded on the cheap grade model, and
# the grade model outranks the research model on every stack but the cheapest, giving writer≠critic.
JUDGE_VOTES = 3


def judge_batch_voted(claims: list["Claim"], votes: int = JUDGE_VOTES) -> list[str]:
    """Run judge_batch `votes` times (fresh calls) and resolve each claim by majority, with
    default-to-flag on a tie or any unresolved ('?') majority. Returns verdicts aligned to `claims`.
    One sample is not reproducible; a small vote is close enough and is the moat's reliability win."""
    if not claims:
        return []
    if votes <= 1:
        return judge_batch(claims)
    # The votes are independent full-set judge calls — run them concurrently instead of serially.
    # bound() re-binds the provider/stack/ledger inside each worker (threads don't inherit contextvars),
    # so a BYOK run still votes on the user's key; ex.map preserves order (the tally is order-independent
    # anyway). Result is identical to the serial [judge_batch(claims) for _ in range(votes)], ~votes× faster.
    with ThreadPoolExecutor(max_workers=votes) as ex:
        rounds = list(ex.map(bound(lambda _i: judge_batch(claims)), range(votes)))
    out: list[str] = []
    for i in range(len(claims)):
        tally: dict[str, int] = {}
        for r in rounds:
            tally[r[i]] = tally.get(r[i], 0) + 1
        top = max(tally.values())
        winners = {v for v, n in tally.items() if n == top}
        if len(winners) == 1 and "?" not in winners:
            out.append(next(iter(winners)))
        elif "FLAG_SELF_INTERESTED" in tally:   # tie/uncertain → take the safe (flag) side if any voter flagged
            out.append("FLAG_SELF_INTERESTED")
        elif "CROSS_CHECK" in tally:            # else prefer the cautious non-trust verdict
            out.append("CROSS_CHECK")
        else:
            out.append(_normalize_verdict(next(iter(winners))))
    return out


def extract_json(text: str):
    """Pull the first JSON object/array out of a model response (handles fences). Picks whichever
    delimiter OPENS FIRST, so an object that contains an array ({"a":[...]}) parses as the object —
    not the inner array. (Parsing the inner array was a real bug: callers got a list and `.get` blew
    up.)"""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    openers = sorted(((candidate.find(o), o, c) for o, c in (("{", "}"), ("[", "]"))
                      if candidate.find(o) != -1))
    for i, opener, closer in openers:  # earliest opener first = outermost value
        j = candidate.rfind(closer)
        if j > i:
            try:
                return json.loads(candidate[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


# --- Stage 1: PLAN -----------------------------------------------------------
def plan(idea: str) -> list[str]:
    out = call("plan", HAIKU, max_tokens=400, prompt=(
        "You are the research planner for an Idea→Offer engine. Given a plain-text "
        "business idea from a solo operator, output the 3 most decision-relevant "
        "research lanes to investigate (e.g. market size, competitor/pricing norms, "
        "buyer pain). Each lane is one specific researchable question.\n\n"
        f"IDEA:\n{idea}\n\n"
        'Reply ONLY with JSON: {"lanes": ["question 1", "question 2", "question 3"]}'
    ))
    data = extract_json(out) or {}
    lanes = data.get("lanes") if isinstance(data, dict) else None
    if not lanes:
        lanes = ["What is the market size and number of target buyers?",
                 "Who are the competitors and what are the pricing norms?",
                 "What is the buyer's most acute, expensive pain point?"]
    return lanes[:3]


# --- Stage 2: RESEARCH fan-out ----------------------------------------------
@dataclass
class Claim:
    text: str
    source_url: str
    quantitative: bool
    promotes_category: str | None
    as_of: int | None = None   # the year the stat refers to (for staleness labeling); None if unstated


RESEARCH_ATTEMPTS = 2   # one reprompt if the first reply has no parseable, gradeable claims


def _parse_claims(data) -> list[Claim]:
    """The research seam's schema validator: a claim must carry text AND a real http(s) source URL
    (a claim with no gradeable source can't go through the moat, so it's dropped, not laundered)."""
    claims: list[Claim] = []
    for c in data if isinstance(data, list) else []:
        if not (isinstance(c, dict) and c.get("text") and c.get("source_url")):
            continue
        url = str(c["source_url"]).strip()
        if not url.startswith(("http://", "https://")):
            continue
        claims.append(Claim(
            text=str(c["text"]).strip(),
            source_url=url,
            quantitative=bool(c.get("quantitative", True)),
            promotes_category=(c.get("promotes_category") or None),
            as_of=_as_year(c.get("as_of")),
        ))
    return claims


def research_lane(idea: str, lane: str) -> list[Claim]:
    base = (
        "You are a research agent for an Idea→Offer engine. Use web_search to answer "
        "the question with SPECIFIC, sourced facts. Prefer hard numbers. For every "
        "claim, record the exact source URL you took it from.\n\n"
        f"BUSINESS IDEA:\n{idea}\n\nRESEARCH QUESTION:\n{lane}\n\n"
        "After researching, reply with ONLY a JSON array of up to 5 claims:\n"
        '[{"text": "the claim incl. the number", "source_url": "https://...", '
        '"quantitative": true, "promotes_category": "the thing this number makes look '
        'good, e.g. \'outsourced X\', or null if neutral", '
        '"as_of": 2024}]\n'
        "as_of = the year the statistic actually refers to (NOT today’s date), or null if the "
        "source states no year. This is used to flag stale numbers."
    )
    # generate → validate (schema) → reprompt once if nothing parseable came back. Good runs return on
    # the first attempt; only an empty/malformed reply costs the (bounded) retry.
    feedback = ""
    claims: list[Claim] = []
    for _ in range(RESEARCH_ATTEMPTS):
        out = call("research", HAIKU, max_tokens=1600, tools=[WEB_SEARCH_TOOL], prompt=base + feedback)
        claims = _parse_claims(extract_json(out))
        if claims:
            return claims
        feedback = ("\n\nYour previous reply had no parseable claims with real http(s) source URLs. "
                    "Reply with ONLY the JSON array described above, each claim with a real source_url.")
    return claims


def _as_year(v) -> int | None:
    """Coerce a model-reported as_of into a plausible 4-digit year, else None."""
    try:
        y = int(str(v).strip()[:4])
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= 2100 else None


# --- Stage 3: SYNTH ----------------------------------------------------------
def synthesize(idea: str, claims: list[Claim]) -> str:
    research_block = "\n".join(
        f"- {c.text}  [{c.source_url}]" for c in claims)
    return call("synth", SONNET, max_tokens=3500, prompt=(
        "You are the synthesis stage of an Idea→Offer engine. From the operator's idea "
        "and the cited research below, produce a sellable artifact set in markdown: "
        "(1) structured brief, (2) offer definition, (3) packaging + pricing, "
        "(4) go-to-market, (5) delivery playbook, (6) 30-day roadmap. Cite the research "
        "inline with its URL. Where a number comes from a source that profits if you "
        "believe it, mark it '(unverified vendor claim)'. Be concrete and specific.\n\n"
        f"IDEA:\n{idea}\n\nCITED RESEARCH:\n{research_block}"
    ))


# --- Stage 4: GATE -----------------------------------------------------------
@dataclass
class Verdict:
    claim: Claim
    tier: str
    judge: str
    flagged: bool
    reason: str
    stale: bool = False                       # quant stat older than the staleness window
    checks: dict = field(default_factory=dict)  # the atomic gate booleans (for receipts/surfacing)


# Staleness gate (deterministic, no model call). A quantitative claim whose stat year (`as_of`) is more
# than this many months before the run year is flagged as stale — the engine had no staleness concept
# before. 36 months chosen 2026-06-30 (Sam); a per-category override table is a later knob.
STALE_WINDOW_MONTHS = 36


def _now_year() -> int:
    return time.gmtime().tm_year


def _is_stale(c: Claim, now_year: int | None = None, months: int = STALE_WINDOW_MONTHS) -> bool:
    """A quant claim with a known stat-year older than the window is stale. Unknown year (as_of None)
    is NOT stale — we don't flag what we can't date."""
    if not c.quantitative or not c.as_of:
        return False
    return ((now_year or _now_year()) - int(c.as_of)) * 12 > months


def gate_claim(c: Claim, jv: str | None = None, now_year: int | None = None) -> Verdict:
    """Decompose the source-credibility verdict into atomic booleans the SCRIPT routes from, instead of
    one holistic token. Pass `jv` to reuse a voted/batched verdict instead of judging here.
      - self_interested : the moat's model judgment (voted upstream) + the registry COI rule
      - stale           : deterministic, the stat is past the staleness window
      - tier            : registry/heuristic classification (a label)
    `flagged` is pure boolean algebra over these (default-to-flag). The model picks no route."""
    tier, sells = classify_domain(c.source_url)
    if jv is None:
        jv = judge(c)
    self_interested = (
        jv == "FLAG_SELF_INTERESTED"
        or (tier == TIER_VENDOR and c.quantitative and sells is not None
            and sells == c.promotes_category)
        or (c.quantitative and tier in (TIER_VENDOR, TIER_UNKNOWN, TIER_FORUM)
            and jv != "TRUST")
    )
    stale = _is_stale(c, now_year)
    checks = {"self_interested": self_interested, "stale": stale, "tier": tier}
    flagged = self_interested or stale
    if self_interested:
        if tier == TIER_PRIMARY:
            reason = "primary source but the judge flagged it self-interested → cross-check"
        else:
            reason = f"{tier.lower()} source + judge={jv} → needs a primary cite"
    elif stale:
        reason = f"{tier.lower()} source, but the stat is from {c.as_of} (past the {STALE_WINDOW_MONTHS}-month window) → stale, re-verify"
    elif tier == TIER_PRIMARY:
        reason = "primary/authoritative source"
    elif tier == TIER_RESEARCH:
        reason = "third-party research firm"
    else:
        reason = f"{tier.lower()} source, judge={jv}"
    return Verdict(c, tier, jv, flagged, reason, stale=stale, checks=checks)


def gate_claims(claims: list[Claim], votes: int = JUDGE_VOTES, now_year: int | None = None) -> list[Verdict]:
    """Batched + VOTED gate: vote the self-interested judge across the full claim set, then assemble a
    verdict per claim from the atomic checks (judge vote + deterministic staleness). Use this over a
    per-claim `gate_claim` loop whenever you have the full claim set up front (teardown + pipeline)."""
    jvs = judge_batch_voted(claims, votes=votes)
    return [gate_claim(c, jv, now_year=now_year) for c, jv in zip(claims, jvs)]


def self_interested(tier: str, jv: str) -> bool:
    """The property the gate exists to kill: a claim resting on a source that profits
    if you believe it. Judge-led (the unattended mechanism) + the registry's known
    vendors. NOT registry-led — a fresh neutral domain the judge clears counts as safe,
    because the hand-built tier registry can't know every neutral source on a new niche."""
    return jv == "FLAG_SELF_INTERESTED" or tier == TIER_VENDOR


def survives(tier: str, jv: str) -> bool:
    return not self_interested(tier, jv)


# --- Stage 5: RE-SEARCH flagged ---------------------------------------------
@dataclass
class Rescue:
    original: Verdict
    new_url: str | None
    new_tier: str
    new_judge: str
    rescued: bool


def research_primary(c: Claim) -> Rescue | None:
    out = call("research2", HAIKU, max_tokens=900, tools=[WEB_SEARCH_TOOL], prompt=(
        "A claim in our research came from a source that may be self-interested. Use "
        "web_search to find the SAME fact stated by a PRIMARY or NEUTRAL source "
        "(government / official statistics / standards body / independent research "
        "firm), not a vendor that profits from the claim.\n\n"
        f"CLAIM: {c.text}\nORIGINAL SOURCE: {c.source_url}\n\n"
        'Reply ONLY with JSON: {"found": true/false, "source_url": "https://...", '
        '"note": "what the neutral source says"}'
    ))
    data = extract_json(out) or {}
    if not isinstance(data, dict) or not data.get("source_url"):
        return Rescue(None, None, "NONE", "?", False)  # placeholder, fixed by caller
    new_url = str(data["source_url"]).strip()
    new_tier, _ = classify_domain(new_url)
    new_claim = Claim(c.text, new_url, c.quantitative, c.promotes_category, c.as_of)
    new_judge = judge(new_claim)
    return Rescue(None, new_url, new_tier, new_judge, survives(new_tier, new_judge))


# --- Orchestrator ------------------------------------------------------------
DEFAULT_IDEA = (
    "I'm good with automation and AI tools. I've noticed that small, independent "
    "property-management companies (the ones managing a few hundred rental units) are "
    "slow to respond to tenant maintenance requests and leasing inquiries, and they "
    "lose tenants and prospective renters because of it. I think I could sell them "
    "something AI-powered, but I don't know exactly what the offer is or how I'd sell it."
)


def main() -> int:
    idea = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IDEA
    t0 = time.time()
    print("\n=== LIVE PIPELINE: Idea → Offer (unattended) ===")
    print(f"\nPROMPT (fresh niche):\n{idea}\n")

    # 1. PLAN
    lanes = plan(idea)
    print("PLAN — research lanes:")
    for ln in lanes:
        print(f"  • {ln}")

    # 2. RESEARCH fan-out (parallel)
    print("\nRESEARCH — Haiku fan-out (live web_search, parallel)…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        # bound() re-binds the active provider inside each worker (threads don't inherit contextvars),
        # so a BYOK run's fan-out still runs on the user's key.
        lane_claims = list(ex.map(bound(lambda ln: research_lane(idea, ln)), lanes))
    claims = [c for lane in lane_claims for c in lane]
    quant = [c for c in claims if c.quantitative]
    print(f"  {len(claims)} claims gathered ({len(quant)} quantitative).")

    # 3. SYNTH
    print("\nSYNTH — Sonnet artifact set…")
    artifacts = synthesize(idea, claims)
    with open("live_run_artifacts.md", "w") as f:
        f.write(f"# Live run — artifact set\n\n**Prompt:** {idea}\n\n---\n\n{artifacts}\n")
    print(f"  artifact set written ({len(artifacts)} chars) → live_run_artifacts.md")

    # 4. GATE
    print("\nGATE — credibility check on quantitative claims (heuristic + Haiku judge)…")
    verdicts = gate_claims(quant)  # one batched judge call for all claims (token win)
    flagged = [v for v in verdicts if v.flagged]
    clean = [v for v in verdicts if not v.flagged]
    for v in verdicts:
        mark = "FLAG" if v.flagged else ("OK  " if v in clean else "weak")
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        print(f"  [{mark}] {host:<28} {v.claim.text[:70]}")
    n = len(verdicts) or 1
    print(f"\n  pre-re-search: {len(clean)}/{n} clean ({len(clean)/n:.0%}), "
          f"{len(flagged)} flagged ({len(flagged)/n:.0%})")

    # 5. RE-SEARCH flagged → survival
    print("\nRE-SEARCH — forcing a primary/neutral cite for each flagged claim…")
    rescues: list[Rescue] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(bound(lambda v: research_primary(v.claim)), flagged))
    for v, r in zip(flagged, results):
        r.original = v
        rescues.append(r)
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none found)"
        print(f"  [{'RESCUED' if r.rescued else 'still weak'}] "
              f"{v.claim.text[:55]} → {host} (tier={r.new_tier}, judge={r.new_judge})")

    rescued = [r for r in rescues if r.rescued]
    survived = len(clean) + len(rescued)
    survival_q = survived / n
    rescue_rate = (len(rescued) / len(flagged)) if flagged else 0.0

    # --- Report ---
    cost = LEDGER.cost()
    elapsed = time.time() - t0
    print("\n=== RESULTS ===")
    print(f"(a) POST-GATE SURVIVAL QUALITY")
    print(f"    initial clean         : {len(clean)}/{n} ({len(clean)/n:.0%})")
    print(f"    flagged → re-searched : {len(flagged)}")
    print(f"    rescued to primary    : {len(rescued)}/{len(flagged) or 0} "
          f"(rescue rate {rescue_rate:.0%})")
    print(f"    >> SURVIVAL after gate: {survived}/{n} = {survival_q:.0%} "
          f"land on a primary/neutral cite")
    print(f"\n(b) MEASURED $/RUN")
    for stage, c in sorted(LEDGER.breakdown().items(), key=lambda x: -x[1]):
        print(f"    {stage:<10} ${c:.4f}")
    print(f"    web searches: {LEDGER.searches()}  (${LEDGER.searches()*WEB_SEARCH_PRICE:.4f})")
    print(f"    >> TOTAL    : ${cost:.4f}/run   (model estimate was $0.21)")
    print(f"\n    wall-clock: {elapsed:.0f}s")

    write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed)
    print("\n  results written → test_01_live_results_auto.md\n")
    return 0


def write_results_md(idea, lanes, verdicts, clean, flagged, rescues,
                     survived, n, survival_q, rescue_rate, cost, elapsed) -> None:
    bd = LEDGER.breakdown()
    n_rescued = len([r for r in rescues if r.rescued])
    lines = [
        "# Test #1 (live) — post-gate survival quality + measured $/run",
        "",
        "Ran the thin unattended pipeline (Haiku research fan-out → Sonnet synthesis → "
        "source-credibility gate) end-to-end on one fresh prompt, with real `web_search`. "
        "This closes the open number from `test_01_results.md`.",
        "",
        f"**Prompt (fresh niche — not seen in either dogfood run):** {idea}",
        "",
        "**Research lanes (auto-planned):**",
        *[f"- {ln}" for ln in lanes],
        "",
        "## (a) Post-gate survival quality — THE open number",
        "",
        f"- Quantitative claims gated: **{n}**",
        f"- Clean on first pass: **{len(clean)}/{n} ({len(clean)/n:.0%})**",
        f"- Flagged (self-interested / non-primary) → forced re-search: **{len(flagged)}**",
        f"- Rescued to a primary/neutral cite: **{n_rescued}/{len(flagged)}** "
        f"(rescue rate **{rescue_rate:.0%}**)",
        f"- **Survival after the gate: {survived}/{n} = {survival_q:.0%}** of cited "
        "quantitative claims land on a primary/neutral source.",
        "",
        "### Per-claim",
        "",
        "| verdict | source | claim | judge |",
        "|---|---|---|---|",
    ]
    for v in verdicts:
        host = urlparse(v.claim.source_url).netloc.removeprefix("www.")
        mark = "FLAG" if v.flagged else ("clean" if v in clean else "weak")
        lines.append(f"| {mark} | {host} | {v.claim.text[:80]} | {v.judge} |")
    lines += ["", "### Re-search outcomes", "",
              "| original claim | new source | tier | judge | rescued |",
              "|---|---|---|---|---|"]
    for r in rescues:
        host = urlparse(r.new_url).netloc.removeprefix("www.") if r.new_url else "(none)"
        lines.append(f"| {r.original.claim.text[:60]} | {host} | {r.new_tier} | "
                     f"{r.new_judge} | {'yes' if r.rescued else 'no'} |")
    lines += [
        "",
        "## (b) Measured $/run vs the $0.21 estimate",
        "",
        "| stage | cost |",
        "|---|--:|",
        *[f"| {s} | ${c:.4f} |" for s, c in sorted(bd.items(), key=lambda x: -x[1])],
        f"| **total** | **${cost:.4f}** |",
        "",
        f"- Model estimate (`pipeline_economics.py`, lean routing): **$0.21/run**.",
        f"- Measured live: **${cost:.4f}/run** ({LEDGER.searches()} web searches "
        f"@ $0.01 included).",
        f"- Wall-clock: {elapsed:.0f}s.",
        "",
        "## Caveats",
        "- One run, one niche — survival % is indicative, not a distribution. Re-run "
        "across niches to get a confidence interval.",
        "- Web-search server-tool cost ($10/1k searches) is included here but was NOT in "
        "the $0.21 token-only model — see the breakdown above for the split.",
        "- Re-search success depends on whether a neutral source actually publishes the "
        "fact; a 'still weak' outcome can mean the number only exists in vendor marketing "
        "(itself a useful signal to down-rank the claim).",
    ]
    # Auto-generated machine output. The curated file of record (with interpretation)
    # is test_01_live_results.md — kept separate so a re-run doesn't wipe the analysis.
    with open("test_01_live_results_auto.md", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
