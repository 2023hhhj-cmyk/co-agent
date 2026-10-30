import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agents.ceo.agent import CEOAgent
from models.task import Task, ProjectPlan

@pytest.fixture
def setup(monkeypatch):
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='<h1>hello world</h1>'),write_file=AsyncMock())
    a.llm=SimpleNamespace(chat_with_json=AsyncMock())
    monkeypatch.setattr('agents.ceo.agent.check_html',AsyncMock(return_value={'loaded':True,'visible_text':'hello world','console_errors':[],'page_errors':[],'external_requests':[]}))
    plan=ProjectPlan(requirement='x',client_id='c1',container_name='muzhu-customer-c1',volume_name='muzhu-customer-c1')
    task=Task(title='page',description='test',assigned_to='developer',acceptance='shows hello world')
    out={'content':'done','metadata':{'files':['index.html'],'passed':True}}
    return a,plan,task,out

@pytest.mark.asyncio
async def test_invalid_model_response_retried_and_browser_evidence_checked(setup):
    a,plan,task,out=setup
    a.llm.chat_with_json.side_effect=[{'_parse_error':'bad','raw_response':'invalid'}, {'criteria':[{'criterion':'visible text','status':'passed','file':'browser_runtime.json','quote':'hello world'}]}]
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    assert len(out['metadata']['business_acceptance']['attempts'])==1
    assert a.llm.chat_with_json.await_args.kwargs['response_format']=={'type':'json_object'}

@pytest.mark.asyncio
async def test_invalid_model_response_is_recorded_on_failure(setup):
    a,plan,task,out=setup; a.llm.chat_with_json.return_value={'_parse_error':'bad','raw_response':'bad output'}
    a._record_outcome=lambda *x:None
    a._send_feishu_text=AsyncMock()
    context={'outputs':{task.id:out}}
    await a._settle_task('chat',plan,task,context)
    saved=json.loads(a.customer_containers.write_file.await_args.args[2])
    assert not saved['passed'] and len(saved['attempts'])==2 and '两次' in saved['reason']
    assert 'error' in context['outputs'][task.id]

@pytest.mark.asyncio
async def test_wrong_volume_fails_closed(setup):
    a,plan,task,out=setup; plan.volume_name='different-client'
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert not ok and '工作区' in reason
    a.llm.chat_with_json.assert_not_awaited()

@pytest.mark.asyncio
async def test_fabricated_browser_evidence_is_rejected(setup):
    a,plan,task,out=setup
    a.llm.chat_with_json.return_value={'criteria':[{'criterion':'result','status':'passed','file':'browser_runtime.json','quote':'fake text'}]}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert not ok and '证据引用' in reason

@pytest.mark.asyncio
async def test_deployment_checks_actual_url_and_content(setup,monkeypatch):
    a,plan,task,out=setup
    from config.settings import settings
    monkeypatch.setattr(settings,'PUBLIC_BASE_URL','https://example.test')
    url='https://example.test/c/c1/d123456789abc/'
    out['metadata'].update({'action':'static_deploy','site_url':url})
    response=SimpleNamespace(status_code=200,content=b'<h1>hello world</h1>',headers={'content-type':'text/html'})
    client=SimpleNamespace(get=AsyncMock(return_value=response))
    manager=AsyncMock(); manager.__aenter__.return_value=client
    monkeypatch.setattr('httpx.AsyncClient',lambda **kwargs:manager)
    a.llm.chat_with_json.return_value={'criteria':[{'criterion':'URL','status':'passed','file':'deployment_runtime.json','quote':url}]}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    client.get.assert_awaited_once_with(url)
    assert out['metadata']['business_acceptance']['deployment']['content_matches_workspace']

@pytest.mark.asyncio
async def test_deployment_rejects_other_hosts_before_network(setup,monkeypatch):
    a,plan,task,out=setup
    from config.settings import settings
    monkeypatch.setattr(settings,'PUBLIC_BASE_URL','https://example.test')
    out['metadata'].update({'action':'static_deploy','site_url':'https://attacker.test/c/c1/d123456789abc/'})
    called=AsyncMock()
    monkeypatch.setattr('httpx.AsyncClient',called)
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert not ok and '地址' in reason
    called.assert_not_called()

@pytest.mark.asyncio
async def test_invalid_json_uses_fallback_without_overriding_rejection(setup,monkeypatch):
    a,plan,task,out=setup
    a.llm=SimpleNamespace(base_url='https://opencode.ai/inference/go/openai/v1',api_key='fake',model='primary',chat_with_json=AsyncMock(return_value={'_parse_error':'invalid'}))
    fallback=SimpleNamespace(model='glm-5.3-flash',chat_with_json=AsyncMock(return_value={'criteria':[{'criterion':'visible','status':'passed','file':'browser_runtime.json','quote':'hello world'}]}),close=AsyncMock())
    monkeypatch.setattr('agents.ceo.agent.DeepSeekClient',lambda **kwargs:fallback)
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    assert out['metadata']['business_acceptance']['review_model']=='glm-5.3-flash'
    fallback.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_deployment_rejects_other_client_before_network(setup,monkeypatch):
    a,plan,task,out=setup
    from config.settings import settings
    monkeypatch.setattr(settings,'PUBLIC_BASE_URL','https://example.test')
    out['metadata'].update({'action':'static_deploy','site_url':'https://example.test/c/other/d123456789abc/'})
    called=AsyncMock();monkeypatch.setattr('httpx.AsyncClient',called)
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert not ok and '地址' in reason
    called.assert_not_called()
