"""readmit_dimless_writeoffs.py: re-admit the GSV panos written off for missing dims before #184.

Before #184 a GSV record with no width/height was ledgered downloaded=0 at zero requests and never retried;
1,349 washington-dc panos went that way on 2026-09-24/25. The ledger has no reason column, so this tool
removes a row only when all three hold: downloaded=0, fetched_at on one of the named dates, and the pano is
dimensionless in the pano list as served now. Dry-run by default; network-free here (the list is a -c CSV).
"""

import os

import pytest

import readmit_dimless_writeoffs as readmit
import scrape_queue

HEADER = 'pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'

DIMLESS_A = 'dimlessPanoAAAAAAAAAAA'     # written off 09-24: re-admitted
DIMLESS_B = 'dimlessPanoBBBBBBBBBBB'     # written off 09-25: re-admitted
DIMLESS_OLD = 'dimlessPanoOLDOLDOLDOL'   # dimensionless, but written off on another date: kept
DIMMED_GONE = 'dimmedPanoGONEGONEGONE'   # 0 row on 09-24 but the list has dims (a retired pano): kept
DIMLESS_OK = 'dimlessPanoOKOKOKOKOKO'    # a 1 row: kept
DIMLESS_TWO = 'dimlessPanoTWOFIELDSAA'   # a two-field 0 row, no date: kept
MAPILLARY = '1234567890'                 # not GSV: kept

LEDGER = ('pano_id,downloaded\n'
          f'{DIMLESS_TWO},0\n'
          f'{DIMLESS_OLD},0,2026-09-17 02:00:00.000000-07:00\n'
          f'{DIMLESS_A},0,2026-09-24 02:10:00.000000-07:00\n'
          f'{DIMMED_GONE},0,2026-09-24 02:11:00.000000-07:00\n'
          f'{DIMLESS_OK},1,2026-09-24 02:12:00.000000-07:00\n'
          f'{MAPILLARY},0,2026-09-24 02:13:00.000000-07:00\n'
          'torn,row,with,too,many,fields\n'
          f'{DIMLESS_B},0,2026-09-25 02:14:00.000000-07:00\n')


def pano_csv(tmp_path):
    rows = [f'{p},,,38.9,-77.0,180.0,0.0,gsv,True\n'
            for p in (DIMLESS_A, DIMLESS_B, DIMLESS_OLD, DIMLESS_OK, DIMLESS_TWO)]
    rows.append(f'{DIMMED_GONE},16384,8192,38.9,-77.0,180.0,0.0,gsv,True\n')
    rows.append(f'{MAPILLARY},,,38.9,-77.0,180.0,0.0,mapillary,True\n')
    path = tmp_path / 'panos.csv'
    path.write_text(HEADER + ''.join(rows))
    return str(path)


@pytest.fixture
def store(tmp_path):
    storage = tmp_path / 'washington-dc'
    storage.mkdir()
    (storage / 'pano_id_log.csv').write_bytes(LEDGER.encode())
    return storage


def run(tmp_path, store, *extra):
    return readmit.main([str(store), '-c', pano_csv(tmp_path), '--date', '2026-09-24', '--date', '2026-09-25',
                         '--lock', str(tmp_path / 'queue.lock'), *extra])


def test_a_dry_run_counts_and_writes_nothing(tmp_path, store, capsys):
    before = sorted(os.listdir(store))

    assert run(tmp_path, store) == 0

    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()
    assert sorted(os.listdir(store)) == before
    out = capsys.readouterr().out
    assert 'would re-admit 2 row(s)' in out
    assert 'dry run' in out.lower()


def test_apply_removes_exactly_the_matching_rows_and_keeps_both_copies(tmp_path, store, capsys):
    assert run(tmp_path, store, '--apply') == 0

    kept = (store / 'pano_id_log.csv').read_text()
    expected = ''.join(line for line in LEDGER.splitlines(keepends=True)
                       if not line.startswith((DIMLESS_A, DIMLESS_B)))
    assert kept == expected, 'every other line, the header and the torn row included, byte for byte'
    backups = [n for n in os.listdir(store) if n.startswith('pano_id_log.csv.bak-')]
    removed = [n for n in os.listdir(store) if n.startswith('pano_id_log.csv.readmitted-')]
    assert len(backups) == 1 and (store / backups[0]).read_bytes() == LEDGER.encode()
    assert len(removed) == 1
    assert (store / removed[0]).read_text().splitlines() == [
        f'{DIMLESS_A},0,2026-09-24 02:10:00.000000-07:00', f'{DIMLESS_B},0,2026-09-25 02:14:00.000000-07:00']
    assert 're-admitted 2 row(s)' in capsys.readouterr().out


def test_the_downloader_then_sees_them_as_unattempted(tmp_path, store):
    import DownloadRunner
    run(tmp_path, store, '--apply')

    ids, _total, _ok, _fail = DownloadRunner.progress_check(str(store / 'pano_id_log.csv'))

    assert DIMLESS_A not in ids and DIMLESS_B not in ids
    assert {DIMLESS_OLD, DIMMED_GONE, DIMLESS_OK, DIMLESS_TWO, MAPILLARY} <= ids


