"""The tree vocabulary — the node kinds and per-node attachments THIS app builds with.

The engine's tree module (engine/tree.py) owns the mechanics (wiring, walks, bounds,
inheritance); this module declares what the nodes MEAN in the business-planning funnel.
Importing it registers the vocabulary — main.py and context.py both do.

The funnel's kinds, in the order a session meets them:

    idea        the operator's raw prompt — the tree's base; every spread branches under it
    brainstorm  a fork where directions were spread (the node the operator decides AT)
    option      one spread direction — an unchosen candidate until picked/merged
    refined     the converged choice: merged thesis + adversarial cull + light research skim
    fork        a pending integrate-vs-pivot decision raised by a hard-clashing steer
    section     one staged artifact of the plan build (the DEFAULT: legacy nodes carry no
                kind field, so an unmarked node is a section — that default is wire compat)

Attachments (bounded per-node histories):

    board       board reviews + chat convenes; INHERITED so a convene during the funnel
                still steers drafts and shows in exports after commit
    log         build receipts (registered by the engine itself; stays with its node)
    decisions   ids of the standing decisions (app/decisions.py) in force when the node was
                built — stamped at creation so editing/removing a decision can name the steps
                it shaped; never inherited (each node records its OWN build-time truth)

Future relationships — decisions, blockers, linked sub-trees — are new kinds or new
attachments declared HERE, not new tree mechanics.
"""

from engine import tree

tree.set_default_kind("section")

tree.register_kind("idea", label="The idea",
                   desc="the operator's raw input — the tree's base")
tree.register_kind("brainstorm", label="Directions explored",
                   desc="a fork where directions were spread")
tree.register_kind("option",
                   desc="one spread direction (title comes from the direction itself)")
tree.register_kind("refined", label="Refined idea",
                   desc="the converged choice — merged thesis + cull + research skim")
tree.register_kind("fork", label="Fork",
                   desc="a pending integrate-vs-pivot decision")
tree.register_kind("section",
                   desc="one staged artifact of the plan build (default/legacy kind)")

BOARD = tree.register_attachment(
    "board", max_items=12, inherit=True,
    desc="board reviews + chat convenes — the advisory trail follows the branch")

DECISIONS = tree.register_attachment(
    "decisions", max_items=30, inherit=False,
    desc="ids of the standing decisions in force when this node was built — the reference "
         "trail the revisit-a-decision modal walks (stamped at creation, never inherited)")
