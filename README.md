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
| 缓存 | Redis + ChromaDB（向量存储） |
| LLM | DeepSeek |
| 消息 | 飞书 WebSocket + 企业微信回调 |
| 部署 | Docker + Nginx 反向代理 |
| 可观测性 | Prometheus + OpenTelemetry |

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
| Finance | 合同、账单、成本核算 |
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
tools/           LLM 客户端 + Claude Code + 部署工具
workflows/       工作流引擎（DAG + 拓扑排序）
```

## 安全

- **输入校验**：Prompt 注入 / SQL 注入 / XSS 检测
- **沙箱执行**：Docker 容器隔离 + 降级只读模式
- **访问控制**：Agent 间 RBAC + API Key 认证
- **审计日志**：全操作记录到 PostgreSQL
- **速率限制**：全局 60 req/min，Webhook 120 req/min
- **安全头**：nosniff / DENY / HSTS
