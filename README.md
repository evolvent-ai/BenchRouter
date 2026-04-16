# BenchRouter

Zero-adaptation LLM evaluation via containerized benchmark routing.

BenchRouter is a router, not a framework — it never modifies the original benchmark's logic. Adding a benchmark costs **2 YAML files and 1 script**. No base classes, no plugin system, no framework imports.

## How it works

```
benchrouter run mcpmark --model gpt-5.4
```

One command. BenchRouter registers the benchmark, builds the Docker image on first use, runs tasks in parallel, and aggregates results. The original benchmark code runs as-is inside containers — BenchRouter only handles routing and orchestration.

## Integrated Benchmarks

| Benchmark | Domain | Mode | Integration | Description |
|---|---|---|---|---|
| [MCPMark](benchmarks/mcpmark/) | Tool Calling | DinD | Bridge script | MCP tool-use evaluation (filesystem, Postgres, Playwright) |
| [WebArena](benchmarks/webarena/) | Web Interaction | DinD | Bridge script | Real web app interaction (9.6GB Magento, 182 tasks) |
| [SWE-bench Pro](benchmarks/swe-bench-pro/) | Software Engineering | Simple | Official code + patch | 731 real GitHub issues, contamination-free |
| [tau2-bench](benchmarks/tau2-bench/) | Customer Service | Simple | Bridge script | 5 domains (airline, retail, telecom, banking, knowledge) |
| [Toolathlon](benchmarks/toolathlon-local/) | Tool Calling | DinD | Bridge script | 35+ MCP servers, no external credentials |
| [BrowseComp](benchmarks/browsecomp/) | Web Browsing | Simple | Bridge script | OpenAI simple-evals browser benchmark |
| [lmms-eval](benchmarks/lmms-eval/) | Multimodal | Simple | Python API + patch | MMMU (70+ image, 30+ video benchmarks) |
| [xbench](benchmarks/xbench/) | Knowledge | Simple | Official code + patch | ScienceQA, encrypted dataset, contamination-free |

**Two execution modes:**
- **Simple** — single container, direct execution (16 GB RAM)
- **DinD** — isolated Docker daemon per task for benchmarks that spawn containers (32 GB RAM)

## Quick Start

### Prerequisites

- Python >= 3.12
- Docker Desktop (running)
- [uv](https://docs.astral.sh/uv/)
- An OpenAI-compatible model API (vLLM, OpenAI, OpenRouter, etc.)

### Install

```bash
git clone https://github.com/evolvent-ai/BenchRouter.git
cd BenchRouter
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install -e .
```

### Configure

```bash
cp .env.example .env
# Edit .env with your values, then:
set -a; source .env; set +a
```

| Variable | Description | Example |
|---|---|---|
| `BENCHROUTER_MODEL_ENDPOINT` | OpenAI-compatible API base URL | `https://openrouter.ai/api/v1` |
| `OPENAI_API_KEY` | API key | `sk-or-v1-...` |
| `BENCHROUTER_MODEL_NAME` | Model name | `openai/gpt-5.4` |

See `.env.example` for optional per-benchmark variables.

### Run

```bash
# Start server
benchrouter-server --data-dir /tmp/benchrouter-data --port 9000

# Register and run a benchmark
benchrouter benchmark register --name mcpmark --path benchmarks/mcpmark/
benchrouter run mcpmark --model gpt-5.4

# Compare models
benchrouter compare mcpmark --detail
```

## Adding a Benchmark

A valid benchmark needs exactly three things:

**1.** `benchmark.yaml` — declares tasks:

```yaml
name: "my-bench"
tasks:
  - name: default
    environment: my-bench
```

**2.** `task.yaml` — per-task config:

```yaml
name: "default"
command: "python /app/benchmark/eval.py"
timeout: 3600
```

**3.** A script that reads env vars and writes JSON:

```
Input:  BENCHROUTER_MODEL_ENDPOINT, BENCHROUTER_MODEL_NAME, OPENAI_API_KEY
Output: $BENCHROUTER_OUTPUT_DIR/result.json  →  {"overall": 0.85}
```

For DinD tasks (benchmarks that need their own Docker daemon), add `docker: true` to `task.yaml`.

```bash
benchrouter benchmark register --name my-bench --path benchmarks/my-bench/
benchrouter run my-bench --model your-model
```

See [How to Add a Benchmark](docs/how-to-add-benchmark.md) for details.

## CLI Reference

```bash
# Run evaluations
benchrouter run <benchmark> --model <model>
benchrouter run <benchmark> --model <model> --tasks <task1,task2>

# Manage benchmarks
benchrouter benchmark register --name <name> --path <path>
benchrouter benchmark list

# Monitor
benchrouter eval list
benchrouter eval status --run-id <run_id>
benchrouter eval result --run-id <run_id>
benchrouter logs <job_id> --follow

# Compare
benchrouter compare <benchmark>
benchrouter compare <benchmark> --detail
```

## Project Structure

```
BenchRouter/
├── benchrouter/
│   ├── server/          # FastAPI server, scheduler, registry
│   ├── client/          # Typer CLI
│   └── sdk/             # In-container runner + DinD entrypoint
├── benchmarks/          # 8 integrated benchmarks
├── docs/                # Developer documentation
└── tests/               # Unit tests
```
