"""In-app help copy — the product facts the help assistant is grounded in.

pricing_facts() is GENERATED from tiers.py/billing.py so the help chat can never drift from
the live ladder (a contract test rejects dead copy); system() is the full system block (the
help skill + the generated facts); MOCK_REPLY is the no-key dev answer.
"""

from __future__ import annotations

from app import billing, tiers
from app import skill_registry as skills


def pricing_facts() -> str:
    """The PRICING FACTS block for the help prompt, GENERATED from tiers.py + billing.py — the single
    sources of truth — so help can never drift from the live ladder again. (The 2026-07-06 QA caught
    help quoting the dead '$13 one-time, no subscription' model from a hardcoded prompt.)"""
    price = billing.PDF_PRICE_CENTS / 100
    lines = [
        "PRICING FACTS (answer any cost/subscription question ONLY from these, never from memory):",
        "- Getting started is free, no account: your idea spreads into directions and merges into a "
        "refined, first-pass-researched idea on the house. Building the full plan (the deep research "
        "step) is where an account comes in — sign up, then either bring your own key or subscribe. "
        "A plan without an account is cleaned up after about 48 hours.",
        "- Free on your own API key (OpenRouter or Anthropic, BYOK): unlimited use, every model crew, "
        "every feature. Model usage bills to their key, typically well under a dollar per plan.",
        f"- Raw export (.zip/.md) is always free. The polished investor-grade PDF: free WITH a small "
        f"'Built with FILG' watermark on your own key, or a one-time ${price:g} unlocks THAT plan's "
        "clean (watermark-free) PDF — re-downloading it is free, a new plan pays its own unlock. "
        "Every subscription includes unlimited clean PDFs.",
    ]
    cat = tiers.catalog()
    up = ("upgrade for a bigger allowance, add your own key as a fallback, or wait for the renewal"
          if len(cat) > 1 else "add your own key as a fallback, or wait for the renewal")
    lines.append(
        "- The monthly subscription runs on FILG's hosted key (no API key needed) and unlocks every "
        "feature and every model crew. It has a monthly usage allowance that resets with the "
        f"billing period; hitting it means {up}:")
    for t in cat:
        lines.append(f"  * {t['label']}: ${t['price']:g}/mo — all features and model crews, "
                     f"unlimited clean PDFs, ~${t['cap_cents'] / 100:g}/mo of included model usage.")
    return "\n".join(lines)


def system() -> str:
    """The help system block: the `help` SKILL (app/skills/help/SKILL.md — how-to, money-answer rules,
    the not-authoritative-on-pricing/legal disclaimer) + the PRICING FACTS generated from the live
    ladder. Skill = judgment and rules; generated block = numbers. Neither can drift alone."""
    return skills.system("help") + "\n\n" + pricing_facts()

MOCK_REPLY = ("This is mock help (no key bound). In the real app: type your idea on the home page, then "
              "use 'I'm with you' to lock each part and build the next, or 'Not feeling it' to redo a part. "
              "It runs on your own API key.")
