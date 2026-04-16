# MCPMark 集成文档（v0.4）

本文档详细介绍 MCPMark 评测套件在 BenchRouter 中的集成方式。

---

## 概述

MCPMark 是一个 MCP（Model Context Protocol）工具调用评测框架，测试模型在多轮对话中使用 MCP 工具的能力。BenchRouter 通过 **Bridge 脚本模式** 将 MCPMark 作为一个 benchmark 接入，无需修改 MCPMark 源码。

MCPMark benchmark 包含 4 个 task，分别测试不同的 MCP 工具：

| Task | MCP 服务 | 需要 Sidecar | 说明 |
|------|----------|-------------|------|
| `mcpmark-fs` | filesystem | 否 | 文件系统操作 |
| `mcpmark-pg` | postgres | 是（PostgreSQL） | 数据库操作 |
| `mcpmark-pw` | playwright | 否 | 浏览器操作 |
| `mcpmark-pw-webarena` | playwright_webarena | 是（WebArena/Magento） | Web 应用交互 |

---

## benchmark.yaml 结构

```yaml
name: "mcpmark"
description: "MCPMark MCP tool-use benchmark suite — multi-turn agentic eval with MCP tool calling"
version: "1.0.0"
tasks:
  - name: mcpmark-fs
    environment: mcpmark-fs
  - name: mcpmark-pg
    environment: mcpmark-pg
  - name: mcpmark-pw
    environment: mcpmark-pw
  - name: mcpmark-pw-webarena
    environment: mcpmark-pw-webarena
```

**关键设计决策：**

- 每个 task 使用独立的 `environment`（Docker 镜像），而不是共享一个。这是因为不同 task 可能需要不同的基础镜像（如 `evalsysorg/mcpmark:latest` vs `fanqingm/mcpmark:latest`）。
- task 名称与子目录名一一对应：`mcpmark-fs/`、`mcpmark-pg/`、`mcpmark-pw/`、`mcpmark-pw-webarena/`。

---

## 4 个 Task 详解

### mcpmark-fs（文件系统）

**task.yaml：**

```yaml
name: "mcpmark-fs"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark filesystem benchmark — multi-turn agentic eval with MCP tool calling"
timeout: 3600
```

**特点：**

- 最简单的 task，无需 sidecar
- 测试模型使用 MCP filesystem 工具进行文件读写操作
- 超时 1 小时
- MCPMark pipeline 参数：`--mcp filesystem --task-suite easy --tasks file_context/uppercase`

---

### mcpmark-pg（PostgreSQL）

**task.yaml：**

```yaml
name: "mcpmark-pg"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark PostgreSQL benchmark"
timeout: 7200
sidecars:
  - name: postgres
    image: "pgvector/pgvector:0.8.0-pg17-bookworm"
    environment:
      POSTGRES_PASSWORD: "password"
    ports: ["5432:5432"]
    healthcheck:
      test: "pg_isready -U postgres"
      interval: "2s"
      timeout: "5s"
      retries: 30
extra_env:
  POSTGRES_HOST: "postgres"
  POSTGRES_PORT: "5432"
  POSTGRES_USERNAME: "postgres"
  POSTGRES_PASSWORD: "password"
  POSTGRES_DATABASE: "postgres"
```

**特点：**

- 需要 PostgreSQL sidecar（使用 pgvector 镜像）
- Healthcheck 通过 `pg_isready` 确认数据库就绪
- `extra_env` 将数据库连接信息注入评测容器
- 评测容器中的 MCPMark 通过 `POSTGRES_HOST: "postgres"` 连接 sidecar（Docker Compose 网络中的服务名即为主机名）
- 超时 2 小时
- MCPMark pipeline 参数：`--mcp postgres --task-suite easy --tasks chinook/update_employee_info`

**执行流程：**

```
Scheduler 生成 docker-compose.yml
    │
    ├── 服务 "postgres"：pgvector 镜像
    │   └── healthcheck: pg_isready
    │
    └── 服务 "eval"：mcpmark-pg 镜像
        ├── depends_on: postgres (service_healthy)
        ├── environment: POSTGRES_HOST=postgres, ...
        └── command: python -m benchrouter.sdk.runner
```

---

### mcpmark-pw（Playwright）

**task.yaml：**

```yaml
name: "mcpmark-pw"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark Playwright browser benchmark"
timeout: 3600
```

**特点：**

