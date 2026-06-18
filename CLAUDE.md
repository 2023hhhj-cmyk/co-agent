# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **最后更新**：2026-06-18（同步 P0-P2 安全修复 + 实际模块状态）

## 项目概述

多Agent协同系统 - 一人公司运营平台，融合 **MetaGPT 角色机制** + **ChatDev 图工作流**。

核心业务：为中小企业打造Agent平台，自动化运营流程。完整产品规格见 `product.md`。

## 服务器与部署

- SSH连接：`ssh muzhu-ai`（配置在 `~/.ssh/config`，详见桌面 `muzhu-secrets.txt`）
- 服务器项目目录：`/root/muzhu-ai/`
- 部署命令（**注意：项目根目录没有 `scripts/deploy.sh`**，用 rsync 手动同步）：
  ```bash
  # 从本地推送到服务器
  rsync -avz --delete \
    --exclude='venv/' --exclude='__pycache__/' --exclude='.git/' \
    --exclude='.code-review-graph/' --exclude='.claude/' \
    --exclude='docs/code-review-*.md' \
    -e ssh ./ muzhu-ai:/root/muzhu-ai/

  # 重启服务
  ssh muzhu-ai "lsof -t -i:8080 | xargs kill; cd /root/muzhu-ai && nohup venv/bin/python main.py > /tmp/co-agent.log 2>&1 &"
  ```
- 服务端口：8080（PID 通过 `lsof -i:8080` 查看）
- 服务器启动命令：`cd /root/muzhu-ai && venv/bin/python main.py`

## 常用命令

```bash
# 本地运行
source venv/bin/activate
python main.py                     # 启动 FastAPI 服务 (0.0.0.0:8080)

# 安装依赖
pip install -r requirements.txt

# 数据库初始化（需要 PostgreSQL 运行）
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql

# 运行测试（已配置 pytest，含 asyncio 自动模式）
pytest                              # 全部测试
pytest tests/test_p0_fixes.py       # 仅 P0 修复测试
pytest --cov=. --cov-report=html     # 覆盖率报告（见 P3 #26）
```

## 环境变量

在 `.env` 中配置（模板见 `.env.example`）。必须设置以下变量：

- `DEEPSEEK_API_KEY` — LLM 调用必需
- `FEISHU_APP_SECRET` — 飞书 webhook 签名验证必需（P0 #6 修复后强制）
- `WECOM_*_SECRET` / `WECOM_*_AES_KEY` — 企业微信解密/签名
- `REDIS_URL` / `DATABASE_URL` — 持久化与状态

## 架构

### 三层融合设计

```
YAML 工作流定义 (ChatDev) — 当前由 CEO 动态生成 DAG
    ↓
WorkflowGraph → WorkflowExecutor（拓扑排序，分层并行）
    ↓
Agent 执行层 (MetaGPT Think-Act-Observe)
    ↓
工具层（LLM Router → DeepSeek / Claude Code CLI / Docker / Nginx）
```

### 工作流引擎（`workflows/`）

- **DAG 定义**：YAML 模板在 `templates/project_template.yaml`，CEO 动态生成
- **WorkflowGraph**（`graph.py`）：Kahn 拓扑排序 + DFS 环检测 + 条件边路由
- **WorkflowExecutor**（`executor.py`）：按拓扑层执行，同层 `asyncio.gather` 并行

### Agent 系统（`agents/`）

- **BaseAgent**（`base.py`）：抽象基类，Think-Act-Observe 循环
- 三种反应模式（`ReactMode`）：
  - `REACT`：LLM 动态选择动作（CEO、Developer、DevOps）
  - `BY_ORDER`：顺序执行（PM、Designer、DevOps、Finance）
  - `PLAN_AND_ACT`：计划后执行（**尚未实现**，回退为 REACT）
- 6 个 Agent 实现：
  - **CEO**（`ceo/agent.py`）— 飞书入口，需求拆解 + 卡片回调
  - **Developer**（`developer/agent.py`）— Claude Code 编码
  - **DevOps**（`devops/agent.py`）— Docker 部署 + 监控
  - **FrontendExpert**（`frontend_expert/agent.py`）— 前端开发
  - **Finance**（`finance/agent.py`）— 财务结算
  - **CustomerService**（`customer_service/agent.py`）— 企业微信客服

### 消息通信（`communication/`）

- **MessageBus**（`message_bus.py`）：发布/订阅 + **Redis 持久化**（P1 #9 修复）
  - 消息传递仍是 in-memory 实时通路，Redis 仅做审计/历史
  - `get_persisted_history()` 用于重启后审计
