#!/usr/bin/env python3
"""
Gibberish gate — a tiny, stdlib-only "is this even an idea?" check for the intake box.

Why: an unmetered run costs real compute (the cost-discipline invariant). If someone keyboard-mashes
"asldkfj alskdjf lkasjdf", we'd rather not spend a Haiku fan-out + Sonnet synthesis on it — and it's
funnier to hand them a pre-rolled roast instead. This runs BEFORE any LLM call or session create, so
a flagged input costs $0.

Design bias: **only flag total nonsense.** Poor spelling, bad grammar, jargon, acronyms, and terse-
but-real ideas must all pass — we are never mean about a real (if rough) idea. The heuristics are
deliberately conservative: a real sentence has stopwords; acronyms are short; real words rarely stack
4+ consonants or run vowel-starved. Only a pile of long, unpronounceable tokens trips it.
"""

from __future__ import annotations

import hashlib
import re

VOWELS = set("aeiouy")
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")  # keyboard rows → catch home-row mashes (qwerty, asdfgh)


def _kbd_mash(tok: str) -> bool:
    return any(tok in row or tok in row[::-1] for row in _ROWS)

# Frequent stopwords + business-idea words. Two of these present → it's a real sentence, never flag.
COMMON = {
    "a", "an", "and", "or", "but", "the", "to", "of", "for", "in", "on", "at", "with", "from", "by",
    "is", "are", "be", "do", "i", "im", "ive", "you", "we", "my", "me", "it", "this", "that", "they",
    "them", "their", "there", "how", "what", "who", "want", "wanna", "need", "help", "make", "money",
    "sell", "selling", "business", "idea", "people", "customers", "client", "clients", "app", "service",
    "online", "start", "starting", "build", "building", "like", "good", "get", "can", "new", "about",
    "would", "could", "should", "have", "has", "not", "dont", "into", "out", "up", "so", "if", "your",
    "our", "more", "work", "use", "using", "run", "running", "small", "local", "market", "product",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z]+", (text or "").lower())


def _bad_word(tok: str) -> bool:
    """A *long* token that looks unpronounceable. Short tokens (<5) are exempt so acronyms/initialisms
    (crm, hvac, saas, b2b, ai, seo) and short real words never count against the input."""
    if len(tok) < 5:
        return False
    if _kbd_mash(tok):                             # contiguous keyboard-row slice → mash
        return True
    if not any(c in VOWELS for c in tok):          # no vowel at all → mash
        return True
    run = mx = 0                                   # longest consonant run
    for c in tok:
        run = 0 if c in VOWELS else run + 1
        mx = max(mx, run)
    if mx >= 4:                                    # 4+ stacked consonants → mash
        return True
    if sum(c in VOWELS for c in tok) / len(tok) < 0.25:  # vowel-starved → mash
        return True
    return False


def looks_like_gibberish(text: str) -> bool:
    """True only for total nonsense. See module docstring for the (intentionally lenient) rules."""
    toks = _tokens(text)
    if not toks:                                   # all symbols/numbers, no letters
        return True
    if sum(1 for t in toks if t in COMMON) >= 2:   # a real sentence has stopwords → never flag
        return False
    longs = [t for t in toks if len(t) >= 5]
    if len(longs) < 3:                             # not enough to judge — except one long unbroken mash
        return len(toks) == 1 and len(toks[0]) >= 12 and _bad_word(toks[0])
    return sum(1 for t in longs if _bad_word(t)) / len(longs) >= 0.6


# Pre-rolled roasts. Edgy-absurd, never cruel about anything real. Picked deterministically by input
# hash so the same mash always gets the same roast (no slot-machine re-rolls).
ROASTS = [
    {"title": "Your business plan is ready 🎉",
     "body": "**The vision:** you enjoy wasting free API tokens and don't take your future "
             "seriously.\n\n**Go-to-market:** keep mashing the keyboard and hope a unicorn falls out.\n\n"
             "**Unit economics:** $0 revenue, infinite vibes.\n\nWhen you've got a *real* idea, I'm "
             "genuinely good at this — try again. 👇"},
    {"title": "Plan complete. You're welcome. 🚀",
     "body": "**Who it's for:** people who like eating glue and find reading optional.\n\n**The offer:** "
             "a premium consultancy in pressing random keys, billed by the smudge.\n\n**Biggest risk:** "
             "the keyboard files a restraining order.\n\nDrop in an actual idea and I'll build you "
             "something you could sell. 👇"},
    {"title": "Bold pivot. Here's your roadmap. 🧠",
     "body": "**Your edge:** you got hit on the head as a child and have a passion for smelling your "
             "own farts.\n\n**Week 1:** monetize the fumes.\n\n**Week 2:** raise a seed round from other "
             "fart enthusiasts.\n\nOr — wild idea — tell me a real business and I'll grade the research "
             "for real. 👇"},
    {"title": "Strategy locked in. 📈",
     "body": "**Thesis:** you typed with your elbows and called it entrepreneurship.\n\n**Positioning:** "
             "the world's first artisanal nonsense brand.\n\n**Pricing:** whatever your cat walks across "
             "next.\n\nWhen you're ready to be serious, so am I — give me one real sentence about your "
             "idea. 👇"},
    {"title": "Congrats, founder. The deck writes itself. 💼",
     "body": "**Problem:** you have a keyboard and too much confidence.\n\n**Solution:** none, "
             "structurally.\n\n**TAM:** every other person who fell asleep on their spacebar.\n\nI can "
             "actually turn an idea into a sellable offer — so hand me one and let's go. 👇"},
]


def roast(text: str) -> dict:
    """Deterministically pick a roast for a gibberish input. Returns {title, body} (body is markdown)."""
    h = int(hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest(), 16)
    return ROASTS[h % len(ROASTS)]


if __name__ == "__main__":  # self-test (no API)
    gib = [
        "asdlfk asd fa lskdjf llaskjdflkajs dflk asdfasd lf lk asdlfk sladkf lkasdf",
        "asldkfjasldkfjaslkdfjlkasjdflkjasdf",
        "qwerty asdfgh zxcvbn poiuyt",
        "123456789012345",
    ]
    real = [
        "I like basketball, Magic the Gathering, and food, and I'm good at sales",
        "i wanna help dentists with there missed calls usign ai but dunno what to charge",
        "premium dog grooming subscription in brooklyn",
        "b2b saas crm for hvac smbs",
        "ai voice agent for dental offices",
        "teaching guitar to beginners over zoom",
    ]
    for g in gib:
        assert looks_like_gibberish(g), f"should flag: {g!r}"
    for r in real:
        assert not looks_like_gibberish(r), f"should NOT flag: {r!r}"
    assert roast("asdf")["title"] and roast("asdf") == roast("asdf")  # deterministic
    print("gibberish.py self-test OK —", len(ROASTS), "roasts; flags nonsense, spares real ideas")
