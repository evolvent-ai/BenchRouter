"""Tests for benchrouter.server.scheduler — core orchestration logic.

These tests cover submit_eval validation, _aggregate_run_results logic,
DinD runtime detection, and the unified _run_job_container dispatch
without requiring Docker. Docker-dependent execution paths are not tested here.
"""

import os
import json
import tarfile
import tempfile
import shutil
from unittest.mock import patch, MagicMock

import pytest
import yaml

from benchrouter.server.models import JobStatus, RunStatus, EvalJobInfo, EvalRunInfo
from benchrouter.server.scheduler import (
    EvalScheduler,
    DEFAULT_MEM_LIMIT,
    DIND_MEM_LIMIT,
    DEFAULT_CPU_PERIOD,
    DEFAULT_CPU_QUOTA,
)


@pytest.fixture
def tmp_data_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _register_benchmark(scheduler, tmp_dir, name, tasks_config):
    """Helper: create and register a benchmark archive."""
    src = os.path.join(tmp_dir, f"src_{name}")
    os.makedirs(src)

    benchmark_yaml = {
        "name": name,
        "tasks": [{"name": t, "environment": f"{name}-{t}"} for t in tasks_config],
    }
    with open(os.path.join(src, "benchmark.yaml"), "w") as f:
        yaml.dump(benchmark_yaml, f)

    for task_name, task_cfg in tasks_config.items():
        task_dir = os.path.join(src, task_name)
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.yaml"), "w") as f:
            yaml.dump(task_cfg, f)

    archive = os.path.join(tmp_dir, f"{name}.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for entry in os.listdir(src):
            tar.add(os.path.join(src, entry), arcname=entry)

    scheduler.register_benchmark(name, archive)


def _register_env(scheduler, name):
    """Helper: register a fake environment and set it to ready."""
    scheduler.registry.register_environment(name, b"FROM python:3.12\n")
    scheduler.registry.set_environment_status(name, "ready")


class TestSubmitEvalValidation:
    """Test submit_eval without actually running Docker containers."""

    def test_benchmark_not_registered(self, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        with pytest.raises(ValueError, match="not registered"):
            sched.submit_eval("nonexistent", "http://x/v1", "model")

    def test_no_tasks_defined(self, tmp_data_dir):
        """A benchmark.yaml with empty tasks list should fail at registration."""
        sched = EvalScheduler(data_dir=tmp_data_dir)
        src = os.path.join(tmp_data_dir, "empty_src")
        os.makedirs(src)
        with open(os.path.join(src, "benchmark.yaml"), "w") as f:
            yaml.dump({"name": "empty", "tasks": []}, f)
        archive = os.path.join(tmp_data_dir, "empty.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(os.path.join(src, "benchmark.yaml"), arcname="benchmark.yaml")
        with pytest.raises(ValueError, match="must have a 'tasks' list"):
            sched.register_benchmark("empty", archive)

    def test_task_filter_no_match(self, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo"},
        })
        _register_env(sched, "bench-t1")
        with pytest.raises(ValueError, match="Unknown task"):
            sched.submit_eval("bench", "http://x/v1", "model", tasks=["nonexistent"])

    @patch.object(EvalScheduler, "_run_benchmark")
    def test_successful_submit_creates_run_and_jobs(self, mock_run, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo 1"},
            "t2": {"name": "t2", "command": "echo 2"},
        })
        _register_env(sched, "bench-t1")
        _register_env(sched, "bench-t2")

        result = sched.submit_eval("bench", "http://x/v1", "model")

        assert "run_id" in result
        assert len(result["job_ids"]) == 2
        assert set(result["tasks"]) == {"t1", "t2"}
        assert result["status"] == "QUEUED"

        # Check run exists
        run = sched.get_run(result["run_id"])
        assert run is not None
        assert run["benchmark_name"] == "bench"
        assert len(run["child_job_ids"]) == 2

        # Check jobs exist
        for jid in result["job_ids"]:
            job = sched.get_job(jid)
            assert job is not None
            assert job["run_id"] == result["run_id"]
            assert job["benchmark_name"] == "bench"
            assert job["task_name"] in ("t1", "t2")

    @patch.object(EvalScheduler, "_run_benchmark")
    def test_submit_with_task_filter(self, mock_run, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo 1"},
            "t2": {"name": "t2", "command": "echo 2"},
            "t3": {"name": "t3", "command": "echo 3"},
        })
        _register_env(sched, "bench-t1")
        _register_env(sched, "bench-t2")
        _register_env(sched, "bench-t3")

        result = sched.submit_eval(
            "bench", "http://x/v1", "model", tasks=["t1", "t3"]
        )
        assert len(result["job_ids"]) == 2
        assert set(result["tasks"]) == {"t1", "t3"}

    def test_missing_model_endpoint(self, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo"},
        })
        _register_env(sched, "bench-t1")
        with pytest.raises(ValueError, match="model_endpoint is required"):
            sched.submit_eval("bench", "", "model")