def test_imagery_is_never_touched(tmp_path, store):
    """A re-admitted pano whose .jpg is already there is re-registered as skipped by the downloader; this
    tool reads no image and writes none."""
    shard = store / DIMLESS_A[:2]
    shard.mkdir()
    jpg = shard / (DIMLESS_A + '.jpg')
    jpg.write_bytes(b'stored imagery')

    run(tmp_path, store, '--apply')

    assert jpg.read_bytes() == b'stored imagery'
    assert sorted(os.listdir(shard)) == [DIMLESS_A + '.jpg']


def test_a_second_apply_finds_nothing(tmp_path, store, capsys):
    run(tmp_path, store, '--apply')
    after_first = (store / 'pano_id_log.csv').read_bytes()
    capsys.readouterr()

    assert run(tmp_path, store, '--apply') == 0

    assert (store / 'pano_id_log.csv').read_bytes() == after_first
    assert 're-admitted 0 row(s)' in capsys.readouterr().out
    assert len([n for n in os.listdir(store) if n.startswith('pano_id_log.csv.bak-')]) == 1, \
        'nothing to remove, so no second backup'


def test_it_refuses_while_the_queue_holds_its_lock(tmp_path, store, capsys):
    with scrape_queue.exclusive_lock(str(tmp_path / 'queue.lock')):
        if os.name != 'posix':
            pytest.skip('msvcrt locks are per process on Windows, so a same-process holder is not seen')
        assert run(tmp_path, store, '--apply') == 3

    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()


def test_no_ledger_is_refused(tmp_path):
    empty = tmp_path / 'nothing-here'
    empty.mkdir()
    assert readmit.main([str(empty), '-c', pano_csv(tmp_path), '--date', '2026-09-24',
                         '--lock', str(tmp_path / 'queue.lock')]) == 3


@pytest.mark.parametrize('bad', ['2026-9-24', 'yesterday', '2026-09-24T00'])
def test_a_date_must_be_a_day(tmp_path, store, bad):
    with pytest.raises(SystemExit) as exited:
        readmit.main([str(store), '-c', pano_csv(tmp_path), '--date', bad])
    assert exited.value.code == 2


def test_a_date_is_required(tmp_path, store):
    """No default: the dates are what scope the removal to one known incident."""
    with pytest.raises(SystemExit) as exited:
        readmit.main([str(store), '-c', pano_csv(tmp_path)])
    assert exited.value.code == 2


# --- #216 review N1: the three behaviours a mutant survived ---------------------------------------------------

def test_a_row_appended_after_the_dry_run_read_survives_the_apply(tmp_path, store, monkeypatch):
    """R5: the ledger is re-read under the lock. A nightly run that appended a row between the first read and
    the lock must not lose it to a rewrite from the stale copy."""
    appended = 'lateRowPanoAAAAAAAAAAA,1,2026-10-07 02:00:00.000000-07:00\n'
    real_lock = scrape_queue.exclusive_lock

    def lock_after_an_append(path):
        with open(store / 'pano_id_log.csv', 'a', newline='') as f:
            f.write(appended)
        return real_lock(path)

    monkeypatch.setattr(scrape_queue, 'exclusive_lock', lock_after_an_append)

    assert run(tmp_path, store, '--apply') == 0

    kept = (store / 'pano_id_log.csv').read_text()
    assert kept.endswith(appended)
    assert DIMLESS_A not in kept and DIMLESS_B not in kept


def test_one_missing_dimension_is_enough(tmp_path, store):
    """R8: a record with a width and no height (or the reverse) has no frame either."""
    csv_path = tmp_path / 'one-dim.csv'
    csv_path.write_text(HEADER + f'{DIMLESS_A},16384,,38.9,-77.0,180.0,0.0,gsv,True\n'
                        + f'{DIMLESS_B},,8192,38.9,-77.0,180.0,0.0,gsv,True\n')

    assert readmit.main([str(store), '-c', str(csv_path), '--date', '2026-09-24', '--date', '2026-09-25',
                         '--lock', str(tmp_path / 'queue.lock'), '--apply']) == 0

    kept = (store / 'pano_id_log.csv').read_text()
    assert DIMLESS_A not in kept and DIMLESS_B not in kept


def test_apply_takes_the_queue_lock_at_the_path_given(tmp_path, store, monkeypatch):
    """R6, on every platform: the rewrite happens inside the queue's lock, on --lock's path."""
    taken = []
    real_lock = scrape_queue.exclusive_lock

    def spy(path):
        taken.append(path)
        return real_lock(path)

    monkeypatch.setattr(scrape_queue, 'exclusive_lock', spy)

    run(tmp_path, store, '--apply')

    assert taken == [str(tmp_path / 'queue.lock')]


def test_a_held_lock_refuses_and_writes_nothing(tmp_path, store, monkeypatch):
    def held(path):
        raise scrape_queue.QueueLocked('held by pid 1234')

    monkeypatch.setattr(scrape_queue, 'exclusive_lock', held)
    before = sorted(os.listdir(store))

    assert run(tmp_path, store, '--apply') == 3

    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()
    assert sorted(os.listdir(store)) == before


def test_a_dry_run_never_takes_the_lock(tmp_path, store, monkeypatch):
    monkeypatch.setattr(scrape_queue, 'exclusive_lock', lambda path: pytest.fail('a dry run takes no lock'))
    assert run(tmp_path, store) == 0
