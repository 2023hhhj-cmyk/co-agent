# Co-Agent Code Review Report

> 历史审查快照（2026-06-18），不是当前代码的复审结论。2026-10-06 的实现状态与边界见 [AGENTS.md](../AGENTS.md) 和 [产品说明](product.md)。本次保留原报告正文。

> 审查时间：2026-06-18 · 范围：61 文件 / 486 节点 / 2597 边
> 图谱效率：已用 `code-review-graph` 节省约 95% token 探索成本
> 审查人：Claude Code

## 📊 总体健康度

| 维度 | 评分 | 说明 |
|------|------|------|
| 架构设计 | ⭐⭐⭐⭐ | MetaGPT + ChatDev 融合清晰，但高耦合 44 边 |
| 安全防护 | ⭐⭐ | 已有 InputGuard/Sandbox，但多处"防御但不阻断" |
| 并发/一致性 | ⭐⭐ | 多个共享可变状态缺乏锁 |
| 可观测性 | ⭐⭐⭐⭐ | 已有 Prometheus 指标 + trace_id |
| 可测试性 | ⭐ | `tests/` 为空，0% 覆盖率 |
| 错误处理 | ⭐⭐ | 大量 `try/except` 吞错、缺失重试 |

---

## 🔴 P0 严重漏洞（必须立即修复）

### 1. Wecom 客户端身份验证头根本未带 Token
**文件**：`channels/wecom.py:170-175`
```python
async def _auth_headers(self) -> dict[str, str]:
    """获取带认证的请求头"""
    token = await self._get_access_token()     # ← 算了但没用
    return {
        "Content-Type": "application/json; charset=utf-8",   # ← 没带 token!
    }
```
`token` 算出来后没放进 headers。`send_text_to_external` 走 `_auth_params` 把 token 塞进 URL（`?access_token=xxx`），全链路日志会把 token 写进 access log。

**修复**：要么把 token 放 `Authorization: Bearer` 头，要么确认所有调用都走 `_auth_params`，并清除日志中的 URL。

---

### 2. Wecom 加解密使用非恒定时间比较（计时攻击）
**文件**：`channels/wecom.py:46-50`
```python
def verify_signature(self, ...) -> bool:
    params = sorted([...])
    sha1 = hashlib.sha1(...).hexdigest()
    return sha1 == msg_signature      # ← `==` 触发短路径短路
```
攻击者通过响应时间差能逐字符推断 SHA1。

**修复**：`return hmac.compare_digest(sha1, msg_signature)`。

---

### 3. Wecom `_get_access_token` 竞态（双发重试风暴）
**文件**：`channels/wecom.py:141-168`
两路 webhook 同时到达并 token 过期 → 两个并发请求同时刷新 → 两次网络往返 + 后写入覆盖前写入。`self._access_token = ...` 没有任何锁。

**修复**：`asyncio.Lock` 串行化刷新逻辑，或缓存"正在刷新"的 future。

---

### 4. `response_url` 来自外部 → SSRF + 凭据泄露
**文件**：`main.py:778-787`
```python
resp = await client.post(
    response_url,        # ← 来自企业微信回调体的明文字段
    json={"msgtype": "markdown", "markdown": {"content": response}},
)
```
`response_url` 来自 `msg.get("response_url", "")`。如果攻击者构造一条带恶意 `response_url` 的加密消息（前提是签名通过），可以：
- 携带 CEO 的内部回复内容去任意 HTTPS 端点（数据外泄）
- 通过 `response_url` 内网地址探测内网端口（SSRF）

**修复**：白名单校验 `response_url` 的 host（如只允许 `*.qq.com`），或完全改成主动调用 wecom `message/send` 接口。

---

### 5. CEO 计划确认缺乏来源绑定（横向越权）
**文件**：`agents/ceo/agent.py:487-503`
```python
async def _handle_card_callback(self, chat_id, action, plan_id, message_id):
    plan = self._active_plans.get(chat_id)   # ← 只看 chat_id
    if action == "confirm":
        await self._execute_plan(chat_id, plan)  # ← 任何能向此 chat_id 发消息的人都能触发
```
`chat_id` 是飞书的会话 ID（多人/群共享）。如果 A 用户发起的项目，B 用户通过相同 chat_id 发送伪造的卡片回调（action=confirm），就能启动 A 的项目计划。

**修复**：回调必须带 `sender_open_id`，与 `plan.user_message_id` 关联的 sender 匹配才能确认。

---