- 路由：`send_to="*"` 广播 + `watch` 集合过滤；定向投递按 `send_to`
- 过滤自身消息（`sent_from == self` 不投递）
- **CSMessageQueue**（`queue.py`）：客服消息队列（Redis List）+ `QueueWorker`

### LLM 路由（`tools/llm/`）

- **LLMRouter**（`router.py`）：Agent 角色 → 任务类型 → LLM 提供者
  - 全部任务统一路由至 **DeepSeek**（`deepseek-v4-pro`）
  - ClaudeClient 仍保留但未路由
- 客户端（`client.py`）为 httpx 原生调用
- `chat_with_json` 多策略 JSON 解析（P2 #20 增强：原文/markdown/嵌入/数组）
- 响应缓存（`acceleration/cache.py`）：相同 prompt 命中缓存

### 通道（`channels/`）

- **FeishuClient**（`feishu.py`）：飞书 WebSocket 长连接 + 事件订阅
  - `verify_webhook_signature` 用 `app_secret` HMAC-SHA256（P0 #6 修复）
  - 幂等性用 Redis SETNX（P2 #25 修复）
- **WecomClient**（`wecom.py`）：企业微信 + 智能机器人
  - `_validate_response_url` 白名单校验（P0 #4 SSRF 修复）
  - `WecomCrypto` 加解密用 `hmac.compare_digest` 恒定时间（P0 #2）
  - `_get_access_token` 用 `asyncio.Lock` + double-check（P0 #3）

### 部署工具（`tools/deploy/`）

- **ProjectAnalyzer**（`analyzer.py`）：用 Claude Code 分析项目，生成 Dockerfile
- **CacheManager**（`cache.py`）：项目分析结果缓存（24h）
- **DockerManager**（`docker.py`）：容器生命周期 + 端口分配
- **PortManager**（`docker.py`）：端口分配管理（8001-9000）
- **NginxManager**（`nginx.py`）：反向代理配置
- **ServiceMonitor**（`monitor.py`）：每 30 秒健康检查 + 自动重启
  - 回调包 `_safe_invoke_callback` 异常隔离（P2 #23）
  - 部署前 `_safe_write_file` 备份已有 Dockerfile（P2 #22）

### 数据模型（`models/`）

- `agent.py`：AgentRole、ActionDef、ReactMode
- `message.py`：Message（cause_by/send_to）、MessageQueue、MessageRole
- `task.py`：Task、TaskStatus、ProjectPlan（含 `creator_sender_id` P0 #5）
- `workflow.py`：WorkflowDef、GraphDef、NodeDef、EdgeDef、NodeType
- `deploy.py`：DeployRecord、ServiceStatus、ServiceHealth
- `customer.py`：CustomerSession、QueuedMessage

### 存储（`storage/`）

- **Database**（`database.py`）：asyncpg 异步连接（min=2, max=20）
- **StateManager**（`state_manager.py`）：redis.asyncio 封装
  - `dedup_check(key, ttl=86400)` 用 SETNX 原子去重（P1 #15）
  - 启动时检查 Redis AOF 配置
- 数据库 Schema（7 张表）：projects、tasks、agent_states、conversations、memories、audit_logs、transactions
- 客服独立 DB（`init_cs_db.sql`）：cs_customer_projects / cs_faq / cs_conversations / cs_escalations

### 安全系统（`security/`）✅ 已实现

- **InputGuard**（`input_guard.py`）：外部输入校验层
  - 长度限制 5000 chars
  - **P0 #7 增强**：17+ 模式，含中文/工具调用/零宽字符/编码混淆/markdown 隐藏
  - Unicode NFKC 归一化（防同形字绕过）
- **SandboxedExecutor**（`sandbox.py`）：Docker 沙箱
  - 资源限制：512MB / 1 CPU / 网络隔离 / 300s 超时
  - **P1 #12 修复**：`--tmpfs /tmp:rw,noexec` + `--security-opt no-new-privileges`
  - **P1 #13 修复**：降级模式只给 `["Read", "Glob", "Grep"]` 不暴露 Bash
- **PolicyEnforcer**（`access_control.py`）：Agent 权限策略
- **AuditLogger**（`audit.py`）：审计日志
  - **P2 #21 修复**：DB 写失败入 ring buffer + Prom 指标
  - `retry_failed_events()` 补偿写入

### 记忆系统（`memory/`）✅ 已实现

