#!/usr/bin/env python3
"""Shared bridge implementation for BenchRouter <-> tau2-bench."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


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
    api_mode: str
    bedrock_api_key: str


def _parse_int_optional(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


def _parse_float_optional(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return float(value)


def _parse_json_object(name: str, default: dict[str, Any]) -> dict[str, Any]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _parse_json_list(name: str) -> list[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{name} must be a JSON list of strings")
    return parsed


def _parse_task_ids(name: str) -> list[str] | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _normalize_model_name(model_name: str, provider: str) -> str:
    if "/" in model_name:
        return model_name
    if provider:
        return f"{provider}/{model_name}"
    return model_name


def _normalize_bedrock_model_id(model_name: str) -> str:
    if "/" not in model_name:
        return model_name
    return model_name.split("/", 1)[1]


def _resolve_cli_models(config: Tau2Config) -> tuple[str, str]:
    if config.api_mode == "bedrock_converse":
        agent_model_id = _normalize_bedrock_model_id(config.model_name)
        user_model_id = _normalize_bedrock_model_id(config.user_model_name)
        return f"openai/{agent_model_id}", f"openai/{user_model_id}"

    agent_model = _normalize_model_name(config.model_name, config.model_provider)
    user_model = _normalize_model_name(config.user_model_name, config.model_provider)
    return agent_model, user_model


def _detect_default_api_mode(model_endpoint: str, bedrock_api_key: str) -> str:
    if "bedrock-runtime" in model_endpoint.lower() and bedrock_api_key.startswith("ABSK"):
        return "bedrock_converse"
    return "openai_compat"


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return str(content)


def _to_bedrock_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    bedrock_messages: list[dict[str, Any]] = []
    system_prompts: list[dict[str, str]] = []

    for msg in messages:
        role = msg.get("role")
        text = _extract_text_content(msg.get("content", ""))
        if role == "system":
            if text:
                system_prompts.append({"text": text})
            continue
        if role in {"user", "assistant"}:
            bedrock_messages.append({"role": role, "content": [{"text": text}]})

    if not bedrock_messages:
        bedrock_messages.append({"role": "user", "content": [{"text": ""}]})

    return bedrock_messages, system_prompts


class _BedrockToOpenAIProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_request(self) -> dict[str, Any] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_response(400, {"error": {"message": "invalid Content-Length"}})
            return None

        try:
            body = self.rfile.read(content_length)
            request_data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "invalid JSON body"}})
            return None

        if not isinstance(request_data, dict):
            self._json_response(400, {"error": {"message": "request body must be a JSON object"}})
            return None

        return request_data

    def _parse_model_and_messages(self, req: dict[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
        model_id = str(req.get("model", ""))
        if not model_id:
            self._json_response(400, {"error": {"message": "model is required"}})
            return None

        normalized_model_id = _normalize_bedrock_model_id(model_id.removeprefix("openai/"))

        raw_messages = req.get("messages", [])
        if not isinstance(raw_messages, list):
            self._json_response(400, {"error": {"message": "messages must be a list"}})
            return None

        messages: list[dict[str, Any]] = []
        for item in raw_messages:
            if isinstance(item, dict):
                messages.append(item)
            else:
                self._json_response(400, {"error": {"message": "messages must contain JSON objects"}})
                return None

        return normalized_model_id, messages

    @staticmethod
    def _build_bedrock_payload(req: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
        bedrock_messages, system_prompts = _to_bedrock_messages(messages)
        inference_cfg: dict[str, Any] = {}
        if req.get("max_tokens") is not None:
            inference_cfg["maxTokens"] = req["max_tokens"]
        if req.get("temperature") is not None:
            inference_cfg["temperature"] = req["temperature"]
        if req.get("top_p") is not None:
            inference_cfg["topP"] = req["top_p"]

        payload: dict[str, Any] = {"messages": bedrock_messages}
        if system_prompts:
            payload["system"] = system_prompts
        if inference_cfg:
            payload["inferenceConfig"] = inference_cfg
        return payload

    def _call_bedrock(self, model_id: str, payload: dict[str, Any]) -> requests.Response | None:
        bridge = self.server.bridge_context  # type: ignore[attr-defined]
        bedrock_url = f"{bridge['endpoint'].rstrip('/')}/model/{model_id}/converse"
        try:
            return requests.post(
                bedrock_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {bridge['api_key']}",
                },
                json=payload,
                timeout=120,
            )
        except requests.RequestException as exc:
            self._json_response(502, {"error": {"message": f"Bedrock upstream request failed: {exc}"}})
            return None

    @staticmethod
    def _extract_content_text(data: dict[str, Any]) -> str:
        output_msg = data.get("output", {}).get("message", {})
        if not isinstance(output_msg, dict):
            return ""
        content_list = output_msg.get("content", [])
        if not isinstance(content_list, list):
            return ""
        text_parts = []
        for item in content_list:
            if isinstance(item, dict) and "text" in item:
                text_parts.append(str(item["text"]))
        return "\n".join(text_parts)

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> dict[str, int]:
        usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("inputTokens", 0) or 0)
        completion_tokens = int(usage.get("outputTokens", 0) or 0)
        total_tokens = int(usage.get("totalTokens", prompt_tokens + completion_tokens) or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._json_response(404, {"error": {"message": "Not Found"}})
            return

        request_data = self._read_request()
        if request_data is None:
            return

        parsed = self._parse_model_and_messages(request_data)
        if parsed is None:
            return
        model_id, messages = parsed

        payload = self._build_bedrock_payload(request_data, messages)
        response = self._call_bedrock(model_id, payload)
        if response is None:
            return

        if response.status_code != 200:
            self._json_response(response.status_code, {"error": {"message": f"Bedrock error: {response.text}"}})
            return

        data = response.json()
        if not isinstance(data, dict):
            self._json_response(502, {"error": {"message": "Bedrock response must be a JSON object"}})
            return

        content_text = self._extract_content_text(data)
        usage = self._extract_usage(data)
        self._json_response(
            200,
            {
                "id": "bedrock-proxy-chatcmpl",
                "object": "chat.completion",
                "model": f"openai/{model_id}",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class _BedrockOpenAICompatProxy:
    def __init__(self, endpoint: str, api_key: str):
        self.endpoint = endpoint
        self.api_key = api_key
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        host = "127.0.0.1"
        self.server = ThreadingHTTPServer((host, 0), _BedrockToOpenAIProxyHandler)
        self.server.bridge_context = {"endpoint": self.endpoint, "api_key": self.api_key}  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        return f"http://{host}:{port}/v1"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def load_config() -> Tau2Config:
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "").strip()
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default").strip()
    user_model_name = os.environ.get("TAU2_USER_MODEL", model_name).strip()
    model_provider = os.environ.get("TAU2_MODEL_PROVIDER", "openai").strip()

    bedrock_api_key = (
        os.environ.get("BEDROCK_API_KEY", "").strip()
        or os.environ.get("BEDROCK_BEARER_TOKEN", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )

    api_mode = os.environ.get("TAU2_API_MODE", "auto").strip().lower()
    if api_mode not in {"auto", "openai_compat", "bedrock_converse"}:
        raise ValueError("TAU2_API_MODE must be one of: auto, openai_compat, bedrock_converse")
    if api_mode == "auto":
        api_mode = _detect_default_api_mode(model_endpoint, bedrock_api_key)

    extra_args = []
    cli_extra = os.environ.get("TAU2_EXTRA_ARGS", "").strip()
    if cli_extra:
        extra_args.extend(shlex.split(cli_extra))
    extra_args.extend(_parse_json_list("TAU2_EXTRA_ARGS_JSON"))

    return Tau2Config(
        model_endpoint=model_endpoint,
        model_name=model_name,
        user_model_name=user_model_name,
        model_provider=model_provider,
        output_dir=Path(os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")),
        domain=os.environ.get("TAU2_DOMAIN", "airline").strip(),
        task_split=os.environ.get("TAU2_TASK_SPLIT", "base").strip(),
        task_set=os.environ.get("TAU2_TASK_SET") or None,
        task_ids=_parse_task_ids("TAU2_TASK_IDS"),
        num_tasks=_parse_int_optional("TAU2_NUM_TASKS"),
        num_trials=int(os.environ.get("TAU2_NUM_TRIALS", "1")),
        max_steps=int(os.environ.get("TAU2_MAX_STEPS", "200")),
        max_errors=int(os.environ.get("TAU2_MAX_ERRORS", "10")),
        max_concurrency=int(os.environ.get("TAU2_MAX_CONCURRENCY", "3")),
        seed=int(os.environ.get("TAU2_SEED", "300")),
        timeout=_parse_float_optional("TAU2_TIMEOUT"),
        agent_impl=os.environ.get("TAU2_AGENT", "llm_agent").strip(),
        user_impl=os.environ.get("TAU2_USER", "user_simulator").strip(),
        agent_llm_args=_parse_json_object("TAU2_AGENT_LLM_ARGS_JSON", {"temperature": 0.0}),
        user_llm_args=_parse_json_object("TAU2_USER_LLM_ARGS_JSON", {"temperature": 0.0}),
        retrieval_config=os.environ.get("TAU2_RETRIEVAL_CONFIG") or None,
        retrieval_kwargs=(
            _parse_json_object("TAU2_RETRIEVAL_CONFIG_KWARGS_JSON", {})
            if os.environ.get("TAU2_RETRIEVAL_CONFIG_KWARGS_JSON")
            else None
        ),
        extra_args=extra_args,
        api_mode=api_mode,
        bedrock_api_key=bedrock_api_key,
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
    agent_model, user_model = _resolve_cli_models(config)

    command = [
        sys.executable,
        "-m",
        "tau2.cli",
        "run",
        "--domain",
        config.domain,
        "--agent",
        config.agent_impl,
        "--user",
        config.user_impl,
        "--agent-llm",
        agent_model,
        "--agent-llm-args",
        json.dumps(config.agent_llm_args),
        "--user-llm",
        user_model,
        "--user-llm-args",
        json.dumps(config.user_llm_args),
        "--task-split-name",
        config.task_split,
        "--num-trials",
        str(config.num_trials),
        "--max-steps",
        str(config.max_steps),
        "--max-errors",
        str(config.max_errors),
        "--max-concurrency",
        str(config.max_concurrency),
        "--seed",
        str(config.seed),
        "--auto-resume",
        "--save-to",
        str(_run_dir(config)),
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


def run_subprocess(command: list[str]) -> int:
    working_dir = "/app" if os.path.isdir("/app") else os.getcwd()
    completed = subprocess.run(command, cwd=working_dir, check=False)
    return completed.returncode


def _metrics_to_dict(metrics: Any) -> dict[str, Any]:
    if hasattr(metrics, "model_dump"):
        return metrics.model_dump()
    if hasattr(metrics, "dict"):
        return metrics.dict()
    raise TypeError("Unknown metrics object type")


def _build_benchrouter_metrics(config: Tau2Config, tau2_return_code: int) -> dict[str, Any]:
    from tau2.data_model.simulation import Results
    from tau2.metrics.agent_metrics import compute_metrics

    tau2_results_path = _tau2_results_path(config)
    results = Results.load(tau2_results_path)
    metrics = compute_metrics(results)
    metrics_dict = _metrics_to_dict(metrics)

    pass_hat_ks = metrics_dict.get("pass_hat_ks", {}) or {}
    overall = pass_hat_ks.get(1)
    if overall is None:
        overall = metrics_dict.get("avg_reward", 0.0)
    agent_model, user_model = _resolve_cli_models(config)

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
            "task_ids": config.task_ids,
            "num_tasks": config.num_tasks,
            "num_trials": config.num_trials,
            "max_steps": config.max_steps,
            "max_errors": config.max_errors,
            "max_concurrency": config.max_concurrency,
            "seed": config.seed,
            "timeout": config.timeout,
            "agent_model": agent_model,
            "user_model": user_model,
            "model_endpoint": config.model_endpoint,
            "api_mode": config.api_mode,
            "run_dir": str(_run_dir(config)),
            "results_file": str(tau2_results_path),
            "tau2_return_code": tau2_return_code,
            "extra_args": config.extra_args,
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

    proxy: _BedrockOpenAICompatProxy | None = None
    openai_base = config.model_endpoint

    if config.api_mode == "bedrock_converse":
        if not config.bedrock_api_key:
            write_result(
                config,
                {
                    "overall": 0.0,
                    "error": "BEDROCK_API_KEY or OPENAI_API_KEY must be set for bedrock_converse mode",
                },
            )
            return 1
        parsed = urlparse(config.model_endpoint)
        if "bedrock-runtime" not in parsed.netloc:
            write_result(
                config,
                {
                    "overall": 0.0,
                    "error": "bedrock_converse mode requires a Bedrock Runtime endpoint",
                },
            )
            return 1
        proxy = _BedrockOpenAICompatProxy(config.model_endpoint, config.bedrock_api_key)
        openai_base = proxy.start()

    os.environ["OPENAI_API_BASE"] = openai_base
    if config.api_mode == "bedrock_converse":
        os.environ.setdefault("OPENAI_API_KEY", config.bedrock_api_key or "DUMMY_KEY")

    command = build_tau2_command(config)
    print("Running tau2:", " ".join(command))

    try:
        return_code = run_subprocess(command)
        if return_code != 0:
            print(f"WARNING: tau2 exited with code {return_code}", file=sys.stderr)
    finally:
        if proxy is not None:
            proxy.stop()

    tau2_results_path = _tau2_results_path(config)
    if not tau2_results_path.exists():
        write_result(
            config,
            {
                "overall": 0.0,
                "error": f"tau2 output not found: {tau2_results_path}",
                "tau2_return_code": return_code,
            },
        )
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
