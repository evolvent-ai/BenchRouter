# SWE-bench Pro Benchmark

SWE-bench Pro 是评测 LLM Agent 解决真实软件工程任务的 benchmark，共 731 个任务，覆盖 11 个开源仓库。每个任务给定一个代码库快照和 issue，模型需要生成代码修改来解决问题并通过测试。

本目录将 SWE-bench Pro 适配到 BenchRouter 的 benchmark→task 两层架构。

## 架构设计

**DinD（Docker-in-Docker）模式**：外层容器是轻量的 `python:3.12-slim + Docker CE`，负责调用 LLM 生成 unified diff patch；测试通过 `docker run` 在官方 swebench 镜像（`jefzda/sweap-images:*`）内执行，完全隔离。

```
外层容器（python:3.12-slim + Docker CE）
  ├── 调用 LLM → 生成 unified diff patch
  └── 调用官方 swe_bench_pro_eval.py（--use_local_docker）
        └── docker run jefzda/sweap-images:* → 应用 patch + 运行测试
```

**官方评测**：直接调用 `swe_bench_pro_eval.py`，不重新实现评测逻辑。

## 目录结构

```
benchmarks/swe-bench-pro/
├── benchmark.yaml.template # benchmark 配置模板
├── generate_tasks.py       # 按需生成 N 个任务
├── generate_all_tasks.py   # 生成全部 731 个任务
├── _utils.py               # 共享工具函数
├── default/                # 评测脚本模板
│   ├── task.yaml           # 任务配置模板
│   └── run_swebench.py     # 评测脚本（thin router）
├── environments/           # Docker 环境定义（自动生成，gitignored）
│   └── <task-name>/
│       └── Dockerfile      # DinD 外层容器
├── <task-name>/            # 各 task 目录（自动生成，gitignored）
│   ├── task.yaml
│   ├── run_swebench.py
│   └── instances/
│       └── <instance_id>.json
├── README.md
└── .gitignore
```

> **注意**：`benchmark.yaml`、task 目录和 environments 目录由脚本自动生成，不提交到 git。
> 使用前需要运行 `generate_tasks.py` 生成这些文件。

## 评测流程

### 前置依赖

```bash
uv pip install pandas datasets
```

### 代理配置

构建 Docker 镜像时需要访问 GitHub 和 Hugging Face，如果网络受限，需要配置 Docker 全局代理。

编辑 `/etc/docker/daemon.json`（示例代理地址 `http://192.168.0.188:7890`，替换为你的实际地址）：

```json
{
  "proxies": {
    "http-proxy": "http://192.168.0.188:7890",
    "https-proxy": "http://192.168.0.188:7890",
    "no-proxy": "localhost,127.0.0.1"
  }
}
```

```bash
sudo systemctl restart docker
```

### 子集测试（推荐先跑）

```bash
# 1. 生成任务配置（会自动下载 CSV 数据集）
python benchmarks/swe-bench-pro/generate_tasks.py --count 5 --repo NodeBB

# 2. 注册环境（构建 Docker 镜像）
benchrouter env register --name nodebb-nodebb-04998908 \
  --dockerfile benchmarks/swe-bench-pro/environments/nodebb-nodebb-04998908/Dockerfile
# ... 注册其他 4 个环境

# 3. 注册 benchmark
benchrouter benchmark register --name swe-bench-pro --path benchmarks/swe-bench-pro

# 4. 配置 API 并运行
export BENCHROUTER_MODEL_ENDPOINT='https://api.deepseek.com/v1'
export OPENAI_API_KEY='your-key'

benchrouter run swe-bench-pro --model deepseek-chat --tasks nodebb-nodebb-04998908
```

**注意**：首次运行会自动从 Hugging Face 下载数据集（~23MB CSV）。

### 全量测试（731 个任务）

```bash
python benchmarks/swe-bench-pro/generate_all_tasks.py --pull

benchrouter benchmark register --name swe-bench-pro --path benchmarks/swe-bench-pro
benchrouter run swe-bench-pro --model deepseek-chat
```

## 评分机制

采用官方 SWE-bench Pro 评分标准：

