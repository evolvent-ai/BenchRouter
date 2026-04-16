#!/usr/bin/env python3
"""
BenchRouter <-> MCPMark bridge script for Playwright WebArena benchmark.

Runs MCPMark playwright_webarena evaluation inside a DinD container.
The MCPMark PlaywrightStateManager handles its own docker run/stop for
the shopping_admin container via the inner Docker daemon.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


EXPECTED_IMAGE = "shopping_admin_final_0719"


def _ensure_shopping_admin_image():
    """Tag the loaded shopping image to the name MCPMark expects."""
    # Check if expected image already exists
    r = subprocess.run(
        ["docker", "image", "inspect", EXPECTED_IMAGE],
        capture_output=True, timeout=10,
    )
    if r.returncode == 0:
        return

    # Find the loaded image (the tar may use a different name)
    r = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, timeout=10,
    )
    for line in r.stdout.strip().splitlines():
        if "shopping" in line.lower():
            print(f"Tagging {line} -> {EXPECTED_IMAGE}")
            subprocess.run(
                ["docker", "tag", line, EXPECTED_IMAGE],
                check=True, timeout=10,
            )
            return

    print(f"WARNING: No shopping image found to tag as {EXPECTED_IMAGE}",
          file=sys.stderr)


def main():
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

    if not model_endpoint:
        print("ERROR: BENCHROUTER_MODEL_ENDPOINT is not set", file=sys.stderr)
        sys.exit(1)

    os.environ["OPENAI_API_BASE"] = model_endpoint

    # Ensure the shopping_admin image is tagged with the name MCPMark expects.
    # The cached tar may contain a differently-named image (e.g. shopping_final_0712).
    _ensure_shopping_admin_image()

    # openai/ prefix for LiteLLM to use OpenAI-compatible protocol
    if not model_name.startswith("openai/"):
        model_name = f"openai/{model_name}"

    mcpmark_output_dir = "/tmp/mcpmark_results"
    os.makedirs(mcpmark_output_dir, exist_ok=True)

    cmd = [
        sys.executable, "-m", "pipeline",
        "--mcp", "playwright_webarena",
        "--tasks", "shopping_admin/ny_expansion_analysis",
        "--models", model_name,
        "--exp-name", "benchrouter",
        "--output-dir", mcpmark_output_dir,
        "--k", "1",
        "--timeout", "600",
    ]

    print(f"Running MCPMark pipeline: {' '.join(cmd)}")
    print(f"Model: {model_name}")
    print(f"Endpoint: {model_endpoint}")

    result = subprocess.run(cmd, cwd="/app", timeout=7200)

    if result.returncode != 0:
        print(f"WARNING: MCPMark pipeline exited with code {result.returncode}",
              file=sys.stderr)

    # Collect MCPMark results
    mcpmark_base = Path(mcpmark_output_dir) / "benchrouter"
    task_results = []

    for meta_path in mcpmark_base.rglob("meta.json"):
        try:
            with open(meta_path) as f:
                task_meta = json.load(f)
            if "execution_result" not in task_meta:
                continue
            task_results.append({
                "task_name": task_meta.get("task_name", meta_path.parent.name),
                "success": task_meta.get("execution_result", {}).get("success", False),
                "error_message": task_meta.get("execution_result", {}).get("error_message"),
                "verification_output": task_meta.get("execution_result", {}).get("verification_output"),
                "token_usage": task_meta.get("token_usage", {}),
                "turn_count": task_meta.get("turn_count"),
                "agent_execution_time": task_meta.get("agent_execution_time", 0),
                "task_execution_time": task_meta.get("task_execution_time", 0),
            })
        except Exception as e:
            print(f"WARNING: Failed to read {meta_path}: {e}", file=sys.stderr)

    total = len(task_results)
    passed = sum(1 for t in task_results if t["success"])

    benchrouter_result = {
        "overall": round(passed / total, 4) if total > 0 else 0.0,
        "total_tasks": total,
        "successful_tasks": passed,
        "failed_tasks": total - passed,
        "per_task": task_results,
        "mcpmark_config": {
            "mcp_service": "playwright_webarena",
            "task_suite": "standard",
            "tasks": "shopping_admin/ny_expansion_analysis",
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    if total == 0:
        benchrouter_result["error"] = "MCPMark pipeline produced no results"

    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result written to {result_path}")
    print(f"Overall score: {benchrouter_result['overall']}")
    print(f"Tasks: {passed}/{total} passed")


if __name__ == "__main__":
    main()
