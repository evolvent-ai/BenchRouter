# BenchRouter v0.4 设计文档

## 1. 概述与设计哲学

BenchRouter 是一个轻量级的 LLM 评测平台（Evaluation-as-a-Service），用于标准化地对大语言模型进行评测。

### 核心设计原则

1. **最小化集成成本**：添加新 benchmark 只需要写两个 YAML 文件（`benchmark.yaml` + `task.yaml`）和一个评测脚本，无需学习框架 API。
2. **模型外置**：BenchRouter 不管理模型。模型预部署在外部（vLLM、OpenAI 兼容 API 等），BenchRouter 只负责调度评测并收集结果。
3. **容器隔离**：每个 task 在独立的 Docker 容器中执行，天然实现环境隔离和资源限制。
4. **零 Ray 依赖**：整个系统基于 FastAPI + Docker，不依赖任何分布式计算框架。
5. **Benchmark-Task 层级结构**：一个 benchmark 包含多个 task，task 并行执行，结果自动聚合。

### v0.3 到 v0.4 的关键变化

| 维度 | v0.3 | v0.4 |
|------|------|------|
| 配置文件 | 单一 `benchrouter.yaml` | `benchmark.yaml` + 各 task 的 `task.yaml` |
| 执行模型 | 单个 Job | Run（父）+ N 个并行 Job（子） |
| 环境管理 | 提交时指定 environment | 由 task 定义 environment，按需自动构建 |
| 结果 | 单个分数 | 按 task 分数 + benchmark 级聚合 |
| API | `/evals` | 新增 `/runs` 端点，保留 `/evals` 作为 job 级查询 |
| 提交请求 | `SubmitEvalRequest` 含 `environment` 字段 | `SubmitEvalRequest` 移除 `environment` 字段，新增 `tasks` 过滤 |

---

## 2. 协议合约

BenchRouter 的核心设计是「契约驱动」：benchmark 作者只需遵循简单的文件协议，不需要导入任何 BenchRouter 库。

### 2.1 benchmark.yaml

每个 benchmark 的根目录必须包含 `benchmark.yaml`，定义 benchmark 元信息和 task 列表。

```yaml
name: "mcpmark"                          # benchmark 唯一标识
description: "MCPMark MCP tool-use benchmark suite"  # 描述
version: "1.0.0"                         # 版本号
tasks:                                   # task 列表（必需，不可为空）
  - name: mcpmark-fs                     # task 名 = 子目录名
    environment: mcpmark-fs              # Docker environment 名
  - name: mcpmark-pg
    environment: mcpmark-pg
  - name: mcpmark-pw
    environment: mcpmark-pw
```

**校验规则**（由 `Registry.register_benchmark` 执行）：
- `benchmark.yaml` 必须存在于归档根目录
- `tasks` 字段必须存在且非空
- 每个 task 必须有 `name` 字段
- 每个 task 的 `name` 对应一个子目录，该子目录中必须存在 `task.yaml`

### 2.2 task.yaml

每个 task 子目录中必须包含 `task.yaml`，定义具体的评测执行方式。

**简单模式**（无 sidecar）：

```yaml
name: "mcpmark-fs"
command: "python /app/benchmark/run_mcpmark.py"   # 容器内执行的命令
description: "MCPMark filesystem benchmark"
timeout: 3600                                      # 可选，秒，默认 7200
```

**Sidecar 模式**（需要外部服务）：

```yaml
name: "mcpmark-pg"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark PostgreSQL benchmark"
timeout: 7200
sidecars:                                   # sidecar 服务列表
  - name: postgres                          # 服务名（Docker Compose 服务名）
    image: "pgvector/pgvector:0.8.0-pg17-bookworm"
    environment:                            # 传给 sidecar 容器的环境变量
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]                    # 端口映射
    healthcheck:                            # 健康检查（可选）
      test: "pg_isready -U postgres"
      interval: "2s"
      timeout: "5s"
      retries: 30
    post_start:                             # 服务启动后执行的命令（可选）
      - "mysql -u user -p... -e 'UPDATE ...'"
      - "/var/www/app/bin/cache:flush"
extra_env:                                  # 额外注入评测容器的环境变量
  POSTGRES_HOST: "postgres"
  POSTGRES_PORT: "5432"
```