### 6. 飞书 Webhook 签名验证可被绕过
**文件**：`main.py:614-628`
```python
if settings.FEISHU_VERIFICATION_TOKEN:
    expected = FeishuClient.verify_webhook_signature(...)
    if signature != expected:
        ...
else:
    logger.warning("FEISHU_VERIFICATION_TOKEN 未配置，跳过签名验证（不安全）")
```
"未配置 → 警告 + 放行"是经典错误模式。生产环境忘了配 env 就裸奔。
并且 `verify_webhook_signature` 自身有 bug：飞书官方签名是 `hmac_sha256(verification_token, body)` 直接对 body，而代码用 `f"{timestamp}\n{token}\n{body}"` 三段拼接——公式错了，就算配了 token 也通不过校验。

**修复**：
- 强制 token 必填，启动时若空直接 panic
- 对照飞书官方 OpenAPI 文档修正签名拼接

---

### 7. Prompt 注入 + 反射到 LLM 的攻击面
**文件**：`agents/ceo/agent.py:672-732`、`agents/customer_service/agent.py:667-714`
`_read_all_project_files` 把 `/root/*/claude.md` 和 `product.md` 截断后塞进 system prompt。任何能写文件到 `/root/<name>/` 的人（包括 LLM 通过 Claude Code 自己写文件）就能持续影响 CEO 的行为。

**附加问题**：`InputGuard` 用纯正则做 prompt 注入检测（`security/input_guard.py:31-46`），13 条规则极易绕过：
- 大小写绕过（`IgNoRe`）
- Unicode 同形字（`İgnore`）
- 编码混淆（`ignore​ previous`）
- 隐藏指令（`<!-- ignore previous -->`）

**修复**：
- project 文件内容不要直接拼进 prompt，应作为 RAG 检索结果
- 引入第二 LLM 做 prompt-injection 分类（不建议 100% 靠规则）
- 文件变更需审计签名（`audit.py:log_file_access` 已有但没接）

---

## 🟠 P1 高危（应在一周内修复）

### 8. CEO Agent 共享 `_current_*` 实例字段（线程/任务不安全）
**文件**：`agents/ceo/agent.py:172-174, 776-780`
```python
self._current_chat_id = chat_id
self._current_sender_id = sender_id
self._current_chat_type = chat_type
```
FastAPI webhook 是并发处理的。用户 A 的请求设置 `self._current_sender_id = "A"`，异步任务中调用 `_send_feishu_text` 时，用户 B 的请求可能已经把 `_current_sender_id` 改成了 "B"——回复发错对象。

**修复**：用 `contextvars.ContextVar` 或把 sender 信息作为参数显式传入每个发送函数。

---

### 9. `MessageBus` 内存队列 + 无持久化
**文件**：`communication/message_bus.py:29-40`、`models/message.py:75-96`
- `MessageQueue` 用 `asyncio.Queue`，进程重启 = 消息全丢
- `get_history` 只有 1000 条滚动窗口
- 没有任何投递确认机制
- `BaseAgent.put_message` 用 `asyncio.create_task` 触发 fire-and-forget，可能在 await 之前被 GC

**修复**：换 Redis Streams 做持久化、ack 机制；`put_message` 改 `await`。

---

### 10. 卡片回调存在重复触发风险
**文件**：`main.py:268-275`
```python
card_msg_id = f"card_{value.get('action', '')}_{value.get('plan_id', '')}_{int(asyncio.get_event_loop().time())}"
```
- 用 `time()` 拼 message_id 在高并发下可能撞车
- `asyncio.get_event_loop()` 在 3.12+ 已废弃
- `asyncio.get_event_loop()` 在非主线程中拿不到 running loop

**修复**：`uuid.uuid4().hex` + `asyncio.get_running_loop()`。

---

### 11. DevOps 部署链路缺路径校验
**文件**：`agents/devops/agent.py:430-449, 482-495`
```python
def _extract_project_dir(self, message: Message) -> Optional[str]:
    if "client_dir" in message.metadata:
        return message.metadata["client_dir"]   # ← 元数据直传，无校验
```
- `client_dir` 来自 LLM 拆解结果（CEO Agent 的 JSON 解析），如果 LLM 被 prompt 注入输出 `{"client_dir": "/etc"}`，DevOps 就会 `docker build /etc`（虽然能失败，但会读 `/etc` 全部内容写进镜像）
- `_write_config_files` 直接覆盖 `Dockerfile` 和 `docker-compose.yml`，没确认文件存在
- `claude -p <full_prompt>` 中 `full_prompt` 含用户原文 → 沙箱内有完整 Bash 工具 → 沙箱可写 → 一旦逃逸就 host 沦陷

**修复**：
- `client_dir` 必须落在 `/root/<safe_name>/` 白名单内
- 关键部署信息走 `extra_context` 强类型，不从 message.content 解析
- 沙箱里 `claude` 的命令也走 `claude --append-system-prompt` 而非 `-p` 拼接

---

