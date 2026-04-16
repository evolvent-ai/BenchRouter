# BenchRouter v0.4 Design Document

## 1. Overview and Philosophy

BenchRouter is a lightweight, command-driven Evaluation-as-a-Service platform for LLM evaluation. The core design principle is: **remove unnecessary abstractions and minimize the cost of adding a new benchmark.**

Key tenets:

- **Benchmark authors write scripts, not plugins.** Any program that reads environment variables and writes a JSON file can be an BenchRouter task.
- **Docker is the only runtime contract.** Every task runs in a Docker container. The platform handles orchestration; the benchmark handles evaluation logic.
- **Models are external.** BenchRouter does not serve models. Models are pre-deployed behind an OpenAI-compatible API (vLLM, OpenAI, custom proxy, etc.). BenchRouter only schedules evaluation runs against those endpoints.
- **Hierarchical benchmarks.** A benchmark is a collection of related tasks. Each task is independently containerized, executed in parallel, and scored. Results are automatically aggregated.
- **Sidecar orchestration.** Tasks that need external services (databases, web applications) declare sidecars in their `task.yaml`. BenchRouter generates Docker Compose files on the fly.

### What Changed in v0.4

| v0.3 | v0.4 |
|------|------|
| Single `benchrouter.yaml` per benchmark | Split into `benchmark.yaml` + per-task `task.yaml` |
| One benchmark = one Docker run | One benchmark = one Run (parent) with N parallel Jobs |
| Flat job model | Run + Job hierarchy with aggregation |
| Manual environment pre-registration required | On-demand Docker image building (`_ensure_environment`) |
| `SubmitEvalRequest` had `environment` field | Environment resolved from task config; no `environment` in submit |
| No cross-task result aggregation | Automatic mean-score aggregation across tasks |
| `/evals` endpoints only | New `/runs` endpoints for benchmark-level tracking |


## 2. Protocol Contract

BenchRouter defines a minimal contract between the platform and benchmark code. A benchmark author must provide two YAML files and follow a simple I/O convention.

### 2.1 benchmark.yaml

Lives at the root of the benchmark directory. Declares the benchmark identity and its tasks.

```yaml
name: "mcpmark"                          # unique identifier
description: "MCPMark MCP tool-use benchmark suite"
version: "1.0.0"
tasks:
  - name: mcpmark-fs                     # subdirectory name
    environment: mcpmark-fs              # Docker environment name
  - name: mcpmark-pg
    environment: mcpmark-pg
```

**Required fields:**
- `name` (string) -- benchmark identifier, must match the registration name.
- `tasks` (list) -- each entry must have `name` (matching a subdirectory) and optionally `environment`.

**Optional fields:**
- `description` (string)
- `version` (string)

### 2.2 task.yaml

Lives in each task subdirectory (e.g., `mcpmark-fs/task.yaml`). Declares how to run a single task.

```yaml
name: "mcpmark-pg"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark PostgreSQL benchmark"
timeout: 7200                            # seconds, default 7200
result_file: "result.json"               # default "result.json"
result_mapping:
  overall: "pass@1"                      # remap non-standard field names
sidecars:                                # optional, triggers Docker Compose mode
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
    post_start:                          # commands to run after the sidecar is healthy
      - "some-init-command"
extra_env:                               # additional env vars for the eval container
  POSTGRES_HOST: "postgres"
```

**Required fields:**
- `name` (string) -- task identifier.
- `command` (string) -- shell command executed inside the container.

**Optional fields:**
- `description` (string)
- `timeout` (integer) -- max execution time in seconds. Default: 7200 (2 hours).
- `result_file` (string) -- filename to read results from. Default: `result.json`.
- `result_mapping` (dict) -- maps non-standard field names to the `overall` key expected by the runner.
- `sidecars` (list) -- sidecar service definitions for Docker Compose mode.
- `extra_env` (dict) -- additional environment variables injected into the eval container.

### 2.3 Environment Variables

The following environment variables are injected into every eval container by the scheduler:

