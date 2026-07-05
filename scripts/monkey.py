#!/usr/bin/env python3
"""The interaction monkey — seeded random-walk UI testing for /v2 (2026-07-06, Sam's ask).

Humans break the app with "inexplicable random interaction chains": open node A, arm a pivot,
switch to the board, type something, browse an earlier node, arm a pivot somewhere else, reload
mid-build. Scripted happy paths never catch those. This harness does what the human does — a
SEEDED random walk over the real click vocabulary — and after EVERY action asserts the invariants
that must hold no matter what order things happened in.

Run (mock server recommended — fast + free):
    FILG_MOCK=1 FILG_DB=/tmp/monkey.db uvicorn app.main:app --port 8600 &
    python scripts/monkey.py --base http://127.0.0.1:8600 --seed 7 --steps 60
    python scripts/monkey.py --chains            # the known-nasty fixed chains instead of random
    python scripts/monkey.py --seeds 1,2,3       # several seeds in one go

A failure prints the seed + the full action log (replayable: same seed = same walk) and saves a
screenshot next to this script. Exit code 1 on any invariant break.

INVARIANTS (the contract, independent of action order):
  I1  no JS page errors
  I2  exactly one mode pill lit; a lit display (RMODE/BMODE/HMODE) matches its pill
  I3  an armed pivot ghost implies build mode + the ghost node in the DOM
  I4  a focused node's body is never empty (the transitional-body contract)
  I5  a WIP box implies work in flight (WIP_LABEL or status=researching); none when idle
  I6  the server tree is coherent: active exists in nodes; a brainstorm active renders checkboxes
      when focused + idle
  I7  the send button recovers (not stuck disabled) once quiescent
  I8  at most one WIP box at a time
"""

import argparse
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
IDEA = "i want to bake cookies with ex cons"
PROMPTS = [
    "make it more premium",
    "what does the board think about pricing?",
    "how big is this market?",
    "add a holiday angle to it",
    "keep going",
    "where are we at?",
]


