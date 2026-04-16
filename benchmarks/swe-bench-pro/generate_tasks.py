#!/usr/bin/env python3
"""
Generate task configurations for SWE-bench Pro.

Usage:
  python generate_tasks.py                          # All instances with local images
  python generate_tasks.py --pull                   # All instances, pull missing images
  python generate_tasks.py --count 10 --repo NodeBB # 10 NodeBB instances only
  python generate_tasks.py --dataset path/to.csv    # Use local CSV instead of HF
"""

import json
import os
import sys
import shutil
import argparse
import pandas as pd
from pathlib import Path

from _utils import (
    parse_csv_field,
    make_task_name,
    get_docker_image_name,
    get_local_images,
    pull_image,
    generate_dockerfile,
)

BENCHMARK_DIR = Path(__file__).parent
RUN_SCRIPT = BENCHMARK_DIR / "default" / "run_swebench.py"


def download_dataset():
    """Download SWE-bench Pro dataset from Hugging Face."""
    csv_path = Path("/tmp/swe_bench_pro_external_hf_v2.csv")
    if csv_path.exists():
        return csv_path
    print("Downloading dataset from Hugging Face...")
    try:
        from datasets import load_dataset
        ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
        ds.to_csv(str(csv_path))
        print(f"✓ Downloaded to {csv_path}")
    except Exception as e:
        print(f"ERROR: Failed to download dataset: {e}")
        print("Please install datasets: pip install datasets")
        sys.exit(1)
    return csv_path


def generate(count=None, repo_filter=None, pull=False, dataset_csv=None):
    """Generate task directories, environments, and benchmark.yaml.

    Args:
        count: Max number of instances. None means all.
        repo_filter: Only include instances matching this repo name.
        pull: Auto-pull missing Docker images from Docker Hub.
        dataset_csv: Path to CSV. Downloads from HF if not provided.
    """
    if dataset_csv:
        dataset_csv = Path(dataset_csv)
        if not dataset_csv.exists():
            print(f"ERROR: Dataset not found: {dataset_csv}")
            sys.exit(1)
    else:
        dataset_csv = download_dataset()

    df = pd.read_csv(dataset_csv)
    if repo_filter:
        df = df[df["repo"].str.lower().str.contains(repo_filter.lower())]

    print(f"Total instances: {len(df)}")

    try:
        local_images = get_local_images()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    rows = []
    for _, row in df.iterrows():
        if count is not None and len(rows) >= count:
            break
        image = get_docker_image_name(row["instance_id"])
        if image in local_images:
            rows.append(row)
        elif pull:
            if pull_image(image):
                local_images.add(image)
                rows.append(row)

    print(f"Found {len(rows)} instances with available images"
          + (f" (filter: {repo_filter})" if repo_filter else ""))
    if not rows:
        print("ERROR: No images available. Try running with --pull.")
        sys.exit(1)

    # Clean up old generated task dirs and environments
    keep = {"default", "__pycache__", "mini-swe-agent"}
    for item in BENCHMARK_DIR.iterdir():
        if item.is_dir() and item.name not in keep:
            shutil.rmtree(item)

    tasks = []
    env_register_cmds = []

    for row in rows:
        instance_id = row["instance_id"]
        task_name = make_task_name(instance_id)
        env_name = task_name
        docker_image = get_docker_image_name(instance_id)

        task_dir = BENCHMARK_DIR / task_name
        task_dir.mkdir(exist_ok=True)

        instances_dir = task_dir / "instances"
        if instances_dir.exists():
            shutil.rmtree(instances_dir)
        instances_dir.mkdir()

        (task_dir / "task.yaml").write_text(
            f'name: "{task_name}"\n'
            f'command: "python /app/benchmark/run_swebench.py"\n'
            f'description: "{instance_id}"\n'
            f'timeout: 3600\n'
            f'docker: true\n'
        )

        shutil.copy(RUN_SCRIPT, task_dir / "run_swebench.py")

        (instances_dir / f"{instance_id}.json").write_text(json.dumps({
            "instance_id": instance_id,
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "problem_statement": row["problem_statement"],
            "requirements": row.get("requirements", ""),
            "interface": row.get("interface", ""),
            "docker_image": docker_image,
            "fail_to_pass": parse_csv_field(row["fail_to_pass"], "fail_to_pass", instance_id),
            "pass_to_pass": parse_csv_field(row["pass_to_pass"], "pass_to_pass", instance_id),
            "selected_test_files_to_run": parse_csv_field(row["selected_test_files_to_run"], "selected_test_files_to_run", instance_id),
        }, indent=2))

        env_dir = BENCHMARK_DIR / "environments" / env_name
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "Dockerfile").write_text(generate_dockerfile())

        tasks.append({"name": task_name, "environment": env_name})
        env_register_cmds.append(
            f"benchrouter env register --name {env_name} "
            f"--dockerfile benchmarks/swe-bench-pro/environments/{env_name}/Dockerfile"
        )

    # Update benchmark.yaml
    tasks_yaml = "\n".join(
        f"  - name: {t['name']}\n    environment: {t['environment']}"
        for t in tasks
    )
    (BENCHMARK_DIR / "benchmark.yaml").write_text(
        f'name: "swe-bench-pro"\n'
        f'description: "SWE-bench Pro: Evaluating LLM agents on long-horizon software engineering tasks"\n'
        f'version: "0.1.0"\n'
        f'tasks:\n{tasks_yaml}\n'
    )

    task_names = ",".join(t["name"] for t in tasks)

    print(f"\n✓ Generated {len(tasks)} tasks → {BENCHMARK_DIR}")
    print(f"✓ benchmark.yaml updated")
    print(f"\n=== Next steps ===\n")
    print(f"1. Register environments (builds Docker images):")
    for cmd in env_register_cmds[:3]:
        print(f"   {cmd}")
    if len(env_register_cmds) > 3:
        print(f"   ... and {len(env_register_cmds) - 3} more")
    print(f"\n2. Register benchmark:")
    print(f"   benchrouter benchmark register --name swe-bench-pro --path benchmarks/swe-bench-pro")
    print(f"\n3. Run:")
    print(f"   benchrouter run swe-bench-pro --model deepseek-chat"
          + (f" --tasks {task_names}" if count else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SWE-bench Pro task configurations")
    parser.add_argument("--count", type=int, default=None,
                        help="Max instances to generate (default: all)")
    parser.add_argument("--repo", type=str, default=None,
                        help="Filter by repo name (e.g. NodeBB)")
    parser.add_argument("--pull", action="store_true",
                        help="Auto-pull missing Docker images")
    parser.add_argument("--dataset", default=None,
                        help="Path to CSV (optional, downloads from HF if not provided)")
    args = parser.parse_args()
    generate(count=args.count, repo_filter=args.repo, pull=args.pull, dataset_csv=args.dataset)
