---
name: BenchRouter
description: "Guide for understanding the BenchRouter LLM evaluation framework (v0.4) and adding new benchmarks."
---

# BenchRouter v0.4 — Claude Code Skill

## Architecture Overview

BenchRouter is a lightweight LLM evaluation platform. Models are pre-deployed externally (vLLM, OpenAI-compatible API). BenchRouter only orchestrates evaluation tasks inside Docker containers.

```
Benchmark (benchmark.yaml)
├── environments/                                        ← Dockerfiles for each environment
│   ├── env-a/Dockerfile
│   └── env-b/Dockerfile
├── Task A/  (task.yaml + code)                          ← independent Docker container
├── Task B/  (task.yaml + code)                          ← runs in parallel
└── Task C/  (task.yaml + code)                          ← results auto-aggregated
```

**Core abstractions:**
- **Benchmark** — a group of related evaluations, defined by `benchmark.yaml` at the root
- **Task** — a single evaluation unit in a subdirectory, defined by `task.yaml` + executable code
- **Run** — one execution of a benchmark; spawns N parallel Jobs (one per task)
- **Job** — one execution of a single task inside a Docker container

**Execution flow:**
1. User registers a benchmark (tarballs the directory to the server)
2. User submits a run: `benchrouter run <benchmark> --model <name>`
3. Scheduler creates a Run with N child Jobs
4. Each Job: auto-builds Docker image from `environments/{env_name}/Dockerfile` if needed -> launches container -> SDK runner reads `task.yaml` -> executes `command` -> reads `result.json` -> standardizes output
5. Scheduler aggregates per-task `overall` scores into a benchmark-level mean

**Key source files:**
- `benchrouter/server/scheduler.py` — Run/Job orchestration, Docker/Compose execution, result aggregation
- `benchrouter/server/models.py` — Pydantic models (EvalJobInfo, EvalRunInfo, SubmitEvalRequest)
- `benchrouter/server/registry.py` — File-based persistence for benchmarks, environments, jobs, runs
- `benchrouter/sdk/runner.py` — In-container entrypoint: reads `task.yaml`, runs command, standardizes `result.json`
- `benchrouter/client/cli.py` — Typer CLI (`benchrouter run`, `benchrouter benchmark register`, etc.)

## The Contract

Every task must satisfy one simple contract:

1. **Read** environment variables injected by the scheduler
2. **Write** a JSON file to `$BENCHROUTER_OUTPUT_DIR` containing an `overall` field (float, 0.0-1.0)

### Environment Variables Injected by Scheduler

| Variable | Description | Example |
|---|---|---|
| `BENCHROUTER_BENCHMARK_DIR` | Path to task files inside container (read-only mount) | `/app/benchmark` |
| `BENCHROUTER_OUTPUT_DIR` | Path to write results (read-write mount) | `/app/results` |
| `BENCHROUTER_MODEL_ENDPOINT` | OpenAI-compatible API base URL | `https://api.example.com/v1` |
| `BENCHROUTER_MODEL_NAME` | Model identifier to pass in API calls | `gpt-4o` |
| `OPENAI_API_KEY` | API key for the model endpoint | `sk-...` |
| `PYTHONPATH` | Set to `/app` so `benchrouter.sdk` is importable | `/app` |

Additional variables from `extra_env` in `task.yaml` are merged in (e.g., database credentials for sidecar tasks).

### Result File

The SDK runner (`python -m benchrouter.sdk.runner`) handles standardization. Your task command must produce a JSON file in `$BENCHROUTER_OUTPUT_DIR`. By default the runner looks for `result.json` with an `overall` key. You can customize both via `task.yaml`:

```yaml
result_file: "scores.json"       # default: "result.json"
result_mapping:
  overall: "pass@1"              # default: "overall"
```

The runner wraps your raw output into a standardized envelope:
```json
{
  "benchmark_name": "...",
  "model_name": "...",
  "overall": 0.85,
  "raw_metrics": { /* your original JSON */ }
}
```

