# =============================================================================
# merge-branches.ps1 -- integrate the per-machine dev branches, land them on main,
#                       and DEPLOY every changed service to live. One command, end to
#                       end. The agent (Claude Code) is the human-in-the-loop: this
#                       script does the deterministic, mechanical work and STOPS for the
#                       agent only where judgment is needed (a real merge conflict, a red
#                       test). Everything else -- land + auto-detect-and-deploy + prune --
#                       is automatic.
#
# == AGENT RUNBOOK (read this when a developer drops this file into Claude Code) =======
#   The team SOP is: a developer pastes the path to this script into Claude Code and asks
#   you to "merge + deploy". You (the agent) DRIVE THE WHOLE PIPELINE TO LIVE:
#
#     1. Run it:   .\tools\merge-branches.ps1
#        (optionally .\tools\merge-branches.ps1 -DryRun FIRST to print the land+deploy
#         plan and change nothing -- good when you're unsure what will ship.)
#
#     2. If it STOPS on a MERGE CONFLICT: it has aborted that one branch's merge and left
#        the clean merges on `integration/merge`. Open the conflicting files, resolve them
#        SEMANTICALLY -- preserve BOTH developers' intent (e.g. two people who rebuilt the
#        same screen); never just pick one side blindly -- commit the resolution, then
#        re-run this script. It will pick up from a clean state.
#
#     3. If it STOPS on a RED CI TEST: the integrated tree fails a gate. Fix the failure
#        (on the integration result), then re-run. NEVER bypass the tests or land a red tree.
#
#     4. On success it has ALREADY: landed `integration/merge` into `main` (fast-forward,
#        pushed), deployed every service whose files changed (see the mapping below), and
#        pruned the dev branches now contained in `main`. Report to the developer exactly
#        which services deployed and to which URLs.
#
#   You are the only "judgment" in the loop -- the script never auto-resolves a conflict
#   and never lands or deploys a tree that failed CI. Do not work around those stops.
# =====================================================================================
#
# WHAT IT DOES (default, no flags):
#   0. if your working tree has local changes, commit + push them to THIS machine's own
#      dev branch first (delegates to push-branch.ps1) so your work is integrated too.
#   1. fetch + discover every per-machine branch on origin (everything except main).
#   2. create a throwaway `integration/merge` branch off origin/main.
#   3. merge each branch in turn -- on the FIRST conflict it aborts that merge and STOPS
#      (hand off to the agent per the runbook above).
#   4. run the CI tests locally against the integrated result; STOP if anything is red.
#   5. LAND: fast-forward `main` to the integrated result and push origin main.
#   6. DEPLOY: diff the integrated result against the old main, map each changed path to
#      its deploy script, and run each (build-as-yourself -> gcloud run deploy). See the
#      path -> deploy-script mapping in Resolve-DeployPlan below.
#   7. PRUNE: delete the remote dev branches whose commits are now contained in origin/main
#      (safe by construction -- it can never drop unmerged work).
#
# FLAGS (opt out of pieces of the pipeline):
#   -DryRun        do steps 1-4 locally, then PRINT the land + deploy + prune plan and
#                  change NOTHING on origin or in production. (Reflects COMMITTED branches;
#                  commit/push local WIP first to see it in the plan.)
#   -NoPush        integrate + test, then STOP before landing (the old review-first
#                  behavior). Prints the manual land/deploy commands.
#   -NoDeploy      land to main, but do NOT deploy the changed services (deploy later).
#   -NoPrune       skip the branch cleanup at the end.
#   -Exclude a,b   skip specific dev branches (comma-separated).
#   -DeleteMerged  standalone: ONLY prune remote branches already contained in origin/main
#                  (runs nothing else). Unchanged from the original tool.
#
# USAGE
#   .\tools\merge-branches.ps1                  # integrate -> land -> deploy -> prune
#   .\tools\merge-branches.ps1 -DryRun          # preview the whole plan, change nothing
#   .\tools\merge-branches.ps1 -NoPush          # integrate + test, then stop for review
#   .\tools\merge-branches.ps1 -Exclude alex/wip
#   .\tools\merge-branches.ps1 -DeleteMerged    # prune-only
# =============================================================================

