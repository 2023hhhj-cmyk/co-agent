# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **最后更新**：2026-07-02（同步 P3 阶段 + analytics 埋点系统 + 修正测试统计与 deploy 脚本）

## 项目概述

多Agent协同系统 - 一人公司运营平台，融合 **MetaGPT 角色机制** + **ChatDev 图工作流**。

核心业务：为中小企业打造Agent平台，自动化运营流程。完整产品规格见 `product.md`。

## 服务器与部署

- SSH：`ssh muzhu-ai`（`~/.ssh/config` 配置，凭证在桌面 `muzhu-secrets.txt`）
- 服务器目录：`/root/muzhu-ai/`
- **推荐部署**：直接跑 `./scripts/deploy.sh`（自动 rsync + 建 venv + 装依赖 + 跑 `init_db.sql` + 生成 `start.sh`）
  - 部署前需在本地设 `PGPASSWORD` 环境变量
- 手动部署（不推荐，deploy.sh 失败时用）：
  ```bash
  rsync -avz --delete \
    --exclude='venv/' --exclude='__pycache__/' --exclude='.git/' \
    --exclude='.code-review-graph/' --exclude='.claude/' \
    --exclude='docs/code-review-*.md' \
    -e ssh ./ muzhu-ai:/root/muzhu-ai/
  ssh muzhu-ai "lsof -t -i:8080 | xargs kill; cd /root/muzhu-ai && nohup venv/bin/python main.py > /tmp/co-agent.log 2>&1 &"
  ```
- 服务端口：8080（PID：`lsof -i:8080`）
- 启动：`cd /root/muzhu-ai && venv/bin/python main.py`（单进程 `uvicorn.run`，无多 worker）

## 常用命令

```bash
source venv/bin/activate
python main.py                       # 启动 FastAPI（0.0.0.0:8080）
pip install -r requirements.txt
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_cs_db.sql  # 客服独立 DB

pytest                                # 全部测试（含覆盖率，见 P3 #26）
pytest tests/test_p1_fixes.py         # 按阶段跑
pytest --cov=. --cov-report=html      # 覆盖率报告 → htmlcov/
```

## 环境变量

模板 `.env.example`。**生产必设**：

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | LLM 调用 |
| `FEISHU_APP_SECRET` | 飞书 webhook 签名验证（P0 #6 后强制）|
| `WECOM_*_SECRET` / `WECOM_*_AES_KEY` | 企业微信解密/签名 |
| `REDIS_URL` / `DATABASE_URL` | 持久化与状态 |
| `ANALYTICS_VERBOSE=1` | 埋点 stderr 镜像（调试用）|
| `ANALYTICS_LOG_PATH=...` | 自定义埋点日志路径（默认 `./analytics_events.log`）|

## 架构

```
YAML 工作流定义 (ChatDev) — 当前由 CEO 动态生成 DAG
    ↓
WorkflowGraph → WorkflowExecutor（拓扑排序，分层并行）
    ↓
Agent 执行层 (MetaGPT Think-Act-Observe)
    ↓
工具层（LLM Router → DeepSeek / Claude Code CLI / Docker / Nginx）
```

### 模块速查

| 模块 | 关键文件 | 作用 |
|------|---------|------|
| **Agent** | `agents/base.py` + 6 个子目录 | BaseAgent / CEO / Developer / DevOps / FrontendExpert / Finance / CustomerService |
| **Workflow** | `workflows/{graph,executor,loader,converter}.py` | DAG 拓扑 + 拓扑层并行执行 + YAML 加载 |
| **LLM** | `tools/llm/{router,client,prompt}.py` | LLMRouter（统一路由 DeepSeek） + httpx 客户端 + 多策略 JSON 解析 |
| **Code** | `tools/code/claude_code.py` | Claude Code CLI 封装（Developer Agent 用）|
| **Deploy** | `tools/deploy/{analyzer,docker,nginx,monitor,cache}.py` | ProjectAnalyzer / DockerManager / NginxManager / ServiceMonitor / CacheManager |
| **Channel** | `channels/{feishu,wecom}.py` | 飞书 WebSocket / 企业微信回调 |
| **Message** | `communication/{message_bus,queue}.py` | MessageBus（pub/sub + Redis 审计 P1 #9）+ CSMessageQueue |
| **Memory** | `memory/{working,session,knowledge,graph,manager,importance,consolidator}.py` | 三层记忆 + 关联图 + LLM 提炼 |
| **Storage** | `storage/{database,state_manager}.py` | asyncpg（min=2, max=20）+ redis.asyncio（SETNX 原子去重 P1 #15）|
| **Security** | `security/{input_guard,sandbox,access_control,audit}.py` | InputGuard / SandboxedExecutor / PolicyEnforcer / AuditLogger |
| **Observability** | `observability/{logging,metrics,health}.py` + `alerts.yml` | 结构化日志 + Prom 指标 + 健康检查 + 6 条告警 |
| **Acceleration** | `acceleration/{cache,prompt}.py` | LLM ResponseCache + EmbeddingCache + PromptOptimizer |
| **Model** | `models/{agent,message,task,workflow,deploy,customer}.py` | dataclass + Enum |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务元信息 |
| GET | `/health` | 完整健康检查（PG/Redis/ChromaDB/Docker） |
| GET | `/metrics` | Prometheus 指标（text/plain） |
| POST | `/api/requirement` | 提交需求，触发完整工作流 |
| GET | `/api/agents` | 所有 Agent 状态 |
| GET | `/api/workflow` | 工作流拓扑信息 |
| GET | `/api/messages/history` | 消息历史 |
| GET | `/api/devops/services` | 已部署服务列表 |
| GET | `/api/devops/services/{name}` | 单服务详情 |
| POST | `/api/devops/services/{name}/restart` | 重启服务 |
| POST | `/api/devops/services/{name}/stop` | 停止服务 |
| POST | `/api/feishu/webhook` | 飞书事件回调（签名验证 P0 #6） |
| GET/POST | `/api/wecom/ceo/callback` | 企微 CEO 回调（GET=URL 验证）|
| GET/POST | `/api/wecom/cs/callback` | 企微客服回调（GET=URL 验证）|

