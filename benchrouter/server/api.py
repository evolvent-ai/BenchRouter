import tempfile
import os
import logging
from fastapi import FastAPI, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse

from .scheduler import EvalScheduler
from .models import SubmitEvalRequest

app = FastAPI(title="BenchRouter API", version="0.4.0")
logger = logging.getLogger(__name__)

scheduler = None
DATA_DIR = os.environ.get("BENCHROUTER_DATA_DIR", "/data/benchrouter")


@app.on_event("startup")
async def startup_event():
    global scheduler
    scheduler = EvalScheduler(data_dir=DATA_DIR)
    logger.info(f"BenchRouter server started. Data dir: {DATA_DIR}")


# ======== Benchmarks ========


@app.post("/benchmarks/{name}/upload")
async def upload_benchmark(name: str, file: UploadFile = File(...)):
    """Upload a benchmark as a tar.gz archive."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    try:
        content = await file.read()
        tmp.write(content)
        tmp.close()
        result = scheduler.register_benchmark(name, tmp.name)
        return {"status": "registered", "benchmark": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        os.unlink(tmp.name)


@app.get("/benchmarks")
async def list_benchmarks():
    return scheduler.list_benchmarks()


@app.get("/benchmarks/{name}")
async def get_benchmark(name: str):
    result = scheduler.get_benchmark(name)
    if not result:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return result


@app.delete("/benchmarks/{name}")
async def delete_benchmark(name: str):
    ok = scheduler.delete_benchmark(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Benchmark not found")
    return {"status": "deleted"}


# ======== Environments ========


@app.post("/environments/{name}/register")
async def register_environment(name: str, dockerfile: UploadFile = File(...)):
    """Upload a Dockerfile; server builds the image asynchronously."""
    content = await dockerfile.read()
    result = scheduler.register_environment(name, content)
    return {"status": "building", "environment": result}


@app.get("/environments")
async def list_environments():
    return scheduler.list_environments()


@app.get("/environments/{name}")
async def get_environment(name: str):
    result = scheduler.get_environment(name)
    if not result:
        raise HTTPException(status_code=404, detail="Environment not found")
    return result


@app.delete("/environments/{name}")
async def delete_environment(name: str):
    ok = scheduler.delete_environment(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Environment not found")
    return {"status": "deleted"}


# ======== Runs (benchmark-level) ========


@app.post("/evals/submit")
async def submit_eval(request: SubmitEvalRequest):
    try:
        result = scheduler.submit_eval(
            benchmark=request.benchmark,
            model_endpoint=request.model_endpoint,
            model_name=request.model_name,
            api_key=request.api_key,
            tasks=request.tasks,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/runs")
async def list_runs():
    return scheduler.list_runs()


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = scheduler.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Enrich with child job statuses
    run["child_jobs"] = []
    for jid in run.get("child_job_ids", []):
        job = scheduler.get_job(jid)
        if job:
            run["child_jobs"].append(job)
    return run


@app.get("/runs/{run_id}/result")
async def get_run_result(run_id: str):
    run = scheduler.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not run.get("aggregated_result"):
        raise HTTPException(status_code=404, detail="Result not available yet")
    return run["aggregated_result"]


# ======== Eval Jobs (task-level) ========


@app.get("/evals")
async def list_evals():
    return scheduler.list_jobs()


@app.get("/evals/compare")
async def compare_results(
    benchmark: str = Query(None, description="Filter by benchmark name"),
    models: str = Query(None, description="Comma-separated model names to compare"),
    detail: bool = Query(False, description="Include per-task breakdown"),
):
    """Compare evaluation results across models for a benchmark."""
    index = scheduler.registry.read_result_index()
    if benchmark:
        index = [e for e in index if e.get("benchmark") == benchmark]
    if models:
        model_set = {m.strip() for m in models.split(",")}
        index = [e for e in index if e.get("model") in model_set]
    if not detail:
        # Only show benchmark-level aggregate entries (task=None)
        index = [e for e in index if e.get("task") is None]
    return index


@app.get("/evals/{job_id}")
async def get_eval(job_id: str):
    result = scheduler.get_job(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@app.get("/evals/{job_id}/result")
async def get_eval_result(job_id: str):
    result = scheduler.get_job_result(job_id)
    if not result:
        raise HTTPException(status_code=404, detail="Result not available yet")
    return result


@app.get("/evals/{job_id}/logs")
async def get_eval_logs(
    job_id: str,
    follow: bool = Query(False, description="Stream logs in real-time"),
    tail: int = Query(100, description="Number of recent log lines to return"),
):
    """Get logs from a running or completed evaluation job."""
    job = scheduler.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if follow and job["status"] == "RUNNING":
        return StreamingResponse(
            scheduler.stream_job_logs(job_id),
            media_type="text/plain",
        )

    logs = scheduler.get_job_logs(job_id, tail=tail)
    if logs is None:
        raise HTTPException(status_code=404, detail="Logs not available (container not found)")
    return {"job_id": job_id, "logs": logs}
