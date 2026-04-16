#!/usr/bin/env python3

import glob
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Optional


def getenv_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got: {value}") from exc


def load_json(path: Path) -> dict:
    with open(path) as file:
        return json.load(file)


def read_task_list(path: Path) -> list[str]:
    tasks = []
    with open(path) as file:
        for line in file:
            task_name = line.strip()
            if task_name and not task_name.startswith("#"):
                tasks.append(task_name)
    return tasks


def latest_file(pattern: str) -> Optional[Path]:
    matches = [Path(path) for path in glob.glob(pattern)]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def ensure_runtime_configs(repo_dir: Path) -> None:
    configs_dir = repo_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    global_configs_path = configs_dir / "global_configs.py"
    global_configs_path.write_text(
        "from addict import Dict\n"
        "global_configs = Dict(\n"
        "    podman_or_docker=\"docker\",\n"
        "    notion_preprocess_with_playwright=False,\n"
        ")\n",
        encoding="utf-8",
    )

    token_key_session_path = configs_dir / "token_key_session.py"
    if not token_key_session_path.exists():
        token_key_session_path.write_text(
            "from addict import Dict\nall_token_key_session = Dict()\n",
            encoding="utf-8",
        )


def run_command(command: list[str], cwd: Path) -> None:
    print("Running:", " ".join(shlex.quote(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def build_parallel_command(dump_dir: Path, image_name: str) -> list[str]:
    command = [
        "uv",
        "run",
        "python",
        "run_parallel.py",
        "--tasks_folder",
        os.environ.get("TOOLATHLON_TASKS_FOLDER", "finalpool"),
        "--model_short_name",
        os.environ["BENCHROUTER_MODEL_NAME"].split("/")[-1],
        "--provider",
        os.environ.get("TOOLATHLON_PROVIDER", "unified"),
        "--maxstep",
        str(getenv_int("TOOLATHLON_MAXSTEP", 100)),
        "--workers",
        str(getenv_int("TOOLATHLON_WORKERS", 1)),
        "--timeout",
        str(getenv_int("TOOLATHLON_TIMEOUT", 1800)),
        "--dump_path",
        str(dump_dir),
        "--task_list",
        os.environ["TOOLATHLON_TASK_LIST_FILE"],
        "--eval_config",
        os.environ.get("TOOLATHLON_EVAL_CONFIG", "scripts/formal_run_v0.json"),
        "--image_name",
        image_name,
        "--runner",
        os.environ.get("TOOLATHLON_RUNNER", "containerized"),
        "--runmode",
        os.environ.get("TOOLATHLON_RUNMODE", "quickstart"),
    ]

    agent_framework = os.environ.get("TOOLATHLON_AGENT_FRAMEWORK")
    if agent_framework:
        command.extend(["--agent_framework", agent_framework])

    tag = os.environ.get("TOOLATHLON_TAG")
    if tag:
        command.extend(["--tag", tag])

    return command


def build_single_command(dump_dir: Path, image_name: str) -> list[str]:
    return [
        "bash",
        "scripts/run_single_containerized.sh",
        os.environ.get("TOOLATHLON_TASK_DIR", "finalpool/find-alita-paper"),
        os.environ.get("TOOLATHLON_RUNMODE", "quickstart"),
        str(dump_dir),
        os.environ["BENCHROUTER_MODEL_NAME"],
        os.environ.get("TOOLATHLON_PROVIDER", "unified"),
        str(getenv_int("TOOLATHLON_MAXSTEP", 100)),
        os.environ.get("TOOLATHLON_EVAL_CONFIG", "scripts/formal_run_v0.json"),
        image_name,
    ]


def load_parallel_result(dump_dir: Path) -> dict:
    # run_parallel.py writes a flat file:
    # {dump_path}/execution_report_{tasks_folder}_{model_short_name}_{tag}.json
    execution_report = latest_file(str(dump_dir / "execution_report_*.json"))
    if not execution_report:
        raise FileNotFoundError(
            f"Toolathlon did not produce an execution_report in {dump_dir}"
        )

    execution_report_data = load_json(execution_report)
    summary = execution_report_data.get("summary", {})

    total_tasks = int(summary.get("total_tasks") or 0)
    passed = int(summary.get("passed") or 0)
    failed = int(summary.get("failed") or 0)
    not_executed = int(summary.get("not_executed") or 0)

    overall = summary.get("pass_rate_all_percent")
    if overall is not None:
        overall = float(overall) / 100.0
    elif total_tasks > 0:
        overall = passed / total_tasks
    else:
        overall = 0.0

    return {
        "overall": float(overall),
        "total_tasks": total_tasks,
        "passed": passed,
        "failed": failed,
        "not_executed": not_executed,
        "execution_report": execution_report_data,
    }


def load_single_result(dump_dir: Path) -> dict:
    task_dir = os.environ.get("TOOLATHLON_TASK_DIR", "finalpool/find-alita-paper")
    task_name = task_dir.split("/")[-1]
    tasks_folder = os.environ.get("TOOLATHLON_TASKS_FOLDER", "finalpool")
    task_output_dir = dump_dir / tasks_folder / task_name
    eval_res_path = task_output_dir / "eval_res.json"
    if not eval_res_path.exists():
        raise FileNotFoundError(f"Toolathlon did not produce eval_res.json at {eval_res_path}")

    eval_res = load_json(eval_res_path)
    status_path = task_output_dir / "status.json"
    status_data = load_json(status_path) if status_path.exists() else None

    return {
        "overall": 1.0 if eval_res.get("pass") else 0.0,
        "total_tasks": 1,
        "passed": 1 if eval_res.get("pass") else 0,
        "failed": 0 if eval_res.get("pass") else 1,
        "not_executed": 0,
        "eval_res": eval_res,
        "status": status_data,
    }


def main() -> None:
    repo_dir = Path(os.environ.get("TOOLATHLON_REPO_DIR", "/opt/toolathlon"))
    if not repo_dir.exists():
        raise FileNotFoundError(f"Toolathlon repo directory not found: {repo_dir}")

    output_dir = Path(os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results"))
    dump_dir = output_dir / "toolathlon"
    dump_dir.mkdir(parents=True, exist_ok=True)

    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT")
    if not model_endpoint:
        raise ValueError("BENCHROUTER_MODEL_ENDPOINT is required")

    os.environ["TOOLATHLON_OPENAI_BASE_URL"] = model_endpoint
    os.environ["TOOLATHLON_OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "")

    ensure_runtime_configs(repo_dir)

    image_name = os.environ.get("TOOLATHLON_IMAGE_NAME") or os.environ.get("BENCHROUTER_ENV_IMAGE_TAG")
    if not image_name:
        raise ValueError("TOOLATHLON_IMAGE_NAME or BENCHROUTER_ENV_IMAGE_TAG is required")

    task_list_file = os.environ.get("TOOLATHLON_TASK_LIST_FILE")
    use_parallel = False
    if task_list_file:
        task_list_path = Path(task_list_file)
        if task_list_path.exists() and read_task_list(task_list_path):
            use_parallel = True

    if use_parallel:
        command = build_parallel_command(dump_dir, image_name)
        run_command(command, cwd=repo_dir)
        result = load_parallel_result(dump_dir)
    else:
        command = build_single_command(dump_dir, image_name)
        run_command(command, cwd=repo_dir)
        result = load_single_result(dump_dir)

    result["toolathlon_repo_dir"] = str(repo_dir)
    result["toolathlon_dump_dir"] = str(dump_dir)
    result["toolathlon_image_name"] = image_name
    result["model_name"] = os.environ.get("BENCHROUTER_MODEL_NAME")
    result["model_endpoint"] = model_endpoint

    output_path = output_dir / "toolathlon_result.json"
    with open(output_path, "w") as file:
        json.dump(result, file, indent=2)

    print(f"Toolathlon result written to {output_path}")
    print(f"Overall: {result['overall']}")


if __name__ == "__main__":
    main()
