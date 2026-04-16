#!/usr/bin/env python3
"""
BenchRouter <-> lmms-eval bridge script.

Translates BenchRouter environment variables into lmms-eval arguments,
runs the evaluation via Python API, and writes an BenchRouter-compatible
result.json.

Supports Anthropic-compatible API endpoints (e.g. Claude via OpenAI-compat
proxy) by converting image_url content blocks to Anthropic's image format
when ANTHROPIC_COMPAT=1 is set.

Runs inside the benchrouter-lmms-eval Docker container where:
  - lmms-eval is pip-installed
  - This script is mounted at /app/benchmark/run_lmms_eval.py (read-only)
  - Results must be written to BENCHROUTER_OUTPUT_DIR (default /app/results)
"""

import json
import os
import sys


def patch_anthropic_compat():
    """Monkey-patch lmms-eval to convert image_url blocks to Anthropic format.

    Anthropic-compatible proxies expect:
        {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}
    instead of OpenAI's:
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    """
    from lmms_eval.protocol import ChatMessages

    original = ChatMessages.to_openai_messages

    def patched(self, video_kwargs=None):
        messages = original(self, video_kwargs)
        for msg in messages:
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            converted = []
            for block in content:
                if not isinstance(block, dict):
                    converted.append(block)
                    continue
                if block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        header, data = url.split(",", 1)
                        media_type = header.split(":")[1].split(";")[0]
                        converted.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        })
                        continue
                converted.append(block)
            msg["content"] = converted
        return messages

    ChatMessages.to_openai_messages = patched
    print("Applied Anthropic-compatible image format patch")


def write_error_result(output_dir, error_msg, task_name, model_name, model_endpoint):
    """Write an error-bearing result.json so BenchRouter can report failures."""
    os.makedirs(output_dir, exist_ok=True)
    result = {
        "overall": 0.0,
        "error": error_msg,
        "metrics": {},
        "lmms_eval_config": {
            "task": task_name,
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }
    with open(os.path.join(output_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)


def main():
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")
    task_name = os.environ.get("LMMS_EVAL_TASK", "mmmu_val")
    anthropic_compat = os.environ.get("ANTHROPIC_COMPAT", "0") == "1"
    primary_metric = os.environ.get("LMMS_EVAL_PRIMARY_METRIC", "")
    limit_str = os.environ.get("LMMS_EVAL_LIMIT", "")
    limit = int(limit_str) if limit_str.isdigit() and int(limit_str) > 0 else None

    if not model_endpoint:
        print("ERROR: BENCHROUTER_MODEL_ENDPOINT is not set", file=sys.stderr)
        write_error_result(output_dir, "BENCHROUTER_MODEL_ENDPOINT not set",
                           task_name, model_name, model_endpoint)
        sys.exit(1)

    if not api_key:
        print("ERROR: OPENAI_API_KEY is not set", file=sys.stderr)
        write_error_result(output_dir, "OPENAI_API_KEY not set",
                           task_name, model_name, model_endpoint)
        sys.exit(1)

    try:
        if anthropic_compat:
            patch_anthropic_compat()

        # Set API credentials via env vars rather than embedding in model_args
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_API_BASE"] = model_endpoint

        from lmms_eval import evaluator

        model_args = f"model={model_name},base_url={model_endpoint}"

        print(f"Running lmms-eval evaluation")
        print(f"Model: {model_name}")
        print(f"Endpoint: {model_endpoint}")
        print(f"Task: {task_name}")
        print(f"Anthropic compat: {anthropic_compat}")

        gen_kwargs = os.environ.get("LMMS_EVAL_GEN_KWARGS",
                                    "max_new_tokens=4096,temperature=0")

        results = evaluator.simple_evaluate(
            model="openai",
            model_args=model_args,
            tasks=[task_name],
            batch_size=1,
            log_samples=False,
            limit=limit,
            gen_kwargs=gen_kwargs,
        )
    except Exception as e:
        error_msg = f"lmms-eval evaluation failed: {e}"
        print(f"ERROR: {error_msg}", file=sys.stderr)
        write_error_result(output_dir, error_msg,
                           task_name, model_name, model_endpoint)
        sys.exit(1)

    # Extract metrics from results
    metrics = {}
    overall = 0.0
    if results and "results" in results:
        task_results = results["results"]
        for tname, tmetrics in task_results.items():
            numeric = {k: v for k, v in tmetrics.items()
                       if isinstance(v, (int, float))}
            metrics[tname] = numeric

            # Select primary metric: explicit env var, or first "acc" metric,
            # or first numeric metric as fallback
            if primary_metric and primary_metric in numeric:
                overall = numeric[primary_metric]
            else:
                acc_metrics = {k: v for k, v in numeric.items() if "acc" in k}
                overall = next(iter(acc_metrics.values()),
                               next(iter(numeric.values()), 0.0))

        print(f"Results: {task_results}")
    else:
        print("WARNING: No results returned", file=sys.stderr)

    benchrouter_result = {
        "overall": overall,
        "metrics": metrics,
        "lmms_eval_config": {
            "task": task_name,
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    if not results or "results" not in results:
        benchrouter_result["error"] = "lmms-eval produced no results"

    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result written to {result_path}")
    print(f"Overall score: {overall}")


if __name__ == "__main__":
    main()