**结果映射**（适配已有 benchmark）：

```yaml
name: "wrapped-bench-default"
command: "python existing_benchmark.py"
result_file: "scores.json"             # 自定义输出文件名（默认 result.json）
result_mapping:                        # 字段映射
  overall: "pass@1"                    # 将 pass@1 映射为 overall
```

### 2.3 环境变量

BenchRouter 在容器内注入以下环境变量：

| 环境变量 | 说明 | 示例 |
|---------|------|------|
| `BENCHROUTER_BENCHMARK_DIR` | benchmark 文件挂载路径 | `/app/benchmark` |
| `BENCHROUTER_MODEL_ENDPOINT` | 模型 API 地址 | `https://api.openai.com/v1` |
| `BENCHROUTER_MODEL_NAME` | 模型名称 | `gpt-4` |
| `BENCHROUTER_OUTPUT_DIR` | 结果输出目录 | `/app/results` |
| `OPENAI_API_KEY` | API Key | `sk-...` |
| `PYTHONPATH` | Python 路径 | `/app` |

此外，`task.yaml` 中的 `extra_env` 字段会被合并注入。

### 2.4 result.json

评测脚本的唯一输出契约：向 `$BENCHROUTER_OUTPUT_DIR/result.json` 写入包含 `overall` 字段的 JSON。

```json
{
  "overall": 0.85,
  "correct": 17,
  "total": 20,
  "details": [...]
}
```

- `overall`：一个数值，代表该 task 的综合得分（必需字段）
- 其余字段任意，作为 `raw_metrics` 保留

如果原始 benchmark 输出的文件名或字段名不同，可以通过 `task.yaml` 中的 `result_file` 和 `result_mapping` 进行映射。Runner 会自动完成标准化。

---

## 3. 架构

### 3.1 组件概览

BenchRouter 由四个核心组件构成：

| 组件 | 模块 | 职责 |
|------|------|------|
| **CLI 客户端** | `benchrouter.client.cli` | 用户交互入口，打包上传 benchmark，提交评测，轮询结果 |
| **API 服务** | `benchrouter.server.api` | FastAPI HTTP 接口，版本 0.4.0 |
| **调度器** | `benchrouter.server.scheduler` | 核心编排引擎，管理 Run/Job 生命周期，Docker 容器调度 |
| **注册表** | `benchrouter.server.registry` | 文件系统存储层，持久化 benchmark、环境、任务、结果 |
| **Runner SDK** | `benchrouter.sdk.runner` | 容器内入口程序，读取 task.yaml，执行命令，标准化结果 |

### 3.2 架构图

```
                         用户
                          |
                     benchrouter CLI
                          |
                    HTTP (REST API)
                          |
                  +-----------------+
                  |   FastAPI 服务   |
                  |   (api.py)      |
                  +-----------------+
                          |
                  +-----------------+
                  | EvalScheduler   |
                  | (scheduler.py)  |
                  +-----------------+
                    /          \
                   /            \
    +-------------+    +------------------+
    |  Registry   |    |   Docker Engine  |
    | (文件存储)   |    |                  |
    +-------------+    +------------------+
    |                        |
    | data/                  | 容器执行
    | ├── benchmarks/        |
    | ├── environments/      |  ┌─────────────────────┐
    | ├── jobs/              |  │  评测容器            │
    | ├── runs/              |  │  ┌───────────────┐  │
    | └── results/           |  │  │ Runner (SDK)  │  │
    |                        |  │  │ → task.yaml   │  │
    |                        |  │  │ → exec 命令   │  │
    |                        |  │  │ → result.json │  │
    |                        |  │  └───────────────┘  │
    |                        |  └─────────────────────┘
    |                        |
    |                        |  ┌─────────────────────┐
    |                        |  │  Sidecar 容器        │
    |                        |  │  (postgres, web app) │
    |                        |  └─────────────────────┘
```

### 3.3 数据流

