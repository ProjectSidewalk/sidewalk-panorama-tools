"""Subprocess tests for DownloadRunner.py.

Two pano CSVs are used. The unsupported-source CSV filters every pano out before any phase runs, so those
tests exercise argument parsing, phase orchestration, and log.csv writing with no network I/O. The gsv-source
CSV feeds the budget tests: those runs stub the per-pano download via a driver script (and cap the depth phase
at 0 requests), so they count what the image phase actually downloads — still with no network I/O.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO_ROOT, 'DownloadRunner.py')
CSV_HEADER = 'pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'
GSV_PANO_IDS = ['testPanoIdGsvAAAAAAAAA', 'testPanoIdGsvBBBBBBBBB']


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


class TestDepthBudgetFloor:
    """--min-depth-runtime reserves the tail of --max-runtime for the depth phase (#43).

    Without a reservation, the images-first shared budget lets an image backlog (a mapathon is the #38
    scenario) starve the depth backfill night after night - exactly when the most new panos arrive. All runs
    here use the unsupported-source CSV, so both phases see empty pano lists and nothing touches the network;
    the assertions read the budget-split line the run prints for cron mail.
    """

    def test_default_reserves_an_hour_for_depth(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120')
        assert result.returncode == 0, result.stderr
        assert 'image phase capped at 60.0 min (60.0 reserved for depth)' in result.stdout

    def test_flag_sets_the_reservation(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '45')
        assert result.returncode == 0, result.stderr
        assert 'image phase capped at 75.0 min (45.0 reserved for depth)' in result.stdout

    def test_zero_floor_restores_the_old_behaviour(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '0')
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout

    def test_floor_larger_than_total_clamps_image_budget_to_zero(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '30')
        assert result.returncode == 0, result.stderr
        assert 'image phase capped at 0.0 min (60.0 reserved for depth)' in result.stdout

    def test_skip_depth_gives_images_the_whole_budget(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--skip-depth')
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout

    def test_no_max_runtime_means_no_reservation(self, tmp_path):
        storage, result = run_downloader(tmp_path)
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout


def write_gsv_csv(tmp_path):
    """Two labelled GSV panos — a supported source, so they reach the image phase's budget guard."""
    csv_path = tmp_path / 'gsv_panos.csv'
    csv_path.write_text(CSV_HEADER
                        + '%s,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n' % GSV_PANO_IDS[0]
                        + '%s,16384,8192,47.7,-122.4,90.0,0.0,gsv,True\n' % GSV_PANO_IDS[1])
    return str(csv_path)


FAKE_NETWORK_DRIVER = '''\
"""Test-only driver: run the real DownloadRunner.py with the per-pano image download stubbed.

Records each pano "downloaded" to $FAKE_DOWNLOAD_LOG instead of touching the network, then executes
DownloadRunner.py itself (argparse, phase orchestration, budget arithmetic, log.csv) unmodified via runpy.
"""
import os
import runpy
import sys

sys.path.insert(0, %(repo_root)r)

import downloaders
from downloaders import DownloadResult


def _fake_download_pano(storage_path, pano_info):
    with open(os.environ['FAKE_DOWNLOAD_LOG'], 'a') as f:
        f.write(pano_info['pano_id'] + '\\n')
    return DownloadResult.success


downloaders.download_pano = _fake_download_pano
sys.argv = ['DownloadRunner.py'] + sys.argv[1:]
runpy.run_path(%(runner)r)
''' % {'repo_root': REPO_ROOT, 'runner': RUNNER}


def run_downloader_with_fake_network(tmp_path, *extra_args):
    """Drive the real DownloadRunner.py against the gsv-source CSV, counting image downloads.

    The unsupported-source CSV never reaches the budget guard (every pano is filtered out before the image
    loop), which is how announcement-only tests could pass while the budget split itself was mutated away.
    These runs use supported panos with download_pano stubbed by FAKE_NETWORK_DRIVER, and --max-depth-requests 0
    so the depth phase issues no requests either — the download counts reflect the real budget arithmetic, with
    zero network I/O.
    """
    driver = tmp_path / 'fake_network_driver.py'
    driver.write_text(FAKE_NETWORK_DRIVER)
    downloads_log = tmp_path / 'downloads.txt'
    downloads_log.write_text('')
    storage = tmp_path / 'storage'
    env = dict(os.environ, FAKE_DOWNLOAD_LOG=str(downloads_log))
    result = subprocess.run(
        [sys.executable, str(driver), 'sidewalk-test.invalid', str(storage), '-c', write_gsv_csv(tmp_path),
         '--max-depth-requests', '0', *extra_args],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120, env=env)
    downloaded = downloads_log.read_text().split()
    return storage, result, downloaded


class TestImageBudgetBehaviour:
    """What the image phase actually downloads under a --min-depth-runtime reservation (#43).

    The review demonstrated that pinning only the budget announcement lets feature-deleting mutations pass the
    whole suite; these tests assert on the downloads themselves.
    """

    def test_reservation_that_consumes_the_budget_downloads_no_images(self, tmp_path):
        storage, result, downloaded = run_downloader_with_fake_network(
            tmp_path, '--max-runtime', '5', '--min-depth-runtime', '5')
        assert result.returncode == 0, result.stderr
        assert downloaded == []

    def test_zero_reservation_downloads_every_pano(self, tmp_path):
        storage, result, downloaded = run_downloader_with_fake_network(
            tmp_path, '--max-runtime', '5', '--min-depth-runtime', '0')
        assert result.returncode == 0, result.stderr
        assert sorted(downloaded) == sorted(GSV_PANO_IDS)


def import_download_runner(tmp_path, monkeypatch):
    """Import DownloadRunner as a module so download_panorama_images can be unit-tested directly.

    The script has no main() — argparse and a full run execute at import — so argv is pointed at the
    network-free unsupported-source CSV with --skip-depth first, and the module cache is cleared so every test
    imports (and therefore runs) afresh.
    """
    monkeypatch.setattr(sys, 'argv',
                        ['DownloadRunner.py', 'sidewalk-test.invalid', str(tmp_path / 'import_storage'),
                         '-c', write_pano_csv(tmp_path), '--skip-depth'])
    monkeypatch.chdir(tmp_path)  # the import-time run and the calls below write scrape.log to cwd
    if not logging.getLogger().handlers:
        # Keep download_panorama_images' logging.basicConfig from opening scrape.log for the whole session,
        # which would pin the tmp dir on Windows.
        logging.getLogger().addHandler(logging.NullHandler())
    sys.modules.pop('DownloadRunner', None)
    import DownloadRunner
    return DownloadRunner


class TestDownloadPanoramaImagesBudget:
    """Direct tests of the image phase's budget guard — the review found it had no unit tests at all."""

    def stub_downloads(self, runner, monkeypatch):
        calls = []

        def fake_download_pano(storage_path, pano_info):
            calls.append(pano_info['pano_id'])
            return runner.DownloadResult.success

        monkeypatch.setattr(runner, 'download_pano', fake_download_pano)
        return calls

    def test_zero_budget_downloads_nothing(self, tmp_path, monkeypatch):
        runner = import_download_runner(tmp_path, monkeypatch)
        calls = self.stub_downloads(runner, monkeypatch)
        storage = tmp_path / 'direct_storage'
        storage.mkdir()
        panos = [{'pano_id': p, 'source': 'gsv'} for p in GSV_PANO_IDS]

        result = runner.download_panorama_images(str(storage), panos, run_start_time=datetime.now(),
                                                 max_runtime_minutes=0.0)

        assert calls == []
        assert result == (0, 0, 0, 0, 0)

    def test_no_budget_downloads_every_pano(self, tmp_path, monkeypatch):
        runner = import_download_runner(tmp_path, monkeypatch)
        calls = self.stub_downloads(runner, monkeypatch)
        storage = tmp_path / 'direct_storage'
        storage.mkdir()
        panos = [{'pano_id': p, 'source': 'gsv'} for p in GSV_PANO_IDS]

        result = runner.download_panorama_images(str(storage), panos, run_start_time=datetime.now(),
                                                 max_runtime_minutes=None)

        assert sorted(calls) == sorted(GSV_PANO_IDS)
        assert result == (2, 0, 0, 0, 2)


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
