"""Tests for DownloadRunner.py: subprocess runs of the whole script, plus in-process calls.

Subprocess tests: the unsupported-source pano CSV filters every pano out before any phase runs, so those runs
exercise argument parsing, phase orchestration, and log.csv writing with no network I/O. The gsv-source CSVs
feed the budget tests: those runs stub the per-pano download (via a driver script for subprocess runs) and cap
the depth phase at 0 requests, so they count what the image phase actually downloads — still no network I/O.

In-process tests: since #52.1 the module imports inertly (argv, filesystem, logging, and signal setup all live
in main()), so tests import it normally and either call main() with a controlled argv — after replacing
DownloadRunner.download_pano, bound at its import by `from downloaders import ...` — or call the phase
functions directly with plain arguments.
"""

import ast
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time

import requests

import downloaders
import DownloadRunner

import pytest

from conftest import posix_only

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
    # A directory where pano_id_log.csv belongs crashes the image phase on its first ledger open - after the
    # xml-stub fields are known, before any image or depth fields exist. (A malformed ledger no longer
    # crashes: the row-tolerant reader skips bad lines, see test_damaged_ledger_rows_are_skipped_not_fatal.)
    (storage / 'pano_id_log.csv').mkdir()

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
# run_name='__main__' so the script's `if __name__ == '__main__'` guard fires - this driver exists to
# exercise true script-style execution.
runpy.run_path(%(runner)r, run_name='__main__')
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
    """Calling DownloadRunner.main() (or configure_logging directly) mutates process-wide state - root logger
    handlers, urllib3's level, the SIGTERM handler; snapshot and restore it around every test so the
    in-process calls below can't leak into each other or into the rest of the suite."""
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


def call_main(monkeypatch, tmp_path, csv_rows, *extra_args):
    """Run DownloadRunner.main() in-process and return (storage path, per-pano call log).

    DownloadRunner.download_pano - the name its image loop calls, bound at its import by
    `from downloaders import ...` - is replaced with a recording stub first, so the real
    fetch -> filter -> run_scraper_and_log_results orchestration runs with zero network I/O.
    """
    csv_path = tmp_path / 'panos.csv'
    csv_path.write_text(CSV_HEADER + csv_rows)
    storage = tmp_path / 'storage'
    calls = []
    monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))
    monkeypatch.chdir(tmp_path)  # keep any stray relative paths inside this test's tmp dir
    DownloadRunner.main(['sidewalk-test.invalid', str(storage), '-c', str(csv_path), '--skip-depth',
                         *extra_args])
    return storage, calls


def gsv_pano_infos():
    return [{'pano_id': p, 'source': 'gsv'} for p in GSV_PANO_IDS]


def test_exhausted_budget_stops_download_panorama_images_before_the_first_pano(monkeypatch, tmp_path):
    image_storage = tmp_path / 'image_storage'
    image_storage.mkdir()
    calls = []
    monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))

    result = DownloadRunner.download_panorama_images(str(image_storage), gsv_pano_infos(),
                                                     run_start_monotonic=time.monotonic() - 600,
                                                     max_runtime_minutes=5.0)

    assert calls == [], "an exhausted budget must break the loop before any download"
    assert result == (0, 0, 0, 0, 0)


def test_no_budget_lets_download_panorama_images_process_every_pano(monkeypatch, tmp_path):
    """max_runtime_minutes=None means unlimited, however stale the start time is."""
    image_storage = tmp_path / 'image_storage'
    image_storage.mkdir()
    calls = []
    monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))

    result = DownloadRunner.download_panorama_images(str(image_storage), gsv_pano_infos(),
                                                     run_start_monotonic=time.monotonic() - 600,
                                                     max_runtime_minutes=None)

    # Set comparison: the loop shuffles what it attempts, so only coverage is deterministic (see
    # TestFailedPanosDoNotMonopoliseTheQueue for why).
    assert sorted(calls) == sorted(GSV_PANO_IDS)
    assert result == (3, 0, 0, 0, 3)


