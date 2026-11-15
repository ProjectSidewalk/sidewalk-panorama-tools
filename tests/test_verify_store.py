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


def noisy_baseline_jpeg():
    """A baseline JPEG large enough that half of it is real scan data."""
    import numpy as np
    pixels = (np.random.RandomState(0).rand(256, 512, 3) * 255).astype('uint8')
    buf = io.BytesIO()
    Image.fromarray(pixels).save(buf, 'JPEG', quality=90)
    return buf.getvalue()


NOISY = noisy_baseline_jpeg()
# A preallocating copy that died at half: libjpeg decodes the zero tail as scan data without an error (#242 review).
ZERO_FILLED = NOISY[:len(NOISY) // 2] + b'\x00' * (len(NOISY) - len(NOISY) // 2)
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
    # One stamped row, one two-field row from before #129, one blank stamp: the shapes a real ledger holds.
    (storage / 'pano_id_log.csv').write_text(
        'pano_id,downloaded,fetched_at\naaWHOLE,1,2026-11-01T02:00:00-07:00\nbbCUT,1\nccTRAILER,1,\n',
        newline='')
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

    def test_a_zero_filled_cut_is_incomplete_although_it_decodes(self, tmp_path):
        """libjpeg reads the zeros as scan data; only the missing EOI after the first scan gives it away."""
        path = put(tmp_path, 'aaX', ZERO_FILLED)
        with Image.open(str(path)) as image:
            image.load()                        # the premise: the decode alone accepts it
        assert common.stored_jpeg_verdict(str(path)) == common.STORED_JPEG_INCOMPLETE

    def test_another_format_named_jpg_is_incomplete(self, tmp_path):
        buf = io.BytesIO()
        Image.new('RGB', (8, 8)).save(buf, 'PNG')
        assert common.stored_jpeg_verdict(str(put(tmp_path, 'aaX', buf.getvalue()))) == \
            common.STORED_JPEG_INCOMPLETE

    @pytest.mark.parametrize('chunk_size', [1, 2, 3, 7, 4096])
    def test_the_eoi_scan_finds_a_marker_straddling_a_chunk_edge(self, tmp_path, chunk_size):
        """`previous[-1:] + chunk` is what finds an FF D9 split across two reads; at chunk size 1 every marker is."""
        whole = put(tmp_path, 'aaX', BIG_TRAILER)
        assert common._eoi_after_first_scan(str(whole), chunk_size=chunk_size)
        cut = put(tmp_path, 'aaY', ZERO_FILLED)
        assert not common._eoi_after_first_scan(str(cut), chunk_size=chunk_size)

    def test_a_cut_file_is_refused_without_a_full_decode(self, tmp_path, monkeypatch):
        """The EOI scan runs first, so a file reported every run costs one read, not a raster."""
        path = put(tmp_path, 'aaX', CUT)
        monkeypatch.setattr(common.Image, 'open', lambda *a, **k: pytest.fail('decoded a file with no EOI'))
        assert common.stored_jpeg_verdict(str(path)) == common.STORED_JPEG_INCOMPLETE

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

    def test_a_report_and_a_ledger_on_one_path_is_refused(self, store, tmp_path):
        same = tmp_path / 'one.csv'
        assert run([str(store), '--report', str(same), '--ledger', str(same)], tmp_path) == \
            verify_store.EXIT_REFUSED
        assert not same.exists()

    def test_a_report_naming_a_file_it_did_not_write_is_refused_untouched(self, store, tmp_path):
        before = (store / 'pano_id_log.csv').read_bytes()
        assert run([str(store), '--report', str(store / 'pano_id_log.csv')], tmp_path) == verify_store.EXIT_REFUSED
        assert (store / 'pano_id_log.csv').read_bytes() == before

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

        code = run([str(store), '--ledger', str(ledger), '--max-runtime', '1'], tmp_path)

        assert code == verify_store.EXIT_FOUND, 'a sweep cut short has not shown the store is clean'
        out = capsys.readouterr().out
        assert '3 unreached' in out and 'Incomplete sweep' in out
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
            'pano_id,downloaded,fetched_at', 'aaWHOLE,1,2026-11-01T02:00:00-07:00', 'ccTRAILER,1,'], \
            'the two-field row goes; the stamped and blank-stamp rows stay byte for byte'
        backups = sorted(p.name for p in store.iterdir() if p.name.startswith('pano_id_log.csv.'))
        assert backups[0].startswith('pano_id_log.csv.bak-')
        assert backups[1].startswith('pano_id_log.csv.quarantined-')
        assert (store / backups[1]).read_text() == 'bbCUT,1\n'

    def test_a_kept_original_stays_when_its_copy_is_quarantined(self, store, tmp_path):
        """#185 Part 2: a `<pano_id>.native` is archive imagery, not a derivative of the pano, so quarantine leaves
        it in the store (as it leaves `.depth.npz`); with the switch on, the next scrape rebuilds the copy from it."""
        q = tmp_path / 'quarantine'
        native = common.native_original_path(str(store / 'bb' / 'bbCUT.jpg'))
        with open(native, 'wb') as f:
            f.write(WHOLE)

        assert run([str(store), '--quarantine', str(q)], tmp_path) == verify_store.EXIT_OK

        assert (q / 'bb' / 'bbCUT.jpg').read_bytes() == CUT
        assert open(native, 'rb').read() == WHOLE
        assert not (q / 'bb' / 'bbCUT.native').exists()

    def test_a_destination_that_exists_is_never_overwritten(self, store, tmp_path, capsys):
        q = tmp_path / 'quarantine'
        put(q, 'bbCUT', b'earlier')

        assert run([str(store), '--quarantine', str(q)], tmp_path) == verify_store.EXIT_FOUND

        assert (q / 'bb' / 'bbCUT.jpg').read_bytes() == b'earlier'
        assert (store / 'bb' / 'bbCUT.jpg').read_bytes() == CUT
        assert 'bbCUT,1' in (store / 'pano_id_log.csv').read_text(), 'a pano left in place keeps its row'
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
        assert 'bbCUT,1' in (store / 'pano_id_log.csv').read_text()


