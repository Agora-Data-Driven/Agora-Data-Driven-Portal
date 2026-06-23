# enable_atrium_uploads.ps1 -- one-time infra for Atrium's "bypass the cap" large-creative uploads.
#
# Atrium admins upload videos/images straight to GCS via a V4 SIGNED URL (the browser PUTs directly
# to the bucket), bypassing Cloud Run's ~32 MiB request cap. Signing is KEYLESS: the runtime SA signs
# via the IAM signBlob API, so NO key file is ever created or committed. This script wires the three
# things that makes that work, and is idempotent -- safe to re-run.
#
#   1. Enable the IAM Service Account Credentials API (provides signBlob).
#   2. Grant the runtime SA roles/iam.serviceAccountTokenCreator on ITSELF (so it may sign as itself).
#   3. Set CORS on the registry bucket so the browser's cross-origin PUT to GCS is allowed.
#
# Fixed facts (see CLAUDE.md): project agora-data-driven, region asia-southeast1, runtime SA
# platform-dash-web@..., registry bucket agora-data-driven-platform-dash.
$ErrorActionPreference = "Stop"

$Project = "agora-data-driven"
$RuntimeSA = "platform-dash-web@agora-data-driven.iam.gserviceaccount.com"
$Bucket = "gs://agora-data-driven-platform-dash"

Write-Host "[..] Enabling iamcredentials.googleapis.com"
gcloud services enable iamcredentials.googleapis.com --project=$Project

Write-Host "[..] Granting roles/iam.serviceAccountTokenCreator to $RuntimeSA on itself"
gcloud iam service-accounts add-iam-policy-binding $RuntimeSA `
  --project=$Project `
  --member="serviceAccount:$RuntimeSA" `
  --role="roles/iam.serviceAccountTokenCreator" | Out-Null

Write-Host "[..] Applying CORS to $Bucket (browser PUT/GET for direct-to-GCS uploads)"
$cors = @'
[
  {
    "origin": [
      "https://platform-dash-c732u7m57a-as.a.run.app",
      "https://platform-dash-585951669065.asia-southeast1.run.app",
      "https://portal.agoradatadriven.com"
    ],
    "method": ["PUT", "GET", "OPTIONS"],
    "responseHeader": ["Content-Type", "x-goog-resumable"],
    "maxAgeSeconds": 3600
  }
]
'@
$tmp = New-TemporaryFile
$tmpPath = $tmp.FullName
try {
  # UTF-8 without BOM (gcloud/JSON parsers choke on a BOM).
  [System.IO.File]::WriteAllText($tmpPath, $cors, (New-Object System.Text.UTF8Encoding($false)))
  # NOTE: pass the path as its own variable -- "$tmp.FullName" inside an arg expands only $tmp and
  # appends the literal ".FullName", pointing gcloud at a path that doesn't exist.
  gcloud storage buckets update $Bucket --cors-file=$tmpPath --project=$Project
} finally {
  Remove-Item $tmpPath -Force -ErrorAction SilentlyContinue
}

Write-Host "[OK] Atrium large-creative uploads are enabled (signed-URL direct-to-GCS)."
