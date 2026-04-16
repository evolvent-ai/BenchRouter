# Toolathlon Local MCP 接入说明

## 目标

将 [Toolathlon](https://github.com/hkust-nlp/Toolathlon) 作为一个 **自托管、local MCP 优先** 的 benchmark 接入 BenchRouter，并尽量把复杂依赖收敛到单个环境镜像里。

当前接入目录：`benchmarks/toolathlon-local/`

## 当前设计

- `benchmarks/toolathlon-local/environments/toolathlon-local/Dockerfile`
  - 构建 Toolathlon 运行环境镜像
  - 在镜像内 clone Toolathlon 仓库并安装依赖
  - 安装完整 Docker CE（`dockerd` + `docker-ce` + `containerd.io`），供 DinD 运行
- `benchmarks/toolathlon-local/benchmark.yaml`
  - 声明 benchmark 元信息
  - 绑定单个 task：`toolathlon-local`
- `benchmarks/toolathlon-local/toolathlon-local/task.yaml`
  - 声明 task 入口、超时和 `docker: true`
  - 让 Scheduler 以 DinD 方式启动评测容器
- `benchmarks/toolathlon-local/toolathlon-local/run_toolathlon.py`
  - 桥接 BenchRouter 和 Toolathlon
  - 把 `BENCHROUTER_MODEL_ENDPOINT` / `OPENAI_API_KEY` 映射为 `TOOLATHLON_OPENAI_*`
  - 运行 Toolathlon smoke task
  - 将 Toolathlon 输出转换为 BenchRouter 所需的 `toolathlon_result.json`
  - 结果直接写到容器内 `/app/results/toolathlon`
- `benchmarks/toolathlon-local/toolathlon-local/tasks/smoke.txt`
  - 默认 smoke task 列表，目前只包含 `find-alita-paper`

## 为什么切到 DinD

Toolathlon 的 `scripts/run_single_containerized.sh` 和 `run_parallel.py --runner containerized` 会在当前容器里再次调用 Docker，启动任务执行容器。

因此 BenchRouter 需要：

- 先启动一个 `--privileged` 的 Toolathlon eval 容器
- 用 `benchrouter/sdk/dind-entrypoint.sh` 在容器内拉起 `dockerd`
- 让 Toolathlon 在容器内继续创建它自己的 task 容器

对应 task 只需声明：

```yaml
docker: true
```

这样 nested container 使用的路径就是评测容器自己的路径，不再需要 DooD 下那套 host path 映射和结果目录 symlink 兼容。

## 默认 smoke 流程

默认配置优先跑一个最小的 local MCP task：

- Task folder: `finalpool`
- Task list: `toolathlon-local/tasks/smoke.txt`
- 当前 smoke task: `find-alita-paper`

之所以先选它，是因为这是 Toolathlon README 里的 quickstart task，且依赖的 MCP servers 偏本地：

- `arxiv_local`
- `filesystem`
- `scholarly`

## 推荐接入顺序

### 1. 构建并注册环境

```bash
benchrouter env register --name toolathlon-local --dockerfile benchmarks/toolathlon-local/environments/toolathlon-local/Dockerfile
benchrouter env status --name toolathlon-local
```

等到状态变成 `ready`。

说明：Toolathlon 环境镜像较重，首次构建会执行 `uv sync`、`npm install` 和 `playwright install chromium`，本地机器上可能需要数分钟。

### 2. 注册 benchmark

```bash
benchrouter benchmark register --name toolathlon-local --path benchmarks/toolathlon-local/
```

### 3. 跑 smoke test

```bash
export BENCHROUTER_MODEL_ENDPOINT=http://your-openai-compatible-endpoint/v1
export OPENAI_API_KEY=dummy-or-real-key

benchrouter run toolathlon-local --model your-model-name
```

### 4. 查看结果

Toolathlon 原始输出会在结果目录下保留：

- `/app/results/toolathlon/`
- `/app/results/toolathlon_result.json`

BenchRouter 最终标准化结果会在：

- `/app/results/result.json`

## 当前边界

这是第一阶段的 **smoke 接入**，不是 Toolathlon 全量支持：

- 当前默认只覆盖 local MCP 友好的最小任务子集
- 当前镜像优先保证 smoke task 所需依赖可用
- 如需扩更多任务，可继续往环境 Dockerfile 里补 Toolathlon 官方镜像中的额外工具和本地 server 依赖

## 后续扩展建议

- 扩展 `toolathlon-local/tasks/smoke.txt` 为更多 local MCP tasks
- 支持通过 `extra_env` 切换 `TOOLATHLON_TASK_LIST_FILE`
- 如果需要 sidecar / 非 DinD 的运行模式，再给 BenchRouter 增加更通用的环境编排配置
