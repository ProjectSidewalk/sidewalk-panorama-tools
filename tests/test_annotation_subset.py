"""Tests for `reports/scripts/annotation_subset.py` (prereg §4, Amendment 3).

The script exists because two things had to change about an already-rendered tile set without re-cutting
tiles: the referent rule widened, and Jon needs a stratified 50 out of what survives. Both are filters
over a `tasks.json`, and both have a failure mode that is silent in the output — a queue that looks
perfectly well-formed while being drawn from the wrong population, or carrying a protocol the annotator
cannot act on.

So the tests here are mostly about what the produced directory *is not*: not filtered by a stale column,
not carrying an answer key, not carrying last week's flag list, not ordered by stratum.
"""

import json
import os
import shutil
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'reports', 'scripts'))

import annotation_subset as asub  # noqa: E402
import annotation_tiles as at  # noqa: E402
import rawlabels  # noqa: E402


def _corpus(rows):
    """A drawn-corpus frame with the columns the script reads, plus a deliberately WRONG `measurable`."""
    return pd.DataFrame([
        {'label_uid': uid, 'label_type': lt, 'tags': tags, 'band': band,
         'pano_id': f'pano{i}', 'measurable': stale}
        for i, (uid, lt, tags, band, stale) in enumerate(rows)
    ])


def _tasks(uids, flags=('object-absent', 'ambiguous', 'occluded')):
    return {'protocol': 'reports/2026-08-09-crop-priors-prereg.md §4',
            'flags': list(flags), 'initial_view_fraction': 1 / 3, 'n_tasks': len(uids),
            'tasks': [{'label_uid': u, 'tile': f'{u.replace(":", "_")}.jpg',
                       'tile_width': 8, 'tile_height': 8, 'label_type': 'CurbRamp',
                       'rubric': 'Mark the centre.'} for u in uids]}


def _rendered_dir(tmp_path, tasks):
    d = tmp_path / 'rendered'
    d.mkdir()
    for t in tasks['tasks']:
        (d / t['tile']).write_bytes(b'not-really-a-jpeg')
    (d / 'tasks.json').write_text(json.dumps(tasks), encoding='utf-8')
    (d / 'geometry.json').write_text(json.dumps({'seed': 7, 'geometry': {}}), encoding='utf-8')
    return d


class TestMeasurableMaskUsesTheLiveRule:
    """The headline guard, and the bug the script was written to make impossible.

    A drawn corpus carries a `measurable` column computed when it was drawn. Amendment 3 moved the rule
    afterwards, so that column is a snapshot: it says 584 where the live rule says 368. Reading it would
    produce a queue of exactly the right shape drawn from the wrong population, and nothing downstream
    could tell.
    """

    def test_it_ignores_a_stale_measurable_column(self):
        corpus = _corpus([
            ('c:1', 'Signal', '[]', '<5', True),           # stale column says measurable...
            ('c:2', 'CurbRamp', '[]', '<5', False),        # ...and says this one is not
        ])
        got = asub.measurable_mask(corpus)
        assert not got.iloc[0], 'Signal is excluded by the live rule regardless of the column'
        assert got.iloc[1], 'a CurbRamp is measurable regardless of the column'

    def test_it_is_the_shared_rule_and_not_a_transcription(self, monkeypatch):
        """If this function grew its own copy of the type list, the corpus filter and the published
        exclusion counts could drift apart — the exact failure `region_tag_mask` was extracted to
        prevent. Patch the rule and the mask must move with it."""
        corpus = _corpus([('c:1', 'CurbRamp', '[]', '<5', True)])
        assert asub.measurable_mask(corpus).iloc[0]
        monkeypatch.setattr(rawlabels, 'NO_REFERENT_TYPES', frozenset({'CurbRamp'}))
        assert not asub.measurable_mask(corpus).iloc[0]

    def test_the_tag_arm_reaches_it_too(self):
        corpus = _corpus([
            ('c:1', 'Obstacle', '[pole]', '<5', True),
            ('c:2', 'Obstacle', '[stairs]', '<5', True),
            ('c:3', 'Obstacle', '[pole,stairs]', '<5', True),
        ])
        got = list(asub.measurable_mask(corpus))
        assert got == [True, False, False], 'one excluded tag is enough, even beside a kept one'


