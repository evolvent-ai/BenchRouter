#!/usr/bin/env python3
"""
BenchRouter <-> WebArena bridge script for Shopping Admin benchmark.

Runs inside a DinD container:
1. Start shopping_admin Docker container
2. Configure Magento base URLs
3. Pre-run auto_login to create browser cookies
4. Generate task configs with cookie paths baked in
5. Run WebArena evaluation per task
6. Collect results → BenchRouter result.json
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

WEBARENA_DIR = "/app/webarena"
AUTH_DIR = os.path.join(WEBARENA_DIR, ".auth")

SERVICE_DEFAULTS = {
    "SHOPPING_ADMIN": "http://localhost:7780/admin",
    "SHOPPING": "http://localhost:7770",
    "REDDIT": "http://localhost:9999",
    "GITLAB": "http://localhost:8023",
    "WIKIPEDIA": "http://localhost:8888",
    "MAP": "http://localhost:3000",
    "HOMEPAGE": "http://localhost:4399",
}

SHOPPING_ADMIN_IMAGE = "shopping_admin_final_0719"
RESULT_PASS_RE = re.compile(r"\[Result\] \(PASS\)")
RESULT_FAIL_RE = re.compile(r"\[Result\] \(FAIL\)")

TIKTOKEN_PATCH = """\
import tiktoken as _tiktoken
_orig = _tiktoken.encoding_for_model
def _patched(model_name):
    try:
        return _orig(model_name)
    except KeyError:
        return _tiktoken.get_encoding("cl100k_base")
_tiktoken.encoding_for_model = _patched
"""


def _ensure_shopping_admin_image():
    """Tag the loaded shopping image to the name this script expects."""
    r = subprocess.run(
        ["docker", "image", "inspect", SHOPPING_ADMIN_IMAGE],
        capture_output=True, timeout=10,
    )
    if r.returncode == 0:
        return

    r = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, timeout=10,
    )
    for line in r.stdout.strip().splitlines():
        if "shopping" in line.lower():
            print(f"Tagging {line} -> {SHOPPING_ADMIN_IMAGE}")
            subprocess.run(
                ["docker", "tag", line, SHOPPING_ADMIN_IMAGE],
                check=True, timeout=10,
            )
            return

    print(f"WARNING: No shopping image found to tag as {SHOPPING_ADMIN_IMAGE}",
          file=sys.stderr)


def _get_service_url(name):
    return os.environ.get(name, SERVICE_DEFAULTS[name])


def _service_env():
    """Build env dict with all service URLs."""
    return {n: _get_service_url(n) for n in SERVICE_DEFAULTS}


def start_shopping_admin():
    """Start shopping_admin container inside DinD and wait for ready."""
    url = _get_service_url("SHOPPING_ADMIN")
    port = urlparse(url).port or 7780

    subprocess.run(["docker", "rm", "-f", "shopping_admin"],
                   capture_output=True, timeout=10)

    subprocess.run([
        "docker", "run", "-d", "--name", "shopping_admin",
        "-p", f"{port}:80", SHOPPING_ADMIN_IMAGE,
    ], check=True)

    print(f"Waiting for Shopping Admin at {url}...")
    for i in range(90):
        try:
            r = subprocess.run(
                ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "3", url],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip() in ("200", "302"):
                print(f"Shopping Admin ready after {i * 2}s")
                return
        except (subprocess.SubprocessError, OSError):
            pass
        time.sleep(2)

    raise RuntimeError("Shopping Admin failed to start within 3 minutes")


def configure_magento():
    """Update Magento base URLs and flush cache."""
    url = _get_service_url("SHOPPING_ADMIN")
    base_url = url.split("/admin")[0].rstrip("/") + "/"

    subprocess.run([
        "docker", "exec", "shopping_admin",
        "mysql", "-u", "magentouser", "-pMyPassword", "magentodb", "-e",
        f"UPDATE core_config_data SET value='{base_url}' "
        f"WHERE path IN ('web/secure/base_url', 'web/unsecure/base_url');",
    ], check=True, timeout=30)

    subprocess.run([
        "docker", "exec", "shopping_admin",
        "/var/www/magento2/bin/magento", "cache:flush",
    ], check=True, timeout=60)

    print("Magento configured and cache flushed")


def run_auto_login():
    """Pre-create browser cookies via Playwright so run.py doesn't need to."""
    os.makedirs(AUTH_DIR, exist_ok=True)

    env = {**os.environ, **_service_env(), "PYTHONPATH": WEBARENA_DIR}
    result = subprocess.run(
        [sys.executable, os.path.join(WEBARENA_DIR, "browser_env", "auto_login.py"),
         "--auth_folder", AUTH_DIR, "--site_list", "shopping_admin"],
        cwd=WEBARENA_DIR, env=env,
        capture_output=True, text=True, timeout=120,
    )

    cookie_file = os.path.join(AUTH_DIR, "shopping_admin_state.json")
    if result.returncode != 0 or not os.path.exists(cookie_file):
        print(f"auto_login stderr: {result.stderr[:300]}", file=sys.stderr)
        raise RuntimeError(
            f"auto_login failed (exit={result.returncode}), cookie not created"
        )

    print(f"Auto-login OK: {cookie_file}")
    return cookie_file


