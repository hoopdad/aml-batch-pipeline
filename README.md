# hello_component — Azure ML Batch Endpoint Test Harness

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
├── analyze.ps1                  # post-run: turn an HTTP log into a markdown report via copilot CLI
│
├── score.py                     # toy scoring script: stamps "approved" on every row
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
   identity needs `Storage Blob Data Contributor` on the workspace's
   default storage account so it can read inputs and write outputs:
   ```powershell
   $PRINCIPAL_ID = az ml compute show --name cpu-cluster `
       --resource-group $RG --workspace-name $WS `
       --query "identity.principal_id" -o tsv
   $STORAGE_ID = az ml workspace show --name $WS --resource-group $RG `
       --query "storage_account" -o tsv
   az role assignment create `
       --assignee-object-id $PRINCIPAL_ID `
       --assignee-principal-type ServicePrincipal `
       --role "Storage Blob Data Contributor" `
       --scope $STORAGE_ID
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
- **PowerShell 5.1+ or PowerShell 7** for `analyze.ps1`.

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
```

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
```

Pipes a structured prompt + the log file path to `copilot -p` and writes
the answer to `batch_invoke_<suffix>.analysis.md`. The markdown report
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

## Files that are gitignored

- `.env` — credentials and workspace identifiers
- `batch-endpoint.yml`, `batch-deployment.yml` — your customized copies
  (templates are tracked)
- `batch_invoke_*.log`, `batch_invoke_*.analysis.md` — run output
- `scripts.txt`, `file.ps1` — personal scratch scripts; equivalents are
  documented in the **Prerequisites** section above
- `*.zip`, `__pycache__/`, `.venv/`, `venv/`

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

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Default credential failed` → falls back to browser | Run `az login` once; you can also set `AZURE_TENANT_ID` if you're in multiple tenants. |
| Deployment fails with "compute not found" | Edit `batch-deployment.yml` to reference your actual compute cluster name (or create `cpu-cluster` per the Prerequisites). |
| Deployment fails reading the input | Cluster's managed identity is missing `Storage Blob Data Contributor` — see Prerequisites step 5. |
| `INPUT_DATA_ASSET` not found | Register the asset (Prerequisites step 4) or update `.env` to point at one that exists. |
| Endpoint name collisions | Endpoint names are auto-generated per run; you'll only see collisions if you set the same suffix manually. Bump `BATCH_ENDPOINT_PREFIX` if needed. |
| Orphan endpoints from interrupted runs | `python cleanup_batch_endpoints.py --dry-run` to inspect, then drop `--dry-run` to delete. |
| `analyze.ps1` says "copilot not found" | Install [GitHub Copilot CLI](https://github.com/github/gh-copilot) and ensure `copilot` is on `PATH`. The raw log file is still usable without it. |

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
