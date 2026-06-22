# Dev workflow — branch → PR → CI → merge

This is how a multi-developer team (each with their own Claude Code) ships changes without the merge
pain. The golden rule: **`main` is always green and deployable; everything else happens on a branch
behind a PR that CI has to pass.**

## Why

Branches that never run CI hide integration bugs until they hit `main`. (We learned this the hard
way: two devs rebuilt the same Atrium screen, and a third's route met a fourth's test only at merge
time — CI on `main` caught it, but only *after* it landed.) PRs run CI *before* merge, so conflicts
and breakages surface early, on the branch, where they're cheap to fix.

## 1. Start from a fresh main

```powershell
git switch main
git pull origin main
```

Pulling first means your branch starts from everyone else's latest — the single biggest reducer of
merge conflicts.

## 2. Work, then push to YOUR machine's branch

Each machine gets its own branch so two people never push to the same one. Set your name once:

```powershell
.\tools\push-branch.ps1 -Dev alex        # remembers it in tools/.devname (gitignored)
```

After that, just push whenever you want to share work or open a PR:

```powershell
.\tools\push-branch.ps1                      # -> branch alex/work
.\tools\push-branch.ps1 -Desc checkout-fix   # -> branch alex/checkout-fix
.\tools\push-branch.ps1 -Message "WIP nav"   # custom commit message
```

It stages everything, refuses to commit secret-looking files, and force-with-lease pushes your
branch (safe — it only updates *your* branch).

## 3. Open a Pull Request to `main`

On GitHub, open a PR from your branch into `main`. **CI runs automatically** (`.github/workflows/ci.yml`):

- esprima JS gate on every dashboard/template (`tools/_validate_dash_js.py`)
- `py_compile` on all portal modules
- the off-cloud Atrium tests (`_workspace_localtest.py`, `_atrium_smoketest.py`)

A red PR cannot merge. Fix it on the branch and push again.

## 4. Integrate + ship — the agent-driven release SOP

When branches are ready, **drop `tools/merge-branches.ps1` into Claude Code and ask it to merge +
deploy.** Claude is the human-in-the-loop; the script does the deterministic work and runs the whole
pipeline to live:

```powershell
.\tools\merge-branches.ps1            # integrate -> CI -> land on main -> deploy changed services -> prune
.\tools\merge-branches.ps1 -DryRun    # preview the land+deploy plan, change nothing
```

In one run it: commits + pushes your local work to your dev branch, fetches every per-machine branch,
merges the clean ones onto a throwaway `integration/merge`, runs the CI tests locally, **lands the
result on `main`**, **auto-detects which services changed and deploys each** (it maps each changed path
to its deploy script — portal → `deploy_dash_platform.ps1`, `clients/<c>/dash/` → that client's dash
deploy, ingest/status likewise), then **prunes** the dev branches now contained in `main`.

It **stops only where judgment is needed** — the first real merge conflict or a red CI test:

- **Conflict** → it aborts that one merge (the clean ones stay on `integration/merge`) and hands off.
  **Claude resolves it semantically** (preserving *both* devs' intent — e.g. two people who rebuilt the
  same screen), commits, and re-runs the script.
- **Red test** → Claude fixes the failure on the integrated tree, then re-runs. **Never** bypass CI.

Opt-outs: `-NoPush` (integrate + test, then stop for review — prints the manual land/deploy commands),
`-NoDeploy` (land but don't deploy), `-NoPrune` (skip cleanup), `-DeleteMerged` (standalone prune of
branches already in `main` — can never drop unmerged work).

> **Branch protection vs. direct land:** this flow pushes straight to `main`. If you enable GitHub
> branch protection (step 5 below) it will block that — then run with `-NoPush` and merge via PR, or
> leave protection off and rely on the local CI gate the script runs before landing.

## 5. Make CI required (one-time, GitHub UI)

Settings → Branches → add a protection rule for `main`:

- ✅ Require a pull request before merging (≥1 approval)
- ✅ Require status checks to pass → select the **test** check
- ✅ Require branches to be up to date before merging

After this, nobody — human or AI — can merge red or stale code into `main`.

## Conventions that keep merges clean

- **Pull `main` before you branch.** Stale bases cause most conflicts.
- **Small, focused PRs** beat one giant branch — they conflict less and review faster.
- **Don't all edit the same file.** If two people must touch `services/portal/dash/main.py` or a
  shared template, say so up front — that's where real conflicts live.
- **Let CI gate it.** If it's red, it doesn't merge. No exceptions.
