"""Tests for refetch_panos.py — the #73 repair pass.

The property this file exists to hold is narrow and load-bearing: **the stored panorama survives every
path but `replaced`**. About half of the labelled panoramas in the store no longer exist at Google, so
for a large share of any work-list the file on disk is the only copy there will ever be, and a repair
tool that can lose one is worse than no repair tool. So every refusal is asserted byte-for-byte against
the original, not merely by its return value.

Network-free: the three gsv seams (#73's extraction) are stubbed, which is what they were extracted for.
The one place real bytes are used is the recovery measurement, which is pinned against the committed
`fover` fixture pair — a genuine 512 body and the 256 body CBK served for the same cell.
"""

import csv
import gzip
import json
import os
import sys

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refetch_panos as rp  # noqa: E402
from downloaders import gsv  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'tiles')

PANO = 'refetchPanoAAAAAAAAAA'
# 13312x6656 is a real zoom-5 geometry, and the smallest one the band table covers - so the band arithmetic
# is exercised without any test decoding a 384 MB frame.
Z5_DIMS = (13312, 6656)


def store_with_pano(tmp_path, pano_id=PANO, dims=Z5_DIMS, mtime='2026-01-01', color=(90, 120, 60)):
    """A store holding one panorama, written old enough to be a `fover`-era file."""
    shard = tmp_path / pano_id[:2]
    shard.mkdir(parents=True, exist_ok=True)
    path = shard / (pano_id + '.jpg')
    # A tiny JPEG carrying a forged SOF is enough: every gate reads dimensions through the header reader,
    # never through a decode, which is the whole reason that reader exists.
    Image.new('RGB', (16, 8), color).save(str(path), 'JPEG')
    _forge_dimensions(str(path), dims)
    stamp = rp._parse_fixed_after(mtime)
    os.utime(str(path), (stamp, stamp))
    return str(path)


def _forge_dimensions(path, dims):
    """Rewrite the SOF header's width/height so a 16x8 JPEG reports `dims`.

    Cheaper than committing a 13312x6656 fixture, and exact for everything under test: no test here decodes
    a stored panorama except the measurement ones, which build their own images.
    """
    with open(path, 'rb') as f:
        data = bytearray(f.read())
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in gsv_sof_markers():
            data[i + 5:i + 7] = dims[1].to_bytes(2, 'big')
            data[i + 7:i + 9] = dims[0].to_bytes(2, 'big')
            break
        i += 1
    with open(path, 'wb') as f:
        f.write(bytes(data))


def gsv_sof_markers():
    from downloaders.common import SOF_MARKERS
    return SOF_MARKERS


def stub_seams(monkeypatch, resolve=None, fetch=None, covers=True):
    """Replace the three gsv seams; returns the list of (pano_id, width, height, zoom) fetches attempted."""
    fetched = []

    def default_resolve(pano_info):
        return int(pano_info['width']), int(pano_info['height']), 5

    def default_fetch(pano_id, width, height, zoom):
        return gsv.StitchedPano(Image.new('RGB', (width, height), (10, 200, 10)), 0, False)

    def recording_fetch(pano_id, width, height, zoom):
        fetched.append((pano_id, width, height, zoom))
        return (fetch or default_fetch)(pano_id, width, height, zoom)

    monkeypatch.setattr(gsv, 'resolve_zoom_and_dims', resolve or default_resolve)
    monkeypatch.setattr(gsv, 'frame_covers_pano', lambda *a: covers)
    monkeypatch.setattr(gsv, 'fetch_pano_image', recording_fetch)
    return fetched


def no_seams(monkeypatch):
    """Assert nothing reaches the network at all."""
    def boom(*args, **kwargs):
        raise AssertionError('this decision must cost zero requests')

    monkeypatch.setattr(gsv, 'resolve_zoom_and_dims', boom)
    monkeypatch.setattr(gsv, 'frame_covers_pano', boom)
    monkeypatch.setattr(gsv, 'fetch_pano_image', boom)


# --- work-list intake -----------------------------------------------------------------------------------

