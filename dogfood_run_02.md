---
status:
  doc: dogfood_run
  run: 02
  subject: Medical billing / RCM service for small practices
  test_variable: niche Sam does NOT know (domain unfamiliarity)
  last_touched: 2026-06-12
---

# Dogfood Run 02 — unfamiliar niche stress test

**Purpose:** Run 01 worked on a niche in Sam's wheelhouse (AI automation for SMBs). This run tests
the real risk: **does the engine still produce sellable, honest output on a niche neither of us can
steer from memory?** Subject: an aspiring solo operator starting a medical billing / RCM service.

**Input prompt (verbatim):**
> "I'm detail-oriented and good with numbers, and I read that small doctor and therapist practices
> struggle to get paid by insurance, so a lot of them outsource their medical billing. I think I
> could start a medical billing service, but I don't know what to sell exactly, how to price it,
> or how to get clients."

---

## Artifact 1 — Structured brief
- **Problem (theirs):** small practices lose 3–10% of revenue to denied/underpaid claims and slow
  A/R; in-house billing costs $55–80K/yr per biller and the experienced staff are retiring.
- **Wedge (yours):** you run their billing for a slice of collections, get paid more by collecting
  more — incentives aligned, unlike a salaried in-house biller.
- **Who it's for:** solo/small independent practices in a **single, low-complexity specialty**
  (mental health / therapy is the easiest entry — repetitive coding, simpler payer rules).
- **Why now:** practice consolidation is squeezing independents (only 22% of physicians still
  independent, down from 38% in 2019); the survivors need to plug revenue leaks to stay solo.

