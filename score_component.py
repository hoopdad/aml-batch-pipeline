import argparse
import pandas as pd
import os

# Parse AML input/output arguments
parser = argparse.ArgumentParser()

parser.add_argument("--input_data", type=str)
parser.add_argument("--output_predictions", type=str)

args = parser.parse_args()

# Read input CSV from AML-mounted path
df = pd.read_csv(args.input_data)

# Fake prediction logic
df["prediction"] = "approved"

# Ensure output folder exists
os.makedirs(args.output_predictions, exist_ok=True)

# Save predictions
output_path = os.path.join(
    args.output_predictions,
    "predictions.csv"
)

df.to_csv(output_path, index=False)

print("Batch scoring complete")
print(df.head())
print(f"Predictions saved to: {output_path}")
