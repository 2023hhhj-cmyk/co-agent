import json
from unittest.mock import AsyncMock
import pytest
from tools.validation.browser import check_html

@pytest.mark.asyncio
async def test_browser_container_is_isolated(monkeypatch):
    launch=AsyncMock()
    proc=AsyncMock(); proc.returncode=0; proc.communicate.return_value=(json.dumps({'loaded':True,'visible_text':'hello'}).encode(),b'')
    cleanup=AsyncMock(); cleanup.wait.return_value=0
    launch.side_effect=[proc,cleanup]
    monkeypatch.setattr('tools.validation.browser.asyncio.create_subprocess_exec',launch)
    result=await check_html('muzhu-customer-c1','index.html')
    args=launch.await_args_list[0].args
    assert args[args.index('--network')+1]=='none' and '--read-only' in args and '--cap-drop' in args
    assert args[args.index('--mount')+1]=='type=volume,source=muzhu-customer-c1,target=/workspace,readonly'
    assert not any('docker.sock' in arg for arg in args)
    assert result['loaded']

@pytest.mark.asyncio
@pytest.mark.parametrize('path',['../index.html','/etc/passwd','.secret.html','nested/../../index.html'])
async def test_browser_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        await check_html('muzhu-customer-c1',path)
