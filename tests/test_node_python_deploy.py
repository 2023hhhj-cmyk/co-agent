import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.devops.agent import DevOpsAgent
from models.message import Message


def make_agent():
    agent = object.__new__(DevOpsAgent)
    agent.agent_id = 'test-agent'
    agent.public_base_url = 'https://example.test'
    agent.monitor = SimpleNamespace(start_monitoring=AsyncMock())
    agent._save_deployment = AsyncMock()
    agent._runtime_ip = AsyncMock(return_value='172.20.0.2')
    agent._add_runtime_location = MagicMock()
    agent._remove_static_location = MagicMock()
    agent._reload_nginx = MagicMock(return_value='成功')
    return agent

@pytest.mark.asyncio
async def test_package_manifest_routes_to_node_build_even_when_declared_dynamic(tmp_path):
    agent=make_agent()
    (tmp_path/'package.json').write_text('{}')
    agent._valid_host_dir=lambda *args:True
    agent._deploy_node_project=AsyncMock(return_value=Message(content='ok',cause_by='deploy_complete'))
    msg=Message(content='deploy',metadata={'client_id':'client','host_dir':str(tmp_path),'upstream_files':[{'path':'package.json'}]})
    result=await agent._deploy(msg)
    assert result.cause_by=='deploy_complete'
    agent._deploy_node_project.assert_awaited_once()

@pytest.mark.asyncio
async def test_node_build_is_networkless_and_publishes_build_output(tmp_path,monkeypatch):
    agent=make_agent(); (tmp_path/'package.json').write_text('{}')
    agent._deploy_static=AsyncMock(return_value=Message(content='ok',cause_by='deploy_complete',metadata={}))
    launched=[]
    async def launch(*args,**kwargs):
        launched.append(args)
        process=AsyncMock();process.returncode=0
        process.communicate.return_value=(b'',b'')
        if args[:2]==('docker','run'):
            mount=args[args.index('--mount')+1];root=mount.split('src=',1)[1].split(',dst=',1)[0]
            (Path(root)/'dist').mkdir();(Path(root)/'dist'/'index.html').write_text('ok')
        return process
    monkeypatch.setattr('agents.devops.agent.asyncio.create_subprocess_exec',launch)
    result=await agent._deploy_node_project(str(tmp_path),'c1','site','c1')
    assert result.metadata['source_kind']=='node_build'
    assert result.metadata['build_evidence']=={'command':'npm run build','exit_code':0,'network':'none','output':'dist/index.html'}
    cmd=launched[0]
    assert cmd[cmd.index('--network')+1]=='none' and '--read-only' in cmd and '--cap-drop' in cmd
    assert agent._deploy_static.await_args.args[0].endswith('/dist')

@pytest.mark.asyncio
async def test_python_rejects_unpinned_or_remote_dependencies(tmp_path,monkeypatch):
    agent=make_agent();(tmp_path/'server.py').write_text('print(1)');(tmp_path/'requirements.txt').write_text('flask>=3.0.0\n')
    original=os.path.realpath
    monkeypatch.setattr('agents.devops.agent.os.path.realpath',lambda p: str(tmp_path) if str(p)=='/var/lib/docker/volumes/muzhu-customer-client/_data' else original(p))
    agent._runtime_command=AsyncMock()
    result=await agent._deploy_python_project(str(tmp_path),'client','service','client')
    assert result.cause_by=='deploy_failed' and '固定版本' in result.content
    agent._runtime_command.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('fail',[False,True])
async def test_python_runtime_is_isolated_and_rolls_back_on_failure(tmp_path,monkeypatch,fail):
    agent=make_agent();(tmp_path/'server.py').write_text('print(1)')
    original=os.path.realpath
    volume='muzhu-customer-c1'
    def realpath(path):
        if str(path)==f'/var/lib/docker/volumes/{volume}/_data':return str(tmp_path)
        return original(path)
    monkeypatch.setattr('agents.devops.agent.os.path.realpath',realpath)
    agent._runtime_command=AsyncMock(return_value='ok')
    agent._runtime_http=AsyncMock(side_effect=RuntimeError('unavailable') if fail else [(200,b'ok'),(200,b'ok')])
    result=await agent._deploy_python_project(str(tmp_path),'c1','service','c1')
    calls=agent._runtime_command.await_args_list
    assert any(c.args[:3]==('network','create','--internal') for c in calls)
    run=next(c.args for c in calls if c.args[0]=='run')
    assert '--read-only' in run and '--cap-drop' in run and 'no-new-privileges' in run
    assert f'type=volume,source={volume},target=/workspace,readonly' in run
    if fail:
        assert result.cause_by=='deploy_failed'
        assert any(c.args[:2]==('rm','-f') for c in calls)
        assert any(c.args[:2]==('network','rm') for c in calls)
        agent._save_deployment.assert_not_awaited()
    else:
        assert result.cause_by=='deploy_complete' and result.metadata['action']=='runtime_deploy'
        agent._save_deployment.assert_awaited_once()
        agent.monitor.start_monitoring.assert_awaited_once()


def test_node_lock_rejects_untrusted_registry_and_file_links(tmp_path):
    import json
    lock=tmp_path/'package-lock.json'
    safe={'lockfileVersion':3,'packages':{'':{},'node_modules/demo':{'resolved':'https://registry.npmjs.org/demo/-/demo-1.0.0.tgz','integrity':'sha512-example'}}}
    lock.write_text(json.dumps(safe)); DevOpsAgent._validate_node_lock(str(lock))
    safe['packages']['node_modules/demo']['resolved']='https://attacker.invalid/demo.tgz'
    lock.write_text(json.dumps(safe))
    with pytest.raises(ValueError): DevOpsAgent._validate_node_lock(str(lock))
    safe['packages']['node_modules/demo']={'link':True,'resolved':'file:../secret'}
    lock.write_text(json.dumps(safe))
    with pytest.raises(ValueError): DevOpsAgent._validate_node_lock(str(lock))