| Variable | Description | Example |
|----------|-------------|---------|
| `BENCHROUTER_BENCHMARK_DIR` | Mount point for the task directory (read-only) | `/app/benchmark` |
| `BENCHROUTER_OUTPUT_DIR` | Mount point for writing results (read-write) | `/app/results` |
| `BENCHROUTER_MODEL_ENDPOINT` | OpenAI-compatible API base URL | `https://api.openai.com/v1` |
| `BENCHROUTER_MODEL_NAME` | Model identifier to pass in API calls | `gpt-4` |
| `OPENAI_API_KEY` | API key for the model endpoint | `sk-...` |
| `PYTHONPATH` | Set to `/app` for SDK import resolution | `/app` |

Any additional variables declared in `extra_env` in `task.yaml` are also injected.

### 2.4 result.json

The task script must write a JSON file to `$BENCHROUTER_OUTPUT_DIR/result.json` (or the filename specified in `result_file`). The file must contain an `overall` key (or the key specified in `result_mapping.overall`) with a numeric score.

```json
{
  "overall": 0.85,
  "correct": 17,
  "total": 20,
  "details": [...]
}
```

The SDK runner reads this file, applies `result_mapping` if present, and writes a standardized `result.json`:

```json
{
  "benchmark_name": "mcpmark-fs",
  "model_name": "gpt-4",
  "overall": 0.85,
  "raw_metrics": { "overall": 0.85, "correct": 17, "total": 20, ... }
}
```


## 3. Architecture

### 3.1 Components

```
                         +-------------------+
                         |    CLI (Typer)    |
                         |  benchrouter client  |
                         +---------+---------+
                                   | HTTP
                                   v
                         +-------------------+
                         | FastAPI Server    |
                         |  api.py (v0.4.0) |
                         +---------+---------+
                                   |
                    +--------------+--------------+
                    |                             |
           +--------v--------+          +---------v---------+
           |   EvalScheduler |          |     Registry      |
           |  scheduler.py   |          |   registry.py     |
           +--------+--------+          +---------+---------+
                    |                             |
         +----------+----------+         +--------v--------+
         |  Docker API / Compose|         | File System     |
         |  (container mgmt)   |         | (data_dir/)     |
         +---------------------+         +-----------------+

        Inside each container:
         +---------------------+
         | SDK Runner          |
         | runner.py           |
         | reads task.yaml     |
         | exec command        |
         | writes result.json  |
         +---------------------+
```

### 3.2 Component Responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| **CLI** | `benchrouter/client/cli.py` | User-facing commands: register, submit, poll, compare. Communicates with server via HTTP. |
| **API Server** | `benchrouter/server/api.py` | FastAPI routes. Thin layer that delegates to the scheduler. |
| **Scheduler** | `benchrouter/server/scheduler.py` | Core orchestration. Creates Runs and Jobs, manages parallel execution, builds Docker environments on demand, aggregates results. |
| **Registry** | `benchrouter/server/registry.py` | File-based persistence for benchmarks, environments, jobs, runs, and results. |
| **Models** | `benchrouter/server/models.py` | Pydantic data models: `EvalJobInfo`, `EvalRunInfo`, `SubmitEvalRequest`, `JobStatus`, `RunStatus`. |
| **Launcher** | `benchrouter/server/launch.py` | Uvicorn entry point with CLI argument parsing (`benchrouter-server`). |
| **SDK Runner** | `benchrouter/sdk/runner.py` | Runs inside the container. Reads `task.yaml`, executes the task command, reads raw results, writes standardized `result.json`. |


## 4. Scheduler Mechanics

### 4.1 Run + Job Model

When a user submits an evaluation, the scheduler creates a two-level structure:

```
Run (benchmark-level)
├── Job (task 1)   ─── Docker container
├── Job (task 2)   ─── Docker container
└── Job (task 3)   ─── Docker container
```

