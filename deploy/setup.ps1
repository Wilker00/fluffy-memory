<#
.SYNOPSIS
    One-time Google Cloud setup for fluffy-memory (Windows / PowerShell).

.DESCRIPTION
    Idempotent: safe to re-run. Each resource is skipped if it already exists.

.EXAMPLE
    ./deploy/setup.ps1 -ProjectId my-project-id
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [string]$Region = "us-central1",
    [string]$TemplateId = "fluffy-memory-fleet-filter",
    [string]$Topic = "fluffy-memory-triggers"
)

$ErrorActionPreference = "Stop"
$Bucket = "$ProjectId-fluffy-memory-staging"

function Test-Resource([scriptblock]$Check) {
    try { & $Check *> $null; return $LASTEXITCODE -eq 0 } catch { return $false }
}

Write-Host "==> Project: $ProjectId   Region: $Region" -ForegroundColor Cyan
gcloud config set project $ProjectId --quiet

Write-Host "==> Enabling APIs (this can take a couple of minutes)" -ForegroundColor Cyan
gcloud services enable `
    aiplatform.googleapis.com `
    storage.googleapis.com `
    cloudtrace.googleapis.com `
    logging.googleapis.com `
    telemetry.googleapis.com `
    modelarmor.googleapis.com `
    pubsub.googleapis.com `
    cloudscheduler.googleapis.com `
    cloudfunctions.googleapis.com `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    eventarc.googleapis.com `
    --project $ProjectId

Write-Host "==> Staging bucket: gs://$Bucket" -ForegroundColor Cyan
if (Test-Resource { gcloud storage buckets describe "gs://$Bucket" --project $ProjectId }) {
    Write-Host "    already exists, skipping"
} else {
    gcloud storage buckets create "gs://$Bucket" `
        --project $ProjectId --location $Region --uniform-bucket-level-access
}

Write-Host "==> Model Armor template: $TemplateId" -ForegroundColor Cyan
if (Test-Resource { gcloud model-armor templates describe $TemplateId --location $Region --project $ProjectId }) {
    Write-Host "    already exists, skipping"
} else {
    gcloud model-armor templates create $TemplateId `
        --location $Region `
        --project $ProjectId `
        --pi-and-jailbreak-filter-settings-enforcement=enabled `
        --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE `
        --malicious-uri-filter-settings-enforcement=enabled `
        --basic-config-filter-enforcement=enabled `
        --template-metadata-log-sanitize-operations
}

Write-Host "==> Model Armor floor settings (project-level baseline)" -ForegroundColor Cyan
# Floor settings work at project scope with no Cloud Organization required,
# so this holds on a personal account where org-level policy is unavailable.
try {
    gcloud model-armor floor-settings update `
        --full-uri="projects/$ProjectId/locations/global/floorSetting" `
        --pi-and-jailbreak-filter-settings-enforcement=enabled `
        --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE
} catch {
    Write-Warning "floor settings update failed (non-fatal; the template still enforces)"
}

Write-Host "==> Pub/Sub topic: $Topic" -ForegroundColor Cyan
if (Test-Resource { gcloud pubsub topics describe $Topic --project $ProjectId }) {
    Write-Host "    already exists, skipping"
} else {
    gcloud pubsub topics create $Topic --project $ProjectId
}

Write-Host "==> Application default credentials" -ForegroundColor Cyan
if (Test-Resource { gcloud auth application-default print-access-token }) {
    Write-Host "    already present"
} else {
    Write-Host "    run: gcloud auth application-default login" -ForegroundColor Yellow
}

@"

Setup complete.

Write these into your .env:

  GOOGLE_CLOUD_PROJECT=$ProjectId
  GOOGLE_CLOUD_LOCATION=$Region
  STAGING_BUCKET=$Bucket
  MODEL_ARMOR_TEMPLATE_ID=$TemplateId
  PUBSUB_TOPIC=$Topic

Next: make smoke   (verifies Agent Runtime works on this account, then cleans up)
"@ | Write-Host -ForegroundColor Green
