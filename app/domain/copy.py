"""Canned domain copy — the mock drafts, waste-of-time mode, and QA/nudge fallbacks.

Everything here is keyed by the section spec (domain/sections.py) and is pure text:
  MOCK_DRAFT   per-section canned drafts for mock mode (dev/tests, zero spend)
  WOD          "waste of time" mode — when the operator forces past the kill gate with no
               substance, each section is a self-aware comedic placeholder. NO research, NO
               board, NO API calls → ~$0, and by design never a credible-looking plan
               (invariant #1: a no-substance idea yields obvious comedy, not a laundered
               plan). The off-ramp is always open: add a real skill/asset/buyer and /revet
               turns this into a real build.
  MOCK_QA      mock-mode QA report notes
  MOCK_NUDGES  fallback quick-edit chips when the nudge call has nothing to work with
"""

MOCK_DRAFT = {
    "brief": ("## Structured brief\n\n**Problem:** the operator can do the work but is stuck on "
              "*what to sell*.\n**Wedge:** a single, specific, outcome-named offer.\n**Who it's "
              "for:** people already trying to solve this and failing.\n**Why now:** demand is "
              "visible and unmet."),
    "offer": ("## Offer\n\nA productized service: you build and run one named outcome for the client "
              "(done-for-you), fixed scope, flat price, short timeline. The buyer pays for the outcome, "
              "not your hours, not a tool they self-serve, not a custom one-off."),
    "why": ("## Why you win\n\nThe alternatives are doing nothing, a DIY tool, or a generalist "
            "competitor. You win on a specific unfair advantage, name it and make it the wedge, not a "
            "vague claim of caring more."),
    "pricing": ("## Packaging & pricing\n\nOne tier to start: a flat setup fee + a small monthly. "
                "Anchor on the outcome's value, not your hours. (Vendor 'leak/ROI' figures are "
                "*unverified*, model per client.)"),
    "gtm": ("## Go-to-market\n\nPost one specific offer in three communities your buyer already "
            "lives in this week. Take the first paying customer before building anything."),
    "delivery": ("## Delivery playbook\n\nDiscovery → build → test → go-live → a monthly "
                 "'what-you-got' report. Templatize each step so it runs the same every time."),
    "roadmap": ("## 30-day roadmap\n\nWk1 reference build · Wk2 list 50 + outreach · Wk3 demos + "
                "pilots · Wk4 convert + ask for one referral."),
}

WOD = {
    "brief":    ("## The setup\n\n**Who it's for:** unclear. **Why now:** also unclear. We asked, you "
                 "clicked the button. Name one real skill or who'd pay and this becomes a real setup."),
    "offer":    ("## What you sell\n\nNo clue, you tell me. You're the one mashing the button. The moment "
                 "you name one real thing you can do, this turns into an actual offer."),
    "why":      ("## Why you win\n\nYou win because you out-clicked the gate. That is not a moat. Give us "
                 "one real edge and we'll write you a real one."),
    "pricing":  ("## What you charge\n\nCharge whatever you like for nothing. The market's counter-offer "
                 "is also nothing. Add a real deliverable and we'll price it."),
    "gtm":      ("## How you get customers\n\nStep one: have something to sell. We're still waiting on "
                 "step one."),
    "delivery": ("## How you deliver\n\nDeliver what, exactly? Name the thing and we'll build the playbook."),
    "roadmap":  ("## Your first 30 days\n\nDay 1 to 30: keep clicking this button. Results: the same as "
                 "now. Or give the gate something real and start over with an actual idea."),
}

MOCK_QA = {"notes": ["Read all seven sections as one plan — same buyer, offer, and price throughout.",
                     "Tightened a few wordy lines so each part stays skimmable.",
                     "Confirmed every cited link is a real source, no placeholders."],
           "fixed": []}

MOCK_NUDGES = ["go bolder", "narrower niche", "cheaper entry", "more specific", "add an upsell"]
