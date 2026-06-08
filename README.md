# Azure ML Batch Endpoint Test Harness

NOTE: NOT PRODUCTION SAFE. FOR TROUBLESHOOTING IN DEV ONLY

REPO CO-CREATED WITH GITHUB COPILOT CLI

A minimal, self-contained sandbox for **end-to-end testing of Azure Machine
Learning batch endpoints**. Each run creates a fresh endpoint + deployment,
invokes it against a sample input, polls until completion, optionally cleans
up — and captures full HTTP-level traces of every call the Azure ML SDK
makes along the way.

Useful when you want to:

- Verify a workspace, compute cluster, and data asset are wired up correctly.
- See exactly which Azure REST endpoints the SDK hits during a batch job
  (control plane vs inference plane, polling cadence, request bodies).
- Reproduce a clean "create → invoke → delete" cycle without leaving
  orphan endpoints around.
- Debug auth, networking, or RBAC issues with concrete on-the-wire evidence.

## What's in the box

```
.
├── invoke_batch_endpoint.py     # main: create endpoint+deployment, invoke, poll, cleanup
├── cleanup_batch_endpoints.py   # one-shot: delete every batch endpoint in the workspace
├── analyze.ps1                  # post-run: copilot CLI turns an HTTP log into a markdown report + .drawio diagram
├── file1.ps1                    # one-time setup: grant cluster MI the four Storage roles batch scoring needs
│
├── score.py                     # batch deployment scoring script (PRS contract: init() + run())
├── score_component.py           # alternative scorer following the Command Component contract
├── component.yaml               # AML component definition for score.py
├── conda.yml                    # conda env layered onto the curated base image
├── dummy_model.txt              # placeholder file you register as the "model"
├── input/data.csv               # 4-row sample CSV used as batch input
│
├── batch-endpoint.yml.template  # copy → batch-endpoint.yml, customize
├── batch-deployment.yml.template
├── .env.template                # copy → .env, fill in your workspace
│
├── requirements.txt
├── .gitignore
└── README.md                    # you are here
```

## Prerequisites

### Azure side

You need an Azure ML workspace with a few resources in place. The CLI
commands below all assume `RG=<your-resource-group>` and
`WS=<your-workspace>`.

1. **Workspace** — any existing Azure ML workspace.
2. **Compute cluster** for the batch deployment to run on:
   ```bash
   az ml compute create --name cpu-cluster --type amlcompute \
     --size Standard_DS3_v2 --min-instances 0 --max-instances 2 \
     --identity-type system_assigned \
     --resource-group $RG --workspace-name $WS
   ```
3. **Registered model** (the deployment YAML needs one, even though the
   scoring script ignores it):
   ```bash
   az ml model create --name dummy-batch-model --version 1 \
     --path ./dummy_model.txt \
     --resource-group $RG --workspace-name $WS
   ```
4. **Registered input data asset** that batch jobs will read:
   ```bash
   az ml data create --name customer_batch_input --version 1 \
     --type uri_file --path ./input/data.csv \
     --resource-group $RG --workspace-name $WS
   ```