class TestAggregateRunResults:
    """Test _aggregate_run_results with manually crafted job states."""

    def _setup(self, tmp_data_dir, task_names):
        """Create scheduler with fake run and jobs."""
        sched = EvalScheduler(data_dir=tmp_data_dir)
        run_id = "testrun"
        child_ids = []

        for i, tn in enumerate(task_names):
            jid = f"job{i}"
            child_ids.append(jid)
            job = EvalJobInfo(
                job_id=jid,
                run_id=run_id,
                task_name=tn,
                benchmark_name="bench",
                env_name="env",
                model_name="model",
                model_endpoint="http://x/v1",
                status=JobStatus.QUEUED,
            )
            sched.jobs[jid] = job

        run = EvalRunInfo(
            run_id=run_id,
            benchmark_name="bench",
            model_name="model",
            model_endpoint="http://x/v1",
            status=RunStatus.RUNNING,
            child_job_ids=child_ids,
        )
        sched.runs[run_id] = run
        return sched, run_id

    def test_all_completed(self, tmp_data_dir):
        sched, run_id = self._setup(tmp_data_dir, ["t1", "t2"])

        # Simulate both tasks completing with results
        for jid, score in [("job0", 0.8), ("job1", 0.6)]:
            sched.jobs[jid].status = JobStatus.COMPLETED
            result_dir = sched.registry.get_result_dir(jid)
            with open(os.path.join(result_dir, "result.json"), "w") as f:
                json.dump({"overall": score}, f)

        sched._aggregate_run_results(run_id)

        run = sched.runs[run_id]
        assert run.status == RunStatus.COMPLETED
        assert run.aggregated_result["overall"] == 0.7
        assert run.aggregated_result["tasks_completed"] == 2
        assert run.aggregated_result["tasks_total"] == 2
        assert run.aggregated_result["task_scores"] == {"t1": 0.8, "t2": 0.6}

    def test_partial_failure(self, tmp_data_dir):
        sched, run_id = self._setup(tmp_data_dir, ["t1", "t2"])

        # t1 succeeds, t2 fails
        sched.jobs["job0"].status = JobStatus.COMPLETED
        result_dir = sched.registry.get_result_dir("job0")
        with open(os.path.join(result_dir, "result.json"), "w") as f:
            json.dump({"overall": 0.9}, f)

        sched.jobs["job1"].status = JobStatus.FAILED
        sched.jobs["job1"].error = "container died"

        sched._aggregate_run_results(run_id)

        run = sched.runs[run_id]
        assert run.status == RunStatus.PARTIAL
        assert run.aggregated_result["overall"] == 0.9
        assert run.aggregated_result["tasks_completed"] == 1
        assert run.aggregated_result["task_scores"]["t1"] == 0.9
        assert run.aggregated_result["task_scores"]["t2"] is None

    def test_all_failed(self, tmp_data_dir):
        sched, run_id = self._setup(tmp_data_dir, ["t1", "t2"])

        sched.jobs["job0"].status = JobStatus.FAILED
        sched.jobs["job1"].status = JobStatus.FAILED

        sched._aggregate_run_results(run_id)

        run = sched.runs[run_id]
        assert run.status == RunStatus.FAILED
        assert run.aggregated_result["overall"] is None
        assert run.aggregated_result["tasks_completed"] == 0

    def test_result_index_written(self, tmp_data_dir):
        sched, run_id = self._setup(tmp_data_dir, ["t1"])

        sched.jobs["job0"].status = JobStatus.COMPLETED
        result_dir = sched.registry.get_result_dir("job0")
        with open(os.path.join(result_dir, "result.json"), "w") as f:
            json.dump({"overall": 1.0}, f)

        sched._aggregate_run_results(run_id)

        index = sched.registry.read_result_index()
        # Should have the benchmark-level aggregate entry
        agg = [e for e in index if e.get("task") is None]
        assert len(agg) == 1
        assert agg[0]["benchmark"] == "bench"
        assert agg[0]["overall"] == 1.0


