"""Tests for DownloadRunner.py.

Subprocess tests: the pano CSV rows all use an unsupported source, so every pano is filtered out before any
phase runs — the script exercises its full argument parsing, phase orchestration, and log.csv writing without
any network I/O.

In-process tests: DownloadRunner is a script whose whole flow runs at import time, so importing it with a
controlled argv — after replacing downloaders.download_pano, which it binds by `from downloaders import ...` —
drives the real CSV → filter → phase call sites network-free and leaves the module in hand for calling
download_panorama_images directly.
"""

import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta

import downloaders

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


# --- Image-phase runtime budget -------------------------------------------------------------------------------
#
# Every subprocess test above filters its panos out before the image loop, so none of them would notice if the
# --max-runtime budget stopped working. These tests feed SUPPORTED (gsv) panos through the real code with the
# per-pano downloader stubbed, so the budget guard and its call-site plumbing are what's under test.

GSV_PANO_IDS = ['gsvPanoIdAAAAAAAAAAAAA', 'gsvPanoIdBBBBBBBBBBBBB', 'gsvPanoIdCCCCCCCCCCCCC']
GSV_CSV_ROWS = ''.join('%s,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n' % p for p in GSV_PANO_IDS)


def recording_download_pano(calls):
    """A download_pano stand-in that records each pano_id and reports success without touching the network."""
    def fake_download_pano(storage_path, pano_info):
        calls.append(pano_info['pano_id'])
        return downloaders.DownloadResult.success
    return fake_download_pano


def load_runner(monkeypatch, tmp_path, csv_rows, *extra_args):
    """Import DownloadRunner.py in-process and return (module, storage path, per-pano call log).

    downloaders.download_pano is replaced with a recording stub BEFORE the import so DownloadRunner's
    `from downloaders import download_pano` binds the stub; the import-time run then exercises the real
    run_scraper_and_log_results call site with zero network I/O.
    """
    csv_path = tmp_path / 'panos.csv'
    csv_path.write_text(CSV_HEADER + csv_rows)
    storage = tmp_path / 'storage'
    calls = []
    monkeypatch.setattr(downloaders, 'download_pano', recording_download_pano(calls))
    monkeypatch.setattr(sys, 'argv', ['DownloadRunner.py', 'sidewalk-test.invalid', str(storage),
                                      '-c', str(csv_path), '--skip-depth', *extra_args])
    monkeypatch.chdir(tmp_path)  # scrape.log is opened relative to the cwd
    spec = importlib.util.spec_from_file_location('download_runner_under_test', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, storage, calls


def gsv_pano_infos():
    return [{'pano_id': p, 'source': 'gsv'} for p in GSV_PANO_IDS]


def test_exhausted_budget_stops_download_panorama_images_before_the_first_pano(monkeypatch, tmp_path):
    runner, _, _ = load_runner(monkeypatch, tmp_path, '')  # empty CSV: the import-time run is a no-op
    image_storage = tmp_path / 'image_storage'
    image_storage.mkdir()
    calls = []
    monkeypatch.setattr(runner, 'download_pano', recording_download_pano(calls))

    result = runner.download_panorama_images(str(image_storage), gsv_pano_infos(),
                                             run_start_time=datetime.now() - timedelta(minutes=10),
                                             max_runtime_minutes=5.0)

    assert calls == [], "an exhausted budget must break the loop before any download"
    assert result == (0, 0, 0, 0, 0)


def test_no_budget_lets_download_panorama_images_process_every_pano(monkeypatch, tmp_path):
    """max_runtime_minutes=None means unlimited, however stale the start time is."""
    runner, _, _ = load_runner(monkeypatch, tmp_path, '')
    image_storage = tmp_path / 'image_storage'
    image_storage.mkdir()
    calls = []
    monkeypatch.setattr(runner, 'download_pano', recording_download_pano(calls))

    result = runner.download_panorama_images(str(image_storage), gsv_pano_infos(),
                                             run_start_time=datetime.now() - timedelta(minutes=10),
                                             max_runtime_minutes=None)

    assert calls == GSV_PANO_IDS
    assert result == (3, 0, 0, 0, 3)


def test_max_runtime_flag_reaches_the_image_download_loop(monkeypatch, tmp_path, capsys):
    """--max-runtime 0 must stop the image phase before any pano downloads.

    This drives the real call site in run_scraper_and_log_results: a conflict resolution that drops
    max_runtime_minutes there (e.g. passes None) downloads all three panos and fails here.
    """
    _, storage, calls = load_runner(monkeypatch, tmp_path, GSV_CSV_ROWS, '--max-runtime', '0')

    assert calls == []
    assert 'IMAGEDOWNLOAD: Max runtime' in capsys.readouterr().out
    with open(storage / 'pano_id_log.csv') as f:
        assert f.read().strip() == 'pano_id,downloaded', "no pano may be attempted or logged"


def test_without_max_runtime_every_supported_pano_is_downloaded(monkeypatch, tmp_path):
    _, storage, calls = load_runner(monkeypatch, tmp_path, GSV_CSV_ROWS)

    assert calls == GSV_PANO_IDS
    with open(storage / 'pano_id_log.csv') as f:
        assert f.read().strip().splitlines() == ['pano_id,downloaded'] + ['%s,1' % p for p in GSV_PANO_IDS]