```
1. CLI 打包 benchmark 目录 → tar.gz → POST /benchmarks/{name}/upload
2. CLI 提交评测 → POST /evals/submit → 创建 Run + N 个 Job
3. Scheduler 为每个 Job 按需构建 Docker 镜像（_ensure_environment）
4. Scheduler 并行启动 N 个容器（简单模式或 Docker Compose）
5. 容器内 Runner 读取 task.yaml → 执行 command → 产出 result.json
6. Scheduler 收集各 Job 结果 → 聚合为 Run 级结果
7. CLI 轮询 /runs/{run_id} → 显示聚合分数
```

---

## 4. 调度机制

### 4.1 Run + Job 模型

提交一次 benchmark 评测会产生一个两层结构：

```
Run（benchmark 级别）
├── Job 1（Task A）  ← 独立 Docker 容器
├── Job 2（Task B）  ← 独立 Docker 容器
└── Job 3（Task C）  ← 独立 Docker 容器
```

- **Run**（`EvalRunInfo`）：benchmark 级别的执行记录，包含聚合结果
- **Job**（`EvalJobInfo`）：单个 task 的执行记录，一个容器

Run 状态枚举（`RunStatus`）：

| 状态 | 含义 |
|------|------|
| `QUEUED` | 已创建，等待执行 |
| `RUNNING` | 至少一个 Job 正在执行 |
| `COMPLETED` | 所有 Job 成功完成 |
| `PARTIAL` | 部分 Job 成功，部分失败 |
| `FAILED` | 所有 Job 均失败 |

Job 状态枚举（`JobStatus`）：

| 状态 | 含义 |
|------|------|
| `QUEUED` | 已创建，等待执行 |
| `RUNNING` | 容器正在运行 |
| `COMPLETED` | 成功完成 |
| `FAILED` | 执行失败（包括超时） |

注意：v0.4 移除了 `TIMEOUT` 状态，超时统一归入 `FAILED`，并在 `error` 字段中标注超时信息。

### 4.2 并行执行

```python
def _run_benchmark(self, run_id, api_key):
    # 1. 将 Run 状态设为 RUNNING
    # 2. 为每个子 Job 启动一个线程
    # 3. 等待所有线程完成
    # 4. 聚合结果
```

并发控制通过 `threading.Semaphore` 实现：
- 默认最大并发数：`DEFAULT_MAX_CONCURRENT = 8`
- 每个 Job 在获取信号量后才开始执行，执行结束释放信号量
- 这意味着如果一个 benchmark 有 20 个 task，最多同时运行 8 个

### 4.3 结果聚合

`_aggregate_run_results` 在所有子 Job 完成后执行：

1. 遍历所有子 Job，收集各 task 的 `overall` 分数
2. 计算 benchmark 级别的聚合分数（有效分数的算术平均值）
3. 根据各 Job 状态确定 Run 状态（COMPLETED / PARTIAL / FAILED）
4. 写入 Run 的 `aggregated_result` 和结果索引

聚合结果结构：

```json
{
  "benchmark_name": "mcpmark",
  "model_name": "gpt-4",
  "overall": 0.75,
  "task_scores": {
    "mcpmark-fs": 0.9,
    "mcpmark-pg": 0.8,
    "mcpmark-pw": 0.55,
    "mcpmark-pw-webarena": null
  },
  "tasks_completed": 3,
  "tasks_total": 4
}
```

### 4.4 按需构建环境（_ensure_environment）

v0.4 引入了按需环境构建机制，不再要求用户在提交评测前手动注册所有环境：

```
_ensure_environment(job):
  1. 检查 Registry 中是否已有该环境且状态为 ready → 直接返回
  2. 如果状态为 building → 轮询等待（最长 6 分钟）
  3. 如果不存在或构建失败：
     a. 在 benchmarks/{benchmark}/environments/{env_name}/Dockerfile 中寻找 Dockerfile
     b. 自动注册环境
     c. 同步构建 Docker 镜像
  4. 构建完成后验证状态
```

环境（Docker 镜像）的命名规则：`benchrouter-env-{name}:latest`

