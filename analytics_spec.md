# Co-Agent 埋点规范（analytics_spec.md）

> 配套代码：`analytics.py`
> 输出文件：`./analytics_events.log`（NDJSON，可被 `ANALYTICS_LOG_PATH` 环境变量覆盖）
> 维护目的：为 AI Agent 岗简历提供可量化、可核对的运行数据。

---

## 1. 设计目标

| 简历 STAR 段 | 想说的话 | 需要的硬数据 |
|--------------|---------|---------------|
| **S** 业务背景 | 中小企业运营链路长，单 Agent 覆盖不了全流程 | 角色数 / 工作流节点数 / 任务平均步数 |
| **A1** 框架设计 | 自研 BaseAgent + ReAct 循环 + 三种反应模式 | agent.think / agent.act 触发次数、平均时长 |
| **A2** 编排能力 | CEO 动态拆解为 DAG，分层并行执行 | workflow.start/node/end 事件、并行节点数 |
| **A3** 记忆与上下文 | 三层记忆 + LLM 自动提炼 | memory.recall / memory.distill 触发、命中率 |
| **R** 产出 | 上线运营 + 端到端可控 | 端到端成功率 / P95 延迟 / LLM 成本 / 注入拦截率 |

**原则：只采"能直接写进简历的数字"，不采细节调试数据。**

---

## 2. 事件清单（11 个）

每行 NDJSON：
```json
{"ts":"2026-06-27T10:00:00.123+00:00","event":"<name>","trace_id":"tr-abc123","<属性>":...}
```

字段说明中：
- `[必]` 必填
- `[选]` 选填
- `trace_id` 由 `analytics.py` 自动注入，无需调用方传

### 2.1 `feishu.message.received` — 飞书消息入口
- **目标**：计算"总接收数 / 端到端成功率分母 / 注入拦截率分母"
- **触发时机**：`agents/ceo/agent.py::handle_feishu_message` 通过幂等去重之后
- **字段**：
  - `user_id_hash` [必] SHA256(open_id)[:16]
  - `msg_len` [必] int
  - `intent` [选] 意图分类结果字符串
  - `dedup` [选] bool，是否被去重跳过（默认 false）

### 2.2 `input.guard.rejected` — 输入被拒
- **目标**：计算 Prompt 注入拦截率
- **触发时机**：`security/input_guard.py::validate` 返回 `allowed=False` 时
- **字段**：
  - `reason_code` [必] 字符串（如 `prompt_injection_zh` / `zero_width` / `too_long`）
  - `pattern_id` [选] 命中的 pattern 下标（便于回溯）
  - `source` [必] 输入来源（如 `feishu`）

### 2.3 `agent.think` — 思考阶段
- **目标**：度量 LLM 调用频次、token 消耗、模型分布
- **触发时机**：`agents/base.py::_react` 中 `_think` 调用的入口与出口
- **字段**：
  - `agent_role` [必] 枚举：ceo/developer/devops/frontend_expert/finance/customer_service
  - `phase` [必] 枚举：`start` / `end`
  - `llm_model` [选] 模型名
  - `prompt_tokens` [选] int
  - `completion_tokens` [选] int
  - `duration_ms` [选] int（仅 end）
  - `success` [选] bool（仅 end）

### 2.4 `agent.act` — 行动阶段
- **目标**：度量动作选择分布、动作执行成功率
- **触发时机**：`agents/base.py::_react` 中 `_act` 调用的入口与出口
- **字段**：
  - `agent_role` [必]
  - `phase` [必] `start` / `end`
  - `action_name` [必]
  - `action_args_hash` [选] SHA256(JSON)[:16]
  - `duration_ms` [选] int（仅 end）
  - `success` [选] bool（仅 end）

### 2.5 `tool.call` — 工具调用
- **目标**：度量工具层稳定性（与 agent.act 区分：tool.call 是工具层视角）
- **触发时机**：代表性工具的入口/出口（详见 §5.3）
- **字段**：
  - `tool_name` [必]
  - `phase` [必] `start` / `end`
  - `tool_args_hash` [选]
  - `exit_code` [选] int（仅 end）
  - `stdout_len` [选] int（仅 end）
  - `duration_ms` [选] int（仅 end）

