<#
.SYNOPSIS
    Run GitHub Copilot CLI against a batch_invoke HTTP profiling log to
    produce a markdown summary of every request the Azure ML SDK made.

.DESCRIPTION
    Wraps `copilot -p <prompt>` so you can reproduce on demand the same
    "what hostnames and paths did the SDK actually hit" report that was
    generated interactively. Output filename mirrors the input log:

        batch_invoke_<suffix>.log  ->  batch_invoke_<suffix>.analysis.md

    If no -LogFile is supplied, the most recently modified
    batch_invoke_*.log in the current directory is analyzed.

.PARAMETER LogFile
    Path to the HTTP profiling log file produced by invoke_batch_endpoint.py.

.EXAMPLE
    .\analyze.ps1
    .\analyze.ps1 -LogFile .\batch_invoke_3f9c1a7b.log
#>

param(
    [Parameter(Position = 0)]
    [string]$LogFile
)

$ErrorActionPreference = 'Stop'

# --- Resolve the log file -------------------------------------------------
if (-not $LogFile) {
    $latest = Get-ChildItem -Path "batch_invoke_*.log" -File -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending |
              Select-Object -First 1
    if (-not $latest) {
        Write-Error "No batch_invoke_*.log files found in the current directory. Pass one explicitly: .\analyze.ps1 <path>"
        exit 1
    }
    $LogFile = $latest.FullName
    Write-Host "No log file specified; using most recent: $LogFile"
}

if (-not (Test-Path $LogFile)) {
    Write-Error "Log file not found: $LogFile"
    exit 1
}

$resolved = (Resolve-Path $LogFile).Path
$prefix   = [IO.Path]::GetFileNameWithoutExtension($resolved)
$dir      = [IO.Path]::GetDirectoryName($resolved)
$outFile  = Join-Path $dir "$prefix.analysis.md"

# --- The prompt -----------------------------------------------------------
$prompt = @"
You are analyzing an Azure ML batch endpoint HTTP profiling log file.

Read the log file at this absolute path:

    $resolved

Context: this log was produced by a Python script (invoke_batch_endpoint.py)
that uses azure-ai-ml + DefaultAzureCredential. In one run the script:
  1. Acquires an Azure AD token (may use IMDS at 169.254.169.254 or a
     login.microsoftonline.com endpoint).
  2. Calls ml_client.batch_endpoints.begin_create_or_update(...) to create
     a batch endpoint (control plane on management.azure.com).
  3. Calls ml_client.batch_deployments.begin_create_or_update(...) to create
     a deployment under that endpoint (control plane).
  4. Calls ml_client.batch_endpoints.invoke(...) — this is the SINGLE
     POST to the endpoint scoring URI on
     <endpoint>.<region>.inference.ml.azure.com/jobs (data/inference plane).
  5. Polls ml_client.jobs.get(...) every 15s until terminal state. Each
     poll is a GET against management.azure.com/.../jobs/<batchjob-guid>
     (control plane).
  6. Optionally deletes the endpoint at the end (control plane DELETE).

Azure's HttpLoggingPolicy emits each request as a line like:
    YYYY-MM-DD HH:MM:SS,mmm [azure.core.pipeline.policies....] DEBUG: Request URL: '<url>'

Your task — output ONLY the markdown report below, no preamble or trailing prose:

# Batch Invoke HTTP Analysis — $prefix

## Summary
One sentence identifying the single actual invoke call: which line number
in the log, which hostname, and the full URL path. Also state the total
number of HTTP requests captured.

## First HTTP Calls (chronological)
A markdown table covering the first 8 distinct calls in chronological
order. Columns:

| # | Log line | Method | Host | Path | What it is |

Interpret each call in the "What it is" column using the context above
(e.g. "IMDS managed-identity token", "endpoint metadata GET",
"deployment metadata GET", "**the actual invoke POST**",
"first job status poll", "endpoint delete", etc.). Bold the row(s)
representing the actual invoke.

## All Unique Endpoints (by count)
A markdown table aggregating every Request URL by (host, path) with
counts, sorted descending. Columns:

| Count | Host | Path |

## Control vs Data Plane
Two short bullet points explaining how many calls hit
management.azure.com (control plane — ARM under
Microsoft.MachineLearningServices) vs *.inference.ml.azure.com
(data/inference plane), and why the control-plane count is so much
higher (job polling loop + pre-invoke metadata lookups).
"@

# --- Run copilot ----------------------------------------------------------
Write-Host ""
Write-Host "Analyzing : $resolved"
Write-Host "Writing   : $outFile"
Write-Host ""

copilot -p $prompt | Out-File -FilePath $outFile -Encoding utf8

if ($LASTEXITCODE -ne 0) {
    Write-Error "copilot exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Analysis written to: $outFile"