param(
    [string]$Exclude = "",
    [switch]$DeleteMerged,
    [switch]$NoPush,
    [switch]$NoDeploy,
    [switch]$NoPrune,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
function Die([string]$m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Must([string]$w) { if ($LASTEXITCODE -ne 0) { Die "$w (exit $LASTEXITCODE)" } }

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path   # tools/ -> repo root
Set-Location $repo

$origBranch = (git rev-parse --abbrev-ref HEAD 2>$null)   # remembered so -DryRun can restore it

# -DryRun integrates locally; it never commits your WIP, so a dirty tree would block the
# integration checkout. Require a clean tree for the preview (the real run commits first).
if ($DryRun -and -not [string]::IsNullOrWhiteSpace((git status --porcelain))) {
    Die "-DryRun needs a clean working tree (it won't commit your changes). Commit/stash first, or run without -DryRun (the live flow commits your WIP to your dev branch automatically)."
}

# -----------------------------------------------------------------------------
# Map the files that changed in this merge to the deploy script(s) that ship them.
# This is the SINGLE SOURCE OF TRUTH for "what gets deployed when X changes". Each
# changed path matches at most one rule; the scripts are deduped and run in `Prio`
# order (client SQL -> job -> dash, then portal, then ingest/status). Paths that map
# to nothing (docs, tools, assets, repo-root files) are correctly ignored.
# -----------------------------------------------------------------------------
function Resolve-DeployPlan {
    # Returns an ARRAY (possibly empty) of @{Service; Script; Prio}, deduped by Script,
    # sorted by Prio. NO closures / no outer-state mutation -- each rule just EMITS a row
    # to the pipeline (the closure-mutates-an-ArrayList pattern silently no-ops when this
    # function runs inside the larger script, so it is deliberately avoided here).
    param([string[]]$Changed, [string]$RepoRoot)

    # For a client path clients/<c>/<sub>/..., find the deploy_*.ps1 in that dir by glob
    # (works for any client key without re-typing it). Returns a row, or $null if none.
    function ClientRow([string]$c, [string]$sub, [string]$pattern, [int]$prio) {
        $dir = Join-Path $RepoRoot "clients/$c/$sub"
        if (-not (Test-Path $dir)) { return $null }
        $f = Get-ChildItem -Path $dir -Filter $pattern -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return [pscustomobject]@{ Service = "client '$c' ($sub)"; Script = $f.FullName; Prio = $prio } }
        Write-Host "    [skip] client '$c' $sub changed but no $pattern in $dir" -ForegroundColor Yellow
        return $null
    }

    $rows = foreach ($cf in $Changed) {
        $p = ($cf -replace '\\', '/')
        if     ($p -match '^services/portal/')                { [pscustomobject]@{ Service = 'platform-dash (portal + Atrium)';   Script = (Join-Path $RepoRoot 'services/portal/dash/deploy_dash_platform.ps1');     Prio = 50 } }
        elseif ($p -match '^services/ingest/')                { [pscustomobject]@{ Service = 'ingest jobs (raw_windsor writers)'; Script = (Join-Path $RepoRoot 'tools/deploy_ingest_jobs.ps1');                     Prio = 60 } }
        elseif ($p -match '^services/status-dashboard/dash/') { [pscustomobject]@{ Service = 'status-dash (web)';                  Script = (Join-Path $RepoRoot 'services/status-dashboard/dash/deploy_dash_status.ps1'); Prio = 70 } }
        elseif ($p -match '^services/status-dashboard/job/')  { [pscustomobject]@{ Service = 'status-export (job)';                Script = (Join-Path $RepoRoot 'services/status-dashboard/job/deploy_job_status.ps1');   Prio = 65 } }
        elseif ($p -match '^clients/([^/]+)/sql/')            { ClientRow $Matches[1] 'sql'  'deploy_views_*.ps1' 10 }
        elseif ($p -match '^clients/([^/]+)/job/')            { ClientRow $Matches[1] 'job'  'deploy_job_*.ps1'   20 }
        elseif ($p -match '^clients/([^/]+)/dash/')           { ClientRow $Matches[1] 'dash' 'deploy_dash_*.ps1'  30 }
        # else: docs/, tools/, assets/, preview/, repo-root files -> nothing to deploy.
    }

    # Drop nulls, keep only deploy scripts that exist, dedupe by Script path, sort by Prio.
    $seen = @{}
    $out = foreach ($r in (@($rows) | Where-Object { $_ } | Sort-Object Prio)) {
        if (-not (Test-Path $r.Script)) { Write-Host "    [skip] $($r.Service) -- deploy script missing: $($r.Script)" -ForegroundColor Yellow; continue }
        if ($seen.ContainsKey($r.Script)) { continue }
        $seen[$r.Script] = $true
        $r
    }
    return @($out)
}

# Per-machine dev branches whose commits are ALREADY in origin/main (safe to delete).
# CRITICAL: exclude the origin/HEAD symref -- its `:short` form is the bare "origin",
# which is not a real branch; trying to delete it errors out and aborts the prune.
function Get-MergedDevBranches([string[]]$Skip) {
    git branch -r --merged origin/main --format='%(refname:short)' |
        Where-Object { $_ -and $_ -ne 'origin/HEAD' -and $_ -ne 'origin' -and $_ -notlike '*->*' } |
        ForEach-Object { ($_ -replace '^origin/', '').Trim() } |
        Where-Object { $_ -and ($Skip -notcontains $_) }
}

# Delete remote branches one at a time, never aborting the batch if one is already gone.
function Remove-RemoteBranches([string[]]$Branches) {
    foreach ($b in $Branches) {
        git push origin --delete $b 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "    deleted origin/$b" -ForegroundColor Yellow }
        else { Write-Host "    [warn] could not delete origin/$b (already gone?)" -ForegroundColor Yellow }
    }
}

