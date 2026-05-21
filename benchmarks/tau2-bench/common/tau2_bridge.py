#!/usr/bin/env python3
"""Shared bridge implementation for BenchRouter <-> tau2-bench."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class Tau2Config:
    model_endpoint: str
    model_name: str
    user_model_name: str
    model_provider: str
    output_dir: Path
    domain: str
    task_split: str
    task_set: str | None
    task_ids: list[str] | None
    num_tasks: int | None
    num_trials: int
    max_steps: int
    max_errors: int
    max_concurrency: int
    seed: int
    timeout: float | None
    agent_impl: str
    user_impl: str
    agent_llm_args: dict[str, Any]
    user_llm_args: dict[str, Any]
    retrieval_config: str | None
    retrieval_kwargs: dict[str, Any] | None
    extra_args: list[str]


def env(name: str, default: Any = None, parser: Callable[[str], Any] = str) -> Any:
    """Generic env-var reader. Returns default for empty/unset; otherwise parser(value)."""
    value = os.environ.get(name, "")
    if value.strip() == "":
        return default
    return parser(value)


def _parse_json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def _parse_json_list(value: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("expected JSON list of strings")
    return parsed


def _parse_task_ids(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_model_name(model_name: str, provider: str) -> str:
    if "/" in model_name or not provider:
        return model_name
    return f"{provider}/{model_name}"


def load_config() -> Tau2Config:
    model_endpoint = env("BENCHROUTER_MODEL_ENDPOINT", "")
    model_name = env("BENCHROUTER_MODEL_NAME", "default")
    user_model_name = env("TAU2_USER_MODEL", model_name)
    model_provider = env("TAU2_MODEL_PROVIDER", "openai")

    extra_args: list[str] = []
    cli_extra = env("TAU2_EXTRA_ARGS", "")
    if cli_extra:
        extra_args.extend(shlex.split(cli_extra))
    extra_args.extend(env("TAU2_EXTRA_ARGS_JSON", [], _parse_json_list))

    return Tau2Config(
        model_endpoint=model_endpoint,
        model_name=model_name,
        user_model_name=user_model_name,
        model_provider=model_provider,
        output_dir=Path(env("BENCHROUTER_OUTPUT_DIR", "/app/results")),
        domain=env("TAU2_DOMAIN", "airline"),
        task_split=env("TAU2_TASK_SPLIT", "base"),
        task_set=env("TAU2_TASK_SET"),
        task_ids=env("TAU2_TASK_IDS", None, _parse_task_ids),
        num_tasks=env("TAU2_NUM_TASKS", None, int),
        num_trials=env("TAU2_NUM_TRIALS", 1, int),
        max_steps=env("TAU2_MAX_STEPS", 200, int),
        max_errors=env("TAU2_MAX_ERRORS", 10, int),
        max_concurrency=env("TAU2_MAX_CONCURRENCY", 3, int),
        seed=env("TAU2_SEED", 300, int),
        timeout=env("TAU2_TIMEOUT", None, float),
        agent_impl=env("TAU2_AGENT", "llm_agent"),
        user_impl=env("TAU2_USER", "user_simulator"),
        agent_llm_args=env("TAU2_AGENT_LLM_ARGS_JSON", {"temperature": 0.0}, _parse_json_object),
        user_llm_args=env("TAU2_USER_LLM_ARGS_JSON", {"temperature": 0.0}, _parse_json_object),
        retrieval_config=env("TAU2_RETRIEVAL_CONFIG"),
        retrieval_kwargs=env("TAU2_RETRIEVAL_CONFIG_KWARGS_JSON", None, _parse_json_object),
        extra_args=extra_args,
    )


def _run_dir(config: Tau2Config) -> Path:
    return config.output_dir / "tau2_run"


def _result_path(config: Tau2Config) -> Path:
    return config.output_dir / "result.json"


def _tau2_results_path(config: Tau2Config) -> Path:
    return _run_dir(config) / "results.json"


def write_result(config: Tau2Config, payload: dict[str, Any]) -> None:
    config.output_dir.mkdir(parents=True, exist_ok=True)
    _result_path(config).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_tau2_command(config: Tau2Config) -> list[str]:
    agent_model = _normalize_model_name(config.model_name, config.model_provider)
    user_model = _normalize_model_name(config.user_model_name, config.model_provider)

    command = [
        sys.executable, "-m", "tau2.cli", "run",
        "--domain", config.domain,
        "--agent", config.agent_impl,
        "--user", config.user_impl,
        "--agent-llm", agent_model,
        "--agent-llm-args", json.dumps(config.agent_llm_args),
        "--user-llm", user_model,
        "--user-llm-args", json.dumps(config.user_llm_args),
        "--task-split-name", config.task_split,
        "--num-trials", str(config.num_trials),
        "--max-steps", str(config.max_steps),
        "--max-errors", str(config.max_errors),
        "--max-concurrency", str(config.max_concurrency),
        "--seed", str(config.seed),
        "--auto-resume",
        "--save-to", str(_run_dir(config)),
    ]

    if config.task_set:
        command.extend(["--task-set-name", config.task_set])
    if config.task_ids:
        command.extend(["--task-ids", *config.task_ids])
    if config.num_tasks is not None:
        command.extend(["--num-tasks", str(config.num_tasks)])
    if config.timeout is not None:
        command.extend(["--timeout", str(config.timeout)])
    if config.retrieval_config:
        command.extend(["--retrieval-config", config.retrieval_config])
    if config.retrieval_kwargs is not None:
        command.extend(["--retrieval-config-kwargs", json.dumps(config.retrieval_kwargs)])

    command.extend(config.extra_args)
    return command


def _metrics_to_dict(metrics: Any) -> dict[str, Any]:
    if hasattr(metrics, "model_dump"):
        return metrics.model_dump()
    if hasattr(metrics, "dict"):
        return metrics.dict()
    raise TypeError("Unknown metrics object type")


def _build_benchrouter_metrics(config: Tau2Config, tau2_return_code: int) -> dict[str, Any]:
    from tau2.data_model.simulation import Results
    from tau2.metrics.agent_metrics import compute_metrics

    results = Results.load(_tau2_results_path(config))
    metrics_dict = _metrics_to_dict(compute_metrics(results))
    pass_hat_ks = metrics_dict.get("pass_hat_ks", {}) or {}
    overall = pass_hat_ks.get(1, metrics_dict.get("avg_reward", 0.0))

    return {
        "overall": float(overall),
        "avg_reward": float(metrics_dict.get("avg_reward", 0.0)),
        "pass_hat_ks": pass_hat_ks,
        "avg_agent_cost": float(metrics_dict.get("avg_agent_cost", 0.0)),
        "total_simulations": int(metrics_dict.get("total_simulations", 0)),
        "total_tasks": int(metrics_dict.get("total_tasks", 0)),
        "infra_error_count": int(metrics_dict.get("infra_error_count", 0)),
        "tau2": {
            "domain": config.domain,
            "task_split": config.task_split,
            "task_set": config.task_set,
            "model_endpoint": config.model_endpoint,
            "tau2_return_code": tau2_return_code,
        },
        "raw_metrics": metrics_dict,
    }


def run_bridge() -> int:
    config = load_config()

    if not config.model_endpoint:
        write_result(config, {"overall": 0.0, "error": "BENCHROUTER_MODEL_ENDPOINT is not set"})
        return 1

    config.output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TAU2_DATA_DIR", "/app/tau2-bench/data")
    os.environ["OPENAI_API_BASE"] = config.model_endpoint

    command = build_tau2_command(config)
    print("Running tau2:", " ".join(command))

    working_dir = "/app" if os.path.isdir("/app") else os.getcwd()
    return_code = subprocess.run(command, cwd=working_dir, check=False).returncode
    if return_code != 0:
        print(f"WARNING: tau2 exited with code {return_code}", file=sys.stderr)

    if not _tau2_results_path(config).exists():
        write_result(config, {
            "overall": 0.0,
            "error": f"tau2 output not found: {_tau2_results_path(config)}",
            "tau2_return_code": return_code,
        })
        return 1

    payload = _build_benchrouter_metrics(config, return_code)
    write_result(config, payload)
    print(f"BenchRouter result written: {_result_path(config)}")
    print(f"Overall: {payload['overall']}")
    return 0


def main() -> None:
    raise SystemExit(run_bridge())


if __name__ == "__main__":
    main()