用户仍然可以通过 `benchrouter env register` 预先注册环境，但如果 `environments/{env_name}/` 目录中包含 Dockerfile，系统会在需要时自动构建。

### 4.5 容器资源限制

| 资源 | 默认值 |
|------|--------|
| 超时时间 | 7200 秒（2 小时） |
| 内存限制 | 16 GB |
| CPU 配额 | 4 核（cpu_quota=400000, cpu_period=100000） |

task 可以在 `task.yaml` 中通过 `timeout` 字段自定义超时时间。

---

## 5. 评测流程详解

### 5.1 简单模式

适用于不需要外部服务的 task（`task.yaml` 中没有 `sidecars` 字段）。

```
_run_job_simple(job_id, api_key, task_config):
  1. 获取并发信号量
  2. 将 Job 状态设为 RUNNING
  3. 获取 Docker 镜像 tag
  4. 构建 volume 挂载：
     - task 目录 → /app/benchmark（只读）
     - 结果目录 → /app/results（读写）
     - SDK 目录 → /app/benchrouter/sdk（只读）
  5. 构建环境变量（标准变量 + extra_env）
  6. docker.containers.run(...)
     - image: 评测镜像
     - command: "python -m benchrouter.sdk.runner"
     - network_mode: "host"
     - detach: True
     - 资源限制: mem_limit, cpu_quota
  7. container.wait(timeout=...)
  8. 检查退出码
  9. 成功 → COMPLETED，写入结果索引
     失败 → FAILED，记录错误日志
  10. 释放信号量
```

容器内的执行由 Runner SDK 驱动：

```
Runner (benchrouter.sdk.runner):
  1. 读取 /app/benchmark/task.yaml
  2. 解析 command、result_file、result_mapping
  3. subprocess.run(command, shell=True, cwd=benchmark_dir)
  4. 读取 result_file（默认 result.json）
  5. 通过 result_mapping 将原始字段映射为标准 overall
  6. 写入标准化 result.json 到 /app/results/
```

### 5.2 Sidecar 模式

适用于需要外部服务（数据库、Web 应用等）的 task（`task.yaml` 中有 `sidecars` 字段）。

```
_run_job_with_sidecars(job_id, api_key, task_config):
  1. 获取并发信号量
  2. 将 Job 状态设为 RUNNING
  3. 生成 docker-compose.yml（_generate_compose_file）：
     - 为每个 sidecar 创建服务定义
     - 创建 eval 主服务（依赖 sidecar 的健康检查）
     - 写入临时目录
  4. docker compose up -d  ← 启动所有服务
  5. 执行 sidecar 的 post_start 命令（如数据库初始化）
  6. docker compose wait eval  ← 等待评测容器完成
  7. 检查 eval 容器退出码
  8. 成功 → COMPLETED
     失败 → FAILED
  9. docker compose down -v --remove-orphans  ← 清理
  10. 清理临时目录
  11. 释放信号量
```

生成的 docker-compose.yml 结构示例：

```yaml
services:
  postgres:
    image: "pgvector/pgvector:0.8.0-pg17-bookworm"
    environment:
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: "2s"
      timeout: "5s"
      retries: 30
  eval:
    image: "benchrouter-env-mcpmark-pg:latest"
    environment:
      BENCHROUTER_BENCHMARK_DIR: /app/benchmark
      BENCHROUTER_MODEL_ENDPOINT: "https://api.openai.com/v1"
      BENCHROUTER_MODEL_NAME: "gpt-4"
      BENCHROUTER_OUTPUT_DIR: /app/results
      OPENAI_API_KEY: "sk-..."
      POSTGRES_HOST: "postgres"
    volumes:
      - /data/.../mcpmark-pg:/app/benchmark:ro
      - /data/.../results:/app/results:rw
    command: "python -m benchrouter.sdk.runner"
    depends_on:
      postgres:
        condition: service_healthy
```

---

## 6. 错误处理

### 6.1 注册阶段

