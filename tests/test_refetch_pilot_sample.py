"""Tests for reports/scripts/refetch_pilot_sample.py, and the pin that matters: the committed pilot subset is
exactly what the committed seed draws from the committed work-list.

The pilot ran against a copy of the panoramas in that subset, so if the draw ever stopped reproducing it, the
pilot artifact would describe a population nobody could re-derive.
"""

import gzip
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
DATA = os.path.join(REPO_ROOT, 'reports', 'data')
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refetch_pilot_sample as sample  # noqa: E402
import refetch_panos as rp  # noqa: E402

FULL_WORKLIST = os.path.join(DATA, '2026-08-19-fover-refetch-worklist-seattle.csv.gz')
PILOT_SUBSET = os.path.join(DATA, '2026-09-05-fover-refetch-pilot-worklist-seattle.csv.gz')
PILOT_SEED = 20260905
PILOT_N = 200


class TestTheCommittedSubsetIsReproducible:
    def test_the_seed_redraws_the_committed_subset_exactly(self):
        rows, _ = sample.read_rows(FULL_WORKLIST)
        redrawn = [r['pano_id'] for r in sample.draw(rows, PILOT_N, PILOT_SEED)]
        committed = [r['pano_id'] for r in sample.read_rows(PILOT_SUBSET)[0]]

        assert redrawn == committed
        assert len(committed) == PILOT_N

    def test_a_different_seed_draws_a_different_subset(self):
        """Discrimination for the test above: a draw() that ignored the seed would still reproduce the file."""
        rows, _ = sample.read_rows(FULL_WORKLIST)

        assert [r['pano_id'] for r in sample.draw(rows, PILOT_N, PILOT_SEED + 1)] != \
            [r['pano_id'] for r in sample.draw(rows, PILOT_N, PILOT_SEED)]

    def test_the_subset_carries_the_full_worklist_rows_unchanged(self):
        """Every column, not just the id: the pass reads width/height off the subset, and a subset that
        re-derived or dropped them would put the dims_changed gate on different footing than the full list."""
        full = {r['pano_id']: r for r in sample.read_rows(FULL_WORKLIST)[0]}
        subset, fieldnames = sample.read_rows(PILOT_SUBSET)

        assert fieldnames == ['pano_id', 'width', 'height', 'band_labels', 'min_pano_y']
        for row in subset:
            assert row == full[row['pano_id']]

    def test_the_subset_keeps_the_full_worklist_order(self):
        order = {r['pano_id']: i for i, r in enumerate(sample.read_rows(FULL_WORKLIST)[0])}
        positions = [order[r['pano_id']] for r in sample.read_rows(PILOT_SUBSET)[0]]

        assert positions == sorted(positions)

    def test_the_subset_reads_back_through_the_pass_with_frames_intact(self):
        records = rp.read_worklist(PILOT_SUBSET)

        assert len(records) == PILOT_N
        assert all({'pano_id', 'width', 'height'} <= set(r) for r in records)


class TestTheCommandLine:
    def write(self, path, ids):
        with gzip.open(str(path), 'wt', newline='', encoding='utf8') as f:
            f.write('pano_id,width,height\n' + ''.join('%s,16384,8192\n' % i for i in ids))
        return str(path)

    def test_it_writes_a_subset_the_pass_can_read(self, tmp_path, capsys):
        src = self.write(tmp_path / 'full.csv.gz', ['p%02d' % i for i in range(30)])
        out = str(tmp_path / 'sub.csv.gz')

        assert sample.main([src, '--n', '5', '--seed', '7', '--write', out]) == 0

        records = rp.read_worklist(out)
        assert len(records) == 5
        assert all(r['width'] == 16384 for r in records)
        assert '5 of 30 rows, seed 7' in capsys.readouterr().out

    def test_it_writes_nothing_without_write(self, tmp_path):
        src = self.write(tmp_path / 'full.csv.gz', ['p%02d' % i for i in range(30)])

        assert sample.main([src, '--n', '5', '--seed', '7']) == 0

        assert [p.name for p in tmp_path.iterdir()] == ['full.csv.gz']

    def test_the_seed_is_required(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            sample.build_parser().parse_args(['w.csv.gz'])

        assert excinfo.value.code == 2
        assert '--seed' in capsys.readouterr().err

    def test_asking_for_more_rows_than_exist_fails_loudly(self, tmp_path):
        src = self.write(tmp_path / 'full.csv.gz', ['p%02d' % i for i in range(3)])

        with pytest.raises(SystemExit) as excinfo:
            sample.main([src, '--n', '5', '--seed', '7'])

        assert 'asked for 5 rows' in str(excinfo.value)
