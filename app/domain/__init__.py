"""The business-planning domain — everything that makes this app THIS product.

The engine (research, grading, the decision tree, providers, metering) is use-case-agnostic;
this package is the plug that turns it into a business-plan builder. It owns:

  nodes     the tree vocabulary — node kinds + per-node attachments this app registers
  sections  the staged build spec — the 7 plan sections, their guides, and section copy
  copy      canned domain copy — mock drafts, waste-of-time mode, roasts' vocabulary

A sibling product on the same engine (see the wedding-planner fork) swaps this package,
the skills/ prompt files, and personas.py — and keeps everything else.
"""