- A **Run** (`EvalRunInfo`) represents one execution of a benchmark. It has a `run_id`, references the benchmark name and model, and tracks child job IDs.
- A **Job** (`EvalJobInfo`) represents one execution of a task. It has a `job_id`, references its parent `run_id`, and tracks the container lifecycle.

**Status enums:**

```
JobStatus:  QUEUED → RUNNING → COMPLETED | FAILED
RunStatus:  QUEUED → RUNNING → COMPLETED | PARTIAL | FAILED
```

`PARTIAL` means some tasks succeeded and some failed. This allows partial results to be reported rather than treating the entire benchmark as failed.

### 4.2 Submission Flow

```python
submit_eval(benchmark, model_endpoint, model_name, api_key, tasks=None)
```

1. Validate the benchmark is registered.
2. Validate `model_endpoint` is non-empty.
3. Read the task list from `benchmark.yaml`. If `tasks` filter is provided, keep only matching tasks.
4. Generate a `run_id` (8-char UUID prefix).
5. For each task, create an `EvalJobInfo` with a unique `job_id`, linked to the `run_id`.
6. Create an `EvalRunInfo` with all child job IDs.
7. Persist both the run and all jobs to disk.
8. Launch `_run_benchmark` in a background thread.
9. Return immediately with `{run_id, job_ids, tasks, status: "QUEUED"}`.

### 4.3 Parallel Execution

`_run_benchmark` is the top-level orchestrator for a single run:

1. Set run status to `RUNNING`.
2. Spawn one thread per job, each calling `_run_job`.
3. Wait for all threads to complete (`join`).
4. Call `_aggregate_run_results`.

Concurrency is bounded by a `threading.Semaphore(max_concurrent)` (default 8). Each job acquires the semaphore before running its container and releases it when done. This prevents resource exhaustion when a benchmark has many tasks.

### 4.4 On-Demand Environment Building (`_ensure_environment`)

Before running a job, the scheduler checks if the required Docker image is available:

1. If the environment is registered and status is `ready`, proceed.
2. If status is `building`, poll every 2 seconds for up to 6 minutes.
3. If the environment does not exist, look for a `Dockerfile` at `benchmarks/{benchmark}/environments/{env_name}/Dockerfile`. If found, auto-register the environment and build synchronously.
4. If building fails or times out, the job fails with an error.

This means benchmark authors can include a `Dockerfile` in `environments/{env_name}/` within their benchmark directory and skip the manual `benchrouter env register` step. The image is built on first use.

### 4.5 Result Aggregation

After all jobs complete, `_aggregate_run_results` computes the benchmark-level score:

1. Collect `overall` from each completed job's `result.json`.
2. Compute the mean of all valid (non-None) scores.
3. Set the run status:
   - `COMPLETED` if all tasks produced results.
   - `PARTIAL` if at least one task succeeded but others failed.
   - `FAILED` if no task produced results.
4. Store the aggregated result in the run object:
   ```json
   {
     "benchmark_name": "mcpmark",
     "model_name": "gpt-4",
     "overall": 0.75,
     "task_scores": {"mcpmark-fs": 0.9, "mcpmark-pg": 0.6},
     "tasks_completed": 2,
     "tasks_total": 4
   }
   ```
5. Append a benchmark-level entry to the result index (`index.jsonl`).

Per-task results are also indexed individually during job completion (`_index_result`).


## 5. Eval Flow

### 5.1 Simple Mode (No Sidecars)

For tasks without `sidecars` in their `task.yaml`:

