# Co-Agent 埋点运维注意事项（analytics_ops_notes.md）

> 配套：`analytics.py` / `analytics_spec.md` / `analytics_events.log`
> 维护人：项目 owner
> 目的：列出上线后必须知道的事，避免踩坑

---

## 1. 多进程部署会丢行 ⚠️ 重要

**问题**：`analytics.py` 用 `threading.Lock` 保护文件写入，仅在**单进程**下原子。

**触发场景**：
- gunicorn `-w 4` 启动 4 个 worker
- uvicorn `--workers 4`
- 任何 fork-based 多进程模型

**结果**：多进程同时写 `analytics_events.log`，行内容会交叉错乱，事件可能丢失或损坏。

**修复方案**（按推荐度）：

| 方案 | 工作量 | 适用场景 |
|------|--------|----------|
| **A. 单写者 + Redis Stream** | 1 天 | 已有 Redis，最快落地 |
| **B. 改写 PostgreSQL `agent_events` 表** | 0.5 天 | 已有 PG 且对延迟不敏感 |
| **C. syslog / journald（系统日志服务）** | 0.5 天 | 服务器本地有 syslog |
| **D. 接受丢行（仅用于演示）** | 0 | 不上生产 |

**A 方案示意**（替换 `analytics.py::_write_line`）：
```python
def _write_line(line: str) -> None:
    try:
        from storage.state_manager import state_manager
        state_manager.redis.xadd("analytics_events", {"data": line}, maxlen=100000)
    except Exception:
        pass  # 失败仍吞掉
```

**何时必须做**：在把服务部署到生产环境**之前**。本地开发可忽略。

---

## 2. 不做日志轮转（文件会无界增长）

**问题**：`analytics_events.log` 会持续追加，几个月后会占满磁盘。

**症状**：服务某天突然报"磁盘空间不足"，可能就是这个文件。

**临时方案**（生产前必做其一）：

### A. logrotate（推荐）
创建 `/etc/logrotate.d/co-agent-analytics`：
```
/Users/hhhj/Desktop/co-agent/analytics_events.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
```
`copytruncate` 关键：避免动到正在打开的文件句柄。

### B. 手动 cron
```bash
# 每周日凌晨 3 点归档
0 3 * * 0 cd /Users/hhhj/Desktop/co-agent && \
  mv analytics_events.log analytics_events.$(date +\%Y\%m\%d).log && \
  gzip analytics_events.$(date +\%Y\%m\%d).log
```

### C. 改用有界的写入管道
把 `_write_line` 改成"每 1000 行或 60 秒刷一次到 S3 / OSS / PG"，最干净但要重写。

---

## 3. 数据采集周期建议

| 时间 | 数据是否足以参考 |
|------|------------------|
| < 3 天 | ❌ 数字波动大，不可用 |
| 3-7 天 | ⚠️ 可参考，但样本量小 |
| **7-30 天** | ✅ 推荐区间 |
| > 30 天 | ✅ 数字稳定，可长期使用 |

**建议**：
1. 部署后**至少跑 7 天**再采集 5 个指标
2. 周末与工作日流量不同，理想是跑 14 天（含两个周末）
3. 重大发版后建议**重置**（`rm analytics_events.log`），从 0 开始统计

---

## 4. 已知假设与拍板（不可改的决策）

> 完整版在 `analytics_spec.md` §7，这里只列**会影响生产行为的**几条。

| 假设 | 决策 | 影响 |
|------|------|------|
| 日志文件路径 | 项目根 `analytics_events.log` | 可用 `ANALYTICS_LOG_PATH` 环境变量覆盖 |
| 跨进程并发 | 单 Lock 接受丢行 | 见 §1 |
| trace_id 来源 | 优先 `observability.logging` ContextVar | 已有 trace_id 会被自动复用 |
| 失败隔离 | `emit_event` 内 try/except | 永不抛错，**不会**阻塞主流程 |
| 错误码枚举 | 8 个固定值 | 在 `agents/base.py::_classify_error` |
| tool.call 范围 | 仅 docker.build / docker.run / analyzer.analyze | 其他工具未埋 |
| 不新增 DB 表 | 纯文件追加 | 想 SQL 查询需先导入日志 |

**如果某条假设不再适用**，改 `analytics.py` 一处即可（helper 设计成单一入口）。

---

## 5. 隐私与合规红线

**绝对不要**往 `fields` 里传：
- 飞书/企微的原始消息文本
- 用户手机号、邮箱、姓名
- 工具的完整参数（命令、文件路径除外）
- 错误堆栈原文
- 任何含 token / api_key / password 的内容

**当前已做的脱敏**：
- `user_id_hash` = SHA256(open_id)[:16]
- `action_args_hash` = SHA256(JSON)[:16]
- `error_msg_hash` = SHA256(str(exception))[:16]
- 消息原文 → 仅存 `msg_len`
- 错误原文 → 归类为 8 个 `error_code` 枚举

**新增埋点前自检**：字段里有没有任何"原文明文"？有则改哈希或删字段。

---

## 6. 故障排查清单

| 现象 | 原因 | 处理 |
|------|------|------|
| `analytics_events.log` 不增长 | emit_event 静默失败 | `logger.debug` 打开看 stderr |
| 文件内容是乱码交叉 | 多进程写冲突 | 见 §1 |
| 磁盘满 | 没轮转 | 见 §2 |
| 指标数字异常小 | 时间窗不够 | 见 §3 |
| trace_id 全是 `tr-` 前缀 | observability 未初始化 | 检查 `setup_logging()` 是否在 main.py 启动时调用 |
| 字段缺失 | caller 没传 | 查 `analytics_spec.md` §2 各事件的必填字段 |

---

## 7. 何时回头看这份文档

- **部署生产前**：必读 §1、§2
- **加新埋点前**：必读 §4、§5
- **数字对不上时**：必读 §6
- **每月一次**：检查 §2 轮转是否在跑、§3 数据是否需要重置

---

## 8. 下一步可选动作

按 ROI 排序：

| 动作 | 价值 | 工作量 | 何时做 |
|------|------|--------|--------|
| 修多进程丢行（§1 方案 A） | **高**（生产前必须） | 1 天 | 部署生产前 |
| 加 logrotate（§2 方案 A） | 中 | 0.1 天 | 部署生产前 |
| 把 5 指标做成 Grafana 面板 | 中 | 1 天 | 数据稳定后 |
| 新增 `tool.call` 覆盖其他工具 | 低 | 1-2 天 | 需要时 |
| 把日志从文件改到 Redis Stream | 中 | 1 天 | 多实例部署时 |
