"""readmit_photosphere_writeoffs.py: re-admit the photospheres the fleet wrote off before #227 (#223).

Every `CAoS` row on the production store was a two-field downloaded=0 when the 2026-11-05 study read them, 901
of them the AF1Qip form the photo host serves. The ledger has no reason column, so this tool removes a row only
when it is a `0` row whose id decodes to a photo id of a --form (default AF1Qip). No pano list, no network.
"""

import os

import pytest

import readmit_photosphere_writeoffs as readmit
import scrape_queue
from downloaders import gsv

AF1QIP_A = 'CAoSLEFGMVFpcE05ZHg3dFhIa1htX193c1JDUW10UmRKd1J4dDY1Ujk5cDFGYWlu'     # two-field 0: re-admitted
AF1QIP_B = 'CAoSLEFGMVFpcE1ub2YyODJZRzVNY3J0OVJjUHhhSGc5ZXNPNExmTnI0WExqOTBJ'     # three-field 0: re-admitted
AF1QIP_OK = 'CAoSLEFGMVFpcE51ZXBzb1lXeGpraURXNzBGSTZHbnRDUlUzT0prMjN1MlZSMWFu'    # a 1 row: kept
CIHM = 'CAoSF0NJSE0wb2dLRUlDQWdJQ0U0ZXpLOXdF'                                     # a refused form's 0: kept
CIAB = 'CAoSHENJQUJJaEFEeWNLejZoTFNPbWY2MWNzQUJ6RDA.'                             # likewise
UNDECODABLE = 'CAoSnot-a-protobuf'                                                # the prefix alone: kept
GSV = 'ordinaryGsvPanoIdAAAAA'                                                    # not a photosphere: kept

LEDGER = ('pano_id,downloaded\n'
          f'{AF1QIP_A},0\n'
          f'{CIHM},0\n'
          f'{GSV},0\n'
          f'{UNDECODABLE},0\n'
          f'{AF1QIP_OK},1,\n'
          f'{CIAB},0\n'
          'torn,row,with,too,many,fields\n'
          f'{AF1QIP_B},0,2026-11-06 02:14:00.000000-08:00\n')


@pytest.fixture
def store(tmp_path):
    storage = tmp_path / 'chicago-il'
    storage.mkdir()
    (storage / 'pano_id_log.csv').write_bytes(LEDGER.encode())
    return storage


def run(tmp_path, *args):
    return readmit.main([*args, '--lock', str(tmp_path / 'queue.lock')])


def test_the_forms_are_the_downloaders_and_the_default_is_the_served_one():
    assert set(readmit.build_parser().parse_args(['x']).forms or ()) == set()
    assert readmit.is_writeoff_of(f'{AF1QIP_A},0\n', ('AF1Qip',))
    assert not readmit.is_writeoff_of(f'{CIHM},0\n', ('AF1Qip',))
    assert readmit.is_writeoff_of(f'{CIHM},0\n', gsv.PHOTOSPHERE_REFUSED_FORMS)
    assert not readmit.is_writeoff_of(f'{AF1QIP_OK},1,\n', ('AF1Qip',))
    assert not readmit.is_writeoff_of(f'{UNDECODABLE},0\n', gsv.PHOTOSPHERE_FORMS)
    assert not readmit.is_writeoff_of('torn,row,with,too,many,fields\n', gsv.PHOTOSPHERE_FORMS)


def test_a_dry_run_counts_and_writes_nothing(tmp_path, store, capsys):
    before = sorted(os.listdir(store))

    assert run(tmp_path, str(store)) == 0

    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()
    assert sorted(os.listdir(store)) == before
    out = capsys.readouterr().out
    assert 'would re-admit 2 row(s)' in out and 'dry run' in out.lower()


def test_apply_removes_exactly_the_af1qip_zero_rows_and_keeps_both_copies(tmp_path, store, capsys):
    assert run(tmp_path, str(store), '--apply') == 0

    kept = (store / 'pano_id_log.csv').read_text()
    expected = ''.join(line for line in LEDGER.splitlines(keepends=True)
                       if not line.startswith((AF1QIP_A, AF1QIP_B)))
    assert kept == expected, 'every other line, the header, the torn row and the refused forms included'
    backups = [n for n in os.listdir(store) if n.startswith('pano_id_log.csv.bak-')]
    removed = [n for n in os.listdir(store) if n.startswith('pano_id_log.csv.readmitted-')]
    assert len(backups) == 1 and (store / backups[0]).read_bytes() == LEDGER.encode()
    assert len(removed) == 1
    assert (store / removed[0]).read_text().splitlines() == [
        f'{AF1QIP_A},0', f'{AF1QIP_B},0,2026-11-06 02:14:00.000000-08:00']
    assert 're-admitted 2 row(s)' in capsys.readouterr().out


