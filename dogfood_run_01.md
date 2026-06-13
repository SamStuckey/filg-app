---
status:
  doc: dogfood_run
  run: 01
  subject: AI lead-response for home-service SMBs
  last_touched: 2026-06-12
---

# Dogfood Run 01 — full artifact set from one vague prompt

**Purpose:** prove the engine produces *sellable* output before building the app. This is what the
product would generate for a stranger's plain-text prompt. Judge it as a customer would.

**Input prompt (verbatim):**
> "I'm good with automation tools and I've noticed local home-service businesses — plumbers, HVAC,
> electricians — miss a lot of calls and are slow to follow up on leads, which costs them jobs. I
> think I could make money helping them with AI, but I don't know exactly what I'd sell or how."

---

## Artifact 1 — Structured brief
- **Problem (theirs):** home-service SMBs miss calls and respond to leads too slowly; the missed
  job goes to whoever answers first.
- **Wedge (yours):** you can build and run the automation that answers and follows up instantly,
  sold as a done-for-you service to owners who will never set it up themselves.
- **Who it's for:** owner-operator home-service businesses (plumbing, HVAC, electrical, roofing)
  doing enough call volume to feel the leak but too small to staff a front office.
- **Why now:** AI voice agents got good and cheap in 2025–26; SMB tools (Goodcall, Rosie, Avoca)
  exist but owners won't wire them up — the gap is implementation + management, not technology.

