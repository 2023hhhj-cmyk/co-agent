# 多Agent协同系统 - 一人公司运营平台

## 项目概述

为一人公司打造的多Agent协同系统，自动化运营流程。

**核心业务**：为中小企业打造Agent平台

**架构特色**：融合 MetaGPT（角色机制）+ ChatDev（图工作流）

---

## Agent角色（5个）

| 角色 | 职责 | 交互对象 |
|------|------|----------|
| CEO | 接收需求（飞书）、拆解任务、调度团队、追踪进度 | 用户（飞书）→ 下级Agent |
| 开发工程师 | 理解需求、设计架构、代码实现、交付产品 | CEO（直接接收任务） |
| UI设计师 | 界面设计、交互原型 | CEO（可选参与） |
| 运维工程师 | 部署、监控、告警 | 开发完成后的产品 |
| 财务人员 | 合同、账单、成本核算 | 项目信息 |

> **注意**：没有产品经理角色。CEO 直接将客户需求转化为 Developer 可执行的详细任务描述。

---

## 系统架构

### 工作流程

```
用户（飞书）──消息──▶ CEO Agent
                        │
                        ├─ 需求不够清晰？→ 飞书追问用户
                        ├─ 需求完整？→ LLM 拆解任务
                        └─ 发确认卡片 → 用户确认
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
               Developer    UI Designer   (可选)
               (异步执行)    (同步执行)
                    │           │
                    └───────────┼───────────┘
                                ▼
                           DevOps 部署
                                ▼
                          Finance 结算
                                ▼
                         飞书推送结果给用户
```

### 企业微信接入

```
普通微信客户 ←→ 企业微信客服机器人 ←→ /api/wecom/cs/callback
                                          ↓
                                    Redis 消息队列（削峰 + 排队通知）
                                          ↓
                                    Queue Worker（5 并发）
                                          ↓
                                    Customer Service Agent
                                      ├─ 进度类问题 → PostgreSQL（projects/tasks）
                                      ├─ 使用/功能类问题 → /root/{客户}/product.md
                                      ├─ 公司通用问题 → PostgreSQL FAQ 表
                                      └─ 超出能力 → 转人工（飞书通知老板）

企业微信老板 ←→ 企业微信 CEO 机器人 ←→ /api/wecom/ceo/callback
                                          ↓
                                    CEO Agent（知识文件 + 人格文件）
```

### 融合设计

```
┌─────────────────────────────────────────────────────────────┐
│                  YAML 工作流定义 (ChatDev)                    │
│  nodes: [CEO, Dev, Design, Ops, Finance]                    │
│  edges: [条件路由, 并行分支, 循环]                             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  GraphExecutor (ChatDev)                      │
│  拓扑排序 → 分层并行执行 → 消息传递                            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  Agent 执行层 (MetaGPT)                       │
│  Role(定义) → Think → Act → Observe → Publish               │
│  + watch(订阅) + cause_by(标记) + react_mode(模式)           │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                  工具集成层                                    │
│  飞书(WebSocket) | Claude Code CLI                           │
└─────────────────────────────────────────────────────────────┘
```

### 核心组件

| 组件 | 来源 | 说明 |
|------|------|------|
| AgentRole | MetaGPT | 角色定义（name, profile, goal, constraints） |
| watch/cause_by | MetaGPT | 消息订阅和来源标记 |
| Think-Act-Observe | MetaGPT | Agent 执行循环 |
| ReactMode | MetaGPT | 反应模式（REACT/BY_ORDER/PLAN_AND_ACT） |
| GraphDef | ChatDev | DAG 图定义 |
| Topology Sort | ChatDev | 拓扑排序，支持并行 |
| Edge Condition | ChatDev | 条件路由 |
| YAML Config | ChatDev | 声明式工作流 |

---

## 工作流示例

```yaml
# templates/project_template.yaml
phases:
  - name: "设计与开发"
    required_agents: ["developer"]
    optional_agents: ["ui_designer"]
    can_parallel: true

  - name: "部署"
    required_agents: ["devops"]
    can_parallel: false

  - name: "结算"
    required_agents: ["finance"]
    can_parallel: false
```

**执行流程**：
```
Layer 1: [dev, design]   # 开发和设计并行（CEO 直接分配任务）
Layer 2: [deploy]        # 部署
Layer 3: [finance]       # 财务记录
```