# =============================================================================
# -DeleteMerged is a standalone, GATED cleanup -- it never runs the merge.
# =============================================================================
$skip = @("main", "HEAD") + (($Exclude -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })

if ($DeleteMerged) {
    Write-Host "[..] Fetching origin" -ForegroundColor Cyan
    git fetch origin --prune; Must "git fetch"
    Write-Host "[..] Deleting remote branches already contained in origin/main" -ForegroundColor Cyan
    $alreadyMerged = @(Get-MergedDevBranches $skip)
    if (-not $alreadyMerged) { Write-Host "    (none are fully merged into main yet -- nothing to delete)" -ForegroundColor Yellow; exit 0 }
    Remove-RemoteBranches $alreadyMerged
    Write-Host "[OK] pruned: $($alreadyMerged -join ', ')" -ForegroundColor Green
    exit 0
}

# =============================================================================
# 0. Capture any local working changes BEFORE we touch branches (commit + push to this
#    machine's own dev branch via push-branch.ps1) so they get integrated below.
#    Skipped under -DryRun (DryRun must not mutate the remote).
# =============================================================================
if ($DryRun) {
    Write-Host "[dry-run] NOT pushing local changes -- the plan reflects COMMITTED branches only." -ForegroundColor Yellow
    if (-not [string]::IsNullOrWhiteSpace((git status --porcelain))) {
        Write-Host "[dry-run] (you have uncommitted changes; commit/push them to see them in the plan)" -ForegroundColor Yellow
    }
} elseif (-not [string]::IsNullOrWhiteSpace((git status --porcelain))) {
    Write-Host "[..] Local changes detected -- committing + pushing them to your branch first" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "push-branch.ps1")
    Must "push-branch (commit + push local changes)"
    Write-Host "[OK] local work pushed -- it will be integrated below" -ForegroundColor Green
}

# =============================================================================
# 1. Fresh view of remotes, then discover the dev branches (origin/* minus main/HEAD).
# =============================================================================
Write-Host "[..] Fetching origin" -ForegroundColor Cyan
git fetch origin --prune; Must "git fetch"

$baseMain = (git rev-parse origin/main).Trim()   # the main we are integrating ON TOP OF
Must "resolve origin/main"