The `overall` value is what gets aggregated across tasks. The scheduler computes the benchmark-level score as the mean of all task `overall` values.

## benchmark.yaml Schema

Located at the root of the benchmark directory.

```yaml
name: "my-benchmark"                    # required — unique benchmark identifier
description: "What this benchmark does" # recommended
version: "1.0.0"                        # recommended
tasks:                                  # required — at least one task
  - name: task-a                        # required — must match a subdirectory name
    environment: task-a                 # required — Docker environment name
  - name: task-b
    environment: task-b
```

The `name` in each task entry must correspond to a subdirectory containing a `task.yaml`. The `environment` determines which Docker image the task runs in; the scheduler auto-builds it from `environments/{env_name}/Dockerfile` within the benchmark directory.

## task.yaml Schema

Located in each task subdirectory.

```yaml
name: "my-benchmark-task-a"             # required — task display name
command: "python eval.py"               # required — shell command to execute
description: "..."                      # optional
timeout: 3600                           # optional — seconds (default: 7200)
result_file: "result.json"              # optional — filename the task writes (default: "result.json")
result_mapping:                         # optional — field name remapping
  overall: "pass@1"                     #   maps "pass@1" in your output to "overall"

# Sidecar services (optional — uses Docker Compose when present)
sidecars:
  - name: postgres                      # service name (used in networking)
    image: "postgres:16"                # Docker image
    environment:                        # env vars for sidecar container
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]                # port mappings
    healthcheck:                        # wait for service readiness
      test: "pg_isready -U postgres"
      interval: "2s"
      timeout: "5s"
      retries: 30
    post_start:                         # commands to run after sidecar is healthy
      - "psql -U postgres -c 'CREATE DATABASE testdb;'"

extra_env:                              # optional — additional env vars for eval container
  POSTGRES_HOST: "postgres"
  POSTGRES_PORT: "5432"
```

## Three Scenarios for Adding Benchmarks

### Scenario 1: Simple (write from scratch)

Best when: you are building a new eval or the logic is straightforward.

**Directory structure:**
```
benchmarks/my-bench/
├── benchmark.yaml
├── environments/
│   └── my-bench/Dockerfile
└── default/
    ├── task.yaml
    └── eval.py
```

**benchmark.yaml:**
```yaml
name: "my-bench"
description: "My new evaluation"
version: "1.0.0"
tasks:
  - name: default
    environment: my-bench
```

**Dockerfile:**
```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install openai pyyaml
```

**default/task.yaml:**
```yaml
name: "my-bench-default"
command: "python eval.py"
```

**default/eval.py** (minimal example):
```python
import os, json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["BENCHROUTER_MODEL_ENDPOINT"],
    api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
)
model = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
output_dir = os.environ["BENCHROUTER_OUTPUT_DIR"]

# ... run evaluation logic, call client.chat.completions.create() ...

result = {"overall": 0.85, "details": [...]}
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(result, f, indent=2)
```

### Scenario 2: Bridge Pattern (wrap an existing benchmark)

Best when: integrating an existing benchmark codebase (e.g., MCPMark, HumanEval) that already has its own Docker image and evaluation logic.

**Key idea:** Write a thin bridge script that translates BenchRouter env vars into whatever the existing tool expects, runs it, then collects its output into BenchRouter's `result.json` format.

**Directory structure:**
```
benchmarks/my-bench/
├── benchmark.yaml
└── task-name/
    ├── task.yaml
    └── run_bridge.py          # the bridge script
```

The Dockerfile for this scenario typically uses the existing benchmark's image as the base. Register it as an environment or place it in `environments/{env_name}/Dockerfile`. For example, MCPMark tasks use `evalsysorg/mcpmark:latest` as their base image, and the bridge script (`run_mcpmark.py`) translates env vars, invokes MCPMark's `pipeline` module, collects `meta.json` outputs, and writes `result.json`.