class TestAllocate:
    def test_it_hands_out_exactly_n(self):
        counts = {'a': 10, 'b': 20, 'c': 70}
        assert sum(asub.allocate(counts, 50).values()) == 50

    def test_it_is_proportional(self):
        alloc = asub.allocate({'a': 10, 'b': 20, 'c': 70}, 100)
        assert alloc == {'a': 10, 'b': 20, 'c': 70}

    @pytest.mark.parametrize('counts,n', [
        ({'tiny': 2, 'big': 98}, 50),
        ({'a': 1, 'b': 1, 'c': 97}, 3),
        ({'a': 7, 'b': 7, 'c': 7, 'd': 1}, 11),
        ({'solo': 5}, 4),
    ])
    def test_it_never_over_allocates_a_stratum_and_still_totals_n(self, counts, n):
        """The invariant, over shapes chosen to stress the remainder loop.

        Worth recording what this does NOT prove, because the first version of this test asserted it and
        was simply wrong: the capacity cap is unreachable under proportional allocation whenever
        `n < total`, since a stratum's share is `n * c / total`, which is below `c` exactly when `n` is
        below `total`. So `min(..., counts[k])` and the `alloc[k] < counts[k]` guard are belt-and-braces
        against a future non-proportional allocator, not live behaviour — and a test claiming to
        exercise them was testing its own misreading.
        """
        alloc = asub.allocate(counts, n)
        assert all(alloc[k] <= counts[k] for k in counts)
        assert sum(alloc.values()) == min(n, sum(counts.values()))

    def test_it_returns_everything_when_n_meets_or_exceeds_the_total(self):
        counts = {'a': 3, 'b': 4}
        assert asub.allocate(counts, 7) == counts
        assert asub.allocate(counts, 99) == counts

    def test_it_is_deterministic_across_dict_orderings(self):
        """Allocation runs before the seed is used, so it must not be a second unseeded source of
        variation: two dicts with the same content in different insertion order must allocate alike."""
        a = asub.allocate({'x': 5, 'y': 5, 'z': 5}, 7)
        b = asub.allocate({'z': 5, 'y': 5, 'x': 5}, 7)
        assert a == b

    def test_an_empty_input_is_not_a_crash(self):
        assert asub.allocate({}, 10) == {}