5. **Storage permissions for the cluster** — the compute cluster's managed
   identity needs **three** roles on the workspace's default storage
   account: `Storage Blob Data Contributor` (for inputs/outputs),
   `Storage Table Data Contributor`, and `Storage Queue Data Contributor`
   (the last two are required because Azure ML's batch scoring engine
   coordinates mini-batches via Azure Tables + Queues — without them
   every batch job fails at startup with a generic "private network /
   authorization failure" error):
   ```powershell
   $PRINCIPAL_ID = az ml compute show --name cpu-cluster `
       --resource-group $RG --workspace-name $WS `
       --query "identity.principal_id" -o tsv
   $STORAGE_ID = az ml workspace show --name $WS --resource-group $RG `
       --query "storage_account" -o tsv
   foreach ($role in @("Storage Blob Data Contributor",
                        "Storage Table Data Contributor",
                        "Storage Queue Data Contributor")) {
       az role assignment create `
           --assignee-object-id $PRINCIPAL_ID `
           --assignee-principal-type ServicePrincipal `
           --role $role `
           --scope $STORAGE_ID
   }
   ```

### Local side

- **Python 3.10+** (the conda env in `conda.yml` is pinned to 3.10).
- **`az` CLI** with the `ml` extension (`az extension add -n ml`) — only
  required for one-time setup commands above; the main scripts use the
  Python SDK.
- **Azure credentials** discoverable by `DefaultAzureCredential`. Easiest:
  `az login`. Falls back to an interactive browser prompt if no cached
  credential is found.
- **(Optional) GitHub Copilot CLI** (`copilot` on `PATH`) — only needed
  to run `analyze.ps1`. Without it, the raw `batch_invoke_*.log` files
  are still useful on their own.
- **(Optional) drawio-ninja** — required only for `analyze.ps1`'s
  diagram pass. Install once:
  ```powershell
  git clone https://github.com/simonpo/drawio-ninja "$env:USERPROFILE\.copilot\m-skills\drawio-ninja"
  ```
  Override the location by setting `$env:DRAWIO_NINJA_DIR`. Skip the
  diagram pass entirely with `analyze.ps1 -NoDiagram`.
- **PowerShell 5.1+ or PowerShell 7** for `analyze.ps1`.
- **Python 3.6+** on `PATH` — used by drawio-ninja's stdlib-only
  `validate.py` after each diagram pass.

## Setup

```powershell
# 1. Clone / unpack the repo, then from its root:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Copy the templates and edit them
Copy-Item .env.template .env
Copy-Item batch-endpoint.yml.template batch-endpoint.yml
Copy-Item batch-deployment.yml.template batch-deployment.yml

# 3. Open .env in an editor and fill in the four real values
#    (subscription id, resource group, workspace name, and optionally
#    your input data asset name if you registered it under a different name).

# 4. Grant the compute cluster's managed identity the four Storage roles
#    batch scoring requires (Blob/File/Table/Queue Data Contributor). This
#    is the single most common reason new workspaces fail batch jobs:
.\file1.ps1 -Cluster cpu-cluster
```

`file1.ps1` is idempotent — re-running it on an already-configured cluster
just confirms each role is assigned and exits cleanly. Skipping it leads
to jobs that *appear* to submit successfully but fail at startup ~5-10
minutes later with a misleading "private network / authorization failure"
error. `invoke_batch_endpoint.py` performs a preflight check at startup
that catches this and points you back here.

The two `*.yml` files normally don't need editing — `invoke_batch_endpoint.py`
overrides the endpoint/deployment names at runtime. Edit them only if you
want to change the compute cluster, base image, or model reference.

### .env example

```dotenv
AZURE_SUBSCRIPTION_ID=11111111-2222-3333-4444-555555555555
AZURE_RESOURCE_GROUP=my-ml-rg
AZURE_ML_WORKSPACE=my-ml-workspace

INPUT_DATA_ASSET=azureml:customer_batch_input:1
INPUT_TYPE=uri_file

BATCH_ENDPOINT_YAML=batch-endpoint.yml
BATCH_DEPLOYMENT_YAML=batch-deployment.yml
BATCH_ENDPOINT_PREFIX=hello-batch

DELETE_ENDPOINT_AFTER_INVOKE=true
```

See `.env.template` for full inline documentation of each variable.

## Usage

### 1. Run a full create → invoke → cleanup cycle

```powershell
python invoke_batch_endpoint.py
```

What happens, in order:

1. Loads `.env`.
2. Generates a unique 8-hex suffix for the run (e.g. `3f9c1a7b`) and
   names the endpoint `hello-batch-3f9c1a7b`, deployment `dep-3f9c1a7b`,
   and HTTP log `batch_invoke_3f9c1a7b.log`. Everything for one run
   shares this suffix.
3. Authenticates via `DefaultAzureCredential` (cached Azure CLI / managed
   identity / VS Code), falling back to interactive browser login.
4. Creates the batch endpoint (== `az ml batch-endpoint create --file ...`).
5. Creates the batch deployment (== `az ml batch-deployment create --file ... --set-default`).
6. Calls `ml_client.batch_endpoints.invoke(...)` — the single actual
   inference-plane request.
7. Polls `ml_client.jobs.get(...)` every 15 seconds until the job reaches
   a terminal state.
8. If `DELETE_ENDPOINT_AFTER_INVOKE=true`, deletes the endpoint
   (cascade-deletes its deployments).

Both stdout and `batch_invoke_<suffix>.log` carry per-call summaries:
hostname, method, URL, request body, response status, response body. The
log file also includes the SDK's full headers via Azure's
`HttpLoggingPolicy`.

### 2. Analyze a run's HTTP traffic

```powershell
.\analyze.ps1                                            # most recent log
.\analyze.ps1 -LogFile .\batch_invoke_3f9c1a7b.log       # a specific log
.\analyze.ps1 -NoDiagram                                 # markdown only
```

Pipes a structured prompt + the log file path to `copilot -p` and writes
two files per run:

- `batch_invoke_<suffix>.analysis.md` — the markdown report
- `batch_invoke_<suffix>.diagram.drawio` — a draw.io diagram of the
  client VM → remote-service call graph (skip with `-NoDiagram`)

The markdown report
includes:

- A one-sentence callout of **the single actual invoke call** vs the
  surrounding noise.
- A chronological table of the first 8 calls with interpretation
  (IMDS token, endpoint lookup, deployment lookup, the invoke POST,
  status poll, endpoint delete...).
- An aggregated unique-endpoint count table.
- A control-plane vs data-plane breakdown.

### 3. Nuke every batch endpoint in the workspace

Useful if previous runs failed mid-flight and left orphan endpoints, or
when bootstrapping the project for the first time:

```powershell
python cleanup_batch_endpoints.py --dry-run   # list, don't delete
python cleanup_batch_endpoints.py             # interactive: type DELETE to confirm
python cleanup_batch_endpoints.py --yes       # non-interactive (CI-safe)
```

Deleting a batch endpoint cascade-deletes its deployments, so this is a
true single-pass cleanup.

## Run artifacts and naming

Everything produced by one run shares an 8-hex suffix:

| Artifact | Example | Notes |
|---|---|---|
| Azure ML endpoint | `hello-batch-3f9c1a7b` | Deleted at end of run if cleanup enabled |
| Azure ML deployment | `dep-3f9c1a7b` | Cascade-deleted with the endpoint |
| Local HTTP log | `batch_invoke_3f9c1a7b.log` | Gitignored |
| Local analysis | `batch_invoke_3f9c1a7b.analysis.md` | Produced by `analyze.ps1`, gitignored |
| Local diagram | `batch_invoke_3f9c1a7b.diagram.drawio` | Produced by `analyze.ps1` (omit with `-NoDiagram`), gitignored |

## Files that are gitignored

- `.env` — credentials and workspace identifiers
- `batch-endpoint.yml`, `batch-deployment.yml` — your customized copies
  (templates are tracked)
- `batch_invoke_*.log`, `batch_invoke_*.analysis.md`, `*.drawio` — run output
- `scripts.txt`, `file.ps1` — personal scratch scripts; equivalents are
  documented in the **Prerequisites** section above
- `*.zip`, `__pycache__/`, `.venv/`, `venv/`

`.amlignore` (committed) is the Azure ML-equivalent of `.gitignore` and
controls what gets uploaded as the **deployment's code snapshot**. It is
deliberately stricter than `.gitignore` — it excludes `.git/`, README,
template files, and the orchestration scripts so the scoring container
only receives `score.py` + `conda.yml`. Azure ML uses `.amlignore` if
present; otherwise it falls back to `.gitignore` (which doesn't cover
`.git/` and will leak local git history into workspace storage).

## How the SDK actually talks to Azure (TL;DR)

The Azure ML SDK splits its traffic across two planes:

- **Control plane → `management.azure.com`** under
  `Microsoft.MachineLearningServices`. All metadata operations live here:
  endpoint create / get, deployment create / get, **job status polling**,
  endpoint delete. This dominates the log — the polling loop alone is one
  request every 15 seconds for the duration of the job.
- **Inference plane → `<endpoint>.<region>.inference.ml.azure.com`**. Only
  the actual scoring submission goes here. For a batch endpoint that's
  exactly **one** POST per `invoke()` call.

Run `analyze.ps1` after any run and you'll get this breakdown computed
for that specific log.

## Two different scoring contracts (don't mix them up)

Azure ML has two distinct programming models for "run my Python script
against some data", and they use **completely different argument
contracts**. This repo includes one example of each.

### Batch deployment (`score.py`)

Used by `batch-deployment.yml` and invoked through `ml_client.batch_endpoints.invoke()`.
Runs on the **Parallel Run Step (PRS)** engine.

- Your script **must** define `init()` and `run(mini_batch)` callbacks.
- Do **NOT** define your own `argparse` — the PRS driver injects a fixed
  set of arguments (`--model`, `--batch_endpoint_enabled`,
  `--mini_batch_size`, `--input_asset_job_data_path`, ...) and any
  unrecognized arg crashes with `SystemExit 2` and a confusing
  `usage: main.py [-h]...` error in the user logs.
- `mini_batch` is a list of file paths (for `UriFile`/`UriFolder` inputs)
  or a pandas DataFrame (for `MLTable` inputs).
- The return value of `run()` is appended to the deployment's output
  file according to the deployment's `output_action` (default
  `append_row` writes to `predictions.csv`).

### Command component (`score_component.py`)

Used by `component.yaml`, invoked as part of a pipeline job.
Runs as a plain Python process.

- Standard CLI script — you define your own `argparse` for whatever
  inputs/outputs the component declares.
- No `init()` / `run()` indirection. Reads the input file, writes the
  output folder, exits.
- This is what you want for pipeline steps, one-off training, and any
  "command-line" workload.

**Failure signal that you're using the wrong one:** if a batch job fails
in `user_logs/std_log_0.txt` with `main.py: error: unrecognized
arguments: --model ... --batch_endpoint_enabled True ...`, your scoring
script is using the Command Component contract (argparse) but the
deployment is calling it via PRS. Either swap to `init()`/`run()`, or
convert the workload to a pipeline job with a command component.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Default credential failed` → falls back to browser | Run `az login` once; you can also set `AZURE_TENANT_ID` if you're in multiple tenants. |
| Deployment fails with "compute not found" | Edit `batch-deployment.yml` to reference your actual compute cluster name (or create `cpu-cluster` per the Prerequisites). |
| Deployment fails reading the input | Cluster's managed identity is missing `Storage Blob Data Contributor` — see Prerequisites step 5. |
| Batch job fails at startup with "Failed to create table/queue ... authorization failure" or "private network" error | Cluster's managed identity is missing `Storage Table Data Contributor` and/or `Storage Queue Data Contributor`. Azure ML's parallel-run engine needs both. See Prerequisites step 5. |
| `.git/` or other local junk ends up in the deployment's code snapshot | `.gitignore` is honored by Azure ML uploads but does NOT exclude `.git/` itself. The included `.amlignore` is the right place to list everything that should be skipped from the code snapshot. |
| Batch job fails in `user_logs/std_log_0.txt` with `main.py: error: unrecognized arguments: --model ... --batch_endpoint_enabled True ...` | The scoring script is using the Command Component contract (argparse) instead of the PRS contract (`init()` + `run()`). See **Two different scoring contracts** above. `score.py` in this repo is PRS-style; `score_component.py` is Command-Component-style. |
| Preflight RBAC check silently skipped | Run `pip install -r requirements.txt` to install `pyyaml` and `azure-mgmt-authorization`. Without them the preflight `import` fails and the check is bypassed. |
| `INPUT_DATA_ASSET` not found | Register the asset (Prerequisites step 4) or update `.env` to point at one that exists. |
| Endpoint name collisions | Endpoint names are auto-generated per run; you'll only see collisions if you set the same suffix manually. Bump `BATCH_ENDPOINT_PREFIX` if needed. |
| Orphan endpoints from interrupted runs | `python cleanup_batch_endpoints.py --dry-run` to inspect, then drop `--dry-run` to delete. |
| `analyze.ps1` says "copilot not found" | Install [GitHub Copilot CLI](https://github.com/github/gh-copilot) and ensure `copilot` is on `PATH`. The raw log file is still usable without it. |
| `analyze.ps1` warns "drawio-ninja rules not found" | Install [drawio-ninja](https://github.com/simonpo/drawio-ninja) (see Prerequisites) or pass `-NoDiagram`. The markdown report is still produced. |
| `analyze.ps1` exits with "Diagram failed final validation" | The generated `.drawio` failed `validate.py`. Re-run; the diagram pass loops on validation but occasionally needs a second attempt. Pass `-NoDiagram` to bypass if blocking you. |

## For AI agents working on this repo

- The "source of truth" for project-specific values is `.env`. Never
  hardcode subscription IDs, resource group names, or workspace names
  into Python or PowerShell — read them from environment variables.
- Never commit `.env`, `batch-endpoint.yml`, or `batch-deployment.yml`
  — only their `.template` counterparts.
- `_run_suffix` in `invoke_batch_endpoint.py` is the single source of
  truth for run identity; reuse it if you need to tie a new artifact to
  the current run.
- The HTTP profiler lives in `RequestResponseProfilerPolicy` in
  `invoke_batch_endpoint.py`. To capture additional metadata per call,
  extend `on_request` / `on_response` there — both stdout summary and
  the file log will pick it up automatically.
- The Azure ML SDK uses `azure-core` HTTP pipelines; per-call logging
  requires both `logging_enable=True` on the client *and* a DEBUG-level
  `azure` logger with a handler attached. Don't remove either or bodies
  will silently get redacted.
- `score.py` and `score_component.py` are **not interchangeable**. They
  follow two different Azure ML contracts (PRS `init()`/`run()` vs
  Command Component argparse). Mixing them up is a silent failure: the
  SDK calls all succeed, the batch job appears to start, but
  `user_logs/std_log_0.txt` shows `unrecognized arguments: --model
  --batch_endpoint_enabled True ...`. See "Two different scoring
  contracts" above before changing either script.
- Storage RBAC for the cluster's managed identity needs **all four**
  roles (Blob/File/Table/Queue Data Contributor). Missing Table or
  Queue causes silent batch-job failures that don't show up in the
  HTTP profiling log. `invoke_batch_endpoint.py` runs a preflight check
  for this and points to `file1.ps1`; never disable the preflight check.
