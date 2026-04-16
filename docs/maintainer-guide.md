# 维护者指南（v0.4）

本文档面向 BenchRouter 项目的开发者和维护者，提供完整的代码走读、架构说明和运维指南。

---

## 目录

1. [项目概览](#项目概览)
2. [逐文件代码解析](#逐文件代码解析)
3. [Run + Job 模型与并行执行](#run--job-模型与并行执行)
4. [按需环境构建](#按需环境构建)
5. [存储布局](#存储布局)
6. [API 参考](#api-参考)
7. [CLI 参考](#cli-参考)
8. [常见维护场景与排障](#常见维护场景与排障)

---

## 项目概览

```
BenchRouter/
├── benchrouter/
│   ├── sdk/
│   │   └── runner.py              # 容器内入口
│   ├── server/
│   │   ├── models.py              # Pydantic 数据模型
│   │   ├── registry.py            # 文件存储层
│   │   ├── scheduler.py           # 核心调度引擎
│   │   ├── api.py                 # FastAPI 路由
│   │   └── launch.py              # uvicorn 启动器
│   └── client/
│       └── cli.py                 # Typer CLI
├── benchmarks/                    # Benchmark 定义
├── examples/                      # 示例 benchmark
├── tests/                         # 测试
└── pyproject.toml                 # 包配置（v0.4.0）
```

入口点（`pyproject.toml` 中定义）：

- `benchrouter` → `benchrouter.client.cli:app`（CLI 工具）
- `benchrouter-server` → `benchrouter.server.launch:main`（服务端）

---

## 逐文件代码解析

### models.py — 数据模型

**路径**: `benchrouter/server/models.py`

定义系统中的核心数据结构，全部使用 Pydantic BaseModel。

**状态枚举：**

- `JobStatus`: `QUEUED` → `RUNNING` → `COMPLETED` / `FAILED`
- `RunStatus`: `QUEUED` → `RUNNING` → `COMPLETED` / `PARTIAL` / `FAILED`
  - `PARTIAL` 是 Run 独有的状态，表示部分 task 成功、部分失败

**核心模型：**

| 模型 | 说明 | 关键字段 |
|------|------|----------|
| `EvalJobInfo` | 单个 task 的执行实例 | `job_id`, `run_id`, `task_name`, `benchmark_name`, `env_name`, `model_name`, `model_endpoint`, `status`, `container_id`, `error` |
| `EvalRunInfo` | 一次 benchmark 执行 | `run_id`, `benchmark_name`, `model_name`, `model_endpoint`, `status`, `child_job_ids`, `task_filter`, `aggregated_result` |
| `SubmitEvalRequest` | 提交评测的请求体 | `benchmark`, `model_endpoint`, `model_name`, `api_key`, `tasks`（可选 task 过滤器） |

**设计要点：**

- `EvalJobInfo.run_id` 将 Job 关联到父 Run
- `EvalRunInfo.child_job_ids` 是 Job ID 的列表，形成 1:N 关系
- `EvalRunInfo.task_filter` 记录实际执行了哪些 task（可能是全部，也可能是用户指定的子集）
- `aggregated_result` 在所有 Job 完成后由 scheduler 填充

---

### registry.py — 文件存储层

**路径**: `benchrouter/server/registry.py`

纯文件系统的存储实现，不依赖任何数据库。所有数据以 JSON / YAML 文件的形式存储在 `data_dir` 下。

**初始化：**

```python
Registry(data_dir="/data/benchrouter")
```

自动创建五个子目录：`benchmarks/`、`environments/`、`jobs/`、`runs/`、`results/`。

**各模块功能：**

| 模块 | 方法 | 说明 |
|------|------|------|
| **Benchmarks** | `register_benchmark(name, archive_path)` | 解压 tar.gz 到 `benchmarks/{name}/`，验证 `benchmark.yaml` 和每个 task 的 `task.yaml` |
| | `get_benchmark(name)` | 读取并返回 `benchmark.yaml` 内容 |
| | `list_benchmarks()` | 列出所有 benchmark |
| | `delete_benchmark(name)` | 删除整个 benchmark 目录 |
| **Tasks** | `get_task_config(benchmark_name, task_name)` | 读取 `benchmarks/{benchmark}/{task}/task.yaml` |
| | `get_task_dir(benchmark_name, task_name)` | 返回 task 子目录路径 |
| **Environments** | `register_environment(name, dockerfile_content)` | 保存 Dockerfile，写入 `meta.json`（status=building） |
| | `set_environment_status(name, status)` | 更新构建状态（building → ready / failed） |
| | `get_environment(name)` | 读取 `meta.json` |
| | `get_environment_dockerfile_dir(name)` | 返回 Dockerfile 所在目录（供 docker build 使用） |
| **Jobs** | `save_job(job)` | 持久化 Job 到 `jobs/{job_id}/job.json` |
| | `get_job(job_id)` | 读取 Job |
| **Runs** | `save_run(run)` | 持久化 Run 到 `runs/{run_id}/run.json` |
| | `get_run(run_id)` | 读取 Run |
| **Results** | `get_result_dir(job_id)` | 返回 `results/{job_id}/`（容器挂载点） |
| | `append_result_index(entry)` | 追加写入 `results/index.jsonl`（append-only 索引） |
| | `read_result_index()` | 读取全部索引条目 |

**注册验证逻辑（`register_benchmark`）：**

1. 解压 tar.gz 到 `benchmarks/{name}/`
2. 检查 `benchmark.yaml` 存在
3. 检查 `tasks` 列表非空
4. 遍历每个 task，确认对应子目录中有 `task.yaml`

---

### scheduler.py — 核心调度引擎

**路径**: `benchrouter/server/scheduler.py`

这是系统的核心，负责评测的全生命周期管理。

**初始化：**

```python
EvalScheduler(data_dir="/data/benchrouter", max_concurrent=8)
```

- 创建 `Registry` 实例
- 加载内存缓存（`self.jobs`、`self.runs` 字典）
- 初始化 Docker 客户端
- 创建并发信号量（默认最多 8 个并发 Job）

**资源限制常量：**

| 常量 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_TIMEOUT` | 7200 | 单个 Job 超时（秒） |
| `DEFAULT_MEM_LIMIT` | "16g" | 容器内存上限 |
| `DEFAULT_CPU_QUOTA` | 400000 | CPU 配额（4 核） |
| `DEFAULT_CPU_PERIOD` | 100000 | CPU 周期 |
| `DEFAULT_MAX_CONCURRENT` | 8 | 最大并发 Job 数 |

**核心方法 `submit_eval()`：**

1. 验证 benchmark 已注册
2. 验证 `model_endpoint` 非空
3. 应用 task 过滤器（如用户指定了 `--tasks`）
4. 为每个 task 创建一个 `EvalJobInfo`（状态=QUEUED）
5. 创建一个 `EvalRunInfo`（状态=QUEUED），包含所有 child_job_ids
6. 持久化 Run 和 Job 到磁盘
7. 启动后台线程执行 `_run_benchmark()`
8. 立即返回 `{run_id, job_ids, tasks, status}`

**并行执行 `_run_benchmark()`：**

```
_run_benchmark(run_id)
    │
    ├── 状态 → RUNNING
    │
    ├── 为每个 child job 启动一个线程 → _run_job()
    │   ├── Thread-1: _run_job(job_1)
    │   ├── Thread-2: _run_job(job_2)
    │   └── Thread-3: _run_job(job_3)
    │
    ├── join() 等待所有线程完成
    │
    └── _aggregate_run_results()
        ├── 收集各 task 的 overall 分数
        ├── 计算平均值
        ├── 设置 Run 状态（COMPLETED / PARTIAL / FAILED）
        └── 写入 result index
```

**单 Job 执行 `_run_job()`：**

1. 调用 `_ensure_environment()` 确保 Docker 镜像就绪
2. 加载 `task.yaml` 配置
3. 根据是否有 `sidecars` 选择执行路径：
   - 无 sidecar → `_run_job_simple()`
   - 有 sidecar → `_run_job_with_sidecars()`

**简单执行 `_run_job_simple()`：**

1. 获取信号量（控制并发）
2. 构建卷挂载和环境变量
3. `docker run`：镜像 + 卷 + 环境变量 + `python -m benchrouter.sdk.runner`
4. `container.wait(timeout)` 等待完成
5. 检查退出码，成功则写入 result index
6. 释放信号量

**Sidecar 执行 `_run_job_with_sidecars()`：**

1. 获取信号量
2. 调用 `_generate_compose_file()` 在临时目录生成 `docker-compose.yml`
3. `docker compose up -d` 启动所有服务
4. 执行 sidecar 的 `post_start` 命令
5. `docker compose wait eval` 等待评测容器完成
6. 检查退出码
7. `docker compose down -v --remove-orphans` 清理
8. 释放信号量

**结果聚合 `_aggregate_run_results()`：**

- 遍历所有 child Job
- 收集每个 COMPLETED Job 的 `overall` 分数
- 计算平均值作为 benchmark overall
- 状态判定：
  - 全部成功 → `COMPLETED`
  - 部分成功 → `PARTIAL`
  - 全部失败 → `FAILED`

---

### api.py — FastAPI 路由

**路径**: `benchrouter/server/api.py`

RESTful API 层，版本号 `0.4.0`。启动时初始化全局 `EvalScheduler` 实例。

数据目录通过环境变量 `BENCHROUTER_DATA_DIR`（默认 `/data/benchrouter`）配置。

路由分为 5 组，详见下方 [API 参考](#api-参考)。

---

### cli.py — Typer CLI

**路径**: `benchrouter/client/cli.py`

基于 Typer 构建的命令行工具，通过 HTTP 调用 server API。

CLI 结构：

```
benchrouter
├── run <benchmark>           # 一键：提交 + 轮询 + 显示结果
├── compare <benchmark>       # 跨模型比较
├── logs <job_id>             # 查看 task 日志
├── benchmark
│   ├── register
│   ├── list
│   └── delete
├── env
│   ├── register
│   ├── list
│   ├── status
│   └── delete
└── eval
    ├── submit
    ├── list
    ├── status
    └── result
```

**关键实现 `_poll_run()`：**

`benchrouter run` 命令提交后，通过 `_poll_run()` 轮询 `/runs/{run_id}`：

1. 每 2 秒请求一次
2. 展示实时进度：`[2/4] mcpmark-fs[C], mcpmark-pg[R], mcpmark-pw[Q], mcpmark-pw-webarena[Q]`
3. 当 status 为 COMPLETED / PARTIAL / FAILED 时停止轮询
4. 请求 `/runs/{run_id}/result` 显示最终结果

---

### runner.py — 容器内入口

**路径**: `benchrouter/sdk/runner.py`

在 Docker 容器内执行，是评测脚本与 BenchRouter 之间的桥梁。

**执行流程：**

1. 从环境变量读取 `BENCHROUTER_BENCHMARK_DIR`（默认 `/app/benchmark`）和 `BENCHROUTER_OUTPUT_DIR`（默认 `/app/results`）
2. 读取 `task.yaml`：获取 `name`、`command`、`result_file`、`result_mapping`
3. `subprocess.run(command, shell=True, cwd=benchmark_dir)` 执行评测命令
4. 读取 `$BENCHROUTER_OUTPUT_DIR/{result_file}`
5. 通过 `result_mapping` 提取 overall 字段
6. 写入标准化的 `result.json`

**标准化结果格式：**

```json
{
  "benchmark_name": "task-name",
  "model_name": "gpt-4",
  "overall": 0.85,
  "raw_metrics": { /* 原始结果 */ }
}
```

---

### launch.py — 服务启动器

**路径**: `benchrouter/server/launch.py`

简单的 uvicorn 启动脚本。

```bash
benchrouter-server --host 0.0.0.0 --port 9000 --data-dir /data/benchrouter
```

启动参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `0.0.0.0` | 绑定地址 |
| `--port` | `9000` | 绑定端口 |
| `--data-dir` | `/data/benchrouter` | 数据存储目录 |

将 `data-dir` 写入环境变量 `BENCHROUTER_DATA_DIR`，供 `api.py` 在 startup 时读取。

---

## Run + Job 模型与并行执行

### 数据关系

```
EvalRunInfo (Run)
├── run_id: "a1b2c3d4"
├── benchmark_name: "mcpmark"
├── status: RUNNING
├── child_job_ids: ["j001", "j002", "j003", "j004"]
│
├── EvalJobInfo (Job) run_id=a1b2c3d4
│   ├── job_id: "j001"
│   ├── task_name: "mcpmark-fs"
│   └── status: COMPLETED
│
├── EvalJobInfo (Job) run_id=a1b2c3d4
│   ├── job_id: "j002"
│   ├── task_name: "mcpmark-pg"
│   └── status: RUNNING
│
├── EvalJobInfo (Job) run_id=a1b2c3d4
│   ├── job_id: "j003"
│   ├── task_name: "mcpmark-pw"
│   └── status: QUEUED
│
└── EvalJobInfo (Job) run_id=a1b2c3d4
    ├── job_id: "j004"
    ├── task_name: "mcpmark-pw-webarena"
    └── status: QUEUED
```

### 并行执行流程

```
submit_eval()
    │
    ├── 1. 验证 benchmark 存在，加载 tasks 列表
    ├── 2. 可选：根据 --tasks 参数过滤
    ├── 3. 创建 Run + N 个 Job（状态=QUEUED）
    ├── 4. 持久化到磁盘
    └── 5. 启动后台线程
         │
         └── _run_benchmark(run_id)
              │
              ├── Run.status → RUNNING
              │
              ├── 并行启动 N 个线程
              │   │
              │   ├── Thread[job_1]: _run_job()
              │   │    ├── _ensure_environment()     # 确保 Docker 镜像
              │   │    ├── 检查 sidecars
              │   │    ├── _run_job_simple()          # 或 _run_job_with_sidecars()
              │   │    │    ├── semaphore.acquire()   # 并发控制
              │   │    │    ├── docker run ...
              │   │    │    ├── container.wait()
              │   │    │    ├── 检查退出码
              │   │    │    └── semaphore.release()
              │   │    └── Job.status → COMPLETED / FAILED
              │   │
              │   ├── Thread[job_2]: _run_job() ...
              │   └── Thread[job_N]: _run_job() ...
              │
              ├── join() 等待所有线程
              │
              └── _aggregate_run_results()
                   ├── 收集各 task overall 分数
                   ├── overall = avg(valid_scores)
                   ├── Run.status → COMPLETED / PARTIAL / FAILED
                   └── 写入 result index
```

### 信号量控制

`threading.Semaphore(max_concurrent=8)` 控制最多同时运行的 Job 数。即使一个 benchmark 有 20 个 task，也只会同时运行 8 个。

### 状态持久化

每次状态变更都调用 `registry.save_job()` / `registry.save_run()` 写磁盘。服务重启时从磁盘恢复内存状态。

---

## 按需环境构建

`_ensure_environment(job)` 实现了 Docker 镜像的按需构建：

```
_ensure_environment(job)
    │
    ├── 检查 registry 中 env 状态
    │   ├── status == "ready"    → 直接返回
    │   ├── status == "building" → 轮询等待（最多 6 分钟）
    │   ├── status == "failed"   → 尝试重建
    │   └── env 不存在           → 自动注册并构建
    │
    ├── 查找 Dockerfile
    │   └── 位于 benchmarks/{benchmark}/environments/{env_name}/Dockerfile
    │
    ├── 如果 env 不存在，自动注册
    │   └── registry.register_environment(env_name, dockerfile_content)
    │
    └── 同步构建镜像
        └── _build_environment(env_name)
            └── docker.images.build(path=dir, tag=image_tag)
```

**镜像命名规则**：`benchrouter-env-{name}:latest`

**注意**：`_ensure_environment` 在后台 Job 线程中同步调用。如果多个 Job 共享同一个 environment，第一个到达的会触发构建，后续的会等待（轮询 status）。

---

## 存储布局

```
/data/benchrouter/                      # DATA_DIR
├── benchmarks/
│   ├── mcpmark/
│   │   ├── benchmark.yaml
│   │   ├── environments/
│   │   │   ├── mcpmark-fs/Dockerfile
│   │   │   ├── mcpmark-pg/Dockerfile
│   │   │   └── ...
│   │   ├── mcpmark-fs/
│   │   │   ├── task.yaml
│   │   │   └── run_mcpmark.py
│   │   └── mcpmark-pg/
│   │       └── ...
│   └── simple-math/
│       ├── benchmark.yaml
│       ├── environments/
│       │   └── simple-math/Dockerfile
│       └── default/
│           ├── task.yaml
│           └── eval.py
│
├── environments/
│   ├── mcpmark-fs/
│   │   ├── Dockerfile
│   │   └── meta.json               # {"name":"mcpmark-fs","image_tag":"benchrouter-env-mcpmark-fs:latest","status":"ready"}
│   └── simple-math/
│       ├── Dockerfile
│       └── meta.json
│
├── jobs/
│   ├── a1b2c3d4/
│   │   └── job.json                 # EvalJobInfo 序列化
│   └── e5f6g7h8/
│       └── job.json
│
├── runs/
│   ├── r001/
│   │   └── run.json                 # EvalRunInfo 序列化
│   └── r002/
│       └── run.json
│
└── results/
    ├── a1b2c3d4/
    │   └── result.json              # 标准化评测结果
    ├── e5f6g7h8/
    │   └── result.json
    └── index.jsonl                  # append-only 结果索引
```

**index.jsonl** 每行一个 JSON 对象，有两种类型：

1. **task 级别**（由 `_index_result` 写入）：

```json
{"job_id":"a1b2","run_id":"r001","benchmark":"mcpmark","task":"mcpmark-fs","model":"gpt-4","overall":0.85,"raw_metrics":{...},"completed_at":"..."}
```

2. **benchmark 级别**（由 `_aggregate_run_results` 写入）：

```json
{"run_id":"r001","benchmark":"mcpmark","task":null,"model":"gpt-4","overall":0.75,"task_scores":{"mcpmark-fs":0.85,"mcpmark-pg":0.65},"completed_at":"..."}
```

`task: null` 标识这是 benchmark 聚合条目，`/evals/compare` API 默认只返回这类条目。

---

## API 参考

服务器默认监听 `http://localhost:9000`。

### Benchmark 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/benchmarks/{name}/upload` | 上传 benchmark tar.gz 压缩包 |
| `GET` | `/benchmarks` | 列出所有已注册 benchmark |
| `GET` | `/benchmarks/{name}` | 获取单个 benchmark 详情 |
| `DELETE` | `/benchmarks/{name}` | 删除 benchmark |

**上传请求**：`multipart/form-data`，字段名 `file`，内容为 tar.gz。

### Environment 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/environments/{name}/register` | 上传 Dockerfile，异步构建镜像 |
| `GET` | `/environments` | 列出所有 environment |
| `GET` | `/environments/{name}` | 获取 environment 详情（含构建状态） |
| `DELETE` | `/environments/{name}` | 删除 environment |

### Run 管理（Benchmark 级别）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/evals/submit` | 提交评测 Run |
| `GET` | `/runs` | 列出所有 Run |
| `GET` | `/runs/{run_id}` | 获取 Run 详情（含子 Job 状态） |
| `GET` | `/runs/{run_id}/result` | 获取 Run 聚合结果 |

**提交请求体**（`SubmitEvalRequest`）：

```json
{
  "benchmark": "mcpmark",
  "model_endpoint": "https://api.openai.com/v1",
  "model_name": "gpt-4",
  "api_key": "sk-...",
  "tasks": ["mcpmark-fs", "mcpmark-pg"]   // 可选，不传则运行所有 task
}
```

**Run 详情响应**（`/runs/{run_id}`）会额外注入 `child_jobs` 列表，包含每个子 Job 的完整状态。

### Job 管理（Task 级别）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/evals` | 列出所有 Job |
| `GET` | `/evals/{job_id}` | 获取 Job 详情 |
| `GET` | `/evals/{job_id}/result` | 获取 Job 结果 |
| `GET` | `/evals/{job_id}/logs` | 获取 Job 日志（支持 `?follow=true` 流式） |

### 比较

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/evals/compare` | 跨模型比较 |

参数：

- `benchmark`：按 benchmark 名过滤
- `models`：逗号分隔的模型名
- `detail`：`true` 时返回 task 级别条目，`false`（默认）只返回 benchmark 级别聚合条目

---

## CLI 参考

### 全局选项

所有命令支持 `--server` 选项指定服务器地址（默认 `http://localhost:9000`）。

### benchrouter run

```bash
benchrouter run <benchmark> --model <model_name> [--tasks task1,task2] [--server URL]
```

一键命令：提交评测 → 轮询进度 → 显示结果。

**必需环境变量**：

- `BENCHROUTER_MODEL_ENDPOINT` — 模型 API 地址
- `OPENAI_API_KEY` — API Key（默认 "EMPTY"）

### benchrouter benchmark

```bash
benchrouter benchmark register --name <name> --path <dir>     # 打包上传
benchrouter benchmark list                                     # 列出所有
benchrouter benchmark delete --name <name>                     # 删除
```

`register` 会将目录打包为 tar.gz 上传到 server。

### benchrouter env

```bash
benchrouter env register --name <name> --dockerfile <path>     # 上传 Dockerfile
benchrouter env list                                           # 列出所有
benchrouter env status --name <name>                           # 查看构建状态
benchrouter env delete --name <name>                           # 删除
```

### benchrouter eval

```bash
benchrouter eval submit --benchmark <name> --model <model> [--tasks t1,t2]  # 提交（不等待）
benchrouter eval list                                          # 列出所有 Run
benchrouter eval status --run-id <id>                          # 查看 Run 状态
benchrouter eval result --run-id <id>                          # 获取 Run 结果 JSON
```

### benchrouter compare

```bash
benchrouter compare <benchmark> [--models m1,m2] [--detail]
```

从 result index 读取历史结果进行跨模型比较。`--detail` 显示 task 级别分数。

### benchrouter logs

```bash
benchrouter logs <job_id> [--follow] [--tail 100]
```

查看 Job 容器日志。`--follow` 实时流式输出。

---

## 常见维护场景与排障

### 1. 环境构建失败

**现象**：`benchrouter env status --name xxx` 显示 `Status: failed`

**排查**：

1. 检查 server 日志中的构建错误
2. 确认 Dockerfile 可以手动构建：`docker build -t test /path/to/dockerfile/dir/`
3. 确认 Docker Desktop 正在运行
4. 修复后重新注册：`benchrouter env register --name xxx --dockerfile /path/to/Dockerfile`

### 2. Job 超时

**现象**：Job 状态为 FAILED，error 包含 "Timeout after Xs"

**排查**：

1. 检查 `task.yaml` 中的 `timeout` 设置是否足够
2. 增大 timeout 值
3. 检查评测脚本是否存在死循环或等待问题

**默认超时**：如果 `task.yaml` 未指定 timeout，使用 `DEFAULT_TIMEOUT = 7200`（2 小时）。

### 3. Sidecar 启动失败

**现象**：Job 报错 "docker compose up failed"

**排查**：

1. 检查 sidecar 镜像是否存在：`docker images | grep <image>`
2. 检查 healthcheck 命令是否正确
3. 手动测试 compose：

```bash
# 查看 scheduler 生成的 compose 文件
# 文件在 /tmp/benchrouter-compose-{job_id}-*/docker-compose.yml
# 注意：Job 完成后临时目录会被自动清理
```

4. 检查 `post_start` 命令是否在 sidecar 中可执行

### 4. 结果缺失

**现象**：Job 状态 COMPLETED 但 result 为 null

**排查**：

1. 确认评测脚本向 `$BENCHROUTER_OUTPUT_DIR/result.json` 写入了结果
2. 如果配置了 `result_file`，确认文件名正确
3. 确认结果 JSON 包含 `overall` 字段（或配置了 `result_mapping`）
4. 检查容器日志：`benchrouter logs <job_id>`

### 5. Run 状态为 PARTIAL

**现象**：部分 task 成功，部分失败

**排查**：

1. 查看 Run 详情：`benchrouter eval status --run-id <id>`
2. 找到 FAILED 的 Job
3. 查看该 Job 的日志和错误信息
4. 可以单独重新运行失败的 task：

```bash
benchrouter run <benchmark> --model <model> --tasks <failed_task_name>
```

### 6. 服务重启后状态恢复

BenchRouter 使用文件存储，服务重启时自动恢复：

- `EvalScheduler.__init__()` 从 `jobs/` 和 `runs/` 目录重新加载所有 Job 和 Run
- 但正在运行的容器不会自动恢复执行
- 对于 RUNNING 状态的 Job，需要手动重新提交

### 7. 磁盘空间清理

```bash
# 清理旧的评测结果
rm -rf /data/benchrouter/results/<old_job_id>/

# 清理旧的 Job/Run 记录
rm -rf /data/benchrouter/jobs/<old_job_id>/
rm -rf /data/benchrouter/runs/<old_run_id>/

# 清理 Docker 镜像
docker image prune -f

# 清理 index.jsonl（注意：会丢失历史比较数据）
# 建议备份后再清理
```

### 8. 并发调优

修改 `EvalScheduler` 初始化参数 `max_concurrent`（当前硬编码为 `DEFAULT_MAX_CONCURRENT = 8`）。

如需修改，编辑 `api.py` 中 `startup_event` 里 `EvalScheduler()` 的初始化参数，或未来通过环境变量注入。

### 9. 添加新的 API 路由

1. 在 `models.py` 中定义请求/响应模型（如需要）
2. 在 `scheduler.py` 中添加业务逻辑方法
3. 在 `api.py` 中添加路由函数
4. 在 `cli.py` 中添加对应的 CLI 命令
5. 确保错误以 `HTTPException` 返回，CLI 端处理 HTTP 状态码

### 10. 调试容器执行

手动模拟 scheduler 的容器执行：

```bash
# 查看 scheduler 使用的镜像
docker images | grep benchrouter-env

# 手动运行容器，模拟 scheduler 行为
docker run --rm \
  -v /data/benchrouter/benchmarks/my-bench/default:/app/benchmark:ro \
  -v /tmp/test-results:/app/results:rw \
  -e BENCHROUTER_BENCHMARK_DIR=/app/benchmark \
  -e BENCHROUTER_MODEL_ENDPOINT=http://host.docker.internal:8000/v1 \
  -e BENCHROUTER_MODEL_NAME=test-model \
  -e BENCHROUTER_OUTPUT_DIR=/app/results \
  -e OPENAI_API_KEY=EMPTY \
  -e PYTHONPATH=/app \
  benchrouter-env-my-bench:latest \
  python -m benchrouter.sdk.runner
```
