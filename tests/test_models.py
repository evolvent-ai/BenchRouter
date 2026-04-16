"""Tests for benchrouter.server.models — data model validation."""

import pytest
from benchrouter.server.models import (
    JobStatus, RunStatus, EvalJobInfo, EvalRunInfo, SubmitEvalRequest,
)


class TestJobStatus:
    def test_values(self):
        assert set(JobStatus) == {
            JobStatus.QUEUED, JobStatus.RUNNING,
            JobStatus.COMPLETED, JobStatus.FAILED,
        }

    def test_no_timeout_status(self):
        """TIMEOUT was removed — only FAILED exists for error cases."""
        assert not hasattr(JobStatus, "TIMEOUT")


class TestRunStatus:
    def test_values(self):
        assert set(RunStatus) == {
            RunStatus.QUEUED, RunStatus.RUNNING,
            RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED,
        }


class TestEvalJobInfo:
    def test_required_fields(self):
        job = EvalJobInfo(
            job_id="abc",
            run_id="run1",
            task_name="fs",
            benchmark_name="mcpmark",
            env_name="mcpmark-fs",
            model_name="gpt-4",
            model_endpoint="http://localhost:8000/v1",
            status=JobStatus.QUEUED,
        )
        assert job.job_id == "abc"
        assert job.run_id == "run1"
        assert job.task_name == "fs"
        assert job.container_id is None
        assert job.error is None

    def test_run_id_is_required(self):
        with pytest.raises(Exception):
            EvalJobInfo(
                job_id="abc",
                # run_id missing
                task_name="fs",
                benchmark_name="mcpmark",
                env_name="mcpmark-fs",
                model_name="gpt-4",
                model_endpoint="http://localhost:8000/v1",
                status=JobStatus.QUEUED,
            )

    def test_task_name_is_required(self):
        with pytest.raises(Exception):
            EvalJobInfo(
                job_id="abc",
                run_id="run1",
                # task_name missing
                benchmark_name="mcpmark",
                env_name="mcpmark-fs",
                model_name="gpt-4",
                model_endpoint="http://localhost:8000/v1",
                status=JobStatus.QUEUED,
            )


class TestEvalRunInfo:
    def test_defaults(self):
        run = EvalRunInfo(
            run_id="r1",
            benchmark_name="mcpmark",
            model_name="gpt-4",
            model_endpoint="http://localhost:8000/v1",
            status=RunStatus.QUEUED,
        )
        assert run.child_job_ids == []
        assert run.task_filter is None
        assert run.aggregated_result is None
        assert run.completed_at is None

    def test_with_children(self):
        run = EvalRunInfo(
            run_id="r1",
            benchmark_name="mcpmark",
            model_name="gpt-4",
            model_endpoint="http://localhost:8000/v1",
            status=RunStatus.RUNNING,
            child_job_ids=["j1", "j2", "j3"],
            task_filter=["fs", "pg", "pw"],
        )
        assert len(run.child_job_ids) == 3


class TestSubmitEvalRequest:
    def test_no_environment_field(self):
        """environment field was removed in v0.4."""
        req = SubmitEvalRequest(
            benchmark="mcpmark",
            model_endpoint="http://localhost:8000/v1",
            model_name="gpt-4",
        )
        assert not hasattr(req, "environment") or "environment" not in req.model_fields

    def test_tasks_optional(self):
        req = SubmitEvalRequest(
            benchmark="mcpmark",
            model_endpoint="http://localhost:8000/v1",
            model_name="gpt-4",
        )
        assert req.tasks is None

    def test_tasks_filter(self):
        req = SubmitEvalRequest(
            benchmark="mcpmark",
            model_endpoint="http://localhost:8000/v1",
            model_name="gpt-4",
            tasks=["fs", "pg"],
        )
        assert req.tasks == ["fs", "pg"]
