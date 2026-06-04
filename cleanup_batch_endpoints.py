"""
One-time cleanup utility: deletes every batch endpoint in the configured
Azure ML workspace. Deleting an endpoint cascade-deletes all of its
deployments, so we don't need to delete deployments individually.

Reads workspace targeting from .env (same file as invoke_batch_endpoint.py).

Usage:
  python cleanup_batch_endpoints.py            # interactive confirm
  python cleanup_batch_endpoints.py --yes      # skip confirmation
  python cleanup_batch_endpoints.py --dry-run  # list, don't delete
"""

import os
import sys
import argparse
from pathlib import Path
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential
from azure.ai.ml import MLClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the interactive confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List endpoints and deployments without deleting.")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent / ".env")

    def _required(key: str) -> str:
        val = os.environ.get(key)
        if not val:
            sys.exit(f"ERROR: required env var '{key}' is not set (check .env).")
        return val

    subscription_id = _required("AZURE_SUBSCRIPTION_ID")
    resource_group  = _required("AZURE_RESOURCE_GROUP")
    workspace_name  = _required("AZURE_ML_WORKSPACE")

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        credential.get_token("https://management.azure.com/.default")
    except Exception as exc:
        print(f"Default credential failed: {exc}")
        print("Falling back to Interactive Browser login...")
        credential = InteractiveBrowserCredential()

    ml_client = MLClient(
        credential=credential,
        subscription_id=subscription_id,
        resource_group_name=resource_group,
        workspace_name=workspace_name,
    )

    print(f"\nWorkspace: {subscription_id} / {resource_group} / {workspace_name}")
    print("Scanning batch endpoints...\n")

    endpoints = list(ml_client.batch_endpoints.list())
    if not endpoints:
        print("No batch endpoints found. Nothing to clean up.")
        return 0

    for ep in endpoints:
        try:
            deps = list(ml_client.batch_deployments.list(endpoint_name=ep.name))
            dep_names = [d.name for d in deps] or ["<none>"]
        except Exception as exc:
            dep_names = [f"<error listing deployments: {exc}>"]
        print(f"  - {ep.name}  (deployments: {', '.join(dep_names)})")

    if args.dry_run:
        print(f"\n--dry-run set: would delete {len(endpoints)} endpoint(s). No changes made.")
        return 0

    if not args.yes:
        print(f"\nAbout to DELETE {len(endpoints)} batch endpoint(s) and ALL their deployments.")
        reply = input("Type 'DELETE' to confirm: ").strip()
        if reply != "DELETE":
            print("Aborted.")
            return 1

    failures = 0
    for ep in endpoints:
        print(f"\nDeleting endpoint '{ep.name}'...")
        try:
            ml_client.batch_endpoints.begin_delete(name=ep.name).result()
            print(f"  Deleted '{ep.name}'.")
        except Exception as exc:
            failures += 1
            print(f"  WARNING: failed to delete '{ep.name}': {exc}")

    print(f"\nDone. {len(endpoints) - failures}/{len(endpoints)} endpoint(s) deleted.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
