---
title: "OpenEval：为什么我们需要一个新的 Agent 评测平台"
description: "仅 52% 的团队在部署前进行 Agent 评测。OpenEval 用 2 个 YAML 文件和一条命令统一了 8 大基准测试。"
coverImage: "/blog/openeval-cover.svg"
coverImageAlt: "OpenEval 平台架构图，展示基于 Docker 的多基准 Agent 评测体系"
ogImage: "/blog/openeval-cover.svg"
date: "2026-04-10"
lastUpdated: "2026-04-10"
author: "OpenEval Team"
tags: ["AI评测", "LLM基准测试", "Agent测试", "MCP", "OpenEval", "SWE-bench", "WebArena"]
---

# OpenEval：为什么我们需要一个新的 Agent 评测平台

## AI Agent 评测的鸿沟在哪里？

57.3% 的组织已将 AI Agent 部署到生产环境，但只有 52.4% 在上线前做过任何离线评测 ([LangChain State of Agent Engineering](https://www.langchain.com/state-of-agent-engineering), 2025)。这不只是一个数字，它直接解释了 RAND Corporation 报告的 80.3% AI 项目失败率。Agent 悄无声息地出错时，没有基准测试结果可以追溯，只有一个从未执行过的评测步骤。

我们做 **OpenEval** 的原因很简单：现有评测框架逼你二选一，要么用一个只测单一维度的庞大工具包，要么自己拼凑半打各有各格式的 benchmark harness。OpenEval 选了第三条路。它是一个轻量、Docker 原生的评测平台，新增一个 benchmark 的成本是两个 YAML 文件加一个脚本。

本文会讲清楚三件事：为什么要做它，和 VLMEvalKit、Harbor 有什么不同，以及哪些设计决策让它与众不同。

> **核心要点**
> - OpenEval 评测的是 AI **Agent**（不只是模型），覆盖 8 个基准测试，一条 `openeval run` 命令搞定 MCP 工具调用到真实软件工程任务的全流程。
> - 仅 52.4% 的组织对 Agent 进行离线评测 ([LangChain](https://www.langchain.com/state-of-agent-engineering), 2025)。OpenEval 让 benchmark 作者无需编写任何框架代码。
> - 不同于 VLMEvalKit（仅视觉语言）或 Harbor（仅代码 Agent），OpenEval 在一个统一平台上覆盖工具调用、网页交互、软件工程、多模态推理和客服评测。
> - 基于 Docker 的沙箱机制，配合 Sidecar 编排和 Docker-in-Docker 隔离，确保每次评测可复现、安全且互不干扰。

---

## 为什么 AI Agent 评测需要新框架？

AI Agent 市场在 2025 年达到 78.4 亿美元，预计 2030 年将增长至 526.2 亿美元，年复合增长率 46.3% ([MarketsandMarkets](https://www.marketsandmarkets.com/Market-Reports/ai-agents-market-15761548.html), 2025)。Gartner 预测到 2026 年底，40% 的企业应用将嵌入任务专属 AI Agent，而 2025 年这一比例不到 5% ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-08-26-gartner-predicts-40-percent-of-enterprise-apps-will-feature-task-specific-ai-agents-by-2026-up-from-less-than-5-percent-in-2025), 2025)。但 Gartner 同时警告，到 2027 年超过 40% 的 Agentic AI 项目将因评测不足和风险控制不力而被取消。

问题不在于 benchmark 数量不够。2025 年的一项调查梳理了超过 100 个不同的 Agent 评测基准，覆盖网页、编程、科学和对话等领域 ([arXiv:2503.16416](https://arxiv.org/abs/2503.16416), 2025)。光 SWE-bench 就有六个变体。真正的问题是碎片化：每个 benchmark 都有自己的 harness、容器配置和结果格式。跑五个 benchmark 就意味着维护五套独立环境和五套不同的 API。

<!-- [UNIQUE INSIGHT] -->
> **我们的观察：** 团队不做评测，不是因为不在乎，而是因为端到端跑通一个 benchmark 需要好几天的环境搭建。评测的成本不在算力，在工程时间。

这就是 OpenEval 要填补的空白。它不是又一个 benchmark，而是一个**运行 benchmark 的平台**。

[INTERNAL-LINK: AI Agent 评测 → OpenEval 快速入门指南]

![Bar chart showing the AI agent evaluation gap - 57.3% have agents in production but only 52.4% run offline evaluations](./assets/evaluation-gap-chart.svg)
*来源：LangChain State of Agent Engineering, 2025（n=1,340）*

---

## OpenEval 和现有框架相比有什么不同？

在对比架构之前，先直接回答这个问题。下表展示了 OpenEval 与团队最常考虑的两个工具的差异：

| 能力维度 | **OpenEval** | **VLMEvalKit** | **Harbor** |
|---|---|---|---|
| **核心定位** | Agent 评测平台 | 视觉语言模型评测 | 编程 Agent 评测 |
| **评测 Agent** | 是（多轮对话、工具调用） | 否（仅模型层面） | 是（编程任务） |
| **Docker 沙箱** | 是（Simple + DinD + Sidecar） | 否 | 是（容器） |
| **MCP 工具评测** | 是（MCPMark、Toolathlon） | 否 | 否 |
| **Benchmark 接入方式** | YAML（2 文件 + 脚本） | 修改 Python 框架代码 | 基于注册表 |
| **Sidecar 编排** | 是（自动 Docker Compose） | 否 | 否 |
| **多模态评测** | 是（通过 lmms-eval） | 是（核心能力，70+ 基准） | 否 |
| **内置 Benchmark 数量** | 8（多领域） | 70+（全部 VLM） | 3+（编程领域） |
| **云端扩展** | 本地 Docker | 否 | 是（Daytona/Modal） |
| **新增 Benchmark 成本** | 2 YAML 文件 + 1 脚本 | 修改框架代码 | 编写适配器类 |

[INTERNAL-LINK: 框架对比详情 → OpenEval vs VLMEvalKit vs Harbor 深度分析]

### VLMEvalKit 的特点是什么？

VLMEvalKit ([open-compass/VLMEvalKit](https://github.com/open-compass/VLMEvalKit)) 在它擅长的领域表现出色：一条命令评测 220+ 视觉语言模型，覆盖 70+ benchmark。如果你要在 MMMU 或 MathVista 上测一个 VLM，它是正确的选择。

但 VLMEvalKit 评测的是**模型能力**，而非 **Agent 能力**。它无法测试一个 Agent 能否正确使用文件系统工具、在真实网页中导航，或生成一个可运行的代码补丁。此外，VLMEvalKit 在宿主机 Python 环境中运行，没有 Docker 隔离。对于标准化的 VQA 任务来说够用，但对需要数据库、Web 服务器或沙箱化代码执行的 Agent 评测来说就不够了。

OpenEval 采取互补而非竞争的策略。我们通过 lmms-eval benchmark（70+ 图像基准、30+ 视频基准）集成了 VLM 评测能力，让你在获得多模态覆盖的同时不必放弃 Docker 原生的沙箱架构。

### Harbor 的特点是什么？

Harbor ([harbor-framework/harbor](https://github.com/av/harbor)) 专注于评测**编程 Agent**，包括 Claude Code、OpenHands、Codex CLI，使用 Terminal-Bench 和 SWE-Bench 等基准。它在这一领域表现突出：支持 4 到 100+ 任务并发执行，可通过 Daytona 和 Modal 实现云端扩展。

Harbor 做深做窄，OpenEval 做宽做广。我们不仅评测代码生成，还评测调用 MCP 工具的 Agent、浏览网页的 Agent、处理多领域客服查询的 Agent，以及解答加密科学问题的 Agent。更重要的是，OpenEval 的 Sidecar 编排机制允许 benchmark 自动启动 PostgreSQL 数据库、Web 应用和浏览器环境，这是 Harbor 的容器模型无法支持的。

<!-- [ORIGINAL DATA] -->
> **我们的发现：** 在集成 SWE-bench Pro 的过程中，我们发现每个任务需要一个独立的 Docker 守护进程，写入层约 8GB。同时运行 8 个任务就需要约 160GB 的临时存储。这就是为什么 DinD 逐任务隔离不只是锦上添花，而是正确性的刚性需求。

---

## OpenEval 的架构是怎样的？

OpenEval 遵循一个简单原则：**benchmark 作者写脚本，不写插件**。只要一个程序能读取环境变量、输出一个 JSON 文件，它就是合法的 OpenEval 任务。以下是系统的整体结构：

![OpenEval system architecture diagram showing four layers - CLI, FastAPI server, scheduler and registry, and Docker execution with three modes](./assets/architecture-diagram.svg)
*OpenEval v0.4 架构：CLI → FastAPI → Scheduler/Registry → 三种 Docker 执行模式*

架构分为四层：

**用户层** — 顶层是基于 Typer 的命令行工具，通过 HTTP 与服务端通信。`openeval run mcpmark --model gpt-4` 一条命令即可启动完整的 benchmark 评测。

**API 层** — 下方的 FastAPI 暴露了 REST 接口，支持 benchmark 管理、运行提交、任务监控、日志流式输出和跨模型对比。

**核心层** — 系统核心是 **EvalScheduler**，管理两级 Run/Job 层级结构：每个 benchmark 对应一个 Run，每个 Run 包含 N 个并行 Job（每个 task 一个）。信号量控制并发度（默认 8 个任务）。同时，**Registry** 提供基于文件的持久化，无需数据库。

**Docker 执行层** — 每个任务都运行在 Docker 容器中。三种模式应对不同的隔离需求：Simple 模式处理简单评测，Sidecar 模式处理需要数据库或服务的任务，DinD 模式处理自身需要运行容器的任务。

---

## 添加 Benchmark 的最小协议是什么？

这里最能体现 OpenEval 的设计哲学。添加一个 benchmark 只需要三样东西：

**1. `benchmark.yaml`** — 声明 benchmark 名称和任务列表：
```yaml
name: "my-benchmark"
tasks:
  - name: "task-a"
    environment: "my-env"
  - name: "task-b"
    environment: "my-env"
```

**2. `task.yaml`** — 每个任务的配置：
```yaml
name: "task-a"
command: "python eval.py"
timeout: 3600
```

**3. 一个评测脚本** — 任何可执行程序，读取环境变量 `$OPENEVAL_MODEL_ENDPOINT`、`$OPENEVAL_MODEL_NAME` 和 `$OPENAI_API_KEY`，然后写入一个包含 `overall` 分数的 `result.json`。

这就是全部协议。没有基类要继承，没有框架依赖要导入，没有插件注册流程。一个 shell 脚本调用 API 然后写 `{"overall": 0.85}` 就是合法的 benchmark 任务。

<!-- [PERSONAL EXPERIENCE] -->
> **我们集成 MCPMark 时**，评测逻辑本身是一个现有的 Python 脚本。我们写了一个 60 行的桥接脚本，把 OpenEval 的环境变量翻译成 MCPMark 期望的参数。整个集成过程涵盖文件系统、PostgreSQL、Playwright 和 WebArena MCP 服务器四个任务，用了一个下午。

对于需要外部服务的 benchmark，`task.yaml` 支持 **Sidecar** 声明：

```yaml
sidecars:
  - name: "postgres"
    image: "pgvector/pgvector:pg17"
    ports: ["5432:5432"]
    healthcheck:
      test: "pg_isready -U postgres"
      interval: "2s"
      retries: 30
    post_start:
      - "psql -U postgres -c 'CREATE DATABASE testdb;'"
```

OpenEval 动态生成 Docker Compose 文件，等待健康检查通过，执行启动后钩子（比如数据库初始化），任务结束后自动清理。无需手动编排。

[INTERNAL-LINK: Sidecar 配置详情 → OpenEval Sidecar 编排文档]

---

## OpenEval 目前支持哪些 Benchmark？

OpenEval 内置了 8 个集成的基准测试，横跨六个不同的评测领域：

![Donut chart showing OpenEval benchmark coverage across 6 domains - Tool Calling, Web Interaction, Software Engineering, Multimodal, Knowledge, and Customer Service](./assets/benchmark-coverage-donut.svg)
*OpenEval v0.4 支持 6 大评测领域的 8 个基准测试*

以下是每个 benchmark 的评测内容和意义：

**MCPMark** — 多轮 Agent 评测，聚焦 MCP 工具调用。四个任务分别测试文件系统操作、PostgreSQL 查询、Playwright 浏览器控制和 WebArena 交互。值得注意的是，它通过 Sidecar 编排使用真实的数据库和浏览器实例，而非 mock。

**WebArena** — 真实 Web 应用交互评测。Agent 使用 Playwright 在 Magento CMS 实例上导航（182 个子任务）。当前 SOTA 任务完成率为 61.7% ([EmergentMind](https://www.emergentmind.com/topics/webarena-benchmark), 2025)，相比发布时的 14% 提升了 4.3 倍。OpenEval 使用 Docker-in-Docker 来托管 9.6GB 的购物管理后台镜像。

**SWE-bench Pro** — 长周期代码生成评测，覆盖 11 个仓库中的 731 个真实 GitHub issue。与原版 SWE-bench Verified 不同，SWE-bench Pro 专门设计来抵抗数据污染。这一点至关重要，因为 OpenAI 的审计发现所有前沿模型的训练数据中都存在重叠。目前顶级 Agent 在 SWE-bench Pro 上的得分约 57%，远低于受污染的 Verified 集上的约 77% ([MorphLLM](https://www.morphllm.com/swe-bench-pro), 2026)。

**lmms-eval** — 多模态评测，覆盖 70+ 图像基准和 30+ 视频基准（包括 MMMU）。直接集成 lmms-eval Python API，并对 Anthropic API 兼容性做了运行时修补。

**tau2-bench** — 客服 Agent 评测，覆盖航空、零售、电信、银行和知识型任务五个领域。值得一提的是，即便是 GPT-4o 在这些任务上的成功率也不到 50%，充分说明了真实服务交互的难度。

**xbench** — 防污染 ScienceQA 评测，使用加密数据集。专门测试知识完整性，杜绝 benchmark 泄露风险。

**Toolathlon** — 基于 MCP Server 的工具调用评测，无需任何外部凭证。通过 Docker-in-Docker 隔离在本地完整运行。

**BrowseComp** — OpenAI simple-evals 套件中的浏览器导航评测，专注测试 Agent 的网页浏览能力。

---

## 哪些设计原则让 OpenEval 与众不同？

五条核心原则贯穿了每一个设计决策：

**1. Docker 是唯一的运行时契约。** 没有 Python 版本要求，没有特定 ML 框架依赖，不需要配置 Ray 集群。能在容器里跑的程序，就能在 OpenEval 上跑。这从根本上消除了困扰 benchmark 可复现性的"在我机器上能跑"问题。

**2. 模型是外部的。** OpenEval 不托管模型，只评测模型。模型运行在 OpenAI 兼容 API 后面（vLLM、OpenAI、Anthropic、DeepSeek 或任何代理）。这种解耦意味着你可以评测任何模型，无需修改评测平台。

**3. 基于文件的存储，无需数据库。** Benchmark 是 tar.gz 归档，结果是 JSON 文件，索引是追加写入的 JSONL。不需要维护 PostgreSQL，不需要跑 migration。`ls` 和 `jq` 就是你的调试工具。

**4. 层级化执行。** 一个 benchmark 产生一个 Run，包含 N 个并行 Job。失败的任务不会阻塞成功的任务。完成了 8 个任务中 7 个的 Run 会返回 PARTIAL 状态，并汇总已成功任务的分数。部分失败的结果仍然有价值。

**5. 按需构建环境。** 在 benchmark 目录中放一个 `Dockerfile`，OpenEval 会自动发现它，首次使用时构建镜像，后续运行直接使用缓存。不需要手动 `docker build`，不需要维护镜像仓库。

![Lollipop chart comparing benchmark integration complexity - OpenEval requires 2 YAML files while others need framework modifications](./assets/benchmark-complexity-lollipop.svg)
*各评测框架接入新 benchmark 的复杂度对比*

---

## OpenEval 如何保证可复现性与隔离性？

Docker 在开发者中的采用率在 2025 年达到 71.1%，这是 Stack Overflow 开发者调查中所有技术的最大单年涨幅 ([Stack Overflow](https://survey.stackoverflow.co/2025), 2025)。在 AI 工具用户中，这一比例更是高达 75%。Docker 对 Agent 评测来说不仅方便，更是可复现性的标准。

OpenEval 完全拥抱了这一点，提供三个隔离级别：

**Simple 模式** — 一个容器跑一个任务。Benchmark 目录以只读方式挂载在 `/app/benchmark`，结果写入 `/app/results`。默认资源限制为 16GB 内存和 4 个 CPU 核心。xbench、BrowseComp 和 lmms-eval 使用此模式。

**Sidecar 模式** — 对于更复杂的场景，OpenEval 根据 `task.yaml` 中的 sidecar 声明动态生成 Docker Compose 文件。服务带健康检查启动，运行启动后初始化脚本（数据库种子数据、配置刷新），任务结束后自动清理。MCPMark 和 tau2-bench 使用此模式。

**DinD 模式** — 最深层的隔离级别。它处理的是任务本身需要运行容器的 benchmark（例如 SWE-bench Pro 为每个 instance 生成 Docker 镜像，WebArena 需要托管 9.6GB 的 Web 应用镜像）。每个任务通过 Sysbox 运行时或 privileged 模式获得独立的 Docker 守护进程。预缓存的镜像从 tar 归档加载，避免重复拉取。DinD 容器默认分配 32GB 内存。

无论哪种模式，每个容器都能获得相同的环境变量：`OPENEVAL_MODEL_ENDPOINT`、`OPENEVAL_MODEL_NAME`、`OPENAI_API_KEY` 和 `OPENEVAL_OUTPUT_DIR`。容器内的 SDK runner 读取 `task.yaml`，执行命令，验证结果 JSON 并标准化输出。如果结果文件使用了非标准字段名（比如 `pass@1` 而非 `overall`），`result_mapping` 配置会处理转换，无需改代码。

---

## OpenEval 未来有什么规划？

MCP 生态正在快速增长。月度 SDK 下载量已超过 9,700 万次，超过 10,000 个活跃的 MCP Server 部署在 Claude、ChatGPT、Cursor、Gemini、VS Code 和 Microsoft Copilot 等平台 ([Pento AI](https://www.pento.ai/blog/a-year-of-mcp-2025-review), 2025)。2025 年 12 月，Anthropic 将 MCP 捐赠给 Linux Foundation 的 Agentic AI Foundation，表明 MCP 已成为长期标准。

随着 MCP 成为 Agent 与工具交互的标准协议，严格的 MCP 工具评测需求只会越来越强。OpenEval 已经通过 MCPMark 和 Toolathlon 做好了布局，但我们正积极向多个方向拓展。

**近期优先事项** 包括集成更多 MCP 专项 benchmark，比如 MCP-Universe（Salesforce 的 6 领域、11 服务器 benchmark，即便 GPT-5 也只得到 43.7%）和 GAIA（466 个测试多步推理和工具调用的真实问题）。此外，我们在推进云原生扩展，支持跨分布式 Docker 主机的大规模评测运行，以及跨模型对比排行榜系统。

<!-- [UNIQUE INSIGHT] -->
我们也在探索 **RL 奖励集成**，把 OpenEval 标准化的结果格式与强化学习流水线对接，让 benchmark 分数直接作为模型微调的训练信号。换句话说，评测不再只是一份报告，而是成为训练循环的一部分。

欢迎贡献。如果你构建了一个 Agent benchmark 并想集成进来，协议设计上是刻意从简的。有明确评测指标的项目成功率达 54%，而没有指标的仅为 12% ([RAND Corporation, via Pertama Partners](https://www.pertamapartners.com/insights/ai-project-failure-statistics-2026), 2025)。OpenEval 的使命是让评测步骤足够轻量，让"没时间做评测"不再成为借口。

[INTERNAL-LINK: 贡献指南 → OpenEval Benchmark 接入文档]

---

## 常见问题

### OpenEval 能评测所有 LLM 还是只支持特定供应商？

OpenEval 支持任何部署在 OpenAI 兼容 API 端点后面的模型。包括 OpenAI、Anthropic（通过代理）、DeepSeek、vLLM 自托管模型和自定义推理服务。平台只负责调度评测，不托管模型。根据 [Stanford HAI AI Index](https://hai.stanford.edu/ai-index/2025-ai-index-report) (2025) 的数据，排名第一和第十的模型之间的差距已缩小到仅 5.4%，这使得跨供应商多模型评测变得越来越重要。

### OpenEval 和直接运行 SWE-bench 或 WebArena 有什么区别？

直接运行 SWE-bench 需要 120GB+ 存储、x86_64 硬件，以及为每个任务手动配置 Docker。OpenEval 将 SWE-bench Pro（731 个任务）封装为自动化的 DinD 隔离、并行执行（默认 8 路并发）和结果汇总。你只需一条 `openeval run swe-bench-pro --model gpt-4` 命令，替代原来的多步手动流程。WebArena 和所有其他内置 benchmark 同理。

### OpenEval 适合生产监控还是仅用于离线评测？

OpenEval 的设计定位是离线评测，即针对标准化任务集的可控、可复现评测。对于生产监控（在线评测），LangChain 2025 年调查发现即便已经有 Agent 在生产运行的组织中，也只有 37.3% 进行在线评测。OpenEval 首先解决离线评测的空白，这是大多数团队需要迈出的第一步。

### 如何将自定义 Benchmark 添加到 OpenEval？

编写一个 `benchmark.yaml` 列出任务，每个任务一个 `task.yaml` 指定命令和环境，再加一个脚本读取环境变量并写入 `result.json`。如需自定义依赖，在 `environments/` 中放一个 Dockerfile，OpenEval 在首次运行时自动构建镜像。协议有意设计得尽可能精简。如果你的评测逻辑已经以脚本形式存在，为 OpenEval 做适配通常只需几十行代码。

### 能只运行 Benchmark 中的部分任务吗？

可以。使用 `openeval run <benchmark> --tasks task-a,task-b` 选择子集，只有指定的任务会被调度。结果仍然在 benchmark 层面汇总你所选择的任务。

---

## 写在最后

AI Agent 市场正以 2030 年 520 亿美元的规模飞速前进，但超过 40% 的 Agentic AI 项目因缺乏评测基础设施面临取消风险 ([Gartner](https://www.gartner.com/en/newsroom/press-releases/2025-06-25-gartner-predicts-over-40-percent-of-agentic-ai-projects-will-be-canceled-by-end-of-2027), 2025)。评测鸿沟的本质不是工具问题，而是协议问题。现有框架要么只测单一维度（VLMEvalKit 的视觉语言方向、Harbor 的代码生成方向），要么需要重量级集成（AgentBench 的硬编码环境）。

OpenEval 提供了第三条路：一个轻薄的平台层，**Docker 是唯一契约**，**benchmark 是脚本而非插件**。这意味着什么？今天已有八个 benchmark 横跨六大领域，一套基于 YAML 的协议让第九个 benchmark 的接入只需一个下午，三种 Docker 执行模式（Simple、Sidecar、DinD）灵活到能应对从加密知识测试到 9.6GB Web 应用镜像的一切场景。

那么，值得问一个问题：如果你的团队在没有标准化评测的情况下部署 Agent，第一次无声故障的代价是多少？RAND 的数据表明这个代价很高。而用 OpenEval 做评测的成本是两个 YAML 文件、一个脚本和一条 CLI 命令。

**快速开始：**

```bash
pip install openeval
openeval-server --host 0.0.0.0 --port 9000
openeval run mcpmark --model your-model-name
```

在 GitHub 上关注和贡献：[github.com/evolvent-ai/OpenEval](https://github.com/evolvent-ai/OpenEval)
