"""The endpoint-C redraw's pool rule and draw (reports/scripts/tilt_jm_pool.py), and the judging page's
blind gate (reports/scripts/tilt_adjudicate_ui.py)."""
import json
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports', 'scripts'))
import tilt_adjudicate as ta  # noqa: E402
import tilt_adjudicate_ui as ui  # noqa: E402
import tilt_jm_pool as jm  # noqa: E402

JON, MIKEY = list(jm.LEAD_LABELLERS)


def _v(*pairs):
    return json.dumps([{'user_id': u, 'validation': v} for u, v in pairs])


class TestThePoolRule:
    def test_lead_votes_ignore_everyone_else(self):
        assert jm.lead_votes(_v((JON, 'Agree'), ('someone', 'Disagree'))) == {'jon': 'Agree'}
        assert jm.lead_votes(None) == {} and jm.lead_votes('not json') == {}

    @pytest.mark.parametrize('made,votes,ok', [
        (True, {}, True),                                   # made by a lead labeller
        (False, {'jon': 'Agree'}, True),                    # vouched for by one
        (False, {'mikey': 'Unsure'}, False),                # unsure is not a vouch
        (False, {}, False),                                 # nobody vouched
        (True, {'mikey': 'Disagree'}, False),               # a lead Disagree vetoes even their own label
        (False, {'jon': 'Agree', 'mikey': 'Disagree'}, False),
    ])
    def test_vouched(self, made, votes, ok):
        assert jm.vouched(made, votes) is ok


def _cands(rows):
    return pd.DataFrame(rows, columns=['label_uid', 'pano_id', 'city', 'label_type', 'era_arm'])


class TestTheDraw:
    def test_one_label_per_pano_and_excluded_panos_stay_out(self):
        c = _cands([('x:%d' % k, 'p%d' % (k // 2), 'c', 'CurbRamp', 'legacy+mid') for k in range(10)])
        s = jm.stratified_draw(c, 's', per_type=6, city_cap=99, exclude_panos={'p0'})
        assert s['pano_id'].is_unique and 'p0' not in set(s['pano_id']) and len(s) == 4

    def test_a_flat_city_cap_binds_and_the_cell_cap_fills_the_rest(self):
        c = _cands([('x:%d' % k, 'p%d' % k, 'only-city', 'CurbRamp', 'post179') for k in range(10)])
        assert len(jm.stratified_draw(c, 's', per_type=6, city_cap=4)) == 4
        assert len(jm.stratified_draw(c, 's', per_type=6, cell_cap=2)) == 6       # the fill pass

    def test_the_cell_cap_spreads_before_it_fills(self):
        rows = [('a:%d' % k, 'pa%d' % k, 'big', 'CurbRamp', 'post179') for k in range(10)]
        rows += [('b:%d' % k, 'pb%d' % k, 'small', 'CurbRamp', 'post179') for k in range(2)]
        s = jm.stratified_draw(_cands(rows), 's', per_type=4, cell_cap=2)
        assert s['city'].value_counts().to_dict() == {'big': 2, 'small': 2}

    def test_the_draw_is_a_function_of_the_seed(self):
        c = _cands([('x:%d' % k, 'p%d' % k, 'c%d' % (k % 3), 'Obstacle', 'legacy+mid') for k in range(30)])
        a = jm.stratified_draw(c, 's1', per_type=6, city_cap=99)['label_uid'].tolist()
        assert a == jm.stratified_draw(c, 's1', per_type=6, city_cap=99)['label_uid'].tolist()
        assert a != jm.stratified_draw(c, 's2', per_type=6, city_cap=99)['label_uid'].tolist()


def _batch(tmp_path):
    out = tmp_path / 'adj'
    (out / 'sheets').mkdir(parents=True)
    (out / 'tasks.json').write_text(json.dumps({'t1': {'label_type': 'CurbRamp', 'tags': '[]'}}))
    (out / 'draw.json').write_text(json.dumps({'min_abs_t_deg': 6.0}))
    return out


class TestThePage:
    def test_a_batch_serves_tasks_verdicts_and_notes(self, tmp_path):
        out = _batch(tmp_path)
        ta.record(str(out), 't1', 'A=C', 'jon', comment='ramp spans both')
        s = ui.Batch(str(out), 'jon').state()
        assert s['min_abs_t'] == 6.0 and s['order'] == ['t1']
        assert s['verdicts'] == {'t1': 'A=C'} and s['comments'] == {'t1': 'ramp spans both'}

    def test_a_batch_with_an_unsealed_key_serves_nothing(self, tmp_path):
        out = _batch(tmp_path)
        (out / 'key.json').write_text('{}')
        s = ui.Batch(str(out), 'jon').state()
        assert 'error' in s and 'tasks' not in s and 'verdicts' not in s

    def test_the_page_offers_every_choice_the_tool_accepts(self):
        for c in ta.CHOICES:
            assert "'%s'" % c in ui.PAGE


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(REPO_ROOT, 'reports', 'data')


def test_the_beta_draw_rebuilds_from_committed_data(tmp_path, capsys):
    """#194 review finding 6: `draw --design beta` must cut the beta windows. Rebuilding the committed batch
    through the CLI pins it: with `ta.DESIGNS[args.design]` dropped, crop_jobs.csv names C's windows and
    this fails."""
    beta = os.path.join(DATA, '2026-09-29-tilt-beta-jm')
    out = tmp_path / 'beta'
    jm.main(['draw', '--pool', os.path.join(DATA, '2026-09-29-tilt-jm-pool.csv.gz'),
             '--pose', os.path.join(DATA, '2026-09-29-tilt-pose-jm.csv.gz'), '--design', 'beta',
             '--seed', 'beta20260929', '--min-abs-t', '5', '--cell-cap', '2',
             '--exclude', os.path.join(DATA, '2026-09-29-tilt-adjudication-jm'),
             '--exclude', os.path.join(DATA, '2026-09-30-tilt-adjudication-jm-b2'), '--out', str(out)])
    capsys.readouterr()
    # Values, not bytes: on CI (Ubuntu / py3.10) a derived float's last printed digit differs from the
    # Windows build that wrote the committed file (selection.csv byte 22411, '2' against '3').
    for name in ('selection.csv', 'crop_jobs.csv'):
        a = pd.read_csv(out / 'sealed' / name, dtype={'pano_id': str})
        b = pd.read_csv(os.path.join(beta, 'sealed', name), dtype={'pano_id': str})
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-9, atol=0, obj=name)
    jobs = pd.read_csv(out / 'sealed' / 'crop_jobs.csv')
    assert set(jobs['window']) == {'b050', 'b100', 'b150'}
    with open(out / 'draw.json', encoding='utf-8') as a, open(os.path.join(beta, 'draw.json'), encoding='utf-8') as b:
        assert json.load(a) == json.load(b)
