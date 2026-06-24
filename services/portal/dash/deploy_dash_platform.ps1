# =============================================================================
# deploy_dash_platform.ps1 -- REDEPLOY the portal Cloud Run service `platform-dash`
#                             ONLY (build a fresh image, then create-or-update the
#                             service). This is the fast inner-loop redeploy.
#
# Use this for code/template changes to the portal after the one-time standup
# (services\portal\deploy.ps1) has already created the SA, bucket, secrets,
# IAM, and APIs. This script does NOT touch IAM, buckets, or secrets -- it only
# rebuilds the image and rolls the service. The secrets it mounts must already exist
# (they are created by deploy.ps1).
#
# RUN AS YOURSELF -- never via Cloud Build from a laptop. We use `gcloud builds submit
# --tag` ONLY to build the image (no actAs needed for a build), then deploy the Cloud
# Run service from this laptop AS YOU (you do have actAs on the runtime SA). A
# cloudbuild-driven deploy would fail: the Cloud Build SA cannot
# iam.serviceAccounts.actAs the runtime SA.
#
# Idempotent: `gcloud run deploy` is create-or-update.
#
# USAGE
#   .\deploy_dash_platform.ps1            # build, redeploy
#   .\deploy_dash_platform.ps1 -SkipBuild # reuse current image, redeploy only
# =============================================================================

param([switch]$SkipBuild)

# --- Constants (use literally; never invent alternatives) --------------------
$PROJECT  = "agora-data-driven"
$REGION   = "asia-southeast1"   # Singapore. One region, never another.
$REPO     = "agora"             # shared Artifact Registry docker repo
$PLATFORM = "platform-dash"     # the portal Cloud Run service
$WEB_SA   = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$BUCKET   = "agora-data-driven-platform-dash"  # PRIVATE registry bucket

# Cookie domain the portal mints the SSO cookie on (leading dot -> all subdomains).
$COOKIE_DOMAIN = ".agoradatadriven.com"

# Google Tag Manager container loaded site-wide on every portal HTML page (GA4 is configured INSIDE
# this container in the GTM UI). Set to "" to ship with GTM OFF; the app injects nothing unless this
# is non-empty. Local preview never runs this script, so it stays untracked.
$GTM_CONTAINER_ID = "GTM-KKWX37RG"

# Secrets mounted as env vars (Secret Manager, :latest). Created by deploy.ps1.
$SESSION_SECRET = "platform-dash-session-key"
$SSO_SECRET     = "platform-sso-key"

# This script stays on the default $ErrorActionPreference = "Continue": gcloud writes
# ordinary progress to stderr, and under "Stop" PowerShell wraps that stderr as a
# terminating NativeCommandError and aborts mid-script EVEN ON SUCCESS. We gate on
# $LASTEXITCODE explicitly via Must.

# --- Helpers (Die / Must) ----------------------------------------------------
function Die([string]$msg) { Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }
function Must([string]$what) {
    # Call AFTER a native command; fails the script if that command returned non-zero.
    if ($LASTEXITCODE -ne 0) { Die "$what (exit $LASTEXITCODE)" }
}

# Resolve the build dir from THIS script's own location (the Dockerfile/main.py sit next
# to this script) so it works regardless of the caller's current directory.
$DASH_DIR = $PSScriptRoot

# =============================================================================
# Step 1 -- Resolve a short git SHA for the image tag.
# =============================================================================
Write-Host "[..] Resolving image tag" -ForegroundColor Cyan
$SHA = (git -C $DASH_DIR rev-parse --short HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($SHA)) {
    # Not a git repo (or no commits yet): fall back to a timestamped manual tag so the
    # image is still uniquely identifiable.
    $SHA = "manual-" + (Get-Date -Format "yyyyMMddHHmmss")
    Write-Host "    not a git repo; using fallback tag $SHA" -ForegroundColor Yellow
}
$SHA = $SHA.Trim()
$AR_HOST = "$REGION-docker.pkg.dev"
$IMG = "$AR_HOST/$PROJECT/$REPO/${PLATFORM}:$SHA"
Write-Host "[OK] image = $IMG"

# =============================================================================
# Step 2 -- Build the image (build ONLY -- no actAs needed; we deploy ourselves).
# =============================================================================
if (-not $SkipBuild) {
    Write-Host "[..] Building image $IMG" -ForegroundColor Cyan
    gcloud builds submit $DASH_DIR --tag $IMG --project=$PROJECT
    Must "build image for $PLATFORM"
    Write-Host "[OK] built $IMG"
} else {
    Write-Host "[..] -SkipBuild: deploying existing image $IMG" -ForegroundColor Yellow
}

# =============================================================================
# Step 3 -- Deploy the portal Cloud Run service AS YOURSELF with the runtime SA.
#
#   Org policy: Domain Restricted Sharing rejects --allow-unauthenticated. Deploy with
#   --no-invoker-iam-check instead; the Flask app does its OWN password/SSO auth in
#   process, and the private registry JSON is only ever read behind the portal login.
# =============================================================================
# Assemble the env-var list; only ship GTM_CONTAINER_ID when it's set (empty -> GTM stays off).
$ENV_VARS = "COOKIE_DOMAIN=$COOKIE_DOMAIN,REGISTRY_BUCKET=$BUCKET,REGISTRY_OBJECT=platform.json"
if (-not [string]::IsNullOrWhiteSpace($GTM_CONTAINER_ID)) { $ENV_VARS += ",GTM_CONTAINER_ID=$GTM_CONTAINER_ID" }

Write-Host "[..] Deploying Cloud Run service $PLATFORM" -ForegroundColor Cyan
gcloud run deploy $PLATFORM `
    --image $IMG `
    --region $REGION `
    --project $PROJECT `
    --service-account $WEB_SA `
    --no-invoker-iam-check `
    --update-env-vars $ENV_VARS `
    --update-secrets "SESSION_SECRET=${SESSION_SECRET}:latest,SSO_SECRET=${SSO_SECRET}:latest"
Must "deploy Cloud Run service $PLATFORM"
Write-Host "[OK] deployed $PLATFORM (tag $SHA)" -ForegroundColor Green
