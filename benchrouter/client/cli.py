import typer
import requests
import os
import tarfile
import tempfile
import json
import time
from typing import Optional

app = typer.Typer(name="benchrouter", help="BenchRouter CLI — LLM Evaluation Platform")
benchmark_app = typer.Typer(help="Manage benchmarks")
env_app = typer.Typer(help="Manage environments")
eval_app = typer.Typer(help="Manage evaluation jobs")

app.add_typer(benchmark_app, name="benchmark")
app.add_typer(env_app, name="env")
app.add_typer(eval_app, name="eval")

DEFAULT_SERVER = "http://localhost:9000"
DEFAULT_TIMEOUT = 30  # seconds for normal requests


# ---- Helpers ----


def _poll_run(server: str, run_id: str):
    """Poll a benchmark run until all tasks complete, then display results."""
    while True:
        try:
            resp = requests.get(f"{server}/runs/{run_id}", timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            run = resp.json()
            status = run["status"]
        except requests.exceptions.RequestException:
            time.sleep(2)
            continue

        # Show per-task progress
        child_jobs = run.get("child_jobs", [])
        done = sum(
            1 for j in child_jobs
            if j["status"] in ("COMPLETED", "FAILED")
        )
        total = len(child_jobs)
        if total > 0:
            task_summary = ", ".join(
                f"{j['task_name']}[{j['status'][:1]}]" for j in child_jobs
            )
            typer.echo(f"\r  [{done}/{total}] {task_summary}   ", nl=False)

        if status in ("COMPLETED", "PARTIAL", "FAILED"):
            typer.echo()
            break

        time.sleep(2)

    if status == "FAILED":
        typer.echo("Run failed: all tasks failed.", err=True)
        raise typer.Exit(1)

    # Display results
    resp = requests.get(f"{server}/runs/{run_id}/result", timeout=DEFAULT_TIMEOUT)
    if resp.status_code == 200:
        result = resp.json()
        typer.echo(f"\n  Benchmark: {result['benchmark_name']}")
        overall = result.get("overall")
        if overall is not None:
            typer.echo(f"  Overall:   {overall:.4f}")
        else:
            typer.echo("  Overall:   N/A")
        typer.echo(
            f"  Tasks:     {result['tasks_completed']}/{result['tasks_total']} completed"
        )
        task_scores = result.get("task_scores", {})
        if task_scores:
            typer.echo("\n  Per-task scores:")
            for task, score in task_scores.items():
                score_str = f"{score:.4f}" if score is not None else "FAILED"
                typer.echo(f"    {task:<30} {score_str}")
        typer.echo()

        if status == "PARTIAL":
            typer.echo("Warning: some tasks failed.", err=True)
            raise typer.Exit(1)
    else:
        typer.echo("Run completed but no aggregated result available.", err=True)
        raise typer.Exit(1)


# ---- Top-level: benchrouter run ----


@app.command("run")
def run(
    benchmark: str = typer.Argument(..., help="Registered benchmark name"),
    model: str = typer.Option(..., help="Model name"),
    tasks: Optional[str] = typer.Option(
        None, help="Comma-separated task names to run (default: all)"
    ),
    server: str = typer.Option(DEFAULT_SERVER, help="BenchRouter server URL"),
):
    """One-command evaluation: submit + wait + show results.

    Requires environment variables:
      BENCHROUTER_MODEL_ENDPOINT  Model API base URL
      OPENAI_API_KEY           API key (default: EMPTY)
    """
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT")
    if not model_endpoint:
        typer.echo(
            "Error: BENCHROUTER_MODEL_ENDPOINT environment variable is required",
            err=True,
        )
        raise typer.Exit(1)
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")

    payload = {
        "benchmark": benchmark,
        "model_endpoint": model_endpoint,
        "model_name": model,
        "api_key": api_key,
    }
    if tasks:
        payload["tasks"] = [t.strip() for t in tasks.split(",")]

    # Submit
    try:
        resp = requests.post(f"{server}/evals/submit", json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        typer.echo(f"Error submitting: {e}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    run_id = data["run_id"]
    task_names = data.get("tasks", [])
    typer.echo(
        f"Run {run_id} submitted ({len(task_names)} tasks: {', '.join(task_names)}). Waiting..."
    )

    _poll_run(server, run_id)


# ---- Benchmark commands ----


@benchmark_app.command("register")
def benchmark_register(
    name: str = typer.Option(..., help="Benchmark name"),
    path: str = typer.Option(..., help="Path to benchmark directory"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Package and upload a benchmark directory to the server."""
    if not os.path.isdir(path):
        typer.echo(f"Error: {path} is not a directory", err=True)
        raise typer.Exit(1)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    with tarfile.open(tmp.name, "w:gz") as tar:
        for entry in os.listdir(path):
            tar.add(os.path.join(path, entry), arcname=entry)
    tmp.close()

    try:
        with open(tmp.name, "rb") as f:
            resp = requests.post(
                f"{server}/benchmarks/{name}/upload",
                files={"file": (f"{name}.tar.gz", f, "application/gzip")},
                timeout=300,
            )
        resp.raise_for_status()
        typer.echo(f"Benchmark '{name}' registered.")
    except requests.exceptions.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    finally:
        os.unlink(tmp.name)


@benchmark_app.command("list")
def benchmark_list(server: str = typer.Option(DEFAULT_SERVER)):
    """List all registered benchmarks."""
    resp = requests.get(f"{server}/benchmarks", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    benchmarks = resp.json()
    if not benchmarks:
        typer.echo("  No benchmarks registered.")
        return
    for b in benchmarks:
        task_names = [t["name"] for t in b.get("tasks", [])]
        typer.echo(
            f"  {b['name']}  v{b.get('version', '-')}  "
            f"{b.get('description', '')}  tasks: {task_names}"
        )


@benchmark_app.command("delete")
def benchmark_delete(
    name: str = typer.Option(..., help="Benchmark name"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Delete a registered benchmark."""
    resp = requests.delete(f"{server}/benchmarks/{name}", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    typer.echo(f"Benchmark '{name}' deleted.")


# ---- Environment commands ----


@env_app.command("register")
def env_register(
    name: str = typer.Option(..., help="Environment name"),
    dockerfile: str = typer.Option(..., help="Path to Dockerfile"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Upload a Dockerfile; the server builds the image."""
    if not os.path.isfile(dockerfile):
        typer.echo(f"Error: {dockerfile} not found", err=True)
        raise typer.Exit(1)

    with open(dockerfile, "rb") as f:
        resp = requests.post(
            f"{server}/environments/{name}/register",
            files={"dockerfile": ("Dockerfile", f)},
            timeout=DEFAULT_TIMEOUT,
        )
    resp.raise_for_status()
    typer.echo(f"Environment '{name}' registered. Image is building on server.")


@env_app.command("list")
def env_list(server: str = typer.Option(DEFAULT_SERVER)):
    """List all environments."""
    resp = requests.get(f"{server}/environments", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    envs = resp.json()
    if not envs:
        typer.echo("  No environments registered.")
        return
    for e in envs:
        typer.echo(f"  {e['name']}  [{e['status']}]  {e['image_tag']}")


@env_app.command("status")
def env_status(
    name: str = typer.Option(..., help="Environment name"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Check environment build status."""
    resp = requests.get(f"{server}/environments/{name}", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    info = resp.json()
    typer.echo(f"  Name:   {info['name']}")
    typer.echo(f"  Image:  {info['image_tag']}")
    typer.echo(f"  Status: {info['status']}")


@env_app.command("delete")
def env_delete(
    name: str = typer.Option(..., help="Environment name"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Delete a registered environment."""
    resp = requests.delete(f"{server}/environments/{name}", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    typer.echo(f"Environment '{name}' deleted.")


# ---- Eval commands ----


@eval_app.command("submit")
def eval_submit(
    benchmark: str = typer.Option(..., help="Registered benchmark name"),
    model: str = typer.Option(..., help="Model name"),
    tasks: Optional[str] = typer.Option(None, help="Comma-separated task names"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Submit an evaluation run.

    Requires environment variables:
      BENCHROUTER_MODEL_ENDPOINT  OPENAI_API_KEY
    """
    model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT")
    if not model_endpoint:
        typer.echo(
            "Error: BENCHROUTER_MODEL_ENDPOINT environment variable is required",
            err=True,
        )
        raise typer.Exit(1)
    api_key = os.environ.get("OPENAI_API_KEY", "EMPTY")

    payload = {
        "benchmark": benchmark,
        "model_endpoint": model_endpoint,
        "model_name": model,
        "api_key": api_key,
    }
    if tasks:
        payload["tasks"] = [t.strip() for t in tasks.split(",")]

    try:
        resp = requests.post(f"{server}/evals/submit", json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        typer.echo(
            f"Run submitted: {result['run_id']}  "
            f"Tasks: {result.get('tasks', [])}  Status: {result['status']}"
        )
    except requests.exceptions.RequestException as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)


@eval_app.command("list")
def eval_list(server: str = typer.Option(DEFAULT_SERVER)):
    """List all runs."""
    resp = requests.get(f"{server}/runs", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    runs = resp.json()
    if not runs:
        typer.echo("  No runs.")
        return
    for r in runs:
        tasks_total = len(r.get("child_job_ids", []))
        typer.echo(
            f"  {r['run_id']}  {r['benchmark_name']}  {r['model_name']}  "
            f"[{r['status']}]  {tasks_total} tasks"
        )


@eval_app.command("status")
def eval_status(
    run_id: str = typer.Option(..., help="Run ID"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Check evaluation run status."""
    resp = requests.get(f"{server}/runs/{run_id}", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    r = resp.json()
    typer.echo(f"  Run ID:      {r['run_id']}")
    typer.echo(f"  Benchmark:   {r['benchmark_name']}")
    typer.echo(f"  Model:       {r['model_name']} @ {r['model_endpoint']}")
    typer.echo(f"  Status:      {r['status']}")
    child_jobs = r.get("child_jobs", [])
    if child_jobs:
        typer.echo(f"  Tasks:")
        for j in child_jobs:
            typer.echo(f"    {j['task_name']:<30} [{j['status']}]")
    if r.get("error"):
        typer.echo(f"  Error:       {r['error']}")


@eval_app.command("result")
def eval_result(
    run_id: str = typer.Option(..., help="Run ID"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Get evaluation results."""
    resp = requests.get(f"{server}/runs/{run_id}/result", timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    typer.echo(json.dumps(resp.json(), indent=2))


# ---- Compare command ----


@app.command("compare")
def compare(
    benchmark: str = typer.Argument(..., help="Benchmark name to compare"),
    models: Optional[str] = typer.Option(
        None, help="Comma-separated model names (default: all)"
    ),
    detail: bool = typer.Option(
        False, "--detail", "-d", help="Show per-task breakdown"
    ),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """Compare evaluation results across models for a benchmark."""
    params = {"benchmark": benchmark, "detail": str(detail).lower()}
    if models:
        params["models"] = models

    resp = requests.get(f"{server}/evals/compare", params=params, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    entries = resp.json()

    if not entries:
        typer.echo("No results found.")
        return

    if detail:
        # Show per-task breakdown
        typer.echo(f"\n  Benchmark: {benchmark} (per-task detail)")
        typer.echo(f"  {'Model':<30} {'Task':<25} {'Overall':>10}")
        typer.echo(f"  {'-'*30} {'-'*25} {'-'*10}")
        for e in sorted(entries, key=lambda x: (x.get("model", ""), x.get("task", ""))):
            model = e.get("model", "?")
            task = e.get("task", "(aggregate)")
            overall = e.get("overall", "N/A")
            if isinstance(overall, float):
                typer.echo(f"  {model:<30} {task:<25} {overall:>10.4f}")
            else:
                typer.echo(f"  {model:<30} {task:<25} {str(overall):>10}")
    else:
        # Benchmark-level comparison
        typer.echo(f"\n  Benchmark: {benchmark}")
        typer.echo(f"  {'Model':<40} {'Overall':>10}")
        typer.echo(f"  {'-'*40} {'-'*10}")
        for e in sorted(entries, key=lambda x: x.get("overall", 0) or 0, reverse=True):
            model = e.get("model", "?")
            overall = e.get("overall", "N/A")
            if isinstance(overall, float):
                typer.echo(f"  {model:<40} {overall:>10.4f}")
            else:
                typer.echo(f"  {model:<40} {str(overall):>10}")
    typer.echo()


# ---- Log commands ----


@app.command("logs")
def logs(
    job_id: str = typer.Argument(..., help="Job ID"),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Stream logs in real-time"
    ),
    tail: int = typer.Option(100, help="Number of recent log lines"),
    server: str = typer.Option(DEFAULT_SERVER),
):
    """View logs from a task job."""
    if follow:
        try:
            resp = requests.get(
                f"{server}/evals/{job_id}/logs",
                params={"follow": "true"},
                stream=True,
            )
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                if chunk:
                    typer.echo(chunk, nl=False)
        except KeyboardInterrupt:
            pass
        except requests.exceptions.RequestException as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)
    else:
        resp = requests.get(
            f"{server}/evals/{job_id}/logs",
            params={"tail": tail},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        typer.echo(data.get("logs", ""))


if __name__ == "__main__":
    app()