- 无需 sidecar（浏览器在评测容器内运行）
- 测试模型使用 MCP Playwright 工具进行浏览器操作
- 评测容器镜像需要预装 Playwright 和浏览器

---

### mcpmark-pw-webarena（Playwright + WebArena）

**task.yaml：**

```yaml
name: "mcpmark-pw-webarena"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark Playwright WebArena benchmark (Shopping Admin/Magento)"
timeout: 7200
sidecars:
  - name: shopping_admin
    image: "shopping_admin_final_0719"
    ports: ["7780:80"]
    healthcheck:
      test: "curl -sf http://localhost:80/admin || exit 1"
      interval: "10s"
      timeout: "15s"
      retries: 60
    post_start:
      - 'mysql -u magentouser -pMyPassword magentodb -e "UPDATE core_config_data SET value=''http://shopping_admin:80/'' WHERE path IN (''web/secure/base_url'', ''web/unsecure/base_url'');"'
      - "/var/www/magento2/bin/magento cache:flush"
extra_env:
  WEBARENA_SIDECAR_MODE: "true"
  WEBARENA_SHOPPING_ADMIN_URL: "http://shopping_admin:80"
  PLAYWRIGHT_HEADLESS: "true"
  PLAYWRIGHT_BROWSER: "chromium"
```

**特点：**

- 最复杂的 task，需要 WebArena（Magento 电商应用）作为 sidecar
- Sidecar 镜像 `shopping_admin_final_0719` 是一个预构建的 Magento 应用镜像
- Healthcheck 通过 curl 检测管理后台是否就绪（最多等 10 分钟：interval=10s * retries=60）
- **post_start**：sidecar 启动后需要两步初始化
  1. 更新 Magento 数据库中的 base URL 为 Docker Compose 网络内的地址
  2. 刷新 Magento 缓存
- `extra_env` 告诉评测脚本使用 sidecar 模式并提供 URL
- 超时 2 小时
- MCPMark pipeline 参数：`--mcp playwright_webarena --task-suite easy --tasks shopping_admin/ny_expansion_analysis_easy`

**执行流程：**

```
Scheduler 生成 docker-compose.yml
    │
    ├── 服务 "shopping_admin"：Magento 镜像
    │   ├── healthcheck: curl admin 页面
    │   └── post_start:
    │       ├── 更新数据库 base URL
    │       └── 刷新缓存
    │
    └── 服务 "eval"：mcpmark-pw-webarena 镜像
        ├── depends_on: shopping_admin (service_healthy)
        ├── environment: WEBARENA_SHOPPING_ADMIN_URL=http://shopping_admin:80, ...
        └── command: python -m benchrouter.sdk.runner
```

---

## Bridge 脚本模式

每个 task 子目录中的 `run_mcpmark.py` 是 BenchRouter 与 MCPMark 之间的桥梁。所有 4 个 task 的 bridge 脚本结构完全相同，仅 MCPMark pipeline 参数不同。

### Bridge 脚本执行流程

```
容器启动
    │
    └── python -m benchrouter.sdk.runner
         │
         ├── 读取 task.yaml
         ├── command: "python /app/benchmark/run_mcpmark.py"
         │
         └── subprocess.run("python /app/benchmark/run_mcpmark.py")
              │
              ├── 1. 读取 BenchRouter 环境变量
              │   ├── BENCHROUTER_MODEL_ENDPOINT
              │   ├── BENCHROUTER_MODEL_NAME
              │   ├── OPENAI_API_KEY
              │   └── BENCHROUTER_OUTPUT_DIR
              │
              ├── 2. 转换为 MCPMark 格式
              │   ├── OPENAI_API_BASE = BENCHROUTER_MODEL_ENDPOINT  (LiteLLM 兼容)
              │   └── model_name = "openai/" + model_name        (LiteLLM 前缀)
              │
              ├── 3. 调用 MCPMark pipeline
              │   └── python -m pipeline --mcp <service> --models <model> ...
              │
              ├── 4. 收集 MCPMark 输出
              │   └── 遍历 /tmp/mcpmark_results/benchrouter/**/meta.json
              │       └── 提取 execution_result.success, token_usage 等
              │
              └── 5. 写入 BenchRouter 格式的 result.json
                  └── $BENCHROUTER_OUTPUT_DIR/result.json
```

### Bridge 脚本核心代码解析

**环境变量转换**（所有 bridge 脚本共用的模式）：

