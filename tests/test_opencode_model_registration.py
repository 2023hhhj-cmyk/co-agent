import json
from unittest.mock import AsyncMock
import pytest
from security.sandbox import SandboxedExecutor, settings

@pytest.mark.asyncio
@pytest.mark.parametrize('model,expected', [('opencode-go/deepseek-v4.1-flash',True),('opencode-go/glm-5.3-flash',False)])
async def test_model_registration_is_scoped(monkeypatch,tmp_path,model,expected):
    project=tmp_path/'project'; project.mkdir()
    monkeypatch.setattr(settings,'OPENCODE_MODEL',model)
    runner=SandboxedExecutor(base_dir=str(tmp_path))
    monkeypatch.setattr(runner,'_check_docker',lambda:True)
    monkeypatch.setattr(runner,'_model_env',lambda:{'OPENCODE_API_KEY':'test-key'})
    process=AsyncMock(); process.returncode=0
    process.communicate.return_value=(b'{"type":"text","part":{"text":"OK"}}\n',b'')
    launch=AsyncMock(return_value=process)
    monkeypatch.setattr('security.sandbox.asyncio.create_subprocess_exec',launch)
    result=await runner.execute('hello',working_dir=str(project))
    assert result.success
    args=launch.await_args.args
    found=[arg for arg in args if arg.startswith('OPENCODE_CONFIG_CONTENT=')]
    assert bool(found)==expected
    if expected:
        config=json.loads(found[0].split('=',1)[1])
        assert 'deepseek-v4.1-flash' in config['provider']['opencode-go']['models']
    assert 'test-key' not in ' '.join(args)
