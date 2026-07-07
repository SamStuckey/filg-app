"""Spine conductor tests (migration step 1).

The conductor (`spine.run_engine`) must reproduce the legacy build_evidence chain
byte-for-byte — same graded rows, same stats, same lanes — while adding a typed phase
log. The pipeline seams (plan/research/grade/re-search) are monkeypatched so this runs
with no API spend; the assertions ARE the golden-output check on the deterministic
assembly + routing the conductor owns."""

from engine import pipeline
from engine import spine
from engine.pipeline import Claim, Rescue, Verdict


def _claim(text, url, quant=True, as_of=None):
    return Claim(text=text, source_url=url, quantitative=quant, promotes_category=None, as_of=as_of)


# A fixed scenario exercising every row path: a cleared claim, a flagged-then-rescued
# claim, a flagged-and-labeled claim, and a non-quantitative claim (dropped before the gate).
LANES = ["L0 market?", "L1 pricing?"]
C_OK = _claim("2.5M businesses", "https://census.gov/x", as_of=2022)
C_FLAG1 = _claim("FLAG 62% missed calls", "https://vendor.io/a", as_of=None)
C_FLAG2 = _claim("FLAG 80% prefer us", "https://vendor.io/b", as_of=2019)
C_TEXT = _claim("qualitative note", "https://blog.com/c", quant=False)


def _install(monkeypatch):
    monkeypatch.setattr(pipeline, "plan", lambda idea: list(LANES))
    monkeypatch.setattr(pipeline, "research_lane",
                        lambda idea, ln: {"L0 market?": [C_OK, C_FLAG1],
                                          "L1 pricing?": [C_FLAG2, C_TEXT]}[ln])

    def fake_gate(claims, votes=None):
        out = []
        for c in claims:
            flag = c.text.startswith("FLAG")
            out.append(Verdict(c, "VENDOR" if flag else "PRIMARY",
                               "FLAG_SELF_INTERESTED" if flag else "TRUST", flag, "r"))
        return out

    monkeypatch.setattr(pipeline, "gate_claims", fake_gate)
    monkeypatch.setattr(pipeline, "research_primary",
                        lambda c: Rescue(None, "https://primary.gov/p", "PRIMARY", "TRUST", True))


def test_run_engine_golden_rows(monkeypatch):
    _install(monkeypatch)
    rows, stats, lanes = spine.run_engine("an idea", headlines=1)

    assert lanes == LANES
    # non-quant claim never reaches the gate; 2 cleared (1 passed + 1 rescued), 1 labeled.
    assert stats == {"checked": 3, "cleared": 2, "flagged": 1}

    cleared = rows[0]
    assert cleared["mark"] == "ok" and cleared["text"] == "2.5M businesses"
    assert cleared["url"] == "https://census.gov/x" and cleared["tier"] == "PRIMARY"
    assert cleared["judge"] == "TRUST" and cleared["as_of"] == 2022 and cleared["lane"] == "L0 market?"

    rescued = rows[1]
    assert rescued["mark"] == "ok" and rescued["text"] == "FLAG 62% missed calls"
    assert rescued["url"] == "https://primary.gov/p"            # re-sourced to the primary cite
    assert rescued["note"].startswith("re-sourced") and rescued["lane"] == "L0 market?"

    labeled = rows[2]
    assert labeled["mark"] == "warn" and labeled["text"] == "FLAG 80% prefer us"
    assert labeled["url"] == "https://vendor.io/b" and labeled["as_of"] == 2019
    assert labeled["lane"] == "L1 pricing?"

    # triangulation: the two ok rows share lane L0 with different hosts → both corroborated.
    assert rows[0]["corroborated"] is True and rows[0]["sources"] == 2
    assert rows[1]["corroborated"] is True
    assert "corroborated" not in rows[2]   # warn rows aren't triangulation-labeled


def test_cost_cap_skips_research_and_labels(monkeypatch):
    # When the run blows past the cap before re-search, the fan-out is skipped and every flagged claim
    # is labeled (invariant #2, code-enforced) — research_primary must NOT be called.
    _install(monkeypatch)
    monkeypatch.setattr(spine, "_run_cost", lambda: 999.0)   # pretend the run already overspent

    def boom(c):
        raise AssertionError("research_primary called despite the cost cap")

    monkeypatch.setattr(pipeline, "research_primary", boom)
    events = []
    rows, stats, _ = spine.run_engine("an idea", headlines=1, on_phase=events.append, cost_cap=2.0)
    assert stats == {"checked": 3, "cleared": 1, "flagged": 2}      # both flagged labeled, none rescued
    assert any(e.id == "re-search" and e.verdict == spine.PAUSE for e in events)
    assert all("primary.gov" not in r["url"] for r in rows)         # nothing was re-sourced


def test_run_engine_phase_log(monkeypatch):
    _install(monkeypatch)
    events = []
    spine.run_engine("an idea", headlines=1, on_phase=events.append)
    assert [e.id for e in events] == ["plan", "research", "grade", "re-search", "assemble"]
    assert all(e.verdict == spine.PASS for e in events)
    kinds = {e.id: e.kind for e in events}
    assert kinds["grade"] == spine.JUDGE and kinds["assemble"] == spine.DETERMINISTIC


