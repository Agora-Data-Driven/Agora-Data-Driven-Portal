<#
.SYNOPSIS
    Per-session preflight (~30s) for the Agora Data Driven monorepo. Confirms BOTH
    gcloud credential systems are live (reauthing only when needed), pins the project,
    and checks whether the shared ingest secret + raw dataset exist yet (noting, not
    failing, when they do not -- expected on a fresh project). Ends with a "Common
    commands" cheat sheet.
.DESCRIPTION
    Run this at the start of every work session. It tolerates probe failures and
    reauths instead of aborting.
#>

# Two independent gcloud credential systems:
# gcloud keeps two independent logins and the org enforces periodic reauth on each, so
# either can expire without the other:
#   - CLI creds (used by `gcloud secrets ...`, refreshed via `gcloud auth login`)
#   - Application Default Credentials / ADC (used by the Python client libraries --
#     google-cloud-bigquery / -storage / -secret-manager -- refreshed via
#     `gcloud auth application-default login`).
# A morning preflight must check BOTH.
#
# NOTE: this script deliberately does NOT set $ErrorActionPreference = "Stop".
# gcloud writes ordinary progress to stderr. Under $ErrorActionPreference = "Stop"
# PowerShell wraps that stderr as a terminating NativeCommandError and aborts mid-script
# EVEN ON SUCCESS. This script therefore stays on the default "Continue" and gates on
# $LASTEXITCODE explicitly (or tolerates probe failures and reauths instead).

$PROJECT = "agora-data-driven"

# Probe targets: the shared ingest secret and the shared raw layer.
$PROBE_SECRET  = "windsor-api-key"   # shared ingest API key (Secret Manager)
$PROBE_DATASET = "raw_windsor"       # shared raw layer (BigQuery)

# ---------------------------------------------------------------------------
# Resolve python: prefer the repo .venv, fall back to system python.
# ---------------------------------------------------------------------------
$REPO    = Split-Path -Parent $PSScriptRoot
$venvPy  = Join-Path $REPO ".venv/Scripts/python.exe"
if (Test-Path $venvPy) { $PY = $venvPy } else { $PY = "python" }
Write-Host "[OK] Python: $PY" -ForegroundColor Green

# ---------------------------------------------------------------------------
# gcloud must be on PATH
# ---------------------------------------------------------------------------
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "[ERROR] gcloud is not on PATH. Run scripts/setup.ps1 (or open a new terminal)." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# CLI credentials -- reauth only if the probe fails
# ---------------------------------------------------------------------------
Write-Host "[..] Checking gcloud CLI credentials" -ForegroundColor Cyan
$null = gcloud auth print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] CLI creds expired/absent -- launching browser login" -ForegroundColor Yellow
    gcloud auth login
} else {
    Write-Host "[OK] CLI credentials valid" -ForegroundColor Green
}

# Pin project (cheap, idempotent).
gcloud config set project $PROJECT 2>$null

# ---------------------------------------------------------------------------
# Application Default Credentials -- reauth only if the probe fails
# ---------------------------------------------------------------------------
Write-Host "[..] Checking Application Default Credentials (ADC)" -ForegroundColor Cyan
$null = gcloud auth application-default print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] ADC expired/absent -- launching browser login" -ForegroundColor Yellow
    gcloud auth application-default login
} else {
    Write-Host "[OK] ADC valid" -ForegroundColor Green
}

# Keep the ADC quota project aligned (harmless to repeat).
gcloud auth application-default set-quota-project $PROJECT 2>$null

# ---------------------------------------------------------------------------
# Echo the active account so the operator can confirm who they are
# ---------------------------------------------------------------------------
$acct = (gcloud config get-value account 2>$null)
Write-Host "[OK] Active account: $acct" -ForegroundColor Green

# ---------------------------------------------------------------------------
# Verify the shared ingest secret is readable (CLI creds).
# Capture output to $null so the secret never prints.
# ---------------------------------------------------------------------------
Write-Host "[..] Verifying secret '$PROBE_SECRET' is readable" -ForegroundColor Cyan
$null = gcloud secrets versions access latest --secret=$PROBE_SECRET --project=$PROJECT 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] Secret '$PROBE_SECRET' not readable yet -- expected on a fresh project (it is created when you wire up Windsor ingest). If you DID create it, check CLI creds / IAM." -ForegroundColor Yellow
} else {
    Write-Host "[OK] Secret '$PROBE_SECRET' readable (CLI credentials)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Ping BigQuery (the shared raw_windsor dataset) via the venv python (ADC).
# ---------------------------------------------------------------------------
Write-Host "[..] Pinging BigQuery dataset '$PROBE_DATASET'" -ForegroundColor Cyan
& $PY -c @"
from google.cloud import bigquery
bq = bigquery.Client(project='$PROJECT')
bq.get_dataset('$PROJECT.$PROBE_DATASET')
print('ok')
"@ 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[..] BigQuery dataset '$PROBE_DATASET' not reachable yet -- expected on a fresh project (created by the Windsor ingest standup). If you DID create it, check ADC / IAM." -ForegroundColor Yellow
} else {
    Write-Host "[OK] BigQuery dataset '$PROBE_DATASET' reachable (ADC)" -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# Common commands cheat sheet
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "================ Common commands ================" -ForegroundColor Cyan
Write-Host "Run a Windsor connector loader locally (GA4 example):" -ForegroundColor White
Write-Host "  .\.venv\Scripts\python.exe ingest\windsor_data_pull\ga4\ga4_loader.py" -ForegroundColor Gray
Write-Host ""
Write-Host "Run the template client export job locally (force a rebuild):" -ForegroundColor White
Write-Host "  `$env:FORCE_REBUILD='1'; .\.venv\Scripts\python.exe clients\client_template\job\main.py" -ForegroundColor Gray
Write-Host ""
Write-Host "Deploy a client dash (manual build + deploy as yourself):" -ForegroundColor White
Write-Host "  .\clients\client_template\dash\deploy_dash_template.ps1" -ForegroundColor Gray
Write-Host "  (Org policy forbids public Cloud Run: deploy uses --no-invoker-iam-check," -ForegroundColor Gray
Write-Host "   never --allow-unauthenticated; the Flask app does its own password/SSO auth.)" -ForegroundColor Gray
Write-Host ""
Write-Host "Deploy the shared Windsor ingest jobs + schedulers:" -ForegroundColor White
Write-Host "  .\scripts\deploy_ingest_jobs.ps1" -ForegroundColor Gray
Write-Host ""
Write-Host "Resolve the project number at runtime (NEVER hardcode it):" -ForegroundColor White
Write-Host "  gcloud projects describe $PROJECT --format='value(projectNumber)'" -ForegroundColor Gray
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "[OK] Preflight complete." -ForegroundColor Green
