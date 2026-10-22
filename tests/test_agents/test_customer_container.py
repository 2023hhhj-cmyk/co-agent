"""客户容器由 CEO 创建，Developer 只在容器工作区里写文件。"""
import pytest

from agents.ceo.agent import CEOAgent
from agents.developer.agent import DeveloperAgent
from models.message import Message
from tools.code.opencode import OpenCodeResult
from tools.deploy.customer_container import CustomerContainers, container_name_for


class Runner:
    def __init__(self):
        self.commands = []
        self.stdin = []

    async def __call__(self, args, stdin):
        self.commands.append(list(args))
        self.stdin.append(stdin.decode("utf-8") if stdin else "")
        if args[:1] == ["inspect"]:
            return 1, "", "missing"
        return 0, "", ""


class MemoryApi:
    def __init__(self):
        self.files = {}

    async def makedirs(self, name, path):
        self.files.setdefault(path, "")

    async def write_file(self, name, path, text):
        self.files[path] = text

    async def read_file(self, name, path, limit=2000):
        return (self.files.get(path) or "")[:limit]

    async def exists(self, name, path):
        return bool(self.files.get(path))


class ScriptLLM:
    def get_last_usage(self):
        return {}

    async def chat(self, messages, temperature=0.2, max_tokens=1200):
        return '{"items":[{"name":"api","detail":"写接口","depends_on":[],"output_file":"api.py"}]}'


class ContainerCode:
    def __init__(self, api):
        self.api = api
        self.calls = []

    async def execute(self, task, working_dir=None, context=None, volume=None):
        self.calls.append({"working_dir": working_dir, "context": context or "", "volume": volume})
        if context and "本项必须留下文件：" in context:
            rel = context.split("本项必须留下文件：", 1)[1].splitlines()[0].strip()
            await self.api.write_file(volume, f"/workspace/{rel}", "ok")
        return OpenCodeResult(success=True, output="STDOUT", working_dir=working_dir)


@pytest.mark.asyncio
async def test_ceo_creates_workspace_container_without_database():
    runner = Runner()
    ceo = CEOAgent.__new__(CEOAgent)
    ceo.llm = None
    ceo.product_doc = ""
    ceo._active_plans = {}
    ceo.template = {"agent_capabilities": {}}
    ceo.customer_containers = CustomerContainers(runner=runner)
    plan = await ceo._decompose_tasks("客户：acme 做一个内部工具", "chat", "m1")

    assert plan.container_name == container_name_for("acme")
    assert plan.volume_name == plan.container_name
    assert plan.client_dir == "/workspace"
    assert plan.design_doc_path == "/workspace/需求文档.md"
    joined = [" ".join(cmd) for cmd in runner.commands]
    assert any(cmd.startswith("volume create ") for cmd in joined)
    assert any("entrypoint" in cmd and "sleep" in cmd for cmd in joined)
    assert all("postgres" not in cmd and "redis" not in cmd for cmd in joined)
    assert any("内部工具" in text for text in runner.stdin)


class FailRunner:
    async def __call__(self, args, stdin):
        return 1, "", "docker missing"


@pytest.mark.asyncio
async def test_container_failure_is_recorded_and_surfaced(tmp_path):
    ceo = CEOAgent.__new__(CEOAgent)
    ceo.llm = None
    ceo.product_doc = ""
    ceo._active_plans = {}
    ceo.template = {"agent_capabilities": {}}
    ceo.customer_containers = CustomerContainers(runner=FailRunner())
    plan = await ceo._decompose_tasks("客户：acme 做一个内部工具", "chat", "m1")

    assert plan.container_error
    assert plan.container_name == ""
    assert plan.client_dir == ""

    dev = DeveloperAgent(llm_client=ScriptLLM(), base_dir=str(tmp_path))
    result = await dev.run(Message(
        content="实现接口",
        cause_by="ceo_task_assignment",
        metadata={
            "task_id": "t1",
            "task_title": "实现时间接口",
            "container_error": plan.container_error,
        },
    ))
    assert result.cause_by == "error"
    assert "客户容器不可用" in result.content


@pytest.mark.asyncio
async def test_developer_builds_files_in_customer_volume(tmp_path):
    api = MemoryApi()
    await api.write_file("muzhu-customer-flowtest", "/workspace/需求文档.md", "# 时间接口\n")
    code = ContainerCode(api)
    dev = DeveloperAgent(llm_client=ScriptLLM(), base_dir=str(tmp_path))
    dev.customer_containers = api
    dev.opencode = code
    result = await dev.run(Message(
        content="实现接口\n验收：留下 api.py",
        cause_by="ceo_task_assignment",
        metadata={
            "task_id": "t1",
            "task_title": "实现时间接口",
            "acceptance": "留下 api.py",
            "container_name": "muzhu-customer-flowtest",
            "volume_name": "muzhu-customer-flowtest",
            "client_dir": "/workspace",
            "design_doc_path": "/workspace/需求文档.md",
        },
    ))
    assert result.cause_by == "developer_result"
    assert code.calls[0]["volume"] == "muzhu-customer-flowtest"
    assert code.calls[0]["working_dir"] == "/workspace"
    assert "时间接口" in code.calls[0]["context"]
    assert "/workspace/tasks/t1/plan.md" in api.files
    assert "/workspace/tasks/t1/result.md" in api.files
    assert "状态：完成" in api.files["/workspace/tasks/t1/result.md"]
    assert not (tmp_path / "flowtest").exists()