### 12. Docker 沙箱 `tmpfs` + 大量工具 = 沙箱逃逸风险面
**文件**：`security/sandbox.py:144-155`
```python
"--tmpfs", "/tmp:rw,exec,size=256m",
"-v", f"{cwd}:/workspace:rw",
```
- `--tmpfs /tmp:rw,exec` 可执行，攻击者可以放编译好的二进制
- 网络是 `none`，但容器内 `claude` CLI 若有任何对外调用（plugin、telemetry）会失败，导致 Claude Code 行为降级
- `--read-only` 配 `tmpfs:exec` 本身合规，但 Claude Code 的 mcp server 经常需要更多目录可写

**修复**：在 tmpfs 关闭 exec（`exec` 改为 `noexec`），并用 `--security-opt no-new-privileges` + seccomp profile。

---

### 13. `_build_command` 沙箱回退模式失效
**文件**：`security/sandbox.py:226-230`
```python
cmd = ["claude", "-p", full_prompt]
for tool in READONLY_TOOLS:
    cmd.extend(["--allowed-tools", tool])
```
**`READONLY_TOOLS` 包含 `"Bash(read-only)"`**，但 Claude Code 实际不会按这个语法做限制（`Bash(read-only)` 不是合法工具名）。`Read` 类的限制是误用——Claude Code 仍然可以执行 Bash 并读写 workspace。

**修复**：在沙箱回退模式中直接禁用 Bash 工具（只给 Read/Glob/Grep），或用 `--permission-mode` 等真支持的参数。

---

### 14. 限流装饰器挂错位置导致 webhooks 不被限流
**文件**：`main.py:598, 710, 827`
```python
@app.post("/api/feishu/webhook")
@limiter.limit("120/minute")
async def feishu_webhook(request: Request): ...
```
slowapi 装饰器在 `request: Request` 上工作，但 Feishu webhook body 通常带大消息 + 签名，120/分钟会被攻击者用大 body 撑爆带宽。`wecom_ceo_callback` 同理。

**修复**：基于 body 字节数限流（如 `5 MB/min`）+ 消息去重池持久化到 Redis（当前是内存 set，重启清零）。

---

### 15. 内存缓存中重放保护不足
**文件**：`main.py:706-765`、`agents/ceo/agent.py:120-169`
```python
_ceo_processed_messages: set[str] = set()
...
if len(_ceo_processed_messages) > 1000:
    _ceo_processed_messages = set(list(_ceo_processed_messages)[-500:])
```
- 全局可变 set 在并发请求下不是线程/任务安全（`set.add` + `list[-500:]` 不是原子操作）
- 切窗口时丢 500 条 → 重放窗口
- CEO Agent 内部还有一份 `_processed_messages`，**两套独立去重**会导致相同 message_id 走两条路径

**修复**：用 Redis `SETNX` 实现去重 + TTL = 24h。

---

## 🟡 P2 中危（一个月内规划修复）

### 16. 高耦合 44 边：`ceo-handle` ↔ `models-dict`
CEO/CS/DevOps 三个 Agent 直接调用 `models/agent.py`、`models/message.py`、`models/task.py` 的 Pydantic 字段定义。任何模型字段重命名都波及三个 Agent。

**建议**：在 Agent 和 Model 之间加 `AgentAdapter`/`MessageMapper` 层。

### 17. 异常吞没掩盖根因
`agents/ceo/agent.py:262, 333, 421, 564, 670, 731, 784` 等几十处都是：
```python
except Exception as e:
    logger.error(f"...")
    return <默认值>   # ← 业务静默失败
```
没有 stack trace、没上报到 `audit_logger`，监控面板看不到。

### 18. CS 队列断电丢消息
`CSMessageQueue` 用 `LPUSH/RPOP` 但 Redis 默认配置可能开启 AOF/RDB 时间窗口内的数据丢失。`save_notify_msg_id` 也没用持久化 keyspace notification。

### 19. 速率限制 IP-based 容易被 NAT 误伤
`get_remote_address` 用 `request.client.host`，在企业 NAT 后是同一个 IP → 所有员工共享限流配额。
**建议**：结合 `X-Forwarded-For`（nginx 配 `set_header`）+ 路径粒度（webhook 与 API 限流分离）。

### 20. LLM 响应解析脆弱
`agents/ceo/agent.py:443-461`、`tools/llm/client.py:128-139` 都靠手写 JSON 解析。LLM 输出任意 markdown 包裹时极易失败，且失败时只 log warning。

**建议**：用 `instructor`/`outlines` 等结构化输出库 + Pydantic schema 校验。

### 21. `audit_logs` 写失败时静默
`security/audit.py:60-72`：DB 异常被吞了。审计日志正是排查事故时最需要的。

### 22. DevOps 部署时 `cd /root/<client_name>` 写 `Dockerfile` 覆盖
**文件**：`agents/devops/agent.py:482-495`
直接把生成内容覆盖用户项目的 `Dockerfile` 和 `docker-compose.yml`。如果用户项目已有 Dockerfile，会被默默覆盖。

