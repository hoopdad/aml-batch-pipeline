import os
import re
import sys
import time
import uuid
import logging
import traceback
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
from azure.identity import (
    DefaultAzureCredential,
    InteractiveBrowserCredential,
)
from azure.ai.ml import MLClient, Input, load_batch_endpoint, load_batch_deployment
from azure.ai.ml.constants import AssetTypes
from azure.core.pipeline.policies import SansIOHTTPPolicy

# ============================================================
# CONFIGURATION (from .env)
# ============================================================

load_dotenv(Path(__file__).parent / ".env")


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        sys.exit(f"ERROR: required environment variable '{key}' is not set (check .env).")
    return val


SUBSCRIPTION_ID = _required("AZURE_SUBSCRIPTION_ID")
RESOURCE_GROUP  = _required("AZURE_RESOURCE_GROUP")
WORKSPACE_NAME  = _required("AZURE_ML_WORKSPACE")

INPUT_DATA_ASSET = _required("INPUT_DATA_ASSET")
INPUT_TYPE       = os.environ.get("INPUT_TYPE", AssetTypes.URI_FILE)

BATCH_ENDPOINT_YAML   = os.environ.get("BATCH_ENDPOINT_YAML", "batch-endpoint.yml")
BATCH_DEPLOYMENT_YAML = os.environ.get("BATCH_DEPLOYMENT_YAML", "batch-deployment.yml")
ENDPOINT_PREFIX       = os.environ.get("BATCH_ENDPOINT_PREFIX", "hello-batch")

# When true, delete the endpoint (and its deployments) at the end of the run.
DELETE_ENDPOINT_AFTER_INVOKE = os.environ.get(
    "DELETE_ENDPOINT_AFTER_INVOKE", "true"
).strip().lower() in ("1", "true", "yes", "y")

# Build a fresh, globally-unique-within-workspace name on every run.
# Azure ML endpoint name rules: 3-32 chars, lowercase alphanumeric + hyphens,
# must start with a letter. Prefix + 8-hex suffix stays well under 32 chars.
_run_suffix     = uuid.uuid4().hex[:8]
ENDPOINT_NAME   = f"{ENDPOINT_PREFIX}-{_run_suffix}"[:32]
DEPLOYMENT_NAME = f"dep-{_run_suffix}"

POLL_INTERVAL_SECONDS = 15
POLL_TIMEOUT_MINUTES  = 60

# ============================================================
# HTTP PROFILING
# ============================================================
# Captures every HTTP request/response made by the SDK.
# - Full headers + bodies are written to PROFILE_LOG_FILE via the
#   built-in azure.core HttpLoggingPolicy (DEBUG level).
# - A concise per-call summary (hostname, method, URL, body, status)
#   is printed to stdout by the custom policy below.

PROFILE_LOG_FILE = f"batch_invoke_{_run_suffix}.log"
MAX_BODY_PREVIEW = 2000  # chars/bytes printed inline


class RedactingFilter(logging.Filter):
    """Strip secrets out of azure-core log records before they're written.

    Azure's HttpLoggingPolicy redacts Authorization/SAS tokens at INFO level,
    but the lower-level azure.core.pipeline.policies._universal DEBUG logger
    emits full bearer tokens and full SAS query strings. This filter masks
    them post-format so neither the file log nor stdout ever contains them.
    """

    _patterns = [
        # 'Authorization': 'Bearer eyJ...'  (header dict dump form)
        (re.compile(r"(['\"]Authorization['\"]\s*:\s*['\"])[^'\"]+(['\"])"),
         r"\1<REDACTED>\2"),
        # Authorization: Bearer eyJ...  (raw header form)
        (re.compile(r"(Authorization:\s*Bearer\s+)\S+"),
         r"\1<REDACTED>"),
        # Bare bearer tokens that survive the above (paranoia)
        (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{40,}"),
         r"\1<REDACTED>"),
        # SAS query-string secrets + ARM LRO continuation tokens (t/c/s/h
        # carry signed payloads with embedded certs). api-version stays
        # visible because it's public and useful for debugging.
        (re.compile(r"([?&](?:sig|skoid|sktid|sks|sv|sp|skv|st|se|sr|ske|skt|t|c|s|h)=)[^&'\"\s]+"),
         r"\1<REDACTED>"),
    ]

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        for pat, repl in self._patterns:
            msg = pat.sub(repl, msg)
        # Replace the record's payload so the formatter doesn't re-interpolate args
        record.msg = msg
        record.args = ()
        return True


