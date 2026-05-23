import os
import json
import hashlib
import shutil
import tarfile
import logging
from typing import Optional, List

import yaml

logger = logging.getLogger(__name__)


class Registry:
    """File-based registry for benchmarks, environments, runs, and jobs."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.benchmarks_dir = os.path.join(data_dir, "benchmarks")
        self.environments_dir = os.path.join(data_dir, "environments")
        self.jobs_dir = os.path.join(data_dir, "jobs")
        self.runs_dir = os.path.join(data_dir, "runs")
        self.results_dir = os.path.join(data_dir, "results")
        self.image_cache_dir = os.path.join(data_dir, "image-cache")

        for d in [
            self.benchmarks_dir,
            self.environments_dir,
            self.jobs_dir,
            self.runs_dir,
            self.results_dir,
            self.image_cache_dir,
        ]:
            os.makedirs(d, exist_ok=True)

        self._recover_interrupted_swaps()
        self._reset_stale_building_envs()

    def _reset_stale_building_envs(self):
        """Reset environments stuck in 'building' status after a crash/restart."""
        if not os.path.isdir(self.environments_dir):
            return
        for name in os.listdir(self.environments_dir):
            env = self.get_environment(name)
            if env and env["status"] == "building":
                logger.info(f"Resetting stale 'building' environment: {name}")
                self.set_environment_status(name, "failed")

    def _recover_interrupted_swaps(self):
        """Recover from crashes during benchmark replacement.

        If a .bak dir exists without the main dir, the swap was interrupted —
        rename .bak back. Also clean up orphaned .bak and staging dirs.
        """
        if not os.path.isdir(self.benchmarks_dir):
            return
        for entry in os.listdir(self.benchmarks_dir):
            full = os.path.join(self.benchmarks_dir, entry)
            if entry.endswith(".bak") and os.path.isdir(full):
                original = full[:-4]  # strip .bak
                if not os.path.exists(original):
                    logger.info(f"Recovering interrupted benchmark swap: {entry} -> {entry[:-4]}")
                    os.rename(full, original)
                else:
                    shutil.rmtree(full, ignore_errors=True)
            elif entry.startswith(".") and os.path.isdir(full):
                # Orphaned staging dir from interrupted upload
                logger.info(f"Cleaning up orphaned staging dir: {entry}")
                shutil.rmtree(full, ignore_errors=True)

    # ---- Benchmarks ----

    @staticmethod
    def _validate_name(name: str):
        """Reject names that conflict with internal temp/backup patterns."""
        if not name or name.startswith(".") or name.endswith(".bak") or "/" in name or "\\" in name:
            raise ValueError(
                f"Invalid name '{name}': must not start with '.', end with '.bak', or contain path separators"
            )

    def register_benchmark(self, name: str, archive_path: str) -> dict:
        """Extract a tar.gz archive into benchmarks/{name}/ and validate structure.

        Extracts to a temp directory first so the old benchmark is preserved if
        validation fails. Also records the archive's SHA-256 digest under
        benchmarks/{name}/_archive.sha256 for provenance.
        """
        import tempfile

        self._validate_name(name)

        # Hash the source archive for provenance.
        archive_digest = hashlib.sha256()
        with open(archive_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                archive_digest.update(chunk)
        archive_sha256 = archive_digest.hexdigest()

        dest = os.path.join(self.benchmarks_dir, name)
        staging = tempfile.mkdtemp(dir=self.benchmarks_dir, prefix=f".{name}_tmp_")
        try:
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(staging, filter="data")

            # Validate benchmark.yaml exists
            manifest_path = os.path.join(staging, "benchmark.yaml")
            if not os.path.exists(manifest_path):
                raise FileNotFoundError(
                    f"benchmark.yaml not found in benchmark archive for '{name}'"
                )
            with open(manifest_path) as f:
                manifest = yaml.safe_load(f)

            # Validate tasks
            tasks = manifest.get("tasks")
            if not tasks:
                raise ValueError(
                    f"benchmark.yaml for '{name}' must have a 'tasks' list"
                )
            for task_def in tasks:
                task_name = task_def.get("name")
                if not task_name:
                    raise ValueError(f"Each task in benchmark '{name}' must have a 'name'")
                task_yaml = os.path.join(staging, task_name, "task.yaml")
                if not os.path.exists(task_yaml):
                    raise FileNotFoundError(
                        f"task.yaml not found for task '{task_name}' in benchmark '{name}'"
                    )

            # Validation passed — swap with backup so old version survives failures
            backup = None
            if os.path.exists(dest):
                backup = dest + ".bak"
                # Remove stale backup from a prior interrupted upload
                if os.path.exists(backup):
                    shutil.rmtree(backup, ignore_errors=True)
                os.rename(dest, backup)
            try:
                os.rename(staging, dest)
            except Exception:
                # Restore backup if rename failed
                if backup and os.path.exists(backup):
                    os.rename(backup, dest)
                raise
            if backup and os.path.exists(backup):
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # Record archive SHA-256 alongside the extracted benchmark.
        with open(os.path.join(dest, "_archive.sha256"), "w") as f:
            f.write(archive_sha256 + "\n")

        return manifest

    def get_benchmark_digest(self, name: str) -> Optional[str]:
        """Return the recorded archive SHA-256 for a benchmark, or None."""
        path = os.path.join(self.benchmarks_dir, name, "_archive.sha256")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read().strip() or None

    def snapshot_run_manifests(
        self, run_id: str, benchmark_name: str, task_names: List[str]
    ) -> Optional[str]:
        """Copy benchmark.yaml and per-task task.yaml files into the run dir.

        Returns the snapshot directory path so callers can record it on the
        run record. Silently no-ops if the source files are missing.
        """
        src_root = os.path.join(self.benchmarks_dir, benchmark_name)
        if not os.path.isdir(src_root):
            return None
        snapshot_dir = os.path.join(self.runs_dir, run_id, "manifests")
        os.makedirs(snapshot_dir, exist_ok=True)

        src_manifest = os.path.join(src_root, "benchmark.yaml")
        if os.path.exists(src_manifest):
            shutil.copy2(src_manifest, os.path.join(snapshot_dir, "benchmark.yaml"))

        for task_name in task_names:
            src_task = os.path.join(src_root, task_name, "task.yaml")
            if os.path.exists(src_task):
                task_dir = os.path.join(snapshot_dir, task_name)
                os.makedirs(task_dir, exist_ok=True)
                shutil.copy2(src_task, os.path.join(task_dir, "task.yaml"))

        return snapshot_dir

    def get_benchmark(self, name: str) -> Optional[dict]:
        manifest = os.path.join(self.benchmarks_dir, name, "benchmark.yaml")
        if not os.path.exists(manifest):
            return None
        with open(manifest) as f:
            return yaml.safe_load(f)

    def list_benchmarks(self) -> List[dict]:
        results = []
        if not os.path.isdir(self.benchmarks_dir):
            return results
        for name in sorted(os.listdir(self.benchmarks_dir)):
            if name.startswith(".") or name.endswith(".bak"):
                continue
            info = self.get_benchmark(name)
            if info:
                results.append(info)
        return results

    def delete_benchmark(self, name: str) -> bool:
        d = os.path.join(self.benchmarks_dir, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
            return True
        return False

    # ---- Tasks ----

    def get_task_config(self, benchmark_name: str, task_name: str) -> Optional[dict]:
        """Read task.yaml from benchmarks/{benchmark_name}/{task_name}/."""
        task_yaml = os.path.join(
            self.benchmarks_dir, benchmark_name, task_name, "task.yaml"
        )
        if not os.path.exists(task_yaml):
            return None
        with open(task_yaml) as f:
            return yaml.safe_load(f)

    def get_task_dir(self, benchmark_name: str, task_name: str) -> Optional[str]:
        d = os.path.join(self.benchmarks_dir, benchmark_name, task_name)
        return d if os.path.isdir(d) else None

    # ---- Environments ----

    def register_environment(self, name: str, dockerfile_content: bytes) -> dict:
        """Save Dockerfile and return metadata. Build happens separately."""
        self._validate_name(name)
        dest = os.path.join(self.environments_dir, name)
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "Dockerfile"), "wb") as f:
            f.write(dockerfile_content)
        image_tag = f"benchrouter-env-{name}:latest"
        meta = {
            "name": name,
            "image_tag": image_tag,
            "status": "building",
        }
        self._write_json(os.path.join(dest, "meta.json"), meta)
        return meta

    def set_environment_status(self, name: str, status: str):
        meta_path = os.path.join(self.environments_dir, name, "meta.json")
        if not os.path.exists(meta_path):
            return
        meta = self._read_json(meta_path)
        meta["status"] = status
        self._write_json(meta_path, meta)

    def get_environment(self, name: str) -> Optional[dict]:
        meta_path = os.path.join(self.environments_dir, name, "meta.json")
        if not os.path.exists(meta_path):
            return None
        return self._read_json(meta_path)

    def get_environment_dockerfile_dir(self, name: str) -> Optional[str]:
        d = os.path.join(self.environments_dir, name)
        return d if os.path.isdir(d) else None

    def list_environments(self) -> List[dict]:
        results = []
        if not os.path.isdir(self.environments_dir):
            return results
        for name in sorted(os.listdir(self.environments_dir)):
            info = self.get_environment(name)
            if info:
                results.append(info)
        return results

    def delete_environment(self, name: str) -> bool:
        d = os.path.join(self.environments_dir, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
            return True
        return False

    def get_image_cache_dir(self) -> str:
        return self.image_cache_dir

    # ---- Jobs ----

    def save_job(self, job: dict):
        job_dir = os.path.join(self.jobs_dir, job["job_id"])
        os.makedirs(job_dir, exist_ok=True)
        self._write_json(os.path.join(job_dir, "job.json"), job)

    def get_job(self, job_id: str) -> Optional[dict]:
        path = os.path.join(self.jobs_dir, job_id, "job.json")
        if not os.path.exists(path):
            return None
        return self._read_json(path)

    def list_jobs(self) -> List[dict]:
        results = []
        if not os.path.isdir(self.jobs_dir):
            return results
        for jid in sorted(os.listdir(self.jobs_dir)):
            j = self.get_job(jid)
            if j:
                results.append(j)
        return results

    def get_result_dir(self, job_id: str) -> str:
        d = os.path.join(self.results_dir, job_id)
        os.makedirs(d, exist_ok=True)
        return d

    def save_job_logs(self, job_id: str, logs: str):
        """Persist container logs to disk so they survive container removal."""
        job_dir = os.path.join(self.jobs_dir, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, "logs.txt"), "w") as f:
            f.write(logs)

    def get_job_log_path(self, job_id: str) -> Optional[str]:
        """Return path to persisted log file, or None if not saved."""
        path = os.path.join(self.jobs_dir, job_id, "logs.txt")
        return path if os.path.exists(path) else None

    # ---- Runs ----

    def save_run(self, run: dict):
        run_dir = os.path.join(self.runs_dir, run["run_id"])
        os.makedirs(run_dir, exist_ok=True)
        self._write_json(os.path.join(run_dir, "run.json"), run)

    def get_run(self, run_id: str) -> Optional[dict]:
        path = os.path.join(self.runs_dir, run_id, "run.json")
        if not os.path.exists(path):
            return None
        return self._read_json(path)

    def list_runs(self) -> List[dict]:
        results = []
        if not os.path.isdir(self.runs_dir):
            return results
        for rid in sorted(os.listdir(self.runs_dir)):
            r = self.get_run(rid)
            if r:
                results.append(r)
        return results

    # ---- Result Index ----

    def append_result_index(self, entry: dict):
        """Append a result entry to the append-only index file."""
        index_path = os.path.join(self.results_dir, "index.jsonl")
        with open(index_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_result_index(self) -> list:
        """Read all entries from the result index."""
        index_path = os.path.join(self.results_dir, "index.jsonl")
        if not os.path.exists(index_path):
            return []
        entries = []
        with open(index_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    # ---- Helpers ----

    def _read_json(self, path: str) -> dict:
        with open(path) as f:
            return json.load(f)

    def _write_json(self, path: str, data):
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