## Artifact 2 — Opportunity research (cited; CREDIBILITY-FLAGGED — see verdict)
- US medical-billing-outsourcing market **~$15–17B (2024), ~12–14% CAGR**. [Grand View](https://www.grandviewresearch.com/industry-analysis/us-medical-billing-outsourcing-market) (high conf)
- **First-pass claim denial ~12% overall; small practices ~15–18%.** [Aptarro](https://www.aptarro.com/insights/us-healthcare-denial-rates-reimbursement-statistics) (med-high)
- Practices lose **3–10% of revenue** to billing errors; cost per denied claim ~$25. (med — range)
- **Days in A/R:** ≤30 good, 40–50 average, 60+ underperforming. [IC System](https://www.icsystem.com/track-your-healthcare-accounts-receivable-days/) (high)
- **~395,000 independent practices; only 22.4% of physicians independent (2025, down from 37.8%
  in 2019).** [AMA](https://www.ama-assn.org/system/files/2024-prp-pp-characteristics.pdf) (high — primary source)
- In-house billing cost **$55–80K/yr** (one biller); outsourcing runs **4–8% of collections**.
  [CareCloud](https://carecloud.com/cost-of-medical-billing-services/) (med-high)
- ⚠️ "Outsourcing clinics 30% more likely to hit net-revenue targets" and "switching gains
  $201,600/yr" — **sourced from billing-vendor blogs; self-interested, treat as marketing not fact.**
  (This is the run's key finding — see verdict.)

## Artifact 3 — Offer definition
**What you sell:** full-service billing for one specialty — claim submission, denial management,
A/R follow-up, patient statements — priced on a slice of what you collect, so you only win when
they do.
**Packaging + pricing** (grounded in norms: 5–8% of net collections, MGMA ~5%; setup $0–3K
[Neolytix](https://neolytix.com/articles/what-is-the-going-rate-for-medical-billing-services/)):
- **Core:** **6% of net collections**, no setup fee for the first 2–3 clients (to build case studies).
- **Easy add-on:** patient-statement / balance follow-up.
- **Stack cost to deliver:** practice-management + clearinghouse — Office Ally (free + ~$35/mo ERA)
  or Tebra/AdvancedMD (~$429/mo); per-claim $0.10–0.40. Run **dual clearinghouses** (Change
  Healthcare 2024 breach lesson). [OneMed](https://www.onemedbilling.com/blog-details/top-10-clearinghouses-in-medical-billing-with-pros-and-cons)
- **Margin reality:** at 6% of a $400K-collections therapy practice = ~$24K/yr/client; viable at
  3–5 clients, but **labor-intensive** — this is a services business, not passive.

## Artifact 4 — Go-to-market
- **ICP for first clients:** newly-opening or solo **mental-health / therapy** practices (simplest
  billing, growing segment, owners hate admin).
- **Channels, ranked:**
  1. **Free A/R audit as the lead magnet** — competitors (BillingParadise, Medmax) use it; "here's
     exactly how much cash is stuck in >90-day A/R, no obligation." [Medmax](https://medmaxtechnologiesllc.com/medical-billing-audit-services/)
  2. **Cold outreach to practice managers, niched by specialty** — you control targeting. [Tebra](https://www.tebra.com/theintake/healthcare-reports/billing-companies/getting-paid-how-to-get-medical-billing-clients)
  3. **Specialty associations + local networks** (state psychology/therapy associations).
  4. **Referrals** — slow to start, dominant once you have 1–2 happy clients.
- **Pitch that converts:** practices switch when their current biller is unresponsive or denials
  climb — lead with the audit number, then aligned-incentive pricing.

## Artifact 5 — Delivery playbook
1. **Pre-reqs:** business entity + **HIPAA compliance** (BAAs, encryption, training — penalties to
   $1.5M/yr), E&O insurance (~$1–2K/yr), optionally a **CPB/CPC cert** (AAPC, ~$300–500) for
   credibility. [HIPAA Journal](https://www.hipaajournal.com/hipaa-compliance-and-medical-billing/)
2. **Onboard a client:** sign BAA; access their PM system or set up yours; map fee schedule + payers;
   handle credentialing/enrollment ($200–500/provider/yr).
3. **Run the cycle:** code → scrub → submit → work denials → post payments → patient statements.
4. **Report monthly:** clean-claim rate, days in A/R, net collection rate, $ recovered — the metrics
   that justify your fee and earn the referral.

## Artifact 6 — Execution roadmap (first 60 days)
- **Wk 1–2:** pick mental-health niche; form entity; stand up HIPAA-compliant tooling + one
  clearinghouse; get E&O; (optional) start CPB study.
- **Wk 3–4:** build the free-A/R-audit offer + a one-page specialty pitch; list 40 local therapy
  practices; start cold outreach.
- **Wk 5–8:** run audits for interested practices; convert 1–2 to a no-setup pilot at 6%; obsess
  over delivery; ask for a referral.
- **First validation:** one practice signs and lets you run a month of claims. That's the proof.

---

## VERDICT — did quality hold on an unfamiliar niche?
**Mostly yes — and the failure modes are exactly the ones the product must engineer around.**

**Held up well:**
- Structure and completeness were identical to Run 01 — full arc, operator-actionable, specific.
- The **best numbers came from primary/authoritative sources** the engine found unprompted: AMA on
  independent-practice decline, MGMA/Aptarro on denial rates, real pricing norms (5–8%), real
  barriers (HIPAA, credentialing, clearinghouse, certs) neither of us knew going in. On an
  unfamiliar niche, it surfaced the *non-obvious* gotchas (dual clearinghouses, easiest specialty
  to bill). That's the product earning its keep.

**Where it strained (the important part):**
- ⚠️ **Self-interested sources slipped in.** "30% more likely to hit revenue targets" and "$201,600/yr
  from switching" come from **billing-vendor marketing blogs**, not neutral data. A naive engine
  would print these as fact. I caught and flagged them *because I was reviewing* — the unattended
  product needs the **verify step to down-rank vendor/SEO-blog sources** and demand a primary cite,
  or it will confidently ship marketing fluff as research. **This is the single most important build
  requirement surfaced by this run.**
- Without domain knowledge, **I can't fully verify the medium-confidence numbers** (denial %, cost
  ranges). The product can't either — so it must **show its sources and confidence inline** (this run
  does) and never launder a blog stat into a hard claim. Honesty-of-sourcing *is* the feature.

**Comparison to Run 01:** quality held; the difference is that on a familiar niche I could sanity-
check facts, and on an unfamiliar one I'm leaning entirely on the engine's sourcing discipline. So
the unattended pipeline's value lives or dies on **source-credibility ranking + the verify gate** —
that's now the top spec item, ahead of UI or anything else.

## Net takeaway across both dogfoods
The engine produces sellable, whole-arc output on both a familiar and an unfamiliar niche. The
moat (cited research) is real but **fragile without a source-quality filter**. Build priority order:
1) source-credibility ranking + verify gate, 2) unattended prompt→artifact pipeline + cost/run,
3) everything else. Then landing-page signal before app polish.
