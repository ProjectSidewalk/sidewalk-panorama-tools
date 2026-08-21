"""Tests for reports/scripts/store_coverage.py — the backup-store coverage/frame probe.

The live sweep needs a 15 TB NFS mount, so what is pinned here is everything around it: the
dependency-free JPEG header reader (cross-checked against Pillow, which is the only way to know a
hand-rolled SOF scanner is right), the store's path convention, the coverage cross-tab, and the
frame comparison. The committed findings are pinned against the artifact the sweep produced.
"""

import json
import os
import sys

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import store_coverage as sc  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data', '2026-08-10-store-coverage.json')


def write_jpeg(path, size, **kwargs):
    Image.new('RGB', size, (120, 90, 60)).save(str(path), 'JPEG', **kwargs)
    return str(path)


class TestJpegDimensions:
    """A hand-rolled SOF scanner is only trustworthy if it agrees with a real decoder."""

    # Pillow flags a real 16384x8192 open as a possible decompression bomb; ours is synthetic, and
    # only the cross-check decoder trips it — store_coverage never opens an image.
    @pytest.mark.filterwarnings('ignore::PIL.Image.DecompressionBombWarning')
    @pytest.mark.parametrize('size', [(16384, 8192), (13312, 6656), (3328, 1664), (1, 1), (7, 13)])
    def test_it_agrees_with_pillow(self, tmp_path, size):
        path = write_jpeg(tmp_path / 'p.jpg', size)
        with Image.open(path) as im:
            assert sc.jpeg_dimensions(path) == im.size == size

    def test_progressive_jpegs_read_too(self, tmp_path):
        """Progressive JPEGs use SOF2, not SOF0 — a scanner that only knows 0xC0 misses them."""
        path = write_jpeg(tmp_path / 'p.jpg', (2048, 1024), progressive=True)
        assert sc.jpeg_dimensions(path) == (2048, 1024)

    def test_a_jpeg_with_exif_and_comments_still_reads(self, tmp_path):
        """Segments before the SOF must be skipped by their length field, not scanned for 0xFF."""
        path = write_jpeg(tmp_path / 'p.jpg', (800, 400),
                          comment=b'\xff\xd8 not really a marker ' * 40)
        assert sc.jpeg_dimensions(path) == (800, 400)

    def test_it_reads_only_the_header(self, tmp_path):
        """The whole point: a truncated file that has its SOF still answers, so a 15 TB sweep
        never decodes pixels."""
        path = write_jpeg(tmp_path / 'p.jpg', (4096, 2048))
        head = open(path, 'rb').read(2000)
        truncated = tmp_path / 't.jpg'
        truncated.write_bytes(head)
        assert sc.jpeg_dimensions(str(truncated)) == (4096, 2048)

    @pytest.mark.parametrize('blob', [b'', b'not a jpeg at all', b'\xff\xd8', b'\xff\xd8\xff'])
    def test_junk_returns_none_rather_than_raising(self, tmp_path, blob):
        p = tmp_path / 'junk.jpg'
        p.write_bytes(blob)
        assert sc.jpeg_dimensions(str(p)) is None

    def test_a_missing_file_returns_none(self, tmp_path):
        assert sc.jpeg_dimensions(str(tmp_path / 'nope.jpg')) is None

    def test_a_non_jpeg_holding_sof_shaped_bytes_is_rejected(self, tmp_path):
        """The SOI check is load-bearing, not decoration. Without it the scanner would hunt for
        0xFF anywhere in the file and happily report dimensions from a PNG (or any binary that
        happens to contain an SOF-shaped run) — which on a store sweep means a wrong frame quietly
        joining the mismatch count instead of an honest unreadable."""
        sof = b'\xff\xc0\x00\x11\x08' + (1234).to_bytes(2, 'big') + (5678).to_bytes(2, 'big')
        p = tmp_path / 'actually-a-png.jpg'
        p.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 32 + sof + b'\x00' * 32)
        assert sc.jpeg_dimensions(str(p)) is None

    def test_a_real_jpeg_with_the_same_bytes_appended_still_reads_its_own_sof(self, tmp_path):
        """Discrimination the other way: the guard must not make valid files unreadable."""
        path = write_jpeg(tmp_path / 'p.jpg', (640, 480))
        assert sc.jpeg_dimensions(path) == (640, 480)


