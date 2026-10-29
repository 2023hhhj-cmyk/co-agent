# Muzhu-AI · 多 Agent 协同系统

一人公司运营平台——融合 **MetaGPT 角色机制** + **ChatDev 图工作流**，为中小企业提供 Agent 自动化运营。

## 架构

```
用户需求（飞书/企微）
     ↓
  CEO Agent（需求理解 → 任务分解）
     ↓
  WorkflowGraph（DAG 拓扑排序，分层并行）
     ↓
  Agent 执行层（Think-Act-Observe 循环）
     ↓
  工具层（LLM Router → DeepSeek）
     ↓
  DevOps（Docker + Nginx 自动部署）
```

## 技术栈

| 层 | 技术 |
|------|------|
| 框架 | FastAPI + Uvicorn |
| 数据库 | PostgreSQL（asyncpg） |
| 缓存 | Redis；长期知识向量存在 PostgreSQL + pgvector |
| LLM | DeepSeek |
| 消息 | 飞书 WebSocket + 企业微信回调 |
| 部署 | Docker + Nginx 反向代理 |
| 代码生成 | opencode CLI（`tools/code/opencode.py`） |
| 可观测性 | JSON 日志 + trace_id + Prometheus + 健康检查 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 API Key 和数据库连接信息

# 初始化数据库
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql

# 启动
python main.py
```

## Agent 角色

| 角色 | 职责 |
|------|------|
| CEO | 接收需求、拆解任务、调度团队、追踪进度 |
| Developer | 理解需求、架构设计、代码实现 |
| Frontend Expert | 前端界面开发 |
| DevOps | Docker 部署、Nginx 配置、服务监控 |
| Customer Service | 企业微信客服、FAQ、转人工 |

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/requirement` | 提交需求，触发完整工作流 |
| GET | `/api/agents` | Agent 状态列表 |
| GET | `/api/workflow` | 工作流拓扑信息 |
| GET | `/api/devops/services` | 已部署服务列表 |
| POST | `/api/devops/services/{name}/restart` | 重启服务 |
| POST | `/api/devops/services/{name}/stop` | 停止服务 |
| POST | `/api/feishu/webhook` | 飞书事件回调 |
| GET | `/health` | 健康检查 |
| GET | `/metrics` | Prometheus 指标 |

## 项目结构

```
docs/            产品规格、面试复习、安全审查
analytics/       埋点代码、统计脚本、规范和运行日志
tests/           测试代码、样例数据、覆盖率配置与生成报告
agents/          Agent 角色实现（CEO、Dev、DevOps 等）
channels/        飞书 + 企业微信通信层
communication/   消息总线（发布/订阅）
config/          配置管理（pydantic-settings）
memory/          三层记忆系统（工作/会话/知识）
models/          数据模型定义
observability/   Prometheus 指标 + 健康检查
scripts/         部署脚本 + 数据库初始化
security/        安全四层（输入校验/沙箱/权限/审计）
storage/         PostgreSQL + Redis 封装
templates/       项目模板（YAML）
tools/           LLM 客户端 + opencode + 部署工具
workflows/       工作流引擎（DAG + 拓扑排序）
```

## 安全

- **输入校验**：Prompt 注入 / SQL 注入 / XSS 检测
- **CLI 沙箱**：默认强制 Docker、项目目录挂载、只读根与资源限制；不可用时拒绝执行，联网使用 bridge
- **访问控制**：Agent 间 RBAC + API Key 认证
- **审计日志**：全操作记录到 PostgreSQL
- **HTTP 限流**：显式装饰的 Webhook 为 120 req/min；默认 60 req/min 尚缺全局中间件接入
- **安全头**：nosniff / DENY / HSTS

## 项目资料

- [完整产品规格](docs/product.md)
- [埋点事件与指标规范](analytics/analytics_spec.md)
- [埋点运维说明](analytics/analytics_ops_notes.md)
- [安全审查报告](docs/code-review-2026-06-18.md)

在项目根目录执行 `python analytics/report.py` 生成指标报告；
默认读取 `analytics/logs/analytics_events.log`，也可传入日志路径或设置 `ANALYTICS_LOG_PATH`。
执行 `pytest` 后，覆盖率报告生成在 `tests/artifacts/coverage/`。
2026-10-06 收集到 247 个测试用例（含参数化），收集不等于全部通过；此前指定功能回归集为 179 passed。

