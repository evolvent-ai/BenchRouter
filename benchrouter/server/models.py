from pydantic import BaseModel
from typing import Optional, List
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"       # some tasks succeeded, some failed
    FAILED = "FAILED"


class EvalJobInfo(BaseModel):
    job_id: str
    run_id: str
    task_name: str
    benchmark_name: str
    env_name: str
    model_name: str
    model_endpoint: str
    status: JobStatus
    container_id: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[str] = None


class EvalRunInfo(BaseModel):
    run_id: str
    benchmark_name: str
    model_name: str
    model_endpoint: str
    status: RunStatus
    child_job_ids: List[str] = []
    task_filter: Optional[List[str]] = None
    aggregated_result: Optional[dict] = None
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


class SubmitEvalRequest(BaseModel):
    benchmark: str
    model_endpoint: str
    model_name: str
    api_key: str = "EMPTY"
    tasks: Optional[List[str]] = None