class TestTaskConfigLoading:
    def test_load_task_config(self, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo", "timeout": 999},
        })
        config = sched._load_task_config("bench", "t1")
        assert config["timeout"] == 999

    def test_get_task_timeout(self, tmp_data_dir):
        sched = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo", "timeout": 123},
            "t2": {"name": "t2", "command": "echo"},  # no timeout
        })
        assert sched._get_task_timeout("bench", "t1") == 123
        assert sched._get_task_timeout("bench", "t2") == 7200  # DEFAULT_TIMEOUT


class TestRunPersistence:
    @patch.object(EvalScheduler, "_run_benchmark")
    def test_runs_survive_restart(self, mock_run, tmp_data_dir):
        """Runs and jobs should be reloaded from disk on scheduler init."""
        sched1 = EvalScheduler(data_dir=tmp_data_dir)
        _register_benchmark(sched1, tmp_data_dir, "bench", {
            "t1": {"name": "t1", "command": "echo"},
        })
        _register_env(sched1, "bench-t1")

        result = sched1.submit_eval("bench", "http://x/v1", "model")
        run_id = result["run_id"]

        # Create a new scheduler instance (simulates restart)
        sched2 = EvalScheduler(data_dir=tmp_data_dir)
        assert sched2.get_run(run_id) is not None
        for jid in result["job_ids"]:
            assert sched2.get_job(jid) is not None


class TestDindRuntime:
    """Test DinD runtime detection and _run_job dispatch."""

    def test_detect_sysbox_runtime(self, tmp_data_dir):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"io.containerd.runc.v2":{},"sysbox-runc":{}}',
                returncode=0,
            )
            sched = EvalScheduler(data_dir=tmp_data_dir)
            assert sched._dind_runtime == "sysbox-runc"

    def test_detect_privileged_fallback(self, tmp_data_dir):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout='{"io.containerd.runc.v2":{}}',
                returncode=0,
            )
            sched = EvalScheduler(data_dir=tmp_data_dir)
            assert sched._dind_runtime == "privileged"

    def test_detect_runtime_on_error(self, tmp_data_dir):
        with patch("subprocess.run", side_effect=Exception("no docker")):
            sched = EvalScheduler(data_dir=tmp_data_dir)
            assert sched._dind_runtime == "privileged"