def generate_task_configs(task_id_set=None, cookie_path=None):
    """Replace URL placeholders and bake cookie path into per-task JSON files.

    WebArena's run.py re-runs auto_login per task if storage_state is a relative
    path. By writing the absolute cookie path here, run.py will find the file
    and skip re-login.
    """
    raw_path = os.path.join(WEBARENA_DIR, "config_files", "test.raw.json")
    with open(raw_path) as f:
        content = f.read()

    for name in SERVICE_DEFAULTS:
        placeholder = f"__{name}__"
        content = content.replace(placeholder, _get_service_url(name))

    configs = json.loads(content)
    config_dir = os.path.join(WEBARENA_DIR, "config_files")
    for config in configs:
        if task_id_set is None or config["task_id"] in task_id_set:
            # Bake the pre-created cookie path so run.py skips auto_login
            if cookie_path and config.get("storage_state"):
                config["storage_state"] = cookie_path
            task_path = os.path.join(config_dir, f"{config['task_id']}.json")
            with open(task_path, "w") as f:
                json.dump(config, f, indent=2)

    return {c["task_id"]: c for c in configs}


def load_task_ids():
    """Load pre-computed shopping_admin task IDs."""
    benchmark_dir = os.environ.get("BENCHROUTER_BENCHMARK_DIR", "/app/benchmark")
    ids_path = os.path.join(benchmark_dir, "shopping_admin_task_ids.json")
    with open(ids_path) as f:
        return json.load(f)


def _ensure_tiktoken_patch():
    """Install tiktoken patch via sitecustomize so WebArena code stays unmodified."""
    site_dir = "/tmp/_site_patch"
    os.makedirs(site_dir, exist_ok=True)
    patch_path = os.path.join(site_dir, "sitecustomize.py")
    if not os.path.exists(patch_path):
        with open(patch_path, "w") as f:
            f.write(TIKTOKEN_PATCH)
    return site_dir


def run_single_task(task_id, model_name, results_dir, env):
    """Run a single WebArena task. Returns (score, error_or_None)."""
    task_result_dir = os.path.join(results_dir, f"task_{task_id}")
    if os.path.exists(task_result_dir):
        shutil.rmtree(task_result_dir)
    os.makedirs(task_result_dir)

    # PYTHONPATH must include both tiktoken patch dir and WEBARENA_DIR so that
    # run.py's internal auto_login subprocess (which uses bare "python") can
    # import browser_env and also get the tiktoken patch
    site_dir = _ensure_tiktoken_patch()
    env = {**env, "PYTHONPATH": f"{site_dir}:{WEBARENA_DIR}"}

    cmd = [
        sys.executable, os.path.join(WEBARENA_DIR, "run.py"),
        "--test_start_idx", str(task_id),
        "--test_end_idx", str(task_id + 1),
        "--model", model_name,
        "--provider", "openai",
        "--mode", "chat",
        "--instruction_path",
        os.path.join(WEBARENA_DIR, "agent/prompts/jsons/p_cot_id_actree_2s.json"),
        "--result_dir", task_result_dir,
        "--observation_type", "accessibility_tree",
        "--action_set_tag", "id_accessibility_tree",
        "--current_viewport_only",
        "--max_steps", "15",
        "--top_p", "0.95",
        "--temperature", "1.0",
    ]

    try:
        result = subprocess.run(
            cmd, cwd=WEBARENA_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=300,
        )
        output = result.stdout
        # Print WebArena output so it appears in container logs
        if output.strip():
            print(output)

        if RESULT_PASS_RE.search(output):
            return 1.0, None
        if RESULT_FAIL_RE.search(output):
            return 0.0, None

        error_file = os.path.join(task_result_dir, "error.txt")
        if os.path.exists(error_file):
            with open(error_file) as f:
                return 0.0, f"WebArena error: {f.read().strip()[-300:]}"

        tail = output.strip().split("\n")[-5:]
        return 0.0, f"exit={result.returncode}: {' | '.join(tail)}"

    except subprocess.TimeoutExpired:
        return 0.0, "Timeout after 300s"
    except Exception as e:
        return 0.0, str(e)


