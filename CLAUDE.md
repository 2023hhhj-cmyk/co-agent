# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **最后更新**：2026-10-06（按当前本地代码核对；不代表服务器已更新）

## 项目概述

多Agent协同系统 - 一人公司运营平台，融合 **MetaGPT 角色机制** + **ChatDev 图工作流**。

核心业务：为中小企业打造Agent平台，自动化运营流程。完整产品规格见 `docs/product.md`。

## 服务器与部署

- SSH 别名：`muzhu-ai`；约定服务器目录：`/root/muzhu-ai/`。
- `./scripts/deploy.sh` 同步文件和 Skills、创建 venv、安装依赖、执行 `scripts/init_db.sql` 并生成 `start.sh`；**不会自动启动或重启服务**。
- 脚本不上传 `.env`，服务器需单独配置环境变量。数据库初始化读取远端 SSH 会话中的 `PGPASSWORD`；只在本地设置不会被当前脚本自动转发。
- 启动：在服务器执行 `cd /root/muzhu-ai && ./start.sh`；当前入口为端口 8080 的单进程 `uvicorn.run`。
- 更新已运行服务时需确认对应服务进程并重启，再检查健康端点；脚本成功输出不能代替运行验证。
- 最近一次部署因 SSH 主机密钥不匹配未完成。需通过可信渠道核对新密钥；本文仅确认本地代码，不确认服务器版本。

## 常用命令

```bash
source venv/bin/activate
python main.py                       # 启动 FastAPI（0.0.0.0:8080）
pip install -r requirements.txt
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_cs_db.sql  # 客服独立 DB

pytest                                # 全部测试（含覆盖率，见 P3 #26）
pytest tests/test_p1_fixes.py         # 按阶段跑
pytest                              # 覆盖率报告 → tests/artifacts/coverage/
```

## 环境变量

模板 `.env.example`。**生产必设**：

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | LLM 调用 |
| `FEISHU_APP_SECRET` | 飞书 webhook 签名验证（P0 #6 后强制）|
| `WECOM_*_SECRET` / `WECOM_*_AES_KEY` | 企业微信解密/签名 |
| `REDIS_URL` / `DATABASE_URL` | 持久化与状态 |
| `WORKSPACE_MAX_CONCURRENT_TASKS=5` | 工作流节点与客服处理共享并发上限（单进程） |
| `LLM_RPM_LIMIT=60` / `LLM_TPM_LIMIT=60000` | LLMClient 统一滚动 60 秒请求 / Token 配额 |
| `LLM_MAX_RETRIES=3` / `LLM_RETRY_BASE_DELAY=1` / `LLM_RETRY_MAX_DELAY=30` | LLMClient 统一临时错误指数退避重试；服务端 Retry-After 优先 |
| `ANALYTICS_VERBOSE=1` | 埋点 stderr 镜像（调试用）|
| `ANALYTICS_LOG_PATH=...` | 自定义埋点日志路径（默认 `./analytics/logs/analytics_events.log`）|

## 架构

```
YAML 业务阶段模板 → CEO 动态拆解任务和依赖 → 用户确认（IM 入口）
    ↓
WorkflowGraph → WorkflowExecutor（拓扑排序，分层并行）
    ↓
Agent 执行层 (MetaGPT Think-Act-Observe)
    ↓
工具层（LLM Router → DeepSeek / opencode CLI / Docker / Nginx）
```

### 模块速查

