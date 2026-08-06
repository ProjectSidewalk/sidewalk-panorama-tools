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
    stub.write_text('#!/bin/bash\necho "$@" > "$CAPTURE_FILE"\nexit "${PYTHON3_EXIT_CODE:-0}"\n')
    stub.chmod(0o755)
    # sshfs/umount stubs so the remote-mount path is testable without a mount. umount records that it ran,
    # since the entrypoint must unmount even when the runner crashes.
    sshfs = stub_dir / 'sshfs'
    sshfs.write_text('#!/bin/bash\nexit "${SSHFS_EXIT_CODE:-0}"\n')
    sshfs.chmod(0o755)
    umount = stub_dir / 'umount'
    umount.write_text('#!/bin/bash\ntouch "$UMOUNT_CALLED_FILE"\n')
    umount.chmod(0o755)
    umount_called_file = tmp_path / 'umount-called'

    def run(*args, python3_exit_code=0, sshfs_exit_code=0):
        env = dict(os.environ,
                   PATH=f"{stub_dir}:{os.environ['PATH']}",
                   CAPTURE_FILE=str(capture_file),
                   PYTHON3_EXIT_CODE=str(python3_exit_code),
                   SSHFS_EXIT_CODE=str(sshfs_exit_code),
                   UMOUNT_CALLED_FILE=str(umount_called_file))
        result = subprocess.run(['bash', ENTRYPOINT, *args],
                                cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30)
        forwarded = capture_file.read_text().split() if capture_file.exists() else None
        return result, forwarded

    run.umount_called = umount_called_file.exists
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
    # A wrong invocation from cron must not look like a successful scrape (#49).
    assert result.returncode != 0


class TestExitCodes:
    """The container's exit status is the only signal cron-level monitoring sees; it must tell the truth (#49)."""

    def test_runner_failure_propagates_on_the_local_path(self, run_entrypoint):
        result, _ = run_entrypoint('sidewalk-test.invalid', python3_exit_code=7)
        assert result.returncode == 7

    def test_runner_failure_propagates_on_the_sshfs_path(self, run_entrypoint):
        # Before #49 the trailing `; umount` made the container always exit with umount's status.
        result, forwarded = run_entrypoint('sidewalk-test.invalid', 'user@host:/panos', '2222',
                                           python3_exit_code=7)
        assert forwarded is not None
        assert result.returncode == 7

    def test_umount_runs_even_when_the_runner_crashes(self, run_entrypoint):
        run_entrypoint('sidewalk-test.invalid', 'user@host:/panos', '2222', python3_exit_code=7)
        assert run_entrypoint.umount_called()

    def test_failed_mount_skips_the_runner_and_fails(self, run_entrypoint):
        # sshfs failing used to short-circuit the && silently: no scrape, exit 0.
        result, forwarded = run_entrypoint('sidewalk-test.invalid', 'user@host:/panos', '2222',
                                           sshfs_exit_code=1)
        assert forwarded is None, "the runner must not start against an unmounted store"
        assert result.returncode != 0