_azure_logger = logging.getLogger("azure")
_azure_logger.setLevel(logging.DEBUG)
_file_handler = logging.FileHandler(PROFILE_LOG_FILE, mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
)
_file_handler.addFilter(RedactingFilter())
_azure_logger.addHandler(_file_handler)
_azure_logger.propagate = False


class RequestResponseProfilerPolicy(SansIOHTTPPolicy):
    """Print a clean per-call summary of every HTTP request and response."""

    _counter = 0
    _USER_SCRIPT = os.path.abspath(__file__)

    @classmethod
    def _find_user_caller(cls):
        """Walk the live stack and return (filename, lineno, function) of the
        deepest frame that lives in user code (i.e. not inside azure/* and
        not inside site-packages / the Python stdlib). Falls back to the
        outermost frame of this script if nothing else matches."""
        stack = traceback.extract_stack()
        best = None
        for frame in stack:
            fn = os.path.abspath(frame.filename)
            low = fn.lower()
            if "site-packages" in low:
                continue
            if os.sep + "azure" + os.sep in low:
                continue
            # Skip stdlib / this policy itself
            if low.endswith(os.sep + "traceback.py"):
                continue
            best = frame
        # Prefer an exact match in the user script if we found one
        for frame in stack:
            if os.path.abspath(frame.filename) == cls._USER_SCRIPT:
                best = frame
                break
        if best is None:
            return ("<unknown>", 0, "<unknown>")
        return (best.filename, best.lineno, best.name)

    def on_request(self, request):
        RequestResponseProfilerPolicy._counter += 1
        self._call_id = RequestResponseProfilerPolicy._counter
        http_request = request.http_request
        parsed = urlparse(http_request.url)
        caller_file, caller_line, caller_func = self._find_user_caller()
        caller_str = f"{os.path.basename(caller_file)}:{caller_line} in {caller_func}()"
        # Marker into the file log so it lines up with the SDK's own DEBUG entries
        _azure_logger.debug(
            "PROFILER REQUEST #%d %s %s  <- caller %s",
            self._call_id, http_request.method, http_request.url, caller_str,
        )
        print(f"\n>>> HTTP REQUEST #{self._call_id}")
        print(f"    Caller:   {caller_str}")
        print(f"    Method:   {http_request.method}")
        print(f"    Hostname: {parsed.hostname}")
        print(f"    URL:      {http_request.url}")
        body = http_request.body
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                try:
                    preview = body[:MAX_BODY_PREVIEW].decode("utf-8", errors="replace")
                except Exception:
                    preview = repr(body[:MAX_BODY_PREVIEW])
            else:
                preview = str(body)[:MAX_BODY_PREVIEW]
            print(f"    Body:     {preview}")
        else:
            print("    Body:     <none>")

    def on_response(self, request, response):
        http_response = response.http_response
        call_id = getattr(self, "_call_id", "?")
        _azure_logger.debug(
            "PROFILER RESPONSE #%s %s %s",
            call_id, http_response.status_code, http_response.request.url,
        )
        print(f"<<< HTTP RESPONSE #{call_id}")
        print(f"    Status:   {http_response.status_code} {getattr(http_response, 'reason', '')}")
        print(f"    URL:      {http_response.request.url}")
        try:
            text = http_response.text()
        except Exception as exc:
            text = f"<unreadable body: {exc}>"
        if text:
            preview = text[:MAX_BODY_PREVIEW]
            suffix = "..." if len(text) > MAX_BODY_PREVIEW else ""
            print(f"    Body:     {preview}{suffix}")
        else:
            print("    Body:     <empty>")


print(f"HTTP profiling enabled. Full trace -> {PROFILE_LOG_FILE}")

# ============================================================
# AUTHENTICATION
# ============================================================

credential = None
try:
    print("Trying DefaultAzureCredential (az login / cached credentials)...")
    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    credential.get_token("https://management.azure.com/.default")
    print("Using cached Azure credentials.")
except Exception as exc:
    print(f"Default credential failed: {exc}")
    print("Falling back to Interactive Browser login...")
    credential = InteractiveBrowserCredential()

# ============================================================
# AML CLIENT
# ============================================================

ml_client = MLClient(
    credential=credential,
    subscription_id=SUBSCRIPTION_ID,
    resource_group_name=RESOURCE_GROUP,
    workspace_name=WORKSPACE_NAME,
    logging_enable=True,
    additional_policies=[RequestResponseProfilerPolicy()],
)