def test_max_runtime_flag_reaches_the_image_download_loop(monkeypatch, tmp_path, capsys):
    """--max-runtime 0 must stop the image phase before any pano downloads.

    This drives the real call site in run_scraper_and_log_results: a conflict resolution that drops
    max_runtime_minutes there (e.g. passes None) downloads all three panos and fails here.
    """
    storage, calls = call_main(monkeypatch, tmp_path, GSV_CSV_ROWS, '--max-runtime', '0')

    assert calls == []
    assert 'IMAGEDOWNLOAD: Max runtime' in capsys.readouterr().out
    with open(storage / 'pano_id_log.csv') as f:
        assert f.read().strip() == 'pano_id,downloaded', "no pano may be attempted or logged"


def test_without_max_runtime_every_supported_pano_is_downloaded(monkeypatch, tmp_path):
    storage, calls = call_main(monkeypatch, tmp_path, GSV_CSV_ROWS)

    assert sorted(calls) == sorted(GSV_PANO_IDS)
    with open(storage / 'pano_id_log.csv') as f:
        lines = f.read().strip().splitlines()
    # The ledger is written in attempt order, which the loop shuffles; the header's position is not.
    assert lines[0] == 'pano_id,downloaded'
    assert sorted(lines[1:]) == sorted('%s,1' % p for p in GSV_PANO_IDS)


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
# In-process tests. Since #52.1 the module imports inertly, so these call the extracted functions directly
# with plain arguments; _isolate_process_state (autouse) undoes the process-wide state that calling main()
# or configure_logging() configures.
# ---------------------------------------------------------------------------------------------------------

def test_depth_crash_keeps_the_image_phases_real_counts(tmp_path, monkeypatch):
    """misaugstad's review concern on #49: a depth-phase crash must not discard what the image phase downloaded.

    The subprocess crash tests can't cover this - their filtered-out panos make every completed count 0, which
    is indistinguishable from fabricated zeros. Here the image phase reports real nonzero counts.
    """
    storage = tmp_path / 'crash_storage'
    storage.mkdir()
    monkeypatch.setattr(DownloadRunner, 'download_panorama_images', lambda *a, **k: (3, 1, 2, 4, 10))

    def depth_boom(*args, **kwargs):
        raise RuntimeError('depth phase exploded')
    monkeypatch.setattr(DownloadRunner.gsv, 'download_depth_maps', depth_boom)

    panos = [{'pano_id': 'testPanoIdAAAAAAAAAAAA', 'source': 'gsv'}]
    with pytest.raises(RuntimeError, match='depth phase exploded'):
        DownloadRunner.run_scraper_and_log_results(str(storage), panos, panos, skip_depth=False)

    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[6:11] == ['3', '1', '2', '4', '10'], "the image phase's real counts must survive"
    assert fields[11] != ''  # image duration was recorded too
    assert fields[12:] == [''] * 6  # depth and total never finished - blank, not fabricated


def test_an_overwide_log_row_errors_instead_of_silently_widening(tmp_path):
    """Blank-padding computes 18 - len(fields); a future 19th field must fail loudly, not no-op the padding."""
    with pytest.raises(AssertionError):
        DownloadRunner.write_log_csv_row(str(tmp_path), ['x'] * 19)


def test_log_row_write_failure_dumps_the_row_to_stderr(tmp_path, capsys):
    """If appending to log.csv itself fails (unmounted store), the counts must survive somewhere cron can mail."""
    with pytest.raises(OSError):
        DownloadRunner.write_log_csv_row(str(tmp_path / 'gone' / 'unmounted'), ['2026-08-06 01:00:00', 1, 2])
    assert '2026-08-06 01:00:00,1,2' in capsys.readouterr().err


