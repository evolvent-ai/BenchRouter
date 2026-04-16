import logging
import uuid
import threading
import subprocess
import shutil
import docker
import os
import json
from datetime import datetime, timezone
from typing import Dict, Optional, List

import yaml

from .models import JobStatus, RunStatus, EvalJobInfo, EvalRunInfo
from .registry import Registry

logger = logging.getLogger(__name__)

# Defaults for container resource limits
DEFAULT_TIMEOUT = 7200  # 2 hours
DEFAULT_MEM_LIMIT = "16g"
DIND_MEM_LIMIT = "32g"
DEFAULT_CPU_QUOTA = 400000  # 4 cores (with cpu_period=100000)
DEFAULT_CPU_PERIOD = 100000
DEFAULT_MAX_CONCURRENT = 8


class EvalScheduler:
    """
    Orchestrates evaluation jobs with benchmark→task hierarchy.
    - A benchmark contains one or more tasks (declared in benchmark.yaml).
    - Each task has its own task.yaml, environment, and Docker execution.
    - Running a benchmark creates a Run (parent) with N Jobs (one per task).
    - Tasks execute in parallel, results are aggregated.
    """

    def __init__(self, data_dir: str = "/data/benchrouter", max_concurrent: int = DEFAULT_MAX_CONCURRENT):
        self.registry = Registry(data_dir)
        self.jobs: Dict[str, EvalJobInfo] = {}
        self.runs: Dict[str, EvalRunInfo] = {}
        self._semaphore = threading.Semaphore(max_concurrent)
        self._env_build_locks: Dict[str, threading.Lock] = {}
        self._env_build_locks_lock = threading.Lock()

        # Reload persisted jobs
        for j in self.registry.list_jobs():
            self.jobs[j["job_id"]] = EvalJobInfo(**j)

        # Reload persisted runs
        for r in self.registry.list_runs():
            self.runs[r["run_id"]] = EvalRunInfo(**r)

        try:
            self.docker_client = docker.from_env(timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            self.docker_client = None

        self._dind_runtime = self._detect_dind_runtime()

    def _detect_dind_runtime(self) -> str:
        """Check if sysbox-runc is available, fallback to privileged."""
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{json .Runtimes}}"],
                capture_output=True, text=True, timeout=5,
            )
            if "sysbox-runc" in result.stdout:
                return "sysbox-runc"
        except Exception:
            pass
        return "privileged"

    # ---- Benchmark registration ----

    def register_benchmark(self, name: str, archive_path: str) -> dict:
        return self.registry.register_benchmark(name, archive_path)

    def list_benchmarks(self) -> list:
        return self.registry.list_benchmarks()

    def get_benchmark(self, name: str) -> Optional[dict]:
        return self.registry.get_benchmark(name)

    def delete_benchmark(self, name: str) -> bool:
        return self.registry.delete_benchmark(name)

    # ---- Environment registration ----

    def register_environment(self, name: str, dockerfile_content: bytes) -> dict:
        lock = self._get_env_build_lock(name)
        with lock:
            meta = self.registry.register_environment(name, dockerfile_content)
            # Start build thread while still holding lock so delete can't slip in
            threading.Thread(
                target=self._safe_build_environment, args=(name,), daemon=True
            ).start()
        return meta

    def _safe_build_environment(self, name: str):
        """Build environment, acquiring the per-env lock to serialize builds."""
        lock = self._get_env_build_lock(name)
        with lock:
            # Re-check: if another thread already built it, skip
            env = self.registry.get_environment(name)
            if env and env["status"] == "ready":
                return
            self._build_environment(name)

    def _build_environment(self, name: str):
        """Build Docker image from the stored Dockerfile."""
        build_dir = self.registry.get_environment_dockerfile_dir(name)
        meta = self.registry.get_environment(name)
        image_tag = meta["image_tag"]
        self.registry.set_environment_status(name, "building")
        try:
            logger.info(f"Building environment image {image_tag} from {build_dir}")
            self.docker_client.images.build(path=build_dir, tag=image_tag, rm=True)
            try:
                self._export_environment_image_cache(image_tag)
            except Exception as export_error:
                logger.warning(
                    f"Failed to export environment image cache for {name}: {export_error}"
                )
            self.registry.set_environment_status(name, "ready")
            logger.info(f"Environment {name} built successfully: {image_tag}")
        except Exception as e:
            logger.error(f"Failed to build environment {name}: {e}")
            self.registry.set_environment_status(name, "failed")

    def _export_environment_image_cache(self, image_tag: str):
        """Export built environment image as a tar for DinD tasks to preload.

        Filename uses the same convention as dind-entrypoint.sh: replace / and : with _
        so that the entrypoint's selective-load matching works correctly.
        """
        image_cache_dir = self.registry.get_image_cache_dir()
        os.makedirs(image_cache_dir, exist_ok=True)

        # e.g. "benchrouter-env-toolathlon-local:latest" → "benchrouter-env-toolathlon-local_latest"
        cache_filename = image_tag.replace("/", "_").replace(":", "_") + ".tar"
        cache_path = os.path.join(image_cache_dir, cache_filename)
        # Always re-export; os.replace below does an atomic swap
        temp_path = f"{cache_path}.tmp"
        log_interval_bytes = 512 * 1024 * 1024
        bytes_written = 0
        next_log_at = log_interval_bytes

        try:
            image_stream = self.docker_client.api.get_image(image_tag)
            with open(temp_path, "wb") as tar_file:
                for chunk in image_stream:
                    tar_file.write(chunk)
                    bytes_written += len(chunk)
                    if bytes_written >= next_log_at:
                        logger.info(
                            "Exporting image cache for %s: %.1f MiB written",
                            image_tag,
                            bytes_written / (1024 * 1024),
                        )
                        next_log_at += log_interval_bytes
            os.replace(temp_path, cache_path)
            logger.info(
                "Exported environment image cache for %s to %s (%.1f MiB)",
                image_tag,
                cache_path,
                bytes_written / (1024 * 1024),
            )
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def list_environments(self) -> list:
        return self.registry.list_environments()

    def get_environment(self, name: str) -> Optional[dict]:
        return self.registry.get_environment(name)

    def delete_environment(self, name: str) -> bool:
        lock = self._get_env_build_lock(name)
        with lock:
            return self.registry.delete_environment(name)

    # ---- Eval submission ----

    def submit_eval(
        self,
        benchmark: str,
        model_endpoint: str,
        model_name: str,
        api_key: str = "EMPTY",
        tasks: Optional[List[str]] = None,
    ) -> dict:
        """Submit a benchmark run. Creates a Run with N parallel task Jobs."""
        bm = self.registry.get_benchmark(benchmark)
        if not bm:
            raise ValueError(f"Benchmark '{benchmark}' not registered")

        if not model_endpoint:
            raise ValueError("model_endpoint is required (set BENCHROUTER_MODEL_ENDPOINT)")

        all_tasks = bm.get("tasks", [])
        if not all_tasks:
            raise ValueError(f"Benchmark '{benchmark}' has no tasks defined")

        # Apply task filter if specified
        if tasks:
            task_set = set(tasks)
            available_names = {t["name"] for t in all_tasks}
            unknown = task_set - available_names
            if unknown:
                raise ValueError(
                    f"Unknown task(s) {sorted(unknown)}. Available: {sorted(available_names)}"
                )
            all_tasks = [t for t in all_tasks if t["name"] in task_set]

        now = datetime.now(timezone.utc).isoformat()
        run_id = str(uuid.uuid4())[:16]

        # Create child jobs
        child_job_ids = []
        for task_def in all_tasks:
            job_id = str(uuid.uuid4())[:16]
            child_job_ids.append(job_id)
            env_name = task_def.get("environment", benchmark)

            job = EvalJobInfo(
                job_id=job_id,
                run_id=run_id,
                task_name=task_def["name"],
                benchmark_name=benchmark,
                env_name=env_name,
                model_name=model_name,
                model_endpoint=model_endpoint,
                status=JobStatus.QUEUED,
                created_at=now,
            )
            self.jobs[job_id] = job
            self.registry.save_job(job.model_dump())

        # Create parent run
        run = EvalRunInfo(
            run_id=run_id,
            benchmark_name=benchmark,
            model_name=model_name,
            model_endpoint=model_endpoint,
            status=RunStatus.QUEUED,
            child_job_ids=child_job_ids,
            task_filter=[t["name"] for t in all_tasks],
            created_at=now,
        )
        self.runs[run_id] = run
        self.registry.save_run(run.model_dump())

        # Launch parallel execution
        threading.Thread(
            target=self._run_benchmark, args=(run_id, api_key), daemon=True
        ).start()

        return {
            "run_id": run_id,
            "job_ids": child_job_ids,
            "tasks": [t["name"] for t in all_tasks],
            "status": "QUEUED",
        }

    # ---- Benchmark execution (parallel tasks) ----

    def _run_benchmark(self, run_id: str, api_key: str):
        """Run all tasks in a benchmark in parallel, then aggregate results."""
        run = self.runs[run_id]
        run.status = RunStatus.RUNNING
        self.registry.save_run(run.model_dump())

        threads = []
        for job_id in run.child_job_ids:
            t = threading.Thread(
                target=self._run_job, args=(job_id, api_key), daemon=True
            )
            threads.append(t)
            t.start()

        # Wait for all child threads to complete
        for t in threads:
            t.join()

        # Aggregate results
        self._aggregate_run_results(run_id)

    def _aggregate_run_results(self, run_id: str):
        """Compute benchmark-level results from individual task results."""
        run = self.runs[run_id]
        task_scores = {}
        any_failed = False

        for job_id in run.child_job_ids:
            job = self.jobs[job_id]
            if job.status == JobStatus.COMPLETED:
                result = self.get_job_result(job_id)
                if result:
                    task_scores[job.task_name] = result.get("overall")
                else:
                    task_scores[job.task_name] = None
                    any_failed = True
            else:
                any_failed = True
                task_scores[job.task_name] = None

        # Determine run status
        valid_scores = [v for v in task_scores.values() if v is not None]
        if not valid_scores:
            run.status = RunStatus.FAILED
        elif any_failed:
            run.status = RunStatus.PARTIAL
        else:
            run.status = RunStatus.COMPLETED

        aggregate_overall = (
            round(sum(valid_scores) / len(valid_scores), 4) if valid_scores else None
        )

        run.aggregated_result = {
            "benchmark_name": run.benchmark_name,
            "model_name": run.model_name,
            "overall": aggregate_overall,
            "task_scores": task_scores,
            "tasks_completed": len(valid_scores),
            "tasks_total": len(run.child_job_ids),
        }
        run.completed_at = datetime.now(timezone.utc).isoformat()
        self.registry.save_run(run.model_dump())

        # Write benchmark-level entry to result index
        self.registry.append_result_index({
            "run_id": run_id,
            "benchmark": run.benchmark_name,
            "task": None,
            "model": run.model_name,
            "overall": aggregate_overall,
            "task_scores": task_scores,
            "completed_at": run.completed_at,
        })

    # ---- Single task job execution ----

    def _get_task_timeout(self, benchmark_name: str, task_name: str) -> int:
        """Read timeout from task.yaml, fallback to default."""
        config = self.registry.get_task_config(benchmark_name, task_name)
        if config and isinstance(config.get("timeout"), (int, float)):
            return int(config["timeout"])
        return DEFAULT_TIMEOUT

    def _load_task_config(self, benchmark_name: str, task_name: str) -> dict:
        """Load full task.yaml config."""
        return self.registry.get_task_config(benchmark_name, task_name) or {}

    def _get_env_build_lock(self, env_name: str) -> threading.Lock:
        with self._env_build_locks_lock:
            if env_name not in self._env_build_locks:
                self._env_build_locks[env_name] = threading.Lock()
            return self._env_build_locks[env_name]

    def _wait_for_env_building(self, env_name: str, max_wait: int = 360):
        """Poll until environment leaves 'building' status. No locks held."""
        import time as _time
        for _ in range(max_wait // 2):
            _time.sleep(2)
            env = self.registry.get_environment(env_name)
            if not env or env["status"] != "building":
                return env
        return self.registry.get_environment(env_name)

    def _ensure_environment(self, job):
        """Auto-build Docker image from benchmark's environments/ dir if env not ready."""
        env_name = job.env_name
        need_build = False

        # Fast path: already ready
        env = self.registry.get_environment(env_name)
        if env and env["status"] == "ready":
            return

        # If building, wait without holding any lock (avoids deadlock with API builder)
        if env and env["status"] == "building":
            logger.info(f"Environment '{env_name}' is building, waiting...")
            env = self._wait_for_env_building(env_name)
            if env and env["status"] == "ready":
                return
            if env and env["status"] == "building":
                raise RuntimeError(f"Timed out waiting for environment '{env_name}' to build")

        # Acquire per-env lock — serializes all builds (auto-build and API) for this env.
        # Threads that see "building" status wait OUTSIDE this lock (above), so no deadlock.
        lock = self._get_env_build_lock(env_name)
        with lock:
            env = self.registry.get_environment(env_name)
            if env and env["status"] == "ready":
                return
            if env and env["status"] == "building":
                # Another thread is building but hasn't finished (e.g. API build in progress).
                # Release lock and wait outside.
                pass
            else:
                # Not exists or failed — set up and build while holding lock
                # Check registered environment dir first (from API), then benchmark-local
                registered_dir = self.registry.get_environment_dockerfile_dir(env_name)
                env_dir = os.path.join(
                    self.registry.benchmarks_dir, job.benchmark_name, "environments", env_name
                )
                if registered_dir and os.path.exists(os.path.join(registered_dir, "Dockerfile")):
                    # Already registered via API — just rebuild
                    pass
                elif os.path.exists(os.path.join(env_dir, "Dockerfile")):
                    # Benchmark-local Dockerfile — needs auto-registration
                    pass
                else:
                    raise RuntimeError(
                        f"No Dockerfile found for environment '{env_name}' "
                        f"(checked registered env and benchmark '{job.benchmark_name}')"
                    )
                if not env:
                    logger.info(f"Auto-registering environment '{env_name}' from {env_dir}")
                    dest = os.path.join(self.registry.environments_dir, env_name)
                    if os.path.isdir(dest):
                        shutil.rmtree(dest)
                    shutil.copytree(env_dir, dest)
                    image_tag = f"benchrouter-env-{env_name}:latest"
                    with open(os.path.join(dest, "meta.json"), "w") as _f:
                        json.dump({"name": env_name, "image_tag": image_tag, "status": "building"}, _f)
                else:
                    self.registry.set_environment_status(env_name, "building")

                logger.info(f"Building environment '{env_name}'...")
                self._build_environment(env_name)
                env = self.registry.get_environment(env_name)
                if not env or env["status"] != "ready":
                    raise RuntimeError(f"Failed to build environment '{env_name}'")
                return

        # If we got here, status was "building" — wait outside lock
        logger.info(f"Environment '{env_name}' is building (detected under lock), waiting...")
        env = self._wait_for_env_building(env_name)
        if env and env["status"] == "ready":
            return
        raise RuntimeError(f"Failed to build environment '{env_name}'")

    def _run_job(self, job_id: str, api_key: str):
        """Run a single task job in a Docker container."""
        job = self.jobs[job_id]

        try:
            # Auto-build Docker image if needed
            self._ensure_environment(job)
        except Exception as e:
            logger.error(f"Job {job_id} failed during environment setup: {e}")
            job.status = JobStatus.FAILED
            job.error = str(e)
            self.registry.save_job(job.model_dump())
            return

        task_config = self._load_task_config(job.benchmark_name, job.task_name)

        self._run_job_container(job_id, api_key, task_config)

    def _build_volumes(self, job, task_config=None):
        """Build volume mounts dict for task and result directories."""
        task_dir = self.registry.get_task_dir(job.benchmark_name, job.task_name)
        result_dir = self.registry.get_result_dir(job.job_id)

        # Mount the SDK so runner is available inside container
        sdk_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sdk",
        )

        volumes = {
            os.path.abspath(task_dir): {
                "bind": "/app/benchmark",
                "mode": "ro",
            },
            os.path.abspath(result_dir): {
                "bind": "/app/results",
                "mode": "rw",
            },
        }
        if os.path.isdir(sdk_dir):
            volumes[os.path.abspath(sdk_dir)] = {
                "bind": "/app/benchrouter/sdk",
                "mode": "ro",
            }

        # DinD volumes
        if task_config and task_config.get("docker"):
            image_cache_dir = self.registry.get_image_cache_dir()
            if os.path.isdir(image_cache_dir):
                volumes[os.path.abspath(image_cache_dir)] = {
                    "bind": "/var/lib/docker-cache",
                    "mode": "ro",
                }

            # Mount a host directory for the inner Docker data so overlay2
            # works (kernel 5.10 can't do overlay2-on-overlay2).
            dind_data_dir = os.path.join(
                self.registry.data_dir, "dind-storage", job.job_id
            )
            os.makedirs(dind_data_dir, exist_ok=True)
            volumes[os.path.abspath(dind_data_dir)] = {
                "bind": "/var/lib/docker",
                "mode": "rw",
            }

        return volumes

    def _build_environment_vars(self, job, api_key, task_config):
        """Build container environment variables, merging extra_env."""
        env_vars = {
            "BENCHROUTER_BENCHMARK_DIR": "/app/benchmark",
            "BENCHROUTER_MODEL_ENDPOINT": job.model_endpoint,
            "BENCHROUTER_MODEL_NAME": job.model_name,
            "BENCHROUTER_OUTPUT_DIR": "/app/results",
            "OPENAI_API_KEY": api_key,
            "PYTHONPATH": "/app",
        }
        extra_env = task_config.get("extra_env", {})
        env_vars.update(extra_env)

        if task_config.get("docker"):
            env_vars["BENCHROUTER_DOCKER_ENABLED"] = "true"
            # Tell DinD entrypoint which cached images to load (skip the rest)
            docker_images = task_config.get("docker_images") or []
            if docker_images:
                env_vars["BENCHROUTER_DIND_IMAGES"] = ",".join(docker_images)

        return env_vars

    def _index_result(self, job_id: str, job):
        """Append job result to the result index for per-task tracking."""
        try:
            result_data = self.get_job_result(job_id)
            if result_data:
                self.registry.append_result_index({
                    "job_id": job_id,
                    "run_id": job.run_id,
                    "benchmark": job.benchmark_name,
                    "task": job.task_name,
                    "model": job.model_name,
                    "overall": result_data.get("overall"),
                    "raw_metrics": result_data.get("raw_metrics"),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as idx_err:
            logger.warning(f"Failed to update result index for job {job_id}: {idx_err}")

    def _run_job_container(self, job_id: str, api_key: str, task_config: dict):
        """Run a job in a Docker container, with optional DinD for docker: true tasks."""
        job = self.jobs[job_id]
        container = None
        timeout = self._get_task_timeout(job.benchmark_name, job.task_name)
        use_dind = task_config.get("docker", False)

        self._semaphore.acquire()
        try:
            job.status = JobStatus.RUNNING
            self.registry.save_job(job.model_dump())

            env_meta = self.registry.get_environment(job.env_name)
            image_tag = env_meta["image_tag"]

            volumes = self._build_volumes(job, task_config)
            environment = self._build_environment_vars(job, api_key, task_config)

            # DinD parameters
            run_kwargs = {}
            if use_dind:
                if self._dind_runtime == "sysbox-runc":
                    run_kwargs["runtime"] = "sysbox-runc"
                else:
                    run_kwargs["privileged"] = True
                command = "/app/benchrouter/sdk/dind-entrypoint.sh"
                mem_limit = DIND_MEM_LIMIT
            else:
                command = "python -m benchrouter.sdk.runner"
                run_kwargs["network_mode"] = "host"
                mem_limit = DEFAULT_MEM_LIMIT

            container = self.docker_client.containers.run(
                image=image_tag,
                environment=environment,
                volumes=volumes,
                command=command,
                detach=True,
                mem_limit=mem_limit,
                cpu_period=DEFAULT_CPU_PERIOD,
                cpu_quota=DEFAULT_CPU_QUOTA,
                **run_kwargs,
            )
            job.container_id = container.id
            self.registry.save_job(job.model_dump())

            result = container.wait(timeout=timeout)

            if result["StatusCode"] != 0:
                logs = container.logs(tail=50).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Container exited with code {result['StatusCode']}. Logs:\n{logs}"
                )

            result_data = self.get_job_result(job_id)
            if not result_data:
                raise RuntimeError(
                    f"Container exited successfully but no result.json was produced"
                )
            job.status = JobStatus.COMPLETED
            self._index_result(job_id, job)

        except Exception as e:
            recovered = False
            if container is not None:
                try:
                    container.reload()
                    if container.status == "running":
                        logger.warning(f"Killing timed-out container for job {job_id}")
                        container.kill()
                    elif (
                        container.status == "exited"
                        and container.attrs.get("State", {}).get("ExitCode") == 0
                    ):
                        result_data = self.get_job_result(job_id)
                        if result_data and result_data.get("overall") is not None:
                            logger.info(
                                f"Job {job_id} container exited successfully despite "
                                f"wait error, recovering result"
                            )
                            job.status = JobStatus.COMPLETED
                            self._index_result(job_id, job)
                            recovered = True
                except Exception:
                    pass

            if not recovered:
                logger.error(f"Job {job_id} failed: {e}")
                job.status = JobStatus.FAILED
                if "timed out" in str(e).lower() or "read timed out" in str(e).lower():
                    job.error = f"Timeout after {timeout}s. Task exceeded the time limit."
                else:
                    job.error = str(e)

        finally:
            if container is not None:
                try:
                    logs_text = container.logs(tail=100000).decode("utf-8", errors="replace")
                    self.registry.save_job_logs(job_id, logs_text)
                except Exception:
                    pass
                try:
                    container.remove(force=True)
                except Exception as rm_err:
                    logger.warning(f"Failed to remove container for job {job_id}: {rm_err}")
            # Clean up DinD storage directory
            if use_dind:
                dind_data_dir = os.path.join(
                    self.registry.data_dir, "dind-storage", job_id
                )
                try:
                    shutil.rmtree(dind_data_dir, ignore_errors=True)
                except Exception:
                    pass
            self._semaphore.release()
            self.registry.save_job(job.model_dump())

    # ---- Run queries ----

    def get_run(self, run_id: str) -> Optional[dict]:
        run = self.runs.get(run_id)
        return run.model_dump() if run else None

    def list_runs(self) -> list:
        return [r.model_dump() for r in self.runs.values()]

    # ---- Job queries ----

    def get_job(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        return job.model_dump() if job else None

    def list_jobs(self) -> list:
        return [j.model_dump() for j in self.jobs.values()]

    def get_job_result(self, job_id: str) -> Optional[dict]:
        result_path = os.path.join(
            self.registry.get_result_dir(job_id), "result.json"
        )
        if os.path.exists(result_path):
            with open(result_path) as f:
                return json.load(f)
        return None

    def get_job_logs(self, job_id: str, tail: int = 100) -> Optional[str]:
        """Get logs from a job — persisted file first, then live container."""
        from collections import deque

        tail = max(tail, 1)

        # Try persisted logs first (available after container removal)
        log_path = self.registry.get_job_log_path(job_id)
        if log_path and os.path.exists(log_path):
            with open(log_path) as f:
                last_lines = deque(f, maxlen=tail)
            return "".join(last_lines)

        # Fall back to live container
        job = self.jobs.get(job_id)
        if not job or not job.container_id:
            return None
        try:
            container = self.docker_client.containers.get(job.container_id)
            return container.logs(tail=tail).decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            return None
        except Exception as e:
            logger.error(f"Failed to get logs for job {job_id}: {e}")
            return None

    def stream_job_logs(self, job_id: str):
        """Stream logs from a running job's container. Yields log chunks."""
        job = self.jobs.get(job_id)
        if not job or not job.container_id:
            return
        try:
            container = self.docker_client.containers.get(job.container_id)
            for chunk in container.logs(stream=True, follow=True):
                yield chunk.decode("utf-8", errors="replace")
        except docker.errors.NotFound:
            return
        except Exception as e:
            logger.error(f"Failed to stream logs for job {job_id}: {e}")
            return