**Bridge script pattern:**
```python
import os, json, subprocess, sys

# 1. Read BenchRouter env vars
model_endpoint = os.environ["BENCHROUTER_MODEL_ENDPOINT"]
model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

# 2. Translate to the existing tool's env vars / CLI args
os.environ["EXISTING_TOOL_API_URL"] = model_endpoint

# 3. Run the existing tool
subprocess.run([sys.executable, "-m", "existing_tool", "--model", model_name], check=True)

# 4. Collect results and write result.json
raw_results = json.load(open("/tmp/existing_output/scores.json"))
overall = raw_results["accuracy"]

result = {"overall": overall, "raw": raw_results}
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(result, f, indent=2)
```

**Alternative (no bridge script):** If the existing tool already writes a JSON file, you can point `task.yaml` directly at it using `result_file` and `result_mapping`:
```yaml
name: "wrapped-bench-default"
command: "python existing_benchmark.py"
result_file: "scores.json"
result_mapping:
  overall: "pass@1"
```

### Scenario 3: Sidecar Pattern (tasks needing external services)

Best when: the evaluation needs a database, web application, or other service alongside the eval container.

When `task.yaml` contains a `sidecars` list, the scheduler uses Docker Compose instead of a single container. It generates a `docker-compose.yml` with:
- One service per sidecar (with health checks and optional `post_start` commands)
- An `eval` service that `depends_on` all sidecars and runs the evaluation

**Example — PostgreSQL sidecar:**
```yaml
name: "my-db-eval"
command: "python eval.py"
timeout: 7200
sidecars:
  - name: postgres
    image: "pgvector/pgvector:0.8.0-pg17-bookworm"
    environment:
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]
    healthcheck:
      test: "pg_isready -U postgres"
      interval: "2s"
      timeout: "5s"
      retries: 30
extra_env:
  POSTGRES_HOST: "postgres"
  POSTGRES_PORT: "5432"
  POSTGRES_PASSWORD: "password"
```

**Example — Heavy sidecar with post_start (WebArena):**
```yaml
sidecars:
  - name: shopping_admin
    image: "shopping_admin_final_0719"
    ports: ["7780:80"]
    healthcheck:
      test: "curl -sf http://localhost:80/admin || exit 1"
      interval: "10s"
      timeout: "15s"
      retries: 60
    post_start:
      - "mysql -u magentouser -pMyPassword magentodb -e \"UPDATE ...\""
      - "/var/www/magento2/bin/magento cache:flush"
extra_env:
  WEBARENA_SHOPPING_ADMIN_URL: "http://shopping_admin:80"
```

Sidecar networking: containers communicate via Docker Compose service names (e.g., `postgres`, `shopping_admin`). The eval container references sidecars by their `name` field in `extra_env`.

## Register and Run Commands

```bash
# 1. Start the server
benchrouter-server --data-dir /tmp/benchrouter-data --port 9000

# 2. Register environment (builds Docker image on server)
benchrouter env register --name my-bench --dockerfile benchmarks/my-bench/environments/my-bench/Dockerfile
benchrouter env status --name my-bench    # wait for "Status: ready"

# 3. Register benchmark (uploads directory to server)
benchrouter benchmark register --name my-bench --path benchmarks/my-bench/

# 4. Run all tasks
export BENCHROUTER_MODEL_ENDPOINT=https://your-api/v1
export OPENAI_API_KEY=sk-...
benchrouter run my-bench --model your-model

# 5. Run specific tasks only
benchrouter run my-bench --model your-model --tasks task-a,task-b

# 6. View results
benchrouter eval status --run-id <run_id>
benchrouter eval result --run-id <run_id>

# 7. Compare models
benchrouter compare my-bench
benchrouter compare my-bench --detail
```

Note: environment auto-build is supported. If you place a `Dockerfile` at `environments/{env_name}/Dockerfile` within the benchmark directory, the scheduler will auto-register and build the environment when a job runs. Explicit `benchrouter env register` is optional but recommended for faster first runs.