## 埋点系统（analytics）✅ 已实现

为简历采集可量化的真实运行数据。配套 4 个文件：

- `analytics.py` — 写入器，NDJSON 追加到 `analytics_events.log`（可用 `ANALYTICS_LOG_PATH` 覆盖）
- `analytics_spec.md` — 11 个事件类型定义 + 5 个简历指标公式
- `analytics_ops_notes.md` — 运维注意事项（多进程丢行 / logrotate / 隐私红线）
- `analytics_report.py` — 统计脚本，一行命令产出 5 个指标

**11 个事件**：`feishu.message.received` / `input.guard.rejected` / `agent.think` / `agent.act` / `agent.error` / `tool.call` / `memory.recall` / `memory.distill` / `workflow.start` / `workflow.node` / `workflow.end`

**5 个简历指标**：端到端成功率 / P95 延迟 / 并行加速比 / LLM 单任务成本 / Prompt 注入拦截率

```bash
# 实时观察（部署时调试用）
ANALYTICS_VERBOSE=1 python main.py

# 产出简历数字
python analytics_report.py > resume_metrics.txt
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

**测试统计（截至 2026-07-02）**：131 个测试函数 — 17 P1 + 22 P2 + 7 P3 + 85 unit。

> 注：早期 P0 阶段记录 89 个测试，但 `tests/test_p0_fixes.py` 实际未提交文件（可能合并入其他测试或被 P3 清理）。P1 起有可追溯的测试文件。

## 实现进度

### 已完成 ✅
- 基础设施（Redis、PostgreSQL、ChromaDB、Docker）
- 融合架构（MetaGPT 角色 + ChatDev 图工作流）
- 6 个 Agent（CEO、Developer、DevOps、FrontendExpert、Finance、CustomerService）
- 工具集成（飞书 / 企业微信 / Docker / Nginx / Claude Code CLI）
- 三层记忆 + MemoryGraph + LLM 提炼
- 安全系统（InputGuard / Sandbox / PolicyEnforcer / AuditLogger）
- 可观测性（JSON 日志 + Prom 指标 + 健康检查 + 6 条告警）
- 加速层（LLM 响应缓存 + 嵌入缓存 + Prompt 优化）
- 客服系统（CSMessageQueue + QueueWorker + 独立 DB）
- 监控告警（ServiceMonitor + 自动重启 + 飞书通知）
- **埋点系统**（11 事件 + 5 指标 + 报告脚本）
- **P3 覆盖率配置**（pytest --cov，源模块化排除）

### 已知架构问题 ⏳
- 6 个社区高耦合（`ceo-handle` ↔ `models-dict` 44 边）— 架构重构待办
- `templates/project_template.yaml` 是占位 YAML，CEO 实际动态生成 DAG
- `tools/design/`、`tools/document/`、`tools/project/` 仍只有空 `__init__.py`
- 多进程部署会丢埋点行（见 `analytics_ops_notes.md` §1）

### 暂未实现 ⬜
- `PLAN_AND_ACT` 反应模式（设计在 `ReactMode` 枚举里但未实现）
- 知识图谱 RAG 检索的语义混合策略
- GitHub Actions CI 自动化测试
- Dockerfile 模板的审计基线（非 root / 多阶段 / digest 固定）
- 飞书 WebSocket 长连接重连机制
