"""VOICE copy-lint gate (the deterministic AI-tells linter + the author reprompt seam).

The linter must be verified against BOTH a known-clean and a known-dirty fixture before it
can be trusted to gate (a linter that false-positives blocks valid work). The author seam
must converge (clean output passes through; dirty output is reprompted, never stripped)."""

import spine
import voice_lint


# ── the linter: known-good and known-bad fixtures ─────────────────────────────
def test_clean_text_passes():
    clean = ("We install an AI front desk that answers every call and texts back missed leads. "
             "Live in 5 days, flat monthly fee, no per-lead pricing.")
    assert voice_lint.is_clean(clean)
    assert voice_lint.lint(clean) == []


def test_dirty_text_is_caught():
    dirty = ("This robust, seamless platform leverages synergy to elevate and unlock your business "
             "— honestly, it's a testament. In today's market that matters.")
    kinds = {f.kind for f in voice_lint.lint(dirty)}
    assert "em-dash" in kinds
    assert '"robust"' in kinds and '"leverage"' not in kinds  # "leverages" is not the word "leverage"
    assert '"synergy"' in kinds and '"elevate"' in kinds and '"unlock"' in kinds
    assert "\"honest(ly)\"" in kinds and "\"in today's ...\"" in kinds


def test_findings_carry_located_context_not_just_counts():
    f = voice_lint.lint("a robust thing")[0]
    assert "robust" in f.context  # actionable: the model can locate it


def test_feedback_lists_each_offender():
    fb = voice_lint.feedback(voice_lint.lint("a robust, seamless build"))
    assert "remove" in fb and "robust" in fb and "seamless" in fb


def test_voice_rule_single_sources_the_wordlist():
    # The prompt rule is generated from the same BANNED_WORDS the linter gates on — no drift.
    for w in voice_lint.BANNED_WORDS:
        assert w in voice_lint.VOICE_RULE
    # skill_registry re-exports the same object (one source for prompt + gate).
    import skill_registry
    assert skill_registry.VOICE is voice_lint.VOICE_RULE


# ── the author seam: converge, don't strip ────────────────────────────────────
def test_run_author_returns_clean_first_try():
    out, residual = spine.run_author(lambda fb: "a plain clean sentence", voice_lint.lint)
    assert out == "a plain clean sentence" and residual == []


def test_run_author_reprompts_until_clean():
    calls = []

    def gen(fb):
        calls.append(fb)
        return "first robust draft" if len(calls) == 1 else "a clean second draft"

    out, residual = spine.run_author(gen, voice_lint.lint, max_fix=3)
    assert out == "a clean second draft" and residual == []
    assert len(calls) == 2                      # regenerated once
    assert calls[0] == "" and "robust" in calls[1]  # feedback located the offender for the rewrite


def test_run_author_is_bounded_and_surfaces_residual():
    # A writer that never cleans up must not loop forever; the residual is surfaced, not stripped.
    out, residual = spine.run_author(lambda fb: "always robust and seamless", voice_lint.lint, max_fix=3)
    assert "robust" in out                       # we never edit the text ourselves
    assert any(f.kind == '"robust"' for f in residual)


def test_run_author_plateau_stops_early():
    calls = []

    def gen(fb):
        calls.append(fb)
        return "same robust output"             # identical offenders every round

    spine.run_author(gen, voice_lint.lint, max_fix=5)
    assert len(calls) == 2                        # plateau detected after the repeat, well under max_fix
