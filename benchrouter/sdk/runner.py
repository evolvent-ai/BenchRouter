"""BenchRouter Runner — reads task.yaml, executes task command, standardizes results."""

import os
import json
import subprocess
import logging

import yaml

logger = logging.getLogger("benchrouter.runner")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    benchmark_dir = os.environ.get("BENCHROUTER_BENCHMARK_DIR", "/app/benchmark")
    output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

    # 1. Read manifest
    manifest_path = os.path.join(benchmark_dir, "task.yaml")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"task.yaml not found in {benchmark_dir}")

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    name = manifest["name"]
    command = manifest["command"]
    result_file = manifest.get("result_file", "result.json")
    result_mapping = manifest.get("result_mapping", {})

    logger.info(f"Task: {name}")
    logger.info(f"Command: {command}")

    # 2. Execute task command
    os.makedirs(output_dir, exist_ok=True)
    result = subprocess.run(command, shell=True, cwd=benchmark_dir)
    if result.returncode != 0:
        raise RuntimeError(f"Task command failed with exit code {result.returncode}")

    # 3. Read and standardize results
    raw_path = os.path.join(output_dir, result_file)
    if not os.path.exists(raw_path):
        raise FileNotFoundError(
            f"Task did not produce {result_file} in {output_dir}"
        )

    with open(raw_path) as f:
        raw = json.load(f)

    overall_key = result_mapping.get("overall", "overall")
    if overall_key not in raw:
        raise KeyError(
            f"Result file missing '{overall_key}' field. "
            f"Available keys: {list(raw.keys())}"
        )

    standardized = {
        "benchmark_name": name,
        "model_name": os.environ.get("BENCHROUTER_MODEL_NAME", "default"),
        "overall": raw[overall_key],
        "raw_metrics": raw,
    }

    # Write standardized result (overwrite if result_file was already result.json)
    output_path = os.path.join(output_dir, "result.json")
    with open(output_path, "w") as f:
        json.dump(standardized, f, indent=2)

    logger.info(f"Done. Overall: {standardized['overall']}")


if __name__ == "__main__":
    main()