def main():
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

    if not model_endpoint:
        print("ERROR: BENCHROUTER_MODEL_ENDPOINT is not set", file=sys.stderr)
        sys.exit(1)

    # Phase 1: Start shopping_admin + configure Magento
    if os.environ.get("BENCHROUTER_DOCKER_ENABLED") == "true":
        _ensure_shopping_admin_image()
        start_shopping_admin()
        configure_magento()

    # Phase 2: Pre-create browser cookies
    cookie_path = run_auto_login()

    # Phase 3: Generate configs with cookie path baked in
    task_ids = load_task_ids()
    task_limit = int(os.environ.get("WEBARENA_TASK_LIMIT", "0"))
    if task_limit > 0:
        task_ids = task_ids[:task_limit]
    task_id_set = set(task_ids)

    configs_by_id = generate_task_configs(task_id_set, cookie_path)

    missing = task_id_set - configs_by_id.keys()
    if missing:
        print(f"WARNING: {len(missing)} task IDs not in configs", file=sys.stderr)
        task_ids = [tid for tid in task_ids if tid in configs_by_id]

    print(f"\nWebArena evaluation: {len(task_ids)} tasks")
    print(f"Model: {model_name}")
    print(f"Endpoint: {model_endpoint}")

    # Phase 4: Setup env
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = api_key
    env["OPENAI_API_BASE"] = model_endpoint  # openai v0.27 reads this natively
    for name in SERVICE_DEFAULTS:
        env.setdefault(name, SERVICE_DEFAULTS[name])

    results_dir = "/tmp/webarena_results"
    os.makedirs(results_dir, exist_ok=True)

    # Phase 5: Run tasks
    task_results = []
    for i, task_id in enumerate(task_ids):
        config = configs_by_id.get(task_id, {})
        intent = config.get("intent", "")
        print(f"\n[{i + 1}/{len(task_ids)}] Task {task_id}: {intent[:80]}")

        score, error = run_single_task(task_id, model_name, results_dir, env)
        print(f"  Result: {'PASS' if score == 1.0 else 'FAIL'}"
              + (f" ({error})" if error else ""))

        task_results.append({
            "task_id": task_id,
            "score": score,
            "success": score == 1.0,
            "intent": intent,
            **({"error": error} if error else {}),
        })

    # Phase 6: Write result.json
    total = len(task_results)
    passed = sum(1 for t in task_results if t["success"])

    benchrouter_result = {
        "overall": round(passed / total, 4) if total > 0 else 0.0,
        "total_tasks": total,
        "successful_tasks": passed,
        "failed_tasks": total - passed,
        "per_task": task_results,
        "webarena_config": {
            "benchmark": "webarena",
            "subset": "shopping_admin",
            "model_name": model_name,
            "model_endpoint": model_endpoint,
        },
    }

    if total == 0:
        benchrouter_result["error"] = "No tasks were executed"

    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, "result.json")
    with open(result_path, "w") as f:
        json.dump(benchrouter_result, f, indent=2)

    print(f"\nBenchRouter result: {result_path}")
    print(f"Score: {benchrouter_result['overall']} ({passed}/{total} passed)")


if __name__ == "__main__":
    main()