- `fail_to_pass`：原本失败、修复后必须通过的测试
- `pass_to_pass`：原本通过、修复后必须保持通过的测试

当所有测试均通过时，该任务得分为 **1.0**，否则为 **0.0**。

整体分数 = 通过任务数 / 总任务数。

## 实现细节

### 评测脚本（run_swebench.py）

1. 读取任务配置（`instances/<instance_id>.json`）
2. 调用 LLM 生成 unified diff patch
3. 将 patch 传给官方 `swe_bench_pro_eval.py`（`--use_local_docker`）
4. 官方脚本在内层容器中应用 patch 并运行测试
5. 解析官方输出，写入 `result.json`

### Docker 镜像（Dockerfile）

外层容器基于 `python:3.12-slim`，包含 Docker CE 和官方 SWE-bench Pro 工具：

```dockerfile
FROM python:3.12-slim-bookworm

RUN apt-get install -y git docker.io
RUN git clone --depth=1 https://github.com/scaleapi/SWE-bench_Pro-os /app/swe-bench-pro
RUN pip install datasets && python3 -c "from datasets import load_dataset; ..."
RUN pip install -r /app/swe-bench-pro/requirements.txt
RUN pip install litellm openai anthropic
```

### 任务配置（task.yaml）

```yaml
name: "nodebb-nodebb-04998908"
command: "python /app/benchmark/run_swebench.py"
description: "instance_NodeBB__NodeBB-04998908..."
timeout: 3600
docker: true
```

## 依赖

- **官方镜像**：`jefzda/sweap-images:*`（内层容器，包含代码库和测试环境）
- **官方评测脚本**：`swe_bench_pro_eval.py`（从 GitHub 自动克隆）
- **数据集**：`ScaleAI/SWE-bench_Pro`（构建镜像时从 Hugging Face 自动下载）

## 环境变量

- `BENCHROUTER_MODEL_NAME`：模型名称（如 `deepseek-chat`）
- `BENCHROUTER_MODEL_ENDPOINT`：API 端点
- `BENCHROUTER_MAX_TOKENS`：最大 token 数（默认 8192）
- `OPENAI_API_KEY`：API 密钥（litellm 通过此变量传递）

## 磁盘空间说明

### 为什么每个任务容器占用 20GB+？

本实现使用 `--use_local_docker`（官方 Beta 模式），外层容器内部运行一个独立的 Docker daemon。由于外层容器本身使用 overlay2 存储驱动，内层 Docker daemon 无法嵌套使用 overlay2，会自动降级为 **vfs 存储驱动**。

vfs 不做 copy-on-write，每次启动内层容器都会把整个镜像文件系统完整复制一份：

- 内层镜像（`jefzda/sweap-images:*`）压缩约 2GB，vfs 解压后约 8GB
- 每个外层容器各自拉取、各自解压，互不共享
- 8 个任务并发 = 约 160GB 写层

**对比官方推荐方式（Modal）**：官方默认通过 [Modal](https://modal.com) 云沙箱运行内层容器，镜像在云端，本地不占空间。`--use_local_docker` 是官方标注的 Beta 功能，本地空间消耗大是其固有限制。

**对比 WebArena**：WebArena 的外层容器直接运行 Playwright 测试，内层只启动一个长期运行的服务容器（`shopping_admin_final_0719`），所有任务共用同一个内层容器，不存在重复拉取问题。SWE-bench Pro 每个任务需要不同的代码库快照镜像，无法共享。

### 清理方式

每轮跑完后手动清理已停止的容器：

```bash
docker container prune -f
```

建议每次 `benchrouter run` 完成后立即执行，避免磁盘积压。

## 已知限制

- 每个任务需要独立的 Docker 镜像（外层 ~1GB，内层官方镜像 ~2-3GB）
- 使用 `--use_local_docker` 时内层 Docker 降级为 vfs，每个任务容器写层约 20GB
- 首次构建需要拉取官方基础镜像和下载数据集
- 单个任务超时时间较长（默认 3600 秒）
- 官方仓库以 `--depth=1` 克隆，版本随上游变化
