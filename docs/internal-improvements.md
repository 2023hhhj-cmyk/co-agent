# Agent 内部通信：待解决的四个问题

> 记录时间：2026-10-23
> 状态：全部未开始，按下面顺序逐个解决
> 背景：当前没有消息总线，Agent 之间是「CEO 星型派单 + 共享 context + 共享记忆」

## 1. 上游产物 300 字截断

**现象**
`agents/ceo/agent.py` 的 `_fill_assignment()` 拼任务描述时，上游结果只取
`content[:300]`。文件清单、验收细节、失败原因基本丢失，下游只能重新扫描工作区。

**影响**
任务链越长、产出越结构化，信息丢失越严重；下游行为依赖"猜"和"再扫一遍"。

**建议方案**
结构化交接 `Handoff{files: [], summary, acceptance, artifacts}` 放进
`task_metadata`，下游按需读取；文本描述只保留摘要。

**改动面**：`agents/ceo/agent.py::_fill_assignment`、`workflows/executor.py::AgentNodeExecutor`

---

## 2. 无消息级持久化

**现象**
`context["outputs"]` 是内存 dict，进程崩溃即丢。`audit_logs` 只记任务级结果，
不是消息级的交接记录，无法重放某一次 Agent → Agent 的传递。

**影响**
故障复盘只能看日志文本；无法回放、无法从中间节点续跑。

**建议方案**
新增 `agent_messages` 表（id / trace_id / sent_from / send_to / cause_by /
content / metadata / created_at），交接时落库；提供按 trace 回放。

**改动面**：新增表 + `workflows/executor.py` 写入点 + `MemoryManager` 可选读取

---

## 3. 纯同步、无事件驱动

**现象**
`WorkflowExecutor` 按层 `asyncio.gather`，必须等整层所有节点结束才进入下一层。
同层任务耗时差异大时产生木桶效应（2 秒的配置任务等 60 秒的编码任务）。

**影响**
端到端延迟被最慢节点拖住；空闲槽位无法利用。

**建议方案**
依赖计数 + 完成即触发：每个节点维护未完成前置数量，归零即提交执行，
不再按层屏障。

**改动面**：`workflows/executor.py` 主循环（`execute` / `dispatch`）

---

## 4. 共享实例、共享记忆

**现象**
`agents` 字典是进程级单例，`memory_manager` 全局共享，CEO 计划只按 `chat_id` 管理。
跨客户、跨会话的上下文存在串味风险，不是严格的用户隔离架构。

**影响**
多客户并行时可能读到别人的记忆；权限与数据边界只在入口做了一次。

**建议方案**
按 `session_id`（客户/项目）划分上下文与记忆命名空间；Agent 实例改为
每 session 绑定上下文对象，而不是共享可变状态。

**改动面**：`agents/base.py`（上下文注入）、`memory/manager.py`（命名空间）、
`main.py`（session 传递）

---

## 建议顺序

1. 第 1 项（半天，立刻提升可靠性，可当场验证）
2. 第 3 项（1 天，性能收益最直观）
3. 第 2 项（1–2 天，为复盘和续跑打基础）
4. 第 4 项（最伤筋动骨，放最后）
