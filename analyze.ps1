<#
.SYNOPSIS
    Run GitHub Copilot CLI against a batch_invoke HTTP profiling log to
    produce a markdown summary of every request the Azure ML SDK made, and
    (by default) a draw.io diagram of the client → remote-service call graph.

.DESCRIPTION
    Wraps `copilot -p <prompt>` so you can reproduce on demand the same
    "what hostnames and paths did the SDK actually hit" report that was
    generated interactively. Output filenames mirror the input log:

        batch_invoke_<suffix>.log  ->  batch_invoke_<suffix>.analysis.md
                                       batch_invoke_<suffix>.diagram.drawio

    If no -LogFile is supplied, the most recently modified
    batch_invoke_*.log in the current directory is analyzed.

    Diagram generation uses the bundled simonpo/drawio-ninja instructions.
    Default install location is:
        $env:USERPROFILE\.copilot\m-skills\drawio-ninja\
    Override with the DRAWIO_NINJA_DIR environment variable. The script
    runs validate.py against the output. Pass -NoDiagram to skip.

.PARAMETER LogFile
    Path to the HTTP profiling log file produced by invoke_batch_endpoint.py.

.PARAMETER NoDiagram
    Skip the second copilot pass that generates the .drawio diagram.

.EXAMPLE
    .\analyze.ps1
    .\analyze.ps1 -LogFile .\batch_invoke_3f9c1a7b.log
    .\analyze.ps1 -NoDiagram
#>

param(
    [Parameter(Position = 0)]
    [string]$LogFile,

    [switch]$NoDiagram
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
$drawio   = Join-Path $dir "$prefix.diagram.drawio"

# Bundled drawio-ninja assets. Default: ~/.copilot/m-skills/drawio-ninja/
# Override with the DRAWIO_NINJA_DIR environment variable.
$drawioNinjaDir = if ($env:DRAWIO_NINJA_DIR) {
    $env:DRAWIO_NINJA_DIR
} else {
    Join-Path $env:USERPROFILE ".copilot\m-skills\drawio-ninja"
}
$drawioRules     = Join-Path $drawioNinjaDir "drawio.instructions.md"
$drawioValidator = Join-Path $drawioNinjaDir "validate.py"
$drawioExample   = Join-Path $drawioNinjaDir "examples\azure-architecture.drawio"

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

copilot -p $prompt --allow-all-tools --add-dir "$dir" | Out-File -FilePath $outFile -Encoding utf8

if ($LASTEXITCODE -ne 0) {
    Write-Error "copilot exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Analysis written to: $outFile"

# --- Diagram generation (optional second pass) ----------------------------
if ($NoDiagram) {
    Write-Host "Skipping diagram generation (-NoDiagram)."
    exit 0
}

if (-not (Test-Path $drawioRules)) {
    Write-Warning "drawio-ninja rules not found at: $drawioRules"
    Write-Warning "Install with:"
    Write-Warning "  git clone https://github.com/simonpo/drawio-ninja `"$drawioNinjaDir`""
    Write-Warning "Or set the DRAWIO_NINJA_DIR environment variable to your install. Pass -NoDiagram to skip. Skipping diagram."
    exit 0
}

Write-Host ""
Write-Host "Generating diagram : $drawio"
Write-Host ""

$diagramPrompt = @"
You are generating a draw.io (.drawio) XML diagram from an Azure ML batch
endpoint HTTP analysis report.

Read these three files first:

  ANALYSIS REPORT : $outFile
  DRAWIO RULES    : $drawioRules
  EXAMPLE         : $drawioExample

Write a complete, structurally-valid .drawio file to this absolute path
(use your file-write tool; do not just print XML to stdout):

  $drawio

The diagram must show:

  - One "Client Application" vertex on the left, labelled with the source
    script name (invoke_batch_endpoint.py) and a note that it runs on a
    local VM using the azure-ai-ml SDK + DefaultAzureCredential.
  - One vertex per distinct remote host called in the analysis, stacked
    vertically on the right in chronological order of first contact.
    Show the hostname on each service vertex.
  - One unidirectional edge from the Client to each service. Each edge
    label must include:
      * step number (1, 2, 3, ...)
      * the high-level function being performed
        (e.g. authentication, control plane, asset upload, invoke)
      * the specific SDK method names or HTTP operations involved
        (e.g. DefaultAzureCredential.get_token,
        ml_client.batch_endpoints.begin_create_or_update,
        POST /jobs)
  - Highlight the actual data-plane invoke call with a red, bold edge.

Follow ALL rules in DRAWIO RULES. Critical reminders:
  - Start the file with: <?xml version="1.0" encoding="UTF-8"?>
  - Use page="0" (infinite canvas).
  - Include root cells id="0" and id="1".
  - All cell IDs unique sequential integers; vertices before edges.
  - Inside value="..." attributes, use only <br/> for line breaks
    (no <b>, <i>, or any other HTML tag) and &#xa; for newlines in
    multi-line edge labels.

After writing the file, validate it by running:

  python "$drawioValidator" "$drawio"

If the validator reports any errors, FIX the file and re-validate.
Keep iterating until you see "VALID". Do not stop until validation
passes.

When done, print a single line: DIAGRAM_OK
"@

$drawioSkillDir = $drawioNinjaDir
copilot -p $diagramPrompt --allow-all-tools --add-dir "$dir" --add-dir "$drawioSkillDir"

if ($LASTEXITCODE -ne 0) {
    Write-Warning "copilot exited with code $LASTEXITCODE during diagram generation."
    exit $LASTEXITCODE
}

# Final independent validation pass
Write-Host ""
Write-Host "Final validation pass..."
python "$drawioValidator" "$drawio"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Diagram failed final validation: $drawio"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Diagram written to: $drawio"
