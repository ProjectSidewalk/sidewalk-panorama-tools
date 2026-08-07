"""Subprocess tests for DownloadRunner.py.

The pano CSV rows all use an unsupported source, so every pano is filtered out before any phase runs — the
script exercises its full argument parsing, phase orchestration, and log.csv writing without any network I/O.
"""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO_ROOT, 'DownloadRunner.py')
CSV_HEADER = 'pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'


def write_pano_csv(tmp_path):
    csv_path = tmp_path / 'panos.csv'
    csv_path.write_text(CSV_HEADER
                        + 'testPanoIdAAAAAAAAAAAA,16384,8192,47.6,-122.3,180.0,0.0,unsupported,True\n'
                        + 'testPanoIdBBBBBBBBBBBB,16384,8192,47.7,-122.4,90.0,0.0,unsupported,True\n')
    return str(csv_path)


def run_downloader(tmp_path, *extra_args):
    storage = tmp_path / 'storage'
    result = subprocess.run(
        [sys.executable, RUNNER, 'sidewalk-test.invalid', str(storage), '-c', write_pano_csv(tmp_path), *extra_args],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    return storage, result


def last_log_fields(storage):
    with open(storage / 'log.csv') as f:
        return f.read().strip().splitlines()[-1].split(',')


def test_log_csv_keeps_18_positional_fields(tmp_path):
    storage, result = run_downloader(tmp_path)
    assert result.returncode == 0, result.stderr

    fields = last_log_fields(storage)
    assert len(fields) == 18
    # Field 1 is the run timestamp; with every pano filtered out, the xml stub, image, and depth counts are all 0.
    assert fields[1:6] == ['0'] * 5
    assert fields[6:12] == ['0'] * 6
    assert fields[12:17] == ['0'] * 5


def test_skip_depth_writes_zero_depth_columns_and_no_ledger(tmp_path):
    storage, result = run_downloader(tmp_path, '--skip-depth')
    assert result.returncode == 0, result.stderr

    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[12:17] == ['0'] * 5
    assert not (storage / 'depth_log.csv').exists()


def test_deprecated_attempt_depth_flag_warns_but_runs(tmp_path):
    storage, result = run_downloader(tmp_path, '--attempt-depth', '--skip-depth')
    assert result.returncode == 0, result.stderr
    assert '--attempt-depth is deprecated' in result.stdout
    assert len(last_log_fields(storage)) == 18


def test_max_depth_requests_flag_is_accepted(tmp_path):
    storage, result = run_downloader(tmp_path, '--max-depth-requests', '10')
    assert result.returncode == 0, result.stderr
    assert len(last_log_fields(storage)) == 18


def write_mixed_label_csv(tmp_path):
    """Two GSV panos, only one of which carries labels."""
    csv_path = tmp_path / 'mixed.csv'
    csv_path.write_text(CSV_HEADER
                        + 'testPanoIdLabeledAAAAA,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n'
                        + 'testPanoIdUnlabeledBBB,16384,8192,47.7,-122.4,90.0,0.0,gsv,False\n')
    return str(csv_path)


def run_selection_only(tmp_path, *extra_args):
    """Run far enough to print the phase selection, without downloading anything.

    --max-runtime 0 makes the image loop break on its first iteration and --skip-depth keeps the depth phase out,
    so this stays network-free while still exercising the real selection logic.
    """
    storage = tmp_path / 'storage'
    result = subprocess.run(
        [sys.executable, RUNNER, 'sidewalk-test.invalid', str(storage), '-c', write_mixed_label_csv(tmp_path),
         '--max-runtime', '0', '--skip-depth', *extra_args],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_depth_covers_unlabeled_panos_that_the_image_phase_skips(tmp_path):
    """--all-panos gates images only; depth is wanted for the whole corpus (ProjectSidewalk#39)."""
    stdout = run_selection_only(tmp_path)
    assert 'Panos: 2 supported, 1 eligible for image download, 2 GSV panos eligible for depth' in stdout


def test_all_panos_widens_the_image_phase_only(tmp_path):
    stdout = run_selection_only(tmp_path, '--all-panos')
    assert 'Panos: 2 supported, 2 eligible for image download, 2 GSV panos eligible for depth' in stdout


# Runs DownloadRunner.py (via runpy — argparse at module scope rules out an import) with Session.get stubbed
# to capture the HTTP config the pano-list fetch actually uses, reported as JSON on the last stdout line.
# No network I/O: the stub raises SystemExit before anything touches a socket.
FETCH_CONFIG_PROBE = '''\
import json
import os
import runpy
import sys

import requests

runner, storage = sys.argv[1], sys.argv[2]
# Running a script directly puts its directory on sys.path; runpy does not, so add it for `import downloaders`.
sys.path.insert(0, os.path.dirname(os.path.abspath(runner)))
captured = {}


def capturing_get(self, url, **kwargs):
    retries = {}
    for scheme in ('https', 'http'):
        retry = self.get_adapter(scheme + '://example.com').max_retries
        retries[scheme] = {'total': retry.total, 'connect': retry.connect, 'read': retry.read}
    timeout = kwargs.get('timeout')
    captured.update(url=url, timeout=list(timeout) if isinstance(timeout, tuple) else timeout,
                    trust_env=self.trust_env, retries=retries)
    raise SystemExit(0)


requests.Session.get = capturing_get
sys.argv = ['DownloadRunner.py', 'sidewalk-test.invalid', storage]
try:
    runpy.run_path(runner, run_name='__main__')
except SystemExit:
    pass
print(json.dumps(captured))
'''


def test_pano_list_fetch_session_configuration(tmp_path):
    """Pin the pano-list fetch's HTTP config (#51 review).

    - timeout (30, 600): the read timeout applies per socket op INCLUDING the wait for the status line, and
      /adminapi/panos plausibly buffers the whole JSON server-side before its first byte on the largest
      cities — a tight read timeout would kill exactly the fetch it is meant to protect.
    - Retry read=0: a time-to-first-byte/read timeout must fail once, not hammer the admin endpoint six
      times; connect failures keep retrying.
    - trust_env off: parity with the http.client path this replaced (no env-proxy routing, no env CA
      overrides) on a fleet cron whose environment we don't control.
    - The retry adapter is mounted on http:// as well, so a redirect hop can't silently lose the policy.
    """
    probe = tmp_path / 'fetch_config_probe.py'
    probe.write_text(FETCH_CONFIG_PROBE)
    result = subprocess.run(
        [sys.executable, str(probe), RUNNER, str(tmp_path / 'storage')],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.strip().splitlines()[-1])

    assert captured['url'] == 'https://sidewalk-test.invalid/adminapi/panos'
    assert captured['timeout'] == [30, 600]
    assert captured['trust_env'] is False
    for scheme in ('https', 'http'):
        retry = captured['retries'][scheme]
        assert retry['total'] == 5, scheme
        assert retry['connect'] == 5, scheme
        assert retry['read'] == 0, scheme
