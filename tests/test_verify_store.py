"""verify_store.py (#239): the read-only sweep for stored files that are not whole JPEGs, and its quarantine."""

import hashlib
import io
import os

import pytest
from PIL import Image

import scrape_queue
import verify_store
from downloaders import common


def jpeg_bytes(size=(64, 32), color=(90, 120, 150)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'JPEG', quality=90)
    return buf.getvalue()


WHOLE = jpeg_bytes()
CUT = WHOLE[:len(WHOLE) // 2]                       # a copy that died partway: the header still reads
BIG_TRAILER = WHOLE + b'\x00' * (common.TRAILING_BYTES_WINDOW + 1024)   # whole, but past the EOI window


def put(storage, pano_id, data, name=None):
    shard = storage / pano_id[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / (name or pano_id + '.jpg')
    path.write_bytes(data)
    return path


def tree_digest(root):
    """Every file under root, with its bytes: what 'read-only' means."""
    digest = {}
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, 'rb') as f:
                digest[os.path.relpath(path, root)] = hashlib.sha256(f.read()).hexdigest()
    return digest


@pytest.fixture
def store(tmp_path):
    storage = tmp_path / 'city'
    storage.mkdir()
    put(storage, 'aaWHOLE', WHOLE)
    put(storage, 'bbCUT', CUT)
    put(storage, 'ccTRAILER', BIG_TRAILER)
    (storage / 'pano_id_log.csv').write_text(
        'pano_id,downloaded,fetched_at\naaWHOLE,1,\nbbCUT,1,\nccTRAILER,1,\n', newline='')
    return storage


def run(argv, tmp_path):
    return verify_store.main(argv + ['--lock', str(tmp_path / 'queue.lock')])


class TestTheVerdict:
    """common.stored_jpeg_verdict, which the image phases share."""

    def test_a_whole_file_is_complete(self, tmp_path):
        assert common.stored_jpeg_verdict(str(put(tmp_path, 'aaX', WHOLE))) == common.STORED_JPEG_COMPLETE

    def test_a_cut_file_is_incomplete_on_both_opinions(self, tmp_path):
        """The decode must be load(): open alone reads only the header, which a cut file still has."""
        path = put(tmp_path, 'aaX', CUT)
        assert common.jpeg_dimensions(str(path)) is not None, 'the premise: the header survives the cut'
        assert common.stored_jpeg_verdict(str(path)) == common.STORED_JPEG_INCOMPLETE

    def test_a_long_trailer_fails_the_cheap_check_and_passes_the_decode(self, tmp_path):
        path = put(tmp_path, 'aaX', BIG_TRAILER)
        assert not common.is_complete_jpeg(str(path))
        assert common.stored_jpeg_verdict(str(path)) == common.STORED_JPEG_DECODED

    @pytest.mark.parametrize('data', [b'', b'<html>error</html>', b'\xff\xd8'], ids=['empty', 'html', 'soi only'])
    def test_not_a_jpeg_at_all_is_incomplete(self, tmp_path, data):
        assert common.stored_jpeg_verdict(str(put(tmp_path, 'aaX', data))) == common.STORED_JPEG_INCOMPLETE

    def test_a_missing_file_is_incomplete_not_a_crash(self, tmp_path):
        assert common.stored_jpeg_verdict(str(tmp_path / 'nope.jpg')) == common.STORED_JPEG_INCOMPLETE

    def test_store_sftp_still_exposes_the_same_check(self):
        from downloaders import store_sftp
        assert store_sftp.is_complete_jpeg is common.is_complete_jpeg


class TestReadOnlyByDefault:

    def test_it_finds_only_the_cut_file_and_changes_nothing(self, store, tmp_path, capsys):
        before = tree_digest(store)

        assert run([str(store)], tmp_path) == verify_store.EXIT_FOUND

        assert tree_digest(store) == before
        out = capsys.readouterr().out
        assert '1 whole, 1 whole only on a full decode, 1 NOT whole' in out
        assert 'bbCUT' in out and 'aaWHOLE' not in out.split('not whole:')[-1]

    def test_a_clean_store_exits_0(self, tmp_path):
        storage = tmp_path / 'city'
        put(storage, 'aaWHOLE', WHOLE)
        assert run([str(storage)], tmp_path) == verify_store.EXIT_OK

    def test_the_display_copy_is_not_judged_as_a_pano(self, store, tmp_path, capsys):
        put(store, 'aaWHOLE', CUT, name='aaWHOLE.w8192.jpg')
        run([str(store)], tmp_path)
        assert '1 NOT whole' in capsys.readouterr().out

    def test_the_report_lists_the_cut_file(self, store, tmp_path):
        report = tmp_path / 'report.csv'
        run([str(store), '--report', str(report)], tmp_path)
        assert report.read_text().splitlines() == ['pano_id,verdict,bytes', 'bbCUT,incomplete,%d' % len(CUT)]

    def test_no_storage_directory_is_refused(self, tmp_path):
        assert run([str(tmp_path / 'nope')], tmp_path) == verify_store.EXIT_REFUSED


class TestTheResumeLedger:

    def test_a_second_run_reads_nothing_it_has_judged(self, store, tmp_path, monkeypatch, capsys):
        ledger = tmp_path / 'verify.csv'
        run([str(store), '--ledger', str(ledger)], tmp_path)
        calls = []
        monkeypatch.setattr(common, 'stored_jpeg_verdict', lambda p: calls.append(p))

        assert run([str(store), '--ledger', str(ledger)], tmp_path) == verify_store.EXIT_FOUND, \
            'the cut file is still reported from the ledger'

        assert calls == []
        assert '3 already judged' in capsys.readouterr().out

    def test_a_replaced_file_is_judged_again(self, store, tmp_path):
        ledger = tmp_path / 'verify.csv'
        run([str(store), '--ledger', str(ledger)], tmp_path)
        (store / 'bb' / 'bbCUT.jpg').write_bytes(WHOLE)

        assert run([str(store), '--ledger', str(ledger)], tmp_path) == verify_store.EXIT_OK

    def test_the_budget_leaves_the_rest_unreached(self, store, tmp_path, capsys, monkeypatch):
        clock = iter([0.0] + [600.0] * 10)
        monkeypatch.setattr(verify_store.time, 'monotonic', lambda: next(clock))
        ledger = tmp_path / 'verify.csv'

        run([str(store), '--ledger', str(ledger), '--max-runtime', '1'], tmp_path)

        assert '3 unreached' in capsys.readouterr().out
        assert ledger.read_text().splitlines() == [','.join(verify_store.LEDGER_FIELDS)]

    def test_a_foreign_ledger_is_refused_untouched(self, store, tmp_path):
        before = (store / 'pano_id_log.csv').read_bytes()
        assert run([str(store), '--ledger', str(store / 'pano_id_log.csv')], tmp_path) == verify_store.EXIT_REFUSED
        assert (store / 'pano_id_log.csv').read_bytes() == before

    def test_a_torn_last_row_costs_one_row(self, store, tmp_path):
        ledger = tmp_path / 'verify.csv'
        ledger.write_text('pano_id,size,mtime_ns,verdict\naaWHOLE,12', newline='')
        run([str(store), '--ledger', str(ledger)], tmp_path)
        rows = ledger.read_text().splitlines()
        assert rows[1] == 'aaWHOLE,12' and len(rows) == 5


class TestQuarantine:

    def test_it_removes_the_row_then_moves_the_file(self, store, tmp_path):
        q = tmp_path / 'quarantine'
        put(store, 'bbCUT', CUT, name='bbCUT.w8192.jpg')        # its display copy goes with it
        (store / 'bb' / 'bbCUT.depth.npz').write_bytes(b'PK depth')

        assert run([str(store), '--quarantine', str(q)], tmp_path) == verify_store.EXIT_OK

        assert not (store / 'bb' / 'bbCUT.jpg').exists()
        assert (q / 'bb' / 'bbCUT.jpg').read_bytes() == CUT, 'the bytes exist somewhere, unchanged'
        assert (q / 'bb' / 'bbCUT.w8192.jpg').exists() and not (store / 'bb' / 'bbCUT.w8192.jpg').exists()
        assert (store / 'bb' / 'bbCUT.depth.npz').exists(), 'the depth artifact is not the image'
        assert (store / 'pano_id_log.csv').read_text().splitlines() == [
            'pano_id,downloaded,fetched_at', 'aaWHOLE,1,', 'ccTRAILER,1,']
        backups = sorted(p.name for p in store.iterdir() if p.name.startswith('pano_id_log.csv.'))
        assert backups[0].startswith('pano_id_log.csv.bak-')
        assert backups[1].startswith('pano_id_log.csv.quarantined-')
        assert (store / backups[1]).read_text() == 'bbCUT,1,\n'

    def test_a_destination_that_exists_is_never_overwritten(self, store, tmp_path, capsys):
        q = tmp_path / 'quarantine'
        put(q, 'bbCUT', b'earlier')

        assert run([str(store), '--quarantine', str(q)], tmp_path) == verify_store.EXIT_FOUND

        assert (q / 'bb' / 'bbCUT.jpg').read_bytes() == b'earlier'
        assert (store / 'bb' / 'bbCUT.jpg').read_bytes() == CUT
        assert 'bbCUT,1,' in (store / 'pano_id_log.csv').read_text(), 'a pano left in place keeps its row'
        assert 'destination exists' in capsys.readouterr().out

    def test_a_failed_move_after_the_row_is_gone_leaves_the_file(self, store, tmp_path, monkeypatch):
        """Row first, so an interrupted quarantine leaves an unledgered file - which the image phase refuses
        as not whole (#239) - and never a `1` row with no file."""
        def broken(source, destination):
            raise OSError('disk full')
        monkeypatch.setattr(verify_store, '_move_verified', broken)

        assert run([str(store), '--quarantine', str(tmp_path / 'q')], tmp_path) == verify_store.EXIT_FOUND

        assert (store / 'bb' / 'bbCUT.jpg').read_bytes() == CUT
        assert 'bbCUT' not in (store / 'pano_id_log.csv').read_text()

    def test_a_copy_that_does_not_match_keeps_the_source(self, tmp_path, monkeypatch):
        source = put(tmp_path / 'src', 'bbCUT', CUT)
        monkeypatch.setattr(verify_store.filecmp, 'cmp', lambda *a, **k: False)

        with pytest.raises(OSError):
            verify_store._move_verified(str(source), str(tmp_path / 'dst' / 'bbCUT.jpg'))

        assert source.read_bytes() == CUT
        assert not (tmp_path / 'dst' / 'bbCUT.jpg').exists() and not (tmp_path / 'dst' / 'bbCUT.jpg.part').exists()

    def test_inside_the_store_is_refused(self, store, tmp_path):
        before = tree_digest(store)
        assert run([str(store), '--quarantine', str(store / 'q')], tmp_path) == verify_store.EXIT_REFUSED
        assert tree_digest(store) == before

    def test_the_queue_lock_held_refuses_and_moves_nothing(self, store, tmp_path):
        before = tree_digest(store)
        lock = tmp_path / 'queue.lock'
        with scrape_queue.exclusive_lock(str(lock)):
            code = verify_store.main([str(store), '--quarantine', str(tmp_path / 'q'), '--lock', str(lock)])
        assert code == verify_store.EXIT_REFUSED
        assert tree_digest(store) == before

    def test_a_file_whole_on_the_recheck_is_left(self, store, tmp_path, monkeypatch):
        bad = {'bbCUT': (common.STORED_JPEG_INCOMPLETE, len(CUT))}
        (store / 'bb' / 'bbCUT.jpg').write_bytes(WHOLE)
        moved, left = verify_store.quarantine(str(store), bad, str(tmp_path / 'q'))
        assert moved == [] and 'whole when re-checked' in left['bbCUT']
        assert 'bbCUT,1,' in (store / 'pano_id_log.csv').read_text()