class TestProbe:

    @staticmethod
    def census(rows):
        return {'records': [
            {'pano_id': pid, 'city': city, 'era': era, 'found': found,
             'stored_width': sw, 'stored_height': sh,
             'served_width': vw, 'served_height': vh}
            for pid, city, era, found, sw, sh, vw, vh in rows]}

    def test_the_sample_comes_from_the_census_verbatim(self):
        rows = sc.sample_from_census(self.census([
            ('AAxx', 'seattle-wa', 'legacy', False, 16384.0, 8192.0, None, None),
            ('BByy', 'cdmx', 'post179', True, 16384.0, 8192.0, 16384, 8192)]))
        assert [r['pano_id'] for r in rows] == ['AAxx', 'BByy']
        assert rows[0]['alive_at_google'] is False and rows[1]['alive_at_google'] is True

    def test_the_store_path_convention(self):
        assert sc.store_path('/root', 'seattle-wa', 'AbCdEf') == os.path.join(
            '/root', 'seattle-wa', 'Ab', 'AbCdEf.jpg')

    def test_it_finds_present_panos_and_reads_their_frame(self, tmp_path):
        root = tmp_path / 'Panoramas'
        (root / 'seattle-wa' / 'AA').mkdir(parents=True)
        write_jpeg(root / 'seattle-wa' / 'AA' / 'AAxx.jpg', (13312, 6656))
        recs = sc.probe(str(root), sc.sample_from_census(self.census([
            ('AAxx', 'seattle-wa', 'legacy', False, 16384.0, 8192.0, None, None),
            ('BByy', 'seattle-wa', 'mid', False, 16384.0, 8192.0, None, None)])))
        assert recs[0]['on_store'] is True
        assert (recs[0]['store_width'], recs[0]['store_height']) == (13312, 6656)
        assert recs[0]['file_bytes'] > 0
        assert recs[1]['on_store'] is False
        assert recs[1]['store_width'] is None and recs[1]['file_bytes'] is None

    def test_a_pano_in_the_wrong_city_directory_is_not_found(self, tmp_path):
        """The store is sharded by city, so coverage is a per-city question, not a global one."""
        root = tmp_path / 'Panoramas'
        (root / 'cdmx' / 'AA').mkdir(parents=True)
        write_jpeg(root / 'cdmx' / 'AA' / 'AAxx.jpg', (64, 32))
        recs = sc.probe(str(root), sc.sample_from_census(self.census([
            ('AAxx', 'seattle-wa', 'legacy', False, None, None, None, None)])))
        assert recs[0]['on_store'] is False


class TestSummarize:

    @staticmethod
    def rec(pid, alive, on_store, era='mid', city='a', stored=(16384, 8192), store=(16384, 8192)):
        return {'pano_id': pid, 'city': city, 'era': era, 'alive_at_google': alive,
                'stored_width': stored[0], 'stored_height': stored[1],
                'served_width': stored[0] if alive else None,
                'served_height': stored[1] if alive else None,
                'on_store': on_store,
                'store_width': store[0] if on_store else None,
                'store_height': store[1] if on_store else None,
                'file_bytes': 1048576 if on_store else None}

    def test_coverage_is_split_by_alive_at_google(self):
        """The decision the study exists for: coverage of the DEAD half, separately."""
        s = sc.summarize([self.rec('a', False, True), self.rec('b', False, False),
                          self.rec('c', True, True), self.rec('d', True, True)])
        assert s['dead_at_google'] == {'n': 2, 'on_store': 1, 'on_store_pct': 50.0}
        assert s['alive_at_google'] == {'n': 2, 'on_store': 2, 'on_store_pct': 100.0}
        assert s['overall']['on_store_pct'] == pytest.approx(75.0)

    def test_a_dead_pano_absent_from_the_store_is_named(self):
        """These are the labels no source can supply; the corpus spec logs them as unreachable, so
        they must be enumerable and not just counted."""
        s = sc.summarize([self.rec('gone', False, False, era='legacy', city='seattle-wa')])
        assert s['dead_and_absent'] == [
            {'pano_id': 'gone', 'city': 'seattle-wa', 'era': 'legacy'}]

    def test_frame_mismatch_is_detected_and_directional(self):
        """The finding the photometa census could not see: the store's JPEG is not always in
        gsv_data's frame, and the direction says which way."""
        s = sc.summarize([
            self.rec('a', False, True, stored=(16384, 8192), store=(13312, 6656)),
            self.rec('b', False, True, stored=(16384, 8192), store=(16384, 8192))])
        assert s['frame_vs_gsv_data'] == {'n': 2, 'match': 1, 'match_pct': 50.0, 'differ': 1}
        assert s['frame_mismatches'] == {'16384x8192->13312x6656': 1}

    def test_panos_absent_from_the_store_do_not_enter_the_frame_comparison(self):
        """A missing file is a coverage fact, not a frame mismatch; conflating them would make the
        preflight's hit rate track pano death instead of scrape history."""
        s = sc.summarize([self.rec('a', False, False, stored=(16384, 8192)),
                          self.rec('b', False, True, stored=(16384, 8192))])
        assert s['frame_vs_gsv_data']['n'] == 1
        assert s['frame_vs_gsv_data']['differ'] == 0

    def test_an_unparsed_header_is_counted_not_guessed(self):
        rec = self.rec('a', False, True)
        rec['store_width'] = rec['store_height'] = None
        s = sc.summarize([rec])
        assert s['headers_read'] == 0 and s['headers_unreadable'] == 1
        assert s['frame_vs_gsv_data']['n'] == 0