# ============================================================
# PREFLIGHT: verify the compute cluster's managed identity has the
# Storage roles required for batch scoring jobs to start successfully.
#
# Why this matters: if Table or Queue Data Contributor are missing,
# the SDK calls (create endpoint, create deployment, invoke, poll)
# all succeed with 200/201/202, but the batch job itself fails at
# startup ~5-10 minutes later with a misleading "private network /
# authorization failure" error inside the parallel-run engine. The
# HTTP profiling log shows nothing wrong. Failing here (in ~2 sec)
# saves a lot of confusion.
# ============================================================

REQUIRED_CLUSTER_ROLES = {
    "Storage Blob Data Contributor",             # inputs/outputs
    "Storage File Data Privileged Contributor",  # workspace file share
    "Storage Table Data Contributor",            # parallel-run engine tracking
    "Storage Queue Data Contributor",            # parallel-run engine coordination
}


def _preflight_check_cluster_rbac():
    """Best-effort check that the deployment cluster's MI has the four
    Storage roles batch scoring needs. Aborts with a clear remediation
    pointer if any are missing. Silently skips if anything about the
    lookup fails (e.g. permission to read role assignments), since the
    real failure (the job itself) is still visible downstream."""
    try:
        # Read the cluster name out of the deployment YAML
        import yaml
        with open(BATCH_DEPLOYMENT_YAML, "r", encoding="utf-8") as f:
            dep_yaml = yaml.safe_load(f) or {}
        compute_ref = dep_yaml.get("compute", "")
        # Format is "azureml:<cluster-name>" or "azureml:<cluster-name>:<ver>"
        cluster_name = compute_ref.split(":")[1] if ":" in compute_ref else compute_ref
        if not cluster_name:
            print("Preflight: could not parse compute cluster from "
                  f"'{BATCH_DEPLOYMENT_YAML}'; skipping RBAC check.")
            return

        # Look up cluster MI principal id + workspace storage account scope
        cluster = ml_client.compute.get(cluster_name)
        principal_id = getattr(getattr(cluster, "identity", None), "principal_id", None)
        if not principal_id:
            print(f"Preflight: cluster '{cluster_name}' has no system-assigned "
                  "managed identity; skipping RBAC check. Enable it with:\n"
                  f"  az ml compute update --name {cluster_name} "
                  f"--resource-group {RESOURCE_GROUP} "
                  f"--workspace-name {WORKSPACE_NAME} --identity-type SystemAssigned")
            return

        ws = ml_client.workspaces.get(WORKSPACE_NAME)
        storage_scope = ws.storage_account

        # Use the Authorization Management client to list role assignments
        from azure.mgmt.authorization import AuthorizationManagementClient
        auth_client = AuthorizationManagementClient(credential, SUBSCRIPTION_ID)
        assignments = list(auth_client.role_assignments.list_for_scope(
            scope=storage_scope,
            filter=f"principalId eq '{principal_id}'"
        ))
        # Map role definition IDs to names
        present = set()
        for a in assignments:
            try:
                rd = auth_client.role_definitions.get_by_id(a.role_definition_id)
                present.add(rd.role_name)
            except Exception:
                pass

        missing = REQUIRED_CLUSTER_ROLES - present
        if missing:
            print()
            print("=" * 60)
            print("PREFLIGHT FAILURE: cluster managed identity is missing required Storage roles.")
            print("=" * 60)
            print(f"Cluster:        {cluster_name}")
            print(f"Principal ID:   {principal_id}")
            print(f"Storage scope:  {storage_scope}")
            print(f"Missing roles:  {', '.join(sorted(missing))}")
            print()
            print("Without these, the batch job will appear to submit successfully")
            print("but fail at startup ~5-10 minutes later with a misleading")
            print("'private network / authorization failure' error.")
            print()
            print("Fix by running the included setup script:")
            print(f"  .\\file1.ps1 -Cluster {cluster_name}")
            sys.exit(3)
        else:
            print(f"Preflight OK: cluster '{cluster_name}' has all 4 required Storage roles.")
    except SystemExit:
        raise
    except ImportError as exc:
        print(f"Preflight: skipping RBAC check ({exc}). "
              "Install pyyaml + azure-mgmt-authorization to enable it.")
    except Exception as exc:
        print(f"Preflight: RBAC check skipped due to error: {exc}")


_preflight_check_cluster_rbac()

# ============================================================
# CREATE BATCH ENDPOINT  (== az ml batch-endpoint create --file batch-endpoint.yml)
# ============================================================