def test_urllib3_is_quieted_and_scrape_log_rotates(tmp_path):
    """DEBUG-level urllib3 chatter means one synchronous sshfs write per HTTP request and unbounded growth."""
    DownloadRunner.configure_logging(str(tmp_path / 'scrape.log'))

    assert logging.getLogger('urllib3').getEffectiveLevel() == logging.WARNING
    rotating = [h for h in logging.getLogger().handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 10 * 1024 * 1024
    assert rotating[0].backupCount == 3


def test_sigterm_is_translated_to_systemexit_so_the_evidence_row_still_lands(tmp_path, monkeypatch):
    """docker stop sends SIGTERM; CPython's default dies without running finally blocks, losing the row (#49).

    The handler is installed by main() - not by importing the module (test_import_is_side_effect_free pins
    the other side of that seam) - so run a harmless all-filtered mini-scrape to get it installed.
    """
    monkeypatch.chdir(tmp_path)
    DownloadRunner.main(['sidewalk-test.invalid', str(tmp_path / 'storage'), '-c', write_pano_csv(tmp_path),
                         '--skip-depth'])

    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler), "DownloadRunner.main() must install a SIGTERM handler"
    with pytest.raises(SystemExit) as excinfo:
        handler(signal.SIGTERM, None)
    assert excinfo.value.code == 143  # the conventional 128+15, what a signal death reports anyway


# ---------------------------------------------------------------------------------------------------------
# The #52 seams: importing the module is inert; argv handling lives in main(); the fetch-and-scrape
# orchestration lives in run(), callable with plain arguments.
# ---------------------------------------------------------------------------------------------------------

def _fresh_import(monkeypatch, argv):
    """Import DownloadRunner from scratch under a controlled argv and return the module."""
    monkeypatch.setattr(sys, 'argv', argv)
    monkeypatch.delitem(sys.modules, 'DownloadRunner', raising=False)
    import DownloadRunner as module
    return module


def test_import_is_side_effect_free(tmp_path, monkeypatch):
    """Importing DownloadRunner must not read argv, touch the filesystem, or mutate process-wide state -
    all of that belongs to main() (#52.1). On the pre-#52 script this fails at the import itself: argparse
    runs at module scope, sees pytest's argv, and exits 2."""
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    urllib3_before = logging.getLogger('urllib3').level
    sigterm_before = signal.getsignal(signal.SIGTERM)

    _fresh_import(monkeypatch, ['pytest'])

    assert list(root.handlers) == handlers_before, "import must not add root-logger handlers"
    assert logging.getLogger('urllib3').level == urllib3_before
    assert signal.getsignal(signal.SIGTERM) is sigterm_before, "import must not install signal handlers"
    assert list(tmp_path.iterdir()) == [], "import must not create directories or log files"


def test_main_with_bad_argv_exits_2(tmp_path, monkeypatch):
    """argparse's error contract survives the main() extraction, exercised in-process."""
    monkeypatch.chdir(tmp_path)
    module = _fresh_import(monkeypatch, ['pytest'])

    for argv in ([], ['host-only']):
        with pytest.raises(SystemExit) as excinfo:
            module.main(argv)
        assert excinfo.value.code == 2


def test_run_writes_evidence_row_when_fetch_raises(tmp_path, monkeypatch):
    """The #49 evidence path at the new run() seam: a pano-list fetch crash must leave a blank-padded
    18-field log.csv row whose real timestamp shows a run started and produced nothing. In-process and
    deterministic - unlike the subprocess variant, which relies on .invalid DNS failing through the whole
    retry stack."""
    monkeypatch.chdir(tmp_path)
    module = _fresh_import(monkeypatch, ['pytest'])
    storage = tmp_path / 'storage'
    storage.mkdir()

    def fetch_boom(sidewalk_server_fqdn):
        raise RuntimeError('webserver down')

    monkeypatch.setattr(module, 'fetch_pano_ids_from_webserver', fetch_boom)

    with pytest.raises(RuntimeError, match='webserver down'):
        module.run('sidewalk-test.invalid', str(storage))

    fields = last_log_fields(storage)
    assert len(fields) == 18
    assert fields[0] != ''  # a real timestamp: evidence the run started
    assert fields[1:] == [''] * 17  # no phase ran - all blank, not fake zeros


# --- Retry semantics and source ordering (#41, #40) -----------------------------------------------------------


def failing_download_pano(calls, error):
    def fake_download_pano(storage_path, pano_info):
        calls.append(pano_info['pano_id'])
        raise error
    return fake_download_pano