$branches = git branch -r --format='%(refname:short)' |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -like 'origin/*' } |
    ForEach-Object { $_ -replace '^origin/', '' } |
    Where-Object { $_ -and ($skip -notcontains $_) }

if (-not $branches) { Write-Host "[OK] no dev branches to merge -- main is already current." -ForegroundColor Green; exit 0 }
Write-Host "[OK] branches to integrate: $($branches -join ', ')"

# =============================================================================
# 2. Throwaway integration branch off the CURRENT origin/main.
# =============================================================================
$intg = "integration/merge"
Write-Host "[..] Creating $intg off origin/main" -ForegroundColor Cyan
git switch -C $intg origin/main; Must "create $intg"

# =============================================================================
# 3. Merge each branch; STOP on the first conflict (hand off to the agent per runbook).
# =============================================================================
$merged = @()
foreach ($b in $branches) {
    Write-Host "[..] Merging $b" -ForegroundColor Cyan
    git merge --no-ff -m "Merge $b into $intg" "origin/$b"
    if ($LASTEXITCODE -ne 0) {
        git merge --abort
        Write-Host ""
        Write-Host "[CONFLICT] $b does not merge cleanly -- aborted that merge." -ForegroundColor Red
        Write-Host "  Already integrated cleanly: $($merged -join ', ')" -ForegroundColor Yellow
        Write-Host "  AGENT: resolve this branch's conflict semantically (preserve BOTH devs' intent)," -ForegroundColor Yellow
        Write-Host "         commit, then re-run this script. The $intg branch holds the clean merges so far."
        exit 1
    }
    $merged += $b
}
Write-Host "[OK] all branches merged cleanly: $($merged -join ', ')" -ForegroundColor Green

# =============================================================================
# 4. Run the CI tests locally before trusting the integrated result.
# =============================================================================
Write-Host "[..] Running the off-cloud CI tests against the integrated tree" -ForegroundColor Cyan
$py = Join-Path $repo ".venv-portal\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = Join-Path $repo ".venv\Scripts\python.exe" }
if (-not (Test-Path $py)) { Die "no python venv found (.venv-portal or .venv). Run tools\setup.ps1 / preview once." }

Push-Location (Join-Path $repo "services\portal\dash")
& $py _workspace_localtest.py | Out-Null; $t1 = $LASTEXITCODE
& $py _atrium_smoketest.py    | Out-Null; $t2 = $LASTEXITCODE
Pop-Location
if ($t1 -ne 0 -or $t2 -ne 0) {
    Die "integration tests FAILED (workspace=$t1 smoke=$t2) -- do NOT land this. AGENT: fix the failure on the integrated tree, then re-run. The $intg branch holds the result."
}
Write-Host "[OK] integration tests green" -ForegroundColor Green

# =============================================================================
#    Compute the deploy plan now (used by -DryRun, -NoPush, and the live path).
# =============================================================================
$changed = git diff --name-only $baseMain $intg | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$plan = @(Resolve-DeployPlan -Changed $changed -RepoRoot $repo)   # @() => always a real array

# =============================================================================
# -NoPush / -DryRun: stop here. Print exactly what WOULD happen, change nothing live.
# =============================================================================
if ($NoPush -or $DryRun) {
    Write-Host ""
    $tag = if ($DryRun) { "[dry-run]" } else { "[no-push]" }
    Write-Host "$tag $intg is clean + green. It was NOT landed or deployed." -ForegroundColor Green
    Write-Host "$tag would LAND:   git switch main; git merge --ff-only $intg; git push origin main"
    if ($plan.Count -gt 0) {
        Write-Host "$tag would DEPLOY (changed services):"
        foreach ($s in $plan) { Write-Host "           - $($s.Service)  ->  $($s.Script)" }
    } else {
        Write-Host "$tag would DEPLOY: (nothing -- no deployable service changed)"
    }
    Write-Host "$tag would PRUNE:  dev branches once contained in main (.\tools\merge-branches.ps1 -DeleteMerged)"
    if ($DryRun) {
        # Restore the branch we started on and drop the throwaway integration branch.
        if ($origBranch -and $origBranch -ne 'HEAD' -and $origBranch -ne $intg) { git switch $origBranch *>$null } else { git switch main *>$null }
        git branch -D $intg *>$null
    }
    exit 0
}

