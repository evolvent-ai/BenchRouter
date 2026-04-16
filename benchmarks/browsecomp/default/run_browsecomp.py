#!/usr/bin/env python3
"""
BenchRouter <-> BrowseComp bridge script.

Translates BenchRouter environment variables into simple-evals arguments,
runs the BrowseComp evaluation, and writes an BenchRouter-compatible result.json.

Runs inside the benchrouter-browsecomp Docker container where:
  - simple-evals code lives at /app/simple_evals/ (PYTHONPATH=/app)
  - This script is mounted at /app/benchmark/run_browsecomp.py (read-only)
  - Results must be written to BENCHROUTER_OUTPUT_DIR (default /app/results)
"""

import glob
import json
import os
import subprocess
import sys


def main():
    # 1. Read BenchRouter environment variables
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

    if not model_endpoint:
        print("ERROR: BENCHROUTER_MODEL_ENDPOINT is not set", file=sys.stderr)
        sys.exit(1)

    # 2. Set OPENAI_BASE_URL so the OpenAI client in simple-evals talks to our endpoint
    os.environ["OPENAI_BASE_URL"] = model_endpoint
    os.environ["OPENAI_API_KEY"] = api_key

    # 3. Run simple-evals browsecomp evaluation
    grader_model = os.environ.get("BENCHROUTER_GRADER_MODEL", "gpt-4.1-2025-04-14")
    examples = os.environ.get("BENCHROUTER_EXAMPLES", "")
    cmd = [
        sys.executable, "-m", "simple_evals.simple_evals",
        "--model", model_name,
        "--eval", "browsecomp",
        "--grader", grader_model,
    ]
    if examples.isdigit() and int(examples) > 0:
        cmd.extend(["--examples", examples])

    print(f"Running BrowseComp evaluation: {' '.join(cmd)}")
    print(f"Model: {model_name}")
    print(f"Endpoint: {model_endpoint}")

    result = subprocess.run(cmd, cwd="/app", timeout=14400)

    if result.returncode != 0:
        print(f"WARNING: simple-evals exited with code {result.returncode}",
              file=sys.stderr)

    # 4. Collect results from /tmp/browsecomp_*.json
    safe_name = model_name.replace("/", "_")
    result_files = sorted(glob.glob(f"/tmp/browsecomp_{safe_name}*.json"))
    # Exclude _allresults.json files (they contain full conversation data, too large)
    result_files = [f for f in result_files if "_allresults" not in f]

    metrics = {}
    score = None
    if result_files:
        # Take the most recent result file
        with open(result_files[-1]) as f:
            metrics = json.load(f)
        score = metrics.get("score")
        print(f"Found metrics: {metrics}")
    else:
        print("WARNING: No result files found in /tmp/", file=sys.stderr)

    # 5. Build BenchRouter result.json
    benchrouter_result = {
        "overall": score if score is not None else 0.0,
        "metrics": metrics,
        "browsecomp_config": {
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    if score is None:
        benchrouter_result["error"] = "BrowseComp evaluation produced no results"

    # 6. Write result.json
    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result written to {result_path}")
    print(f"Overall score: {benchrouter_result['overall']}")


if __name__ == "__main__":
    main()