class TestRetrySemantics:
    """#41: transient failures (exceptions) must not be ledgered - the depth ledger's semantics - while
    permanent verdicts (DownloadResult.failure: the source has nothing for this pano) stay terminal."""

    def test_transient_failure_is_not_ledgered_and_retries_next_run(self, monkeypatch, tmp_path):
        storage = tmp_path / 'storage'
        storage.mkdir()
        attempts = []
        monkeypatch.setattr(DownloadRunner, 'download_pano',
                            failing_download_pano(attempts, requests.ConnectionError('mid-download blip')))

        result = DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        assert result == (0, 0, 1, 0, 1), "the failure still counts in THIS run's totals"
        with open(storage / 'pano_id_log.csv') as f:
            assert f.read().strip() == 'pano_id,downloaded', "a transient failure must leave no ledger row"

        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(attempts))
        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        assert attempts == [GSV_PANO_IDS[0], GSV_PANO_IDS[0]], "the next run must re-attempt it"
        with open(storage / 'pano_id_log.csv') as f:
            assert f.read().strip().splitlines()[1:] == ['%s,1' % GSV_PANO_IDS[0]]

    def test_permanent_failure_writes_zero_row_and_is_never_reattempted(self, monkeypatch, tmp_path):
        """DownloadResult.failure is the downloader's verdict on the PANO itself (no imagery at either zoom,
        undeterminable dims) - permanent, ledgered, terminal, exactly as today. This pin keeps the transient
        carve-out from widening."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        attempts = []

        def no_imagery(storage_path, pano_info):
            attempts.append(pano_info['pano_id'])
            return downloaders.DownloadResult.failure

        monkeypatch.setattr(DownloadRunner, 'download_pano', no_imagery)
        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        with open(storage / 'pano_id_log.csv') as f:
            assert f.read().strip().splitlines()[1:] == ['%s,0' % GSV_PANO_IDS[0]]

        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(attempts))
        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        assert attempts == [GSV_PANO_IDS[0]], "a ledgered 0-row is terminal"

    def test_preexisting_downloaded_zero_rows_stay_terminal(self, monkeypatch, tmp_path):
        """THE back-compat constraint: production stores hold years of downloaded=0 rows (permanent
        no-imagery mixed with old transient failures). Nothing may make that backlog retryable - it would
        be re-attempted against --max-runtime on every nightly run, fleet-wide. Deleting the 0-rows stays
        the manual force-retry lever."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        (storage / 'pano_id_log.csv').write_text('pano_id,downloaded\n%s,0\n' % GSV_PANO_IDS[0])
        attempts = []
        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(attempts))

        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        assert attempts == []


