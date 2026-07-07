"""The beacon: engine/ stays use-case-agnostic.

The engine package must carry no business-planning vocabulary — product language lives in
app/ (domain, skills, personas, prompts) and reaches the engine only through typed seams
(ResearchFraming, the tree kind/attachment registries). This test greps the engine source
for the words that would mean the boundary leaked; when a new use case forks the app layer,
the engine should need zero edits.

If a hit here is genuinely engine-neutral prose (not product language), add a targeted
allowance below with a comment — don't weaken the token list.
"""

import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "engine"

# Product vocabulary that must not appear in engine code. Word-boundary matched,
# case-insensitive. "vendor"/"source"/"claim" are engine concepts (credibility grading) and
# deliberately absent from this list.
BANNED = [
    "business idea", "business plan", "solo operator",
    "founder", "buyer", "go-to-market", r"\bgtm\b", r"\boffer\b",
    "pricing norms", "teardown", "persona", "board of directors",
    "wedding",   # and the sibling product's vocabulary cuts both ways
]

# Engine-neutral usages that happen to contain a banned token.
ALLOWED_LINES = {
    # model_catalog / pipeline price tables talk about token *prices*, not product pricing
}


def test_engine_source_carries_no_product_vocabulary():
    hits = []
    for path in sorted(ENGINE.glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            for tok in BANNED:
                pat = tok if tok.startswith("\\b") or "\\b" in tok else rf"\b{re.escape(tok)}\b"
                if re.search(pat, line, re.IGNORECASE) and (path.name, i) not in ALLOWED_LINES:
                    hits.append(f"{path.name}:{i}: [{tok}] {line.strip()[:100]}")
    assert not hits, "engine/ leaked product vocabulary:\n" + "\n".join(hits)


def test_engine_does_not_import_the_app():
    hits = []
    for path in sorted(ENGINE.glob("*.py")):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.match(r"\s*(from|import)\s+app\b", line):
                hits.append(f"{path.name}:{i}: {line.strip()}")
    assert not hits, "engine/ imports the app layer:\n" + "\n".join(hits)