```python
# BenchRouter 环境变量
model_endpoint = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "")
model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
output_dir = os.environ.get("BENCHROUTER_OUTPUT_DIR", "/app/results")

# 转换为 MCPMark/LiteLLM 格式
os.environ["OPENAI_API_BASE"] = model_endpoint     # LiteLLM 读取此变量作为 base URL
if not model_name.startswith("openai/"):
    model_name = f"openai/{model_name}"             # LiteLLM 需要 "openai/" 前缀
```

**MCPMark pipeline 调用**（各 task 不同的部分）：

| Task | `--mcp` | `--task-suite` | `--tasks` |
|------|---------|----------------|-----------|
| mcpmark-fs | `filesystem` | `easy` | `file_context/uppercase` |
| mcpmark-pg | `postgres` | `easy` | `chinook/update_employee_info` |
| mcpmark-pw | `playwright` | `easy` | *(视具体配置)* |
| mcpmark-pw-webarena | `playwright_webarena` | `easy` | `shopping_admin/ny_expansion_analysis_easy` |

**结果收集与转换**：

MCPMark 将每个子任务的结果写入 `meta.json`，包含 `execution_result.success` 等字段。Bridge 脚本遍历所有 `meta.json`，统计成功率，生成 BenchRouter 格式的 result.json：

```json
{
  "overall": 0.75,                    // passed / total
  "total_tasks": 4,
  "successful_tasks": 3,
  "failed_tasks": 1,
  "per_task": [                       // 每个 MCPMark 子任务的详细信息
    {
      "task_name": "file_context/uppercase",
      "success": true,
      "error_message": null,
      "verification_output": "...",
      "token_usage": {"prompt_tokens": 100, "completion_tokens": 50},
      "turn_count": 3,
      "agent_execution_time": 12.5,
      "task_execution_time": 15.0
    }
  ],
  "mcpmark_config": {                 // 记录 MCPMark 运行配置
    "mcp_service": "filesystem",
    "task_suite": "easy",
    "tasks": "file_context/uppercase",
    "model_name": "openai/gpt-4",
    "model_endpoint": "https://api.openai.com/v1"
  }
}
```

---

## Sidecar 配置详解

### Sidecar 与评测容器的网络

当 task 配置了 `sidecars`，scheduler 自动生成 Docker Compose 文件。所有服务在同一个 Compose 网络中，通过服务名互相访问：

```
Docker Compose Network
├── eval          ← 评测容器（运行 runner + bridge 脚本）
├── postgres      ← sidecar（mcpmark-pg）
└── shopping_admin ← sidecar（mcpmark-pw-webarena）
```

评测容器中可以直接用 `postgres:5432` 或 `shopping_admin:80` 访问 sidecar。

### Healthcheck

Scheduler 为有 healthcheck 的 sidecar 生成 `depends_on` 配置，确保 sidecar 完全就绪后才启动评测容器：

```yaml
# 生成的 docker-compose.yml 片段
services:
  postgres:
    image: pgvector/pgvector:0.8.0-pg17-bookworm
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 5s
      retries: 30
  eval:
    depends_on:
      postgres:
        condition: service_healthy
```

如果 sidecar 没有配置 healthcheck，则使用 `condition: service_started`（仅确保容器启动，不等待就绪）。

### post_start

`post_start` 命令在 sidecar healthcheck 通过后、eval 容器开始执行前运行。通过 `docker compose exec` 在对应 sidecar 容器内执行：

```bash
docker compose -p benchrouter-{job_id} -f docker-compose.yml \
  exec -T shopping_admin sh -c "mysql -u magentouser ..."
```

每条命令有 120 秒超时。如果任何命令失败，整个 Job 报错。

### extra_env

`task.yaml` 中的 `extra_env` 会合并到评测容器的环境变量中。这是将 sidecar 连接信息传递给评测脚本的标准方式：

```yaml
extra_env:
  POSTGRES_HOST: "postgres"         # 服务名 = Docker Compose 网络中的主机名
  POSTGRES_PORT: "5432"
  POSTGRES_USERNAME: "postgres"
  POSTGRES_PASSWORD: "password"
```

---

## 运行方式

### 运行全部 4 个 task

