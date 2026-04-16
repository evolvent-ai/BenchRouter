# 如何添加 Benchmark（v0.4）

本文档介绍如何在 BenchRouter v0.4 中添加新的 Benchmark。

---

## 架构总览

BenchRouter 采用两级配置结构：

```
benchmarks/my-bench/
├── benchmark.yaml          # Benchmark 级配置（声明包含哪些 task）
├── environments/           # Dockerfile 存放目录（按环境名组织）
│   ├── env-a/Dockerfile    # 环境 env-a 的 Dockerfile
│   └── env-b/Dockerfile    # 环境 env-b 的 Dockerfile（如果不同 task 需要不同环境）
├── task-a/                 # Task 子目录（目录名 = task 名）
│   ├── task.yaml           # Task 级配置（命令、超时、sidecar 等）
│   └── eval.py             # 评测脚本
└── task-b/
    ├── task.yaml
    └── eval.py
```

**核心原则：**

- `benchmark.yaml` 只做声明——定义 benchmark 名称和包含的 task 列表
- 每个 task 是一个独立子目录，包含 `task.yaml` 和评测脚本；Dockerfile 统一放在 `environments/{env_name}/` 下
- 需要 Docker 的 task 声明 `docker: true`，通过 DinD 在隔离的 daemon 中运行
- Task 在各自的 Docker 容器中并行执行，互不影响
- 结果由 scheduler 自动聚合（各 task overall 分数取平均）

**执行流程：**

```
benchrouter run my-bench --model gpt-4
    │
    ▼
  CLI → POST /evals/submit
    │
    ▼
  Scheduler 创建 Run（父级）+ N 个 Job（每个 task 一个）
    │
    ▼
  并行执行：每个 Job 在独立 Docker 容器中运行
    │  容器入口：python -m benchrouter.sdk.runner
    │  runner 读取 task.yaml → 执行 command → 标准化 result.json
    │
    ▼
  所有 Job 完成后，Scheduler 聚合结果
    │  overall = avg(各 task 的 overall)
    │
    ▼
  CLI 轮询 /runs/{run_id} 直到完成，显示最终结果
```

---

## MCPMark 集成示例（Bridge 模式）

MCPMark 是一个独立的评测框架。BenchRouter 通过 **Bridge 脚本** 将其接入，而非修改 MCPMark 源码。

```
benchmarks/mcpmark/
├── benchmark.yaml              # 声明 4 个 task
├── environments/
│   ├── mcpmark-fs/Dockerfile
│   ├── mcpmark-pg/Dockerfile
│   ├── mcpmark-pw/Dockerfile
│   └── mcpmark-pw-webarena/Dockerfile
├── mcpmark-fs/
│   ├── task.yaml               # command 指向 bridge 脚本
│   └── run_mcpmark.py          # Bridge：BenchRouter 环境变量 → MCPMark 参数
├── mcpmark-pg/
│   ├── task.yaml               # docker: true + PostgreSQL 启动
│   └── run_mcpmark.py
└── ...
```

Bridge 脚本的职责：

1. 读取 BenchRouter 环境变量（`BENCHROUTER_MODEL_ENDPOINT`、`BENCHROUTER_MODEL_NAME`、`OPENAI_API_KEY`）
2. 转换为目标框架的参数格式（如 MCPMark 通过 `OPENAI_API_BASE` + LiteLLM 协议）
3. 调用目标框架的 pipeline
4. 收集目标框架的输出，转换为 BenchRouter 格式的 `result.json`（必须包含 `overall` 字段）
5. 写入 `$BENCHROUTER_OUTPUT_DIR/result.json`

---

## 场景一：简单脚本 Benchmark

最简场景——一个 Python 脚本直接调用模型 API 并计算分数。

### 1. 创建目录

```
benchmarks/my-bench/
├── benchmark.yaml
├── environments/
│   └── my-bench/Dockerfile
└── default/
    ├── task.yaml
    └── eval.py
```

### 2. 编写 benchmark.yaml

```yaml
name: "my-bench"
description: "My custom benchmark"
version: "1.0.0"
tasks:
  - name: default
    environment: my-bench    # 使用的 Docker environment 名称
```

### 3. 编写 task.yaml

