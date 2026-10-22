# 面试复习备战 (Interview Preparation)

## 编排核心

### 动态/静态建图：
节点并非静态写死，而是由 [CEO Agent](file:///Users/hhhj/Desktop/co-agent/agents/ceo/agent.py) 基于业务 SOP 模板（[`project_template.yaml`](file:///Users/hhhj/Desktop/co-agent/templates/project_template.yaml)）进行动态规划： 
- CEO Agent 拿到自然语言需求后拆解成任务列表（[`ProjectPlan`](file:///Users/hhhj/Desktop/co-agent/models/task.py#L126)）；  
- 通过 [`PlanToGraphConverter`](file:///Users/hhhj/Desktop/co-agent/workflows/converter.py#L24) 把任务转换为节点（[`NodeDef`](file:///Users/hhhj/Desktop/co-agent/models/workflow.py#L26)），把任务间的依赖关系转换为边（[`EdgeDef`](file:///Users/hhhj/Desktop/co-agent/models/workflow.py#L52)）。

**核心流程**：我的工作流程是，当我向ceo agent下达某个需求的指令时，ceo agent会先根据一个业务模板将需求拆解为任务列表和依赖关系，向我确认后通过把任务转换为节点，任务之间的依赖关系转化为边来形成一个图。程序会先使用DFS染色法来确认图中无环，接下来工作流调度执行器会使用kahn算法来计算分层，使用asyncio.gather方法来分层并发调用同层节点，每个节点会由另一个执行器来执行agent。

### 图合法性保障（环检测）：
在执行前，先用 [`WorkflowGraph._has_cycle`](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py#L188-L209)（DFS 染色法）确保图中没有任何死锁或循环依赖。

### 分层并行执行（Kahn 算法）：
用 [`topological_sort`](file:///Users/hhhj/Desktop/co-agent/workflows/graph.py#L211-L248) 计算拓扑分层（入度为 0 的批次）；  
执行器在同一层内通过 `asyncio.gather` 触发无依赖节点的并发执行（比如前端专家与后端专家同时开工，大幅压缩端到端耗时）。

### 并发调度核心考点（面试高频）：

1. **为什么用 Kahn 算法而非 DFS 做拓扑排序？**
   - **DFS 局限**：产出单一直线拓扑序，无法识别哪些任务可并行；
   - **Kahn 优势**：基于入度与 BFS 逐层出队，同一层（入度为 0）节点彼此无依赖，天然输出 `List[List[str]]` 分层并发结构。

2. **为什么选择 `asyncio` 异步协程而非多线程/多进程？**
   - **I/O 密集型适配**：Agent执行的大部分时间在等待 LLM API、Docker 引擎或网络响应；
   - **极低切换开销**：单线程事件循环无锁竞争、无系统线程上下文切换开销，单机高并发承载能力远高于线程池/进程池。

3. **并发流量如何控制，防止冲垮系统或触发大模型限流（Rate Limit）？**
   - **并发窗口控制**：在 [`WorkflowExecutor`](file:///Users/hhhj/Desktop/co-agent/workflows/executor.py#L166) 中支持 `max_parallel` 参数，结合 `asyncio.Semaphore` 限制同层最大并发协程数；
   - **客户端防击穿**：在 LLM Router 层配合指数退避重试（Exponential Backoff）和滚动窗口流控，规避供应商的 RPM/TPM 配额超限。
   我通过共享 Semaphore 限制整个服务同时执行的工作任务数量，在统一 LLM 客户端使用滚动窗口控制 RPM 和 TPM。请求前预留 Token 预算，响应后按实际用量校正；对于临时错误，采用带随机抖动的有限次数指数退避重试，并确保重试仍遵守配额限制。

4. **同层某个 Agent 异常崩溃，流程如何保证鲁棒性（容错与异常隔离）？**
   - **异常隔离**：在 [`workflows/executor.py`](file:///Users/hhhj/Desktop/co-agent/workflows/executor.py#L221) 中使用 `asyncio.gather(*tasks, return_exceptions=True)`，防止单个子任务抛错导致同层其他正常任务被中断；
   - **降级与拦截**：异常被封装存入 `context["outputs"][node_id] = {"error": ...}`；下一层启动前校验前置依赖，若依赖失败则触发动态跳过/降级，并上报 CEO 触发飞书告警。
   首先，agent会检测到任务的异常，记录错误日志并向上抛出，工作流通过return_exceptions来收集失败存入context中，接下来，依赖这一失败节点的下游节点会自动跳过，不受影响的节点继续执行，ceo agent最后会汇总结果向我报告。

5. **并发写入执行上下文（`context`）是否存在竞态条件（Race Condition）？**
   - **逻辑隔离**：各 Agent 仅向 `context["outputs"][node_id]` 写入属于自己的命名空间 Key，键空间物理隔离无互斥冲突；
   - **单线程事件循环机制**：协程纯内存字典赋值不发生线程交替（仅在 `await` 处让出执行权），无并发死锁或脏写风险。

6. **🔥进阶追问：按层并发（Layer-by-Layer）有什么缺点？如何优化？**
   - **木桶效应（Straggler Problem）**：同层任务耗时不同（如快速配置 2s vs 复杂编码 60s），下一层即使只依赖已完成的任务，也必须被迫等待本层所有任务结束；
   - **进阶优化方案**：演进为**基于事件驱动的响应式调度（Event-driven / Promise 机制）**，每个节点仅监控自身直接前置依赖，依赖满足立刻触发运行，无需等待同层无关节点，进一步消除长尾等待。

---

## 运维与基础设施：Nginx 反向代理核心考点

### 核心概念全景（快速背诵）：
- **反向代理**：挡在服务器前面统一迎客，保护后端，隐藏细节；
- **Upstream**：定义后端服务器池，方便横向轻松扩展；
- **负载均衡**：通过轮询、权重、IP Hash，分流更加合理；
- **动静分离**：Nginx 自己处理静态，后端专心处理动态；
- **健康检查与容错**：自动隔离故障服务器，保证服务高可用；
- **nginx.conf 配置**：大括号大统领下，使用 `nginx -s reload` 瞬间生效。

### 结合本项目的实际落地与高频考点：

1. **反向代理在本项目中如何落地？**
   - 在 [tools/deploy/nginx.py](file:///Users/hhhj/Desktop/co-agent/tools/deploy/nginx.py) 中封装 `NginxManager`。统一以域名路径方式暴露服务：`http://loveboos-ai.com/{service_name}`；
   - **保护后端与隐藏细节**：外部客户端仅感知单一主域名入口，Nginx 挡在前面隐藏了后端 Docker 容器的宿主机物理 IP、容器内部网络及动态分配端口（8001-9000），避免暴露内部端口风险。

2. **动静分离在工程中的实践：**
   - 在 [agents/devops/agent.py](file:///Users/hhhj/Desktop/co-agent/agents/devops/agent.py#L210-L232) 中实现了“快速静态部署路径”：
     - 若生成的项目仅包含纯前端静态资源（如仅检测到 `index.html`），直接配置 Nginx 静态托管（利用 OS `sendfile` 零拷贝高性能响应），跳过 Docker 镜像编译与容器生命周期开销；
     - 包含后端 API 或运行时依赖（Python/Node.js 等）的应用，才走 Docker 容器化并通过反向代理转发动态请求，实现动静分离。

3. **nginx.conf 配置热重载机制（`nginx -s reload`）：**
   - **零宕机生效（Zero-Downtime）**：DevOps Agent 渲染生成新的 location 配置块后，先执行 `nginx -t` 进行语法校验，校验通过后再调用 `nginx -s reload`；
   - **底层机制**：向 Master 进程发送 `SIGHUP` 信号。Master 重新加载新配置，启动新的 Worker 进程处理后续新连接，旧 Worker 进程处理完当前存量连接后平滑退出，确保客户端访问无感知、不断流。

4. **Upstream 与负载均衡机制：**
   - 在 upstream 块中定义后端服务集群池，支持服务轻松横向伸缩；
   - **分流策略**：支持轮询（Round Robin 默认均分）、权重（Weight 按实例硬件性能分配配额）、IP Hash（按客户端 IP 做一致性哈希，解决特定场景下的会话粘滞问题）。

5. **健康检查与容错：**
   - 配置 upstream 的 `max_fails` 与 `fail_timeout` 机制。当某后端容器异常或超时，Nginx 自动标记并隔离故障节点，将流量转给健康节点；结合项目的 [tools/deploy/monitor.py](file:///Users/hhhj/Desktop/co-agent/tools/deploy/monitor.py) 监控告警，实现自动化故障自愈。

---

## 通信与异步流控：飞书与企业微信海量请求处理（高并发与削峰）

### 核心痛点与背景：
- **协议时延错配**：外部 IM 平台（企微 Webhook、飞书 WebSocket）是**毫秒级交互**（企微要求 5 秒内必应答，超时会触发腾讯官方 3 次重发重试机制）；而底层大模型（LLM）推理是**长耗时计算（5~30 秒）**。
- **裸调雪崩风险**：如果来一个请求就直调一次 LLM，瞬间会引发：企微超时重发风暴 $\to$ 大模型供应商 RPM/TPM Rate Limit（429 报错） $\to$ 连接池耗尽 $\to$ 重复请求导致 Token 账单翻倍。

### 五层防洪调峰体系（按序防御）：

1. **第一道：入口频控（Rate Limiting）**
   - 在 [main.py](file:///Users/hhhj/Desktop/co-agent/main.py#L390) 基于 SlowAPI 设置接口限流中间件（`@limiter.limit("120/minute")`）；
   - 在网关入口直接拦截恶意脚本与高频 DoS 刷量，超出阈值立即返回 HTTP 429，不穿透到底层业务。

2. **第二道：Redis 原子去重与幂等（P1 #15 & P2 #25）**
   - 在 [storage/state_manager.py](file:///Users/hhhj/Desktop/co-agent/storage/state_manager.py#L74) 中封装 `dedup_check`；
   - 底层采用 Redis 原语 `SET dedup:{key} "1" NX EX 86400`（原子加锁 + 24h 自动淘汰）；
   - 在 [main.py](file:///Users/hhhj/Desktop/co-agent/main.py#L1053) 的企微回调入口处检查 `wecom_cs:{msg_id}`，重复到达的消息直接秒回 `{"code": 0}` 并丢弃，彻底消除因网络抖动或超时重发引发的重复入队和 Token 浪费。

3. **第三道：异步解耦与削峰填谷（核心解耦）**
   - 在 [communication/queue.py](file:///Users/hhhj/Desktop/co-agent/communication/queue.py) 封装 `CSMessageQueue`（基于 Redis List）；
   - **立即应答**：Webhook 收到消息完成验签与去重后，只执行 `await cs_queue.enqueue(queued_msg)`（`LPUSH`），耗时 < 5ms 即刻向企微返回 200，彻底消除企微 5 秒超时重发风险；
   - **智能排队提示**：若检测到 `queue_len > 0`，主动下发企微消息告知客户：*“您前面有 X 位客户在等待，预计 X×30 秒内回复”*，并保存提示消息 ID；LLM 处理完成后**自动撤回该提示消息**并推送解答结果。

4. **第四道：受控并发消费（QueueWorker 保护大模型）**
   - 在 [communication/queue.py](file:///Users/hhhj/Desktop/co-agent/communication/queue.py#L113-L184) 部署常驻协程 `QueueWorker`；
   - **批次与并发受控**：设置 `batch_size=5, interval=1.0s`，通过 `asyncio.gather` 每次最多同时处理 5 条客服消息，将大模型 API 调用压力严格封顶在供应商配额安全线内；
   - **单任务异常隔离**：通过 `_safe_handle` 独立捕获单条消息的超时或异常，杜绝单点故障中断整批消息消费。

5. **第五道：LLM 响应缓存（ResponseCache 算力削减）**
   - 在 [acceleration/cache.py](file:///Users/hhhj/Desktop/co-agent/acceleration/cache.py) 中接入 Redis 响应缓存；
   - 常见 FAQ 或重复提问在 `temperature <= 0.5` 确定性规则下，直接从缓存读取历史答案返回，0 成本且 10ms 内极速响应。

### 飞书专有机制：独立线程隔离与跨事件循环投递
- 在 [channels/feishu.py](file:///Users/hhhj/Desktop/co-agent/channels/feishu.py#L73-L121) 中，飞书 WebSocket 运行在独立的后台 Daemon 线程；
- 收到消息后压入线程安全的 `queue.Queue()`，由轮询线程通过 `asyncio.run_coroutine_threadsafe(handler, main_loop)` 派发到主事件循环；
- 避免了网络长连接的心跳检测受主事件循环 CPU 负载干扰。

### 核心考点深度精讲（面试连环问）：

#### 1. 深入考点：SETNX 是什么？Redis 原子去重与接口幂等性到底是什么？
- **SETNX 原语**：全称 `SET if Not eXists`（不存在才设置）。
  - `SET key val NX`：键不存在才写入返回 `True`，键已存在则放弃写入返回 `False`；
  - 配合 `EX 86400`（24小时 TTL），实现自动过期的分布式防重锁。
- **为什么必须是“原子操作”（Atomicity）？**
  - 如果拆成“先查 `exists` 再执行 `set`”，在高并发或超时重发瞬间，多个协程会同时查到“不存在”，进而同时写入并重复调用大模型，造成**去重失效（竞态条件 Race Condition）**；
  - Redis 执行命令是**单线程事件循环模型**，`SET ... NX` 将检查与写入合并为单条机器指令，天然具备原子性，确保极端并发下绝对只有 1 个请求能返回 `True`。
- **什么是幂等性（Idempotence）？**
  - **定义**：同一操作发起 1 次或多次重试，系统的业务结果与外部影响完全一致（$f(f(x)) = f(x)$）；
  - **项目体现**：企微 5 秒超时自动重试 3 次，首笔请求通过 `SETNX` 标记并处理，后续重试请求返回 `False` 直接秒回 `{"code": 0}`，避免重复入队和重复扣减大模型 Token 费用。
- **P1 #15 安全修复背景（为什么淘汰内存 `set()`）**：
  - 早期代码使用 Python 内存 `_processed_messages = set()`；
  - **严重缺陷**：无法跨进程/实例共享、服务发版重启即清空、内存无 TTL 自动淘汰易导致 OOM 爆内存、且多协程非原子。改用 Redis SETNX 彻底根治。

#### 2. 深入考点：企微流量削峰与异步排队通知的完整时序闭环
- **全流程四阶段时序闭环**：
  1. **生产者（毫秒级入队）**：企微 Webhook 收到消息，验签与 `SETNX` 幂等校验后，调用 [CSMessageQueue.enqueue](file:///Users/hhhj/Desktop/co-agent/communication/queue.py#L38)（底层 `LPUSH cs:message_queue`），并**在 5 毫秒内秒回企微 200/code:0**，彻底规避 5 秒超时重试风暴；
  2. **智能排队安抚**：入队前用 `LLEN` 检测当前排队长度，若 `queue_len > 0`，立刻调用企微 API 主动向客户推送一条安抚通知：*“您前面有 X 位客户在等待，预计 X×30 秒内回复您”*，并将通知的 `msgid` 保存到 Redis（`cs:notify_msg:{customer_id}`）；
  3. **受控匀速消费**：后台常驻协程 [QueueWorker](file:///Users/hhhj/Desktop/co-agent/communication/queue.py#L113) 按照 `batch_size=5, interval=1.0s` 恒定节奏通过 `RPOP` 批量取出消息，并发交给客服 Agent，把大模型压强限制在安全红线内；
  4. **结果推送与通知自动撤回**：客服 Agent 调用 LLM 推理生成最终答案并推送给客户；随后从 Redis 取出刚才暂存的排队通知 ID，**调用企微 `recall` 接口将排队提示无痕撤回**，实现整洁优雅的会话体验。

---

## 记忆架构：三层记忆、智能强化与跨会话召回体系（Memory Architecture）

### 核心痛点与背景：
- **Context Window 有限**：多 Agent 长周期协同中，无法将所有历史对话直接塞给大模型，否则很快触发 Token 溢出与注意力分散；
- **跨节点/跨项目遗忘**：上游 CEO、架构师的决策，下游 Developer 和 DevOps 无法高效继承；上一项目的踩坑经验无法在下一个新项目中复用；
- **历史信息噪音泛滥**：无筛选的上下文会导致“你好”、“收到”等无意义闲聊稀释关键技术决策。

### 全景架构设计（三层物理存储 + 三项智能强化 + 统一门面）：

```
┌─────────────────────────────────────────────────────────────┐
│                 MemoryManager (统一入口门面)                   │
├─────────────────────────────────────────────────────────────┤
│  Layer 1: WorkingMemory  (短期工作记忆 · 纯内存 Deque)         │
│  Layer 2: SessionMemory  (中期会话记忆 · Redis热缓存 + PG持久化)│
│  Layer 3: KnowledgeMemory(长期知识记忆 · PostgreSQL + pgvector) │
├─────────────────────────────────────────────────────────────┤
│  智能强化层：                                                │
│  1. ImportanceScorer   (五维记忆重要性评分算法 0.0~1.0)       │
│  2. MemoryConsolidator (LLM 驱动的后台异步记忆提炼器)          │
│  3. MemoryGraph        (基于 Redis Sets + PG 的轻量关联图谱)  │
└─────────────────────────────────────────────────────────────┘
```

### 1. 三层记忆的物理划分与生命周期

| 记忆分层 | 对应代码 | 物理存储介质 | 生命周期与容量 | 核心职责与存储内容 |
| :--- | :--- | :--- | :--- | :--- |
| **Layer 1: 短期工作记忆 (Working)** | [memory/working.py](file:///Users/hhhj/Desktop/co-agent/memory/working.py) | **Python 原生内存** (`collections.deque`) | 单节点任务内，容量上限 **20 条** (FIFO淘汰) | 充当 Agent 单次思考运行的 **Scratchpad（临时白板）**。记录当前轮次的消息流，任务开始 `clear()`，任务结束归档到 L2。**零 I/O 开销，毫秒级读写**。 |
| **Layer 2: 中期会话记忆 (Session)** | [memory/session.py](file:///Users/hhhj/Desktop/co-agent/memory/session.py) | **写时双写 + 读时回填**：Redis (7天TTL) + PostgreSQL (永久) | 跨任务/跨轮次，Redis 存近期 **50 个任务** | 维护项目上下文、最近的多 Agent 交付流水、客户多轮聊天记录。PG 作为 Truth of Source（冷备），Redis 承载高频热查。 |
| **Layer 3: 长期知识记忆 (Knowledge)** | [memory/knowledge.py](file:///Users/hhhj/Desktop/co-agent/memory/knowledge.py) | **PostgreSQL + pgvector** + 百炼 text-embedding-v4 | **跨项目、永久保存** | 存储被高度提炼过的经验教训、技术踩坑笔记、标准开发规范（SOP）、企业 FAQ。支持**自然语言语义相似度匹配（Cosine Sim）**。 |

### 2. 三项关键智能强化设计

#### (1) 五维重要性评分算法（Importance Scoring）
- **代码实现**：[memory/importance.py](file:///Users/hhhj/Desktop/co-agent/memory/importance.py)
- **算法公式**：计算输出 **0.0 ~ 1.0** 的重要性综合得分：
  1. **来源角色 (30%)**：CEO 决策 (1.0) > Developer 经验 (0.8) > Frontend (0.75) > DevOps (0.7) > 客服 (0.5)；
  2. **记忆类型 (25%)**：经验教训 `experience` (1.0) > 最佳实践 `best_practice` (0.9) > 架构决策 `decision` (0.85) > 任务流水 `task` (0.6) > 闲聊 `chat` (0.3)；
  3. **内容质量 (20%)**：梯形长度评分，50~500 字黄金区间得满分，过短（无意义）或过长（杂乱）扣分；
  4. **时间衰减 (15%)**：模拟**艾宾浩斯遗忘曲线**（$e^{-\lambda \cdot \Delta t}$），历史久远的记忆逐步降权；
  5. **访问热度 (10%)**：被命中召回频次越高，得分累加。

#### (2) LLM 驱动的后台异步记忆提炼器（Memory Consolidation）
- **代码实现**：[memory/consolidator.py](file:///Users/hhhj/Desktop/co-agent/memory/consolidator.py)
- **机制**：采用**“快速归档 + 异步提炼（Fire-and-Forget）”**机制，任务结束时绝不阻塞业务主工作流：
  1. **快归档**：任务执行完，先把 L1 Working Memory 的原始多轮日志快速塞入 L2 Session Memory（耗时 < 5ms）；
  2. **慢提炼**：后台启动异步协程，调用大模型（DeepSeek）将几千字的流水日志提炼为结构化 JSON：
     - `summary`（2~3 句话摘要）：替换掉 L2 中的冗长流水；
     - `key_decisions`（关键决策）与 `technical_notes`（技术要点）：**提纯后持久化写入 L3 KnowledgeMemory 向量库**；
     - `tags`（标签）：送入记忆图谱建立索引。

#### (3) 轻量级记忆图谱（Memory Graph）
- **代码实现**：[memory/graph.py](file:///Users/hhhj/Desktop/co-agent/memory/graph.py)
- **选型考量**：放弃笨重的 Neo4j（单机高开销、部署复杂），基于 **Redis Sets 内存集合 + PostgreSQL 关系表** 实现轻量图谱：
  - **边结构**：`same_project`（同项目关联，权重 0.9）、`same_tags`（同技术标签关联，权重 0.7）；
  - **存储实现**：Redis 存储 `memory:edges:{id}`（出边集合）与 `memory:tag:{tag}`（标签倒排索引），支持纳秒级 `SMEMBERS` 查询，PG 持久化 `memory_edges` 表；
  - **图扩跳（Expand）**：当召回一条核心记忆时，自动沿着图谱的边扩展 1 跳，把同项目的其他依赖节点或踩坑记忆一并带出。

### 3. 跨会话召回流程（Recall Pipeline）
- **代码实现**：[memory/manager.py:L350-L488](file:///Users/hhhj/Desktop/co-agent/memory/manager.py#L350-L488)
- **流转步骤**：
  1. **全层收集**：L1 工作记忆全量并入 + L2 关键词匹配 + L3 pgvector 语义向量匹配；
  2. **内容去重**：基于内容前 100 字符的 Hash 物理去重；
  3. **图谱扩跳**：若启用 `graph` 策略，以种子记忆为节点在 `MemoryGraph` 扩跳关联记忆；
  4. **多策略混合排序**：
     - `semantic`: $0.3 \times \text{重要度} + 0.7 \times \text{语义相似度}$；
     - `graph`: $0.4 \times \text{重要度} + 0.6 \times \text{图关联权重}$；
     - `comprehensive`: $0.6 \times \text{重要度} + 0.4 \times \text{时间新鲜度}$。
  5. **指标埋点**：上报 `memory.recall` 埋点事件（记录策略、命中数与耗时）。

### 💡 面试标准回答模板（直接背诵）：

> “为了解决 Agent 容易丢失历史上下文以及 Token 窗口爆炸的矛盾，我设计了一套**‘分层存储 + 异步提炼 + 图谱增强’**的记忆体系：  
> 
> 1. **在分层设计上**：  
>    * **短期（Working）**：纯内存 Deque（上限20条），专供单任务节点内快速 Scratchpad 记录；  
>    * **中期（Session）**：Redis（7天热缓存）+ PG（冷持久化）双写，维护多轮会话与近期任务上下文；  
>    * **长期（Knowledge）**：基于 PostgreSQL + pgvector，存储跨项目沉淀的经验与 FAQ。  
> 
> 2. **在流转与降噪上**：  
>    * 引入了**五维重要性评分算法**（角色、类型、质量、遗忘曲线衰减、访问热度），自动对记忆打 0~1 的权重分；  
>    * 节点结束后通过后台非阻塞协程由 **LLM 自动提炼**出决策摘要和技术要点，反哺到长期知识库；  
> 
> 3. **在召回上**：  
>    * 放弃了沉重的 Neo4j，利用 **Redis Sets 构建了轻量级 MemoryGraph**。召回时结合‘向量语义检索 + 图谱 1 跳关系扩展 + 重要性加权排序’，既保证了语义相关，又补充了同项目或同标签的结构化上下文。”

---

### 4. 深入考点：Session Memory 在 Redis 存 7 天会不会打爆内存？（无懈可击的面试版本）

> “我们在设计 Redis 内存防护时做了**主动防御（Hard Limit）与被动防御（TTL）**的组合：
> 
> - **结构设计**：在 Redis 里面，任务被拆成两部分存储：任务索引通过 `LTRIM` 严格限制最新 50 条活跃索引；任务实体经过 LLM 压缩提炼，单条控制在 1~2KB；
> - **容量边界测算（Worst-Case）**：
>   - 即使在高频调度下，5 个 Agent 7 天内累计并发产生上万个临时详情任务，按 7 天 TTL 窗口峰值计算，总常驻数据量也在 **50MB ~ 100MB** 区间；
>   - 如果是常规交互，常驻活跃数据甚至不足 **10MB**；
> - **兜底兜牢**：Redis 配置了 `maxmemory-policy volatile-lru`，即使外部突然涌入极端流量，Redis 也会优先从带有 7 天 TTL 的过期键中按最近最少使用淘汰，保证绝对不会击穿系统物理内存引发 OOM 宕机。”

---

### 5. 深入考点：我们为什么要设计记忆图谱？（突破纯向量 RAG 的缺陷）

#### 核心痛点与动机（纯向量检索的三大死穴）：
1. **丢失因果与拓扑归属（孤岛效应）**：向量检索只能按字面或语义匹配“孤立的点”，无法表达多 Agent 工作流中任务的前后因果链（如 DevOps 遇到端口被占用，纯向量只能搜出排错 FAQ，图谱沿 `same_project` 边才能追溯到前序 Developer 占用了该端口）；
2. **跨角色的“语境壁垒”**：CEO 描述商业业务，Developer 描述代码实现，DevOps 描述底层容器报错，不同角色用词风格完全不同，向量余弦相似度极低，导致排错时无法跨语境召回上游的需求与架构决策；
3. **隐式技术经验迁移**：不同项目（如电商与外卖）业务无关，但使用相同技术栈（`[FastAPI, WebSocket, Nginx]`）。图谱通过 `same_tags` 标签倒排，自动打通跨项目的踩坑经验关联。

#### 标准答题口径（直接背诵）：
> “单纯依赖向量检索（Vector RAG）只能解决‘文本语义相似’的问题，但多 Agent 协同存在致命的**‘信息孤岛’与‘因果断层’**：  
> 
> 比如不同角色（CEO 讲业务、DevOps 讲容器报错）语言风格完全不同，向量相似度极低，导致排错时根本找不到上游的需求背景；且向量库无法表达任务之间的前后依赖因果关系。  
> 
> 为此，我们设计了轻量级 **MemoryGraph**，构建了 `same_project`（同项目因果拓扑，权重 0.9）与 `same_tags`（跨项目技术栈经验，权重 0.7）两类关联边：  
> 
> 检索时采用 **Graph RAG 模式**：先用向量检索粗筛出 1~2 条最相关的‘种子记忆’，再在图谱中沿着关系边向外辐射 **1 跳（1-Hop）扩跳**，将关联的架构决策和历史踩坑一并捞出。  
> 
> 同时，我们避免引入庞大笨重的 Neo4j，而是基于 **Redis Sets 内存集合 + PostgreSQL 关系表** 实现了这一图谱，既满足了微秒级的拓扑扩展，又保持了极轻量的运维架构。”




