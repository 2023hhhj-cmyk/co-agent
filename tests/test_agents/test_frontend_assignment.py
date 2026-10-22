"""FrontendExpert 按 CEO 派发的一项任务执行，并交回结果文档。"""
import json
from pathlib import Path

import pytest

from agents.frontend_expert.agent import FrontendExpertAgent
from models.message import Message
from tools.code.opencode import OpenCodeResult


class ScriptLLM:
    def __init__(self, payload):
        self.payload = payload

    def get_last_usage(self):
        return {}

    async def chat(self, messages, temperature=0.2, max_tokens=1200, **kwargs):
        return json.dumps(self.payload)


class FakeCode:
    def __init__(self, write=True):
        self.write = write
        self.calls = []

    async def execute(self, task, working_dir=None, context=None, volume=None):
        self.calls.append({
            "task": task, "working_dir": working_dir, "context": context or "", "volume": volume,
        })
        if self.write and context and "本项必须留下文件：" in context:
            rel = context.split("本项必须留下文件：", 1)[1].splitlines()[0].strip()
            path = Path(working_dir) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("ok", encoding="utf-8")
        return OpenCodeResult(success=True, output="STDOUT_SHOULD_NOT_TRAVEL", working_dir=working_dir)


def agent(tmp, llm, code):
    dev = FrontendExpertAgent(llm_client=llm, base_dir=str(tmp))
    dev.opencode = code
    return dev


def assignment(tmp, **extra):
    client = tmp / "flowtest"
    client.mkdir()
    doc = client / "需求文档.md"
    doc.write_text("# 落地页\n做一个产品落地页。\n", encoding="utf-8")
    meta = {
        "task_id": "t1",
        "task_title": "实现落地页",
        "acceptance": "留下 index.html",
        "design_doc_path": str(doc),
        "client_dir": str(client),
        "upstream": ["- 上游说明: /work/spec.md"],
    }
    meta.update(extra)
    return Message(
        content="实现产品落地页\n验收：留下 index.html\n需求文档：" + str(doc),
        cause_by="ceo_task_assignment",
        metadata=meta,
    )


@pytest.mark.asyncio
async def test_plan_runs_in_order_and_returns_result_doc(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "page", "detail": "写落地页", "depends_on": [], "output_file": "index.html"},
        {"name": "readme", "detail": "写说明", "depends_on": ["page"], "output_file": "README.md"},
    ]}), code)
    result = await dev.run(assignment(tmp_path, acceptance="留下 index.html 和 README.md"))

    assert result.cause_by == "frontend_result"
    assert result.metadata["ok"] is True
    result_path = Path(result.metadata["result_path"])
    text = result_path.read_text(encoding="utf-8")
    assert "状态：完成" in text and "结论：通过" in text
    assert result_path.parent.joinpath("plan.md").exists()
    assert len(code.calls) == 2
    assert code.calls[0]["working_dir"].endswith("flowtest")
    assert "需求文档摘录" in code.calls[0]["context"]
    assert "设计规范" in code.calls[0]["context"]
    assert "上游说明" in code.calls[0]["context"]
    assert "STDOUT_SHOULD_NOT_TRAVEL" not in code.calls[1]["context"]
    assert "已完成小项" in code.calls[1]["context"]
    assert not (tmp_path / "frontend_").exists()


@pytest.mark.asyncio
async def test_missing_output_fails_and_skips_dependent(tmp_path):
    code = FakeCode(write=False)
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "page", "detail": "写落地页", "depends_on": [], "output_file": "index.html"},
        {"name": "readme", "detail": "写说明", "depends_on": ["page"], "output_file": "README.md"},
    ]}), code)
    result = await dev.run(assignment(tmp_path))

    assert result.cause_by == "error"
    text = Path(result.metadata["result_path"]).read_text(encoding="utf-8")
    assert "状态：失败" in text
    assert "page：失败" in text
    assert "readme：跳过" in text
    assert "未通过：缺少 index.html" in text
    assert len(code.calls) == 1


@pytest.mark.asyncio
async def test_cyclic_plan_does_not_call_opencode(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "a", "detail": "甲", "depends_on": ["b"], "output_file": "a.html"},
        {"name": "b", "detail": "乙", "depends_on": ["a"], "output_file": "b.html"},
    ]}), code)
    result = await dev.run(assignment(tmp_path, acceptance=""))
    assert result.cause_by == "error"
    assert code.calls == []
    text = Path(result.metadata["result_path"]).read_text(encoding="utf-8")
    assert "循环依赖" in text


@pytest.mark.asyncio
async def test_per_item_skills_injection_and_filtering(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "setup", "detail": "初始化项目", "depends_on": [],
         "output_file": "package.json",
         "skills": ["frontend-design", "web-artifacts-builder"]},
        {"name": "page", "detail": "写页面", "depends_on": ["setup"],
         "output_file": "index.html",
         "skills": ["react-best-practices", "not-a-real-skill"]},
    ]}), code)
    result = await dev.run(assignment(tmp_path, acceptance="留下 package.json 和 index.html"))
    assert result.metadata["ok"] is True

    setup_ctx = code.calls[0]["context"]
    page_ctx = code.calls[1]["context"]
    assert "web-artifacts-builder" in setup_ctx
    assert "accessibility-engineer" not in setup_ctx
    assert "react-best-practices" in page_ctx
    assert "web-artifacts-builder" not in page_ctx
    assert "not-a-real-skill" not in page_ctx


@pytest.mark.asyncio
async def test_default_skill_when_plan_omits_skills(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "page", "detail": "写页面", "depends_on": [], "output_file": "index.html"},
    ]}), code)
    await dev.run(assignment(tmp_path))
    assert "## frontend-design" in code.calls[0]["context"]
    assert "accessibility-engineer" not in code.calls[0]["context"]


@pytest.mark.asyncio
async def test_container_error_is_surfaced(tmp_path):
    dev = agent(tmp_path, ScriptLLM({"items": []}), FakeCode())
    result = await dev.run(Message(
        content="做落地页",
        cause_by="ceo_task_assignment",
        metadata={"task_id": "t1", "container_error": "docker missing"},
    ))
    assert result.cause_by == "error"
    assert "客户容器不可用" in result.content
