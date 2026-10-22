"""Security boundary checks without Docker, model calls or real credentials."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from security.sandbox import SandboxedExecutor, SandboxResult
from tools.code.opencode import OpenCodeTool


def _ndjson(*events) -> bytes:
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode()


@pytest.mark.asyncio
async def test_all_cli_calls_default_to_docker_and_missing_docker_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr('security.sandbox.shutil.which', lambda _: None)
    launch = AsyncMock()
    monkeypatch.setattr('security.sandbox.asyncio.create_subprocess_exec', launch)
    tool = OpenCodeTool(base_dir=str(tmp_path), timeout=900)
    assert isinstance(tool.sandbox, SandboxedExecutor)
    assert tool.sandbox.timeout == 900
    result = await tool.execute('implement', working_dir=str(tmp_path / 'project'))
    assert not result.success
    assert '宿主机' in result.error
    launch.assert_not_called()


def test_project_mount_rejects_parent_sibling_and_symlink_escape(tmp_path):
    base = tmp_path / 'projects'
    base.mkdir()
    project = base / 'demo'
    project.mkdir()
    runner = SandboxedExecutor(base_dir=str(base))
    assert runner._validate_working_dir(str(project)) == str(project)
    with pytest.raises(ValueError):
        runner._validate_working_dir(str(base))
    # Existing sensitive directories outside allowed roots must not be mounted.
    with pytest.raises(ValueError):
        runner._validate_working_dir('/etc')
    (project / 'escape').symlink_to('/etc', target_is_directory=True)
    with pytest.raises(ValueError):
        runner._validate_working_dir(str(project / 'escape'))
    with pytest.raises(ValueError):
        runner._validate_working_dir(str(tmp_path) + '-sibling')


@pytest.mark.asyncio
@pytest.mark.parametrize('is_error', [False, True])
async def test_container_flags_env_scope_json_and_tools(monkeypatch, tmp_path, is_error):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr('security.sandbox.shutil.which', lambda _: '/usr/bin/docker')
    runner = SandboxedExecutor(base_dir=str(tmp_path), allowed_tools=['Read', 'Edit'])
    monkeypatch.setattr(runner, '_model_env', lambda: {'OPENCODE_API_KEY': 'test-token'})
    launched = []
    env_paths = []
    async def launch(*cmd, **kwargs):
        assert cmd[:2] == ('docker', 'run')
        assert '--read-only' in cmd and '--cap-drop' in cmd
        assert cmd[cmd.index('--network') + 1] == 'bridge'
        assert 'no-new-privileges' in cmd
        assert '--pids-limit' in cmd
        assert 'opencode' in cmd and cmd[cmd.index('opencode') + 1] == 'run'
        assert cmd[cmd.index('--format') + 1] == 'json'
        assert cmd[cmd.index('--dir') + 1] == '/workspace'
        assert '--auto' in cmd
        mounts = [cmd[i+1] for i, value in enumerate(cmd) if value == '--mount']
        assert mounts == [f'type=bind,source={project},target=/workspace']
        assert not any('docker.sock' in arg for arg in cmd)
        assert 'test-token' not in ' '.join(cmd)
        env_path = Path(cmd[cmd.index('--env-file') + 1])
        assert env_path.stat().st_mode & 0o777 == 0o600
        assert env_path.read_text() == 'OPENCODE_API_KEY=test-token\n'
        env_paths.append(env_path)
        # prompt is the positional right after "opencode run"
        assert '/workspace' in cmd[cmd.index('opencode') + 2]
        launched.append(cmd)
        process = AsyncMock()
        process.returncode = 0 if not is_error else 1
        if is_error:
            out = _ndjson({'type': 'error', 'error': {'name': 'UnknownError',
                           'data': {'message': 'boom'}}})
        else:
            out = _ndjson({'type': 'text', 'part': {'type': 'text', 'text': 'done'}})
        process.communicate.return_value = (out, b'')
        return process
    monkeypatch.setattr('security.sandbox.asyncio.create_subprocess_exec', launch)
    result = await runner.execute(f'edit {project}/app.py', working_dir=str(project))
    assert result.success is not is_error
    assert len(launched) == 1
    assert all(not p.exists() for p in env_paths)


@pytest.mark.asyncio
async def test_opencode_events_parse_text_and_tokens(tmp_path):
    runner = SandboxedExecutor(base_dir=str(tmp_path))
    raw = _ndjson(
        {'type': 'text', 'part': {'type': 'text', 'text': 'hello '}},
        {'type': 'text', 'part': {'type': 'text', 'text': 'world'}},
        {'type': 'step_finish', 'part': {'type': 'step-finish', 'cost': 0.01,
            'tokens': {'input': 10, 'output': 4, 'cache': {'read': 2, 'write': 1}}}},
    ).decode()
    from security.sandbox import _parse_opencode_events
    text, tokens, error = _parse_opencode_events(raw)
    assert text == 'hello \nworld'
    assert tokens['input_tokens'] == 10 and tokens['output_tokens'] == 4
    assert tokens['cache_read_input_tokens'] == 2 and tokens['cache_creation_input_tokens'] == 1
    assert tokens['total_cost_usd'] == 0.01
    assert error == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [asyncio.TimeoutError, asyncio.CancelledError])
async def test_timeout_and_cancellation_remove_container(monkeypatch, tmp_path, failure):
    project = tmp_path / 'project'
    project.mkdir()
    runner = SandboxedExecutor(base_dir=str(tmp_path))
    monkeypatch.setattr(runner, '_check_docker', lambda: True)
    monkeypatch.setattr(runner, '_model_env', lambda: {'OPENCODE_API_KEY': 'fake'})
    process = AsyncMock()
    process.returncode = None
    process.kill = lambda: None
    process.communicate.side_effect = failure()
    monkeypatch.setattr('security.sandbox.asyncio.create_subprocess_exec', AsyncMock(return_value=process))
    cleanup = AsyncMock()
    monkeypatch.setattr(runner, '_remove_container', cleanup)
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await runner.execute('edit', working_dir=str(project))
    else:
        assert not (await runner.execute('edit', working_dir=str(project))).success
    cleanup.assert_awaited_once()
    assert cleanup.call_args.args[0].startswith('muzhu-oc-')
    process.wait.assert_awaited_once()


def test_model_config_allowlist_and_precedence(monkeypatch, tmp_path):
    monkeypatch.setattr('security.sandbox.Path.home', lambda: tmp_path)
    auth_dir = tmp_path / '.local' / 'share' / 'opencode'
    auth_dir.mkdir(parents=True)
    (auth_dir / 'auth.json').write_text(json.dumps({
        'opencode-go': {'type': 'api', 'key': 'from-auth-json'},
        'other-provider': {'type': 'api', 'key': 'never-used'},
    }))
    from security.sandbox import MODEL_ENV_KEYS, settings
    for key in MODEL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
        if hasattr(settings, key):
            monkeypatch.setattr(settings, key, '')
    # 环境变量优先于本地登录态
    monkeypatch.setenv('OPENCODE_API_KEY', 'from-env')
    runner = SandboxedExecutor(base_dir=str(tmp_path))
    assert runner._model_env() == {'OPENCODE_API_KEY': 'from-env'}
    # 无环境变量时回退到宿主机 opencode 登录态
    monkeypatch.delenv('OPENCODE_API_KEY', raising=False)
    assert runner._model_env() == {'OPENCODE_API_KEY': 'from-auth-json'}
    # 非法换行必须被拒绝
    monkeypatch.setenv('OPENCODE_API_KEY', 'bad\nDATABASE_URL=leak')
    with pytest.raises(ValueError):
        runner._model_env()


@pytest.mark.asyncio
async def test_wrapper_preserves_result_and_usage(monkeypatch, tmp_path):
    runner = SandboxedExecutor(base_dir=str(tmp_path))
    runner.execute = AsyncMock(return_value=SandboxResult(
        True, output='done', tokens={'input_tokens': 12, 'output_tokens': 4},
    ))
    tool = OpenCodeTool(base_dir=str(tmp_path), sandbox=runner)
    result = await tool.execute('edit', working_dir=str(tmp_path / 'project'))
    assert result.success and result.tokens['input_tokens'] == 12
    runner.execute.assert_awaited_once()