class TestTheMoveAndItsEdges:

    def test_a_corrupted_copy_with_the_same_size_and_mtime_is_caught(self, tmp_path, monkeypatch):
        """The compare must read the bytes: copy2 keeps size and mtime, so a shallow compare passes this."""
        import shutil
        source = put(tmp_path / 'src', 'bbCUT', CUT)

        def corrupting_copy(src, dst):
            data = bytearray(open(src, 'rb').read())
            data[10] ^= 0xFF
            with open(dst, 'wb') as f:
                f.write(bytes(data))
            shutil.copystat(src, dst)
        monkeypatch.setattr(verify_store.shutil, 'copyfile', corrupting_copy)

        with pytest.raises(OSError, match='did not match'):
            verify_store._move_verified(str(source), str(tmp_path / 'dst' / 'bbCUT.jpg'))

        assert source.read_bytes() == CUT
        assert os.listdir(tmp_path / 'dst') == [], 'no .part left behind'

    def test_a_read_only_file_can_be_quarantined(self, tmp_path):
        import stat as _stat
        source = put(tmp_path / 'src', 'bbCUT', CUT)
        os.chmod(source, _stat.S_IREAD)
        destination = tmp_path / 'dst' / 'bb' / 'bbCUT.jpg'
        try:
            verify_store._move_verified(str(source), str(destination))
        finally:
            for p in (source, destination):
                if p.exists():
                    os.chmod(p, _stat.S_IREAD | _stat.S_IWRITE)
        assert destination.read_bytes() == CUT and not source.exists()
        assert not os.path.exists(str(destination) + '.part')

    def test_a_failed_copy_leaves_no_part(self, tmp_path, monkeypatch):
        source = put(tmp_path / 'src', 'bbCUT', CUT)

        def failing_copy(src, dst):
            with open(dst, 'wb') as f:
                f.write(b'half')
            raise OSError('disk full')
        monkeypatch.setattr(verify_store.shutil, 'copyfile', failing_copy)

        with pytest.raises(OSError, match='disk full'):
            verify_store._move_verified(str(source), str(tmp_path / 'dst' / 'bbCUT.jpg'))
        assert os.listdir(tmp_path / 'dst') == [] and source.read_bytes() == CUT

    def test_the_never_overwrite_backstop(self, tmp_path):
        """A destination that appeared after quarantine's own check is still never overwritten."""
        source = put(tmp_path / 'src', 'bbCUT', CUT)
        destination = put(tmp_path / 'dst', 'bbCUT', b'earlier')

        with pytest.raises(FileExistsError):
            verify_store._move_verified(str(source), str(destination))
        assert destination.read_bytes() == b'earlier' and source.read_bytes() == CUT
        assert sorted(os.listdir(destination.parent)) == ['bbCUT.jpg']

    def test_a_source_that_cannot_be_deleted_says_so(self, tmp_path, monkeypatch):
        source = put(tmp_path / 'src', 'bbCUT', CUT)
        real_remove = os.remove

        def remove(path):
            if path == str(source):
                raise PermissionError('read-only')
            real_remove(path)
        monkeypatch.setattr(verify_store.os, 'remove', remove)

        with pytest.raises(OSError, match='could not be deleted'):
            verify_store._move_verified(str(source), str(tmp_path / 'dst' / 'bbCUT.jpg'))
        assert (tmp_path / 'dst' / 'bbCUT.jpg').read_bytes() == CUT and source.exists()

    def test_a_file_gone_since_the_sweep_is_left(self, store, tmp_path):
        moved, left = verify_store.quarantine(str(store), {'zzGONE': ('incomplete', 1)}, str(tmp_path / 'q'))
        assert moved == [] and left == {'zzGONE': 'gone since the sweep'}

    def test_a_bad_file_with_no_ledger_row_is_moved_and_the_ledger_untouched(self, store, tmp_path):
        put(store, 'ddLOOSE', CUT)
        before = (store / 'pano_id_log.csv').read_bytes()

        moved, left = verify_store.quarantine(str(store), {'ddLOOSE': ('incomplete', len(CUT))},
                                              str(tmp_path / 'q'))

        assert moved == ['ddLOOSE'] and left == {}
        assert (store / 'pano_id_log.csv').read_bytes() == before
        assert not [p for p in os.listdir(store) if p.startswith('pano_id_log.csv.')], 'no rewrite, no backup'

    def test_a_display_copy_that_cannot_be_moved_is_left_and_exits_1(self, store, tmp_path, monkeypatch, capsys):
        put(store, 'bbCUT', CUT, name='bbCUT.w8192.jpg')
        real = verify_store._move_verified

        def move(source, destination):
            if source.endswith('.w8192.jpg'):
                raise OSError('locked')
            real(source, destination)
        monkeypatch.setattr(verify_store, '_move_verified', move)

        assert run([str(store), '--quarantine', str(tmp_path / 'q')], tmp_path) == verify_store.EXIT_FOUND
        assert 'display copy' in capsys.readouterr().out
        assert (store / 'bb' / 'bbCUT.w8192.jpg').exists() and not (store / 'bb' / 'bbCUT.jpg').exists()
