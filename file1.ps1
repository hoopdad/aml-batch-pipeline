<#
.SYNOPSIS
    One-time setup: grant the compute cluster's managed identity all four
    Azure Storage roles required for batch endpoint scoring jobs to succeed.

.DESCRIPTION
    Azure ML batch endpoints fail at startup if the compute cluster's managed
    identity is missing any of:
      - Storage Blob Data Contributor          (inputs/outputs)
      - Storage File Data Privileged Contributor (workspace file share)
      - Storage Table Data Contributor         (parallel-run engine tracking)
      - Storage Queue Data Contributor         (parallel-run engine coordination)

    The "Table" and "Queue" roles are the easy ones to miss — without them
    every batch job fails with a misleading "private network / authorization
    failure" error inside the parallel-run engine, ~5-10 minutes after the
    job is submitted (no failure is visible in the HTTP profiling log).

    This script is idempotent: re-running it just confirms each role is
    already assigned and exits cleanly.

.PARAMETER ResourceGroup
    Azure resource group containing the workspace. Defaults to AZURE_RESOURCE_GROUP from .env.

.PARAMETER Workspace
    Azure ML workspace name. Defaults to AZURE_ML_WORKSPACE from .env.

.PARAMETER Cluster
    Name of the compute cluster used by the batch deployment. Required
    (no .env equivalent — the cluster name lives in batch-deployment.yml).

.EXAMPLE
    .\file1.ps1 -Cluster mikeo-automl-cpu-v2
    .\file1.ps1 -Cluster cpu-cluster -ResourceGroup my-rg -Workspace my-ws
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Cluster,

    [string]$ResourceGroup,
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'

# --- Resolve workspace targeting from .env when not passed explicitly ---
if (-not $ResourceGroup -or -not $Workspace) {
    $envFile = Join-Path $PSScriptRoot ".env"
    if (-not (Test-Path $envFile)) {
        Write-Error "ResourceGroup/Workspace not provided and no .env file found at $envFile."
        exit 1
    }
    $envValues = @{}
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$') {
            $envValues[$matches[1]] = $matches[2]
        }
    }
    if (-not $ResourceGroup) { $ResourceGroup = $envValues['AZURE_RESOURCE_GROUP'] }
    if (-not $Workspace)     { $Workspace     = $envValues['AZURE_ML_WORKSPACE'] }
}

if (-not $ResourceGroup -or -not $Workspace) {
    Write-Error "Could not resolve ResourceGroup or Workspace from .env or parameters."
    exit 1
}

Write-Host "Workspace : $Workspace (rg: $ResourceGroup)"
Write-Host "Cluster   : $Cluster"
Write-Host ""

# --- Look up the cluster's managed identity + storage account scope ---
$principalId = az ml compute show --name $Cluster --resource-group $ResourceGroup --workspace-name $Workspace --query "identity.principal_id" -o tsv 2>$null
if (-not $principalId) {
    Write-Error "Cluster '$Cluster' has no system-assigned managed identity. Enable it with:`n  az ml compute update --name $Cluster --resource-group $ResourceGroup --workspace-name $Workspace --identity-type SystemAssigned"
    exit 1
}

$storageId = az ml workspace show --name $Workspace --resource-group $ResourceGroup --query "storage_account" -o tsv 2>$null
if (-not $storageId) {
    Write-Error "Could not resolve the workspace's storage account."
    exit 1
}

Write-Host "Cluster MI principal : $principalId"
Write-Host "Storage scope        : $storageId"
Write-Host ""

# --- Roles required for batch scoring to work end-to-end ---
$requiredRoles = @(
    'Storage Blob Data Contributor',            # inputs/outputs (blobs)
    'Storage File Data Privileged Contributor', # workspace file share
    'Storage Table Data Contributor',           # parallel-run engine tracking
    'Storage Queue Data Contributor'            # parallel-run engine coordination
)

# --- One Graph call to get current assignments, then diff ---
$existing = az role assignment list --assignee $principalId --scope $storageId --query "[].roleDefinitionName" -o tsv 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to list role assignments. Are you signed in with sufficient permissions?"
    exit 1
}
$existingSet = @{}
foreach ($r in ($existing -split "`n")) { if ($r.Trim()) { $existingSet[$r.Trim()] = $true } }

$missing = $requiredRoles | Where-Object { -not $existingSet.ContainsKey($_) }
$present = $requiredRoles | Where-Object { $existingSet.ContainsKey($_) }

Write-Host "Current state of required roles:"
foreach ($r in $present) { Write-Host "  [OK]      $r" }
foreach ($r in $missing) { Write-Host "  [MISSING] $r" }
Write-Host ""

if ($missing.Count -eq 0) {
    Write-Host "All required roles already assigned. Nothing to do."
    exit 0
}

Write-Host "Granting $($missing.Count) missing role(s)..."
foreach ($role in $missing) {
    Write-Host "  -> $role"
    az role assignment create `
        --assignee-object-id $principalId `
        --assignee-principal-type ServicePrincipal `
        --role $role `
        --scope $storageId 2>&1 | Select-String -Pattern '"roleDefinitionName"|error' | ForEach-Object { Write-Host "     $_" }
}

Write-Host ""
Write-Host "Done. Note: Azure RBAC propagation takes 30-60 seconds."
Write-Host "Wait briefly before running invoke_batch_endpoint.py."