---

## 技术栈

| 层次 | 技术 |
|------|------|
| LLM | DeepSeek（主）+ Claude（辅） |
| Agent编排 | 自研（MetaGPT + ChatDev 融合） |
| 消息通信 | 内存 MessageBus（发布/订阅） |
| 飞书接入 | lark-oapi SDK（WebSocket 长连接） |
| 企业微信接入 | httpx 原生调用（智能机器人 + 回调） |
| 状态存储 | Redis + PostgreSQL |
| 向量数据库 | ChromaDB |
| Web框架 | FastAPI |

---

## 项目结构

```
co-agent/
├── agents/                # 6个Agent（MetaGPT角色机制）
│   ├── base.py           # Agent基类（Think-Act-Observe）
│   ├── ceo/agent.py      # CEO：飞书 + 企业微信 + 任务拆解 + 调度
│   │   ├── product.md    # CEO 知识文件（公司和产品信息）
│   │   └── soul.md       # CEO 人格文件（性格和沟通风格）
│   ├── customer_service/agent.py # 客服：企业微信智能机器人
│   │   ├── product.md    # 客服知识文件
│   │   └── soul.md       # 客服人格文件
│   ├── developer/agent.py # 开发：Claude Code 构建产品 + 自动生成 product.md
│   ├── frontend_expert/agent.py
│   ├── devops/agent.py
│   └── finance/agent.py
├── channels/              # 通信通道层
│   ├── feishu.py         # 飞书 API + WebSocket 长连接
│   └── wecom.py          # 企业微信 API + 智能机器人
├── workflows/             # 工作流引擎（ChatDev图结构）
│   ├── graph.py          # 图定义和拓扑排序
│   ├── executor.py       # 工作流执行器
│   ├── loader.py         # YAML加载器
│   └── yaml/             # 工作流配置
├── models/                # 数据模型
│   ├── message.py        # 消息模型
│   ├── agent.py          # Agent角色定义
│   ├── task.py           # 任务模型（Task, ProjectPlan）
│   └── workflow.py       # 工作流定义
├── templates/             # 项目模板
│   └── project_template.yaml  # 阶段约束模板
├── communication/         # 消息系统
│   └── message_bus.py    # 消息总线（发布/订阅）
├── tools/llm/             # LLM工具
│   ├── client.py         # DeepSeek/Claude客户端
│   ├── router.py         # LLM路由器
│   └── prompt.py         # 提示词管理
├── tools/code/            # 代码工具
│   └── claude_code.py    # Claude Code CLI 封装
├── storage/               # 存储系统
├── config/                # 配置管理
└── main.py                # FastAPI入口
```

---

## API接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 根路径 |
| `/health` | GET | 健康检查 |
| `/api/requirement` | POST | 创建需求（触发工作流） |
| `/api/agents` | GET | Agent状态 |
| `/api/workflow` | GET | 工作流信息 |
| `/api/messages/history` | GET | 消息历史 |
| `/api/feishu/webhook` | POST | 飞书事件回调（备用） |
| `/api/wecom/ceo/callback` | GET/POST | 企业微信 CEO 机器人回调 |
| `/api/wecom/cs/callback` | GET/POST | 企业微信客服机器人回调 |
| `/api/devops/services` | GET | 所有已部署服务 |
| `/api/devops/services/{name}` | GET | 单个服务详情 |
| `/api/devops/services/{name}/restart` | POST | 重启服务 |
| `/api/devops/services/{name}/stop` | POST | 停止服务 |
| `/docs` | GET | API文档 |

---

## LLM混合策略

| 场景 | 使用 | 原因 |
|------|------|------|
| 代码审查、复杂bug修复 | Claude | 理解能力更强 |
| 简单代码生成、重构 | DeepSeek | 够用且便宜 |
| 架构设计、技术方案 | Claude | 推理能力更强 |
| 日常编码、CRUD | DeepSeek | 性价比高 |
| 实际产品构建 | Claude Code CLI | 通过 CC Switch 配置模型，支持并行执行 |
| CEO 任务拆解 | DeepSeek | 需要结构化 JSON 输出 |

**Developer Agent 工作方式**：
- 调用 Claude Code CLI 在客户专属目录下构建实际产品
- 模型选择由 CC Switch 管理（当前配置：mimo-v2.5-pro）
- 支持最多 8 个并行实例
- 最终交付成果摘要文档，而非代码本身