三层架构：
- **WorkingMemory**（`working.py`）：任务级短期记忆
- **SessionMemory**（`session.py`）：会话级
- **KnowledgeMemory**（`knowledge.py`）：语义检索（ChromaDB）
- **MemoryGraph**（`graph.py`）：记忆关联图
- **MemoryManager**（`manager.py`）：统一接口 + LLM 自动提炼
- **importance**（`importance.py`）/ **consolidator**（`consolidator.py`）：记忆巩固

### 可观测性（`observability/`）✅ 已实现

- **setup_logging**（`logging.py`）：JSON 结构化日志 + trace_id
- **HealthChecker**（`health.py`）：依赖健康检查
- **metrics.py**：Prometheus 指标（agent_state / task_duration / http_requests / sandbox_mode / input_rejected / queue_depth）
- **alerts.yml**：Prometheus 告警规则（6 个：AgentError / SandboxDegraded / HighInputRejectRate / QueueOverflow / LLMHighLatency / AgentNoActivity）

### 加速层（`acceleration/`）✅ 已实现

- **ResponseCache**（`cache.py`）：LLM 响应缓存
- **EmbeddingCache**：嵌入向量缓存
- **PromptOptimizer**：prompt 优化

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务元信息 |
| GET | `/health` | 完整健康检查（PG/Redis/ChromaDB/Docker） |
| GET | `/metrics` | Prometheus 指标（text/plain） |
| POST | `/api/requirement` | 提交需求，触发完整工作流 |
| GET | `/api/agents` | 获取所有 Agent 状态 |
| GET | `/api/workflow` | 工作流拓扑信息 |
| GET | `/api/messages/history` | 消息历史 |
| GET | `/api/devops/services` | 已部署服务列表 |
| GET | `/api/devops/services/{name}` | 单个服务详情 |
| POST | `/api/devops/services/{name}/restart` | 重启服务 |
| POST | `/api/devops/services/{name}/stop` | 停止服务 |
| POST | `/api/feishu/webhook` | 飞书事件回调（含签名验证 P0 #6） |
| POST | `/api/wecom/ceo/callback` | 企业微信 CEO 回调 |
| POST | `/api/wecom/cs/callback` | 企业微信客服回调 |
| GET | `/api/wecom/ceo/callback` | CEO 回调 URL 验证 |
| GET | `/api/wecom/cs/callback` | 客服回调 URL 验证 |

## 安全修复历史

完整报告：`docs/code-review-2026-06-18.md`

| 阶段 | Commit | 范围 |
|------|--------|------|
| **P0** | `0b817d8b` | 7 个严重漏洞（认证/授权/SSRF/注入） |
| **P1** | `a820d11d` | 8 个高危漏洞（并发/沙箱/限流/持久化） |
| **P2** | `15bf22ab` | 9 个中危漏洞（异常处理/限流/审计/部署监控） |

测试覆盖：146 个（89 P0 + 28 P1 + 29 P2）。

## 实现进度

### 已完成 ✅
- 基础设施（Redis、PostgreSQL、ChromaDB、Docker）
- 融合架构（MetaGPT 角色 + ChatDev 图工作流）
- 6 个 Agent（CEO、Developer、DevOps、FrontendExpert、Finance、CustomerService）
- 工具集成（飞书 WebSocket、企业微信、Docker、Nginx、Claude Code CLI）
- 三层记忆系统（Working / Session / Knowledge + MemoryGraph）
- 安全系统（InputGuard / Sandbox / PolicyEnforcer / AuditLogger）
- 可观测性（结构化日志 + Prometheus 指标 + 健康检查 + 告警规则）
- 加速层（LLM 响应缓存 + 嵌入缓存）
- 客服系统（CSMessageQueue + QueueWorker）
- 监控告警（ServiceMonitor + 自动重启 + 飞书通知）

### 已知架构问题 ⏳
- `memory/`、`security/`、`observability/`、`acceleration/` 已在最新代码中实现，但**老 CLAUDE.md 写的是"空"** — 本文档已同步
- 6 个社区高耦合（`ceo-handle` ↔ `models-dict` 44 边）— 架构重构待办
- `templates/project_template.yaml` 是占位 YAML，CEO 实际动态生成 DAG
- `tools/design/`、`tools/document/`、`tools/project/` 仍只有空 `__init__.py`

### 暂未实现 ⬜
- `PLAN_AND_ACT` 反应模式（设计在 `ReactMode` 枚举里但未实现）
- 知识图谱 RAG 检索的语义混合策略
- GitHub Actions CI 自动化测试
- Dockerfile 模板的审计基线（非 root 用户 / 多阶段 / 镜像 digest 固定）
- 飞书 WebSocket 长连接重连机制
