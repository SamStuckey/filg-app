"""The staged build spec — WHAT this product builds, in order, and what "done right" means.

This is the business-planning use case's stage list. The planner walks it generically
(step 0..N, finalize-and-advance, terminal at N); everything product-shaped about the walk
lives here:

  key    stable internal id — keys the mock/WOD copy, history entries, and the per-stage
         model-call slug (`plan_<key>`); never rename without a data migration
  file   the artifact filename the finalized draft lands under in node["files"] — also the
         unit the PDF/zip exports and the per-plan unlock key address
  title  operator-facing display name (also injected into the synthesis prompt)
  sub    one-line subtitle shown under the title
  guide  optional extra synthesis instruction — forces the section to answer what a generic
         draft leaves vague (what am I actually selling? why me?)

A sibling product on the engine swaps THIS list (the wedding fork's seven decision domains
are the proof); the planner's mechanics don't change.
"""

SECTIONS = [
    {"key": "brief",    "file": "1-the-setup.md",            "title": "The setup",             "sub": "who it's for & why now"},
    {"key": "offer",    "file": "2-what-you-sell.md",        "title": "What you sell",         "sub": "the offer & business model",
     "guide": "State plainly WHAT the operator sells AND how it's produced — pick one and name it: a "
              "done-for-you build/service, a productized repeatable package, reselling/white-labeling "
              "an existing tool, or their own software. Make it unambiguous whether they're building "
              "it custom, productizing it, reselling someone else's, or selling a service — and say "
              "exactly what the buyer is paying for."},
    {"key": "why",      "file": "3-why-you-win.md",          "title": "Why you win",           "sub": "alternatives & your edge",
     "guide": "Name the REAL alternatives the buyer weighs — including doing nothing / DIY and the "
              "obvious competitor or substitute, then make the specific, defensible case for why THIS "
              "operator wins anyway, led by their unfair advantage (founder edge). No 'we care more'; "
              "give a defensible reason a buyer picks them over the named alternatives."},
    {"key": "pricing",  "file": "4-what-you-charge.md",      "title": "What you charge",       "sub": "packaging & price"},
    {"key": "gtm",      "file": "5-how-you-get-customers.md","title": "How you get customers", "sub": "go-to-market"},
    {"key": "delivery", "file": "6-how-you-deliver.md",      "title": "How you deliver",       "sub": "delivery playbook"},
    {"key": "roadmap",  "file": "7-your-first-30-days.md",   "title": "Your first 30 days",    "sub": "the roadmap"},
]
N = len(SECTIONS)


def section(key: str) -> dict | None:
    return next((s for s in SECTIONS if s["key"] == key), None)


# The final-QA verdict, decomposed into atomic yes/no checks the SCRIPT routes from (instead of
# one holistic "is this good?"). Each is voted; failures (with reasons) feed the editor pass.
QA_CHECKS = [
    ("CONSISTENT", "Do ALL sections describe the SAME buyer, the SAME core offer, the SAME price, and "
                   "the SAME primary channel, with no drift between sections?"),
    ("NO_CONTRADICTION", "Is the plan free of statements that directly contradict each other across "
                         "sections?"),
    ("NO_INVENTED_STAT", "Does the plan avoid presenting any NEW statistic or hard number that is not "
                         "already supported by the cited research (i.e. nothing fabricated)?"),
]