```
User: benchrouter run simple-math --model gpt-4
  │
  ├─ CLI → POST /evals/submit
  │         │
  │         └─ Scheduler.submit_eval()
  │              ├─ Creates Run + 1 Job
  │              └─ Spawns background thread
  │
  ├─ _run_benchmark(run_id)
  │    └─ _run_job(job_id)
  │         │
  │         ├─ _ensure_environment()
  │         │    └─ Build Docker image if not ready
  │         │
  │         ├─ docker.containers.run(
  │         │    image=benchrouter-env-simple-math:latest,
  │         │    command="python -m benchrouter.sdk.runner",
  │         │    volumes={task_dir: /app/benchmark, result_dir: /app/results, sdk_dir: /app/benchrouter/sdk},
  │         │    environment={BENCHROUTER_MODEL_ENDPOINT, BENCHROUTER_MODEL_NAME, ...},
  │         │    network_mode="host",
  │         │    mem_limit="16g", cpu_quota=400000,
  │         │  )
  │         │
  │         ├─ container.wait(timeout=7200)
  │         │
  │         └─ On success: status=COMPLETED, index result
  │            On failure: status=FAILED, capture error
  │
  └─ _aggregate_run_results(run_id)
       └─ Mean of task scores → run.aggregated_result
```

**Inside the container**, the SDK runner (`python -m benchrouter.sdk.runner`) does:

1. Read `/app/benchmark/task.yaml`.
2. Execute `command` (e.g., `python eval.py`) with `cwd=/app/benchmark`.
3. Read `$BENCHROUTER_OUTPUT_DIR/{result_file}`.
4. Apply `result_mapping` if present.
5. Write standardized `/app/results/result.json`.

### 5.2 Sidecar Mode (Docker Compose)

For tasks with `sidecars` in their `task.yaml`:

```
_run_job(job_id)
  │
  ├─ _ensure_environment()
  │
  ├─ _generate_compose_file(job_id)
  │    └─ Creates docker-compose.yml in temp dir:
  │         services:
  │           postgres:          # sidecar
  │             image: pgvector/pgvector:...
  │             healthcheck: ...
  │           eval:              # main eval container
  │             image: benchrouter-env-mcpmark-pg:latest
  │             depends_on: {postgres: service_healthy}
  │             command: python -m benchrouter.sdk.runner
  │             volumes: [task_dir, result_dir, sdk_dir]
  │             environment: [BENCHROUTER_*, extra_env]
  │
  ├─ docker compose up -d
  │
  ├─ Execute post_start commands on sidecars
  │    e.g., database migrations, config flushes
  │
  ├─ docker compose wait eval
  │    (blocks until eval container exits)
  │
  ├─ Check eval exit code
  │    On success: COMPLETED
  │    On failure: FAILED + capture logs
  │
  └─ docker compose down -v --remove-orphans
       (always, even on failure)
```

### 5.3 Volume Mounts

Every container gets three volume mounts:

| Host Path | Container Path | Mode | Purpose |
|-----------|---------------|------|---------|
| `{data_dir}/benchmarks/{benchmark}/{task}/` | `/app/benchmark` | ro | Task code and data |
| `{data_dir}/results/{job_id}/` | `/app/results` | rw | Result output |
| `benchrouter/sdk/` | `/app/benchrouter/sdk` | ro | SDK runner (so it can be invoked inside the container) |

### 5.4 Resource Limits

Default container limits (defined in `scheduler.py`):

| Limit | Default | Constant |
|-------|---------|----------|
| Timeout | 7200s (2 hours) | `DEFAULT_TIMEOUT` |
| Memory | 16 GB | `DEFAULT_MEM_LIMIT` |
| CPU | 4 cores | `DEFAULT_CPU_QUOTA=400000` with `DEFAULT_CPU_PERIOD=100000` |
| Max concurrent jobs | 8 | `DEFAULT_MAX_CONCURRENT` |


## 6. Error Handling

### 6.1 Job-Level Errors

| Error | Behavior |
|-------|----------|
| Environment build fails | Job status = `FAILED`, error message captured. |
| Container exits with non-zero code | Job status = `FAILED`, last 50 lines of logs captured in `job.error`. |
| Container timeout | Job status = `FAILED`, error = `"Timeout after {N}s. Task exceeded the time limit."` Container is killed. |
| `result.json` missing | The SDK runner raises `FileNotFoundError` inside the container, causing non-zero exit. |
| `overall` key missing | The SDK runner raises `KeyError` inside the container, causing non-zero exit. |
| Docker Compose `up` fails | Job status = `FAILED`, stderr captured. |
| `post_start` command fails | Job status = `FAILED`, stderr captured. |

