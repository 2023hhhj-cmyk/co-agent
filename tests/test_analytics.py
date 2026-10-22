"""Verify event writing and reporting after analytics directory consolidation."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_event_write_and_report_from_another_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    log = tmp_path / 'events.log'
    env = dict(os.environ, ANALYTICS_LOG_PATH=str(log))
    subprocess.run(
        [sys.executable, '-c',
         'from analytics import emit_event; emit_event("workflow.start", workflow_id="check")'],
        cwd=root, env=env, check=True, capture_output=True, text=True,
    )
    assert json.loads(log.read_text())['workflow_id'] == 'check'
    report = subprocess.run(
        [sys.executable, str(root / 'analytics' / 'report.py')],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    assert 'workflow.start' in report.stdout
    assert str(log) in report.stdout
