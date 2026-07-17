"""
analytics_report.py — 从 analytics_events.log 产出 5 个简历指标 + 事件分布。

用法：
    python analytics_report.py                 # 默认读 ./analytics_events.log
    python analytics_report.py path/to/log     # 指定日志路径
    ANALYTICS_LOG_PATH=... python analytics_report.py

退出码：0=有数据, 1=无数据/读不到文件。
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

LOG_PATH = Path(os.environ.get("ANALYTICS_LOG_PATH", "analytics_events.log"))


def _load(path: Path):
    if not path.exists():
        print(f"[error] 日志不存在: {path}", file=sys.stderr)
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[warn] 跳过损坏行 {ln}", file=sys.stderr)
    return events


def _percentile(values: list, p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * p / 100), len(s) - 1)
    return float(s[idx])


def _parallel_speedup(events) -> str:
    """Σ(节点 duration) / max(同 workflow 同层总时长)。按 workflow 分组计算平均。"""
    from collections import defaultdict

    layers: dict = defaultdict(lambda: defaultdict(int))  # wf -> (depth, layer_total_ms)
    sums: dict = defaultdict(int)  # wf -> sum(node duration_ms)

    for e in events:
        if e["event"] == "workflow.node" and e.get("phase") == "end":
            key = (e["workflow_id"], e["depth"])
            layers[e["workflow_id"]][key] = max(
                layers[e["workflow_id"]][key], e.get("duration_ms", 0)
            )
            sums[e["workflow_id"]] += e.get("duration_ms", 0)

    if not sums:
        return "n/a (无 workflow.node end 事件)"
    ratios = []
    for wf, total in sums.items():
        wall = max(layers[wf].values()) if layers[wf] else 0
        if wall > 0:
            ratios.append(total / wall)
    return f"{sum(ratios)/len(ratios):.2f}x ({len(ratios)} 个 workflow)"


def _llm_cost_per_task(events) -> str:
    think_tokens = sum(
        e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)
        for e in events
        if e["event"] == "agent.think" and e.get("phase") == "end"
    )
    ends = [e for e in events if e["event"] == "workflow.end"]
    if not ends:
        return "n/a (无 workflow.end)"
    return f"{think_tokens / len(ends):.0f} tokens/task (样本={len(ends)})"


def main() -> int:
    # ponytail: positional arg overrides env var; env var overrides default.
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_PATH
    events = _load(path)
    if not events:
        print("[hint] 没有事件数据，请先跑业务或调 emit_event 写入。")
        return 1

    counter = Counter(e["event"] for e in events)
    print(f"日志: {path}  共 {len(events)} 条\n")

    print("=== 事件类型分布 ===")
    for name, n in counter.most_common():
        print(f"  {n:>5}  {name}")

    print("\n=== 5 个简历指标 ===")
    starts = [e for e in events if e["event"] == "workflow.start"]
    ends = [e for e in events if e["event"] == "workflow.end"]
    recv = [e for e in events if e["event"] == "feishu.message.received"]
    rej = [e for e in events if e["event"] == "input.guard.rejected"]

    if starts and ends:
        succ = sum(1 for e in ends if e["status"] == "success")
        print(f"  端到端成功率:        {succ}/{len(ends)} = {succ/len(ends):.1%}")
        p95 = _percentile([e["duration_s"] for e in ends], 95)
        print(f"  P95 端到端延迟:      {p95:.2f}s")
    else:
        print("  端到端成功率/P95:    n/a (无 workflow.start/end)")

    print(f"  多 Agent 并行加速比:  {_parallel_speedup(events)}")
    print(f"  LLM 单任务平均成本:   {_llm_cost_per_task(events)}")

    if recv:
        print(f"  Prompt 注入拦截率:    {len(rej)}/{len(recv)} = {len(rej)/len(recv):.1%}")
    else:
        print("  Prompt 注入拦截率:    n/a (无 feishu.message.received)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
