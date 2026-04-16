#!/usr/bin/env python3
"""
BenchRouter <-> xbench bridge script.

Patches xbench's get_llm_response to route through BenchRouter's endpoint,
then runs the official xbench_evals.py entry point as-is.
"""

import csv
import json
import os
import sys

import numpy as np
from openai import OpenAI

# Add official xbench-evals to Python path
sys.path.insert(0, "/app/xbench-evals")


def patch_xbench():
    """Monkey-patch xbench's get_llm_response to use BenchRouter's endpoint."""
    import language_models

    model_endpoint = os.environ["BENCHROUTER_MODEL_ENDPOINT"]
    model_name = os.environ["BENCHROUTER_MODEL_NAME"]
    api_key = os.environ["OPENAI_API_KEY"]
    judge_model = os.environ.get("XBENCH_JUDGE_MODEL", model_name)
    judge_endpoint = os.environ.get("XBENCH_JUDGE_ENDPOINT", model_endpoint)
    judge_api_key = os.environ.get("XBENCH_JUDGE_API_KEY", api_key)

    model_client = OpenAI(api_key=api_key, base_url=model_endpoint, timeout=180.0)
    judge_client = OpenAI(api_key=judge_api_key, base_url=judge_endpoint, timeout=120.0)

    def patched_get_llm_response(prompt, model, judge=False):
        client = judge_client if judge else model_client
        actual_model = judge_model if judge else model_name
        try:
            resp = client.chat.completions.create(
                model=actual_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            if judge:
                return resp.choices[0].message.content
            finish_reason = resp.choices[0].finish_reason
            return {
                "response": resp.choices[0].message.content,
                "cost": None,
                "length_cutoff": (finish_reason == "length"),
                "safety_cutoff": (finish_reason == "content_filter"),
                "api_error": False,
            }
        except Exception as e:
            print(f"API error: {e}")
            if judge:
                return None
            return {
                "response": None, "cost": None,
                "length_cutoff": False, "safety_cutoff": False, "api_error": True,
            }

    language_models.get_llm_response = patched_get_llm_response
    print(f"Patched get_llm_response -> {model_name} @ {model_endpoint}")
    print(f"Judge -> {judge_model} @ {judge_endpoint}")


def main():
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")
    n_repeats = int(os.environ.get("XBENCH_N_REPEATS", "1"))

    if not model_endpoint or not api_key:
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "result.json"), "w") as f:
            json.dump({"overall": 0.0, "error": "BENCHROUTER_MODEL_ENDPOINT or OPENAI_API_KEY not set"}, f)
        sys.exit(1)

    # Patch API routing before running official code
    patch_xbench()

    # Run official xbench_evals.py via its main(), faking sys.argv
    # Sanitize model name for filesystem (e.g. "anthropic/claude-sonnet-4.5" -> "anthropic_claude-sonnet-4.5")
    safe_model_name = model_name.replace("/", "_")
    dataset_file = os.environ.get("XBENCH_DATASET", "data/ScienceQA.csv")
    dataset = f"/app/xbench-evals/{dataset_file}"

    # Optional: limit number of questions for quick testing
    limit = int(os.environ.get("XBENCH_LIMIT", "0"))
    if limit > 0:
        import tempfile
        with open(dataset, encoding="utf-8") as fin:
            lines = fin.readlines()
        limited_path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, dir="/tmp"
        )
        limited_path.write(lines[0])  # header
        for line in lines[1:limit + 1]:
            limited_path.write(line)
        limited_path.close()
        dataset = limited_path.name
        print(f"XBENCH_LIMIT={limit}: using {dataset} with {limit} questions")

    os.chdir("/app/xbench-evals")
    sys.argv = ["xbench_evals.py", "--model", safe_model_name, "--dataset", dataset, "--n-repeats", str(n_repeats)]

    from xbench_evals import main as xbench_main
    xbench_main()

    # Parse the official output CSV -> BenchRouter result.json
    csv_file = f"{safe_model_name}_results.csv"
    avg_scores = []
    best_scores = []
    mv_scores = []

    with open(csv_file, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            avg_scores.append(float(row["avg_score"]))
            best_scores.append(float(row["best_of_n"]))
            mv_scores.append(float(row["majority_vote_score"]))

    overall = float(np.mean(avg_scores)) if avg_scores else 0.0

    benchrouter_result = {
        "overall": round(overall, 4),
        "metrics": {
            "avg_score": round(overall, 4),
            "best_of_n": round(float(np.mean(best_scores)), 4) if best_scores else 0.0,
            "majority_vote": round(float(np.mean(mv_scores)), 4) if mv_scores else 0.0,
            "n_questions": len(avg_scores),
            "n_repeats": n_repeats,
        },
        "xbench_config": {
            "dataset": dataset_file,
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result written to {result_path}")
    print(f"Overall: {overall*100:.1f}%")


if __name__ == "__main__":
    main()
