"""The business-planning research framing — what an engine run is ABOUT for this product.

The engine owns the research CONTRACT (JSON schemas, the claim validator, the credibility
gate); this framing supplies the subject wording its plan/research prompts speak. Passed by
app/teardown.py (and any other app-side caller of engine.evidence.build_evidence) so the
engine itself stays product-blind. The text is the exact wording the prompts always used."""

from engine.pipeline import ResearchFraming

FRAMING = ResearchFraming(
    planner_intro=(
        "You are the research planner for an Idea→Offer engine. Given a plain-text "
        "business idea from a solo operator, output the 3 most decision-relevant "
        "research lanes to investigate (e.g. market size, competitor/pricing norms, "
        "buyer pain). Each lane is one specific researchable question."),
    researcher_intro=(
        "You are a research agent for an Idea→Offer engine. Use web_search to answer "
        "the question with SPECIFIC, sourced facts. Prefer hard numbers. For every "
        "claim, record the exact source URL you took it from."),
    subject_label="BUSINESS IDEA",
    fallback_lanes=("What is the market size and number of target buyers?",
                    "Who are the competitors and what are the pricing norms?",
                    "What is the buyer's most acute, expensive pain point?"),
)