| 错误场景 | 处理方式 |
|---------|---------|
| 归档中缺少 `benchmark.yaml` | `FileNotFoundError`，HTTP 400 |
| `tasks` 字段为空 | `ValueError`，HTTP 400 |
| task 子目录缺少 `task.yaml` | `FileNotFoundError`，HTTP 400 |
| task 缺少 `name` 字段 | `ValueError`，HTTP 400 |

### 6.2 提交阶段

| 错误场景 | 处理方式 |
|---------|---------|
| benchmark 未注册 | `ValueError: Benchmark '{name}' not registered` |
| `model_endpoint` 为空 | `ValueError: model_endpoint is required` |
| task 过滤无匹配 | `ValueError: No matching tasks for filter` |

### 6.3 执行阶段

| 错误场景 | 处理方式 |
|---------|---------|
| 环境构建失败 | Job 标记为 FAILED，错误信息记录到 `job.error` |
| 环境构建超时 | 等待最长 6 分钟后抛出 `RuntimeError` |
| 容器执行超时 | 杀死容器，Job 标记为 FAILED，`error` 包含超时信息 |
| 容器非零退出码 | Job 标记为 FAILED，记录最后 50 行日志 |
| Docker Compose 启动失败 | Job 标记为 FAILED，记录 stderr |
| post_start 命令失败 | Job 标记为 FAILED |
| task 未产出 result.json | Runner 抛出 `FileNotFoundError`，容器非零退出 |
| result.json 缺少 overall 字段 | Runner 抛出 `KeyError`，容器非零退出 |

### 6.4 聚合阶段

- 全部成功 → Run 状态 `COMPLETED`
- 部分成功 → Run 状态 `PARTIAL`，`overall` 仅基于成功的 task 计算
- 全部失败 → Run 状态 `FAILED`，`overall` 为 `null`

---

## 7. 存储结构

所有数据存储在由 `BENCHROUTER_DATA_DIR`（默认 `/data/benchrouter`）指定的目录下：

```
$BENCHROUTER_DATA_DIR/
├── benchmarks/
│   ├── mcpmark/
│   │   ├── benchmark.yaml         # benchmark 配置
│   │   ├── mcpmark-fs/            # task 子目录
│   │   │   ├── task.yaml
│   │   │   └── run_mcpmark.py
│   │   ├── mcpmark-pg/
│   │   │   ├── task.yaml
│   │   │   └── run_mcpmark.py
│   │   └── ...
│   └── simple-math/
│       ├── benchmark.yaml
│       └── default/
│           ├── task.yaml
│           └── eval.py
│
├── environments/
│   └── mcpmark-fs/
│       ├── Dockerfile             # 原始 Dockerfile
│       └── meta.json              # {"name": "...", "image_tag": "...", "status": "ready"}
│
├── jobs/
│   └── {job_id}/
│       └── job.json               # EvalJobInfo 序列化
│
├── runs/
│   └── {run_id}/
│       └── run.json               # EvalRunInfo 序列化（含 aggregated_result）
│
└── results/
    ├── index.jsonl                # 追加式结果索引（JSONL 格式）
    └── {job_id}/
        └── result.json            # 标准化后的评测结果
```

### 7.1 结果索引（index.jsonl）

结果索引是追加式的 JSONL 文件，用于跨模型比较。每行一个 JSON 对象：

**Task 级条目**（每个 Job 完成时写入）：

```json
{"job_id": "abc123", "run_id": "run456", "benchmark": "mcpmark", "task": "mcpmark-fs", "model": "gpt-4", "overall": 0.9, "raw_metrics": {...}, "completed_at": "..."}
```

**Benchmark 级条目**（Run 聚合完成时写入，`task` 为 `null`）：

```json
{"run_id": "run456", "benchmark": "mcpmark", "task": null, "model": "gpt-4", "overall": 0.75, "task_scores": {...}, "completed_at": "..."}
```

### 7.2 持久化与重启恢复

`EvalScheduler` 初始化时从磁盘重新加载所有 Job 和 Run 状态：

```python
def __init__(self, data_dir):
    # ...
    for j in self.registry.list_jobs():
        self.jobs[j["job_id"]] = EvalJobInfo(**j)
    for r in self.registry.list_runs():
        self.runs[r["run_id"]] = EvalRunInfo(**r)
```