### 6.2 Run-Level Errors

| Scenario | Run Status |
|----------|------------|
| All jobs completed | `COMPLETED` |
| Some jobs completed, some failed | `PARTIAL` |
| All jobs failed | `FAILED` |

### 6.3 Cleanup

- **Simple mode:** If a container is still running after an error (e.g., timeout), the scheduler calls `container.kill()`.
- **Sidecar mode:** `docker compose down -v --remove-orphans` is always called in a `finally` block, and the temp compose directory is removed with `shutil.rmtree`.
- The semaphore is always released in a `finally` block to prevent deadlocks.


## 7. Storage Layout

All persistent state lives under a single `data_dir` (default: `/data/benchrouter`, configurable via `--data-dir` or `BENCHROUTER_DATA_DIR`).

```
{data_dir}/
├── benchmarks/
│   ├── mcpmark/
│   │   ├── benchmark.yaml
│   │   ├── environments/           # Dockerfiles for auto-build
│   │   │   ├── mcpmark-fs/Dockerfile
│   │   │   ├── mcpmark-pg/Dockerfile
│   │   │   └── ...
│   │   ├── mcpmark-fs/
│   │   │   ├── task.yaml
│   │   │   └── run_mcpmark.py
│   │   ├── mcpmark-pg/
│   │   │   ├── task.yaml
│   │   │   └── run_mcpmark.py
│   │   └── ...
│   └── simple-math/
│       ├── benchmark.yaml
│       ├── environments/
│       │   └── simple-math/Dockerfile
│       └── default/
│           ├── task.yaml
│           └── eval.py
├── environments/
│   ├── mcpmark-fs/
│   │   ├── Dockerfile
│   │   └── meta.json              # {"name", "image_tag", "status"}
│   └── simple-math/
│       ├── Dockerfile
│       └── meta.json
├── runs/
│   └── {run_id}/
│       └── run.json               # serialized EvalRunInfo
├── jobs/
│   └── {job_id}/
│       └── job.json               # serialized EvalJobInfo
└── results/
    ├── index.jsonl                # append-only result index
    └── {job_id}/
        └── result.json            # standardized task result
```

**Persistence guarantees:**
- Runs and jobs are persisted to disk on every state change.
- On server restart, the scheduler constructor reloads all runs and jobs from disk.
- The result index (`index.jsonl`) is append-only and used for cross-model comparison queries.


## 8. API Endpoints

### 8.1 Benchmarks

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/benchmarks/{name}/upload` | Upload a benchmark as a tar.gz archive. |
| `GET` | `/benchmarks` | List all registered benchmarks. |
| `GET` | `/benchmarks/{name}` | Get a benchmark's `benchmark.yaml` content. |
| `DELETE` | `/benchmarks/{name}` | Delete a benchmark. |

### 8.2 Environments

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/environments/{name}/register` | Upload a Dockerfile. Server builds the image asynchronously. |
| `GET` | `/environments` | List all environments. |
| `GET` | `/environments/{name}` | Get environment status and image tag. |
| `DELETE` | `/environments/{name}` | Delete an environment. |

### 8.3 Runs (Benchmark-Level)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/evals/submit` | Submit a benchmark run. Body: `SubmitEvalRequest`. Returns `{run_id, job_ids, tasks, status}`. |
| `GET` | `/runs` | List all runs. |
| `GET` | `/runs/{run_id}` | Get run details, enriched with child job statuses. |
| `GET` | `/runs/{run_id}/result` | Get aggregated benchmark-level result. |

### 8.4 Jobs (Task-Level)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/evals` | List all jobs. |
| `GET` | `/evals/{job_id}` | Get a single job's details. |
| `GET` | `/evals/{job_id}/result` | Get a single task's standardized result. |
| `GET` | `/evals/{job_id}/logs` | Get container logs. Supports `?follow=true` for streaming and `?tail=N`. |

