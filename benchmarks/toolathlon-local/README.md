# Toolathlon Local Benchmark

这是 Toolathlon 的 BenchRouter 接入骨架，目标是先跑通 local MCP smoke task，再逐步扩成更完整的任务集。

- 环境镜像：`environments/toolathlon-local/Dockerfile`
- benchmark manifest：`benchmark.yaml`
- task manifest：`toolathlon-local/task.yaml`
- 桥接脚本：`toolathlon-local/run_toolathlon.py`
- 默认任务集：`toolathlon-local/tasks/smoke.txt`

当前任务已切到 BenchRouter 的 DinD 模式，在 `task.yaml` 里通过 `docker: true` 声明，运行时由 `dind-entrypoint.sh` 在评测容器内启动 `dockerd`。

更多设计说明见 `docs/toolathlon-integration.md`。

> 本地首次构建时间会比较长；环境 Dockerfile 已兼容 `amd64` / `arm64`，适合在 Apple Silicon 上直接测试。