## Artifact 2 — Opportunity research (cited; verify >3-mo-old figures before quoting)
- ~**2.5M US home-service businesses**, 6.1M workers; market $200B+. [GoDuo](https://www.goduo.co/blog/us-home-services-industry-statistics-you-need-to-know-in-2024)
- **~62% of calls to small businesses go unanswered**; **80% of voicemail callers hang up**. [Aira](https://www.getaira.io/blog/missed-business-calls-statistics)
- **78% of customers buy from the first business that responds.** [Aira](https://www.getaira.io/blog/missed-business-calls-statistics)
- **40–60% of leads arrive outside business hours**; average home-service lead response time
  exceeds **42 hours**; >50% of contractors take 5+ days. [Casey Response](https://caseyresponse.com/blog/lead-response-time-statistics)
- Estimated **$45K–$120K/yr** lost to missed calls at 5–10/week × ~$500/job. [Aira](https://www.getaira.io/blog/missed-business-calls-statistics) (medium confidence — model it per client)
- Tooling exists but adoption is early: AI receptionists run **$49–$299/mo** (Rosie, Goodcall,
  Smith.ai, Avoca) vs. a $35K+/yr human. [NextPhone](https://www.getnextphone.com/blog/best-ai-receptionist) — **the owner's barrier is setup, not price.**
- **Implication:** the buyable thing isn't software (commodity, owner won't configure it) — it's
  **done-for-you setup + ongoing management** with an ROI story the owner can feel.

## Artifact 3 — Offer definition
**What you sell:** "Never miss a job again" — a done-for-you AI front desk that answers every call
24/7, texts back every web/missed lead within seconds, books jobs, and follows up until they
respond. You build it, you run it.

**Packaging + pricing** (grounded in operator norms: setup $999–$2,500, retainer $500–$2,000/mo
[Arsum](https://arsum.com/blog/posts/ai-automation-agency-pricing/), [Upwork](https://www.upwork.com/services/product/admin-customer-support-ai-phone-receptionist-setup-vapi-ai-24-7-call-handling-1979621516270497866)):
- **Setup:** $1,500 one-time (AI receptionist + missed-call text-back + lead follow-up sequence,
  wired to their CRM/calendar).
- **Management:** $500/mo (monitoring, tuning, monthly "jobs recovered" report). Underpriced vs.
  the $45K+/yr leak — that gap *is* the pitch.
- **Land-cheap option:** $0 setup, $750/mo for 6 months (lowers the yes; you keep the system).
- **Your build cost:** Rosie/Goodcall ($49–149/mo) + GoHighLevel ($97–297/mo) + n8n. Margin is
  healthy at 5+ clients. [NetPartners](https://netpartners.marketing/gohighlevel-vs-n8n/)

## Artifact 4 — Go-to-market
- **ICP for first 5 clients:** owner-operators you can reach warm — start in one trade + one metro.
- **Channels, ranked by evidence:**
  1. **Referrals / BNI / local network** — 82% of SMB owners say most business is referral; agencies
     land their first 3–5 clients here. [BNI](https://bniamerica.com/en-US/index)
  2. **Multi-channel outbound** (email + LinkedIn + phone) — 18–25% response vs. 2–3% email-only. [Martal](https://martal.ca/conversion-rate-statistics-lb/)
  3. **Cold call / walk-in** — fast, fits this owner; thin published data but strong fit.
- **Pitch that converts:** ROI framing on **booked jobs, not leads**; lead with a case-study
  number; offer a **14-day pilot** measured by jobs recovered. [Ciela](https://www.ciela.ai/blogs/how-to-use-case-studies-to-close-ai-automation-deals)
- **Demo that closes:** call the prospect's own number after hours, let it go unanswered, then show
  your AI handling the same call. The leak made visible.

## Artifact 5 — Delivery playbook (how you actually do the work)
1. **Discovery (30 min):** their call volume, after-hours %, CRM/calendar, top 5 call reasons.
2. **Build (week 1):** AI receptionist (Rosie/Goodcall) trained on their services/FAQs/booking;
   missed-call→instant-text; web-lead→text+email follow-up sequence (GoHighLevel); calendar booking.
3. **Test:** call scripts for their top scenarios; confirm booking + handoff to owner work.
4. **Go live + baseline:** capture "before" (missed calls/week) to prove ROI later.
5. **Manage (ongoing):** monthly report — calls answered, leads texted, **jobs booked/recovered**;
   tune scripts. The report renews the retainer.
- **Anti-contamination note:** keep each client's data/config siloed (FRI's invariant applies).

## Artifact 6 — Execution roadmap (first 30 days)
- **Wk 1:** pick ONE trade + metro; build a reference system on a dummy business; record the demo.
- **Wk 2:** list 50 local targets; warm-intro/BNI first; start multi-channel outbound; book 5 demos.
- **Wk 3:** run demos (after-hours call trick); offer the 14-day pilot; aim 1–2 pilots live.
- **Wk 4:** prove jobs recovered on a pilot → convert to paid; ask for a referral; reuse the build.
- **First test to talk to a customer:** can you get ONE owner to a paid pilot? That's the validation.

---

## VERDICT — is this output sellable? (honest)
**Yes, with a caveat.** What's strong and beats a generic ChatGPT answer:
- **Cited, specific numbers** (62% unanswered, 78% first-responder, 42-hr response, operator
  pricing $999–2,500 + $500–2,000/mo) — not invented TAM. This is the differentiator working.
- **Concrete, named build stack and price points** an operator could act on Monday.
- **A real sales motion** (the after-hours-call demo) and a pilot structure with an ROI metric.
- It went the whole arc — idea → offer → pricing → GTM → delivery → 30-day roadmap — which is
  exactly the layer competitors skip.

**Caveat / honest gaps (what the product must close):**
- The $45K–$120K leak figure is a back-of-envelope from one source — needs per-client modeling, not
  a headline. A weaker engine would have stated it as fact. (Our verify step should flag this.)
- Output quality here leaned on *me* steering three good searches + synthesis. The product has to
  reproduce this **without a human in the loop** — that's the real build risk, not the artifact format.
- It's strong because this niche is squarely in Sam's wheelhouse (FRI). The open question from the
  business plan stands: does it hold up on a niche Sam *doesn't* know? Next dogfood should test that.

**Call:** the artifact set is genuinely sellable and clearly beats free ChatGPT on citations,
specificity, and completeness. The bottleneck isn't "can the output be good" — it's "can the
pipeline produce this unattended + cheaply." That's now the thing to prototype.
