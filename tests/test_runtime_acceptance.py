import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from agents.ceo.agent import CEOAgent
from config.settings import settings
from models.task import ProjectPlan,Task

@pytest.mark.asyncio
async def test_runtime_acceptance_uses_actual_public_response(monkeypatch):
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='print("hello")'))
    a.llm=SimpleNamespace(chat_with_json=AsyncMock(return_value={'criteria':[{'criterion':'response','status':'passed','file':'deployment_runtime.json','quote':'python-ok'}]}))
    monkeypatch.setattr(settings,'PUBLIC_BASE_URL','https://example.test')
    class Response:
        status_code=200
        headers={"content-type": "application/json; charset=utf-8"}
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def aiter_bytes(self):yield b'{"message":"python-ok"}'
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,method,url):return Response()
    monkeypatch.setattr('httpx.AsyncClient',Client)
    task=Task(title='deploy',description='serve',assigned_to='devops',acceptance='return python-ok')
    plan=ProjectPlan(requirement='test',client_id='c1',container_name='muzhu-customer-c1',volume_name='muzhu-customer-c1')
    out={'content':'ok','metadata':{'action':'runtime_deploy','site_url':'https://example.test/c/c1/d123456789abc/','files':['app.py'],'passed':True}}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    dep=out['metadata']['business_acceptance']['deployment']
    assert dep['status_code']==200 and 'python-ok' in dep['response_body']
    assert dep['content_type']=='application/json; charset=utf-8'

@pytest.mark.asyncio
async def test_source_file_root_location_is_evidence(monkeypatch):
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='print("hello")'))
    a.llm=SimpleNamespace(chat_with_json=AsyncMock(return_value={'criteria':[{'criterion':'root file','status':'passed','file':'artifact_manifest.json','quote':'/workspace/server.py'}]}))
    task=Task(title='source',description='deliver',assigned_to='developer',acceptance='项目根目录存在 server.py')
    plan=ProjectPlan(requirement='test',client_id='c1',container_name='muzhu-customer-c1',volume_name='muzhu-customer-c1')
    out={'content':'ok','metadata':{'files':['server.py'],'passed':True}}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    assert out['metadata']['business_acceptance']['artifact_manifest']['files'][0]['path']=='/workspace/server.py'


@pytest.mark.asyncio
async def test_allocated_tasks_use_user_requirement_for_acceptance():
    agent=object.__new__(CEOAgent)
    agent.template={}
    agent.llm=SimpleNamespace(chat=AsyncMock(return_value=json.dumps({"tasks":[
        {"title":"source","description":"create server.py","assigned_to":"developer","acceptance":"run GET / before deployment"},
        {"title":"deploy","description":"deploy service","assigned_to":"devops","dependencies":["source"],"acceptance":"unrequested browser warning"}
    ]})))
    requirement="交付 server.py 并部署，GET / 返回 ok"
    tasks=await agent._allocate_tasks(requirement,"design")
    assert all(requirement in t.acceptance for t in tasks)
    assert "源码交付阶段" in tasks[0].acceptance and "部署运行阶段" in tasks[1].acceptance
    assert "unrequested browser warning" not in tasks[1].acceptance
    assert "run GET / before deployment" not in tasks[0].acceptance

@pytest.mark.asyncio
async def test_acceptance_rejects_untraceable_criterion():
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='print("ok")'))
    a.llm=SimpleNamespace(chat_with_json=AsyncMock(return_value={'criteria':[{
        'criterion':'invented','status':'passed','file':'server.py','quote':'print',
        'requirement_quote':'不存在的条件'}]}))
    req='交付 server.py'
    task=Task(title='source',description='create',assigned_to='developer',
              acceptance='原始需求（唯一验收依据）：'+req)
    plan=ProjectPlan(requirement=req,client_id='c1',container_name='muzhu-customer-c1')
    out={'metadata':{'files':['server.py']}}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert not ok and '原始需求逐字引用' in reason
    a.llm.chat_with_json.return_value['criteria'][0]['requirement_quote']='server.py'
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    payload=json.loads(a.llm.chat_with_json.call_args.args[0][1]['content'])
    assert payload['stage']=='source' and payload['original_requirement']==req


@pytest.mark.asyncio
async def test_bad_evidence_reference_is_retried_and_recorded():
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='print("ok")'))
    bad={'criteria':[{'criterion':'source','status':'passed','file':'server.py','quote':'invented','requirement_quote':'server.py'}]}
    good={'criteria':[{'criterion':'source','status':'passed','file':'server.py','quote':'print','requirement_quote':'server.py'}]}
    a.llm=SimpleNamespace(chat_with_json=AsyncMock(side_effect=[bad,good]))
    req='交付 server.py'
    task=Task(title='source',description='create',assigned_to='developer',acceptance='原始需求（唯一验收依据）：'+req)
    plan=ProjectPlan(requirement=req,client_id='c1',container_name='muzhu-customer-c1')
    out={'metadata':{'files':['server.py']}}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    assert a.llm.chat_with_json.await_count==2
    assert out['metadata']['business_acceptance']['attempts'][0]['error']=='证据引用不在实际产物中'


@pytest.mark.asyncio
async def test_runtime_condition_deferred_only_in_source_phase():
    a=object.__new__(CEOAgent)
    a.customer_containers=SimpleNamespace(read_file=AsyncMock(return_value='print("ok")'))
    req='交付 server.py 并公开 HTTPS'
    a.llm=SimpleNamespace(chat_with_json=AsyncMock(return_value={'criteria':[
        {'criterion':'source','status':'passed','file':'server.py','quote':'print','requirement_quote':'server.py'},
        {'criterion':'HTTPS','status':'deferred','requirement_quote':'公开 HTTPS'}]}))
    plan=ProjectPlan(requirement=req,client_id='c1',container_name='muzhu-customer-c1')
    task=Task(title='source',description='create',assigned_to='developer',acceptance='原始需求（唯一验收依据）：'+req)
    out={'metadata':{'files':['server.py']}}
    ok,reason=await a._verify_business_acceptance(task,plan,out)
    assert ok,reason
    assert out['metadata']['business_acceptance']['criteria'][1]['status']=='deferred'
    task.assigned_to='devops'
    ok,reason=await a._verify_business_acceptance(task,plan,{'metadata':{'files':['server.py']}})
    assert not ok and '部署阶段不能推迟' in reason
    task.assigned_to='developer'
    a.llm.chat_with_json.return_value['criteria']=a.llm.chat_with_json.return_value['criteria'][1:]
    ok,reason=await a._verify_business_acceptance(task,plan,{'metadata':{'files':['server.py']}})
    assert not ok and '没有实际验收' in reason