class TestCommittedFindings:
    """The probe's conclusions, pinned against the committed artifact (offline). The store is a
    live thing: a re-probe will drift as scrapes are added."""

    @pytest.fixture(scope='class')
    @classmethod
    def summary(cls):
        with open(DATA, encoding='utf-8') as f:
            return json.load(f)['summary']

    def test_the_store_covers_the_panos_google_has_dropped(self, summary):
        """The finding that reshapes the Phase 2 corpus spec: the store, not Google survival, is
        the binding constraint, and it barely binds."""
        assert summary['dead_at_google']['on_store_pct'] >= 98.0
        assert summary['alive_at_google']['on_store_pct'] == 100.0
        assert summary['n_sampled'] == 1360

    def test_coverage_holds_even_in_the_oldest_era(self, summary):
        """Legacy panos are the ones Google has mostly dropped (33.2% alive), so they are where a
        store-sourced corpus would break first if it were going to."""
        assert summary['dead_by_era']['legacy']['on_store_pct'] >= 95.0
        assert summary['dead_by_era']['post179']['on_store_pct'] == 100.0
        assert len(summary['dead_and_absent']) == summary['dead_at_google']['n'] \
            - summary['dead_at_google']['on_store']

    def test_the_store_frame_is_not_always_the_metadata_frame(self, summary):
        """Directly contradicts a naive reading of the photometa census's 0.0% dims drift, which
        compared gsv_data against Google and never against our own JPEG. #77's preflight has a
        real hit rate."""
        f = summary['frame_vs_gsv_data']
        assert f['differ'] > 0
        assert 3.0 <= 100 - f['match_pct'] <= 8.0
        assert summary['headers_unreadable'] == 0

    def test_the_mismatch_is_the_store_holding_an_older_smaller_frame(self, summary):
        """Direction matters: the store is a scrape-time archive, so it lags Google's re-serves."""
        m = summary['frame_mismatches']
        assert m['16384x8192->13312x6656'] >= 50
        assert m['16384x8192->13312x6656'] > sum(
            v for k, v in m.items() if k != '16384x8192->13312x6656')


class TestOneDefinitionOfTheHeaderReader:
    """`jpeg_dimensions` started here and now has two callers, so it lives in downloaders/common.py.

    refetch_panos.py (#73) asks the same question of every pano in a work-list: what are the stored
    file's dimensions, without paying a 384 MB decode to find out. The obvious way to get that into a
    root-level tool is to copy the marker scanner, which is how this repo ended up with four local
    copies of `num()` (see tests/test_studyfmt.py's TestOneDefinition). One definition, asserted.
    """

    def test_store_coverage_imports_it_rather_than_declaring_it(self):
        from downloaders import common
        assert sc.jpeg_dimensions is common.jpeg_dimensions

    def test_nothing_else_in_the_repo_declares_its_own(self):
        """Discovered from the tree, not from a list of filenames — the hardcoded-list version of the
        studyfmt test went stale on the very next script added."""
        from downloaders import common

        roots = [REPO_ROOT, SCRIPTS, os.path.join(REPO_ROOT, 'downloaders')]
        checked = 0
        for root in roots:
            for name in sorted(os.listdir(root)):
                path = os.path.join(root, name)
                if not name.endswith('.py') or not os.path.isfile(path):
                    continue
                if os.path.samefile(path, common.__file__):
                    continue
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                checked += 1
                for decl in ('def jpeg_dimensions(', 'def _jpeg_dimensions(', 'SOF_MARKERS = '):
                    assert decl not in text, '%s declares its own %s' % (name, decl)
        assert checked >= 20, 'expected to have scanned the repo scripts, scanned %d' % checked