class TestRunJobContainer:
    """Test _run_job_container parameters for DinD vs simple mode."""

    def _make_scheduler(self, tmp_data_dir, dind_runtime="privileged"):
        with patch("subprocess.run") as mock_sr:
            mock_sr.return_value = MagicMock(stdout="{}", returncode=0)
            sched = EvalScheduler(data_dir=tmp_data_dir)
        sched._dind_runtime = dind_runtime
        sched.docker_client = MagicMock()
        return sched

    def _make_job(self, sched, tmp_data_dir, task_cfg):
        _register_benchmark(sched, tmp_data_dir, "bench", {
            "t1": task_cfg,
        })
        _register_env(sched, "bench-t1")

        job = EvalJobInfo(
            job_id="testjob",
            run_id="testrun",
            task_name="t1",
            benchmark_name="bench",
            env_name="bench-t1",
            model_name="model",
            model_endpoint="http://x/v1",
            status=JobStatus.QUEUED,
        )
        sched.jobs["testjob"] = job
        return job

    def test_simple_mode_uses_host_network(self, tmp_data_dir):
        """Non-DinD tasks should use network_mode=host."""
        sched = self._make_scheduler(tmp_data_dir)
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo",
        })

        mock_container = MagicMock()
        mock_container.id = "fake-container-id"
        mock_container.wait.return_value = {"StatusCode": 0}
        sched.docker_client.containers.run.return_value = mock_container

        sched._run_job_container("testjob", "key", {"name": "t1", "command": "echo"})

        call_kwargs = sched.docker_client.containers.run.call_args
        assert call_kwargs.kwargs.get("network_mode") == "host"
        assert call_kwargs.kwargs.get("mem_limit") == DEFAULT_MEM_LIMIT
        assert call_kwargs.kwargs.get("command") == "python -m benchrouter.sdk.runner"
        assert "privileged" not in call_kwargs.kwargs
        assert "runtime" not in call_kwargs.kwargs

    def test_dind_mode_privileged(self, tmp_data_dir):
        """DinD tasks should use privileged=True when sysbox is not available."""
        sched = self._make_scheduler(tmp_data_dir, dind_runtime="privileged")
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo", "docker": True,
        })

        mock_container = MagicMock()
        mock_container.id = "fake-container-id"
        mock_container.wait.return_value = {"StatusCode": 0}
        sched.docker_client.containers.run.return_value = mock_container

        sched._run_job_container("testjob", "key", {
            "name": "t1", "command": "echo", "docker": True,
        })

        call_kwargs = sched.docker_client.containers.run.call_args
        assert call_kwargs.kwargs.get("privileged") is True
        assert call_kwargs.kwargs.get("mem_limit") == DIND_MEM_LIMIT
        assert call_kwargs.kwargs.get("command") == "/app/benchrouter/sdk/dind-entrypoint.sh"
        assert "network_mode" not in call_kwargs.kwargs

    def test_dind_mode_sysbox(self, tmp_data_dir):
        """DinD tasks should use runtime=sysbox-runc when available."""
        sched = self._make_scheduler(tmp_data_dir, dind_runtime="sysbox-runc")
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo", "docker": True,
        })

        mock_container = MagicMock()
        mock_container.id = "fake-container-id"
        mock_container.wait.return_value = {"StatusCode": 0}
        sched.docker_client.containers.run.return_value = mock_container

        sched._run_job_container("testjob", "key", {
            "name": "t1", "command": "echo", "docker": True,
        })

        call_kwargs = sched.docker_client.containers.run.call_args
        assert call_kwargs.kwargs.get("runtime") == "sysbox-runc"
        assert "privileged" not in call_kwargs.kwargs
        assert call_kwargs.kwargs.get("mem_limit") == DIND_MEM_LIMIT

    def test_dind_mode_mounts_image_cache(self, tmp_data_dir):
        """DinD tasks should mount image-cache directory if it exists."""
        sched = self._make_scheduler(tmp_data_dir)
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo", "docker": True,
        })

        task_config = {"name": "t1", "command": "echo", "docker": True}
        job = sched.jobs["testjob"]
        volumes = sched._build_volumes(job, task_config)

        image_cache_dir = os.path.abspath(sched.registry.get_image_cache_dir())
        assert image_cache_dir in volumes
        assert volumes[image_cache_dir]["bind"] == "/var/lib/docker-cache"
        assert volumes[image_cache_dir]["mode"] == "ro"

    def test_simple_mode_no_image_cache(self, tmp_data_dir):
        """Non-DinD tasks should NOT mount image-cache."""
        sched = self._make_scheduler(tmp_data_dir)
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo",
        })

        task_config = {"name": "t1", "command": "echo"}
        job = sched.jobs["testjob"]
        volumes = sched._build_volumes(job, task_config)

        image_cache_dir = os.path.abspath(sched.registry.get_image_cache_dir())
        assert image_cache_dir not in volumes

    def test_dind_env_var_set(self, tmp_data_dir):
        """DinD tasks should set BENCHROUTER_DOCKER_ENABLED=true."""
        sched = self._make_scheduler(tmp_data_dir)
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo", "docker": True,
        })

        task_config = {"name": "t1", "command": "echo", "docker": True}
        job = sched.jobs["testjob"]
        env_vars = sched._build_environment_vars(job, "key", task_config)
        assert env_vars["BENCHROUTER_DOCKER_ENABLED"] == "true"

    def test_simple_env_var_not_set(self, tmp_data_dir):
        """Non-DinD tasks should NOT set BENCHROUTER_DOCKER_ENABLED."""
        sched = self._make_scheduler(tmp_data_dir)
        self._make_job(sched, tmp_data_dir, {
            "name": "t1", "command": "echo",
        })

        task_config = {"name": "t1", "command": "echo"}
        job = sched.jobs["testjob"]
        env_vars = sched._build_environment_vars(job, "key", task_config)
        assert "BENCHROUTER_DOCKER_ENABLED" not in env_vars