这意味着服务重启后，历史记录不会丢失。但正在执行中的容器不会自动恢复。

---

## 8. API 端点

API 版本：`0.4.0`，基于 FastAPI。

### 8.1 Benchmark 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/benchmarks/{name}/upload` | 上传 benchmark（tar.gz 归档） |
| `GET` | `/benchmarks` | 列出所有已注册的 benchmark |
| `GET` | `/benchmarks/{name}` | 获取指定 benchmark 信息 |
| `DELETE` | `/benchmarks/{name}` | 删除 benchmark |

### 8.2 环境管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/environments/{name}/register` | 上传 Dockerfile，异步构建镜像 |
| `GET` | `/environments` | 列出所有环境 |
| `GET` | `/environments/{name}` | 获取指定环境状态 |
| `DELETE` | `/environments/{name}` | 删除环境 |

### 8.3 评测提交

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/evals/submit` | 提交评测请求 |

请求体（`SubmitEvalRequest`）：

```json
{
  "benchmark": "mcpmark",
  "model_endpoint": "https://api.openai.com/v1",
  "model_name": "gpt-4",
  "api_key": "sk-...",
  "tasks": ["mcpmark-fs", "mcpmark-pg"]
}
```

- `benchmark`：已注册的 benchmark 名（必需）
- `model_endpoint`：模型 API 地址（必需）
- `model_name`：模型名称（必需）
- `api_key`：API Key，默认 `"EMPTY"`
- `tasks`：可选，task 名列表。不指定则运行所有 task

### 8.4 Run 查询（benchmark 级别）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/runs` | 列出所有 Run |
| `GET` | `/runs/{run_id}` | 获取 Run 详情（含子 Job 状态） |
| `GET` | `/runs/{run_id}/result` | 获取 Run 聚合结果 |

### 8.5 Job 查询（task 级别）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/evals` | 列出所有 Job |
| `GET` | `/evals/{job_id}` | 获取指定 Job 详情 |
| `GET` | `/evals/{job_id}/result` | 获取 Job 结果 |
| `GET` | `/evals/{job_id}/logs` | 获取 Job 日志（支持 `follow` 流式和 `tail` 参数） |

