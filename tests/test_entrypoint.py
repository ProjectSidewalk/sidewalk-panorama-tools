"""Tests for DownloadRunnerDockerEntrypoint.sh flag parsing and forwarding.

python3 is stubbed on PATH so the entrypoint's DownloadRunner.py invocation is captured instead of executed.
Forwarding is asserted explicitly because the entrypoint once parsed a flag without passing it on, silently
disabling the feature.
"""

import os
import subprocess

import pytest

from conftest import posix_only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(REPO_ROOT, 'DownloadRunnerDockerEntrypoint.sh')

# Windows' bash mangles the drive-letter path and can't chmod the python3 stub executable.
pytestmark = posix_only


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


@pytest.fixture
def run_entrypoint_sshfs(tmp_path):
    """Run the 3-arg (sshfs-mounted) entrypoint path — the production invocation — with sshfs, umount, and
    python3 all stubbed on PATH."""
    stub_dir = tmp_path / 'bin'
    stub_dir.mkdir()
    capture_file = tmp_path / 'python3-args.txt'
    sshfs_file = tmp_path / 'sshfs-args.txt'
    (stub_dir / 'python3').write_text('#!/bin/bash\necho "$@" > "$CAPTURE_FILE"\n')
    (stub_dir / 'sshfs').write_text('#!/bin/bash\necho "$@" > "$SSHFS_FILE"\n')
    (stub_dir / 'umount').write_text('#!/bin/bash\nexit 0\n')
    for stub in ('python3', 'sshfs', 'umount'):
        (stub_dir / stub).chmod(0o755)

    def run(*args):
        env = dict(os.environ,
                   PATH=f"{stub_dir}:{os.environ['PATH']}",
                   CAPTURE_FILE=str(capture_file),
                   SSHFS_FILE=str(sshfs_file))
        result = subprocess.run(['bash', ENTRYPOINT, *args],
                                cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30)
        forwarded = capture_file.read_text().split() if capture_file.exists() else None
        mounted = sshfs_file.read_text().split() if sshfs_file.exists() else None
        return result, forwarded, mounted

    return run


def test_sshfs_path_forwards_the_budget_flags(run_entrypoint_sshfs):
    """The sshfs invocation is the one production runs; a flag parsed but dropped only from that line would
    pass every 1-arg test here and still silently disable the feature fleet-wide (it has happened before —
    see the entrypoint's own comment)."""
    result, forwarded, mounted = run_entrypoint_sshfs(
        'sidewalk-test.invalid', 'user@host.invalid:/remote/path', '2222',
        '--max-runtime', '5', '--min-depth-runtime', '45')
    assert result.returncode == 0
    assert mounted is not None and 'user@host.invalid:/remote/path' in mounted
    assert forwarded[:3] == ['DownloadRunner.py', 'sidewalk-test.invalid', '/tmp/download_dest']
    assert forwarded[forwarded.index('--max-runtime') + 1] == '5'
    assert forwarded[forwarded.index('--min-depth-runtime') + 1] == '45'


def test_forwards_all_optional_flags(run_entrypoint):
    result, forwarded = run_entrypoint('sidewalk-test.invalid', '--all-panos', '--max-runtime', '5',
                                       '--max-depth-requests', '100')
    assert result.returncode == 0
    assert forwarded[:3] == ['DownloadRunner.py', 'sidewalk-test.invalid', '/tmp/download_dest']
    assert '--all-panos' in forwarded
    assert forwarded[forwarded.index('--max-runtime') + 1] == '5'
    assert forwarded[forwarded.index('--max-depth-requests') + 1] == '100'


def test_forwards_min_depth_runtime(run_entrypoint):
    result, forwarded = run_entrypoint('sidewalk-test.invalid', '--min-depth-runtime', '45')
    assert result.returncode == 0
    assert forwarded[forwarded.index('--min-depth-runtime') + 1] == '45'


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