---

## 数据库设计

**核心表**：
- `projects` - 项目表
- `tasks` - 任务表
- `transactions` - 财务表
- `conversations` - 对话历史
- `memories` - 记忆表
- `audit_logs` - 审计日志

---

## 部署架构

**服务器**：47.82.5.164（阿里云 ECS）

**服务**：
| 服务 | 端口 | 用途 |
|------|------|------|
| FastAPI | 8080 | Agent应用 |
| Redis | 6379 | 缓存 |
| PostgreSQL | 5432 | 主数据库 |
| ChromaDB | 8000 | 向量数据库 |

---

## 项目进度

**最后更新**：2026-06-11

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1: 基础设施 | ✅ 完成 | Redis、PostgreSQL、ChromaDB、Docker |
| Phase 2: 融合架构 | ✅ 完成 | MetaGPT角色 + ChatDev图工作流 |
| Phase 3: Agent逻辑 | 🔄 进行中 | CEO ✅、Developer ✅、Frontend Expert ✅、DevOps ✅、Customer Service ✅、Finance ⏳ |
| Phase 4: 工具集成 | 🔄 进行中 | 飞书 ✅、Claude Code ✅、Docker ✅、Nginx ✅、Design Skills ✅、企业微信 ✅、其余待实现 |
| Phase 5: 记忆系统 | ⏳ 待开始 | 三层记忆、混合检索 |
| Phase 6: 安全系统 | ⏳ 待开始 | 输入验证、沙箱、权限 |
| Phase 7: 可观测性 | ⏳ 待开始 | Prometheus、Grafana、告警 |

### Agent 完成状态

| Agent | 状态 | 功能 |
|------|------|------|
| CEO Agent | ✅ 完成 | 飞书 WebSocket 通信、企业微信智能机器人、意图分类、需求追问、LLM 任务拆解（模板约束）、确认卡片、混合调度、进度追踪、知识文件（product.md + soul.md）、项目文件读取（claude.md + product.md）、Claude Code 工具 |
| 开发工程师 Agent | ✅ 完成 | 通过 Claude Code CLI 构建实际产品，支持客户项目管理，接收 CEO 直接分配的任务，自动生成 product.md |
| 前端专家 Agent | ✅ 完成 | 通过 Claude Code CLI 生成高质量前端页面（Next.js + Tailwind + shadcn/ui），注入 5 个设计 Skills，四步流水线 |
| 运维工程师 Agent | ✅ 完成 | Docker 容器化部署、Nginx 反向代理、健康检查、持续监控、异常自愈、Web 管理接口 |
| 客服 Agent | ✅ 完成 | 企业微信智能机器人、意图分类（6种）、项目进度查询、product.md 读取、FAQ、转人工、排队通知、知识文件（product.md + soul.md） |
| 财务人员 Agent | ⏳ 壳子 | 只有 LLM 文本生成，无实际记账系统 |

### CEO Agent 功能

- **飞书接入**：WebSocket 长连接，实时收发消息
- **意图分类**：LLM 判断用户意图（新需求/查进度/修改/问答）
- **需求追问**：信息不完整时主动向用户提问
- **任务拆解**：LLM 在项目模板约束内拆解为子任务
- **确认卡片**：飞书交互式卡片，用户确认后才执行
- **混合调度**：简单任务同步、复杂任务异步、独立任务并行
- **进度追踪**：实时飞书通知各阶段进度
- **即时响应**：先回"收到，正在处理..."，后台异步处理

### Developer Agent 功能

- **客户项目管理**：在 `/root/` 下为客户创建专属目录
- **Claude Code 集成**：调用 CLI 构建实际产品（网站、应用等）
- **成果交付**：生成摘要文档交付给下游 Agent
- **支持并行**：最多 8 个 Claude Code 实例并行执行
- **直接接收 CEO 任务**：无需 PM 中转

### Frontend Expert Agent 功能

