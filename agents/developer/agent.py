"""
开发工程师 Agent

只执行 CEO 派来的一项任务：
- 读取客户目录、需求文档、验收标准和上游产物
- 在客户目录写下小计划，逐项调用 opencode
- 把结果文档交回 CEO
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.base import BaseAgent
from models.agent import ActionDef, AgentRole, ReactMode
from models.message import Message
from config.settings import settings
from tools.code.opencode import OpenCodeTool
from workflows.state import rel_path
from tools.deploy.customer_container import ContainerFiles, HostFiles, validate_docker_name
from tools.llm.client import LLMClient

logger = logging.getLogger(__name__)

_FILE_RE = re.compile(
    r"(?<![\w./-])([\w./-]+\.(?:py|md|html|json|ts|tsx|js|jsx|css|yml|yaml|txt|toml))"
)
_ASSIGNMENT_CAUSES = {"ceo_task_assignment", "create_prd", "bug_report"}

DEVELOPER_ROLE = AgentRole(
    name="developer",
    profile="资深开发工程师",
    goal="完成 CEO 分配的一项开发任务，并交回结果文档",
    constraints="只在给定的客户目录中工作，按小计划逐项实现，以结果文档交付",
    tools=["opencode"],
    actions=[
        ActionDef(
            name="execute_task",
            description="按 CEO 分配的任务写小计划并实现",
            prompt_template="",
        ),
    ],
    watch=_ASSIGNMENT_CAUSES,
    react_mode=ReactMode.REACT,
    max_react_loop=2,
)


@dataclass
class _PlanItem:
    name: str
    detail: str
    depends_on: list[str] = field(default_factory=list)
    output_file: str = ""


class DeveloperAgent(BaseAgent):
    """开发工程师 Agent"""

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        base_dir: str = "/root",
        memory_manager: Any = None,
        **kwargs,
    ):
        super().__init__(
            role=DEVELOPER_ROLE,
            llm_client=llm_client,
            memory_manager=memory_manager,
            **kwargs,
        )
        self.base_dir = base_dir
        self.opencode = OpenCodeTool(base_dir=base_dir, timeout=settings.OC_TASK_TIMEOUT)

    async def _think(self, messages: list[Message]) -> Optional[ActionDef]:
        """只处理 CEO 派来的一项任务，不根据自己的产出再选下一步。"""
        if not messages:
            return None
        last = messages[-1]
        if last.cause_by in _ASSIGNMENT_CAUSES:
            return self.role.get_action_by_name("execute_task")
        return None

    async def _act(self, action: ActionDef, context: dict[str, Any] = None) -> Message:
        messages = context.get("messages", []) if context else []
        last = messages[-1] if messages else None
        if action.name != "execute_task" or last is None:
            return Message(
                content=f"未知动作：{action.name}",
                cause_by="error",
                sent_from=self.agent_id,
            )
        return await self._execute_assigned(last)

    async def _execute_assigned(self, message: Message) -> Message:
        """读入任务、写小计划、逐项实现，并写出结果文档。"""
        meta = message.metadata or {}
        task_id = self._safe_id(str(meta.get("task_id") or "task"))
        title = str(meta.get("task_title") or task_id)
        description = self._task_description(message)
        acceptance = str(meta.get("acceptance") or "")
        upstream = self._upstream(meta.get("upstream"))
        container_name = str(meta.get("container_name") or "")
        volume_name = str(meta.get("volume_name") or "")
        container_error = str(meta.get("container_error") or "")
        if container_error:
            return Message(
                content=f"客户容器不可用：{container_error}",
                cause_by="error",
                sent_from=self.agent_id,
            )
        try:
            if container_name or volume_name:
                validate_docker_name(container_name)
                validate_docker_name(volume_name)
                api = getattr(self, "customer_containers", None)
                if api is None:
                    raise ValueError("客户容器不可用")
                client_dir = "/workspace"
                design_doc_path = "/workspace/需求文档.md"
                files = ContainerFiles(api, container_name, volume_name)
            else:
                client_dir = self._resolve_client_dir(str(meta.get("client_dir") or ""))
                design_doc_path = self._resolve_doc_path(
                    str(meta.get("design_doc_path") or ""), client_dir,
                )
                files = HostFiles(client_dir)
        except ValueError as exc:
            return Message(content=str(exc), cause_by="error", sent_from=self.agent_id)

        design_excerpt = (await files.read(design_doc_path))[:2000]
        items = await self._make_plan(task_id, title, description, acceptance)
        work_dir = os.path.join(client_dir, "tasks", task_id)
        await files.makedirs(work_dir)
        plan_path = os.path.join(work_dir, "plan.md")
        await files.write(plan_path, self._render_plan(title, items))

        done: dict[str, str] = {}
        outputs: dict[str, str] = {}
        notes: dict[str, str] = {}
        known = {item.name for item in items}
        for item in items:
            if any(dep not in known for dep in item.depends_on):
                done[item.name] = "failed"
                notes[item.name] = "依赖了不存在的小项"
                continue
        while any(item.name not in done for item in items):
            progressed = False
            for item in items:
                if item.name in done:
                    continue
                if any(dep not in done for dep in item.depends_on):
                    continue
                progressed = True
                if any(done.get(dep) != "success" for dep in item.depends_on):
                    done[item.name] = "skipped"
                    notes[item.name] = "前置小项未完成"
                    continue
                ok, path, note = await self._run_item(
                    item, client_dir, design_doc_path, design_excerpt,
                    acceptance, upstream, outputs, files, volume_name,
                )
                done[item.name] = "success" if ok else "failed"
                notes[item.name] = note
                if path:
                    outputs[item.name] = path
            if not progressed:
                for item in items:
                    if item.name not in done:
                        done[item.name] = "failed"
                        notes[item.name] = "小计划存在循环依赖"
                break

        required = self._required_files(items, acceptance)
        missing = []
        for rel in required:
            if not await files.exists(os.path.join(client_dir, rel)):
                missing.append(rel)
        ok = all(status == "success" for status in done.values()) and not missing
        acceptance_text = "通过" if ok else (
            "未通过：缺少 " + "、".join(missing) if missing else "未通过：有小项失败或被跳过"
        )
        summary = self._summary(title, outputs, acceptance_text)
        result_path = os.path.join(work_dir, "result.md")
        await files.write(result_path, self._render_result(
            title, ok, done, notes, outputs, acceptance, acceptance_text, summary,
        ))
        body = f"结果文档：{result_path}\n\n{summary}"
        return Message(
            content=body,
            cause_by="developer_result" if ok else "error",
            sent_from=self.agent_id,
            metadata={
                "result_path": result_path,
                "plan_path": plan_path,
                "client_dir": client_dir,
                "container_name": container_name,
                "volume_name": volume_name,
                "task_id": task_id,
                "ok": ok,
                "files": [rel_path(p, client_dir, "/workspace") for p in outputs.values() if p],
                "acceptance_text": acceptance_text,
                "passed": ok,
                "error": "" if ok else acceptance_text,
            },
        )

    async def _make_plan(
        self, task_id: str, title: str, description: str, acceptance: str,
    ) -> list[_PlanItem]:
        """能一次做完就只产生一项。拆分由这里决定，不交给 opencode。"""
        fallback = [_PlanItem(
            name=task_id,
            detail=description or title,
            output_file=self._first_file(acceptance),
        )]
        if not self.llm:
            return fallback
        prompt = (
            "把下面这一项开发任务拆成实现小计划。能一次做完就只返回一项。\n"
            "严格输出 JSON，不要其他文字：\n"
            '{"items":[{"name":"英文短名","detail":"要改什么","depends_on":[],"output_file":"相对路径"}]}\n'
            "depends_on 填写其他小项的 name。output_file 是相对客户目录的文件，不要写绝对路径。\n\n"
            f"任务：{title}\n{description}\n验收：{acceptance}"
        )
        try:
            raw = await self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1200,
            )
            items = self._parse_items(raw)
            return items or fallback
        except Exception as exc:
            logger.warning("小计划生成失败，按一项执行: %s", exc)
            return fallback

    def _parse_items(self, raw: str) -> list[_PlanItem]:
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
        data = json.loads(text)
        rows = data.get("items") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            return []
        items = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = self._safe_id(str(row.get("name") or f"item_{len(items)+1}"))
            if name in seen:
                continue
            seen.add(name)
            deps = [self._safe_id(str(dep)) for dep in (row.get("depends_on") or []) if str(dep).strip()]
            items.append(_PlanItem(
                name=name,
                detail=str(row.get("detail") or "").strip() or name,
                depends_on=[dep for dep in deps if dep != name],
                output_file=self._safe_rel(str(row.get("output_file") or "")),
            ))
        return items

    async def _run_item(
        self,
        item: _PlanItem,
        client_dir: str,
        design_doc_path: str,
        design_excerpt: str,
        acceptance: str,
        upstream: list[str],
        outputs: dict[str, str],
        files,
        volume: str,
    ) -> tuple[bool, str, str]:
        context = self._step_context(
            client_dir, design_doc_path, design_excerpt, item, acceptance, upstream, outputs,
        )
        task = f"只实现这一项：{item.detail}"
        if item.output_file:
            task += f"\n完成后把结果写入相对路径 {item.output_file}。不要改这一项之外的范围。"
        task += (
            "\n\n环境限制（重要）：这个工作区没有浏览器、没有图形界面。"
            "不要安装或调用 chrome/playwright/selenium，不要联网校验，不要运行 find /、which、curl 之类的环境探测。"
            "第一步就直接用 write 工具创建目标文件并写完整内容，然后再做文件级自检（读回文件确认），完成后立即结束。"
        )
        result = await self.opencode.execute(
            task=task, context=context, working_dir=client_dir, volume=volume or None,
        )
        path = ""
        output_path = os.path.join(client_dir, item.output_file) if item.output_file else ""
        if output_path and await files.exists(output_path):
            path = output_path
        if not result.success:
            return False, path, (result.error or "opencode 执行失败")[:200]
        if item.output_file and not path:
            return False, "", f"未留下文件 {item.output_file}"
        return True, path, "完成"

    def _step_context(
        self,
        client_dir: str,
        design_doc_path: str,
        design_excerpt: str,
        item: _PlanItem,
        acceptance: str,
        upstream: list[str],
        outputs: dict[str, str],
    ) -> str:
        lines = [
            f"客户目录：{client_dir}",
            f"需求文档：{design_doc_path or '无'}",
        ]
        if design_excerpt:
            lines.append(f"需求文档摘录：\n{design_excerpt}")
        lines.append(f"当前小项：{item.detail}")
        if acceptance:
            lines.append(f"验收：{acceptance}")
        if item.output_file:
            lines.append(f"本项必须留下文件：{item.output_file}")
        if outputs:
            lines.append("已完成小项：")
            for name, path in outputs.items():
                lines.append(f"- {name}: {path}")
        if upstream:
            lines.append("上游产物：")
            lines.extend(f"- {item_text}" for item_text in upstream)
        return "\n".join(lines)

    def _resolve_client_dir(self, raw: str) -> str:
        if not raw:
            raise ValueError("缺少客户目录，无法执行")
        base = os.path.realpath(self.base_dir)
        target = os.path.realpath(raw)
        try:
            inside = os.path.commonpath([base, target]) == base
        except ValueError:
            inside = False
        if not inside or not os.path.isdir(target):
            raise ValueError("客户目录不可用")
        return target

    def _resolve_doc_path(self, raw: str, client_dir: str) -> str:
        if not raw:
            return ""
        path = os.path.realpath(raw)
        base = os.path.realpath(self.base_dir)
        try:
            inside = os.path.commonpath([base, path]) == base
        except ValueError:
            inside = False
        if inside and os.path.isfile(path) and path.startswith(client_dir + os.sep):
            return path
        return ""

    def _required_files(self, items: list[_PlanItem], acceptance: str) -> list[str]:
        files = []
        for item in items:
            if item.output_file and item.output_file not in files:
                files.append(item.output_file)
        if not files:
            # 只在计划没有声明输出文件时，才退回从验收文字里推断文件名。
            for match in _FILE_RE.findall(acceptance or ""):
                rel = self._safe_rel(match)
                if rel and rel not in files:
                    files.append(rel)
        return files

    def _task_description(self, message: Message) -> str:
        text = message.content or ""
        for marker in ("\n验收：", "\n需求文档：", "\n上游产物："):
            if marker in text:
                text = text.split(marker)[0]
        return text.strip()

    def _upstream(self, raw: Any) -> list[str]:
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _render_plan(self, title: str, items: list[_PlanItem]) -> str:
        lines = [f"# 实现计划", "", f"任务：{title}", ""]
        for item in items:
            deps = "、".join(item.depends_on) if item.depends_on else "无"
            lines.extend([
                f"## {item.name}",
                f"- 要改什么：{item.detail}",
                f"- 依赖：{deps}",
                f"- 留下文件：{item.output_file or '无'}",
                "",
            ])
        return "\n".join(lines)

    def _render_result(
        self,
        title: str,
        ok: bool,
        done: dict[str, str],
        notes: dict[str, str],
        outputs: dict[str, str],
        acceptance: str,
        acceptance_text: str,
        summary: str,
    ) -> str:
        lines = [
            "# 任务结果",
            "",
            f"- 任务：{title}",
            f"- 状态：{'完成' if ok else '失败'}",
            "",
            "## 小项",
        ]
        for name, status in done.items():
            label = {"success": "完成", "failed": "失败", "skipped": "跳过"}.get(status, status)
            extra = f"：{notes[name]}" if notes.get(name) else ""
            lines.append(f"- {name}：{label}{extra}")
        lines.extend(["", "## 写下的文件"])
        if outputs:
            lines.extend(f"- {path}" for path in outputs.values())
        else:
            lines.append("- 无")
        lines.extend([
            "",
            "## 验收",
            acceptance or "无",
            "",
            f"结论：{acceptance_text}",
            "",
            "## 给下一项",
            summary,
            "",
        ])
        return "\n".join(lines)

    def _summary(self, title: str, outputs: dict[str, str], acceptance_text: str) -> str:
        files = "、".join(os.path.basename(path) for path in outputs.values()) or "无"
        return f"已处理「{title}」。文件：{files}。验收：{acceptance_text}。"

    def _first_file(self, acceptance: str) -> str:
        found = _FILE_RE.findall(acceptance or "")
        return self._safe_rel(found[0]) if found else ""

    def _safe_id(self, value: str) -> str:
        safe = re.sub(r"[^\w一-鿿-]", "_", value).strip("_")
        return (safe or "task")[:64]

    def _safe_rel(self, value: str) -> str:
        text = (value or "").strip().replace("\\", "/")
        if not text or text.startswith("/") or ".." in text.split("/"):
            return ""
        return text
