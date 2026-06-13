# Graduating `idea_to_offer` → its own repo (`filg`)

This folder is self-contained (engine depends only on the `anthropic` SDK) and ready to become its
own repository. The remote agent session is scoped to `bizdev`, so **run these on your machine.**
They preserve git history for this folder via `git subtree split`.

## History-preserving migration (recommended)
```bash
# 1. In your local bizdev clone, on the branch holding this work:
cd ~/path/to/bizdev
git checkout claude/bold-maxwell-k66vcz        # (or main, once merged)

# 2. Split this folder into its own branch, carrying its history:
git subtree split -P ideas/idea_to_offer -b filg-export

# 3. Make a fresh repo from that branch:
cd ..
mkdir filg && cd filg && git init -b main
git pull ../bizdev filg-export

# 4. Create on GitHub and push:
gh repo create samstuckey/filg --private --source=. --remote=origin --push
```

## Simpler (no history) fallback
```bash
cp -R ~/path/to/bizdev/ideas/idea_to_offer ~/filg
cd ~/filg && git init -b main && git add -A && git commit -m "FILG: initial import from bizdev launchpad"
gh repo create samstuckey/filg --private --source=. --remote=origin --push
```

## After the new repo is verified
1. In `bizdev`, leave a pointer and remove the folder:
   ```bash
   git rm -r ideas/idea_to_offer
   # replace with a one-line stub the index already references
   ```
   (Keep `docs/ideas-index.md`'s "graduated → samstuckey/filg" row.)
2. Add a `CLAUDE.md` to the new repo so Claude Code sessions there have context (can be derived from
   this README + the planning docs).
3. To let the agent work directly in `filg`, add it to a session with `add_repo` (it'll show up after
   you create it).

## Don't bring over
Nothing — it's clean. `.claude/` profile submodule and bizdev's other ideas stay in `bizdev`.
