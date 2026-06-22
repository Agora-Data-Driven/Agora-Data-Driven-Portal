# Run the portal LOCALLY for click-through testing -- no GCP, no ADC, no deploy, NO PASSWORD.
#
# It stands up an isolated venv (.venv-portal) with just Flask + requests, points the data layer at
# a throwaway local folder (.local_portal_data), seeds a demo portal full of clients/workspaces, and
# serves it at http://localhost:8080 with auto-login as a super-admin so you click straight in with
# no password and can edit every workspace in place. The production .venv is deliberately left
# untouched (it excludes the web deps on purpose -- see services/portal/dash/requirements.txt).
#
#   Double-click:   preview/Preview Portal (admin).cmd  (at the repo root)
#   From a shell:   services\portal\dash\run_local.ps1
#
# Stop with Ctrl+C. Delete .local_portal_data to reset the demo. This NEVER touches the real bucket,
# and the no-password mode can ONLY activate locally (it is tied to the relaxed-cookie local posture).
#
#   -WithLogin   show the REAL login page (no auto super-admin) so you can sign in AS A CLIENT.
#   -Port <n>    serve on a different port (default 8080) so a login instance can run alongside.

param(
    [switch]$WithLogin,
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"

$dash = $PSScriptRoot
$repo = (Resolve-Path (Join-Path $dash "..\..\..")).Path

# 1. An isolated local-run venv (bootstrapped from the repo venv we know exists).
$venv = Join-Path $repo ".venv-portal"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[run_local] creating local-run venv at .venv-portal ..."
    $bootstrap = Join-Path $repo ".venv\Scripts\python.exe"
    if (-not (Test-Path $bootstrap)) { $bootstrap = "python" }
    & $bootstrap -m venv $venv
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet "Flask==3.0.3" "requests==2.34.2"
}

# 1b. AI strategy generation is OPTIONAL and OFF by default. It writes the Insight + Action bullet
#     points from the attached Google Doc; without it, "Generate strategy" can only drop a raw doc
#     excerpt into Insight and leaves Action blank (exactly the empty-Action symptom).
#
#     The key is read, in order, from: (1) the ANTHROPIC_API_KEY environment variable, or (2) a
#     gitignored local key file so the DOUBLE-CLICKABLE .cmd works too (a fresh .cmd process does NOT
#     inherit an env var you typed into some other shell). Looked-for files (first one wins):
#         <repo>\.anthropic_key   ·   services\portal\dash\.anthropic_key   ·   <repo>\.env (ANTHROPIC_API_KEY=...)
#     All three names are already covered by .gitignore, so the key is never committed.
if (-not $env:ANTHROPIC_API_KEY) {
    $keyFiles = @(
        (Join-Path $repo ".anthropic_key"),
        (Join-Path $dash ".anthropic_key")
    )
    foreach ($kf in $keyFiles) {
        if (Test-Path $kf) {
            $k = (Get-Content -Raw $kf).Trim()
            if ($k) { $env:ANTHROPIC_API_KEY = $k; Write-Host "[run_local] loaded ANTHROPIC_API_KEY from $kf" -ForegroundColor DarkGray; break }
        }
    }
}
if (-not $env:ANTHROPIC_API_KEY) {
    $envFile = Join-Path $repo ".env"
    if (Test-Path $envFile) {
        $line = (Get-Content $envFile | Where-Object { $_ -match '^\s*ANTHROPIC_API_KEY\s*=' } | Select-Object -First 1)
        if ($line) {
            $k = ($line -replace '^\s*ANTHROPIC_API_KEY\s*=\s*', '').Trim().Trim('"').Trim("'")
            if ($k) { $env:ANTHROPIC_API_KEY = $k; Write-Host "[run_local] loaded ANTHROPIC_API_KEY from $envFile" -ForegroundColor DarkGray }
        }
    }
}
if ($env:ANTHROPIC_API_KEY) {
    $env:FEEDBACK_AI_ENABLED = "1"
    & $py -c "import anthropic" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[run_local] installing 'anthropic' into .venv-portal for AI strategy generation ..."
        & $py -m pip install --quiet "anthropic"
    }
    Write-Host "[run_local] AI strategy generation ENABLED -- 'Generate strategy' will write Insight + Action." -ForegroundColor Green
} else {
    Write-Host "[run_local] AI strategy generation OFF -- 'Generate strategy' only excerpts the doc into Insight; Action stays blank." -ForegroundColor Yellow
    Write-Host "[run_local]   To enable it, put your key in a file named  .anthropic_key  at the repo root, then re-run." -ForegroundColor Yellow
}

# 2. Point the data layer at a throwaway local folder (both registry and workspaces).
$data = Join-Path $repo ".local_portal_data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$env:REGISTRY_LOCAL_DIR = $data
$env:WORKSPACE_LOCAL_DIR = $data

# 3. Local-dev app config: a dummy session secret, relaxed cookies so it works over http, and (unless
#    -WithLogin) the no-password preview mode (DEV_NOAUTH). PORTAL_SECURE_COOKIES=0 is what gates
#    DEV_NOAUTH on, so these two stay together -- it can never activate in the https production deploy.
$env:SESSION_SECRET = "local-dev-secret-not-for-production"
$env:PORTAL_SECURE_COOKIES = "0"
$env:PORT = "$Port"
if ($WithLogin) {
    Remove-Item Env:\PORTAL_DEV_NOAUTH -ErrorAction SilentlyContinue   # show the REAL login page
} else {
    $env:PORTAL_DEV_NOAUTH = "1"                                       # no-password super-admin
}

# 4. Seed the demo clients + workspaces (idempotent). No passwords are needed to log in (DEV_NOAUTH
#    auto-signs you in as super-admin), but seeding gives you real clients/workspaces to click into.
& $py (Join-Path $dash "seed_local.py")

Write-Host ""
if ($WithLogin) {
    Write-Host "[run_local] starting portal at http://localhost:$Port  (LOGIN PAGE -- Ctrl+C to stop)" -ForegroundColor Green
    Write-Host "[run_local] sign in with ANY email + a client password (e.g. riverdance-demo) for the CLIENT view." -ForegroundColor Green
} else {
    Write-Host "[run_local] starting portal at http://localhost:$Port  (no password -- Ctrl+C to stop)" -ForegroundColor Green
    Write-Host "[run_local] you are auto-signed-in as super-admin: every client + in-place editing." -ForegroundColor Green
}
Write-Host ""

# 5. Open the browser once the server is up (a detached helper waits a moment, then opens the URL),
#    then serve in the foreground (this call blocks until Ctrl+C).
Start-Process powershell -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-Command",
    "Start-Sleep -Seconds 3; Start-Process 'http://localhost:$Port/'"
)
& $py (Join-Path $dash "main.py")
