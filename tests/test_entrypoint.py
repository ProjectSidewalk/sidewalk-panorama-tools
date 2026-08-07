"""Tests for DownloadRunnerDockerEntrypoint.sh: flag forwarding, exit codes, unmount, and docker-stop handling.

python3/sshfs/umount are stubbed on PATH so the entrypoint's invocations are captured instead of executed.
Forwarding is asserted explicitly because the entrypoint once parsed a flag without passing it on, silently
disabling the feature.
"""

import os
import signal
import subprocess
import time

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
    # PYTHON3_HANG makes the stub trap TERM and wait, like the real runner mid-scrape; it appends to
    # EVENTS_FILE so tests can assert the shutdown ordering (runner exits, THEN umount).
    stub.write_text('#!/bin/bash\n'
                    'echo "$@" > "$CAPTURE_FILE"\n'
                    'if [ -n "$PYTHON3_HANG" ]; then\n'
                    '    trap \'echo got-term >> "$EVENTS_FILE"; exit 143\' TERM\n'
                    '    echo started >> "$EVENTS_FILE"\n'
                    '    sleep 30 & wait $!\n'
                    'fi\n'
                    'exit "${PYTHON3_EXIT_CODE:-0}"\n')
    stub.chmod(0o755)
    # sshfs/umount stubs so the remote-mount path is testable without a mount. umount records that it ran,
    # since the entrypoint must unmount even when the runner crashes.
    sshfs = stub_dir / 'sshfs'
    sshfs.write_text('#!/bin/bash\nexit "${SSHFS_EXIT_CODE:-0}"\n')
    sshfs.chmod(0o755)
    umount = stub_dir / 'umount'
    umount.write_text('#!/bin/bash\n'
                      'touch "$UMOUNT_CALLED_FILE"\n'
                      'if [ -n "$EVENTS_FILE" ]; then echo umount >> "$EVENTS_FILE"; fi\n'
                      'exit "${UMOUNT_EXIT_CODE:-0}"\n')
    umount.chmod(0o755)
    umount_called_file = tmp_path / 'umount-called'
    events_file = tmp_path / 'events'

    def stub_env(**extra):
        env = dict(os.environ,
                   PATH=f"{stub_dir}:{os.environ['PATH']}",
                   CAPTURE_FILE=str(capture_file),
                   UMOUNT_CALLED_FILE=str(umount_called_file),
                   EVENTS_FILE=str(events_file))
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run(*args, python3_exit_code=0, sshfs_exit_code=0, umount_exit_code=0):
        env = stub_env(PYTHON3_EXIT_CODE=python3_exit_code,
                       SSHFS_EXIT_CODE=sshfs_exit_code,
                       UMOUNT_EXIT_CODE=umount_exit_code)
        result = subprocess.run(['bash', ENTRYPOINT, *args],
                                cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30)
        forwarded = capture_file.read_text().split() if capture_file.exists() else None
        return result, forwarded

    def start_hanging(*args):
        """Launch the entrypoint with a runner stub that hangs until TERMed, for docker-stop tests."""
        proc = subprocess.Popen(['bash', ENTRYPOINT, *args], cwd=str(tmp_path), env=stub_env(PYTHON3_HANG=1),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return proc

    run.umount_called = umount_called_file.exists
    run.events_file = events_file
    run.start_hanging = start_hanging
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
        # The unmount trap must be installed only after a successful mount: unmounting a path that never
        # mounted would be a second error, and its cleanliness would mask the first.
        assert not run_entrypoint.umount_called()

    def test_umount_failure_turns_a_clean_run_nonzero(self, run_entrypoint):
        # A busy or hung unmount means data may not have been flushed to the store - that must not read to
        # cron-level monitoring as a clean night, and the reason must not be discarded (#49).
        result, forwarded = run_entrypoint('sidewalk-test.invalid', 'user@host:/panos', '2222',
                                           umount_exit_code=3)
        assert forwarded is not None
        assert result.returncode != 0
        assert 'WARNING: umount of /tmp/download_dest failed' in result.stderr

    def test_runner_failure_wins_over_umount_failure(self, run_entrypoint):
        # When both fail, the runner's own exit code is the more diagnostic signal and must be preserved.
        result, _ = run_entrypoint('sidewalk-test.invalid', 'user@host:/panos', '2222',
                                   python3_exit_code=7, umount_exit_code=3)
        assert result.returncode == 7
        assert 'WARNING: umount of /tmp/download_dest failed' in result.stderr

    def test_docker_stop_reaches_the_runner_before_umount(self, run_entrypoint):
        """docker stop TERMs only PID 1. The entrypoint must forward it to the runner - whose own handler
        writes the log.csv evidence row - and umount must wait for the runner to actually exit, instead of
        yanking the store out from under a scrape that is still writing (#49)."""
        proc = run_entrypoint.start_hanging('sidewalk-test.invalid', 'user@host:/panos', '2222')
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                if run_entrypoint.events_file.exists() and 'started' in run_entrypoint.events_file.read_text():
                    break
                time.sleep(0.05)
            else:
                pytest.fail("the runner stub never reported starting")
            proc.send_signal(signal.SIGTERM)
            returncode = proc.wait(timeout=10)
        finally:
            proc.kill()
        assert run_entrypoint.events_file.read_text().split() == ['started', 'got-term', 'umount']
        assert returncode == 143  # the runner's own signal-death code, preserved through the traps