### 8.5 Comparison

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/evals/compare` | Compare results across models. Query params: `benchmark`, `models` (comma-separated), `detail` (boolean). |

### 8.6 Submit Request Schema

```json
{
  "benchmark": "mcpmark",
  "model_endpoint": "https://api.openai.com/v1",
  "model_name": "gpt-4",
  "api_key": "sk-...",
  "tasks": ["mcpmark-fs", "mcpmark-pg"]    // optional, default: all
}
```

Note: there is no `environment` field. The environment for each task is resolved from `benchmark.yaml` task definitions.


## 9. Project Structure

```
BenchRouter/
├── benchrouter/
│   ├── sdk/
│   │   └── runner.py                  # In-container entry point
│   ├── server/
│   │   ├── api.py                     # FastAPI routes
│   │   ├── scheduler.py               # Core orchestration
│   │   ├── registry.py                # File-based storage
│   │   ├── models.py                  # Pydantic models
│   │   └── launch.py                  # uvicorn entry point
│   └── client/
│       └── cli.py                     # Typer CLI
├── benchmarks/
│   └── mcpmark/                       # Real benchmark (4 tasks)
│       ├── benchmark.yaml
│       ├── environments/
│       │   ├── mcpmark-fs/Dockerfile
│       │   ├── mcpmark-pg/Dockerfile
│       │   ├── mcpmark-pw/Dockerfile
│       │   └── mcpmark-pw-webarena/Dockerfile
│       ├── mcpmark-fs/
│       ├── mcpmark-pg/
│       ├── mcpmark-pw/
│       └── mcpmark-pw-webarena/
├── examples/
│   ├── simple_math/                   # Minimal example
│   │   ├── benchmark.yaml
│   │   ├── environments/
│   │   │   └── simple-math/Dockerfile
│   │   └── default/
│   │       ├── task.yaml
│   │       └── eval.py
│   └── command_wrap/                  # Wrapping an existing benchmark
│       ├── benchmark.yaml
│       ├── environments/
│       │   └── command-wrap/Dockerfile
│       └── default/
│           ├── task.yaml              # Uses result_mapping
│           └── existing_benchmark.py
├── tests/
│   ├── test_models.py                 # Model validation tests
│   ├── test_registry.py               # Storage tests
│   └── test_scheduler.py              # Orchestration tests
├── pyproject.toml
└── README.md
```

**Entry points** (defined in `pyproject.toml`):
- `benchrouter` -- CLI client (`benchrouter.client.cli:app`)
- `benchrouter-server` -- Server launcher (`benchrouter.server.launch:main`)


## 10. Developer Guide: Adding New Benchmarks

### 10.1 Minimal Benchmark (Single Task)

1. Create the directory structure:

```
benchmarks/my-bench/
├── benchmark.yaml
├── environments/
│   └── my-bench/Dockerfile
└── default/
    ├── task.yaml
    └── eval.py
```

2. Write `benchmark.yaml`:

```yaml
name: "my-bench"
description: "My benchmark"
version: "1.0.0"
tasks:
  - name: default
    environment: my-bench
```

3. Write `default/task.yaml`:

```yaml
name: "my-bench-default"
command: "python eval.py"
```

4. Write `eval.py` following the contract:

```python
import os, json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["BENCHROUTER_MODEL_ENDPOINT"],
    api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
)
model = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
output_dir = os.environ["BENCHROUTER_OUTPUT_DIR"]

# ... run your evaluation logic ...

result = {"overall": 0.85, "details": [...]}
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(result, f)
```

5. Write `Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install openai pyyaml
```

6. Register and run:

```bash
benchrouter env register --name my-bench --dockerfile benchmarks/my-bench/environments/my-bench/Dockerfile
benchrouter benchmark register --name my-bench --path benchmarks/my-bench/
benchrouter run my-bench --model gpt-4
```

### 10.2 Multi-Task Benchmark

Create multiple task subdirectories, each with its own `task.yaml`:

```
benchmarks/my-suite/
├── benchmark.yaml
├── task-a/
│   ├── task.yaml
│   └── eval_a.py
├── task-b/
│   ├── task.yaml
│   └── eval_b.py
└── task-c/
    ├── task.yaml
    └── eval_c.py