### 8.6 结果比较

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/evals/compare` | 跨模型比较结果 |

查询参数：
- `benchmark`：按 benchmark 名过滤
- `models`：逗号分隔的模型名列表
- `detail`：`true` 时返回 task 级明细，`false`（默认）只返回 benchmark 级聚合

---

## 9. 项目结构

```
BenchRouter/
├── benchrouter/
│   ├── sdk/
│   │   └── runner.py              # 容器内入口：读 task.yaml → 执行 → 标准化结果
│   ├── server/
│   │   ├── api.py                 # FastAPI 路由（v0.4.0）
│   │   ├── scheduler.py           # 核心调度：Run + 并行 Job 编排
│   │   ├── registry.py            # 文件系统存储（benchmark/env/job/run/result）
│   │   ├── models.py              # Pydantic 数据模型（JobStatus, RunStatus, EvalJobInfo, EvalRunInfo, SubmitEvalRequest）
│   │   └── launch.py              # uvicorn 启动器（benchrouter-server 入口）
│   └── client/
│       └── cli.py                 # Typer CLI（benchrouter 入口）
│
├── benchmarks/
│   └── mcpmark/                   # MCPMark benchmark（4 个 task）
│       ├── benchmark.yaml
│       ├── mcpmark-fs/
│       │   └── task.yaml
│       ├── mcpmark-pg/
│       │   └── task.yaml
│       ├── mcpmark-pw/
│       │   └── task.yaml
│       └── mcpmark-pw-webarena/
│           └── task.yaml
│
├── examples/
│   ├── simple_math/               # 最简示例：3 道数学题
│   │   ├── benchmark.yaml
│   │   ├── environments/
│   │   │   └── simple-math/Dockerfile
│   │   └── default/
│   │       ├── task.yaml
│   │       └── eval.py
│   └── command_wrap/              # 包装已有 benchmark 的示例
│       ├── benchmark.yaml
│       ├── environments/
│       │   └── command-wrap/Dockerfile
│       └── default/
│           ├── task.yaml          # 含 result_file + result_mapping
│           └── existing_benchmark.py
│
├── tests/
│   ├── mock_model.py              # Mock 模型服务器（本地测试用）
│   ├── test_models.py             # 数据模型单元测试
│   ├── test_registry.py           # Registry 单元测试
│   └── test_scheduler.py          # Scheduler 单元测试（不依赖 Docker）
│
└── pyproject.toml                 # 项目配置，版本 0.4.0
```

### 9.1 入口点

在 `pyproject.toml` 中定义了两个命令行入口：

```toml
[project.scripts]
benchrouter = "benchrouter.client.cli:app"           # CLI 客户端
benchrouter-server = "benchrouter.server.launch:main" # 服务端
```

### 9.2 依赖

```
fastapi >= 0.104.0     # HTTP 框架
uvicorn >= 0.24.0      # ASGI 服务器
pydantic >= 2.0.0      # 数据校验
docker >= 7.0.0        # Docker Python SDK
openai >= 1.0.0        # OpenAI 客户端（评测脚本使用）
requests >= 2.31.0     # HTTP 客户端（CLI 使用）
typer >= 0.9.0         # CLI 框架
pyyaml >= 6.0          # YAML 解析
python-multipart >= 0.0.6  # 文件上传支持
```

---

## 10. 开发者指南：添加新 Benchmark

### 10.1 最小步骤

**第 1 步：创建目录结构**

```
benchmarks/my-bench/
├── benchmark.yaml
├── environments/
│   └── my-bench/Dockerfile
└── default/               # 至少一个 task
    ├── task.yaml
    └── eval.py
```

**第 2 步：编写 benchmark.yaml**

```yaml
name: "my-bench"
description: "My custom benchmark"
version: "1.0.0"
tasks:
  - name: default
    environment: my-bench
```

**第 3 步：编写 task.yaml**

```yaml
name: "my-bench-default"
command: "python eval.py"
```

**第 4 步：编写评测脚本**

评测脚本只需遵循以下契约：
1. 从环境变量读取模型信息：`BENCHROUTER_MODEL_ENDPOINT`、`BENCHROUTER_MODEL_NAME`、`OPENAI_API_KEY`
2. 向 `$BENCHROUTER_OUTPUT_DIR/result.json` 写入包含 `overall` 字段的 JSON

```python
import os
import json
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["BENCHROUTER_MODEL_ENDPOINT"],
    api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
)
model = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
output_dir = os.environ["BENCHROUTER_OUTPUT_DIR"]

# ... 执行评测逻辑 ...

result = {"overall": score, "details": [...]}
os.makedirs(output_dir, exist_ok=True)
with open(os.path.join(output_dir, "result.json"), "w") as f:
    json.dump(result, f)
```

**第 5 步：编写 Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install openai pyyaml
# 安装评测所需的其他依赖
```

**第 6 步：注册并运行**

```bash
benchrouter env register --name my-bench --dockerfile benchmarks/my-bench/environments/my-bench/Dockerfile
benchrouter benchmark register --name my-bench --path benchmarks/my-bench/
benchrouter run my-bench --model your-model
```

### 10.2 包装已有 Benchmark

如果你有一个已有的评测代码库，只需做最小改动即可接入 BenchRouter：

1. **修改模型地址**：将硬编码的模型 URL 改为从 `$BENCHROUTER_MODEL_ENDPOINT` 读取
2. **修改输出路径**：将结果写入 `$BENCHROUTER_OUTPUT_DIR`
3. **字段映射**：如果已有代码输出的字段名不是 `overall`，在 `task.yaml` 中使用 `result_mapping`

```yaml
name: "wrapped-bench"
command: "python existing_benchmark.py"
result_file: "scores.json"         # 已有代码输出的文件名
result_mapping:
  overall: "pass@1"                # 将 pass@1 映射为 overall
```