```bash
# 设置环境变量
export BENCHROUTER_MODEL_ENDPOINT=https://api.openai.com/v1
export OPENAI_API_KEY=sk-your-key

# 启动 server
benchrouter-server --data-dir /data/benchrouter --port 9000

# 注册环境（4 个 task 各自的 Docker 镜像）
benchrouter env register --name mcpmark-fs --dockerfile benchmarks/mcpmark/environments/mcpmark-fs/Dockerfile
benchrouter env register --name mcpmark-pg --dockerfile benchmarks/mcpmark/environments/mcpmark-pg/Dockerfile
benchrouter env register --name mcpmark-pw --dockerfile benchmarks/mcpmark/environments/mcpmark-pw/Dockerfile
benchrouter env register --name mcpmark-pw-webarena --dockerfile benchmarks/mcpmark/environments/mcpmark-pw-webarena/Dockerfile

# 注册 benchmark
benchrouter benchmark register --name mcpmark --path benchmarks/mcpmark/

# 运行全部 task
benchrouter run mcpmark --model gpt-4
```

输出示例：

```
Run a1b2c3d4 submitted (4 tasks: mcpmark-fs, mcpmark-pg, mcpmark-pw, mcpmark-pw-webarena). Waiting...
  [2/4] mcpmark-fs[C], mcpmark-pg[C], mcpmark-pw[R], mcpmark-pw-webarena[R]
  [4/4] mcpmark-fs[C], mcpmark-pg[C], mcpmark-pw[C], mcpmark-pw-webarena[C]

  Benchmark: mcpmark
  Overall:   0.7500
  Tasks:     4/4 completed

  Per-task scores:
    mcpmark-fs                     1.0000
    mcpmark-pg                     0.5000
    mcpmark-pw                     1.0000
    mcpmark-pw-webarena            0.5000
```

### 运行指定 task

```bash
# 只运行 filesystem 和 postgres
benchrouter run mcpmark --model gpt-4 --tasks mcpmark-fs,mcpmark-pg
```

### 使用 eval submit（不等待）

```bash
# 异步提交
benchrouter eval submit --benchmark mcpmark --model gpt-4 --tasks mcpmark-fs

# 手动轮询
benchrouter eval status --run-id <run_id>

# 获取结果
benchrouter eval result --run-id <run_id>
```

### 跨模型比较

```bash
# 在不同模型上分别运行后
benchrouter run mcpmark --model gpt-4
benchrouter run mcpmark --model claude-3-opus

# 比较
benchrouter compare mcpmark
benchrouter compare mcpmark --detail    # 显示 task 级别分数
```

### 查看 Job 日志

```bash
# 先获取 Job ID
benchrouter eval status --run-id <run_id>

# 查看指定 task Job 的日志
benchrouter logs <job_id>

# 实时流式日志
benchrouter logs <job_id> --follow
```

---

## 添加新的 MCPMark Task

如需添加新的 MCP 服务评测（如 MCPMark 新增了某个 MCP 工具的测试），步骤如下：

### 1. 创建 task 子目录

```
benchmarks/mcpmark/
├── environments/
│   └── mcpmark-newservice/Dockerfile   # 如果需要不同的基础镜像
└── mcpmark-newservice/
    ├── task.yaml
    └── run_mcpmark.py                  # Bridge 脚本
```

### 2. 编写 task.yaml

```yaml
name: "mcpmark-newservice"
command: "python /app/benchmark/run_mcpmark.py"
description: "MCPMark new-service benchmark"
timeout: 3600
# 如果需要 sidecar，在此配置
```

### 3. 编写 bridge 脚本

复制任意现有 `run_mcpmark.py` 并修改 MCPMark pipeline 参数：

```python
cmd = [
    sys.executable, "-m", "pipeline",
    "--mcp", "new_service",             # 修改此处
    "--task-suite", "easy",
    "--tasks", "category/task_name",    # 修改此处
    "--models", model_name,
    "--exp-name", "benchrouter",
    "--output-dir", mcpmark_output_dir,
    "--k", "1",
    "--timeout", "600",
]
```

同时修改结果中的 `mcpmark_config`：

```python
"mcpmark_config": {
    "mcp_service": "new_service",       # 修改此处
    "task_suite": "easy",
    "tasks": "category/task_name",      # 修改此处
    ...
}
```

### 4. 更新 benchmark.yaml

```yaml
tasks:
  - name: mcpmark-fs
    environment: mcpmark-fs
  - name: mcpmark-pg
    environment: mcpmark-pg
  - name: mcpmark-pw
    environment: mcpmark-pw
  - name: mcpmark-pw-webarena
    environment: mcpmark-pw-webarena
  - name: mcpmark-newservice              # 新增
    environment: mcpmark-newservice       # 新增
```

### 5. 重新注册 benchmark

```bash
benchrouter benchmark register --name mcpmark --path benchmarks/mcpmark/
```