### 2.6 `memory.recall` — 记忆召回
- **目标**：度量记忆命中率、跨会话上下文
- **触发时机**：`memory/manager.py::recall` 出口
- **字段**：
  - `memory_type` [必] 枚举：`working` / `session` / `knowledge`
  - `items_count` [必] int（返回条数）
  - `hit` [必] bool（items_count > 0）
  - `duration_ms` [选]

### 2.7 `memory.distill` — 记忆提炼
- **目标**：度量 LLM 自动提炼频次与压缩率
- **触发时机**：`memory/consolidator.py::consolidate` 完成时
- **字段**：
  - `agent_role` [选]
  - `items_in` [必] int
  - `items_out` [必] int
  - `duration_ms` [选]

### 2.8 `workflow.start` — 工作流开始
- **目标**：度量工作流规模（节点数、深度）
- **触发时机**：`workflows/executor.py::execute` 入口，拓扑层确定后
- **字段**：
  - `workflow_id` [必]
  - `node_count` [必] int
  - `topo_depth` [必] int

### 2.9 `workflow.node` — 节点执行
- **目标**：度量并行度与单节点时长（核心 Agent 特色指标）
- **触发时机**：节点入口与出口
- **字段**：
  - `workflow_id` [必]
  - `node_id` [必]
  - `node_type` [必]
  - `depth` [必] int（拓扑层，从 0 开始）
  - `parallel_count` [必] int（同层节点数）
  - `phase` [必] `start` / `end`
  - `duration_ms` [选] int（仅 end）
  - `success` [选] bool（仅 end）

### 2.10 `workflow.end` — 工作流结束
- **目标**：端到端成功率、P95 延迟、token 成本
- **触发时机**：`workflows/executor.py::execute` 出口
- **字段**：
  - `workflow_id` [必]
  - `status` [必] 枚举：`success` / `fail` / `partial`
  - `total_nodes` [必] int
  - `success_nodes` [必] int
  - `duration_s` [必] float
  - `total_prompt_tokens` [选] int
  - `total_completion_tokens` [选] int

### 2.11 `agent.error` — Agent 异常
- **目标**：度量错误率
- **触发时机**：6 个 Agent 中任意 catch 块
- **字段**：
  - `agent_role` [必]
  - `error_code` [必] 枚举（见 §4）
  - `error_msg_hash` [必] SHA256(str(exception))[:16]

---

## 3. 简历可写指标（5 个，公式 + 数据源）

| 指标 | 公式 | 数据源 |
|------|------|--------|
| **端到端成功率** | `count(workflow.end status=success) / count(workflow.start)` | workflow.start / workflow.end |
| **P95 端到端延迟** | `percentile(workflow.end.duration_s, 95)` | workflow.end |
| **多 Agent 并行加速比** | `Σ(节点 duration) / max(同 workflow 同层总时长)` | workflow.node |
| **LLM 单任务平均成本** | `Σ(agent.think.prompt_tokens + completion_tokens) / count(workflow.end)` | agent.think / workflow.end |
| **Prompt 注入拦截率** | `count(input.guard.rejected) / count(feishu.message.received)` | input.guard.rejected / feishu.message.received |

---

## 4. 错误码枚举（error_code）

避免堆栈里出现敏感信息，统一为枚举：
- `llm_timeout`
- `llm_json_parse_fail`
- `tool_exit_nonzero`
- `sandbox_degraded`
- `input_too_long`
- `workflow_cycle_detected`
- `agent_max_steps_exceeded`
- `unknown`

---

## 5. 实现说明

### 5.1 命名与位置
- 助手模块：`analytics.py`（项目根目录）
- 写入路径：`./analytics_events.log`（NDJSON）
- 跨进程或多实例：用环境变量 `ANALYTICS_LOG_PATH=/path/to/log` 指定

### 5.2 调用方约定
- 每个埋点调用都要包在 `try/except` 外（`emit_event` 内部已 try/except，这里只是保险）
- 严禁把用户原文/工具原始输出写入 `fields`，必须哈希或仅写长度
- `trace_id` 优先复用 `observability.logging.get_trace_id()`，无需手动传

### 5.3 tool.call 埋点策略
中心化 wrapper 不存在（各工具分散实现），采用**代表性子模块埋点**：
- `tools/deploy/docker.py` — DockerManager 的 build / run / stop
- `tools/deploy/analyzer.py` — ProjectAnalyzer 的 analyze
- 覆盖最常见的"启动 / 停止 / 分析"三类动作

---

## 6. 已知限制