class TestFallbackResolutionReachesTheLog:
    """log.csv column 8, which README documents as "downloaded, but at a fallback resolution".

    `fallback_success` was defined in the enum and threaded through this loop's counters, but no downloader
    ever returned it - so the column has been a hard 0 for every run ever recorded, and
    log_analyzer/analyze.py sums that zero into daily_success. gsv.download_single_pano now returns it when
    the stitch had to be upscaled to reach the pano's reported dims; these pin the loop's half of that.
    """

    def fallback_download_pano(self, attempts):
        def fake(storage_path, pano_info):
            attempts.append(pano_info['pano_id'])
            return downloaders.DownloadResult.fallback_success
        return fake

    def test_a_fallback_is_counted_in_its_own_column_not_as_a_plain_success(self, monkeypatch, tmp_path):
        storage = tmp_path / 'storage'
        storage.mkdir()
        monkeypatch.setattr(DownloadRunner, 'download_pano', self.fallback_download_pano([]))

        success, fallback, fail, skipped, total = DownloadRunner.download_panorama_images(
            str(storage), gsv_pano_infos()[:1])

        assert (success, fallback, fail, skipped, total) == (0, 1, 0, 0, 1)

    def test_a_fallback_is_ledgered_downloaded_1_and_never_reattempted(self, monkeypatch, tmp_path):
        """It is real imagery on disk, just less of it - so it is terminal like any other success. Ledgering
        it 0 would re-download it against --max-runtime every night, forever."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        attempts = []
        monkeypatch.setattr(DownloadRunner, 'download_pano', self.fallback_download_pano(attempts))
        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        with open(storage / 'pano_id_log.csv') as f:
            assert f.read().strip().splitlines()[1:] == ['%s,1' % GSV_PANO_IDS[0]]

        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(attempts))
        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos()[:1])

        assert attempts == [GSV_PANO_IDS[0]], "a ledgered fallback must not be re-attempted"


class TestSourceOrdering:
    """#40: grouping by source put every GSV pano ahead of every Mapillary one, so a city whose GSV backlog
    exceeds --max-runtime starved Mapillary indefinitely - zero progress, and invisibly, since unattempted
    panos leave no ledger trace."""

    def mixed_panos(self):
        return [{'pano_id': 'gsvPanoIdAAAAAAAAAAAAA', 'source': 'gsv'},
                {'pano_id': '111111111111111', 'source': 'mapillary'},
                {'pano_id': 'gsvPanoIdBBBBBBBBBBBBB', 'source': 'gsv'},
                {'pano_id': '222222222222222', 'source': 'mapillary'}]

    def test_filter_preserves_server_order(self, monkeypatch):
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token')
        panos = self.mixed_panos() + [{'pano_id': 'testPanoIdOtherAAAAAAA', 'source': 'bing'}]

        kept = DownloadRunner.filter_supported_sources(panos)

        assert kept == self.mixed_panos()

    def test_filter_without_token_still_drops_mapillary_with_one_warning(self, monkeypatch, capsys):
        monkeypatch.delenv(downloaders.mapillary.TOKEN_ENV_VAR, raising=False)

        kept = DownloadRunner.filter_supported_sources(self.mixed_panos())

        assert [p['pano_id'] for p in kept] == ['gsvPanoIdAAAAAAAAAAAAA', 'gsvPanoIdBBBBBBBBBBBBB']
        out = capsys.readouterr().out
        assert out.count('WARNING') == 1
        assert '2 Mapillary panos skipped' in out

    def test_unsupported_source_warning_still_prints_a_count(self, monkeypatch, capsys):
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token')
        panos = self.mixed_panos() + [{'pano_id': 'testPanoIdOtherAAAAAAA', 'source': 'bing'},
                                      {'pano_id': 'testPanoIdOtherBBBBBBB', 'source': 'bing'}]

        DownloadRunner.filter_supported_sources(panos)

        assert "2 panos with unsupported source 'bing' skipped" in capsys.readouterr().out

    def test_budget_exhaustion_does_not_starve_mapillary(self, monkeypatch, tmp_path):
        """End to end with a fake monotonic clock: 4 interleaved panos, one simulated minute each, and a
        1.5-minute budget that admits exactly 2 attempts - one of them must be Mapillary. Pre-#40 the
        grouped list attempted [gsv, gsv] and Mapillary made zero progress.

        The download loop's shuffle is neutralised here so what's under test is the FILTER's ordering, which
        is what #40 is about; the shuffle has its own tests in TestFailedPanosDoNotMonopoliseTheQueue.
        """
        monkeypatch.setenv(downloaders.mapillary.TOKEN_ENV_VAR, 'test-token')
        monkeypatch.setattr(DownloadRunner.random, 'shuffle', lambda seq: None)
        storage = tmp_path / 'storage'
        storage.mkdir()
        clock = [0.0]
        monkeypatch.setattr(DownloadRunner.time, 'monotonic', lambda: clock[0])
        attempted = []

        def minute_per_pano(storage_path, pano_info):
            attempted.append((pano_info['pano_id'], pano_info['source']))
            clock[0] += 60.0
            return downloaders.DownloadResult.success

        monkeypatch.setattr(DownloadRunner, 'download_pano', minute_per_pano)

        panos = DownloadRunner.filter_supported_sources(self.mixed_panos())
        DownloadRunner.download_panorama_images(str(storage), panos, run_start_monotonic=0.0,
                                               max_runtime_minutes=1.5)

        assert len(attempted) == 2
        assert 'mapillary' in {source for _, source in attempted}, \
            "an interleaved corpus must make Mapillary progress under a budget"


class TestFailedPanosDoNotMonopoliseTheQueue:
    """Ledgering every attempt used to guarantee the frontier advanced. Since #41 a transient failure leaves
    no row, so it keeps its place in the server's ordering forever - and a stable iteration order would
    re-attempt the same failing head block first every night, spending --max-runtime before reaching new
    work. That is #40's starvation bug through a different door, so the loop shuffles what it attempts (the
    depth phase's fix, gsv.download_depth_maps)."""

    def test_only_unledgered_candidates_are_shuffled(self, monkeypatch, tmp_path):
        """The shuffle must see exactly the panos this run could attempt - not the ledgered ones, which
        would put the cost of shuffling on a fully-backfilled multi-million-pano corpus."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        (storage / 'pano_id_log.csv').write_text('pano_id,downloaded\n%s,1\n' % GSV_PANO_IDS[0])
        shuffled = []
        monkeypatch.setattr(DownloadRunner.random, 'shuffle', lambda seq: shuffled.append(list(seq)))
        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano([]))

        DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos())

        assert len(shuffled) == 1
        assert [p['pano_id'] for p in shuffled[0]] == GSV_PANO_IDS[1:]

    def test_an_always_failing_head_block_cannot_stall_the_backlog(self, monkeypatch, tmp_path):
        """Three panos that always fail transiently sit ahead of five healthy ones, and --max-runtime admits
        three attempts a night. Without the shuffle the same three are retried first every run and the
        healthy five are never reached - verified: four runs, zero progress. A rotating stand-in for
        random.shuffle keeps this deterministic; any order-varying shuffle has the same effect."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        panos = ([{'pano_id': 'BAD%019d' % i, 'source': 'gsv'} for i in range(3)]
                 + [{'pano_id': 'OK%020d' % i, 'source': 'gsv'} for i in range(5)])
        run = [0]

        def rotate(seq):
            seq[:] = seq[run[0] % len(seq):] + seq[:run[0] % len(seq)]

        monkeypatch.setattr(DownloadRunner.random, 'shuffle', rotate)
        clock = [0.0]
        monkeypatch.setattr(DownloadRunner.time, 'monotonic', lambda: clock[0])

        def minute_per_pano(storage_path, pano_info):
            clock[0] += 60.0
            if pano_info['pano_id'].startswith('BAD'):
                raise requests.ConnectionError('transient blip')
            return downloaders.DownloadResult.success

        monkeypatch.setattr(DownloadRunner, 'download_pano', minute_per_pano)

        for run[0] in range(6):
            clock[0] = 0.0
            DownloadRunner.download_panorama_images(str(storage), panos, run_start_monotonic=0.0,
                                                    max_runtime_minutes=3.0)

        ledgered, _, _, _ = DownloadRunner.progress_check(str(storage / 'pano_id_log.csv'))
        assert len([p for p in ledgered if p.startswith('OK')]) == 5, \
            "every healthy pano must eventually be reached past a permanently failing head block"
        assert not [p for p in ledgered if p.startswith('BAD')], \
            "the failing panos still must not be ledgered - they stay retryable (#41)"


