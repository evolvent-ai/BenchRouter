"""Shared utilities for SWE-bench Pro task generation scripts."""

import ast
import re
import subprocess
from pathlib import Path


def parse_csv_field(value, field_name, instance_id):
    """Safely parse a CSV field that may be a string repr of a list."""
    if not isinstance(value, str):
        if isinstance(value, list):
            return value
        return []  # NaN or other non-string/non-list → empty list
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError) as e:
        raise ValueError(f"Failed to parse '{field_name}' for {instance_id}: {e}")


def make_task_name(instance_id):
    """Convert instance_id to a short task name safe for BenchRouter."""
    name = instance_id.replace("instance_", "")
    dunder_idx = name.index("__")
    org = name[:dunder_idx]
    after_dunder = name[dunder_idx + 2:]
    m = re.search(r'-([0-9a-f]{40})', after_dunder)
    if m:
        repo = after_dunder[:m.start()]
        commit_short = m.group(1)[:8]
    else:
        repo = after_dunder.split("-")[0]
        commit_short = after_dunder[len(repo):].lstrip("-")[:8]
    return f"{org}-{repo}-{commit_short}".lower()


def get_docker_image_name(instance_id):
    """Derive official Docker image name from instance_id (matches official image_uri.py)."""
    # Special case: one element-web instance keeps full repo name
    if instance_id == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        return "jefzda/sweap-images:element-hq.element-web-element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan"
    name = instance_id.replace("instance_", "")
    dunder_idx = name.index("__")
    org = name[:dunder_idx]
    after_dunder = name[dunder_idx + 2:]
    repo = after_dunder.split("-")[0]
    commit_and_rest = after_dunder[len(repo):]
    repo_part = f"{org}__{repo}"
    repo_prefix = f"{org}.{repo}".lower()
    if commit_and_rest.endswith("-vnan"):
        commit_and_rest = commit_and_rest[:-5]
    tag = f"{repo_prefix}-{repo_part}{commit_and_rest}"
    return f"jefzda/sweap-images:{tag[:128]}"


def get_local_images():
    """Get set of locally available Docker images."""
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to list Docker images. Is Docker running?")
    return set(result.stdout.strip().split("\n"))


def pull_image(image):
    """Pull Docker image from Docker Hub. Returns True if successful."""
    print(f"Pulling {image}...")
    result = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✓ Pulled {image}")
        return True
    print(f"✗ Failed to pull {image}: {result.stderr}")
    return False


def generate_dockerfile():
    """Generate Dockerfile for DinD outer container (python:3.12-slim + Docker CE).

    The outer container calls the LLM to generate a patch, then uses Docker to
    run the official swebench image for testing. Pass HTTPS_PROXY / HTTP_PROXY
    as build args if your environment requires a proxy.
    """
    return (
        "FROM python:3.12-slim-bookworm\n\n"
        "# Uncomment and set your proxy if needed, e.g.:\n"
        "# ENV HTTPS_PROXY=http://192.168.0.188:7890\n"
        "# ENV HTTP_PROXY=http://192.168.0.188:7890\n\n"
        "RUN apt-get update && apt-get install -y \\\n"
        "    git ca-certificates docker.io \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n\n"
        "# Clone official SWE-bench Pro repo\n"
        "RUN git clone --depth=1 https://github.com/scaleapi/SWE-bench_Pro-os /app/swe-bench-pro\n\n"
        "# Patch Docker client timeout (vfs storage driver is slow, default 60s is not enough)\n"
        "RUN sed -i 's/docker.from_env()/docker.from_env(timeout=600)/g' /app/swe-bench-pro/swe_bench_pro_eval.py\n\n"
        "# Download dataset from Hugging Face\n"
        "RUN pip install --no-cache-dir datasets && \\\n"
        "    python3 -c \"from datasets import load_dataset; \\\n"
        "ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test'); \\\n"
        "ds.to_csv('/app/swe-bench-pro/external_hf_v2.csv')\" && \\\n"
        "    test -s /app/swe-bench-pro/external_hf_v2.csv\n\n"
        "# Install official eval dependencies\n"
        "RUN pip install --no-cache-dir -r /app/swe-bench-pro/requirements.txt\n\n"
        "# Install LLM dependencies\n"
        "RUN pip install --no-cache-dir litellm openai anthropic\n"
    )