| 模块 | 关键文件 | 作用 |
|------|---------|------|
| **Agent** | `agents/base.py` + 5 个子目录 | BaseAgent / CEO / Developer / DevOps / FrontendExpert / CustomerService |
| **Workflow** | `workflows/{graph,executor,loader,converter}.py` | DAG 拓扑 + 拓扑层并行执行 + YAML 加载 |
| **LLM** | `tools/llm/{router,client,prompt,rate_limit}.py` | LLMRouter（统一路由 DeepSeek） + httpx 客户端 + 共享 RPM/TPM + 指数退避 + JSON 解析 |
| **Code** | `tools/code/opencode.py` | opencode CLI 封装（Developer Agent 用）|
| **Deploy** | `tools/deploy/{analyzer,docker,nginx,monitor,cache}.py` | ProjectAnalyzer / DockerManager / NginxManager / ServiceMonitor / CacheManager |
| **Channel** | `channels/{feishu,wecom}.py` | 飞书 WebSocket / 企业微信回调 |
| **Message** | `communication/queue.py` | CSMessageQueue（客服 Redis 队列 + Worker） |
| **Memory** | `memory/{working,session,knowledge,graph,manager,importance,consolidator}.py` | 三层记忆 + 关联图 + LLM 提炼 |
| **Storage** | `storage/{database,state_manager}.py` | asyncpg（min=2, max=20）+ redis.asyncio（SETNX 原子去重 P1 #15）|
| **Security** | `security/{input_guard,sandbox,access_control,audit}.py` | InputGuard / SandboxedExecutor / PolicyEnforcer / AuditLogger |
| **Observability** | `observability/{logging,metrics,health}.py` + `alerts.yml` | 结构化日志 + Prom 指标 + 健康检查 + 6 条告警 |
| **Acceleration** | `acceleration/{cache,prompt,limits}.py` | LLM ResponseCache + EmbeddingCache + PromptOptimizer + 共享任务窗口 |
| **Model** | `models/{agent,message,task,workflow,deploy,customer}.py` | dataclass + Enum |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务元信息 |
| GET | `/health` | 完整健康检查（PG/Redis/pgvector/Docker） |
| GET | `/metrics` | Prometheus 指标（text/plain） |
| POST | `/api/requirement` | 提交需求，触发完整工作流 |
| GET | `/api/agents` | 所有 Agent 状态 |
| GET | `/api/workflow` | 工作流拓扑信息 |
| GET | `/api/devops/services` | 已部署服务列表 |
| GET | `/api/devops/services/{name}` | 单服务详情 |
| POST | `/api/devops/services/{name}/restart` | 重启服务 |
| POST | `/api/devops/services/{name}/stop` | 停止服务 |
| POST | `/api/feishu/webhook` | 飞书事件回调（签名验证 P0 #6） |
| GET/POST | `/api/wecom/ceo/callback` | 企微 CEO 回调（GET=URL 验证）|
| GET/POST | `/api/wecom/cs/callback` | 企微客服回调（GET=URL 验证）|

## 埋点系统（analytics）✅ 已实现

采集可量化的真实运行数据。配套 4 个文件：

- `analytics/__init__.py` — 写入器，NDJSON 追加到 `analytics/logs/analytics_events.log`（可用 `ANALYTICS_LOG_PATH` 覆盖）
- `analytics/analytics_spec.md` — 11 个事件类型定义 + 5 个运行指标公式
- `analytics/analytics_ops_notes.md` — 运维注意事项（多进程丢行 / logrotate / 隐私红线）
- `analytics/report.py` — 统计脚本，一行命令产出 5 个指标

**11 个事件**：`feishu.message.received` / `input.guard.rejected` / `agent.think` / `agent.act` / `agent.error` / `tool.call` / `memory.recall` / `memory.distill` / `workflow.start` / `workflow.node` / `workflow.end`

**报告的 5 个统计项**：已结束工作流成功率 / P95 延迟 / 并行比值代理 / think 阶段 tokens/task / 输入拒绝比例。统计口径和使用限制见 `analytics/analytics_spec.md`，不能直接当作实测加速比、金额成本或攻击检测准确率。

```bash
# 实时观察（部署时调试用）
ANALYTICS_VERBOSE=1 python main.py

# 产出运行指标
python analytics/report.py > metrics_report.txt
```

**已知限制**：单 Lock 串行化，多 worker 会丢行（生产前如要水平扩展需改 Redis Stream）；无日志轮转（生产前需加 logrotate）。

## 安全修复历史

完整报告：`docs/code-review-2026-06-18.md`

| 阶段 | Commit | 范围 |
|------|--------|------|
| **P0** | `0b817d8b` | 7 个严重漏洞（认证/授权/SSRF/注入） |
| **P1** | `a820d11d` | 8 个高危漏洞（并发/沙箱/限流/持久化） |
| **P2** | `15bf22ab` | 9 个中危漏洞（异常处理/限流/审计/部署监控） |
| **P3** | `45604ae5` | 工程改进（覆盖率/文档同步/命名增强），非安全修复 |

**测试统计（2026-10-06）**：`venv/bin/python -m pytest --collect-only --no-cov -q` 收集 247 个测试用例（含参数化）。收集成功不代表全部通过；此前本轮功能回归的指定测试集为 179 passed。本次仅更新文档，未重跑全部集成测试。

> 注：早期 P0 阶段记录 89 个测试，但 `tests/test_p0_fixes.py` 实际未提交文件（可能合并入其他测试或被 P3 清理）。P1 起有可追溯的测试文件。

## 实现进度