```yaml
name: "my-bench-default"
command: "python eval.py"
description: "My custom evaluation task"
```

### 4. 编写评测脚本 eval.py

评测脚本需遵循以下契约：

- **输入**：从环境变量读取模型配置
  - `BENCHROUTER_MODEL_ENDPOINT` — 模型 API 地址（OpenAI 兼容格式）
  - `BENCHROUTER_MODEL_NAME` — 模型名
  - `OPENAI_API_KEY` — API Key
  - `BENCHROUTER_OUTPUT_DIR` — 结果写入目录
- **输出**：向 `$BENCHROUTER_OUTPUT_DIR/result.json` 写入 JSON，必须包含 `overall` 字段（0-1 浮点数）

```python
import os
import json
from openai import OpenAI

def main():
    client = OpenAI(
        base_url=os.environ["BENCHROUTER_MODEL_ENDPOINT"],
        api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
    )
    model_name = os.environ.get("BENCHROUTER_MODEL_NAME", "default")
    output_dir = os.environ["BENCHROUTER_OUTPUT_DIR"]

    # 你的评测逻辑
    questions = [
        {"prompt": "What is 1 + 1?", "answer": "2"},
        {"prompt": "What is 2 * 3?", "answer": "6"},
    ]

    correct = 0
    for q in questions:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": q["prompt"]}],
            temperature=0,
        )
        if q["answer"] in resp.choices[0].message.content:
            correct += 1

    # 写 result.json — 必须有 "overall" 字段
    result = {
        "overall": correct / len(questions),
        "correct": correct,
        "total": len(questions),
    }
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)

if __name__ == "__main__":
    main()
```

### 5. 编写 Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install openai pyyaml
```

> **提示**：Dockerfile 必须放在 `environments/{env_name}/` 目录下（例如 `environments/my-bench/Dockerfile`）。scheduler 会自动查找该路径下的 Dockerfile 进行按需构建。

### 6. 注册并运行

```bash
# 注册环境
benchrouter env register --name my-bench --dockerfile benchmarks/my-bench/environments/my-bench/Dockerfile

# 注册 benchmark
benchrouter benchmark register --name my-bench --path benchmarks/my-bench/

# 运行
benchrouter run my-bench --model your-model-name
```

---

## 场景二：包装已有 Benchmark

如果已有一个评测脚本（如 HumanEval），只需做两处最小改动，并通过 `result_mapping` 处理字段名不一致。

### 1. 最小改动

原始代码通常需要修改两行：

```python
# 改前：MODEL_URL = "http://localhost:8000/v1"
# 改后：
MODEL_URL = os.environ.get("BENCHROUTER_MODEL_ENDPOINT", "http://localhost:8000/v1")
OUTPUT_DIR = os.environ.get("BENCHROUTER_OUTPUT_DIR", "./output")
```

### 2. 处理字段名不匹配

如果已有脚本输出 `scores.json` 且使用 `pass@1` 而非 `overall`：

```json
{"pass@1": 0.85, "pass@10": 0.95, "total_tasks": 100}
```

在 `task.yaml` 中配置 `result_file` 和 `result_mapping`：

```yaml
name: "wrapped-bench-default"
command: "python existing_benchmark.py"
result_file: "scores.json"          # 告诉 runner 读哪个文件（默认 result.json）
result_mapping:
  overall: "pass@1"                 # 将 "pass@1" 映射到 BenchRouter 的 "overall"
```

**runner 的处理逻辑：**

1. 执行 `command`
2. 读取 `$BENCHROUTER_OUTPUT_DIR/{result_file}`
3. 用 `result_mapping.overall` 指定的 key 提取 overall 分数
4. 生成标准化的 `result.json`（包含 `overall`、`raw_metrics`、`benchmark_name`、`model_name`）

### 3. 完整目录结构

```
benchmarks/wrapped-bench/
├── benchmark.yaml
├── environments/
│   └── wrapped-bench/Dockerfile
└── default/
    ├── task.yaml                   # 配置 result_file + result_mapping
    └── existing_benchmark.py       # 最小改动的原始脚本
