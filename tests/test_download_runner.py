"""Tests for DownloadRunner.py: subprocess runs of the whole script, plus in-process calls via a fixture.

Subprocess tests: the unsupported-source pano CSV filters every pano out before any phase runs, so those runs
exercise argument parsing, phase orchestration, and log.csv writing with no network I/O. The gsv-source CSVs
feed the budget tests: those runs stub the per-pano download (via a driver script for subprocess runs) and cap
the depth phase at 0 requests, so they count what the image phase actually downloads — still no network I/O.

In-process tests: DownloadRunner is a script whose whole flow runs at import time, so importing it with a
controlled argv — after replacing downloaders.download_pano, which it binds by `from downloaders import ...` —
drives the real CSV → filter → phase call sites network-free and leaves the module in hand for calling
download_panorama_images directly.
"""

import ast
import importlib.util
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time

import downloaders

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(REPO_ROOT, 'DownloadRunner.py')
CSV_HEADER = 'pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'
# The subprocess budget tests' corpus; distinct from GSV_PANO_IDS below, which feeds the in-process ones.
GSV_BUDGET_PANO_IDS = ['testPanoIdGsvAAAAAAAAA', 'testPanoIdGsvBBBBBBBBB']


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


def test_scrape_log_lands_in_storage_not_cwd(tmp_path):
    """A relative scrape.log resolves against the CWD - /app inside Docker - and dies with the container.
    It must live on the pano store next to log.csv, where a failed run's evidence survives (#49)."""
    storage, result = run_downloader(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (storage / 'scrape.log').exists()
    # run_downloader's cwd is tmp_path, so this is where the old relative path would have put it.
    assert not (tmp_path / 'scrape.log').exists()


def test_crash_mid_run_still_writes_a_full_width_log_row(tmp_path):
    """An exception between phases must not leave a short log.csv line the analyzer can't parse (#49).

    The completed phases' fields survive (misaugstad: a failure in phase 3 must not discard what phase 2
    downloaded); the phases that never finished are blank, not fake zeros.
    """
    storage = tmp_path / 'storage'
    storage.mkdir()
    # A pano_id_log.csv without the expected columns crashes the image phase on its first read - after the
    # xml-stub fields are known, before any image or depth fields exist.
    (storage / 'pano_id_log.csv').write_text('wrong,columns\n1,2\n')

    _, result = run_downloader(tmp_path)

    assert result.returncode != 0, "the crash must still fail the run loudly"
    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[0] != ''  # run start timestamp
    assert fields[1:6] == ['0'] * 5  # xml stub completed before the crash
    assert fields[6:] == [''] * 12  # image/depth/total never completed - blank, not fabricated
    # The traceback must land in scrape.log, not just stderr - in Docker, stderr dies with the container.
    # `.exists()` is not enough: the FileHandler opens the file eagerly, so an empty file proves nothing.
    scrape_log = (storage / 'scrape.log').read_text()
    assert 'Traceback' in scrape_log


def test_webserver_fetch_failure_still_leaves_evidence(tmp_path):
    """A server outage - the single most likely nightly failure - crashes before any phase runs (#49).

    It must still fail loudly AND leave both kinds of evidence: the traceback in scrape.log, and a blank-padded
    18-field log.csv row whose real timestamp shows a run started and produced nothing.
    """
    storage = tmp_path / 'storage'
    # No -c flag, so the runner fetches from the webserver; the .invalid TLD guarantees the fetch raises.
    result = subprocess.run(
        [sys.executable, RUNNER, 'sidewalk-test.invalid', str(storage)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)

    assert result.returncode != 0, "a run that scraped nothing must not report success"
    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[0] != ''  # a real timestamp: evidence the run started
    assert fields[1:] == [''] * 17  # no phase ran - all blank, not fake zeros
    assert 'Traceback' in (storage / 'scrape.log').read_text()


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


class TestDepthBudgetMessages:
    """stdout messages for the --min-depth-runtime budget split (cron mails stdout, so these lines are the
    operator's only signal). What the split actually *does* is pinned in TestImageBudgetBehaviour below —
    these runs use the unsupported-source CSV, so both phases see empty pano lists.
    """

    def test_default_makes_no_reservation(self, tmp_path):
        # The fleet plans to drastically lower --max-runtime; a default reservation would silently zero the
        # image phase on every city whose slot is at or under it. Reserving is opt-in.
        storage, result = run_downloader(tmp_path, '--max-runtime', '120')
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout
        assert 'NO images' not in result.stdout

    def test_no_backlog_means_no_reservation(self, tmp_path):
        # No gsv panos in this CSV, so the depth ledger has no unresolved work to reserve for.
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '45')
        assert result.returncode == 0, result.stderr
        assert 'no unresolved depth work' in result.stdout
        assert 'reserved for depth' not in result.stdout

    def test_zero_reservation_is_silent(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '0')
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout

    def test_skip_depth_gives_images_the_whole_budget(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--skip-depth')
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout

    def test_no_max_runtime_means_no_reservation(self, tmp_path):
        storage, result = run_downloader(tmp_path)
        assert result.returncode == 0, result.stderr
        assert 'reserved for depth' not in result.stdout


class TestMinDepthRuntimeValidation:
    """--min-depth-runtime inputs that would otherwise silently no-op or zero the image phase (#43 review):
    -5 and nan silently made no reservation, inf silently zeroed the image phase. Reject them at parse time,
    and tell an operator who typed the flag in a combination where it cannot apply."""

    @pytest.mark.parametrize('bad', ['-5', 'nan', 'inf', '-inf', 'abc'])
    def test_rejects_negative_nan_and_inf(self, tmp_path, bad):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', bad)
        assert result.returncode == 2
        assert '--min-depth-runtime' in result.stderr

    def test_warns_when_given_without_max_runtime(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--min-depth-runtime', '45')
        assert result.returncode == 0, result.stderr
        assert 'WARNING: --min-depth-runtime has no effect without --max-runtime' in result.stdout

    def test_warns_when_given_with_skip_depth(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '45',
                                         '--skip-depth')
        assert result.returncode == 0, result.stderr
        assert 'WARNING: --min-depth-runtime has no effect with --skip-depth' in result.stdout

    def test_effective_combination_does_not_warn(self, tmp_path):
        storage, result = run_downloader(tmp_path, '--max-runtime', '120', '--min-depth-runtime', '45')
        assert result.returncode == 0, result.stderr
        assert 'has no effect' not in result.stdout


def write_gsv_csv(tmp_path):
    """Two labelled GSV panos — a supported source, so they reach the image phase's budget guard."""
    csv_path = tmp_path / 'gsv_panos.csv'
    csv_path.write_text(CSV_HEADER
                        + '%s,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n' % GSV_BUDGET_PANO_IDS[0]
                        + '%s,16384,8192,47.7,-122.4,90.0,0.0,gsv,True\n' % GSV_BUDGET_PANO_IDS[1])
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
        # A zero-image run must be unmistakable in cron mail, not read like ordinary budget exhaustion.
        assert ('WARNING: --min-depth-runtime (5) >= --max-runtime (5); NO images will be downloaded this run'
                in result.stdout)

    def test_zero_reservation_downloads_every_pano(self, tmp_path):
        storage, result, downloaded = run_downloader_with_fake_network(
            tmp_path, '--max-runtime', '5', '--min-depth-runtime', '0')
        assert result.returncode == 0, result.stderr
        assert sorted(downloaded) == sorted(GSV_BUDGET_PANO_IDS)

    def test_default_reserves_nothing(self, tmp_path):
        # Same run as above but with --min-depth-runtime left at its default, which must be 0: the fleet plans
        # to drastically lower --max-runtime, and a default reservation would zero the image phase fleet-wide.
        storage, result, downloaded = run_downloader_with_fake_network(tmp_path, '--max-runtime', '5')
        assert result.returncode == 0, result.stderr
        assert sorted(downloaded) == sorted(GSV_BUDGET_PANO_IDS)

    def test_fully_resolved_depth_ledger_frees_the_whole_budget_for_images(self, tmp_path):
        # The reservation exists to protect a depth *backlog*. Once every pano is resolved in the ledger the
        # depth phase returns in milliseconds, so reserving would burn image throughput for nothing — the image
        # phase must get the full budget even with --min-depth-runtime set.
        storage = tmp_path / 'storage'
        storage.mkdir()
        (storage / 'depth_log.csv').write_text(
            'pano_id,status\n%s,saved\n%s,unavailable\n' % (GSV_BUDGET_PANO_IDS[0], GSV_BUDGET_PANO_IDS[1]))

        storage, result, downloaded = run_downloader_with_fake_network(
            tmp_path, '--max-runtime', '5', '--min-depth-runtime', '5')

        assert result.returncode == 0, result.stderr
        assert sorted(downloaded) == sorted(GSV_BUDGET_PANO_IDS)
        assert 'no unresolved depth work' in result.stdout
        assert 'NO images' not in result.stdout

    def test_backlog_applies_the_reservation_and_announces_it(self, tmp_path):
        # Nothing in the ledger, so both gsv panos are a depth backlog: the reservation must be taken.
        storage, result, downloaded = run_downloader_with_fake_network(
            tmp_path, '--max-runtime', '120', '--min-depth-runtime', '45')
        assert result.returncode == 0, result.stderr
        # The image phase still has 75 minutes — plenty for two stubbed panos.
        assert sorted(downloaded) == sorted(GSV_BUDGET_PANO_IDS)
        assert 'image phase capped at 75.0 min' in result.stdout


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


@pytest.fixture(autouse=True)
def _isolate_process_state():
    """Importing DownloadRunner configures process-wide state (root logger handlers, urllib3's level, the
    SIGTERM handler); snapshot and restore it around every test so the in-process imports below can't leak
    into each other or into the rest of the suite."""
    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    prior_level = root.level
    prior_urllib3_level = logging.getLogger('urllib3').level
    prior_sigterm = signal.getsignal(signal.SIGTERM)
    yield
    for handler in list(root.handlers):
        if handler not in prior_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(prior_level)
    logging.getLogger('urllib3').setLevel(prior_urllib3_level)
    signal.signal(signal.SIGTERM, prior_sigterm)


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
    monkeypatch.chdir(tmp_path)  # keep any stray relative paths inside this test's tmp dir
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
                                             run_start_monotonic=time.monotonic() - 600,
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
                                             run_start_monotonic=time.monotonic() - 600,
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


def test_broken_scrape_log_falls_back_to_stderr_and_the_run_survives(tmp_path):
    """An unopenable scrape.log must not kill the whole scrape (#49): the log file is evidence, not cargo."""
    storage = tmp_path / 'storage'
    storage.mkdir()
    (storage / 'scrape.log').mkdir()  # a directory is unopenable as a file on every OS

    storage, result = run_downloader(tmp_path)

    assert result.returncode == 0, result.stderr
    assert 'logging to stderr' in result.stderr  # one loud warning, then the run proceeds
    assert len(last_log_fields(storage)) == 18


# ---------------------------------------------------------------------------------------------------------
# In-process tests. DownloadRunner is a script - argparse and the whole run execute at import - so the
# fixture points argv at the all-filtered pano CSV, making the import itself a harmless network-free
# mini-run, then hands the module over for direct calls with monkeypatched collaborators.
# ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def download_runner(tmp_path, monkeypatch, _isolate_process_state):
    """Import DownloadRunner in-process; _isolate_process_state undoes the process-wide state the import
    configures (root logger, urllib3 level, SIGTERM) after the test."""
    monkeypatch.setattr(sys, 'argv',
                        ['DownloadRunner.py', 'sidewalk-test.invalid', str(tmp_path / 'import_storage'),
                         '-c', write_pano_csv(tmp_path), '--skip-depth'])
    sys.modules.pop('DownloadRunner', None)
    try:
        import DownloadRunner
        yield DownloadRunner
    finally:
        sys.modules.pop('DownloadRunner', None)


def test_depth_crash_keeps_the_image_phases_real_counts(download_runner, tmp_path, monkeypatch):
    """misaugstad's review concern on #49: a depth-phase crash must not discard what the image phase downloaded.

    The subprocess crash tests can't cover this - their filtered-out panos make every completed count 0, which
    is indistinguishable from fabricated zeros. Here the image phase reports real nonzero counts.
    """
    storage = tmp_path / 'crash_storage'
    storage.mkdir()
    monkeypatch.setattr(download_runner, 'storage_location', str(storage))
    monkeypatch.setattr(download_runner, 'download_panorama_images', lambda *a, **k: (3, 1, 2, 4, 10))

    def depth_boom(*args, **kwargs):
        raise RuntimeError('depth phase exploded')
    monkeypatch.setattr(download_runner.gsv, 'download_depth_maps', depth_boom)

    panos = [{'pano_id': 'testPanoIdAAAAAAAAAAAA', 'source': 'gsv'}]
    with pytest.raises(RuntimeError, match='depth phase exploded'):
        download_runner.run_scraper_and_log_results(panos, panos, skip_depth=False)

    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[6:11] == ['3', '1', '2', '4', '10'], "the image phase's real counts must survive"
    assert fields[11] != ''  # image duration was recorded too
    assert fields[12:] == [''] * 6  # depth and total never finished - blank, not fabricated


def test_an_overwide_log_row_errors_instead_of_silently_widening(download_runner):
    """Blank-padding computes 18 - len(fields); a future 19th field must fail loudly, not no-op the padding."""
    with pytest.raises(AssertionError):
        download_runner.write_log_csv_row(['x'] * 19)


def test_log_row_write_failure_dumps_the_row_to_stderr(download_runner, tmp_path, monkeypatch, capsys):
    """If appending to log.csv itself fails (unmounted store), the counts must survive somewhere cron can mail."""
    monkeypatch.setattr(download_runner, 'storage_location', str(tmp_path / 'gone' / 'unmounted'))
    with pytest.raises(OSError):
        download_runner.write_log_csv_row(['2026-08-06 01:00:00', 1, 2])
    assert '2026-08-06 01:00:00,1,2' in capsys.readouterr().err


def test_urllib3_is_quieted_and_scrape_log_rotates(download_runner):
    """DEBUG-level urllib3 chatter means one synchronous sshfs write per HTTP request and unbounded growth."""
    assert logging.getLogger('urllib3').getEffectiveLevel() == logging.WARNING
    rotating = [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 10 * 1024 * 1024
    assert rotating[0].backupCount == 3


def test_sigterm_is_translated_to_systemexit_so_the_evidence_row_still_lands(download_runner):
    """docker stop sends SIGTERM; CPython's default dies without running finally blocks, losing the row (#49)."""
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "DownloadRunner must install a SIGTERM handler"
    with pytest.raises(SystemExit) as excinfo:
        handler(signal.SIGTERM, None)
    assert excinfo.value.code == 143  # the conventional 128+15, what a signal death reports anyway


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


def test_runtime_budget_arguments_are_passed_by_keyword():
    """#62 and #63 rewrite the same budget-threading call sites. Keywords turn that known merge collision
    into a loud conflict/NameError instead of silently slotting a datetime into the monotonic slot, where it
    only detonates when --max-runtime is set — i.e. in the nightly cron, never in this suite (#51 review)."""
    with open(RUNNER, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    budget_calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if name in ('download_panorama_images', 'download_depth_maps'):
                budget_calls[name] = node
    assert sorted(budget_calls) == ['download_depth_maps', 'download_panorama_images']

    for name, call in budget_calls.items():
        assert len(call.args) == 2, '%s: only storage and the pano list may be positional' % name
        keywords = {kw.arg for kw in call.keywords}
        assert 'run_start_monotonic' in keywords, name
        assert 'max_runtime_minutes' in keywords, name