class Monkey:
    def __init__(self, pg, log):
        self.pg = pg
        self.log = log
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))

    # ── plumbing ──────────────────────────────────────────────────────────────
    def note(self, msg):
        self.log.append(msg)

    def js(self, expr):
        return self.pg.evaluate(expr)

    def settle(self, timeout=45.0):
        """Wait until nothing is in flight: no researching status, no send spinner, no WIP label."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            busy = self.js(
                "!!(typeof S!=='undefined'&&S&&S.status==='researching')||"
                "!!(typeof WIP_LABEL!=='undefined'&&WIP_LABEL)||"
                "!!document.querySelector('.sendspin')")
            if not busy:
                return
            time.sleep(0.25)
        raise AssertionError("never settled — something is stuck in flight (I5/I7 family)")

    # ── the invariant gauntlet (runs after EVERY action) ─────────────────────
    def check(self):
        if self.errors:
            raise AssertionError("I1 JS page error: " + "; ".join(self.errors[:3]))
        lit = self.js("document.querySelectorAll('#modechips .mchip.on').length")
        if lit != 1:
            raise AssertionError(f"I2 {lit} mode pills lit")
        mode_ok = self.js(
            "(function(){const m=document.querySelector('#modechips .mchip.on').dataset.mode;"
            "const r=typeof RMODE!=='undefined'&&RMODE,b=typeof BMODE!=='undefined'&&BMODE,"
            "h=typeof HMODE!=='undefined'&&HMODE;"
            "const displays=[r?'research':null,b?'board':null,h?'help':null].filter(Boolean);"
            "if(displays.length>1)return 'two displays live: '+displays.join('+');"
            "if(displays.length===1&&displays[0]!==m)return 'display '+displays[0]+' vs pill '+m;"
            "return '';})()")
        if mode_ok:
            raise AssertionError("I2 " + mode_ok)
        ghost = self.js(
            "(function(){const armed=typeof PIVOT_FROM!=='undefined'&&PIVOT_FROM;"
            "const g=!!document.querySelector('.gnode.ghost');"
            "if(armed&&!g&&VIEWMODE==='graph')return 'pivot armed but no ghost node';"
            "if(armed&&MODE!=='build')return 'pivot armed outside build mode';"
            "return '';})()")
        if ghost:
            raise AssertionError("I3 " + ghost)
        empty = self.js(
            "(function(){const f=document.querySelector('.gnode.focus');if(!f)return '';"
            "const b=f.querySelector('.nbody');"
            "if(b&&b.innerText.trim().length<3)return 'focused node body is empty';return '';})()")
        if empty:
            raise AssertionError("I4 " + empty)
        wips = self.js("document.querySelectorAll('.gnode.wip').length")
        if wips > 1:
            raise AssertionError(f"I8 {wips} WIP boxes at once")
        busy = self.js(
            "!!(typeof S!=='undefined'&&S&&S.status==='researching')||"
            "!!(typeof WIP_LABEL!=='undefined'&&WIP_LABEL)")
        if wips and not busy:
            raise AssertionError("I5 a WIP box with nothing in flight")
        tree_ok = self.js(
            "(function(){if(typeof S==='undefined'||!S||!S.tree)return '';"
            "const ids=new Set(S.tree.nodes.map(n=>n.id));"
            "if(S.tree.active&&!ids.has(S.tree.active))return 'active not in nodes';"
            "return '';})()")
        if tree_ok:
            raise AssertionError("I6 " + tree_ok)
        # I6b: an idle, focused, ACTIVE brainstorm must offer its checkboxes (the kind-first contract)
        boxes_ok = self.js(
            "(function(){if(typeof S==='undefined'||!S||!S.activeNode)return '';"
            "if(S.status==='researching'||(typeof WIP_LABEL!=='undefined'&&WIP_LABEL))return '';"
            "if(S.activeNode.kind!=='brainstorm')return '';"
            "const f=document.querySelector('.gnode.focus');if(!f)return '';"
            "if(typeof FOCUS==='undefined'||FOCUS!==S.tree.active)return '';"
            "if(!f.querySelector('input[type=checkbox]'))return 'active brainstorm focused w/o checkboxes';"
            "return '';})()")
        if boxes_ok:
            raise AssertionError("I6 " + boxes_ok)
        sendable = self.js("(function(){const b=$('ws-send');return b&&!b.disabled;})()")
        if not sendable:
            raise AssertionError("I7 send button stuck disabled while quiescent")

    # ── the click vocabulary (each guards its own availability) ──────────────
    def act_click_node(self, rng):
        ids = self.js("Array.from(document.querySelectorAll('.gnode')).map(e=>e.dataset.id).filter(Boolean)")
        if not ids:
            return False
        nid = rng.choice(ids)
        self.note(f"click node {nid}")
        self.js(f"gNodeClick({nid!r})")
        return True

    def act_escape(self, rng):
        self.note("escape")
        self.pg.keyboard.press("Escape")
        return True

    def act_pivot_here(self, rng):
        has = self.js("!!(S&&S.tree&&S.tree.active)")
        if not has:
            return False
        self.note("arm pivot on active")
        self.js("pivotActive()")
        return True

    def act_pivot_random(self, rng):
        ids = self.js("(S&&S.tree)?S.tree.nodes.map(n=>n.id):[]")
        if not ids:
            return False
        nid = rng.choice(ids)
        self.note(f"arm pivot on {nid}")
        self.js(f"pivotFromHere({nid!r})")
        return True

    def act_mode(self, rng):
        m = rng.choice(["build", "research", "board", "help"])
        self.note(f"pill {m}")
        self.pg.click(f"#modechips button[data-mode={m}]")
        return True

    def act_prompt(self, rng):
        p = rng.choice(PROMPTS)
        self.note(f"send: {p!r}")
        self.pg.fill("#ws-box", p)
        self.pg.click("#ws-send")
        self.settle()
        # an in-chat confirm may be pending — answer it randomly, that's what humans do
        if self.js("!!document.querySelector('.cmsg:not(.asked) .cbtns .go')"):
            if rng.random() < 0.6:
                self.note("confirm: go")
                self.pg.locator(".cmsg:not(.asked) .cbtns .go").first.click()
            else:
                self.note("confirm: not yet")
                self.pg.locator(".cmsg:not(.asked) .cbtns .nah").first.click()
            self.settle()
        return True

    def act_checkbox(self, rng):
        n = self.pg.locator(".gnode.focus .opt input[type=checkbox]").count()
        if not n:
            return False
        i = rng.randrange(n)
        self.note(f"toggle checkbox {i}")
        self.pg.locator(".gnode.focus .opt input[type=checkbox]").nth(i).click()
        return True

    def act_merge(self, rng):
        if not self.pg.locator(".gnode.focus button:has-text(\"Let's try it\")").count():
            return False
        if not self.js("SEL&&SEL.size"):
            return False
        self.note("merge picks")
        self.pg.locator(".gnode.focus button:has-text(\"Let's try it\")").first.click()
        self.settle()
        return True

    def act_commit(self, rng):
        if not self.pg.locator(".gnode.focus button:has-text(\"I'm sold\")").count():
            return False
        self.note("commit (I'm sold)")
        self.pg.locator(".gnode.focus button:has-text(\"I'm sold\")").first.click()
        self.settle(90)
        return True

    def act_keep_going(self, rng):
        if not self.pg.locator(".gnode.focus button:has-text('Keep going')").count():
            return False
        self.note("keep going")
        self.pg.locator(".gnode.focus button:has-text('Keep going')").first.click()
        self.settle(90)
        return True

    def act_expand_tuck(self, rng):
        if self.js("typeof RMODE!=='undefined'&&RMODE==='split'"):
            self.note("expand research")
            self.js("expandResearch()")
        elif self.js("typeof RMODE!=='undefined'&&RMODE==='expanded'"):
            self.note("tuck research")
            self.js("collapseResearch()")
        elif self.js("typeof BMODE!=='undefined'&&BMODE==='split'"):
            self.note("expand board")
            self.js("expandBoard()")
        else:
            return False
        return True

    def act_seat(self, rng):
        n = self.pg.locator("#bseats .bseat").count()
        if not n or not self.js("BMODE"):
            return False
        i = rng.randrange(n)
        self.note(f"toggle seat {i}")
        self.pg.locator("#bseats .bseat").nth(i).click()
        return True

    def act_graph_keys(self, rng):
        k = rng.choice(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "f"])
        self.note(f"key {k}")
        self.js("document.activeElement&&document.activeElement.blur()")
        self.pg.keyboard.press(k)
        return True

    def act_docs_tab(self, rng):
        v = rng.choice(["docs", "graph"])
        self.note(f"view {v}")
        self.js(f"setView({v!r})")
        return True

    def act_reload(self, rng):
        if not self.js("!!SID"):
            return False
        self.note("RELOAD")
        self.pg.reload()
        self.pg.wait_for_selector(".gnode", timeout=20000)
        self.settle()
        return True

    ACTIONS = [  # (weight, method)
        (5, "act_click_node"), (3, "act_escape"), (2, "act_pivot_here"),
        (2, "act_pivot_random"), (4, "act_mode"), (5, "act_prompt"),
        (3, "act_checkbox"), (2, "act_merge"), (2, "act_commit"),
        (2, "act_keep_going"), (2, "act_expand_tuck"), (1, "act_seat"),
        (2, "act_graph_keys"), (1, "act_docs_tab"), (1, "act_reload"),
    ]

    def walk(self, rng, steps):
        bag = [m for w, m in self.ACTIONS for _ in range(w)]
        for i in range(steps):
            m = rng.choice(bag)
            try:
                did = getattr(self, m)(rng)
            except Exception as e:  # noqa: BLE001 — an action that throws is itself a finding
                raise AssertionError(f"action {m} exploded: {e}") from e
            if not did:
                continue
            self.settle()
            self.check()


# ── the known-nasty fixed chains (Sam's reports, replayed exactly) ────────────
def chain_pivot_board_pivot(mk, rng):
    """open a node → arm pivot → board mode + a convene → browse an earlier node → pivot elsewhere."""
    mk.act_click_node(rng)
    mk.act_pivot_here(rng)
    mk.pg.click("#modechips button[data-mode=board]")
    mk.pg.fill("#ws-box", "what does the board think?")
    mk.pg.click("#ws-send")
    mk.settle()
    mk.check()
    mk.act_click_node(rng)
    mk.act_pivot_random(rng)
    mk.settle()
    mk.check()


def chain_feedback_pick_roll(mk, rng):
    """feedback on the fork → pick two → merge → roll forward (the freeze report)."""
    mk.pg.fill("#ws-box", "lean corporate, keep the story")
    mk.pg.click("#ws-send")
    mk.settle()
    if mk.pg.locator(".gnode.focus .opt input[type=checkbox]").count() >= 2:
        mk.pg.locator(".gnode.focus .opt input[type=checkbox]").nth(0).click()
        mk.pg.locator(".gnode.focus .opt input[type=checkbox]").nth(1).click()
        mk.act_merge(rng)
    mk.check()
    mk.pg.fill("#ws-box", "keep going")
    mk.pg.click("#ws-send")
    mk.settle()
    if mk.js("!!document.querySelector('.cmsg:not(.asked) .cbtns .go')"):
        mk.pg.locator(".cmsg:not(.asked) .cbtns .go").first.click()
        mk.settle(90)
    mk.check()


def chain_reload_midstates(mk, rng):
    """reload at every stage: brainstorm w/ picks, mid-research display, board expanded."""
    if mk.pg.locator(".gnode.focus .opt input[type=checkbox]").count():
        mk.pg.locator(".gnode.focus .opt input[type=checkbox]").first.click()
    mk.act_reload(rng)
    mk.check()
    mk.pg.click("#modechips button[data-mode=research]")
    mk.act_reload(rng)
    mk.check()
    mk.pg.click("#modechips button[data-mode=board]")
    mk.js("expandBoard()")
    mk.act_reload(rng)
    mk.check()


def chain_pivot_pick_sold(mk, rng):
    """pivot from the landed step -> single option -> check it -> 'I'm sold' (skip the merge).
    The checked option must join the committed path, not render passed-over (Sam's QA)."""
    mk.act_pivot_here(rng)
    mk.pg.fill("#ws-box", "add a cause tie-in where proceeds fund at-risk youth orgs")
    mk.pg.click("#ws-send")
    mk.settle()
    mk.check()
    if mk.pg.locator(".gnode.focus .opt input[type=checkbox]").count():
        mk.pg.locator(".gnode.focus .opt input[type=checkbox]").first.click()
        if mk.pg.locator(".gnode.focus button:has-text(\"I'm sold\")").count():
            mk.pg.locator(".gnode.focus button:has-text(\"I'm sold\")").first.click()
            mk.settle(90)
            mk.check()
            picked_on_path = mk.js(
                "(function(){const {m,t}=nodesOf(S);const set=pathSetOf(m,t.active);"
                "return Object.values(m).some(n=>n.kind==='option'&&set[n.id]);})()")
            if not picked_on_path:
                raise AssertionError("chain: checked option not on the committed path after direct commit")


CHAINS = [chain_pivot_board_pivot, chain_feedback_pick_roll, chain_reload_midstates,
          chain_pivot_pick_sold]


def run_one(base, seed, steps, chains_only, headed=False):
    log = []
    rng = random.Random(seed)
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", headless=not headed)
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        mk = Monkey(pg, log)
        try:
            pg.goto(base + "/v2")
            pg.fill("#ws-box", IDEA)
            pg.click("#ws-send")
            pg.wait_for_selector(".gnode.on", timeout=30000)
            mk.settle()
            mk.check()
            if chains_only:
                for ch in CHAINS:
                    log.append(f"── chain {ch.__name__}")
                    ch(mk, rng)
            else:
                mk.walk(rng, steps)
            print(f"seed {seed}: {'chains' if chains_only else f'{steps} steps'} clean ✓")
            return True
        except Exception as e:  # noqa: BLE001
            shot = HERE / f"monkey_fail_seed{seed}.png"
            try:
                pg.screenshot(path=str(shot))
            except Exception:  # noqa: BLE001
                pass
            print(f"\nseed {seed} FAILED: {e}\naction log ({len(log)} steps):")
            for line in log[-30:]:
                print("  ", line)
            print(f"screenshot: {shot}\nreplay: python scripts/monkey.py --seed {seed}"
                  + (" --chains" if chains_only else f" --steps {steps}"))
            return False
        finally:
            b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8600")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seeds", default="")           # e.g. "1,2,3"
    ap.add_argument("--steps", type=int, default=60)
    ap.add_argument("--chains", action="store_true")  # the fixed known-nasty chains
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    seeds = [int(x) for x in a.seeds.split(",") if x] or [a.seed]
    ok = all(run_one(a.base, s, a.steps, a.chains, a.headed) for s in seeds)
    sys.exit(0 if ok else 1)
