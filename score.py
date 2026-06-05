"""
Batch deployment scoring script — Parallel Run Step (PRS) contract.

Required entry points:
  - init(): called once per worker process at startup. Use this to load the
    model, set up any expensive resources, and read AZUREML_MODEL_DIR if
    needed. No arguments, no return value.
  - run(mini_batch): called for each mini-batch the PRS engine dispatches.
    `mini_batch` is a list of input file paths (when input_format is
    UriFile/UriFolder) or a pandas DataFrame (when input_format is MLTable).
    Return value is appended to the deployment's output file according to
    the deployment's output_action setting.

DO NOT define your own argparse here — the PRS driver passes a fixed set
of arguments (--model, --batch_endpoint_enabled, --mini_batch_size, ...)
and unknown args cause a SystemExit 2.

Reference:
  https://learn.microsoft.com/azure/machine-learning/how-to-deploy-batch-with-rest
  https://learn.microsoft.com/python/api/overview/azure/ai-ml-readme#batch-endpoints
"""

import os
import pandas as pd


def init():
    """Called once per worker at startup."""
    print("Scoring init: worker process starting.")
    model_dir = os.environ.get("AZUREML_MODEL_DIR")
    if model_dir:
        print(f"  AZUREML_MODEL_DIR = {model_dir}")
        try:
            contents = os.listdir(model_dir)
            print(f"  Model folder contents: {contents}")
        except Exception as exc:
            print(f"  (could not list model folder: {exc})")
    # In a real scorer you'd load the model here and stash it on a module
    # global. This dummy scorer doesn't need a model.


def run(mini_batch):
    """Score one mini-batch.

    Args:
      mini_batch: list[str] of file paths to process (for UriFile/UriFolder
        inputs). Each file is one CSV in this example.

    Returns:
      pandas.DataFrame appended row-wise to predictions.csv (because the
      deployment is configured with output_action=append_row).
    """
    print(f"Scoring mini-batch of {len(mini_batch)} file(s).")
    frames = []
    for path in mini_batch:
        print(f"  reading {path}")
        df = pd.read_csv(path)
        df["prediction"] = "approved"
        df["source_file"] = os.path.basename(str(path))
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    print(f"  produced {len(result)} prediction rows.")
    return result
