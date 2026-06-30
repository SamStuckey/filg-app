"""Spine conductor tests (migration step 1).

The conductor (`spine.run_engine`) must reproduce the legacy build_evidence chain
byte-for-byte — same graded rows, same stats, same lanes — while adding a typed phase
log. The pipeline seams (plan/research/grade/re-search) are monkeypatched so this runs
with no API spend; the assertions ARE the golden-output check on the deterministic
assembly + routing the conductor owns."""

import pipeline
import spine
from pipeline import Claim, Rescue, Verdict


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

    def fake_gate(claims):
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


def test_run_engine_phase_log(monkeypatch):
    _install(monkeypatch)
    events = []
    spine.run_engine("an idea", headlines=1, on_phase=events.append)
    assert [e.id for e in events] == ["plan", "research", "grade", "re-search", "assemble"]
    assert all(e.verdict == spine.PASS for e in events)
    kinds = {e.id: e.kind for e in events}
    assert kinds["grade"] == spine.JUDGE and kinds["assemble"] == spine.DETERMINISTIC


def test_build_evidence_delegates_to_spine(monkeypatch):
    # The public entry point keeps its signature and returns the conductor's output unchanged.
    _install(monkeypatch)
    import teardown
    rows, stats, lanes = teardown.build_evidence("an idea", 1)
    assert lanes == LANES and stats == {"checked": 3, "cleared": 2, "flagged": 1}
    assert rows[1]["url"] == "https://primary.gov/p"


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
