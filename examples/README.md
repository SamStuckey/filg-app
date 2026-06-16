# Example output — a finished FILG plan

`dentist-missed-calls.md` is an example of the artifact the builder produces when you finish a plan:
the six sections, written one at a time, with the graded-research appendix at the end.

Two things it demonstrates:

1. **The download follows the final decision set.** The plan-builder is a branching decision tree
   (`app/planner.py` + the `tree` column in `app/store.py`). At any point you can go **Back** to revise
   a part, which forks a new branch. The downloadable plan bundles only the **active branch** you
   landed on, so it always reflects the decisions you actually kept. The "Decisions that shaped this
   plan" note at the top of the example shows the path that produced it.
2. **The format.** Plain-language sections an operator could act on, plus the source-credibility
   appendix that labels vendor stats instead of laundering them.

This file is a hand-written reference of the *target* quality. The live mock-mode output is thinner
(canned drafts); the investor-grade formatting pass is tracked in `filg-docs/launch_todo.md`
(Product backlog → "Investor-grade Download plan artifact").