**建议**：写到独立子目录（如 `.deploy/`）或用 `mtime` 备份。

### 23. 监控 callback 错误处理
`tools/deploy/monitor.py`（未读）— `_monitor_loop` 30 秒跑一次。回调异常只是 log，但 `_handle_unhealthy` 的 `on_failure` 如果抛错会导致监控循环退出。

### 24. FastAPI 启动期 lifespan 失败
`main.py:172-312` 里 `lifespan` 启动失败任意一步（如可选依赖不可用），整个 app 启动失败。但里面很多子步骤已经有 try/except 容错（如 KnowledgeMemory），不一致。

### 25. Webhook 缺少 idempotency-key
企业微信/飞书在 5 秒超时后会重试。当前 200 返回 0，没法区分"已处理"和"未处理"。

---

## 🟢 P3 改进建议（设计层/工程实践）

### 26. 缺失测试 — 0% 覆盖率
`tests/` 为空。建议：
- 给 `WecomCrypto` 加向量测试（已知输入/输出对）
- 给 `InputGuard` 加 50+ 注入向量测试
- 给 `WorkflowGraph` 加拓扑排序属性测试（`hypothesis`）
- 给 `MessageBus` 加并发投递测试

### 27. CLAUDE.md 与代码不同步
文档里写 `memory/`, `security/`, `observability/` 为空，实际已经实现。但 SecurityHeaders/PolicyEnforcer 的注入没在文档提及。

### 28. `Dockerfile/compose` 模板未审计
`ProjectAnalyzer.generate_default_dockerfile`（未读）—— 需要确认：
- 是否以非 root 用户运行
- 是否使用多阶段构建
- 是否固定基础镜像 digest

### 29. 长连接重启无重连
`channels/feishu.py:73-118` 的 lark WebSocket 启动后只有 daemon thread，重连机制未实现（lark SDK 自身有，但异常被吞）。

### 30. 配置文件 .env 包含敏感信息
CLAUDE.md 提到需要 `DEEPSEEK_API_KEY`、`WECOM_*_SECRET`。建议：
- 启动时校验所有 secret 不为空
- 集成 Vault/SOPS 而非裸 .env
- CI 中加 `gitleaks`/`trufflehog` 扫描

### 31. CORS 配置硬编码
`config/settings.py:15` 默认 `["http://localhost:3000"]`，生产多前端时易遗漏。

### 32. 可观测性指标有覆盖但告警缺失
`observability/alerts.yml`（未读）需要确认：是否有 oncall 接 PagerDuty/飞书群。

### 33. `ProjectPlan.get_running_tasks` 命名误导
未读但从 graph 看到。`get_running_tasks` 应该返回 `RUNNING` 状态，但实际实现可能是"有依赖未完成"。需确认语义。

### 34. 没有数据库连接池配置
`storage/database.py`（未读）默认 asyncpg 池大小可能不够支撑并发 LLM 调用。

### 35. `csrf_protect` 缺失
API 端点用 `X-API-Key` 但 webhooks 走签名，应该同时校验 `Origin`/`Referer`。

---

## 🎯 修复优先级路线图

```
第 1 周 (P0):  漏洞 1-7 — 飞书/企业微信接入层 + 横向越权
第 2 周 (P0):  漏洞 8-10 — 共享状态/消息总线并发
第 3 周 (P1):  漏洞 11-13 — 部署/沙箱链路加固
第 4 周 (P1):  漏洞 14-15 — 限流/重放保护
第 5-6 周 (P2): 异常处理、监控告警、测试覆盖率
```

## 总结

整体设计清晰（MetaGPT + ChatDev 融合是亮点），但作为**对外服务系统**还有多个生产级别的安全/并发问题。
最严重的是 #4-#6（飞书/企业微信认证可绕过）+ #5（横向越权执行计划），这些是攻击者可以**直接利用的入口**。
其次是 #8-#10（共享状态/消息丢失），属于**可观测但难诊断的稳定性问题**。

建议先把 P0 全部修完并补上单元测试骨架，再考虑扩展功能。

---

## 知识图谱快照

审查时通过 `code-review-graph` 识别的关键信号：

- **10 个社区**，最大 `ceo-handle`（106 节点），最小 `config-settings`（2 节点）
- **高耦合警告**：`ceo-handle` ↔ `models-dict` 跨社区调用 44 次
- **架构 hub 节点**（top 3）：
  1. `DevOpsAgent._deploy` (degree=45)
  2. `Message` class (degree=43)
  3. `wecom_ceo_callback` (degree=39)
- **桥接节点**：ClaudeCodeTool、Message、BaseAgent — 单点故障面
- **知识缺口**：44 个孤立节点、20 个未测试的 hub 节点、1 个单文件社区
- **没有 embeddings**：可考虑运行 `embed_graph_tool` 启用语义搜索