def test_votes_thread_through_to_the_gate(monkeypatch):
    # The moat's vote count is stage-scaled: run_engine passes `votes` down to gate_claims (default 3
    # when unset — the committed deep build; 1 on the throwaway skim). Guard the plumbing.
    _install(monkeypatch)
    seen = {}

    def spy_gate(claims, votes=None):
        seen["votes"] = votes
        return [Verdict(c, "PRIMARY", "TRUST", False, "r") for c in claims]

    monkeypatch.setattr(pipeline, "gate_claims", spy_gate)
    spine.run_engine("an idea", headlines=1)
    assert seen["votes"] == pipeline.JUDGE_VOTES        # default: full ×3 on the committed build
    spine.run_engine("an idea", headlines=1, votes=1)
    assert seen["votes"] == 1                           # skim: one vote


def test_sink_exposes_fetched_claims(monkeypatch):
    # T2: run_engine populates `sink['claims']` with the fetched (Claim, lane) pairs so the merge skim
    # can persist them for a later re-grade. Only quantitative claims (the ones that reach the gate).
    _install(monkeypatch)
    sink = {}
    spine.run_engine("an idea", headlines=1, sink=sink)
    got = {(c.text, lane) for c, lane in sink["claims"]}
    assert got == {("2.5M businesses", "L0 market?"), ("FLAG 62% missed calls", "L0 market?"),
                   ("FLAG 80% prefer us", "L1 pricing?")}          # the non-quant claim is excluded
    assert all(c.quantitative for c, _ in sink["claims"])


def test_regrade_engine_reuses_claims_without_web_fanout(monkeypatch):
    # T2 reuse path: regrade already-fetched claims — NO plan, NO research_lane (the web fan-out).
    _install(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("the web fan-out ran on the reuse path")

    monkeypatch.setattr(pipeline, "plan", boom)
    monkeypatch.setattr(pipeline, "research_lane", boom)
    claim_lanes = [(C_OK, "L0 market?"), (C_FLAG1, "L0 market?"), (C_FLAG2, "L1 pricing?")]
    rows, stats = spine.regrade_engine(claim_lanes, headlines=1)
    assert stats == {"checked": 3, "cleared": 2, "flagged": 1}     # same grades as the full run
    assert rows[0]["text"] == "2.5M businesses" and rows[0]["mark"] == "ok"
    assert rows[1]["url"] == "https://primary.gov/p"               # flagged claim re-sourced by the chase


def test_regrade_votes_the_moat_at_full_strength(monkeypatch):
    # The reuse path must keep the committed deliverable ×3-voted (invariant #1), not reuse a 1-vote skim.
    _install(monkeypatch)
    seen = {}

    def spy_gate(claims, votes=None):
        seen["votes"] = votes
        return [Verdict(c, "PRIMARY", "TRUST", False, "r") for c in claims]

    monkeypatch.setattr(pipeline, "gate_claims", spy_gate)
    spine.regrade_engine([(C_OK, "L0 market?")], headlines=0)
    assert seen["votes"] == pipeline.JUDGE_VOTES                   # full ×3 on what ships


def test_build_evidence_delegates_to_spine(monkeypatch):
    # The public entry point keeps its signature and returns the conductor's output unchanged.
    _install(monkeypatch)
    from engine import teardown
    rows, stats, lanes = teardown.build_evidence("an idea", 1)
    assert lanes == LANES and stats == {"checked": 3, "cleared": 2, "flagged": 1}
    assert rows[1]["url"] == "https://primary.gov/p"


def test_build_evidence_streams_phase_lines(monkeypatch):
    # When a progress stream is wired, the conductor's phase log surfaces as readable ⚙ lines (the
    # showcase activity feed) alongside the leaf sentinels — one per engine phase.
    _install(monkeypatch)
    from engine import teardown
    seen = []
    teardown.build_evidence("an idea", 1, on_progress=seen.append)
    phase_lines = [s for s in seen if s.startswith("⚙ ")]
    ids = [s.split("·")[0].strip().removeprefix("⚙ ").strip() for s in phase_lines]
    assert ids == ["plan", "research", "grade", "re-search", "assemble"]


def test_leaf_sentinels_unchanged(monkeypatch):
    # §LANES§ once + one §LANEDONE§ per lane — the live-fan-out viz contract the frontend parses.
    _install(monkeypatch)
    seen = []
    spine.run_engine("an idea", headlines=1, on_progress=seen.append)
    import json
    lanes_lines = [s for s in seen if s.startswith("§LANES§")]
    done = sorted(int(s[len("§LANEDONE§"):]) for s in seen if s.startswith("§LANEDONE§"))
    assert len(lanes_lines) == 1 and json.loads(lanes_lines[0][len("§LANES§"):]) == LANES
    assert done == [0, 1]