### 已完成 ✅
- 基础设施（Redis、PostgreSQL + pgvector、Docker）
- 融合架构（MetaGPT 角色 + ChatDev 图工作流）
- 5 个 Agent（CEO、Developer、DevOps、FrontendExpert、CustomerService）
- 工具集成（飞书 / 企业微信 / Docker / Nginx / opencode CLI）
- 三层记忆 + MemoryGraph + LLM 提炼
- 安全系统（InputGuard / Sandbox / PolicyEnforcer / AuditLogger）
- 可观测性（JSON 日志 + Prom 指标 + 健康检查 + 6 条告警）
- 加速层（LLM 响应缓存 + 嵌入缓存 + Prompt 优化）
- 客服系统（CSMessageQueue + QueueWorker + 独立 DB）
- 监控告警（ServiceMonitor + 自动重启 + 飞书通知）
- **埋点系统**（11 事件 + 5 指标 + 报告脚本）
- **P3 覆盖率配置**（pytest --cov，源模块化排除）

### 已知架构问题 ⏳
- 历史审查记录过社区间高耦合；44 边为当时快照，本次未重新计算。
- `templates/project_template.yaml` 已包含开发、部署阶段及角色能力；作为 LLM 提示约束，不是直接执行的静态 DAG。
- `tools/design/`、`tools/document/`、`tools/project/` 仍只有空 `__init__.py`
- 多进程部署会丢埋点行（见 `analytics/analytics_ops_notes.md` §1）

### 暂未实现 ⬜
- `PLAN_AND_ACT` 反应模式（设计在 `ReactMode` 枚举里但未实现）
- 知识图谱 RAG 检索的语义混合策略
- GitHub Actions CI 自动化测试
- Dockerfile 模板的审计基线（非 root / 多阶段 / digest 固定）
- 飞书 WebSocket 长连接重连机制

## 调度、失败与流量控制

- 图节点是任务；节点执行层根据角色找到 Agent，并构造任务 Message 调用 `agent.run(message)`。Agent 之间不通过发布订阅传递结果。
- WorkflowGraph 使用 DFS 环检测；WorkflowExecutor 使用 Kahn 拓扑分层，通过 `asyncio.gather(return_exceptions=True)` 收集同层结果。异常记录到 `context["outputs"]`；依赖失败节点的下游标记 skipped，独立分支继续，CEO 汇总 completed / partial / failed。
- 默认单工作流并发窗口 5；所有工作流节点和客服单条处理任务共享 `WORKSPACE_MAX_CONCURRENT_TASKS=5`。Developer 内部子任务另有并发 3；CEO 规划不占节点名额。
- 所有 LLMClient 请求共享滚动 60 秒配额：`LLM_RPM_LIMIT=60`、`LLM_TPM_LIMIT=60000`。请求前预留输入字节估算与最大输出，结束后按 usage 校正；未结束的预留不释放，结束后用量再保留 60 秒。缓存命中不计配额。
- LLM 重试默认 3 次（最多 4 次尝试），基础等待 1/2/4 秒加 0～25% 抖动，本地上限 30 秒，并遵守更长的 Retry-After。仅重试指定临时 HTTP/网络错误；每次尝试重新申请配额。
- 共享窗口限单进程、单事件循环；外部 opencode CLI 的内部模型请求不经过 LLMClient。没有令牌桶；多进程共享限流尚未实现。

## 当前实现边界

- IM 需求流程包含确认；`/api/requirement` 直接拆解执行，不等待确认，其响应 status 不能代替工作流业务成功状态。
- 返回错误 Message 而未抛异常的工具/Agent，尚未全部统一识别为失败节点。通用节点 timeout/retry 字段与条件边尚未在主执行器完整执行；LLM 重试不等于工作流节点重试。
- Agent 实例及部分可变上下文、记忆仍共享，CEO 计划按 chat_id 管理；当前不是严格的用户隔离架构。
- OpenCodeTool 已默认强制使用 Docker 沙箱；缺少 Docker、镜像或认证时返回失败，不降级到宿主机。网络使用 bridge，尚无出站白名单。详见 `docs/opencode-docker.md`。
- SlowAPI 已显式装饰的 Webhook 限制为 120/min；默认 60/min 配置缺少全局中间件接入，不应声称所有 HTTP 请求均受其保护。
- OpenTelemetry 依赖存在，未确认完整 trace exporter 链路；当前可确认的是 JSON 日志、trace_id、Prometheus 指标与健康检查。

opencode CLI 已默认强制 Docker 执行；本地缺少 Docker，真实容器验证待运行环境准备。详见 [配置与验证](docs/opencode-docker.md)。