### 10.3 添加需要 Sidecar 的 Task

如果评测需要数据库、Web 应用等外部服务：

```yaml
name: "my-task-with-db"
command: "python eval.py"
timeout: 7200
sidecars:
  - name: postgres
    image: "postgres:16"
    environment:
      POSTGRES_PASSWORD: "testpass"
    healthcheck:
      test: "pg_isready -U postgres"
      interval: "2s"
      retries: 30
    post_start:
      - "psql -U postgres -c 'CREATE DATABASE testdb;'"
extra_env:
  DB_HOST: "postgres"
  DB_PORT: "5432"
```

`healthcheck` 确保 sidecar 就绪后才启动评测容器。`post_start` 在 sidecar 启动后执行初始化命令。

### 10.4 多 Task Benchmark

一个 benchmark 可以包含多个 task，每个 task 独立执行、独立打分：

```
benchmarks/my-suite/
├── benchmark.yaml
├── task-easy/
│   ├── task.yaml
│   └── eval.py
├── task-medium/
│   ├── task.yaml
│   └── eval.py
└── task-hard/
    ├── task.yaml
    └── eval.py
```

```yaml
# benchmark.yaml
name: "my-suite"
tasks:
  - name: task-easy
    environment: my-suite
  - name: task-medium
    environment: my-suite
  - name: task-hard
    environment: my-suite-heavy  # 可以用不同的环境
```

运行时可以用 `--tasks` 过滤只执行部分 task：

```bash
benchrouter run my-suite --model gpt-4 --tasks task-easy,task-medium
```

---

## 11. 测试

### 11.1 测试结构

测试位于 `tests/` 目录，使用 pytest 框架：

| 文件 | 覆盖范围 |
|------|---------|
| `test_models.py` | 数据模型校验：状态枚举、必填字段、v0.4 字段变更 |
| `test_registry.py` | 文件存储层：benchmark 注册/验证、task 配置、Run/Job 持久化、结果索引、环境 CRUD |
| `test_scheduler.py` | 调度器核心逻辑：提交验证、task 过滤、结果聚合、持久化恢复 |

### 11.2 测试策略

- **不依赖 Docker**：所有单元测试都不需要 Docker 运行环境。执行路径通过 `unittest.mock.patch` 跳过。
- **临时目录隔离**：通过 `tempfile.mkdtemp` 创建临时数据目录，测试结束后清理。
- **辅助函数**：`_make_benchmark_archive` 和 `_register_benchmark` 用于快速构造测试数据。

### 11.3 关键测试用例

**模型测试**（`test_models.py`）：
- 确认 `JobStatus` 不包含已删除的 `TIMEOUT` 状态
- 确认 `RunStatus` 包含 `PARTIAL` 状态
- 确认 `EvalJobInfo` 必须包含 `run_id` 和 `task_name`
- 确认 `SubmitEvalRequest` 不再包含 `environment` 字段

**注册表测试**（`test_registry.py`）：
- benchmark 注册的完整验证链（缺少 benchmark.yaml、空 tasks、缺少 task.yaml）
- task 配置读取和 task 目录解析
- Run/Job 的持久化和读取
- 结果索引的追加和读取
- 环境的注册、状态更新、删除

**调度器测试**（`test_scheduler.py`）：
- 提交验证（未注册 benchmark、空 endpoint、task 过滤不匹配）
- 成功提交创建正确的 Run + Job 结构
- task 过滤只创建匹配的 Job
- 结果聚合的三种场景（全部成功、部分成功、全部失败）
- 聚合写入结果索引
- task 配置和超时读取
- Run/Job 在调度器重启后可恢复

### 11.4 运行测试

```bash
pytest tests/ -v
```

### 11.5 Mock 模型服务器

`tests/mock_model.py` 提供了一个简单的 OpenAI 兼容 API Mock，用于端到端测试：

```bash
python tests/mock_model.py   # 启动在 localhost:8000
```

支持的响应模式：
- 数学题（1+1、2*3、10-4）→ 正确答案
- 代码生成请求 → 返回函数定义
- 其他 → 回显输入
