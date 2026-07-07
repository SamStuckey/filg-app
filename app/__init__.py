"""The FILG app — the business-planning product built on the decision engine.

Everything use-case-specific lives here: the FastAPI surface (main), the staged plan
builder (planner), the funnel stages (brainstorm/intake), personas and the board, skills,
domain copy and prompts (domain/), persistence (store), and billing. The generic
machinery — research, grading, the decision tree, providers, metering — lives in the
sibling `engine` package and is imported, never duplicated.
"""