- **需求分析**：LLM 分析页面类型、设计风格、组件需求，输出结构化分析报告
- **项目初始化**：通过 Claude Code CLI 创建 Next.js + Tailwind CSS + shadcn/ui 项目
- **页面实现**：注入 5 个设计 Skills（frontend-design、brand-guidelines、web-design-guidelines、react-best-practices、playwright）作为 context，生成高质量前端代码
- **审查打磨**：自动审查设计质量（视觉层次、响应式、可访问性、交互），生成 deliverable.md
- **四步流水线**：analyze_requirement → setup_project → implement_frontend → review_and_polish
- **技术栈锁定**：Next.js + Tailwind CSS + shadcn/ui + Lucide Icons，避免 AI 随机选技术栈
- **Skills 注入机制**：每次 Claude Code 调用自动加载设计规范（13701 字符），无需手动调用

### DevOps Agent 功能

- **智能项目分析**：使用 Claude Code 分析项目目录，自动识别技术栈
- **Docker 容器化部署**：自动构建镜像、启动容器、端口管理（8001-9000）
- **Nginx 反向代理**：路径方式访问（`http://loveboos-ai.com/{客户名}`）
- **健康检查**：HTTP 检查 + 容器状态监控，每 30 秒一次
- **异常自愈**：自动重启（最多 3 次），失败后通知 CEO
- **Web 管理接口**：查看服务状态、重启、停止等操作
- **飞书告警**：服务异常时自动通知 CEO
- **智能缓存**：项目分析结果缓存 24 小时，相同项目秒级返回

### 客服 Agent 功能

- **企业微信接入**：通过智能机器人接入，客户在微信内直接对话
- **意图分类**：LLM 判断客户意图（项目进度/使用方法/功能需求/公司FAQ/打招呼/未知）
- **项目进度查询**：从 PostgreSQL 查询客户关联项目的任务进度
- **产品知识问答**：读取客户项目的 product.md，回答使用方法和功能问题
- **公司 FAQ**：从数据库查询公司常见问题答案
- **转人工机制**：无法回答时诚实转人工，通过飞书通知老板
- **排队通知**：消息队列削峰，5 并发处理，排队时提示客户等待人数
- **消息撤回**：处理完成后撤回排队提示消息，保持聊天框整洁
- **会话管理**：每客户独立会话上下文，Redis 缓存 + PostgreSQL 持久化
- **知识文件**：product.md（公司和产品信息）+ soul.md（客服人格定义）

### CEO Agent 知识系统

- **product.md**：公司简介、核心业务、系统能力、服务范围、技术栈
- **soul.md**：人格定义、性格特征、沟通风格、能力边界、回复模板
- **项目文件读取**：遍历所有项目的 `claude.md`（项目概述）和 `product.md`（项目进度）
- **Claude Code 工具**：当项目文件信息不足时，调用 Claude Code 浏览项目代码
- **自动加载**：Agent 启动时自动加载知识文件，回答问题时参考
- **动态更新**：修改知识文件后重启服务即可生效

### Developer Agent 产品文档生成

- **自动生成**：构建完成产品后，自动调用 LLM 生成 product.md
- **内容包含**：项目概述、已实现功能、技术栈、使用说明、已知限制、待实现功能
- **客服使用**：客服 Agent 读取此文件回答客户关于项目的使用和功能问题
- **文件位置**：`/root/{客户名}/product.md`，与项目代码同级目录

**总体进度**：约 **80%** 完成

---

## 后续计划

- [x] ~~强化 UI Designer Agent~~ → 替换为前端专家 Agent（Next.js + Tailwind + shadcn/ui，Skills 注入）
- [x] 强化 DevOps Agent（Docker 部署 + Nginx 反向代理 + 监控）
- [x] 优化 DevOps Agent（Claude Code 分析优化、缓存机制）
- [x] 创建客服 Agent（企业微信智能机器人、意图分类、知识问答、转人工）
- [x] 企业微信接入（智能机器人、消息收发、排队通知、消息撤回）
- [x] CEO Agent 知识系统（product.md + soul.md，自动加载、动态更新）
- [x] Developer Agent 产品文档生成（构建完成后自动生成 product.md）
- [ ] 强化 Finance Agent（接入存储层真正记账）
- [ ] 修复工作流执行器（当前 POST /api/requirement 路径未真正调用 Agent，需接通 CEO 调度逻辑）
- [ ] 记忆系统（三层记忆 + ChromaDB 向量检索）
- [ ] 安全系统（输入校验、沙箱、权限控制）
- [ ] 可观测性（OpenTelemetry + Prometheus + Grafana）
- [ ] 测试框架
- [ ] Web 管理界面（DevOps 管理界面）
