from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agents.ceo.agent import CEOAgent

@pytest.fixture
def judge(monkeypatch):
    monkeypatch.setattr("agents.ceo.agent.check_html", AsyncMock(return_value={"loaded": True}))
    ceo = object.__new__(CEOAgent)
    ceo.customer_containers = SimpleNamespace(read_file=AsyncMock(return_value='<h1>你好世界</h1>'))
    ceo.llm = SimpleNamespace(chat_with_json=AsyncMock())
    return ceo

def result(file='index.html', quote='你好世界', status='passed'):
    return {'criteria':[{'criterion':'显示你好世界','status':status,'file':file,'quote':quote,'reason':'checked'}]}

@pytest.mark.asyncio
async def test_accepts_cited_file_text(judge):
    judge.llm.chat_with_json.return_value = result()
    output={'metadata':{'files':['index.html']}}
    ok, _ = await judge._verify_business_acceptance(SimpleNamespace(acceptance='显示你好世界'),SimpleNamespace(container_name='test',client_id='test',volume_name='muzhu-customer-test'),output)
    assert ok and output['metadata']['business_acceptance']['scope']=='artifact_and_browser'

@pytest.mark.asyncio
async def test_rejects_fake_quote_and_missing_evidence(judge):
    judge.llm.chat_with_json.return_value = result(quote='捏造的证据')
    ok, reason = await judge._verify_business_acceptance(SimpleNamespace(acceptance='显示你好世界'),SimpleNamespace(container_name='test',client_id='test',volume_name='muzhu-customer-test'),{'metadata':{'files':['index.html']}})
    assert not ok and '证据引用' in reason
    judge.llm.chat_with_json.return_value = result(status='insufficient')
    ok, _ = await judge._verify_business_acceptance(SimpleNamespace(acceptance='按钮可用'),SimpleNamespace(container_name='test',client_id='test',volume_name='muzhu-customer-test'),{'metadata':{'files':['index.html']}})
    assert not ok

@pytest.mark.asyncio
async def test_rejects_unsafe_path_and_invalid_model_output(judge):
    plan=SimpleNamespace(container_name='test',client_id='test',volume_name='muzhu-customer-test'); task=SimpleNamespace(acceptance='检查')
    ok,_=await judge._verify_business_acceptance(task,plan,{'metadata':{'files':['../secrets']}})
    assert not ok and judge.customer_containers.read_file.await_count==0
    judge.llm.chat_with_json.return_value={'criteria':[]}
    ok,_=await judge._verify_business_acceptance(task,plan,{'metadata':{'files':['index.html']}})
    assert not ok