# =============================================================================
# 5. LAND: fast-forward main to the integrated result and push.
# =============================================================================
Write-Host "[..] Landing $intg into main" -ForegroundColor Cyan
git switch main;                 Must "switch to main"
git merge --ff-only origin/main; Must "sync local main to origin/main"   # no-op if already current
git merge --ff-only $intg;       Must "fast-forward main to $intg"
git push origin main;            Must "push origin main"
Write-Host "[OK] landed -- main is now $(git rev-parse --short HEAD)" -ForegroundColor Green

# =============================================================================
# 6. DEPLOY every changed service to live (unless -NoDeploy).
# =============================================================================
if ($NoDeploy) {
    Write-Host "[OK] -NoDeploy: skipping deploy. Changed services that would have deployed:" -ForegroundColor Yellow
    foreach ($s in $plan) { Write-Host "      - $($s.Service)  ->  $($s.Script)" }
} elseif ($plan.Count -eq 0) {
    Write-Host "[OK] no deployable service changed -- nothing to deploy." -ForegroundColor Green
} else {
    # Validate the gcloud token UP FRONT (not just that an account is configured) -- an
    # expired token only fails deep inside the build, after main is already landed. Print-
    # access-token is a cheap, side-effect-free probe that fails fast if reauth is needed.
    $acct = (gcloud config get-value account 2>$null)
    $null = gcloud auth print-access-token 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($acct) -or $acct -eq '(unset)') {
        Write-Host "[ERROR] gcloud is not authenticated (account='$acct', token invalid)." -ForegroundColor Red
        Write-Host "        main is already landed ($(git rev-parse --short HEAD)) -- nothing was deployed yet." -ForegroundColor Yellow
        Write-Host "        Run 'gcloud auth login', then deploy the service(s) below DIRECTLY:" -ForegroundColor Yellow
        foreach ($s in $plan) { Write-Host "          $($s.Script)" }
        exit 1
    }
    Write-Host "[..] Deploy plan ($($plan.Count) service(s), as $acct):" -ForegroundColor Cyan
    foreach ($s in $plan) { Write-Host "      - $($s.Service)  ->  $($s.Script)" }
    for ($i = 0; $i -lt $plan.Count; $i++) {
        $s = $plan[$i]
        Write-Host ""
        Write-Host "[..] Deploying $($s.Service)" -ForegroundColor Cyan
        & $s.Script
        if ($LASTEXITCODE -ne 0) {
            # main is already landed, so the change is IN main -- re-running THIS script finds
            # no diff and will NOT redeploy. The correct recovery is to run the remaining
            # deploy script(s) directly after fixing the cause.
            Write-Host "[ERROR] deploy FAILED for $($s.Service) (exit $LASTEXITCODE)." -ForegroundColor Red
            Write-Host "        main is already landed ($(git rev-parse --short HEAD)); the change is IN main, so re-running" -ForegroundColor Yellow
            Write-Host "        merge-branches.ps1 will NOT redeploy. Fix the cause, then run the remaining script(s) directly:" -ForegroundColor Yellow
            for ($j = $i; $j -lt $plan.Count; $j++) { Write-Host "          $($plan[$j].Script)" }
            exit 1
        }
        Write-Host "[OK] deployed $($s.Service)" -ForegroundColor Green
    }
    Write-Host "[OK] all changed services deployed." -ForegroundColor Green
}

# =============================================================================
# 7. PRUNE: delete the dev branches now contained in origin/main (unless -NoPrune).
# =============================================================================
if (-not $NoPrune) {
    git fetch origin --prune *>$null
    $alreadyMerged = @(Get-MergedDevBranches $skip)
    if ($alreadyMerged.Count -gt 0) {
        Write-Host ""
        Write-Host "[..] Pruning dev branches now contained in main: $($alreadyMerged -join ', ')" -ForegroundColor Cyan
        Remove-RemoteBranches $alreadyMerged
        Write-Host "[OK] pruned." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "[OK] DONE -- integrated, landed on main, deployed, and pruned." -ForegroundColor Green
