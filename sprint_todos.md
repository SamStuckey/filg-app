---
status:
  doc: sprint_todos
  last_touched: 2026-06-13
---

# Sam's TODOs — the demand-validation sprint

Full plan + thresholds: `validation_strategy.md`. Copy to post: `copy_bank.md`.
**The only number that decides this: ≥3 pre-payments or 1 paid pilot from ~50 conversations.**

---

## Decisions only you can make (do first — they unblock everything)
1. ~~**Name + domain.**~~ ✓ **DONE — FILG** (filg.ai + fuckitletsgo.ai bought). Update the logo
   text in `landing/index.html` if you want a different treatment than "FILG".
2. **Form / newsletter provider.** The lead magnet *is* a newsletter → I'd use **Buttondown or
   ConvertKit**. Get the endpoint, paste it into `FORM_ENDPOINT` in `landing/index.html`.
3. **Which niches for the weekly teardown** (pipeline of free content). My picks below — change if you have better. Each is one `teardown.py` run.

## Week 0 — setup (~half a day, no app)
- [ ] Deploy `landing/index.html` (Cloudflare Pages / Netlify / Vercel — steps in `landing/README.md`).
- [ ] Wire `FORM_ENDPOINT` (form provider) + uncomment the Plausible line with your domain.
- [ ] Make a **tracking sheet** (1 tab): columns = `date · source · visitors · signups · fakedoor_clicks · interviews_done · paid_commitments · notes`. This is how you read the gate.
- [ ] Generate teardowns #2–#3 so you have a content buffer:
  ```bash
  cd prototype
  python3 teardown.py "AI front desk + missed-call text-back for HVAC and plumbing contractors who lose after-hours jobs"
  python3 teardown.py "AI lead follow-up for independent insurance agents who let quote requests go cold"
  ```
  (#1, law-firm intake, already in `teardowns/`.)

## Weeks 1–2 — problem + resonance
- [ ] **Post build-in-public daily** on X (7 posts written in `copy_bank.md` → "X / build-in-public").
- [ ] **Seed Tier-1 communities** value-first (copy in `copy_bank.md`): one post to **r/AI_Agents**
      (link in first comment), one to **Ottley's AAA Hub Skool** + **AI Automation Society** (no link,
      DM the engaged). Verify each sub/Skool's promo rule before posting — one bad link burns the account.
- [ ] **Recruit + run 15–20 interviews** (outreach DM + the 6-question Mom Test script in `copy_bank.md`).
      Goal: hear what they *already pay* to solve this. Don't pitch.
- [ ] DM 10–15 ICP the **concierge offer** ("I'll build your offer free this week").

## Weeks 3–4 — concierge + climb to money
- [ ] **Run the engine by hand for 5–8 hand-raisers** (you run `pipeline.py` / `teardown.py`, send the result). Cap ~10.
- [ ] After each, **ask the money question** (copy in `copy_bank.md` → "Money ask"). Push to a founding pre-pay.
- [ ] Keep posting + seeding.

## Weeks 5–6 — force the money + decide
- [ ] **Open the founding pre-sale** (locked $39/mo). Take the money now.
- [ ] Offer **1–2 paid pilots** (one-time "done-with-you offer build" fee).
- [ ] *Optional:* **$100–150 ad micro-test** on one generated offer (validates acquisition + proves the product).
- [ ] **Make the call** against the PASS/KILL thresholds in `validation_strategy.md`.

---

## What else needs doing (beyond the sprint)
- **Nothing else is blocking the sprint.** Everything required is above or already built.
- **After a PASS:** graduate `ideas/idea_to_offer/` to its own repo + build the thin MVP
  (`launch_todo.md` Phase 3). When you build, two things from this session carry over: run the gate
  in **label-don't-chase** mode (keeps $/run ~$0.40), and replace the hand registry with the
  judge/allowlist path (`test_01_live_results.md`).
- **Save for the MVP launch (not now):** Product Hunt — it needs 20–50 warm supporters first, which
  the sprint builds.
- **Ongoing:** keep shipping the weekly teardown regardless of the gate — it compounds the list and
  doubles as your build-in-public spine.

## Where the writing is
All post/DM/email copy is in **`copy_bank.md`**, each block labeled with where it goes. Edit to your
voice before posting — it's drafted direct/operator, leaning on the FRI receipt and the gate.