@pytest.mark.asyncio
async def test_python_dependencies_use_wheels_and_separate_networked_installer(tmp_path,monkeypatch):
    agent=make_agent(); (tmp_path/'app.py').write_text('print(1)'); (tmp_path/'requirements.txt').write_text('Flask==3.1.2\n')
    original=os.path.realpath;volume='muzhu-customer-c1'
    monkeypatch.setattr('agents.devops.agent.os.path.realpath',lambda p:str(tmp_path) if str(p)==f'/var/lib/docker/volumes/{volume}/_data' else original(p))
    agent._runtime_command=AsyncMock(return_value='ok');agent._runtime_http=AsyncMock(side_effect=[(200,b'ok'),(200,b'ok')])
    result=await agent._deploy_python_project(str(tmp_path),'c1','service','c1')
    assert result.cause_by=='deploy_complete'
    calls=[c.args for c in agent._runtime_command.await_args_list]
    pip=next(c for c in calls if '--only-binary=:all:' in c)
    assert pip[pip.index('--network')+1]=='bridge' and '--isolated' in pip and '--no-deps' in pip and 'https://pypi.org/simple/' in pip
    assert not any('source='+volume in x for x in pip)
    app=next(c for c in calls if c[0]=='run' and '-d' in c)
    assert app[app.index('--network')+1].startswith('coagent-net-')
    assert any(x.startswith('type=volume,source=coagent-deps-') and x.endswith(',target=/deps,readonly') for x in app)

@pytest.mark.asyncio
async def test_node_rejects_unpinned_or_remote_dependencies(tmp_path,monkeypatch):
    import json
    agent=make_agent();(tmp_path/'package.json').write_text(json.dumps({'dependencies':{'demo':'git+https://bad.invalid/a'}}))
    process=AsyncMock();process.wait.return_value=0
    launch=AsyncMock(return_value=process);monkeypatch.setattr('agents.devops.agent.asyncio.create_subprocess_exec',launch)
    result=await agent._deploy_node_project(str(tmp_path),'c1','service','c1')
    assert result.cause_by=='deploy_failed' and '固定版本' in result.content
    assert all(c.args[:3]==('docker','rm','-f') for c in launch.await_args_list)


@pytest.mark.asyncio
async def test_node_dependencies_require_prevalidated_lock(tmp_path,monkeypatch):
    import json
    agent=make_agent();(tmp_path/'package.json').write_text(json.dumps({'dependencies':{'lodash':'4.17.21'}}))
    proc=AsyncMock();proc.wait.return_value=0
    launch=AsyncMock(return_value=proc);monkeypatch.setattr('agents.devops.agent.asyncio.create_subprocess_exec',launch)
    result=await agent._deploy_node_project(str(tmp_path),'c1','service','c1')
    assert result.cause_by=='deploy_failed' and 'package-lock.json' in result.content
    assert all(c.args[:3]==('docker','rm','-f') for c in launch.await_args_list)

@pytest.mark.asyncio
async def test_stopped_python_deployment_is_restored_without_marking_failed():
    from models.deploy import ServiceStatus
    agent=make_agent()
    deployment='d123456789abc'
    row={'deployment_id':deployment,'service_name':deployment,'client_name':'c1','client_id':'c1',
         'project_dir':'/workspace','kind':'python','container_name':'coagent-app-'+deployment,'port':0,
         'url':'https://example.test/c/c1/'+deployment+'/','status':'stopped','error_message':None,'site_root':''}
    agent.database=SimpleNamespace(fetch=AsyncMock(return_value=[row]))
    agent.docker=SimpleNamespace(is_running=AsyncMock())
    agent.deployed_services={};agent.port_manager=SimpleNamespace(load_allocated=AsyncMock())
    await agent.restore_deployments()
    assert agent.deployed_services[deployment].status==ServiceStatus.STOPPED
    agent.docker.is_running.assert_not_awaited()

@pytest.mark.asyncio
async def test_runtime_stop_and_restart_use_customer_route(monkeypatch):
    import main
    from models.deploy import DeployRecord
    deployment='d123456789abc';client='c1'
    record=DeployRecord(id=deployment,service_name=deployment,client_id=client,client_name=client,client_dir='/workspace',project_type='python',container_name='coagent-app-'+deployment,url='https://example.test/c/c1/'+deployment+'/')
    agent=make_agent();agent.get_deployment=lambda *args:record
    agent._sanitize_name=lambda x:x
    agent.docker=SimpleNamespace(stop=AsyncMock(return_value=True),restart=AsyncMock(return_value=True))
    agent.port_manager=SimpleNamespace(release=AsyncMock())
    agent.monitor=SimpleNamespace(stop_monitoring=AsyncMock(),start_monitoring=AsyncMock())
    agent.refresh_runtime_route=AsyncMock()
    monkeypatch.setattr(main,'agents',{'devops':agent})
    stopped=await main.stop_devops_service(deployment,client)
    assert stopped['status']=='success'
    agent._remove_static_location.assert_called_once_with('c/c1/'+deployment)
    restarted=await main.restart_devops_service(deployment,client)
    assert restarted['status']=='success'
    agent.refresh_runtime_route.assert_awaited_once_with(record)