# --- Numeric pano ids and ledger hygiene (#46, #55) -----------------------------------------------------------

NUMERIC_PANO_IDS = ['123456789012345', '987654321098765']
NUMERIC_CSV_ROWS = ''.join('%s,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n' % p for p in NUMERIC_PANO_IDS)


class TestNumericPanoIds:
    """#46: all-digit (Mapillary-style) pano ids. pandas infers int64 for an all-numeric column, so without a
    dtype pin the ids arrive as ints - crashing every pano_id[:2] shard slice - and the ledger's two reads
    (one dtype=str, one not) disagree on membership, so the whole corpus is re-attempted every run while the
    ledger file is fully rewritten once per pano (#55's real-world trigger). source=gsv on purpose: the dtype
    path is identical for every source, and gsv avoids Mapillary token plumbing."""

    def test_fetch_pano_ids_csv_returns_string_ids(self, tmp_path):
        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text(CSV_HEADER + NUMERIC_CSV_ROWS)

        records = DownloadRunner.fetch_pano_ids_csv(str(csv_path))

        assert [record['pano_id'] for record in records] == NUMERIC_PANO_IDS
        assert all(isinstance(record['pano_id'], str) for record in records)

    def test_fetch_pano_ids_csv_drops_tutorial_and_empty_rows(self, tmp_path):
        """Parity with the webserver path's filter - hand-made CSVs are exactly where junk rows appear."""
        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text(CSV_HEADER
                            + 'tutorial,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n'
                            + ',16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n'
                            + 'testPanoIdRealAAAAAAAA,16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n')

        records = DownloadRunner.fetch_pano_ids_csv(str(csv_path))

        assert [record['pano_id'] for record in records] == ['testPanoIdRealAAAAAAAA']

    def test_metadata_csv_without_a_pano_id_column_fails_loudly(self, tmp_path):
        """drop_duplicates(subset=['pano_id']) used to raise KeyError on a header typo. Normalising by
        .get('pano_id') would instead read every row as blank and filter the file away - a run that
        downloads nothing and exits 0. -c exists for hand-made CSVs, so this has to stay loud."""
        csv_path = tmp_path / 'panos.csv'
        csv_path.write_text('panoid,source\ntestPanoIdRealAAAAAAAA,gsv\n')

        with pytest.raises(ValueError, match='no .pano_id. column'):
            DownloadRunner.fetch_pano_ids_csv(str(csv_path))

    def test_normalize_pano_records_coerces_filters_and_dedupes(self):
        records = [{'pano_id': 123, 'source': 'mapillary'},
                   {'pano_id': 'abc', 'source': 'gsv'},
                   {'pano_id': '123', 'source': 'mapillary'},   # duplicate once coerced
                   {'pano_id': 'tutorial'},
                   {'pano_id': ''},
                   {'pano_id': None},
                   {'pano_id': float('nan')}]

        kept = DownloadRunner._normalize_pano_records(records)

        assert [record['pano_id'] for record in kept] == ['123', 'abc']
        assert all(isinstance(record['pano_id'], str) for record in kept)

    def test_numeric_ids_reach_the_downloader_as_strings(self, monkeypatch, tmp_path):
        """The network-free stand-in for the production crash: the shard slice pano_id[:2] in every
        downloader raises TypeError on an int. The recording stub replaces the code that would slice, so the
        pin here is the boundary type itself."""
        _, calls = call_main(monkeypatch, tmp_path, NUMERIC_CSV_ROWS)

        assert sorted(calls) == sorted(NUMERIC_PANO_IDS)
        assert all(isinstance(pano_id, str) for pano_id in calls)

    def test_numeric_id_ledger_round_trip_skips_on_second_run(self, monkeypatch, tmp_path):
        """The core #46 discriminator: a second run over the same numeric-ID corpus must skip every ledgered
        pano and leave the ledger file byte-for-byte alone. Pre-fix, the str-set/int mismatch re-attempted
        every pano AND took the whole-file rewrite branch - O(n) per pano, and a truncate-in-place of the
        only image ledger (#55)."""
        storage, _ = call_main(monkeypatch, tmp_path, NUMERIC_CSV_ROWS)
        ledger_path = storage / 'pano_id_log.csv'
        ledger_before = ledger_path.read_bytes()
        calls = []
        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))

        DownloadRunner.download_panorama_images(
            str(storage), DownloadRunner.fetch_pano_ids_csv(str(tmp_path / 'panos.csv')))

        assert calls == [], "the second run must skip every ledgered pano"
        assert ledger_path.read_bytes() == ledger_before