```

---

## 场景三：需要 Docker 的 Benchmark（DinD 模式）

某些评测需要在容器内运行 Docker（例如启动数据库、Web 应用等）。BenchRouter 通过 Docker-in-Docker（DinD）提供隔离的 Docker daemon，让原始 benchmark 代码的 `docker run/stop/rm` 直接运行，零修改。

### 1. 在 task.yaml 中声明 `docker: true`

```yaml
name: "mcpmark-pg"
command: "python /app/benchmark/run_mcpmark.py"
timeout: 7200
docker: true
docker_images:
  - "pgvector/pgvector:0.8.0-pg17-bookworm"
extra_env:
  POSTGRES_HOST: "localhost"
  POSTGRES_PORT: "5432"
  POSTGRES_USERNAME: "postgres"
  POSTGRES_PASSWORD: "password"
  POSTGRES_DATABASE: "postgres"
```

- `docker: true` — 启用 DinD 模式，容器内会启动独立的 Docker daemon
- `docker_images` — 声明需要的 Docker 镜像（文档用途，实际通过镜像缓存加载）

### 2. 执行原理

当 scheduler 检测到 `docker: true` 时：

1. 使用 Sysbox runtime（如可用）或 `--privileged` 模式启动容器
2. 容器入口脚本 `dind-entrypoint.sh` 启动内部 Docker daemon
3. 从 `/var/lib/docker-cache/` 加载预缓存的镜像 tar 包
4. 执行 `python -m benchrouter.sdk.runner` 运行评测
5. 评测脚本（bridge 脚本）可以自由使用 `docker run/stop/rm` 等命令

### 3. 镜像缓存

对于大镜像（如 9.6GB 的 `shopping_admin`），在宿主机上预存 tar 包以避免每次运行都下载：

```bash
# 在宿主机上执行
docker save pgvector/pgvector:0.8.0-pg17-bookworm > /data/benchrouter/image-cache/pgvector_pgvector_0.8.0-pg17-bookworm.tar
docker save shopping_admin_final_0719 > /data/benchrouter/image-cache/shopping_admin_final_0719.tar
```

这些 tar 包会以只读方式挂载到容器的 `/var/lib/docker-cache/`，DinD 入口脚本会自动加载。

### 4. Dockerfile 要求

需要 DinD 的环境 Dockerfile 必须安装 Docker CE：

```dockerfile
FROM fanqingm/mcpmark:latest

# Add Docker CE for DinD
RUN apt-get update && apt-get install -y ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu jammy stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*
```

### 5. extra_env 传递给评测容器

`extra_env` 中的变量会注入到评测容器的环境变量中：

```yaml
extra_env:
  POSTGRES_HOST: "localhost"
  POSTGRES_PORT: "5432"
```

> **注意**：DinD 模式下所有服务运行在同一个容器内，使用 `localhost` 而非服务名。

---

## benchmark.yaml 完整 Schema

```yaml
# 必填
name: "benchmark-name"        # Benchmark 唯一标识符
tasks:                        # Task 列表（至少一个）
  - name: "task-name"         # Task 名称，必须对应一个同名子目录
    environment: "env-name"   # Docker environment 名称（用于查找/构建镜像）

# 可选
description: "..."            # 描述信息
version: "1.0.0"              # 版本号
```

**验证规则（registry.py）：**

- 必须存在 `tasks` 列表且非空
- 每个 task 必须有 `name` 字段
- 每个 task 的 `name` 必须对应一个包含 `task.yaml` 的子目录

---

## task.yaml 完整 Schema

```yaml
# 必填
name: "task-name"             # Task 唯一标识符
command: "python eval.py"     # 在容器内执行的命令（shell 模式）

# 可选
description: "..."            # 描述信息
timeout: 7200                 # 超时时间（秒），默认 7200（2 小时）
result_file: "result.json"    # 结果文件名，默认 "result.json"
result_mapping:               # 字段名映射
  overall: "pass@1"           # 将原始结果中的 "pass@1" 映射为 BenchRouter 的 "overall"

# Docker-in-Docker（可选）
docker: true                  # 启用 DinD 模式，容器内可使用 docker 命令
docker_images:                # 声明需要的 Docker 镜像（文档 + 缓存用途）
  - "pgvector/pgvector:0.8.0-pg17-bookworm"

extra_env:                    # 额外注入到评测容器的环境变量
  CUSTOM_KEY: "value"
