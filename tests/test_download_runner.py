"""Tests for DownloadRunner.py: subprocess runs of the whole script, plus in-process calls via a fixture.

The pano CSV rows all use an unsupported source, so every pano is filtered out before any phase runs — the
script exercises its full argument parsing, phase orchestration, and log.csv writing without any network I/O.
"""

import logging
import logging.handlers
import os
import signal
import subprocess
import sys

import pytest

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
def download_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv',
                        ['DownloadRunner.py', 'sidewalk-test.invalid', str(tmp_path / 'import_storage'),
                         '-c', write_pano_csv(tmp_path), '--skip-depth'])
    root = logging.getLogger()
    prior_handlers = list(root.handlers)
    prior_level = root.level
    prior_urllib3_level = logging.getLogger('urllib3').level
    prior_sigterm = signal.getsignal(signal.SIGTERM)
    sys.modules.pop('DownloadRunner', None)
    try:
        import DownloadRunner
        yield DownloadRunner
    finally:
        # The import configures process-wide state (root logger, SIGTERM); undo it so tests stay isolated.
        for handler in list(root.handlers):
            if handler not in prior_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(prior_level)
        logging.getLogger('urllib3').setLevel(prior_urllib3_level)
        signal.signal(signal.SIGTERM, prior_sigterm)
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
