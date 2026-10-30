"""Developer 按 CEO 派发的一项任务执行，并交回结果文档。"""
import json
import os
from pathlib import Path

import pytest

from agents.developer.agent import DeveloperAgent
from models.message import Message
from tools.code.opencode import OpenCodeResult


class ScriptLLM:
    def __init__(self, payload):
        self.payload = payload

    def get_last_usage(self):
        return {}

    async def chat(self, messages, temperature=0.2, max_tokens=1200):
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
    dev = DeveloperAgent(llm_client=llm, base_dir=str(tmp))
    dev.opencode = code
    return dev


def assignment(tmp, **extra):
    client = tmp / "flowtest"
    client.mkdir()
    doc = client / "需求文档.md"
    doc.write_text("# 时间接口\n只要 JSON。\n", encoding="utf-8")
    meta = {
        "task_id": "t1",
        "task_title": "实现时间接口",
        "acceptance": "留下 api.py",
        "design_doc_path": str(doc),
        "client_dir": str(client),
        "upstream": ["- 上游说明: /work/spec.md"],
    }
    meta.update(extra)
    return Message(
        content="实现返回当前时间的接口\n验收：留下 api.py\n需求文档：" + str(doc),
        cause_by="ceo_task_assignment",
        metadata=meta,
    )


@pytest.mark.asyncio
async def test_plan_runs_in_order_and_returns_result_doc(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "api", "detail": "写接口", "depends_on": [], "output_file": "api.py"},
        {"name": "readme", "detail": "写说明", "depends_on": ["api"], "output_file": "README.md"},
    ]}), code)
    result = await dev.run(assignment(tmp_path, acceptance="留下 api.py 和 README.md"))

    assert result.cause_by == "developer_result"
    assert result.metadata["ok"] is True
    result_path = Path(result.metadata["result_path"])
    text = result_path.read_text(encoding="utf-8")
    assert "状态：完成" in text
    assert "结论：通过" in text
    assert "给下一项" in text
    assert result_path.parent.joinpath("plan.md").exists()
    assert len(code.calls) == 2
    assert code.calls[0]["working_dir"].endswith("flowtest")
    assert "需求文档摘录" in code.calls[0]["context"]
    assert "时间接口" in code.calls[0]["context"]
    assert "上游说明" in code.calls[0]["context"]
    assert "STDOUT_SHOULD_NOT_TRAVEL" not in code.calls[1]["context"]
    assert "api.py" in code.calls[1]["context"]
    assert "已完成小项" in code.calls[1]["context"]
    assert not (tmp_path / "client_").exists()


@pytest.mark.asyncio
async def test_missing_output_fails_and_skips_dependent(tmp_path):
    code = FakeCode(write=False)
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "api", "detail": "写接口", "depends_on": [], "output_file": "api.py"},
        {"name": "readme", "detail": "写说明", "depends_on": ["api"], "output_file": "README.md"},
    ]}), code)
    result = await dev.run(assignment(tmp_path))

    assert result.cause_by == "error"
    text = Path(result.metadata["result_path"]).read_text(encoding="utf-8")
    assert "状态：失败" in text
    assert "api：失败" in text
    assert "readme：跳过" in text
    assert "未通过：缺少 api.py" in text
    assert len(code.calls) == 1


@pytest.mark.asyncio
async def test_rejects_missing_or_escaped_client_dir(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": []}), code)
    missing = await dev.run(Message(
        content="做接口",
        cause_by="ceo_task_assignment",
        metadata={"task_id": "t1", "client_dir": ""},
    ))
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    escaped = await dev.run(Message(
        content="做接口",
        cause_by="ceo_task_assignment",
        metadata={"task_id": "t1", "client_dir": str(outside)},
    ))
    assert missing.cause_by == "error" and "客户目录" in missing.content
    assert escaped.cause_by == "error"
    assert code.calls == []


@pytest.mark.asyncio
async def test_cyclic_plan_does_not_call_opencode(tmp_path):
    code = FakeCode()
    dev = agent(tmp_path, ScriptLLM({"items": [
        {"name": "a", "detail": "甲", "depends_on": ["b"], "output_file": "a.py"},
        {"name": "b", "detail": "乙", "depends_on": ["a"], "output_file": "b.py"},
    ]}), code)
    result = await dev.run(assignment(tmp_path, acceptance=""))
    assert result.cause_by == "error"
    assert code.calls == []
    text = Path(result.metadata["result_path"]).read_text(encoding="utf-8")
    assert "循环依赖" in text


@pytest.mark.asyncio
async def test_acceptance_existing_supporting_files_are_evidence_not_claimed_outputs(tmp_path):
    class MultiFileCode(FakeCode):
        async def execute(self, task, working_dir=None, context=None, volume=None):
            result = await super().execute(task,working_dir,context,volume)
            (Path(working_dir) / "build.js").write_text("console.log(1)", encoding="utf-8")
            return result
    dev=agent(tmp_path,ScriptLLM({"items":[{"name":"manifest","detail":"write","depends_on":[],"output_file":"package.json"}]}),MultiFileCode())
    result=await dev.run(assignment(tmp_path,acceptance="交付 package.json 和 build.js，构建生成 dist/index.html"))
    assert result.cause_by=="developer_result"
    assert result.metadata["files"]==["package.json"]
    assert "build.js" in result.metadata["evidence_files"]
    assert "dist/index.html" not in result.metadata["evidence_files"]
