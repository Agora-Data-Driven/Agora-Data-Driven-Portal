# enable_ga4_reporting.ps1 -- one-time infra for the OPT-IN "live GA4 event counts" Website Health feature.
#
# The Website Health tab can show REAL per-event counts (page_view, purchase, ...) pulled from each
# client's GA4 property via the Analytics Data API. This is a deliberate, opt-in deviation from the
# "no new infra" rule (like the Drive-API strategy feature); a default deploy stays infra-free.
#
# Auth is KEYLESS, reusing the same Token-Creator-on-self grant the large-creative uploads use: the
# runtime SA mints a short-lived analytics.readonly token for itself via the IAM Credentials API. This
# script wires the GCP side and is idempotent -- safe to re-run.
#
#   1. Enable the Analytics Data API (analyticsdata) + the IAM Credentials API (iamcredentials).
#   2. Grant the runtime SA roles/iam.serviceAccountTokenCreator on ITSELF (no-op if uploads already did).
#   3. Turn the feature ON for the running service (env GA4_REPORTING_ENABLED=1).
#
# ONE MANUAL, PER-CLIENT STEP remains (it lives in GA4, not GCP, so it can't be scripted here):
#   In Google Analytics -> Admin -> Property Access Management, add the runtime SA email below as a
#   *Viewer* on EACH client's GA4 property. Until then the Data API returns 403 and the tab degrades
#   to a friendly "grant access" message (never a 500).
#
# Then, in the Website Health tab (super admin), paste each client's NUMERIC GA4 property id
# (Admin -> Property Settings -- NOT the G-XXXX measurement id) and click "Load event counts".
#
# Fixed facts (see CLAUDE.md): project agora-data-driven, region asia-southeast1, runtime SA
# platform-dash-web@..., service platform-dash.
#
# Stays on $ErrorActionPreference = "Continue" and gates on $LASTEXITCODE (the repo convention): gcloud
# writes ordinary progress -- even "finished successfully" -- to stderr, which "Stop" would treat as a
# terminating error and abort the script mid-way EVEN ON SUCCESS.
$ErrorActionPreference = "Continue"
function Die([string]$m) { Write-Host "[ERROR] $m" -ForegroundColor Red; exit 1 }
function Must([string]$w) { if ($LASTEXITCODE -ne 0) { Die "$w (exit $LASTEXITCODE)" } }

$Project   = "agora-data-driven"
$Region    = "asia-southeast1"
$Service   = "platform-dash"
$RuntimeSA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"

Write-Host "[..] Enabling analyticsdata.googleapis.com + iamcredentials.googleapis.com"
gcloud services enable analyticsdata.googleapis.com iamcredentials.googleapis.com --project=$Project
Must "enable analyticsdata + iamcredentials APIs"

Write-Host "[..] Granting roles/iam.serviceAccountTokenCreator to $RuntimeSA on itself (idempotent)"
gcloud iam service-accounts add-iam-policy-binding $RuntimeSA `
  --project=$Project `
  --member="serviceAccount:$RuntimeSA" `
  --role="roles/iam.serviceAccountTokenCreator" | Out-Null
Must "grant Token Creator on self"

Write-Host "[..] Turning the feature ON for $Service (GA4_REPORTING_ENABLED=1)"
gcloud run services update $Service `
  --region=$Region `
  --project=$Project `
  --update-env-vars GA4_REPORTING_ENABLED=1
Must "set GA4_REPORTING_ENABLED on $Service"

Write-Host ""
Write-Host "[OK] Live GA4 event counts enabled." -ForegroundColor Green
Write-Host "     NEXT (per client, in Google Analytics -- cannot be scripted):" -ForegroundColor Yellow
Write-Host "       Admin -> Property Access Management -> add this account as a Viewer:" -ForegroundColor Yellow
Write-Host "         $RuntimeSA" -ForegroundColor Yellow
Write-Host "     Then paste the numeric property id in the Website Health tab and Load event counts."
