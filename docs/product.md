# MuZhu-AI 多 Agent 协同系统 · 产品与实现说明

> 更新：2026-10-06。按当前本地代码核对；服务器更新尚未确认。文末保留历史日志，历史描述不代表当前能力。

## 项目概述

面向中小企业的自动化运营平台，以自研 BaseAgent 实现角色协作，借鉴 MetaGPT 的角色机制与 ChatDev 的图工作流设计。不是直接运行这两个框架的完整实现。

## Agent 角色（5 个）

| 角色 | 当前职责 | 能力边界 |
|------|----------|----------|
| CEO | 理解需求、追问、拆解任务、确认计划、调度及汇总 | IM 支持确认；HTTP 需求入口直接执行 |
| Developer | 架构设计、开发子任务拆分、调用 CLI 构建产品 | 内部子任务并发限制为 3 |
| Frontend Expert | 界面设计、前端开发、加载设计 Skills | 通过 opencode CLI 执行代码生成 |
| DevOps | 项目分析、部署、Nginx 配置、健康检查和监控 | 按项目类型选择路径，容器部署不是唯一分支 |
| Customer Service | 企业微信客服、FAQ、客户项目进度查询和转人工 | 通过消息队列和 Worker 处理 |

## 系统架构与工作流

```text
飞书 / 企业微信需求
  → CEO 理解需求、读取业务模板、拆解任务与依赖、确认计划
  → 任务转换为节点，依赖转换为边 → WorkflowGraph
  → DFS 检测环 → Kahn 拓扑分层 → WorkflowExecutor
  → 同层 asyncio.gather → 节点执行层按角色选择 Agent
  → agent.run(Message) → Think / Act / Observe
  → DeepSeek API / opencode CLI / Docker / Nginx 等工具
  → CEO 汇总结果
```

`templates/project_template.yaml` 已包含开发、部署阶段及角色能力。CEO 将其作为 LLM 提示约束，实际任务 DAG 动态生成；模板不是占位文件，也不是直接执行的静态工作流。节点表示任务，同一个 Agent 可以承担多个节点。

工作流节点执行层负责选择角色、组装任务 Message 并调用 `run()`。Agent 之间不通过发布订阅传递结果。

BaseAgent 实现 REACT 与 BY_ORDER；PLAN_AND_ACT 枚举已定义，目前回退到 REACT。图的条件字段和安全条件求值器存在，但主执行器尚未完整执行条件路由。

### 失败处理

BaseAgent 记录异常并向上抛出；同层 gather 收集异常并写入 `context["outputs"]`。依赖失败任务的节点标记 skipped，跳过沿依赖链传播；不受影响的分支继续执行。CEO 汇总成功、失败和跳过的任务，计划状态为 completed / partial / failed。

部分业务路径返回带失败标记的 Message 而未抛异常，主执行器尚未统一将这些结果识别为失败节点。节点模型的通用 timeout/retry 字段尚未完整落实，不能把 LLM 重试描述为所有工作流任务都会重试。

### 共享并发与 LLM 配额

| 配置 | 默认值 | 作用 |
|------|--------|------|
| WORKSPACE_MAX_CONCURRENT_TASKS | 5 | 所有工作流节点与客服单条处理任务共享执行名额 |
| LLM_RPM_LIMIT | 60 | 共享滚动 60 秒请求配额 |
| LLM_TPM_LIMIT | 60000 | 共享 Token 预留与用量配额 |
| LLM_MAX_RETRIES | 3 | 最多额外重试 3 次 |
| LLM_RETRY_BASE_DELAY | 1 | 基础退避秒数 |
| LLM_RETRY_MAX_DELAY | 30 | 本地退避等待上限 |

单工作流还受 `execute(max_parallel=...)` 的窗口限制（默认 5）。Developer 内部子任务另受并发 3 限制；CEO 规划不占工作流节点名额，避免嵌套等待。

LLM 请求前预留输入 UTF-8 字节估算与最大输出 tokens；未结束的预留不释放，结束后按供应商 usage 校正并保留 60 秒。未知用量保留估算，缓存命中不占配额。单次预留超过 TPM 立即报错。

临时 HTTP 408/429/500/502/503/504/529、超时、网络及远端协议错误使用统一指数退避：1/2/4 秒加 0～25% 抖动，遵守更长的 Retry-After；每次尝试重新申请配额。认证、参数、解析错误与主动取消不重试。

当前使用滚动窗口，没有令牌桶。共享限制仅适用于单进程、单主事件循环；多进程需要外部共享状态。opencode CLI 内部模型调用不经过 LLMClient 配额。