print(f"\nCreating batch endpoint '{ENDPOINT_NAME}' from '{BATCH_ENDPOINT_YAML}'...")
endpoint = load_batch_endpoint(source=BATCH_ENDPOINT_YAML)
endpoint.name = ENDPOINT_NAME  # override YAML name with our per-run unique name
endpoint = ml_client.batch_endpoints.begin_create_or_update(endpoint).result()
print(f"Endpoint created: {endpoint.name}  (scoring_uri={getattr(endpoint, 'scoring_uri', None)})")

# ============================================================
# CREATE BATCH DEPLOYMENT  (== az ml batch-deployment create --file batch-deployment.yml --set-default)
# ============================================================

print(f"\nCreating batch deployment '{DEPLOYMENT_NAME}' from '{BATCH_DEPLOYMENT_YAML}'...")
deployment = load_batch_deployment(source=BATCH_DEPLOYMENT_YAML)
deployment.name = DEPLOYMENT_NAME
deployment.endpoint_name = ENDPOINT_NAME  # retarget at the endpoint we just created
deployment = ml_client.batch_deployments.begin_create_or_update(deployment).result()
print(f"Deployment created: {deployment.name}")

print(f"\nMarking '{deployment.name}' as the default deployment for endpoint '{endpoint.name}'...")
endpoint.defaults.deployment_name = deployment.name
endpoint = ml_client.batch_endpoints.begin_create_or_update(endpoint).result()
print(f"Default deployment is now: {endpoint.defaults.deployment_name}")

# ============================================================
# INVOKE BATCH ENDPOINT
# ============================================================

print(f"\nInvoking batch endpoint '{ENDPOINT_NAME}' (deployment '{DEPLOYMENT_NAME}')...\n")

job = ml_client.batch_endpoints.invoke(
    endpoint_name=ENDPOINT_NAME,
    deployment_name=DEPLOYMENT_NAME,
    input=Input(type=INPUT_TYPE, path=INPUT_DATA_ASSET),
    logging_enable=True,
)

job_name = job.name
studio_url = getattr(job, "studio_url", None) or getattr(
    getattr(job, "services", {}).get("Studio", None), "endpoint", None
)

print(f"Batch job submitted.")
print(f"Job name: {job_name}")
if studio_url:
    print(f"Studio:   {studio_url}")

# ============================================================
# POLL FOR STATUS
# ============================================================

terminal_states = {"completed", "failed", "canceled", "cancelled", "notresponding"}

print("\nPolling for completion...\n")

deadline = time.time() + POLL_TIMEOUT_MINUTES * 60
current_job = job
last_status = None
timed_out = False

while True:
    current_job = ml_client.jobs.get(job_name)
    status = current_job.status or "Unknown"

    if status != last_status:
        print(f"Current status: {status}")
        last_status = status

    if status.lower() in terminal_states:
        break

    if time.time() > deadline:
        print(f"\nTimeout: job did not reach a terminal state within {POLL_TIMEOUT_MINUTES} minutes.")
        print(f"Last known status: {status}")
        timed_out = True
        break

    time.sleep(POLL_INTERVAL_SECONDS)

# ============================================================
# FINAL RESULT
# ============================================================

print("\n==================================================")
print("FINAL STATUS")
print("==================================================")
print(f"Job status: {current_job.status}")

outputs = getattr(current_job, "outputs", None) or {}
if outputs:
    print("\nOutputs:")
    for output_name, output_value in outputs.items():
        path = getattr(output_value, "path", None)
        print(f"- {output_name}: {path if path else output_value}")

if studio_url:
    print(f"\nMonitor in AML Studio: {studio_url}")

# ============================================================
# CLEANUP (== az ml batch-endpoint delete --name <ENDPOINT_NAME>)
# ============================================================

if DELETE_ENDPOINT_AFTER_INVOKE:
    print(f"\nDELETE_ENDPOINT_AFTER_INVOKE=true — deleting endpoint '{ENDPOINT_NAME}' "
          "(this cascade-deletes its deployments)...")
    try:
        ml_client.batch_endpoints.begin_delete(name=ENDPOINT_NAME).result()
        print(f"Deleted endpoint '{ENDPOINT_NAME}'.")
    except Exception as exc:
        print(f"WARNING: could not delete endpoint '{ENDPOINT_NAME}': {exc}")
else:
    print(f"\nDELETE_ENDPOINT_AFTER_INVOKE=false — leaving endpoint '{ENDPOINT_NAME}' in place.")

# ============================================================
# EXIT
# ============================================================

if timed_out:
    sys.exit(2)
if current_job.status and current_job.status.lower() != "completed":
    sys.exit(1)