def test_a_named_form_selects_its_rows_instead(tmp_path, store):
    assert run(tmp_path, str(store), '--form', 'CIHM', '--form', 'CIAB', '--apply') == 0

    kept = (store / 'pano_id_log.csv').read_text()
    assert CIHM not in kept and CIAB not in kept
    assert AF1QIP_A in kept and AF1QIP_B in kept


def test_several_storage_dirs_share_one_lock_and_one_report(tmp_path, store, monkeypatch, capsys):
    other = tmp_path / 'knox-oh'
    other.mkdir()
    (other / 'pano_id_log.csv').write_text(f'pano_id,downloaded\n{AF1QIP_OK},0\n')
    taken = []
    real_lock = scrape_queue.exclusive_lock
    monkeypatch.setattr(scrape_queue, 'exclusive_lock', lambda path: taken.append(path) or real_lock(path))

    assert run(tmp_path, str(store), str(other), '--apply') == 0

    assert taken == [str(tmp_path / 'queue.lock')]
    assert AF1QIP_OK not in (other / 'pano_id_log.csv').read_text()
    assert 're-admitted 3 row(s)' in capsys.readouterr().out


def test_the_downloader_then_sees_them_as_unattempted(tmp_path, store):
    import DownloadRunner
    run(tmp_path, str(store), '--apply')

    ids, _total, _ok, _fail = DownloadRunner.progress_check(str(store / 'pano_id_log.csv'))

    assert AF1QIP_A not in ids and AF1QIP_B not in ids
    assert {CIHM, CIAB, GSV, UNDECODABLE, AF1QIP_OK} <= ids


def test_imagery_is_never_touched(tmp_path, store):
    """chicago-il holds five AF1Qip JPEGs left by #223's measurement; the downloader re-registers such a pano
    as skipped. This tool reads no image and writes none."""
    shard = store / AF1QIP_A[:2]
    shard.mkdir()
    jpg = shard / (AF1QIP_A + '.jpg')
    jpg.write_bytes(b'stored imagery')

    run(tmp_path, str(store), '--apply')

    assert jpg.read_bytes() == b'stored imagery'
    assert sorted(os.listdir(shard)) == [AF1QIP_A + '.jpg']


def test_a_second_apply_finds_nothing(tmp_path, store, capsys):
    run(tmp_path, str(store), '--apply')
    after_first = (store / 'pano_id_log.csv').read_bytes()
    capsys.readouterr()

    assert run(tmp_path, str(store), '--apply') == 0

    assert (store / 'pano_id_log.csv').read_bytes() == after_first
    assert 're-admitted 0 row(s)' in capsys.readouterr().out
    assert len([n for n in os.listdir(store) if n.startswith('pano_id_log.csv.bak-')]) == 1


def test_a_missing_ledger_refuses_the_whole_run(tmp_path, store):
    empty = tmp_path / 'nothing-here'
    empty.mkdir()
    assert run(tmp_path, str(store), str(empty), '--apply') == 3
    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()


def test_a_row_appended_after_the_dry_run_read_survives_the_apply(tmp_path, store, monkeypatch):
    appended = 'lateRowPanoAAAAAAAAAAA,1,2026-11-07 02:00:00.000000-08:00\n'
    real_lock = scrape_queue.exclusive_lock

    def lock_after_an_append(path):
        with open(store / 'pano_id_log.csv', 'a', newline='') as f:
            f.write(appended)
        return real_lock(path)

    monkeypatch.setattr(scrape_queue, 'exclusive_lock', lock_after_an_append)

    assert run(tmp_path, str(store), '--apply') == 0

    kept = (store / 'pano_id_log.csv').read_text()
    assert kept.endswith(appended) and AF1QIP_A not in kept


def test_a_held_lock_refuses_and_writes_nothing(tmp_path, store, monkeypatch):
    def held(path):
        raise scrape_queue.QueueLocked('held by pid 1234')

    monkeypatch.setattr(scrape_queue, 'exclusive_lock', held)
    before = sorted(os.listdir(store))

    assert run(tmp_path, str(store), '--apply') == 3

    assert (store / 'pano_id_log.csv').read_bytes() == LEDGER.encode()
    assert sorted(os.listdir(store)) == before


def test_a_dry_run_never_takes_the_lock(tmp_path, store, monkeypatch):
    monkeypatch.setattr(scrape_queue, 'exclusive_lock', lambda path: pytest.fail('a dry run takes no lock'))
    assert run(tmp_path, str(store)) == 0


def test_an_unknown_form_is_a_usage_error(tmp_path, store):
    with pytest.raises(SystemExit) as exited:
        readmit.main([str(store), '--form', 'ZZZZ'])
    assert exited.value.code == 2
