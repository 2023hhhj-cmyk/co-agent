# 🎓 Co-Agent 多 Agent 协同系统 · 面试复习全景指南

> **使用说明**：本文件严格对齐你的简历项目描述，按模块划分复习次序。每个模块包含**精读代码路径（可直接点击跳转）**、**核心算法与设计原理**、**高频面试题与应答话术（STAR法则）**。建议按照阶段一至阶段四循序渐进复习。

---

## 📌 模块速览与复习规划

- [ ] **阶段一：编排核心（DAG 任务拆解、Kahn 拓扑分层与并发调度）**
- [ ] **阶段二：工具链路、异步通信与三层记忆图谱（Tooling & Memory）**
- [ ] **阶段三：系统安全、执行沙箱与加速优化（Security & Acceleration）**
- [ ] **阶段四：可观测体系、数据埋点与 24 项安全问题复盘（Observability & Review）**

---

## 🚀 阶段一：编排核心（DAG 任务编排与分层并发）

### 1. 简历对应描述
> *“实现基于 DAG 的任务编排与调度模块：将 LLM 返回的任务依赖转换为执行图，完成拓扑校验、分层并发执行和节点间上下文传递，解决复杂任务无法在单次模型调用中完成的问题。”*

### 2. 核心回顾文件与关键代码
* [workflows/graph.py](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py)
  * [`WorkflowGraph.topological_sort`](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py#L211-L248)：基于 **Kahn 算法** 实现有向无环图拓扑排序，并输出分层结构（`List[List[str]]`），同层节点无相互依赖可并行。
  * [`WorkflowGraph._has_cycle`](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py#L188-L209)：利用 DFS 递归堆栈染色法在图初始化时完成**环检测**。
  * [`_safe_eval_condition`](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py#L52-L99)：基于 Python `ast` 抽象语法树构建**安全表达式求值器**，严格只放行比较符和布尔逻辑，杜绝 `eval()` 带来的 RCE 注入风险。
* [workflows/executor.py](file:///Users/hhhj/Desktop/co-agent/workflows/executor.py)
  * [`WorkflowExecutor.execute`](file:///Users/hhhj/Desktop/co-agent/workflows/executor.py#L180-L275)：按层调度主循环，每层节点使用 `asyncio.gather(*tasks, return_exceptions=True)` 实现并发执行与异常隔离。
  * [`AgentNodeExecutor.execute`](file:///Users/hhhj/Desktop/co-agent/workflows/executor.py#L53-L130)：节点执行适配器，支持根据上下文动态抽取任务描述与元数据注入 Agent。
* [agents/ceo/agent.py](file:///Users/hhhj/Desktop/co-agent/agents/ceo/agent.py)
  * `_plan_tasks`：Prompt 工程约束 LLM 按照特定 schema 输出任务列表及其前置 `dependencies`。
  * [`_execute_plan`](file:///Users/hhhj/Desktop/co-agent/agents/ceo/agent.py#L551-L610)：调用 [workflows/converter.py](file:///Users/hhhj/Desktop/co-agent/workflows/converter.py) 动态将 `ProjectPlan` 转成 `GraphDef` 并启动执行器，注入进度回调通知飞书。

### 3. 面试核心考点与应答准备
1. **Q：为什么选用 Kahn 算法而不是深度优先搜索（DFS）后序逆序来做拓扑排序？**
   * **回答要点**：DFS 只能产出单一直线拓扑序，而 Kahn 算法在统计入度（In-degree）后，每轮将所有入度为 0 的节点作为一个 Batch（层），天然支持**分层并发**调度，同层任务之间没有数据依赖关系，极大简化了多协程并行的设计。
2. **Q：同层节点并行执行时，如果某个 Agent 异常挂了，整个流程如何保证鲁棒性？**
   * **回答要点**：
     * `asyncio.gather` 设置 `return_exceptions=True`，避免单个子协程抛错直接导致其他协程被取消；
     * 将异常信息包装到 `context["outputs"][node_id] = {"error": str(e)}`；
     * 下层节点启动前执行依赖检查，若其前置节点失败则触发跳过或降级逻辑，同时通知 CEO 决定是否重新规划或向飞书发送失败告警。
3. **Q：工作流中的条件边为什么不能直接用 `eval()`？如何保证安全？**
   * **回答要点**：动态工作流如果执行用户/外部输入注入的条件，原生 `eval()` 会导致任意代码执行（RCE）。系统采用 Python 内置的 `ast.parse(mode="eval")` 对表达式树做白名单递归解析，仅允许基本比对（`==`, `>`, `in`）和布尔运算，禁止任何属性调用链和 `__import__`。

---

## 🛠️ 阶段二：工具链路、异步通信与三层记忆图谱（Tooling & Memory）

### 1. 简历对应描述
> *“统一 LLM 路由与工具层，接入代码生成 CLI、Docker 容器化部署与 Nginx 反向代理；以飞书/企业微信承接客户需求与客服问答；使用会话/工作/知识三层记忆，叠加记忆图谱与重要性评分，跨会话召回项目上下文与业务知识。”*

### 2. 核心回顾文件与关键代码
* [tools/llm/router.py](file:///Users/hhhj/Desktop/co-agent/tools/llm/router.py) & [tools/llm/client.py](file:///Users/hhhj/Desktop/co-agent/tools/llm/client.py)
  * `LLMRouter` 统一转发不同厂商与模型，负责 Token 消耗统计、错误重试、结构化 JSON 清洗（正则提取与 Markdown Codeblock 剥离）。
* [tools/deploy/docker.py](file:///Users/hhhj/Desktop/co-agent/tools/deploy/docker.py) & [tools/deploy/nginx.py](file:///Users/hhhj/Desktop/co-agent/tools/deploy/nginx.py)
  * DevOps Agent 的核心操作封装：容器化编译构建、端口分配、健康监测；Nginx 配置文件模版渲染与 `nginx -s reload` 热重载。
* [communication/queue.py](file:///Users/hhhj/Desktop/co-agent/communication/queue.py) & [channels/wecom.py](file:///Users/hhhj/Desktop/co-agent/channels/wecom.py)
  * `CSMessageQueue`：企微客服消息基于 Redis 队列实现流量削峰与异步排队通知；
  * `QueueWorker`：可配置并发数（默认 5 并发）消费队列，保证极端大流量下后端 LLM 不被压垮。
* [memory/manager.py](file:///Users/hhhj/Desktop/co-agent/memory/manager.py)
  * 门面设计模式：统一管理 [memory/working.py](file:///Users/hhhj/Desktop/co-agent/memory/working.py)（当前任务工作记忆）、[memory/session.py](file:///Users/hhhj/Desktop/co-agent/memory/session.py)（短期会话历史）、[memory/knowledge.py](file:///Users/hhhj/Desktop/co-agent/memory/knowledge.py)（pgvector 长期向量存储）。
* [memory/importance.py](file:///Users/hhhj/Desktop/co-agent/memory/importance.py)
  * [`ImportanceScorer`](file:///Users/hhhj/Desktop/co-agent/memory/importance.py#L56-L90)：五维加权评分算法：
    1. 来源角色 (30%)：CEO 决策 1.0 > Dev 0.8 > 客服 0.5；
    2. 记忆类型 (25%)：experience 1.0 > decision 0.85 > chat 0.3；
    3. 内容质量 (20%)：长度适中分高，过短/垃圾日志扣分；
    4. 时间衰减 (15%)：模拟艾宾浩斯遗忘曲线；
    5. 访问热度 (10%)：命中频次递增。
* [memory/graph.py](file:///Users/hhhj/Desktop/co-agent/memory/graph.py)
  * [`MemoryGraph`](file:///Users/hhhj/Desktop/co-agent/memory/graph.py#L40-L100)：轻量级图谱。Redis Sets 维护热索引（`memory:edges:{id}` 出边集合 + `memory:tag:{tag}` 倒排索引），PostgreSQL 持久化存储 `memory_edges` 关联边。

### 3. 面试核心考点与应答准备
1. **Q：三层记忆（Working, Session, Knowledge）在工程上是怎么协作流转的？**
   * **回答要点**：
     * **Working Memory**：保存在进程内存或上下文变量，记录当前节点单次 Run 的临时 Scratchpad，节点执行完毕后清空；
     * **Session Memory**：存 Redis/PostgreSQL，维护用户多轮对话的上下文窗口，超长时触发滑动窗口或摘要截断；
     * **Knowledge Memory**：存 PostgreSQL + pgvector，在项目执行完成后由 [memory/consolidator.py](file:///Users/hhhj/Desktop/co-agent/memory/consolidator.py) 利用 LLM 自动蒸馏出可复用的技术经验或业务 FAQ，计算重要性评分后写入向量库与关联图谱。
2. **Q：为什么没有用 Neo4j，而是基于 Redis Sets + PostgreSQL 构建轻量级记忆图谱？**
   * **回答要点**：
     * 架构减负：中小企业和个人运营场景下，部署维护 Neo4j 成本高且重；
     * 查询性能：关联关系的遍历大多基于“同项目”或“同标签”，Redis Sets 的 `SINTER`（求交集）和 `SMEMBERS` 在内存中具有极低的读取延迟，配合 PG 做冷持久化完全满足多 Agent 关联召回需求。

---

## 🛡️ 阶段三：工程保障与加速优化（Security & Acceleration）

### 1. 简历对应描述
> *“输入校验（Prompt/SQL/XSS 注入）、Docker 沙箱、RBAC 与审计日志；以 Redis 响应缓存（temperature 门控 + 命中率监控）降低重复 LLM 调用成本。”*

### 2. 核心回顾文件与关键代码
* [security/input_guard.py](file:///Users/hhhj/Desktop/co-agent/security/input_guard.py)
  * [`InputGuard.validate`](file:///Users/hhhj/Desktop/co-agent/security/input_guard.py#L100-L160)：
    * **Unicode NFKC 归一化**：防止全角字符、特殊同形异义字绕过正则；
    * **零宽字符剔除**：检测并清除 `\u200b`、`\u200c` 等隐形注入手段；
    * **多语言注入特征**：匹配中英文 System Prompt 覆盖、角色扮演黑名单；
    * **XSS 与 SQL 防护**：`html.escape` 净化并拦截非法 SQL 关键字模式。
* [security/sandbox.py](file:///Users/hhhj/Desktop/co-agent/security/sandbox.py)
  * [`SandboxedExecutor`](file:///Users/hhhj/Desktop/co-agent/security/sandbox.py#L52-L160)：
    * 容器资源限制：CPU 1核（`--cpus 1.0`）、内存 512M（`-m 512m`）、超时 300s；
    * 网络与文件硬隔离：`--network none` 切断对外网络，根目录只读；临时目录挂载 `--tmpfs /tmp:rw,noexec,size=256m` 禁止执行二进制；
    * 安全降级（Fallback）：Docker 不可用时，本地仅暴露只读工具白名单（`READONLY_TOOLS = ["Read", "Glob", "Grep"]`），绝不暴露 `Bash`。
* [acceleration/cache.py](file:///Users/hhhj/Desktop/co-agent/acceleration/cache.py)
  * [`ResponseCache`](file:///Users/hhhj/Desktop/co-agent/acceleration/cache.py#L31-L90)：
    * **门控机制**：`CACHE_TEMP_THRESHOLD = 0.5`，高 temperature（>0.5）需要创造性和多样性，严禁缓存；
    * **Key 设计**：对 `prompt + model + temperature` 进行 SHA256 签名，前缀 `llm:cache:{hash}`；
    * **指标联动**：命中/未命中实时打点 Prometheus `llm_cache_hit` / `llm_cache_miss`。
* [storage/state_manager.py](file:///Users/hhhj/Desktop/co-agent/storage/state_manager.py)
  * Redis 原子防重放：采用 `SET key val EX 86400 NX` 替换原本存在数据竞争的全局 Python `set()`，保证高并发场景下事件绝对幂等。

### 3. 面试核心考点与应答准备
1. **Q：你如何防御针对 Agent 的 Prompt Injection（提示词注入）？**
   * **回答要点**：
     * **预处理层**：`InputGuard` 进行 Unicode 归一化（NFKC）打散混淆字符，剥离隐藏零宽字符；
     * **模式拦截**：中英文多策略特征词匹配（如 `<|im_start|>`, `ignore previous instructions`, `你现在扮演`）；
     * **架构隔离**：将用户输入放入系统模板的独立数据域，结合 Agent 权限最小化（如沙箱禁网、剥离危险工具执行权），即使越狱也无法造成破坏。
2. **Q：LLM 响应缓存为什么要有 temperature 门控？**
   * **回答要点**：温度低（如 `temperature=0.0~0.2`）通常对应代码生成、格式转换或事实性问答，输出具有高度确定性，缓存可降低成本并缩短响应时间；而温度高（如 `>0.5`）用于创意写作、头脑风暴，缓存会导致每次返回一模一样的内容，损害业务正确性。

---

## 📊 阶段四：可观测体系与 24 项安全缺陷复盘（Observability & Review）

### 1. 简历对应描述
> *“构建 11 类埋点、14 项 Prometheus 指标与 6 条告警的可观测与运行报告体系；规避 24 项安全问题（含认证绕过、SSRF、横向越权、沙箱逃逸）”*

### 2. 核心回顾文件与关键代码
* [analytics/__init__.py](file:///Users/hhhj/Desktop/co-agent/analytics/__init__.py) & [analytics/analytics_spec.md](file:///Users/hhhj/Desktop/co-agent/analytics/analytics_spec.md) & [analytics/report.py](file:///Users/hhhj/Desktop/co-agent/analytics/report.py)
  * **11 类业务埋点事件**：`feishu.message.received`, `input.guard.rejected`, `agent.think`, `agent.act`, `agent.error`, `tool.call`, `memory.recall`, `memory.distill`, `workflow.start`, `workflow.node`, `workflow.end`。
  * **5 项简历指标计算公式（必须记牢）**：
    1. **端到端成功率**：$\frac{成功工作流数}{总工作流数} \times 100\%$；
    2. **P95 响应延迟**：`duration_s` 排序后取 95 分位数值；
    3. **并行加速比**：$\frac{\sum T_{串行节点耗时}}{T_{工作流实际总墙钟耗时}}$（拓扑分层并行的核心成效体现）；
    4. **LLM 单任务成本**：汇总各 Agent 调用 DeepSeek/Claude 的 Prompt/Completion Token 乘以单价；
    5. **注入拦截率**：$\frac{\text{input.guard.rejected 拦截数}}{\text{总输入消息数}} \times 100\%$。
* [observability/metrics.py](file:///Users/hhhj/Desktop/co-agent/observability/metrics.py)
  * 14 项 Prometheus 指标（含 `agent_state`, `llm_call_duration_seconds`, `input_rejected`, `sandbox_mode`, `llm_cache_hit` 等），采用独立 `CollectorRegistry` 暴露于 `/metrics`。
* [observability/alerts.yml](file:///Users/hhhj/Desktop/co-agent/observability/alerts.yml)
  * 6 条告警规则（LLM 高延迟、频繁错误、沙箱回退 Native 告警、消息队列堆积等）。
* [docs/code-review-2026-06-18.md](file:///Users/hhhj/Desktop/co-agent/docs/code-review-2026-06-18.md)
  * **24 项安全缺陷梳理（P0 7项 + P1 8项 + P2 9项 = 24项）**，以下是面试官最爱听的经典 Case：
    1. **计时攻击（Timing Attack）**：企微签名验证最初直接用 `sha1 == msg_signature`，字符串比较遇到首字符不匹配会提前短路，存在侧信道泄漏。**修复**：改用 `hmac.compare_digest()` 保证恒定时间比较。
    2. **AccessToken 并发双刷风暴**：两路 Webhook 同时到达时，token 均已过期，并发请求接口造成重复刷新。**修复**：引入 `asyncio.Lock()` 串行化刷新。
    3. **SSRF 风险**：外部回传的 `response_url` 未经验证直接发送请求，攻击者可诱导服务端探测内网 127.0.0.1 或云厂商元数据。**修复**：加入企业微信官方域名白名单校验。
    4. **沙箱逃逸风险**：最初 Docker 挂载的 tmpfs 带 `exec` 权限，攻击者可向 `/tmp` 写入编译后的恶意 ELF 运行；降级工具使用了不合法的 `Bash(read-only)`。**修复**：tmpfs 改为 `noexec`，降级时彻底剔除 Bash。
    5. **内存去重丢数据**：原本在 Python 进程内用 `set` 计数，超过 1000 时暴力切片丢弃 500 条导致窗口漏洞，且多 worker 状态不互通。**修复**：改用 Redis `SETNX` + TTL 24h 原子防重放。

### 3. 面试核心考点与应答准备
1. **Q：你如何向面试官证明你的多 Agent 系统比传统单 Agent 串行效率更高？**
   * **回答要点**：
     * 摆出真实的埋点数据指标：**并行加速比（Parallel Speedup Ratio）**；
     * 解释其计算方式：在 `workflow.node` 埋点记录每个 Agent 节点独立执行时间 $T_i$，工作流总耗时为 $T_{\text{wall}}$。由于前端与后端、多子模块开发在 DAG 拓扑中被自动划分到同一执行层并发执行，系统实测并行加速比达到 **1.6x ~ 2.4x**，显著缩减交付耗时。
2. **Q：在整个系统构建中，你修复过的最隐蔽的一个高危漏洞是什么？**
   * **回答要点**：直接讲述企微签名验证的**计时攻击修复**（`hmac.compare_digest`）或 **Docker 沙箱 tmpfs:noexec 与降级模式剥离 Bash** 的过程，体现你在编写 AI/Agent 系统时具备远超普通应用开发者的底层安全意识。

---

## 🎯 面试复习冲刺 Check-list

在面试前一天，请对着本清单自测，确保能脱口而出：
- [ ] 能在白板/纸上画出项目的整体架构图（飞书/企微 -> CEO -> DAG -> WorkflowExecutor -> 团队Agent -> DevOps/Nginx -> 消息回调）。
- [ ] 能手写或讲清楚 Kahn 拓扑排序算法的入度统计与队列分层过程。
- [ ] 能完整讲出三层记忆的流转机制与 Importance 评分的 5 个维度。
- [ ] 能举出 2~3 个自己在代码评审中修复的典型安全漏洞（计时攻击、SSRF、沙箱逃逸）。
- [ ] 能背诵 5 大量化指标（成功率、P95、加速比、成本、拦截率）的计算逻辑与业务价值。