## LLM 与代码工具

`tools/llm/router.py` 按角色和任务类型选择客户端，目前路由均指向 DeepSeek。`tools/llm/client.py` 封装 httpx、响应缓存、配额和统一重试；JSON 调用在此基础上解析输出。ClaudeClient 类仍存在，但当前路由未启用 Claude API。

代码生成使用 `tools/code/opencode.py` 的 OpenCodeTool，通过子进程执行 `opencode run <prompt> --model <provider/model> --format json --auto --dir /workspace`，管理工作目录、超时与退出结果。默认模型由 `OPENCODE_MODEL` 配置。其通用批量辅助方法的默认并发不代表工作区总并发。

DevOps 的容器路径为：ProjectAnalyzer 分析 → Docker 配置 → 构建与启动 → Nginx 反向代理 → 健康检查 → ServiceMonitor。另有静态、Node/Python 等项目类型分支。

## 记忆、存储、安全与可观测性

- 三层记忆：工作记忆、会话记忆、知识记忆；配合 MemoryGraph、重要性评分和 LLM 提炼。
- 存储：PostgreSQL（asyncpg，含 pgvector 长期知识）与 Redis。
- 安全模块：InputGuard、SandboxedExecutor、PolicyEnforcer、AuditLogger；API Key、回调签名及安全 HTTP 头在入口实施。
- OpenCodeTool 默认强制进入 Docker，限定项目挂载并设置资源、只读根及权限限制；没有宿主机降级。网络允许模型 API 和依赖下载，未做出站白名单。配置与验证见 [容器执行说明](opencode-docker.md)。
- 可观测性：结构化 JSON 日志、ContextVar trace_id、Prometheus 指标、健康检查和 6 条告警。OpenTelemetry 依赖存在，但未确认完整 exporter 链路。
- HTTP 限流：已装饰的 Webhook 为 120/min；默认 60/min 配置缺少全局中间件接入，不能当作全站已生效限制。WebSocket 不经过 HTTP 限流。
- 加速：LLM 响应缓存、嵌入缓存和 PromptOptimizer；不将历史局部测试倍数作为当前全系统性能结论。

Agent 实例及部分可变上下文、记忆仍共享，CEO 计划按 chat_id 管理；共享并发窗口没有实现严格的不同用户隔离。

## 项目结构

```text
main.py           FastAPI 入口与依赖初始化
config/           环境配置
agents/           六类 Agent、运行时业务知识和人格文件
workflows/        DAG、执行器、转换和 YAML 加载
models/           Agent、任务、Message、工作流及部署模型
tools/llm/        Router、客户端、JSON 解析、配额控制
tools/code/       opencode CLI 封装
tools/deploy/     项目分析、Docker、Nginx、监控及缓存
communication/    客服队列与 Worker
channels/         飞书与企业微信
memory/           三层记忆及强化模块
storage/          PostgreSQL 与 Redis
security/         输入校验、沙箱、权限与审计
observability/    日志、指标、健康检查
acceleration/     缓存、Prompt 优化及共享任务名额
templates/        业务阶段与角色能力模板
scripts/          部署与数据库初始化
analytics/        埋点写入、报告、规范和 logs/
tests/            测试、fixtures/、覆盖率配置和 artifacts/
docs/             本文、历史审查、interview/ 面试资料
```

`tools/design/`、`tools/document/`、`tools/project/` 仍是预留目录，不能当作已完成的工具能力。

## API 与部署