class TestLedgerHygiene:
    def test_damaged_ledger_rows_are_skipped_not_fatal(self, monkeypatch, tmp_path):
        """A ledger line torn by a crash mid-append (or stray garbage) must degrade to re-attempting that
        pano - the depth ledger's semantics (gsv._load_depth_log) - not crash every future run with a
        ParserError (#55)."""
        storage = tmp_path / 'storage'
        storage.mkdir()
        (storage / 'pano_id_log.csv').write_text(
            'pano_id,downloaded\n%s,1\ntrunc\na,b,c\n' % GSV_PANO_IDS[0])
        calls = []
        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))

        result = DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos())

        assert sorted(calls) == GSV_PANO_IDS[1:], "the intact row skips; the torn rows are not fatal"
        assert result == (2, 0, 0, 1, 3)

    @posix_only
    def test_ledger_is_created_group_writable(self, monkeypatch, tmp_path):
        """The depth ledger is chmod'd 0o664 on creation so other lab users can append on the shared store;
        the image ledger must match (#55)."""
        storage, _ = call_main(monkeypatch, tmp_path, GSV_CSV_ROWS)

        assert os.stat(storage / 'pano_id_log.csv').st_mode & 0o777 == 0o664

    def test_a_failed_chmod_does_not_take_the_phase_down(self, monkeypatch, tmp_path):
        """Losing the exists()/open() race to another user's run means chmod'ing a file we don't own. The
        ledger is open and writable either way, so a PermissionError there must not end the image phase -
        the same reasoning both downloaders apply to their shard-dir chmod."""
        storage = tmp_path / 'storage'
        storage.mkdir()

        def denied(*args, **kwargs):
            raise PermissionError(1, 'Operation not permitted')

        monkeypatch.setattr(DownloadRunner.os, 'chmod', denied)
        calls = []
        monkeypatch.setattr(DownloadRunner, 'download_pano', recording_download_pano(calls))

        result = DownloadRunner.download_panorama_images(str(storage), gsv_pano_infos())

        assert sorted(calls) == sorted(GSV_PANO_IDS)
        assert result == (3, 0, 0, 0, 3)

    def test_ledger_rows_are_lf_terminated_like_the_pandas_writer_they_replace(self, monkeypatch, tmp_path):
        """Every existing image ledger was written by pandas to_csv, whose lineterminator defaults to
        os.linesep - '\\n' on the Linux scraper boxes. csv.writer's excel default is '\\r\\n', which would mix
        line endings inside one long-lived production file and put a trailing '\\r' on the downloaded column
        for anything grepping it."""
        storage, _ = call_main(monkeypatch, tmp_path, GSV_CSV_ROWS)

        raw = (storage / 'pano_id_log.csv').read_bytes()
        assert b'\r' not in raw
        assert raw.startswith(b'pano_id,downloaded\n')