## When You Might Need to Modify Skeleton Code

Most benchmarks need zero changes to BenchRouter itself. You only touch framework code if:

1. **New result aggregation logic** — The default aggregation computes the mean of all task `overall` scores (see `_aggregate_run_results` in `scheduler.py`). If your benchmark needs weighted averaging, min, or custom logic, modify that method.

2. **Custom container resources** — Default limits are 16 GB memory, 4 CPU cores, 2-hour timeout. Per-task timeout is configurable in `task.yaml`. Memory/CPU limits require changes to `scheduler.py` constants (`DEFAULT_MEM_LIMIT`, `DEFAULT_CPU_QUOTA`).

3. **Non-Docker execution** — The entire system assumes Docker. If you need bare-metal execution, significant scheduler changes are needed.

4. **New environment variables** — If you need the scheduler to inject variables beyond the standard set, add them in `_build_environment_vars` in `scheduler.py`. However, `extra_env` in `task.yaml` covers most cases without code changes.

5. **New CLI commands** — Add to `benchrouter/client/cli.py` (Typer app). The server API is in `benchrouter/server/api.py` (FastAPI).

## Reference: MCPMark Integration

MCPMark is the flagship multi-task benchmark demonstrating all three patterns.

**Structure:**
```
benchmarks/mcpmark/
├── benchmark.yaml              # declares 4 tasks
├── environments/
│   ├── mcpmark-fs/Dockerfile
│   ├── mcpmark-pg/Dockerfile
│   ├── mcpmark-pw/Dockerfile
│   └── mcpmark-pw-webarena/Dockerfile
├── mcpmark-fs/                 # simple: no sidecars
│   ├── task.yaml
│   └── run_mcpmark.py          # bridge script
├── mcpmark-pg/                 # sidecar: PostgreSQL
│   ├── task.yaml               # sidecars + extra_env
│   └── run_mcpmark.py
├── mcpmark-pw/                 # simple: Playwright (no sidecar)
│   ├── task.yaml
│   └── run_mcpmark.py
└── mcpmark-pw-webarena/        # heavy sidecar: WebArena Magento with post_start
    ├── task.yaml
    └── run_mcpmark.py
```

Each task reuses the same `run_mcpmark.py` bridge script. The script:
1. Reads `BENCHROUTER_MODEL_ENDPOINT`, `BENCHROUTER_MODEL_NAME`, `OPENAI_API_KEY`
2. Sets `OPENAI_API_BASE` for LiteLLM compatibility
3. Prefixes model name with `openai/` for LiteLLM routing
4. Runs MCPMark pipeline via `subprocess`
5. Collects per-task `meta.json` files from MCPMark output
6. Computes pass rate and writes `result.json` with `overall` field

Each task uses a different Docker image (pre-built from MCPMark with the right MCP server baked in). The scheduler auto-builds or finds the environment by the `environment` field in `benchmark.yaml`.

## Checklist for Adding a New Benchmark

- [ ] Create benchmark directory under `benchmarks/` (or `examples/` for demos)
- [ ] Write `benchmark.yaml` with `name`, `tasks` list; each task entry has `name` and `environment`
- [ ] For each task, create a subdirectory matching the task `name`
- [ ] In each task subdirectory, write `task.yaml` with `name` and `command`
- [ ] Place a `Dockerfile` in `environments/{env_name}/` within the benchmark directory
- [ ] Implement the eval script: read env vars, call model API, write `result.json` with `overall`
- [ ] If wrapping an existing tool: write a bridge script or use `result_file`/`result_mapping`
- [ ] If the task needs external services: add `sidecars` and `extra_env` to `task.yaml`
- [ ] Test locally: start server, register env + benchmark, run with a test model
- [ ] Verify `result.json` contains a numeric `overall` field
- [ ] Verify aggregated benchmark score appears in `benchrouter eval result --run-id <id>`
