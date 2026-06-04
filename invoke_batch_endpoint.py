import os
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

_azure_logger = logging.getLogger("azure")
_azure_logger.setLevel(logging.DEBUG)
_file_handler = logging.FileHandler(PROFILE_LOG_FILE, mode="w", encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
)
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
