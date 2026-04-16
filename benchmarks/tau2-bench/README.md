# tau2-bench (BenchRouter Integration)

This benchmark integrates [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) into BenchRouter via a bridge script that preserves the upstream `tau2 run` capability model.

## Integrated domains

- `tau2-airline`
- `tau2-retail`
- `tau2-telecom`
- `tau2-banking-knowledge`
- `tau2-mock`

## What is mapped

- BenchRouter model env -> tau2 LiteLLM env (`OPENAI_API_BASE`)
- tau2 run execution output -> BenchRouter `result.json`
- Official tau2 metrics via `compute_metrics(Results.load(...))`

`overall` in BenchRouter is mapped to:

1. `pass_hat_ks[1]` if present
2. otherwise `avg_reward`

## Configuration

### BenchRouter-provided env

- `BENCHROUTER_MODEL_ENDPOINT`
- `BENCHROUTER_MODEL_NAME`
- `OPENAI_API_KEY`
- `BENCHROUTER_OUTPUT_DIR`

### Common bridge env

- `TAU2_DOMAIN` (task-level default set in each task.yaml)
- `TAU2_USER_MODEL` (default: `BENCHROUTER_MODEL_NAME`)
- `TAU2_MODEL_PROVIDER` (default: `openai`; used to prefix bare model IDs)
- `TAU2_TASK_SPLIT` (default: `base`)
- `TAU2_TASK_SET` (optional)
- `TAU2_TASK_IDS` (comma-separated)
- `TAU2_NUM_TASKS` (optional)
- `TAU2_NUM_TRIALS` (default: `1`)
- `TAU2_MAX_STEPS` (default: `200`)
- `TAU2_MAX_ERRORS` (default: `10`)
- `TAU2_MAX_CONCURRENCY` (default: `3`)
- `TAU2_SEED` (default: `300`)
- `TAU2_TIMEOUT` (optional float)
- `TAU2_AGENT` (default: `llm_agent`)
- `TAU2_USER` (default: `user_simulator`)
- `TAU2_AGENT_LLM_ARGS_JSON` (default: `{"temperature": 0.0}`)
- `TAU2_USER_LLM_ARGS_JSON` (default: `{"temperature": 0.0}`)
- `TAU2_RETRIEVAL_CONFIG` (for `banking_knowledge`)
- `TAU2_RETRIEVAL_CONFIG_KWARGS_JSON` (JSON object)

### Full `tau2 run` pass-through

- `TAU2_EXTRA_ARGS`: shell-like arg string, appended to `tau2 run`
- `TAU2_EXTRA_ARGS_JSON`: JSON list of strings, appended to `tau2 run`

Use pass-through for advanced options (e.g. `--audio-native`, `--auto-review`, `--verbose-logs`, provider-specific flags, etc.).

## Register and run

```bash
benchrouter benchmark register --name tau2-bench --path benchmarks/tau2-bench/
benchrouter run tau2-bench --model your-model-name
```

## Notes

- This integration installs tau2 core + `knowledge` extras and sets `TAU2_DATA_DIR=/app/tau2-bench/data` in the environment image.
- Voice/full-duplex options can be passed through, but additional system/audio dependencies may be required for production voice runs.