测试配置入口 `pytest.ini` 保留在根目录，以便直接运行 `pytest` 自动加载；
覆盖率配置集中在 `tests/.coveragerc`，测试缓存和报告集中在 `tests/artifacts/`。

## 共享并发与 LLM 配额

在 `.env` 设置以下值，修改后重启服务：

```dotenv
WORKSPACE_MAX_CONCURRENT_TASKS=5
LLM_RPM_LIMIT=60
LLM_TPM_LIMIT=60000
```

所有工作流节点与客服 Worker 的单条处理任务共享 5 个执行名额，超出后等待。
`WorkflowExecutor.execute(max_parallel=...)` 还可为单个工作流设置更小的并发窗口。
一个节点算一个工作任务；节点内部的开发子任务仍使用 Developer 自己的并发限制，
CEO 规划入口不占节点名额，避免父流程等待子节点时占满共享窗口。

所有 `LLMClient.chat()` 请求（含 JSON 调用、CEO 规划和记忆提炼）共享滚动 60 秒配额。
缓存命中不消耗 RPM/TPM；发请求前预留“输入 UTF-8 字节估算 + 最大输出 tokens”，
响应后按供应商 usage 校正。未结束的请求一直保留预留量，已结束的用量再保留 60 秒；
失败或取消但没有 usage 时保留估算量，避免未知消耗被当作免费请求。
单次预留量超过 TPM 时立即报错，应缩短输入或降低 `max_tokens`。

这些限制适用于当前单进程、单主事件循环服务，重启会清空窗口。
多个进程或服务器需要改为 Redis 共享配额；外部代码生成 CLI 的内部模型调用不受这里控制。
TPM 输入使用保守估算而非供应商 tokenizer，不能保证与供应商计费窗口完全一致；
供应商账号的其他应用调用也需要预留余量。

统一 LLM 指数退避重试由以下配置控制（默认最多重试 3 次，总共最多 4 次请求）：

```dotenv
LLM_MAX_RETRIES=3
LLM_RETRY_BASE_DELAY=1
LLM_RETRY_MAX_DELAY=30
```

仅对 HTTP 408、429、500、502、503、504、529，以及超时、网络错误、远端协议错误重试。
基础等待 1、2、4 秒，加 0～25% 随机抖动，本地退避上限 30 秒。
`Retry-After` 支持秒数和 HTTP 日期；若服务端要求更久，以服务端最低等待时间为准。
每次重试重新申请 RPM/TPM，失败尝试的未知 Token 消耗仍保留预留预算，
所以实际等待还可能包含配额排队时间。超时可能已产生供应商消耗，重试不保证免费。
认证或参数错误、无效响应解析、缓存写入失败和主动取消不会触发这套重试。
设置 `LLM_MAX_RETRIES=0` 可关闭重试；耗尽次数后向上抛出最后一次原始异常。

## 工作流与当前边界

这是借鉴 MetaGPT 角色机制和 ChatDev 图工作流的自研实现。CEO 读取
`templates/project_template.yaml` 的阶段和角色能力作为提示约束，动态生成任务及依赖；
IM 入口确认后转换为 DAG，HTTP `/api/requirement` 入口直接执行。
DFS 检测环，Kahn 分层，同层通过 `asyncio.gather(return_exceptions=True)` 执行。
节点执行层按角色找到 Agent，构造 Message 调用 `agent.run(message)`。

节点异常写入 context，下游依赖节点标记 skipped，独立分支继续执行，CEO 汇总结果。
返回错误 Message 而未抛异常的路径仍需统一失败判定。条件边和通用节点 timeout/retry
字段尚未在主执行器完整落实；PLAN_AND_ACT 当前回退到 REACT。
Agent 实例和部分上下文、记忆仍共享，当前尚未实现严格的不同用户隔离。

平台部署脚本 `scripts/deploy.sh` 负责同步与初始化，生成 `start.sh`，不会自动启动或重启。
需在远端配置 `.env` 和数据库初始化用的 PGPASSWORD；本地环境变量不会自动转发。
最近一次服务器更新因 SSH 主机密钥不匹配未完成，本地功能状态不代表线上版本。
完整运行说明见 [AGENTS.md](AGENTS.md)，产品能力与边界见 [产品规格](docs/product.md)。

opencode CLI 已默认强制 Docker 执行；本地缺少 Docker，真实容器验证待运行环境准备。详见 [配置与验证](docs/opencode-docker.md)。
