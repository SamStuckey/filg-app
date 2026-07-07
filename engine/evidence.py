#!/usr/bin/env python3
"""
The graded-evidence run — the engine's public research entry point.

`build_evidence` walks the full evidence chain (plan → research fan-out → the
source-credibility gate → re-search of flagged claims → assembled graded rows) via the
deterministic spine conductor (spine.run_engine), and forwards the conductor's typed phase
log as readable activity lines when the caller wired a progress stream. The host app calls
this; what the run is ABOUT arrives via `framing` (pipeline.ResearchFraming) so the engine
carries no product vocabulary.

Prose/synthesis over the graded rows is the host's business — see app/teardown.py for
FILG's offer-summary layer on top of this.
"""

from __future__ import annotations


def build_evidence(idea: str, headlines: int, on_progress=None, on_phase=None, max_lanes=None,
                   votes=None, sink=None, framing=None):
    """Run the evidence chain and return `(rows, stats, lanes)`.

    When the caller wired a progress stream but no explicit phase sink, each conductor phase is
    forwarded as a readable "⚙ <phase> · …" line so the live runner panel shows the real control
    flow (grade verdicts, re-source counts, stale flags), not just the leaf fan-out — the
    machinery is visible, not a debug view. `on_progress` also carries the §LANES§/§LANEDONE§
    leaf sentinels. Lazy import keeps offline callers API-free and lets tests monkeypatch the
    pipeline functions."""
    from .spine import run_engine  # noqa: PLC0415
    if on_phase is None and on_progress is not None:
        def on_phase(ev):
            line = f"⚙ {ev.id} · {ev.detail}"
            if ev.cost:
                line += f" · ${ev.cost:.3f}"
            on_progress(line)
    return run_engine(idea, headlines, on_progress=on_progress, on_phase=on_phase,
                      max_lanes=max_lanes, votes=votes, sink=sink, framing=framing)
