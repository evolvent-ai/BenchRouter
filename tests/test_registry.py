"""Tests for benchrouter.server.registry — file-based storage."""

import os
import json
import tarfile
import tempfile
import shutil

import pytest
import yaml

from benchrouter.server.registry import Registry


@pytest.fixture
def tmp_data_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def registry(tmp_data_dir):
    return Registry(tmp_data_dir)


def _make_benchmark_archive(tmpdir, benchmark_yaml, tasks):
    """Create a tar.gz with benchmark.yaml + task subdirs."""
    src = os.path.join(tmpdir, "src")
    os.makedirs(src)

    with open(os.path.join(src, "benchmark.yaml"), "w") as f:
        yaml.dump(benchmark_yaml, f)

    for task_name, task_yaml_content in tasks.items():
        task_dir = os.path.join(src, task_name)
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.yaml"), "w") as f:
            yaml.dump(task_yaml_content, f)
        # Add a dummy script
        with open(os.path.join(task_dir, "eval.py"), "w") as f:
            f.write("print('hello')\n")

    archive_path = os.path.join(tmpdir, "bench.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        for entry in os.listdir(src):
            tar.add(os.path.join(src, entry), arcname=entry)
    return archive_path


class TestBenchmarkRegistration:
    def test_register_and_get(self, registry, tmp_data_dir):
        archive = _make_benchmark_archive(
            tmp_data_dir,
            benchmark_yaml={
                "name": "test-bench",
                "tasks": [{"name": "t1", "environment": "env1"}],
            },
            tasks={"t1": {"name": "t1", "command": "python eval.py"}},
        )
        result = registry.register_benchmark("test-bench", archive)
        assert result["name"] == "test-bench"
        assert len(result["tasks"]) == 1

        bm = registry.get_benchmark("test-bench")
        assert bm["name"] == "test-bench"

    def test_register_missing_benchmark_yaml(self, registry, tmp_data_dir):
        """Archive without benchmark.yaml should fail."""
        src = os.path.join(tmp_data_dir, "empty_src")
        os.makedirs(src)
        with open(os.path.join(src, "readme.txt"), "w") as f:
            f.write("no yaml here")
        archive = os.path.join(tmp_data_dir, "bad.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(os.path.join(src, "readme.txt"), arcname="readme.txt")

        with pytest.raises(FileNotFoundError, match="benchmark.yaml not found"):
            registry.register_benchmark("bad", archive)

    def test_register_missing_tasks_key(self, registry, tmp_data_dir):
        """benchmark.yaml without tasks key should fail."""
        src = os.path.join(tmp_data_dir, "notask_src")
        os.makedirs(src)
        with open(os.path.join(src, "benchmark.yaml"), "w") as f:
            yaml.dump({"name": "no-tasks"}, f)
        archive = os.path.join(tmp_data_dir, "notask.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(os.path.join(src, "benchmark.yaml"), arcname="benchmark.yaml")

        with pytest.raises(ValueError, match="must have a 'tasks' list"):
            registry.register_benchmark("no-tasks", archive)

    def test_register_missing_task_yaml(self, registry, tmp_data_dir):
        """Task subdirectory without task.yaml should fail."""
        src = os.path.join(tmp_data_dir, "missingtask_src")
        os.makedirs(src)
        with open(os.path.join(src, "benchmark.yaml"), "w") as f:
            yaml.dump({
                "name": "miss",
                "tasks": [{"name": "t1", "environment": "e1"}],
            }, f)
        task_dir = os.path.join(src, "t1")
        os.makedirs(task_dir)
        # No task.yaml in t1/

        archive = os.path.join(tmp_data_dir, "miss.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            for entry in os.listdir(src):
                tar.add(os.path.join(src, entry), arcname=entry)

        with pytest.raises(FileNotFoundError, match="task.yaml not found for task 't1'"):
            registry.register_benchmark("miss", archive)

    def test_list_and_delete(self, registry, tmp_data_dir):
        archive = _make_benchmark_archive(
            tmp_data_dir,
            benchmark_yaml={
                "name": "b1",
                "tasks": [{"name": "t1", "environment": "e1"}],
            },
            tasks={"t1": {"name": "t1", "command": "echo hi"}},
        )
        registry.register_benchmark("b1", archive)
        assert len(registry.list_benchmarks()) == 1

        registry.delete_benchmark("b1")
        assert len(registry.list_benchmarks()) == 0

    def test_get_nonexistent(self, registry):
        assert registry.get_benchmark("nope") is None


class TestTaskConfig:
    def test_get_task_config(self, registry, tmp_data_dir):
        archive = _make_benchmark_archive(
            tmp_data_dir,
            benchmark_yaml={
                "name": "bench",
                "tasks": [{"name": "mytask", "environment": "env"}],
            },
            tasks={"mytask": {
                "name": "mytask",
                "command": "python eval.py",
                "timeout": 600,
            }},
        )
        registry.register_benchmark("bench", archive)

        config = registry.get_task_config("bench", "mytask")
        assert config["name"] == "mytask"
        assert config["timeout"] == 600

    def test_get_task_config_nonexistent(self, registry):
        assert registry.get_task_config("nope", "nope") is None

    def test_get_task_dir(self, registry, tmp_data_dir):
        archive = _make_benchmark_archive(
            tmp_data_dir,
            benchmark_yaml={
                "name": "bench",
                "tasks": [{"name": "t1", "environment": "env"}],
            },
            tasks={"t1": {"name": "t1", "command": "echo"}},
        )
        registry.register_benchmark("bench", archive)
        d = registry.get_task_dir("bench", "t1")
        assert d is not None
        assert os.path.isdir(d)
        assert os.path.exists(os.path.join(d, "task.yaml"))


class TestRunPersistence:
    def test_save_and_get_run(self, registry):
        run = {"run_id": "r1", "benchmark_name": "b", "status": "QUEUED"}
        registry.save_run(run)
        loaded = registry.get_run("r1")
        assert loaded["run_id"] == "r1"
        assert loaded["status"] == "QUEUED"

    def test_list_runs(self, registry):
        registry.save_run({"run_id": "r1", "status": "COMPLETED"})
        registry.save_run({"run_id": "r2", "status": "RUNNING"})
        runs = registry.list_runs()
        assert len(runs) == 2

    def test_get_nonexistent_run(self, registry):
        assert registry.get_run("nope") is None


class TestResultIndex:
    def test_append_and_read(self, registry):
        registry.append_result_index({"benchmark": "b1", "task": None, "overall": 0.8})
        registry.append_result_index({"benchmark": "b1", "task": "t1", "overall": 0.9})
        entries = registry.read_result_index()
        assert len(entries) == 2
        assert entries[0]["task"] is None
        assert entries[1]["task"] == "t1"

    def test_read_empty(self, registry):
        assert registry.read_result_index() == []


class TestEnvironmentCRUD:
    def test_register_and_get(self, registry):
        meta = registry.register_environment("env1", b"FROM python:3.12\n")
        assert meta["status"] == "building"
        assert meta["image_tag"] == "benchrouter-env-env1:latest"

        loaded = registry.get_environment("env1")
        assert loaded["status"] == "building"

    def test_set_status(self, registry):
        registry.register_environment("env1", b"FROM python:3.12\n")
        registry.set_environment_status("env1", "ready")
        assert registry.get_environment("env1")["status"] == "ready"

    def test_delete(self, registry):
        registry.register_environment("env1", b"FROM python:3.12\n")
        assert registry.delete_environment("env1")
        assert registry.get_environment("env1") is None


class TestJobPersistence:
    def test_save_and_get(self, registry):
        job = {"job_id": "j1", "status": "QUEUED", "task_name": "t1"}
        registry.save_job(job)
        loaded = registry.get_job("j1")
        assert loaded["task_name"] == "t1"

    def test_list_jobs(self, registry):
        registry.save_job({"job_id": "j1", "status": "QUEUED"})
        registry.save_job({"job_id": "j2", "status": "RUNNING"})
        assert len(registry.list_jobs()) == 2
