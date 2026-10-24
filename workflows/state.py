"""项目状态黑板。

每个节点完成后产出一份 TaskOutcome，按层累积成 ProjectState；
下游节点拿到的是「祖先闭包」的结构化视图，而不是直接父节点的一行摘要。
"""

from dataclasses import dataclass, field


_STATUS_LABEL = {"ok": "通过", "failed": "失败", "skipped": "跳过"}


def clip_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rel_path(path, *roots: str) -> str:
    """把绝对路径转成相对项目根的路径；无法匹配时退回文件名。"""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return ""
    for root in roots:
        base = str(root or "").rstrip("/")
        if base and raw.startswith(base + "/"):
            return raw[len(base) + 1:]
    if raw.startswith("/"):
        return raw.rsplit("/", 1)[-1]
    return raw


@dataclass
class TaskOutcome:
    """一个节点的结构化产出。"""

    task_id: str
    title: str
    role: str
    depth: int
    status: str                     # ok / failed / skipped
    summary: str = ""
    files: list[str] = field(default_factory=list)
    acceptance: str = ""
    passed: bool = True
    artifacts: dict = field(default_factory=dict)
    error: str = ""
    result_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def line(self) -> str:
        label = _STATUS_LABEL.get(self.status, self.status)
        files = "、".join(self.files[:6]) or "无"
        return f"第{self.depth}层 [{self.role}] {self.title} — {label}（产出：{files}）"


@dataclass
class ProjectState:
    """项目级累积状态：需求、文档、各层产出、产物索引、问题清单。"""

    requirement: str = ""
    design_doc_path: str = ""
    outcomes: dict[str, TaskOutcome] = field(default_factory=dict)

    def add(self, outcome: TaskOutcome) -> None:
        self.outcomes[outcome.task_id] = outcome

    def ordered(self) -> list[TaskOutcome]:
        return sorted(self.outcomes.values(), key=lambda o: (o.depth, o.title))

    def files(self) -> list[tuple[str, str]]:
        """(文件, 产出者) 去重后的累计产物。"""
        seen: dict[str, str] = {}
        for outcome in self.ordered():
            for path in outcome.files:
                seen.setdefault(path, outcome.title)
        return list(seen.items())

    def issues(self) -> list[TaskOutcome]:
        return [o for o in self.ordered() if o.status != "ok"]

    def render(self, current_desc: str, current_acceptance: str = "", budget_chars: int = 6000) -> str:
        """渲染成下游 Agent 的上下文块。预算按字符估算，优先级：当前任务 > 需求 > 上游。"""
        parts: list[str] = []
        if self.requirement:
            parts.append("## 项目需求（CEO）\n" + clip_text(self.requirement, 1000))
        if self.design_doc_path:
            parts.append(f"## 需求文档\n路径：{self.design_doc_path}（需要细节可读取）")

        upstream = self.ordered()
        if upstream:
            lines = []
            for outcome in upstream:
                line = outcome.line()
                if outcome.summary:
                    line += f"\n  摘要：{clip_text(outcome.summary, 120)}"
                lines.append(line)
            parts.append("## 已完成的上游\n" + "\n".join(lines))

        files = self.files()
        if files:
            body = "\n".join(f"- {path}（{producer}）" for path, producer in files[:20])
            if len(files) > 20:
                body += f"\n- ……另有 {len(files) - 20} 个文件"
            parts.append("## 累计产物\n" + body)

        issues = self.issues()
        if issues:
            body = "\n".join(
                f"- [{o.title}] {clip_text(o.error or o.summary, 200)}" for o in issues[:5]
            )
            parts.append("## 已知问题\n" + body)

        current = "## 当前任务\n" + clip_text(current_desc, 1500)
        if current_acceptance:
            current += f"\n\n验收：{clip_text(current_acceptance, 600)}"
        parts.append(current)

        text = "\n\n".join(parts)
        if len(text) > budget_chars:
            text = text[: budget_chars - 1] + "…"
        return text