```

Tasks can share an environment or each have their own. Each task runs in its own container in parallel.

### 10.3 Wrapping an Existing Benchmark

If you have an existing evaluation script that writes results in a non-standard format, use `result_file` and `result_mapping` in `task.yaml`:

```yaml
name: "humaneval-default"
command: "python existing_eval.py"
result_file: "scores.json"          # custom output filename
result_mapping:
  overall: "pass@1"                 # map pass@1 → overall
```

The SDK runner handles the field remapping automatically.

### 10.4 Tasks with Sidecars

For tasks that need external services (database, web app, etc.), declare sidecars in `task.yaml`:

```yaml
name: "my-db-task"
command: "python eval.py"
timeout: 7200
sidecars:
  - name: postgres
    image: "postgres:16"
    environment:
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]
    healthcheck:
      test: "pg_isready -U postgres"
      interval: "2s"
      timeout: "5s"
      retries: 30
    post_start:
      - "psql -U postgres -c 'CREATE DATABASE testdb;'"
extra_env:
  DB_HOST: "postgres"
  DB_PORT: "5432"
```

When sidecars are present, the scheduler generates a `docker-compose.yml` dynamically. The eval container's `depends_on` is set so it starts only after sidecar healthchecks pass. `post_start` commands run on the sidecar after it is healthy but before the eval container starts its work.

### 10.5 On-Demand Environment Building

If you place a `Dockerfile` at `environments/{env_name}/Dockerfile` within the benchmark directory, the environment is built automatically on first use. You do not need to manually register it with `benchrouter env register`. The `_ensure_environment` method in the scheduler handles this transparently.


## 11. Testing

### 11.1 Test Structure

Tests are in the `tests/` directory and use `pytest`:

- `test_models.py` -- validates Pydantic model constraints (required fields, removed fields, enum values).
- `test_registry.py` -- tests file-based storage operations using temp directories (benchmark registration, task config access, run/job persistence, result index, environment CRUD).
- `test_scheduler.py` -- tests orchestration logic without Docker (submit validation, task filtering, result aggregation, run persistence across restarts).

### 11.2 Test Design

Tests that involve the scheduler mock out `_run_benchmark` to avoid Docker dependency:

```python
@patch.object(EvalScheduler, "_run_benchmark")
def test_successful_submit_creates_run_and_jobs(self, mock_run, tmp_data_dir):
    ...
```

Aggregation tests construct job and run objects manually, write fake `result.json` files, and call `_aggregate_run_results` directly.

### 11.3 Running Tests

```bash
uv pip install pytest
pytest tests/ -v
```

### 11.4 What Is Tested

| Area | Coverage |
|------|----------|
| `JobStatus` / `RunStatus` enums | All values present, removed values absent |
| `EvalJobInfo` | Required fields, optional defaults |
| `EvalRunInfo` | Required fields, optional defaults, child job lists |
| `SubmitEvalRequest` | No `environment` field, optional `tasks` filter |
| Benchmark registration | Happy path, missing `benchmark.yaml`, missing `tasks`, missing `task.yaml` |
| Task config | Read `task.yaml`, get task directory |
| Run persistence | Save/get/list runs, survives scheduler restart |
| Job persistence | Save/get/list jobs |
| Result index | Append and read, empty index |
| Environment CRUD | Register, set status, delete |
| Submit validation | Unregistered benchmark, empty tasks, no matching filter, missing endpoint |
| Run+Job creation | Correct run_id, job_ids, task names, parent-child linkage |
| Task filter | Subset of tasks selected correctly |
| Result aggregation | All completed, partial failure, all failed, result index updated |
| Task timeout | Custom timeout from `task.yaml`, default fallback |