class TestReadWorklist:
    def write(self, path, rows, header='pano_id,width,height', gz=False):
        text = header + '\n' + '\n'.join(rows) + '\n'
        opener = gzip.open if gz else open
        with opener(str(path), 'wt', encoding='utf8', newline='') as f:
            f.write(text)
        return str(path)

    def test_it_reads_ids_and_dims(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', ['abc,13312,6656'])

        assert rp.read_worklist(path) == [{'pano_id': 'abc', 'width': 13312, 'height': 6656}]

    def test_it_reads_a_gzipped_worklist(self, tmp_path):
        path = self.write(tmp_path / 'w.csv.gz', ['abc,13312,6656'], gz=True)

        assert rp.read_worklist(path) == [{'pano_id': 'abc', 'width': 13312, 'height': 6656}]

    def test_numeric_ids_stay_strings(self, tmp_path):
        """The #46 bug class. A Mapillary-style all-numeric column infers int64 under pandas, and every
        pano_id[:2] shard slice then explodes - so this reads with `csv`, and this test is why."""
        path = self.write(tmp_path / 'w.csv', ['123456789012345,13312,6656'])

        (record,) = rp.read_worklist(path)
        assert record['pano_id'] == '123456789012345'
        assert isinstance(record['pano_id'], str)

    def test_it_dedupes_keeping_the_first(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', ['abc,13312,6656', 'abc,16384,8192', 'def,13312,6656'])

        assert [r['pano_id'] for r in rp.read_worklist(path)] == ['abc', 'def']
        assert rp.read_worklist(path)[0]['width'] == 13312

    def test_blank_dims_are_omitted_rather_than_guessed(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', ['abc,,'])

        assert rp.read_worklist(path) == [{'pano_id': 'abc'}]

    def test_dims_are_optional_columns(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', ['abc'], header='pano_id')

        assert rp.read_worklist(path) == [{'pano_id': 'abc'}]

    def test_a_missing_id_column_fails_loudly_and_names_the_file(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', ['1,2'], header='panoid,width')

        with pytest.raises(ValueError) as exc:
            rp.read_worklist(path)
        assert 'w.csv' in str(exc.value)

    def test_blank_rows_are_dropped(self, tmp_path):
        path = self.write(tmp_path / 'w.csv', [',13312,6656', 'abc,13312,6656'])

        assert [r['pano_id'] for r in rp.read_worklist(path)] == ['abc']


class TestWalkStore:
    def test_it_finds_every_stored_panorama_and_ignores_everything_else(self, tmp_path):
        store_with_pano(tmp_path, 'aaBBccDDeeFFggHHiiJJ')
        store_with_pano(tmp_path, 'zzYYxxWWvvUUttSSrrQQ')
        (tmp_path / 'aa' / 'aaBBccDDeeFFggHHiiJJ.depth.npz').write_bytes(b'not an image')
        (tmp_path / 'log.csv').write_text('a,b\n')

        assert sorted(r['pano_id'] for r in rp.walk_store(str(tmp_path))) == \
            ['aaBBccDDeeFFggHHiiJJ', 'zzYYxxWWvvUUttSSrrQQ']


class TestLoadLedger:
    def test_it_reads_permanent_outcomes_and_skips_the_header(self, tmp_path):
        path = tmp_path / rp.LEDGER_FILENAME
        path.write_text('pano_id,status\nabc,replaced\ndef,gone\n')

        assert rp.load_ledger(str(path)) == {'abc', 'def'}

    def test_a_torn_line_costs_one_re_examined_panorama_not_a_crash(self, tmp_path):
        """The #55 lesson: a ledger damaged by a crash mid-append must degrade, not take every future run
        down with it."""
        path = tmp_path / rp.LEDGER_FILENAME
        path.write_text('pano_id,status\nabc,replaced\ndef\n,\nghi,gone\n')

        assert rp.load_ledger(str(path)) == {'abc', 'ghi'}

    def test_an_unknown_status_is_ignored(self, tmp_path):
        """A ledger from a newer version of this script degrades to re-examining those panoramas, which is
        safe: every gate re-runs, and the zero-request ones cost nothing."""
        path = tmp_path / rp.LEDGER_FILENAME
        path.write_text('pano_id,status\nabc,teleported\n')

        assert rp.load_ledger(str(path)) == set()

    def test_a_missing_ledger_is_an_empty_one(self, tmp_path):
        assert rp.load_ledger(str(tmp_path / 'nope.csv')) == set()


# --- the zero-request decisions -------------------------------------------------------------------------

class TestDecideWithoutFetching:
    def decide(self, tmp_path, record=None, mtime_cutoff='2026-08-07', allow_dims_change=False):
        return rp.decide_without_fetching(str(tmp_path), record or {'pano_id': PANO},
                                          rp._parse_fixed_after(mtime_cutoff), allow_dims_change)

    def test_no_stored_image_is_absent_because_this_repairs_and_never_backfills(self, tmp_path):
        assert self.decide(tmp_path) == ('absent', None)

    def test_an_unreadable_stored_file_is_left_for_a_human(self, tmp_path):
        shard = tmp_path / PANO[:2]
        shard.mkdir()
        (shard / (PANO + '.jpg')).write_bytes(b'not a jpeg at all')

        assert self.decide(tmp_path) == ('unreadable', None)

    def test_a_pre_zoom5_frame_is_not_affected(self, tmp_path):
        """3328x1664 is a max-zoom-3 panorama. CBK served those at full size at every level, so there is
        nothing to recover - and skipping them without a request is what keeps a store sweep affordable."""
        store_with_pano(tmp_path, dims=(3328, 1664))

        assert self.decide(tmp_path) == ('not_affected', None)

    def test_a_file_written_after_the_fix_is_already_clean(self, tmp_path):
        store_with_pano(tmp_path, mtime='2026-08-08')

        assert self.decide(tmp_path) == ('already_clean', None)

    def test_a_file_written_on_the_cutoff_day_counts_as_clean(self, tmp_path):
        store_with_pano(tmp_path, mtime='2026-08-07')

        assert self.decide(tmp_path) == ('already_clean', None)

    def test_an_older_zoom5_frame_is_fetched_at_the_stored_dims(self, tmp_path):
        store_with_pano(tmp_path)

        assert self.decide(tmp_path) == (None, Z5_DIMS)

    def test_a_worklist_frame_that_disagrees_stops_the_panorama(self, tmp_path):
        """Recovering polar resolution and re-framing a panorama are different decisions. The second moves
        every label's pixel coordinates relative to the image, so it is opt-in and costs zero requests to
        refuse."""
        store_with_pano(tmp_path)
        record = {'pano_id': PANO, 'width': 16384, 'height': 8192}

        assert self.decide(tmp_path, record) == ('dims_changed', None)

    def test_allow_dims_change_fetches_at_the_worklist_frame_instead(self, tmp_path):
        store_with_pano(tmp_path)
        record = {'pano_id': PANO, 'width': 16384, 'height': 8192}

        assert self.decide(tmp_path, record, allow_dims_change=True) == (None, (16384, 8192))

    def test_a_worklist_frame_that_agrees_is_not_a_change(self, tmp_path):
        store_with_pano(tmp_path)
        record = {'pano_id': PANO, 'width': Z5_DIMS[0], 'height': Z5_DIMS[1]}

        assert self.decide(tmp_path, record) == (None, Z5_DIMS)

    def test_the_zero_request_decisions_really_cost_nothing(self, tmp_path, monkeypatch):
        no_seams(monkeypatch)
        store_with_pano(tmp_path, dims=(3328, 1664))

        assert rp.refetch_store(str(tmp_path), [{'pano_id': PANO}])['not_affected'] == 1


# --- the acceptance gates -------------------------------------------------------------------------------

class TestRefetchPanoRefusals:
    """Every refusal must leave the stored bytes exactly as they were. Asserted on the bytes, not the
    return value: a gate that returns the right word while having already overwritten the file is the
    failure this tool cannot have."""

    def run(self, tmp_path, monkeypatch, resolve=None, fetch=None, max_black=rp.MAX_BLACK_FRACTION,
            covers=True):
        path = store_with_pano(tmp_path)
        before = open(path, 'rb').read()
        stub_seams(monkeypatch, resolve=resolve, fetch=fetch, covers=covers)
        outcome = rp.refetch_pano(str(tmp_path), {'pano_id': PANO}, Z5_DIMS, max_black, False, [])
        return outcome, before, open(path, 'rb').read(), tmp_path / PANO[:2]

    def test_a_retired_panorama_is_gone_and_the_stored_copy_is_kept(self, tmp_path, monkeypatch):
        """The case that makes non-destructiveness non-negotiable: ~52% of labelled panoramas are here."""
        outcome, before, after, _shard = self.run(tmp_path, monkeypatch, resolve=lambda info: None)

        assert outcome == 'gone'
        assert after == before

    def test_a_pano_google_now_serves_larger_is_refused_before_the_fan_out(self, tmp_path, monkeypatch):
        """The only silent-corruption path in the tool, and the one gate that can see it.

        Requesting a 26x13 grid for a pano Google now holds at 32x16 returns the top-left 81% of it - at
        the stored file's exact dimensions, with zero undersized tiles and no black anywhere. Every other
        gate passes it. The frame probe is spent BEFORE the fan-out, so a bad frame costs two requests
        rather than 514.
        """
        fetched = []
        outcome, before, after, _shard = self.run(
            tmp_path, monkeypatch, covers=False,
            fetch=lambda p, w, h, z: fetched.append(1) or gsv.StitchedPano(
                Image.new('RGB', (w, h), (1, 2, 3)), 0, False))

        assert outcome == 'frame_grew'
        assert after == before
        assert fetched == [], 'the frame probe must run before the 512-tile fan-out'

    def test_a_fallback_zoom_is_refused_because_swapping_would_be_a_downgrade(self, tmp_path, monkeypatch):
        outcome, before, after, _shard = self.run(
            tmp_path, monkeypatch,
            fetch=lambda p, w, h, z: gsv.StitchedPano(Image.new('RGB', (w, h), (1, 2, 3)), 0, True))

        assert outcome == 'upscaled'
        assert after == before

    def test_an_undersized_tile_means_there_is_nothing_to_gain(self, tmp_path, monkeypatch):
        outcome, before, after, _shard = self.run(
            tmp_path, monkeypatch,
            fetch=lambda p, w, h, z: gsv.StitchedPano(Image.new('RGB', (w, h), (1, 2, 3)), 7, False))

        assert outcome == 'undersized'
        assert after == before

    def test_a_black_bordered_stitch_is_refused(self, tmp_path, monkeypatch):
        """A reported frame larger than what Google serves fills the out-of-range tiles with black. At
        13312/16384 that is 34% of the frame, which sails under the stitcher's own 50% limit."""
        def mostly_black(p, w, h, z):
            image = Image.new('RGB', (w, h), (0, 0, 0))
            image.paste(Image.new('RGB', (w, h // 2), (30, 60, 90)), (0, 0))
            return gsv.StitchedPano(image, 0, False)

        outcome, before, after, _shard = self.run(tmp_path, monkeypatch, fetch=mostly_black)

        assert outcome == 'too_black'
        assert after == before

    def test_a_clean_stitch_is_swapped_in(self, tmp_path, monkeypatch):
        outcome, before, after, shard = self.run(tmp_path, monkeypatch)

        assert outcome == 'replaced'
        assert after != before
        with Image.open(shard / (PANO + '.jpg')) as im:
            assert im.size == Z5_DIMS
        assert list(shard.glob('*.part')) == []

    def test_a_write_that_dies_half_way_leaves_the_original_and_no_part_file(self, tmp_path, monkeypatch):
        """The failure atomic_output_path exists for: a store that fills, or an sshfs mount that drops,
        part-way through a 10 MB save.

        The fake write puts bytes on disk BEFORE raising, which is what makes this discriminating. A save
        that merely raises cannot tell an atomic write from a direct one - both leave the original alone -
        so a version of this test that only raised passed against a `stitched.image.save(final_path)`
        mutant, i.e. against a tool that truncates a panorama Google may no longer have.
        """
        path = store_with_pano(tmp_path)
        before = open(path, 'rb').read()

        def half_written_save(target, *args, **kwargs):
            with open(target, 'wb') as f:
                f.write(b'\xff\xd8truncated garbage')
            raise OSError('store went away mid-write')

        def fetch(p, w, h, z):
            # A real frame, so every gate ahead of the save runs for real; only the write explodes.
            image = Image.new('RGB', (w, h), (10, 200, 10))
            image.save = half_written_save
            return gsv.StitchedPano(image, 0, False)

        stub_seams(monkeypatch, fetch=fetch)

        with pytest.raises(OSError):
            rp.refetch_pano(str(tmp_path), {'pano_id': PANO}, Z5_DIMS, rp.MAX_BLACK_FRACTION, False, [])

        assert open(path, 'rb').read() == before
        assert list((tmp_path / PANO[:2]).glob('*.part')) == []

    def test_the_gates_run_before_any_write_even_when_several_would_fire(self, tmp_path, monkeypatch):
        """Ordering: upscaled is reported ahead of undersized because it is the more damaging swap, but the
        point of the test is that neither writes."""
        outcome, before, after, _shard = self.run(
            tmp_path, monkeypatch,
            fetch=lambda p, w, h, z: gsv.StitchedPano(Image.new('RGB', (w, h), (1, 2, 3)), 9, True))

        assert outcome == 'upscaled'
        assert after == before


# --- the run loop ---------------------------------------------------------------------------------------

class TestRefetchStore:
    def test_permanent_outcomes_are_ledgered_and_transient_ones_are_not(self, tmp_path, monkeypatch):
        store_with_pano(tmp_path, 'aaAAAAAAAAAAAAAAAAAA')
        store_with_pano(tmp_path, 'bbBBBBBBBBBBBBBBBBBB')

        def fetch(pano_id, w, h, z):
            if pano_id.startswith('bb'):
                raise OSError('network blip')
            return gsv.StitchedPano(Image.new('RGB', (w, h), (5, 5, 5)), 0, False)

        stub_seams(monkeypatch, fetch=fetch)
        counts = rp.refetch_store(str(tmp_path), [{'pano_id': 'aaAAAAAAAAAAAAAAAAAA'},
                                                  {'pano_id': 'bbBBBBBBBBBBBBBBBBBB'}])

        assert counts['replaced'] == 1
        assert counts['transient_failures'] == 1
        assert rp.load_ledger(str(tmp_path / rp.LEDGER_FILENAME)) == {'aaAAAAAAAAAAAAAAAAAA'}

    def test_a_second_run_over_a_finished_store_costs_nothing(self, tmp_path, monkeypatch):
        store_with_pano(tmp_path)
        records = [{'pano_id': PANO}]
        stub_seams(monkeypatch)
        rp.refetch_store(str(tmp_path), records)

        no_seams(monkeypatch)
        counts = rp.refetch_store(str(tmp_path), records)

        assert counts['skipped_ledgered'] == 1

    def test_a_lost_ledger_still_costs_nothing_after_a_finished_sweep(self, tmp_path, monkeypatch):
        """The mtime gate is the belt to the ledger's braces: a re-fetched file is newer than the fix date,
        so a store whose ledger was deleted re-examines every panorama and fetches none of them."""
        store_with_pano(tmp_path)
        stub_seams(monkeypatch)
        rp.refetch_store(str(tmp_path), [{'pano_id': PANO}])
        os.remove(str(tmp_path / rp.LEDGER_FILENAME))

        no_seams(monkeypatch)
        counts = rp.refetch_store(str(tmp_path), [{'pano_id': PANO}])

        assert counts['already_clean'] == 1

    def test_dry_run_makes_no_requests_and_writes_no_ledger(self, tmp_path, monkeypatch):
        store_with_pano(tmp_path)
        no_seams(monkeypatch)

        counts = rp.refetch_store(str(tmp_path), [{'pano_id': PANO}], dry_run=True)

        assert counts['would_fetch'] == 1
        assert not (tmp_path / rp.LEDGER_FILENAME).exists()

    def test_it_never_touches_the_nightly_ledgers_or_the_depth_artifacts(self, tmp_path, monkeypatch):
        """Explicit non-goal, asserted rather than documented. The image ledger in particular still reads
        `downloaded=1` for a repaired panorama, which is correct - it IS downloaded."""
        store_with_pano(tmp_path)
        sentinels = {}
        for name, body in (('pano_id_log.csv', 'pano_id,downloaded\n%s,1\n' % PANO),
                           ('depth_log.csv', 'pano_id,status\n%s,saved\n' % PANO),
                           ('log.csv', '2026-01-01,0,0,0,0,0\n')):
            (tmp_path / name).write_text(body)
            sentinels[name] = body
        artifact = tmp_path / PANO[:2] / (PANO + '.depth.npz')
        artifact.write_bytes(b'depth artifact bytes')

        stub_seams(monkeypatch)
        rp.refetch_store(str(tmp_path), [{'pano_id': PANO}])

        for name, body in sentinels.items():
            assert (tmp_path / name).read_text() == body
        assert artifact.read_bytes() == b'depth artifact bytes'

    def test_max_panos_stops_the_run(self, tmp_path, monkeypatch):
        for i in range(4):
            store_with_pano(tmp_path, 'p%d%s' % (i, 'X' * 18))
        fetched = stub_seams(monkeypatch)

        counts = rp.refetch_store(str(tmp_path), [{'pano_id': 'p%d%s' % (i, 'X' * 18)} for i in range(4)],
                                  max_panos=2)

        assert len(fetched) == 2
        assert counts['stop_reason'] == 'max-panos'

    def test_max_runtime_stops_the_run(self, tmp_path, monkeypatch):
        for i in range(3):
            store_with_pano(tmp_path, 'p%d%s' % (i, 'X' * 18))
        stub_seams(monkeypatch)

        counts = rp.refetch_store(str(tmp_path), [{'pano_id': 'p%d%s' % (i, 'X' * 18)} for i in range(3)],
                                  max_runtime_minutes=0.0)

        assert counts['stop_reason'] == 'max-runtime'
        assert not counts.get('replaced')

    def test_the_breaker_trips_rather_than_spending_the_budget_on_a_wall(self, tmp_path, monkeypatch):
        n = rp.MAX_CONSECUTIVE_FAILURES + 3
        for i in range(n):
            store_with_pano(tmp_path, 'p%02d%s' % (i, 'X' * 17))

        def always_fails(*args, **kwargs):
            raise OSError('google is refusing us')

        fetched = stub_seams(monkeypatch, fetch=always_fails)
        counts = rp.refetch_store(str(tmp_path),
                                  [{'pano_id': 'p%02d%s' % (i, 'X' * 17)} for i in range(n)])

        assert counts['stop_reason'] == 'consecutive-failures'
        assert len(fetched) == rp.MAX_CONSECUTIVE_FAILURES
        assert rp.load_ledger(str(tmp_path / rp.LEDGER_FILENAME)) == set()

    def test_a_success_resets_the_breaker(self, tmp_path, monkeypatch):
        n = rp.MAX_CONSECUTIVE_FAILURES + 2
        ids = ['p%02d%s' % (i, 'X' * 17) for i in range(n)]
        for pano_id in ids:
            store_with_pano(tmp_path, pano_id)
        state = {'calls': 0}

        def alternating(pano_id, w, h, z):
            state['calls'] += 1
            if state['calls'] % 2:
                raise OSError('blip')
            return gsv.StitchedPano(Image.new('RGB', (w, h), (5, 5, 5)), 0, False)

        stub_seams(monkeypatch, fetch=alternating)
        counts = rp.refetch_store(str(tmp_path), [{'pano_id': p} for p in ids])

        assert 'stop_reason' not in counts
        assert counts['replaced'] == n // 2

    def test_sample_bounds_the_candidates(self, tmp_path, monkeypatch):
        ids = ['p%02d%s' % (i, 'X' * 17) for i in range(6)]
        for pano_id in ids:
            store_with_pano(tmp_path, pano_id)
        fetched = stub_seams(monkeypatch)

        rp.refetch_store(str(tmp_path), [{'pano_id': p} for p in ids], sample=2)

        assert len(fetched) == 2

    def test_the_ledger_is_appended_not_rewritten(self, tmp_path, monkeypatch):
        ledger = tmp_path / rp.LEDGER_FILENAME
        ledger.write_text('pano_id,status\npreexisting,gone\n')
        store_with_pano(tmp_path)
        stub_seams(monkeypatch)

        rp.refetch_store(str(tmp_path), [{'pano_id': PANO}])

        rows = list(csv.reader(ledger.read_text().splitlines()))
        assert rows[0] == ['pano_id', 'status']
        assert ['preexisting', 'gone'] in rows
        assert [PANO, 'replaced'] in rows


# --- what the re-fetch actually recovered -----------------------------------------------------------------

class TestMeasureRecovery:
    """The measurement the whole re-download decision turns on, and the reason `--measure` exists.

    The trick is the horizon band. CBK served those rows at full size both before and after the fix, so
    whatever `mae_old_vs_new` reads there is our own JPEG round-trip and nothing else - a per-panorama
    noise floor, measured on the same encoder. The bottom band's figure minus that is the recovered detail.
    Without the control there is no way to tell 0.4 MAE of recovered detail from 0.4 MAE of re-encoding.
    """

    def synthetic_pair(self, height=6656, degrade_bottom=True):
        """(old, new) panoramas where the bottom band of `old` is a 2x upscale, as `fover` produced.

        Narrow on purpose - the bands are horizontal strips, so width costs memory and proves nothing.
        """
        width = 64
        rng = np.random.default_rng(1973)
        truth = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
        new = Image.fromarray(truth, 'RGB')
        bottom_top, bottom_bottom = rp.band_pixel_rows(height)[0]
        old = new.copy()
        if degrade_bottom:
            strip = new.crop((0, bottom_top, width, bottom_bottom))
            halved = strip.resize((max(1, width // 2), max(1, strip.height // 2)), Image.LANCZOS)
            old.paste(halved.resize(strip.size, Image.LANCZOS), (0, bottom_top))
        return old, new

    def test_a_degraded_bottom_band_shows_recovery_above_the_noise_floor(self):
        old, new = self.synthetic_pair()

        m = rp.measure_recovery(old, new)

        assert m['bottom']['mae_old_vs_new'] > m['horizon']['mae_old_vs_new']
        assert m['recovered_above_noise'] > 0

    def test_an_undegraded_panorama_shows_no_recovery(self):
        """Discrimination: if this passed too, the metric would just be measuring noise."""
        old, new = self.synthetic_pair(degrade_bottom=False)

        m = rp.measure_recovery(old, new)

        assert m['bottom']['mae_old_vs_new'] == 0
        assert m['recovered_above_noise'] == 0

    def test_it_reports_the_band_pixel_rows_it_measured(self):
        old, new = self.synthetic_pair()

        m = rp.measure_recovery(old, new)

        assert m['bottom']['rows_px'] == [4608, 6656]
        assert m['horizon']['rows_px'] == [2048, 4608]

    def test_the_halving_reference_is_larger_at_the_horizon_than_at_the_poles_on_real_bytes(self):
        """The oversampling argument, re-measured through this function rather than restated: `fover`
        halved the rows where halving costs least. Uses the committed full-resolution tile pair, which is
        real CBK imagery from a polar row and a horizon row of the same Seattle panorama."""
        polar = np.asarray(Image.open(os.path.join(FIXTURES, 'z5_nofover_4_2.jpg')).convert('L'),
                           dtype=np.float32)
        horizon = np.asarray(Image.open(os.path.join(FIXTURES, 'z5_full_8_10.jpg')).convert('L'),
                             dtype=np.float32)

        polar_cost = abs(polar - rp._halve_and_restore(polar)).mean()
        horizon_cost = abs(horizon - rp._halve_and_restore(horizon)).mean()

        assert horizon_cost > polar_cost

    def test_the_committed_fover_pair_is_what_this_metric_is_calibrated_against(self):
        """Real bytes, one parameter apart: the 256 body CBK served with `fover` and the 512 it serves
        without. Upscaling the first is exactly what the old stitcher did, so this is the per-tile version
        of what a stored polar band lost - and it is small, which is finding 5 of the report arriving
        again through a different door."""
        fover = Image.open(os.path.join(FIXTURES, 'z5_fover2_4_2.jpg')).convert('L')
        genuine = Image.open(os.path.join(FIXTURES, 'z5_nofover_4_2.jpg')).convert('L')
        assert fover.size == (256, 256) and genuine.size == (512, 512)

        upscaled = np.asarray(fover.resize(genuine.size, Image.LANCZOS), dtype=np.float32)
        truth = np.asarray(genuine, dtype=np.float32)

        assert 0 < abs(upscaled - truth).mean() < 2.0

    def test_an_unswept_geometry_is_not_measured_rather_than_measured_wrongly(self):
        old = new = Image.new('RGB', (64, 1024), (10, 10, 10))

        assert rp.measure_recovery(old, new) is None

    def test_a_size_mismatch_is_not_measured(self):
        assert rp.measure_recovery(Image.new('RGB', (64, 6656)), Image.new('RGB', (64, 8192))) is None

    def test_the_measurement_reads_the_stored_frame_before_the_swap_replaces_it(self, tmp_path,
                                                                                monkeypatch):
        """One JSON object per line, and - the part that matters - the OLD bytes are what it compared
        against.

        Asserting only that the keys are present is not enough: a version that measured the fresh frame
        against itself would still write a well-formed record, with every figure a confident zero, and the
        pilot would report that re-fetching recovers nothing. So the record has to carry the same non-zero
        recovery the direct call does.
        """
        store_with_pano(tmp_path)
        old, new = self.synthetic_pair()
        expected = rp.measure_recovery(old, new)
        assert expected['recovered_above_noise'] > 0, 'the fixture must have something to recover'

        monkeypatch.setattr(gsv, 'resolve_zoom_and_dims', lambda info: (64, 6656, 5))
        monkeypatch.setattr(gsv, 'frame_covers_pano', lambda *a: True)
        monkeypatch.setattr(gsv, 'fetch_pano_image',
                            lambda p, w, h, z: gsv.StitchedPano(new, 0, False))
        monkeypatch.setattr(Image, 'open', lambda *a, **k: old)

        rp.refetch_store(str(tmp_path), [{'pano_id': PANO}], measure=True)

        lines = (tmp_path / rp.MEASUREMENTS_FILENAME).read_text().strip().split('\n')
        record = json.loads(lines[0])
        assert record['pano_id'] == PANO
        assert record['recovered_above_noise'] == expected['recovered_above_noise']
        assert record['bottom']['mae_old_vs_new'] == expected['bottom']['mae_old_vs_new']


# --- the band table, against the sweep it came from --------------------------------------------------------

class TestWorklistGeneration:
    """reports/scripts/pano_y_histogram.py --write-worklist, and the round trip into this tool.

    The generator and the consumer are in different directories and were written for different audiences
    (a study, and an ops tool), which is exactly the seam where a column rename goes unnoticed. So the
    assertions here run one through the other rather than pinning the CSV's text.
    """

    def cvmetadata(self, pano_id, pano_y, height=8192, width=16384, label_type_id=1):
        return {'pano_id': pano_id, 'pano_x': 10, 'pano_y': pano_y,
                'pano_width': width, 'pano_height': height, 'label_type_id': label_type_id}

    def test_only_panoramas_with_a_bottom_band_label_are_listed(self, tmp_path):
        import pano_y_histogram as h

        a = h.analyse([self.cvmetadata('inTheBandAAAAAAAAAAAA', 6000),
                       self.cvmetadata('fullResBBBBBBBBBBBBBB', 4000),
                       self.cvmetadata('atTheEdgeCCCCCCCCCCCC', 5632)])
        path = str(tmp_path / 'w.csv.gz')
        h.write_worklist(path, a['panos_with_bottom_band_label'])

        assert [r['pano_id'] for r in rp.read_worklist(path)] == ['inTheBandAAAAAAAAAAAA',
                                                                 'atTheEdgeCCCCCCCCCCCC']

    def test_the_worklist_reads_back_through_the_tool_with_its_frame_intact(self, tmp_path):
        import pano_y_histogram as h

        a = h.analyse([self.cvmetadata('inTheBandAAAAAAAAAAAA', 6000, height=6656, width=13312)])
        path = str(tmp_path / 'w.csv.gz')
        h.write_worklist(path, a['panos_with_bottom_band_label'])

        assert rp.read_worklist(path) == [{'pano_id': 'inTheBandAAAAAAAAAAAA',
                                           'width': 13312, 'height': 6656}]

    def test_labels_on_one_panorama_are_counted_and_the_deepest_is_kept(self, tmp_path):
        import pano_y_histogram as h

        a = h.analyse([self.cvmetadata('inTheBandAAAAAAAAAAAA', 6000),
                       self.cvmetadata('inTheBandAAAAAAAAAAAA', 7500),
                       self.cvmetadata('inTheBandAAAAAAAAAAAA', 5700)])

        entry = a['panos_with_bottom_band_label']['inTheBandAAAAAAAAAAAA']
        assert entry['band_labels'] == 3
        assert entry['min_pano_y'] == 5700

    def test_it_is_ordered_deepest_first_so_a_truncated_pass_did_the_valuable_part(self, tmp_path):
        import pano_y_histogram as h

        a = h.analyse([self.cvmetadata('shallowAAAAAAAAAAAAAA', 5700),
                       self.cvmetadata('deepestBBBBBBBBBBBBBB', 8000),
                       self.cvmetadata('middleCCCCCCCCCCCCCCC', 6800)])
        path = str(tmp_path / 'w.csv.gz')
        h.write_worklist(path, a['panos_with_bottom_band_label'])

        assert [r['pano_id'] for r in rp.read_worklist(path)] == ['deepestBBBBBBBBBBBBBB',
                                                                  'middleCCCCCCCCCCCCCCC',
                                                                  'shallowAAAAAAAAAAAAAA']

    def test_photospheres_without_a_frame_never_reach_the_worklist(self, tmp_path):
        """The 1,756 Seattle records with negative pano_y and no dimensions are third-party photospheres
        that never went through the CBK tile path. Binning them would put ids in the work-list that this
        tool would then spend two requests each discovering are not GSV panoramas at all."""
        import pano_y_histogram as h

        rows = [{'pano_id': 'photosphereAAAAAAAAAA', 'pano_y': -720, 'pano_x': 5}]
        a = h.analyse(rows)

        assert a['skipped_no_pano_dimensions'] == 1
        assert a['panos_with_bottom_band_label'] == {}

    def test_the_count_the_histogram_reports_is_the_length_of_the_worklist(self):
        """The report's headline (7,914 of Seattle's 105,181) and the file a pass consumes must be the same
        population - the count changed from len(set) to len(dict) when the ids started being carried."""
        import pano_y_histogram as h

        a = h.analyse([self.cvmetadata('inTheBandAAAAAAAAAAAA', 6000),
                       self.cvmetadata('inTheBandAAAAAAAAAAAA', 7000),
                       self.cvmetadata('alsoInBandBBBBBBBBBBB', 6100),
                       self.cvmetadata('fullResCCCCCCCCCCCCCC', 4000)])
        s = h.summarise('sidewalk-test.example.edu', a)

        assert s['panos_with_bottom_band_label'] == len(a['panos_with_bottom_band_label']) == 2


class TestBandTableMatchesTheSweep:
    """The band lives in three files now: the swept row maps, the label histogram, and this tool. They are
    one measurement read three ways, so a re-capture that moved the band must fail here rather than leaving
    a repair pass measuring the wrong strip."""

    def test_it_matches_the_full_grid_sweeps(self):
        with open(os.path.join(FIXTURES, 'fover_band_map.json')) as f:
            bands = json.load(f)['bands']

        for label, height in (('Seattle 2022', 8192), ('Sydney 2014', 6656)):
            row_map = bands[label]['row_map']
            full = [i for i, c in enumerate(row_map) if c == '2']
            assert rp.BAND_ROWS[height] == (full[0], full[-1], len(row_map)), label

    def test_it_matches_the_label_histogram(self):
        import pano_y_histogram

        assert rp.BAND_ROWS == pano_y_histogram.BAND_ROWS

    def test_the_bottom_band_starts_where_the_full_resolution_band_ends(self):
        for height in rp.BAND_ROWS:
            (bottom_top, bottom_end), (_h_top, horizon_end) = rp.band_pixel_rows(height)
            assert bottom_top == horizon_end
            assert bottom_end == height
