#!/usr/bin/env python3
"""
SWE-bench Pro bridge script for BenchRouter.
Thin router: calls LLM to generate patch, then invokes official swe_bench_pro_eval.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from litellm import completion
except ImportError:
    print("ERROR: litellm not installed", file=sys.stderr)
    sys.exit(1)

def log(msg):
    print(f"[SWE-bench Pro] {msg}", flush=True)

def load_config():
    """Load instance config from /app/benchmark/instances/<instance_id>.json"""
    config_dir = Path("/app/benchmark/instances")
    configs = list(config_dir.glob("*.json"))
    if not configs:
        raise FileNotFoundError(f"No instance config found in {config_dir}")
    if len(configs) > 1:
        raise ValueError(f"Multiple configs found: {configs}")
    with open(configs[0]) as f:
        return json.load(f)

def call_llm(prompt, model_name, api_base, api_key):
    """Call LLM to generate unified diff patch."""
    try:
        max_tokens = int(os.environ.get("BENCHROUTER_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    response = completion(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        api_base=api_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return response.choices[0].message.content

def extract_patch(llm_output):
    """Extract unified diff from LLM output."""
    lines = llm_output.strip().split("\n")
    patch_lines = []
    in_diff = False
    for line in lines:
        if line.startswith("diff --git") or line.startswith("--- ") or line.startswith("+++ "):
            in_diff = True
        if in_diff:
            patch_lines.append(line)
    return "\n".join(patch_lines) if patch_lines else llm_output

def main():
    output_dir = Path("/app/results")
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    instance_id = config["instance_id"]
    log(f"Instance: {instance_id}")

    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "gpt-4")
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    api_key = os.environ.get("OPENAI_API_KEY", "")

    if not model_endpoint or not api_key:
        log("ERROR: BENCHROUTER_MODEL_ENDPOINT and OPENAI_API_KEY required")
        result_data = {"patch_generated": False, "patch_applied": False, "tests_passed": False, "overall": 0.0, "error": "Missing BENCHROUTER_MODEL_ENDPOINT or OPENAI_API_KEY"}
        (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
        return 1

    log(f"Model: {model_name}")

    # Build enhanced prompt with requirements and interface if available
    prompt_parts = [
        "You are a software engineer. Solve the following GitHub issue.",
        "Output ONLY a unified diff patch (git diff format) that can be applied with `git apply`.",
        "",
        f"Repository: {config.get('repo', 'unknown')}",
        "",
        "Issue:",
        config['problem_statement'],
    ]

    if config.get('requirements'):
        prompt_parts.extend(["", "Requirements:", config['requirements']])

    if config.get('interface'):
        prompt_parts.extend(["", "Interface Documentation:", config['interface']])

    prompt_parts.extend([
        "",
        "Output the patch in unified diff format. Start with `diff --git` or `--- a/` lines.",
        "Do NOT include any explanation, only the patch."
    ])

    prompt = "\n".join(prompt_parts)

    log("Calling LLM to generate patch...")
    llm_output = call_llm(prompt, model_name, model_endpoint, api_key)
    patch = extract_patch(llm_output)

    if not patch:
        log("ERROR: No patch generated")
        result = {"patch_generated": False, "patch_applied": False, "tests_passed": False, "overall": 0.0}
        (output_dir / "result.json").write_text(json.dumps(result, indent=2))
        return 1

    log(f"Generated patch ({len(patch)} bytes)")

    patches_json = Path("/tmp/patches.json")
    patches_data = [{
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "patch": patch
    }]
    patches_json.write_text(json.dumps(patches_data, indent=2))
    log(f"Saved patch to {patches_json}")

    # Call official evaluation script
    eval_script = "/app/swe-bench-pro/swe_bench_pro_eval.py"
    dataset_csv = "/app/swe-bench-pro/external_hf_v2.csv"
    scripts_dir = "/app/swe-bench-pro/run_scripts"
    eval_output = Path("/tmp/eval_output")
    eval_output.mkdir(parents=True, exist_ok=True)

    # Download CSV if missing (fallback for failed build-time download)
    if not Path(dataset_csv).exists():
        log("CSV not found, downloading from Hugging Face...")
        try:
            from datasets import load_dataset
            ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
            ds.to_csv(dataset_csv)
            log(f"Downloaded CSV to {dataset_csv}")
        except Exception as e:
            log(f"ERROR: Failed to download CSV: {e}")
            result_data = {"patch_generated": True, "patch_applied": False, "tests_passed": False, "overall": 0.0, "error": str(e)}
            (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
            return 1

    log("Calling official swe_bench_pro_eval.py...")
    cmd = [
        sys.executable, eval_script,
        "--raw_sample_path", dataset_csv,
        "--patch_path", str(patches_json),
        "--output_dir", str(eval_output),
        "--dockerhub_username", "jefzda",
        "--scripts_dir", scripts_dir,
        "--use_local_docker",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800,
            cwd="/app/swe-bench-pro",  # Run in swe-bench-pro directory for relative paths
        )
        log(f"Eval script exit code: {result.returncode}")
        if result.stdout:
            log(f"Eval stdout:\n{result.stdout[:500]}")
        if result.stderr:
            log(f"Eval stderr:\n{result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        log("ERROR: Eval script timed out")
        result_data = {"patch_generated": True, "patch_applied": False, "tests_passed": False, "overall": 0.0}
        (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
        return 1
    except Exception as e:
        log(f"ERROR: Eval script failed: {e}")
        result_data = {"patch_generated": True, "patch_applied": False, "tests_passed": False, "overall": 0.0}
        (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
        return 1

    # Read eval results
    eval_results_file = eval_output / "eval_results.json"
    if not eval_results_file.exists():
        log(f"ERROR: {eval_results_file} not found")
        result_data = {"patch_generated": True, "patch_applied": False, "tests_passed": False, "overall": 0.0}
        (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
        return 1

    with open(eval_results_file) as f:
        eval_results = json.load(f)

    # Convert official results to BenchRouter format
    # Official format: {instance_id: bool} — true means all tests passed
    # patch_applied is inferred from whether stdout.log has content (tests ran)
    instance_result = eval_results.get(instance_id)

    if isinstance(instance_result, bool):
        tests_passed = instance_result
    elif isinstance(instance_result, dict):
        tests_passed = instance_result.get("resolved", False)
    else:
        log(f"ERROR: Unexpected result format: {instance_result}")
        tests_passed = False

    # Check if patch was applied by looking for test output
    stdout_log = eval_output / instance_id / "workspace" / "stdout.log"
    patch_applied = stdout_log.exists() and stdout_log.stat().st_size > 0

    log(f"Patch applied: {patch_applied}, Tests passed: {tests_passed}")

    result_data = {
        "patch_generated": True,
        "patch_applied": patch_applied,
        "tests_passed": tests_passed,
        "overall": 1.0 if tests_passed else 0.0,
        "instance_id": instance_id,
    }

    (output_dir / "result.json").write_text(json.dumps(result_data, indent=2))
    log(f"Wrote result to {output_dir / 'result.json'}")

    return 0

if __name__ == "__main__":
    sys.exit(main())

