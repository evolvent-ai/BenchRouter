#!/usr/bin/env python3
"""
BenchRouter <-> MCPMark bridge script.

Translates BenchRouter environment variables into MCPMark pipeline arguments,
runs the MCPMark filesystem evaluation, and writes an BenchRouter-compatible
result.json.

Runs inside the evalsysorg/mcpmark:latest Docker container where:
  - MCPMark code lives at /app/ (PYTHONPATH=/app)
  - This script is mounted at /app/benchmark/run_mcpmark.py (read-only)
  - Results must be written to BENCHROUTER_OUTPUT_DIR (default /app/results)
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    # 1. Read BenchRouter environment variables
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

    if not model_endpoint:
        print("ERROR: BENCHROUTER_MODEL_ENDPOINT is not set", file=sys.stderr)
        sys.exit(1)

    # 2. Map BenchRouter env vars -> MCPMark env vars
    # [非官方 API 适配] MCPMark 不支持自定义 endpoint 参数，这里通过 LiteLLM
    # 原生环境变量 OPENAI_API_BASE 注入，LiteLLM 会自动读取作为 base URL。
    # 如果用的是 OpenAI 官方 API，这行不需要。
    os.environ["OPENAI_API_BASE"] = model_endpoint
    # OPENAI_API_KEY is already set by BenchRouter scheduler

    # openai/ prefix for LiteLLM to use OpenAI-compatible protocol
    if not model_name.startswith("openai/"):
        model_name = f"openai/{model_name}"

    # 3. MCPMark output to temp dir (avoid conflict with BenchRouter's /app/results mount)
    mcpmark_output_dir = "/tmp/mcpmark_results"
    os.makedirs(mcpmark_output_dir, exist_ok=True)

    # 4. Run MCPMark pipeline — 1 task to verify flow
    cmd = [
        sys.executable, "-m", "pipeline",
        "--mcp", "filesystem",
        "--tasks", "file_context/uppercase",
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

    # 5. Collect MCPMark results
    mcpmark_base = Path(mcpmark_output_dir) / "benchrouter"
    task_results = []

    # Find per-task meta.json files
    for meta_path in mcpmark_base.rglob("meta.json"):
        try:
            with open(meta_path) as f:
                task_meta = json.load(f)
            # Skip summary-level files (they don't have execution_result)
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

    # 6. Build BenchRouter result.json
    total = len(task_results)
    passed = sum(1 for t in task_results if t["success"])

    benchrouter_result = {
        "overall": round(passed / total, 4) if total > 0 else 0.0,
        "total_tasks": total,
        "successful_tasks": passed,
        "failed_tasks": total - passed,
        "per_task": task_results,
        "mcpmark_config": {
            "mcp_service": "filesystem",
            "task_suite": "easy",
            "tasks": "file_context/uppercase",
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    if total == 0:
        benchrouter_result["error"] = "MCPMark pipeline produced no results"

    # 7. Write result.json
    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result written to {result_path}")
    print(f"Overall score: {benchrouter_result['overall']}")
    print(f"Tasks: {passed}/{total} passed")


if __name__ == "__main__":
    main()