class TestDraw:
    @pytest.fixture
    def setup(self):
        rows, uids = [], []
        for i in range(40):
            lt = ['CurbRamp', 'Obstacle'][i % 2]
            band = ['<5', '>30'][(i // 2) % 2]
            uids.append(f'c:{i}')
            rows.append((f'c:{i}', lt, '[]', band, True))
        return _tasks(uids), _corpus(rows)

    def test_it_draws_exactly_n(self, setup):
        tasks, corpus = setup
        assert len(asub.draw(tasks, corpus, 12, seed=1)) == 12

    def test_it_is_deterministic_under_the_seed(self, setup):
        tasks, corpus = setup
        assert asub.draw(tasks, corpus, 12, seed=1) == asub.draw(tasks, corpus, 12, seed=1)

    def test_a_different_seed_draws_differently(self, setup):
        """Otherwise the seed is decoration and the 'independent' 50 is a fixed set."""
        tasks, corpus = setup
        assert asub.draw(tasks, corpus, 12, seed=1) != asub.draw(tasks, corpus, 12, seed=2)

    def test_it_spans_every_stratum(self, setup):
        tasks, corpus = setup
        chosen = asub.draw(tasks, corpus, 12, seed=1)
        frame = corpus.set_index('label_uid').loc[chosen]
        assert set(frame['label_type']) == {'CurbRamp', 'Obstacle'}
        assert set(frame['band']) == {'<5', '>30'}

    def test_it_preserves_the_task_files_order(self, setup):
        """Presentation order is an experimental variable — fatigue, and learning the rubric. A queue
        emitted in draw order walks the annotator through one stratum at a time."""
        tasks, corpus = setup
        chosen = asub.draw(tasks, corpus, 12, seed=3)
        original = [t['label_uid'] for t in tasks['tasks']]
        assert chosen == [u for u in original if u in set(chosen)]

    def test_every_drawn_uid_came_from_the_task_list(self, setup):
        """Jon's 50 must be a SUBSET of what the blind annotator sees or the agreement gate has no
        overlap to compute on."""
        tasks, corpus = setup
        available = {t['label_uid'] for t in tasks['tasks']}
        assert set(asub.draw(tasks, corpus, 12, seed=1)) <= available

    def test_dict_iteration_order_does_not_reach_the_rng(self, setup):
        """The pool is sorted before sampling. Without that, a frame arriving in a different row order
        gives a different draw under the same seed, and the seed stops being a record of what was
        drawn."""
        tasks, corpus = setup
        shuffled = corpus.iloc[::-1].reset_index(drop=True)
        assert asub.draw(tasks, corpus, 12, seed=5) == asub.draw(tasks, shuffled, 12, seed=5)


class TestWriteSubset:
    def test_it_copies_only_the_tiles_the_tasks_name(self, tmp_path):
        tasks = _tasks(['c:1', 'c:2', 'c:3'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        asub.write_subset(asub.subset_tasks(tasks, ['c:1', 'c:3']), str(src), str(out))
        assert sorted(p.name for p in out.iterdir()) == ['c_1.jpg', 'c_3.jpg', 'tasks.json']

    def test_it_never_copies_the_answer_key(self, tmp_path):
        """`annotate_server` refuses `geometry.json` by name when serving; this is the other half, at
        the point where a directory is created. A subset dir that never holds it cannot leak it even if
        something less careful serves it later."""
        tasks = _tasks(['c:1'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        asub.write_subset(tasks, str(src), str(out))
        assert (src / 'geometry.json').exists(), 'the source must actually have one to be a test'
        assert not (out / 'geometry.json').exists()

    def test_it_refreshes_the_flag_list_from_code(self, tmp_path):
        """A rendered `tasks.json` is a snapshot of the protocol when the tiles were cut. Amendment 3
        added `no-extent` afterwards, and copying the block through verbatim produced a queue whose only
        escape hatch for an unboundable referent was the `ambiguous` flag it is distinct from — correct
        tiles, correct labels, and no key to press."""
        stale = _tasks(['c:1'], flags=('object-absent', 'ambiguous', 'occluded'))
        src = _rendered_dir(tmp_path, stale)
        out = tmp_path / 'out'
        asub.write_subset(stale, str(src), str(out))
        written = json.loads((out / 'tasks.json').read_text(encoding='utf-8'))
        assert written['flags'] == list(at.FLAGS)
        assert 'no-extent' in written['flags']

    def test_the_refresh_happens_even_when_nothing_is_narrowed(self, tmp_path):
        """The refresh lives in `write_subset`, not `subset_tasks`, precisely so a run with neither
        --measurable-only nor --n cannot be the one path that emits a stale protocol."""
        stale = _tasks(['c:1'], flags=('object-absent',))
        src = _rendered_dir(tmp_path, stale)
        out = tmp_path / 'out'
        asub.write_subset(stale, str(src), str(out))          # no subset_tasks call at all
        assert json.loads((out / 'tasks.json').read_text(encoding='utf-8'))['flags'] == list(at.FLAGS)

    def test_the_written_task_count_matches_the_task_list(self, tmp_path):
        tasks = _tasks(['c:1', 'c:2', 'c:3'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        asub.write_subset(asub.subset_tasks(tasks, ['c:2']), str(src), str(out))
        written = json.loads((out / 'tasks.json').read_text(encoding='utf-8'))
        assert written['n_tasks'] == len(written['tasks']) == 1

    def test_it_carries_the_view_fraction_through(self, tmp_path):
        """The one instrument constant that is NOT re-read from code, because it belongs to the geometry
        the tiles were cut at: a 60 deg tile opened at a third is the 20 deg view §4 specifies, and a
        subset of those tiles inherits it. Losing it opens every tile at 3x the intended scale."""
        tasks = _tasks(['c:1'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        asub.write_subset(tasks, str(src), str(out))
        written = json.loads((out / 'tasks.json').read_text(encoding='utf-8'))
        assert written['initial_view_fraction'] == pytest.approx(1 / 3)


class TestBlindnessOfTheProducedDirectory:
    def test_the_subset_tasks_leak_nothing_the_server_would_reject(self, tmp_path):
        """Cross-checked against the server's own audit rather than restated, so the two cannot drift:
        whatever `annotate_server.load_tasks` refuses to serve, this must not write."""
        import annotate_server
        tasks = _tasks(['c:1', 'c:2'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        asub.write_subset(asub.subset_tasks(tasks, ['c:1']), str(src), str(out))
        annotate_server.load_tasks(str(out))          # raises if the subset leaked an anchor


class TestMain:
    @pytest.fixture
    def corpus_csv(self, tmp_path):
        corpus = _corpus([
            ('c:1', 'CurbRamp', '[]', '<5', True),
            ('c:2', 'Signal', '[]', '<5', True),
            ('c:3', 'Obstacle', '[stairs]', '>30', True),
            ('c:4', 'Obstacle', '[pole]', '>30', True),
        ])
        path = tmp_path / 'corpus.csv'
        corpus.to_csv(path, index=False)
        return str(path)

    def test_measurable_only_applies_the_live_rule_end_to_end(self, tmp_path, corpus_csv, capsys):
        tasks = _tasks(['c:1', 'c:2', 'c:3', 'c:4'])
        src = _rendered_dir(tmp_path, tasks)
        out = tmp_path / 'out'
        assert asub.main([str(src), '--corpus', corpus_csv, '--out-dir', str(out),
                          '--measurable-only']) == 0
        written = json.loads((out / 'tasks.json').read_text(encoding='utf-8'))
        assert [t['label_uid'] for t in written['tasks']] == ['c:1', 'c:4'], \
            'Signal by type, stairs by tag'

    def test_n_without_seed_is_refused(self, tmp_path, corpus_csv):
        """An unseeded draw cannot be re-derived from the artifact, so the subset would be unreplicable
        the moment the directory is deleted."""
        tasks = _tasks(['c:1'])
        src = _rendered_dir(tmp_path, tasks)
        with pytest.raises(SystemExit):
            asub.main([str(src), '--corpus', corpus_csv, '--out-dir', str(tmp_path / 'o'), '--n', '1'])

    def test_a_mismatched_corpus_is_an_error_not_a_silent_empty_draw(self, tmp_path, corpus_csv):
        """Pointing at the wrong --corpus would otherwise select nothing and write an empty queue, which
        looks like 'the filter was strict' rather than 'these files do not go together'."""
        tasks = _tasks(['other:99'])
        src = _rendered_dir(tmp_path, tasks)
        with pytest.raises(SystemExit):
            asub.main([str(src), '--corpus', corpus_csv, '--out-dir', str(tmp_path / 'o')])

    def test_the_subset_of_a_subset_stays_a_subset(self, tmp_path, corpus_csv):
        """How the real run is built: corpus -> measurable -> Jon's 50. The second stage reads the first
        stage's output, so its tiles have to be there to copy."""
        tasks = _tasks(['c:1', 'c:2', 'c:3', 'c:4'])
        src = _rendered_dir(tmp_path, tasks)
        mid, final = tmp_path / 'mid', tmp_path / 'final'
        asub.main([str(src), '--corpus', corpus_csv, '--out-dir', str(mid), '--measurable-only'])
        asub.main([str(mid), '--corpus', corpus_csv, '--out-dir', str(final),
                   '--n', '1', '--seed', '11'])
        written = json.loads((final / 'tasks.json').read_text(encoding='utf-8'))
        assert len(written['tasks']) == 1
        assert written['tasks'][0]['label_uid'] in {'c:1', 'c:4'}
        assert (final / written['tasks'][0]['tile']).exists()
