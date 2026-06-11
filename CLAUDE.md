# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

多Agent协同系统 - 一人公司运营平台，融合 **MetaGPT 角色机制** + **ChatDev 图工作流**。

核心业务：为中小企业打造Agent平台，自动化运营流程。完整产品规格见 `product.md`。

## 服务器与部署

- SSH连接：`ssh muzhu-ai`（配置在 `~/.ssh/config`，root@47.82.5.164，密码认证）
- 服务器项目目录：`/root/muzhu-ai/`
- 部署：`./scripts/deploy.sh`（rsync 同步 + 远程安装依赖 + 初始化数据库）
- 服务器启动：`cd /root/muzhu-ai && ./start.sh`

## 常用命令

```bash
# 本地运行
source venv/bin/activate
python main.py                     # 启动 FastAPI 服务 (0.0.0.0:8080)

# 安装依赖
pip install -r requirements.txt

# 数据库初始化（需要 PostgreSQL 运行）
psql -h localhost -U muzhu -d muzhu_agent -f scripts/init_db.sql
```

> 项目目前没有测试框架、linter 配置或 Makefile。`tests/` 目录为空。

## 环境变量

在 `.env` 中配置（模板见 `.env.example`）。至少需要设置 `DEEPSEEK_API_KEY` 或 `ANTHROPIC_API_KEY`，否则 LLM 调用无法工作。

关键配置：`HOST`、`PORT`（默认8080）、`DATABASE_URL`、`REDIS_URL`、`CHROMA_HOST`。

## 架构

### 三层融合设计

```
YAML 工作流定义 (ChatDev)
    ↓
WorkflowGraph → WorkflowExecutor（拓扑排序，分层并行）
    ↓
Agent 执行层 (MetaGPT Think-Act-Observe)
    ↓
工具层（LLM Router → DeepSeek/Claude）
```

### 工作流引擎（`workflows/`）

- **DAG 定义**：YAML 文件在 `workflows/yaml/`，当前主流程 `requirement_flow.yaml`
- **WorkflowGraph**（`graph.py`）：Kahn 拓扑排序 + DFS 环检测 + 条件边路由
- **WorkflowExecutor**（`executor.py`）：按拓扑层执行，同层节点 `asyncio.gather` 并行
- **主流程**：CEO → PM → [Dev + Design 并行] → Review → Deploy → Finance

### Agent 系统（`agents/`）

- **BaseAgent**（`base.py`）：抽象基类，实现 Think-Act-Observe 循环
- 三种反应模式（`ReactMode`）：
  - `REACT`：LLM 动态选择动作（CEO、Developer 使用）
  - `BY_ORDER`：顺序执行（PM、Designer、DevOps、Finance 使用）
  - `PLAN_AND_ACT`：计划后执行（尚未实现，回退为 REACT）
- Agent 通过 `watch` 集合订阅消息类型，只处理匹配 `cause_by` 的消息
- 子类必须实现 `_think()` 和 `_act()`

### 消息通信（`communication/`）

- **MessageBus**（`message_bus.py`）：发布/订阅模式，非 Redis Streams
- 消息路由：`send_to="*"` 广播，按 `watch` 集合过滤；指定 `send_to` 则定向投递
- 过滤自身消息（`sent_from == self` 不投递）

### LLM 路由（`tools/llm/`）

- **LLMRouter**（`router.py`）：Agent 角色 → 任务类型 → LLM 提供者
  - DeepSeek（主）：简单代码、PRD、设计、文档、通用任务
  - Claude（辅）：复杂代码、代码审查、架构设计
- 两个客户端（`client.py`）均为 httpx 原生调用，非 OpenAI/Anthropic SDK 封装
- **PromptManager**（`prompt.py`）：管理 system/action prompt 模板

### 部署工具（`tools/deploy/`）

- **ProjectAnalyzer**（`analyzer.py`）：使用 Claude Code 分析项目目录，生成 Dockerfile 和 docker-compose.yml
- **CacheManager**（`cache.py`）：缓存项目分析结果，24 小时过期
- **DockerManager**（`docker.py`）：Docker 容器管理（构建、运行、停止、日志）
- **PortManager**（`docker.py`）：端口分配管理（8001-9000）
- **NginxManager**（`nginx.py`）：Nginx 反向代理配置管理
- **ServiceMonitor**（`monitor.py`）：健康检查和持续监控（每 30 秒）

### 数据模型（`models/`）

- `agent.py`：AgentRole、ActionDef、ReactMode
- `message.py`：Message、MessageQueue、MessageRole
- `workflow.py`：WorkflowDef、GraphDef、NodeDef、EdgeDef、NodeType
- `deploy.py`：DeployRecord、ServiceStatus、ServiceHealth（DevOps 部署记录）

### 存储（`storage/`）

- **Database**（`database.py`）：asyncpg 连接 PostgreSQL
- **StateManager**（`state_manager.py`）：redis.asyncio 缓存
- 数据库 Schema（7 张表）：projects、tasks、agent_states、conversations、memories、audit_logs、transactions

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/requirement` | 提交需求，触发完整工作流 |
| GET | `/api/agents` | 获取所有 Agent 状态 |
| GET | `/api/workflow` | 获取工作流拓扑信息 |
| GET | `/api/messages/history` | 获取消息历史 |
| GET | `/api/devops/services` | 获取所有已部署的服务 |
| GET | `/api/devops/services/{name}` | 获取单个服务详情 |
| POST | `/api/devops/services/{name}/restart` | 重启服务 |
| POST | `/api/devops/services/{name}/stop` | 停止服务 |

## 实现进度

- ✅ 基础设施（Redis、PostgreSQL、ChromaDB、Docker）
- ✅ 融合架构（MetaGPT 角色 + ChatDev 图工作流）
- ✅ Agent 逻辑（CEO、Developer、DevOps）
- ✅ 工具集成（飞书、Claude Code、Docker、Nginx）
- ⬜ 记忆系统（三层记忆、混合检索）— `memory/` 空
- ⬜ 安全系统（输入校验、沙箱、权限）— `security/` 空
- ⬜ 可观测性（Prometheus、Grafana、告警）— `observability/` 空

空的待实现模块：`memory/`、`security/`、`observability/`、`acceleration/`、`tools/design/`、`tools/document/`、`tools/project/`（均只有空 `__init__.py`）
