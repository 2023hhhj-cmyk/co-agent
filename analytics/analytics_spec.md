# Co-Agent 埋点规范

> 更新：2026-10-06，按 `analytics/__init__.py`、`analytics/report.py` 和调用点核对。

## 写入与字段

`from analytics import emit_event, hash_id, hash_args`。日志为 NDJSON，一行一个事件，公共字段为 ts、event、trace_id。默认路径基于模块位置，为项目内 `analytics/logs/analytics_events.log`；ANALYTICS_LOG_PATH 可覆盖。

trace_id 优先使用 observability 上下文，其次使用显式埋点上下文；没有上下文时临时生成，不能保证自动关联整条业务链。写入器不校验事件 schema；调用方负责字段与脱敏。

## 事件（11 类）

| 事件 | 主要调用位置 / 含义 | 主要字段 |
|------|--------------------|----------|
| feishu.message.received | CEO 飞书入口去重后 | user_id_hash、msg_len、intent（可选） |
| input.guard.rejected | InputGuard 拒绝输入 | reason_code、source |
| agent.think | BaseAgent REACT 思考阶段 | agent_role、phase、duration_ms、prompt_tokens、completion_tokens |
| agent.act | BaseAgent REACT 行动阶段 | agent_role、phase、action_name、duration_ms、success |
| agent.error | Agent 异常分类 | agent_role、error_code、error_msg_hash |
| tool.call | 代表性工具调用 | tool_name、phase、duration_ms 及工具特有字段 |
| memory.recall | 记忆召回 | memory_type、items_count、hit、duration_ms |
| memory.distill | 记忆提炼 | items_in、items_out、duration_ms |
| workflow.start | 执行器确定拓扑层后 | workflow_id、node_count、topo_depth |
| workflow.node | 节点执行 | workflow_id、node_id、node_type、depth、parallel_count、phase、duration_ms、success |
| workflow.end | 工作流结束或异常 | workflow_id、status、total_nodes、success_nodes、duration_s |

字段按调用点提供，并非每条事件全部包含。workflow.end 的 status 为 success / partial / fail；跳过节点通过 context 中的 skipped 与 blocked_by 识别，不另设一种事件类型。

工具埋点覆盖 Claude Code、Docker build/run/stop 和项目分析等代表性调用，尚非全工具覆盖。Claude Code 当前发出 end 事件，不应要求每个工具都有 start/end 配对。REACT 的 think/act 统计也不能代表所有反应模式及所有 LLM 调用。

## 报告的实际统计口径

在项目根目录运行：

```bash
python analytics/report.py
python analytics/report.py tests/fixtures/analytics_events.test.log
```

路径优先级：命令行参数 > ANALYTICS_LOG_PATH > 默认路径。无数据或文件不存在返回 1；有效数据返回 0，损坏 JSON 行被跳过。

| 报告标签 | 当前计算方式 | 使用限制 |
|----------|--------------|----------|
| 端到端成功率 | success 结束事件 / 全部结束事件（需要存在 start 和 end） | 是已结束样本成功率；不包括未结束任务，也未按 ID 去重配对 |
| P95 端到端延迟 | 排序 duration_s，取 min(floor(n×0.95), n−1) 下标 | 小样本分位估计，按结束事件统计 |
| 多 Agent 并行加速比 | 每个工作流的节点时长之和 / 所有层中最大的节点时长，再对有效工作流取均值 | 是代理比值；分母不是各层耗时之和或实测墙钟，不能作为真实加速比 |
| LLM 单任务平均成本 | think end 的输入/输出 tokens 之和 / 结束事件数 | 单位 tokens/task，不是金额；遗漏未记录的行动、重试、规划及 CLI 消耗 |
| Prompt 注入拦截率 | 全部 input.guard.rejected / 飞书 received | 可能混合来源及非注入拒绝，甚至超过 100%；不是攻击检测准确率或召回率 |

使用指标前需保留样本、版本、统计窗口与口径。实际加速比需要匹配条件的串行/并行对照；金额成本需要完整用量和对应价格；攻击检测效果需要带标签的评测集。本次只校正文档，未修改报告计算代码。

## 错误与隐私

BaseAgent 错误分类包含 llm_timeout、llm_json_parse_fail、tool_exit_nonzero、sandbox_degraded、input_too_long、workflow_cycle_detected、agent_max_steps_exceeded、unknown。

不得传入原始用户消息、个人信息、API Key、密码、完整工具输出或敏感错误堆栈。hash_id/hash_args 仅辅助调用方脱敏；写入器不会自动清除字段中的秘密。

## 运行限制与验证

threading.Lock 只协调进程内写入；多进程共享文件缺乏跨进程一致性保障，应使用单写者或外部队列。同步写盘可能阻塞事件循环，当前没有可核实的统一耗时指标。写入异常被吞掉，避免向业务抛错，但不代表写入绝不阻塞。

没有内置日志轮转。轮转和排查见 [运维说明](analytics_ops_notes.md)。测试应使用单独日志路径，避免污染或删除真实运行数据：

```bash
ANALYTICS_LOG_PATH=/tmp/co-agent-analytics-test.log venv/bin/python -m pytest tests/test_analytics.py --no-cov -q
```

合成事件或测试 fixture 只能验证写入与统计流程，不能当作生产性能证据。
