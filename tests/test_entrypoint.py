"""Tests for DownloadRunnerDockerEntrypoint.sh flag parsing and forwarding.

python3 is stubbed on PATH so the entrypoint's DownloadRunner.py invocation is captured instead of executed.
Forwarding is asserted explicitly because the entrypoint once parsed a flag without passing it on, silently
disabling the feature.
"""

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO_ROOT, 'DownloadRunnerDockerEntrypoint.sh')


@pytest.fixture
def run_entrypoint(tmp_path):
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    capture_file = tmp_path / 'python3-args.txt'
    stub = stub_dir / 'python3'
    stub.write_text('#!/bin/bash\necho "$@" > "$CAPTURE_FILE"\n')
    stub.chmod(0o755)

    def run(*args):
        env = dict(os.environ,
                   PATH=f"{stub_dir}:{os.environ['PATH']}",
                   CAPTURE_FILE=str(capture_file))
        result = subprocess.run(['bash', ENTRYPOINT, *args],
                                cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30)
        forwarded = capture_file.read_text().split() if capture_file.exists() else None
        return result, forwarded

    return run


def test_forwards_all_optional_flags(run_entrypoint):
    result, forwarded = run_entrypoint('sidewalk-test.invalid', '--all-panos', '--max-runtime', '5',
                                       '--max-depth-requests', '100')
    assert result.returncode == 0
    assert forwarded[:3] == ['DownloadRunner.py', 'sidewalk-test.invalid', '/tmp/download_dest']
    assert '--all-panos' in forwarded
    assert forwarded[forwarded.index('--max-runtime') + 1] == '5'
    assert forwarded[forwarded.index('--max-depth-requests') + 1] == '100'


def test_forwards_skip_depth(run_entrypoint):
    result, forwarded = run_entrypoint('sidewalk-test.invalid', '--skip-depth')
    assert result.returncode == 0
    assert '--skip-depth' in forwarded


def test_deprecated_attempt_depth_warns_and_is_not_forwarded(run_entrypoint):
    result, forwarded = run_entrypoint('sidewalk-test.invalid', '--attempt-depth')
    assert result.returncode == 0
    assert '--attempt-depth is deprecated' in result.stdout
    assert forwarded is not None, "the runner must still be invoked"
    assert '--attempt-depth' not in forwarded


def test_no_args_prints_usage_without_running(run_entrypoint):
    result, forwarded = run_entrypoint()
    assert 'Usage:' in result.stdout
    assert forwarded is None