def test_pano_list_fetch_session_configuration(monkeypatch):
    """Pin the pano-list fetch's HTTP config (#51 review), in-process at the #52.1 seam - the fqdn is now a
    parameter, so no subprocess/runpy probe is needed.

    - timeout (30, 600): the read timeout applies per socket op INCLUDING the wait for the status line, and
      /adminapi/panos plausibly buffers the whole JSON server-side before its first byte on the largest
      cities — a tight read timeout would kill exactly the fetch it is meant to protect.
    - Retry read=0: a time-to-first-byte/read timeout must fail once, not hammer the admin endpoint six
      times; connect failures keep retrying.
    - trust_env off: parity with the http.client path this replaced (no env-proxy routing, no env CA
      overrides) on a fleet cron whose environment we don't control.
    - The retry adapter is mounted on http:// as well, so a redirect hop can't silently lose the policy.
    """
    captured = {}

    class _ProbeStop(Exception):
        """Raised by the stub before anything touches a socket."""

    def capturing_get(self, url, **kwargs):
        retries = {}
        for scheme in ('https', 'http'):
            retry = self.get_adapter(scheme + '://example.com').max_retries
            retries[scheme] = {'total': retry.total, 'connect': retry.connect, 'read': retry.read}
        captured.update(url=url, timeout=kwargs.get('timeout'), trust_env=self.trust_env, retries=retries)
        raise _ProbeStop()

    monkeypatch.setattr(requests.Session, 'get', capturing_get)

    with pytest.raises(_ProbeStop):
        DownloadRunner.fetch_pano_ids_from_webserver('sidewalk-test.invalid')

    assert captured['url'] == 'https://sidewalk-test.invalid/adminapi/panos'
    assert captured['timeout'] == (30, 600)
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