```

---

## result_mapping 详解

BenchRouter 的 runner（`benchrouter/sdk/runner.py`）在容器内执行以下流程：

1. 读取 `task.yaml`
2. 执行 `command`
3. 从 `$BENCHROUTER_OUTPUT_DIR/{result_file}` 读取原始结果 JSON
4. 用 `result_mapping.overall` 找到 overall 分数的字段名（默认就是 `"overall"`）
5. 生成标准化 `result.json`：

```json
{
  "benchmark_name": "task-name",
  "model_name": "gpt-4",
  "overall": 0.85,
  "raw_metrics": { /* 原始结果的完整内容 */ }
}
```

如果原始结果的 key 就是 `overall`，则不需要配置 `result_mapping`。

---

## 容器内环境变量

评测脚本在容器内可以使用以下由 scheduler 自动注入的环境变量：

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `BENCHROUTER_BENCHMARK_DIR` | benchmark 目录挂载路径 | `/app/benchmark` |
| `BENCHROUTER_MODEL_ENDPOINT` | 模型 API 地址 | `https://api.openai.com/v1` |
| `BENCHROUTER_MODEL_NAME` | 模型名称 | `gpt-4` |
| `BENCHROUTER_OUTPUT_DIR` | 结果输出目录 | `/app/results` |
| `OPENAI_API_KEY` | API Key | `sk-...` |
| `PYTHONPATH` | Python 路径 | `/app` |
| `BENCHROUTER_DOCKER_ENABLED` | DinD 模式标志（仅 `docker: true` 时注入） | `true` |

此外，`task.yaml` 中的 `extra_env` 也会注入。

---

## 容器内卷挂载

| 主机路径 | 容器路径 | 模式 | 说明 |
|----------|----------|------|------|
| task 子目录 | `/app/benchmark` | 只读 | 评测脚本和 task.yaml |
| 结果目录 | `/app/results` | 读写 | 评测结果写入 |
| SDK 目录 | `/app/benchrouter/sdk` | 只读 | runner.py（容器入口） |
| 镜像缓存目录 | `/var/lib/docker-cache` | 只读 | DinD 模式下的预缓存镜像（仅 `docker: true`） |

---

## Checklist

添加新 benchmark 前，请逐项确认：

### 目录结构

- [ ] 存在 `benchmark.yaml`
- [ ] 每个 task 有对应的子目录
- [ ] 每个 task 子目录中有 `task.yaml`
- [ ] 存在 Dockerfile（位于 `environments/{env_name}/Dockerfile`）

### benchmark.yaml

- [ ] `name` 字段已设置
- [ ] `tasks` 列表非空
- [ ] 每个 task 有 `name` 和 `environment`
- [ ] task 的 `name` 与子目录名完全一致

### task.yaml

- [ ] `name` 字段已设置
- [ ] `command` 字段已设置，命令在容器内可执行
- [ ] 如果需要容器内使用 Docker，设置了 `docker: true` 且 Dockerfile 安装了 Docker CE
- [ ] 如果结果文件名不是 `result.json`，配置了 `result_file`
- [ ] 如果结果字段名不是 `overall`，配置了 `result_mapping`

### 评测脚本

- [ ] 从 `BENCHROUTER_MODEL_ENDPOINT` 读取模型地址
- [ ] 从 `BENCHROUTER_MODEL_NAME` 读取模型名
- [ ] 从 `OPENAI_API_KEY` 读取 API Key
- [ ] 结果写入 `$BENCHROUTER_OUTPUT_DIR/result.json`（或配置的 `result_file`）
- [ ] 结果 JSON 包含 `overall` 字段（或通过 `result_mapping` 映射）
- [ ] `overall` 是 0-1 之间的浮点数

### Dockerfile

- [ ] 基于合适的基础镜像
- [ ] 安装了评测脚本所需的所有依赖
- [ ] `WORKDIR` 设为 `/app`

### 运行验证

- [ ] `benchrouter env register` 成功且 status 为 ready
- [ ] `benchrouter benchmark register` 成功
- [ ] `benchrouter benchmark list` 能看到新 benchmark 和 task 列表
- [ ] `benchrouter run <name> --model <model>` 能跑通并输出结果