1. **多进程不安全**：当前用 `threading.Lock`，多进程（如 gunicorn 多 worker）会丢行。生产环境若多 worker，请改为外部队列（Redis Stream / Kafka）。
2. **无日志轮转**：文件会无界增长。建议挂 cron 每周归档，或外接 logrotate。
3. **同步写入有阻塞风险**：单事件写盘约 0.5–2 ms，正常流量下可忽略；突发高并发时应改为 buffer + 异步刷盘。

---

## 7. 假设与拍板（计划不清晰处）

| 假设 | 决策 | 理由 |
|------|------|------|
| 日志文件路径 | 项目根 `analytics_events.log` | 便于本地查看，符合"项目根目录维护"要求 |
| 跨进程并发 | 单 Lock 接受丢行 | 当前部署为单进程，简化优先 |
| trace_id 来源 | 优先复用 observability 的 ContextVar | 已有基础设施，避免重复 |
| 失败隔离 | emit_event 内 try/except | 永不阻塞主流程 |
| tool.call 范围 | 选代表性子模块 3 个 | 中心化 wrapper 不存在，全埋工作量大收益低 |
| 不新增 DB 表 | 纯文件追加 | 符合 prompt "运行时埋点日志"要求 |
| 错误码枚举 | 8 个固定值 | 简历数字可解释、可比 |

---

## 8. 如何本地验证

### 步骤 1：跑测试触发埋点

```bash
cd /Users/hhhj/Desktop/co-agent
source venv/bin/activate

# 跑端到端测试，会触发 workflow.start / node / end
pytest tests/ -x -q
```

### 步骤 2：直接调用 emit_event 验证单点

```python
python -c "
from analytics import emit_event, hash_id
emit_event('feishu.message.received', user_id_hash=hash_id('ou_test'), msg_len=42, intent='requirement')
emit_event('input.guard.rejected', reason_code='zero_width', source='feishu')
emit_event('workflow.start', workflow_id='wf-1', node_count=3, topo_depth=2)
emit_event('workflow.node', workflow_id='wf-1', node_id='n1', node_type='agent', depth=0, parallel_count=2, phase='start')
emit_event('workflow.node', workflow_id='wf-1', node_id='n1', node_type='agent', depth=0, parallel_count=2, phase='end', duration_ms=1500, success=True)
emit_event('workflow.end', workflow_id='wf-1', status='success', total_nodes=3, success_nodes=3, duration_s=4.2)
print('done')
"
```

### 步骤 3：检查日志输出

```bash
# 实时查看
tail -f analytics_events.log

# 按事件名过滤
grep '"event":"workflow.end"' analytics_events.log

# 统计事件数
python -c "
import json
from collections import Counter
c = Counter()
with open('analytics_events.log') as f:
    for line in f:
        try:
            c[json.loads(line)['event']] += 1
        except Exception:
            pass
for name, n in sorted(c.items(), key=lambda x: -x[1]):
    print(f'{n:>5}  {name}')
"

# 验证 5 个简历指标
python -c "
import json
from collections import defaultdict

events = [json.loads(l) for l in open('analytics_events.log')]
starts = [e for e in events if e['event']=='workflow.start']
ends = [e for e in events if e['event']=='workflow.end']
received = [e for e in events if e['event']=='feishu.message.received']
rejected = [e for e in events if e['event']=='input.guard.rejected']

if starts and ends:
    succ = sum(1 for e in ends if e['status']=='success')
    print(f'端到端成功率: {succ}/{len(ends)} = {succ/len(ends):.1%}')
    durs = sorted([e['duration_s'] for e in ends])
    p95 = durs[int(len(durs)*0.95)] if durs else 0
    print(f'P95 端到端延迟: {p95:.2f}s')

if received:
    print(f'Prompt 注入拦截率: {len(rejected)}/{len(received)} = {len(rejected)/len(received):.1%}')
"
```

### 步骤 4：期望看到的事件

- 跑测试后，至少能看到：`agent.think` / `agent.act` / `workflow.start` / `workflow.node` / `workflow.end` / `memory.recall` 几类
- 直接调 `emit_event` 后，立即在 `analytics_events.log` 看到 6 行 NDJSON
- 故意发送带零宽字符的飞书消息，能看到 `input.guard.rejected`

### 步骤 5：清理

```bash
rm -f analytics_events.log   # 删掉测试数据，让真实流量重新开始累积
```