API 完整列表见 [AGENTS.md](../AGENTS.md#api-端点)。主要入口为 `/api/requirement`、Agent/工作流查询、DevOps 服务管理及飞书/企微回调；运行检查使用 `/health`、`/metrics`。

`/api/requirement` 直接拆解和执行，不等待 IM 确认。接口返回的 status 不能替代计划业务结果，部分失败仍可能收到成功形态的接口响应。

约定服务器为 SSH 别名 `muzhu-ai`、目录 `/root/muzhu-ai/`、端口 8080、单进程启动。`scripts/deploy.sh` 同步文件和 Skills、初始化 venv/依赖/数据库并生成 `start.sh`，不会自动重启。远端 `.env` 需单独维护；数据库初始化读取远端 PGPASSWORD，本地设置不会自动转发。

最近部署尝试因 SSH 主机密钥不匹配未完成。这里说明本地实现，不能据此判断服务器已运行最新代码。

## 测试与埋点

2026-10-06 使用 `venv/bin/python -m pytest --collect-only --no-cov -q` 收集到 247 个用例（含参数化）；收集不代表全部通过。此前指定功能回归集为 179 passed。本次文档更新未重跑全部集成测试。

```bash
venv/bin/python -m pytest
venv/bin/python -m pytest --collect-only --no-cov -q
python analytics/report.py
```

pytest 入口为根目录 `pytest.ini`，覆盖率配置为 `tests/.coveragerc`，生成报告在 `tests/artifacts/coverage/`。

埋点定义 11 类事件；默认日志在 `analytics/logs/analytics_events.log`，可用 ANALYTICS_LOG_PATH 覆盖。报告的 5 项统计存在口径边界：成功率按已结束样本、并行比值不是实际加速比、成本仅 think 阶段 tokens/task、拒绝比例不是攻击检测准确率。详见 [规范](../analytics/analytics_spec.md) 与 [运维说明](../analytics/analytics_ops_notes.md)。

## 后续工作

- 统一业务失败 Message 与异常的节点失败判定。
- 落实通用节点 timeout/retry、条件路由及 PLAN_AND_ACT。
- 隔离不同用户的 Agent 上下文和记忆。
- 多进程共享并发/LLM 配额与埋点单写者。
- 完整 HTTP 默认限流，并在服务器验证 CLI 容器运行及完善出站控制。
- 完善 CI、Dockerfile 审计基线、飞书连接恢复与 Web 管理界面。

## 历史记录说明

以下为原有修改日志，仅保留当时记录。其中测试数量、全局 HTTP 限流和“移除 Claude API”等表述应以上文当前实现说明为准。

## 更新日志

### 2026-06-17 — LLM 简化 + 安全升级

**LLM 架构简化**
- 所有 LLM 路由统一使用 DeepSeek，移除 Claude API 作为辅助模型
- `router.py`：全部任务类型映射至 DeepSeek
- `__init__.py`：移除 ClaudeClient 导出
- 保留 opencode CLI（代码生成命令行工具）不受影响

**代码托管**
- 创建 GitHub 私有仓库 `2023hhhj-cmyk/co-agent`
- 清理 Git 历史：删除 `.env`、`venv/`、敏感配置文件残留
- `.env.example` 脱敏：真实 IP/密码替换为占位符
- 添加 `README.md` 项目说明

---

### 2026-06-16 — 安全漏洞修复（严重 + 高危）

**严重漏洞（Critical）**
| 漏洞 | 修复 |
|------|------|
| `eval()` 任意代码执行 | AST 白名单求值器替代 `eval()`，阻止沙箱逃逸 |
| 6 处硬编码密码 | 删除源码中所有数据库/Redis 密码，改为环境变量 |
| 路径遍历（customer_name） | `_safe_path()` + `realpath()` 校验 |
| 符号链接遍历 | `os.path.islink()` 跳过 + 路径验证 |
| Docker 挂载注入 | `_validate_working_dir()` + 白名单限制 |

**高危漏洞（High）**
| 漏洞 | 修复 |
|------|------|
| CORS 通配符+凭证 | 改为 `CORS_ORIGINS` 白名单 |
| 飞书 Webhook 无签名验证 | HMAC-SHA256 签名验证 |
| 所有 API 无认证 | `X-API-Key` 中间件 |
| 无速率限制 | `slowapi` 全局 60/min + Webhook 120/min |
| 无 TLS | SSL 证书配置支持 |
| SSH HostKeyChecking 禁用 | 删除 + host key 预检查 |

---

### 2026-06-17 — 安全漏洞修复（中危）

| 问题 | 修复 |
|------|------|
| API 错误消息泄露内部路径 | 7 处 `str(e)` 改为通用错误消息 |
| 健康检查暴露依赖详情 | 仅返回 `healthy`/`degraded` |
| Metrics 暴露 Python 进程信息 | 自定义 `CollectorRegistry` |
| 缺少安全 HTTP 头 | `nosniff` + `DENY` + `HSTS` |
| LLM 日志可能含 API Key | 脱敏：仅记录错误类型 |
| 缓存键 SHA-256 截断碰撞 | 16→32 位 hex（128 bit） |
| 缓存 invalidate 空壳 | SCAN+DELETE 实现 |
| 嵌入缓存 TTL 过短 | 1h→24h |
| Redis key 注入 | `agent_id`/`task_id`/`chat_id` 白名单校验 |
| 数据库连接池无上限 | `min_size=2, max_size=20` |
| `print()` 散落 | 统一改为 `logger` |

---


opencode CLI 已默认强制 Docker 执行；本地缺少 Docker，真实容器验证待运行环境准备。详见 [配置与验证](opencode-docker.md)。
