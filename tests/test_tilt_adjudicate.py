"""Tests for reports/scripts/tilt_adjudicate.py — endpoint C of the #54 tilt study: a blind
three-way forced choice between the production crop window centred at the stored pano_y, the same
window where the #4784 leak would put the feature (the rig pixel, y - T(b)*h/180 in F1's measured
sign), and its mirror (y + T(b)*h/180)."""

import builtins
import hashlib
import json
import os
import shutil
import sys

import numpy as np
import pandas as pd
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import CropRunner  # noqa: E402
import tilt_adjudicate as ta  # noqa: E402
import tilt_geometry as tg  # noqa: E402

W, H = 2048, 1024
ADJ_DIR = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-tilt-adjudication')
REPORT_MD = os.path.join(REPO_ROOT, 'reports', '2026-09-26-tilt-error-study.md')


def _label(uid, pano_id, pano_x, pano_y, label_type='CurbRamp', era='post179', measurable=True, tags='[]'):
    city, lid = uid.split(':')
    return dict(label_uid=uid, city=city, label_id=int(lid), pano_id=pano_id, label_type=label_type, tags=tags,
                era=era, pano_x=float(pano_x), pano_y=float(pano_y), pano_width=float(W), pano_height=float(H),
                measurable=measurable)


def _pose(pano_id, city='seattle-wa', npz=None, xml=None, jpg=(W, H)):
    row = dict(city=city, pano_id=pano_id, jpg_present=1, jpg_width=jpg[0], jpg_height=jpg[1],
               npz_present=0, pitch_deg=np.nan, roll_deg=np.nan, xml_present=0, xml_pano_yaw_deg=np.nan,
               xml_tilt_yaw_deg=np.nan, xml_tilt_pitch_deg=np.nan)
    if npz is not None:
        row.update(npz_present=1, pitch_deg=npz[0], roll_deg=npz[1])
    if xml is not None:
        yaw, tyaw, tp = tg.pitch_roll_to_xml_tilt(100.0, *xml)
        row.update(xml_present=1, xml_pano_yaw_deg=float(yaw), xml_tilt_yaw_deg=float(tyaw),
                   xml_tilt_pitch_deg=float(tp))
    return row


def test_windows_are_stored_plus_minus_T_in_pixels():
    c = ta.window_centres(600.0, 4.0, 1024)
    assert c == {'stored': 600.0, 'leak': 600.0 - 4.0 * 1024 / 180, 'antileak': 600.0 + 4.0 * 1024 / 180}
    pano = Image.fromarray(np.zeros((H, W, 3), np.uint8))
    wins = ta.cut_windows(pano, 1000.0, 600.0, 4.0)
    width = int(round(CropRunner.crop_window_width(600.0, W, H)))
    for name, (box, crop, marker) in wins.items():
        assert box.width == width                       # same size for all three: size cannot leak identity
        assert not box.shifted
        assert box.top + box.height / 2 == pytest.approx(c[name], abs=1.0)
        assert crop.size == (box.width, box.height)


def _synthetic_run(tmp_path, n=6):
    rng = np.random.default_rng(0)
    root = tmp_path / 'panos'
    labels = []
    for i in range(n):
        pid = 'ab%04d' % i
        (root / 'seattle-wa' / 'ab').mkdir(parents=True, exist_ok=True)
        Image.fromarray(rng.integers(0, 255, (H, W, 3), dtype=np.uint8)).save(
            root / 'seattle-wa' / 'ab' / (pid + '.jpg'))
        labels.append(dict(_label('seattle-wa:%d' % (100 + i), pid, 1500 + i, 600 + i), T_deg=5.0, dbear=0.0, pitch_deg=5.0,
                           roll_deg=0.0, era_arm='post179', pose_source='npz', scrape_era='modern'))
    out = tmp_path / 'adj'
    ta.build_sheets(pd.DataFrame(labels), str(root), str(out), seed='t')
    return labels, out


def test_order_is_random_per_label_and_kept_only_in_the_key(tmp_path):
    labels, out = _synthetic_run(tmp_path, n=12)
    sheets = sorted(os.listdir(out / 'sheets'))
    assert len(sheets) == 12
    for name in sheets:
        for lab in labels:
            assert str(lab['label_id']) not in name and lab['pano_id'] not in name
    tasks = json.load(open(out / 'tasks.json', encoding='utf-8'))
    for t in tasks.values():
        assert set(t) == {'label_type', 'tags'}          # a whitelist: an order leaked as indices fails
    assert not (out / 'key.json').exists()
    key = json.load(open(out / 'sealed' / 'key.json', encoding='utf-8'))
    orders = {tuple(v['order']) for v in key.values()}
    assert len(orders) >= 3
    for v in key.values():
        assert sorted(v['order']) == ['antileak', 'leak', 'stored']


def test_record_and_next_never_reveal_the_key(tmp_path, capsys):
    _, out = _synthetic_run(tmp_path, n=3)
    token = ta.next_unjudged(str(out), 'j')
    assert token is not None
    ta.record(str(out), token, 'B', 'j')
    capsys.readouterr()
    assert ta.next_unjudged(str(out), 'j') != token
    verdicts = ta.load_verdicts(str(out), 'j')
    assert verdicts == {token: 'B'}
    with pytest.raises(ValueError):
        ta.record(str(out), token, 'D', 'j')


def _corpus(rows):
    return pd.DataFrame(rows)


def test_selection_uses_the_live_measurable_rule():
    """The CSV says measurable, the live rule says a Signal has no referent: excluded."""
    corpus = _corpus([_label('seattle-wa:1', 'abP1', 0, 700, label_type='Signal', measurable=True),
                      _label('seattle-wa:2', 'abP1', 0, 700, label_type='CurbRamp', measurable=False)])
    pose = pd.DataFrame([_pose('abP1', npz=(6.0, 0.0))])
    c = ta.candidates(corpus, pose)
    assert list(c['label_uid']) == ['seattle-wa:2']


def test_selection_threshold_on_T_not_on_pitch():
    """#5174's gap as a test: pitch 9 deg but the label is at b = +90 (pano_x = 3/4 w), roll 0 -> T ~ 0."""
    corpus = _corpus([_label('seattle-wa:1', 'abP1', 0.75 * W, 700),
                      _label('seattle-wa:2', 'abP1', 0.5 * W, 700)])
    pose = pd.DataFrame([_pose('abP1', npz=(9.0, 0.0))])
    c = ta.candidates(corpus, pose)
    assert list(c['label_uid']) == ['seattle-wa:2']
    assert c['T_deg'].iloc[0] == pytest.approx(9.0)


def test_pose_source_matches_scrape_era():
    corpus = _corpus([_label('seattle-wa:1', 'abX', 0.5 * W, 700), _label('seattle-wa:2', 'abN', 0.5 * W, 700),
                      _label('seattle-wa:3', 'abZ', 0.5 * W, 700)])
    pose = pd.DataFrame([_pose('abX', npz=(-6.0, 0.0), xml=(6.0, 0.0)),   # 2019 stitch, re-posed since
                         _pose('abN', npz=(6.0, 0.0)),
                         _pose('abZ')])                                   # no pose at all
    c = ta.candidates(corpus, pose, min_abs_T=0.0).set_index('label_uid')
    assert c.loc['seattle-wa:1', 'pose_source'] == 'xml'
    assert c.loc['seattle-wa:1', 'pitch_deg'] == pytest.approx(6.0)
    assert c.loc['seattle-wa:2', 'pose_source'] == 'npz'
    assert 'seattle-wa:3' not in c.index


def test_dims_mismatch_is_ineligible():
    corpus = _corpus([_label('seattle-wa:1', 'abP1', 0.5 * W, 700)])
    pose = pd.DataFrame([_pose('abP1', npz=(6.0, 0.0), jpg=(W * 2, H * 2))])
    assert len(ta.candidates(corpus, pose)) == 0


def test_draw_is_stratified_and_reports_the_shortfall():
    rows = [dict(label_uid='c:%d' % i, era_arm='legacy+mid' if i < 30 else 'post179') for i in range(40)]
    sel, short = ta.draw(pd.DataFrame(rows), 24, 's')
    assert (sel['era_arm'] == 'legacy+mid').sum() == 24
    assert (sel['era_arm'] == 'post179').sum() == 10
    assert short == {'legacy+mid': 0, 'post179': 14}
    again, _ = ta.draw(pd.DataFrame(rows[::-1]), 24, 's')
    assert sorted(again['label_uid']) == sorted(sel['label_uid'])


def test_score_counts_and_binomial():
    key = {('t%d' % i): {'order': ['stored', 'leak', 'antileak'], 'era_arm': 'post179'} for i in range(48)}
    s = ta.score({t: 'A' for t in key}, key)
    arm = s['arms']['post179']
    assert arm['stored'] == 48 and arm['n'] == 48
    assert arm['p_stored_vs_third'] < 1e-15
    even = {t: 'ABC'[i % 3] for i, t in enumerate(sorted(key))}
    arm = ta.score(even, key)['arms']['post179']
    assert (arm['stored'], arm['leak'], arm['antileak']) == (16, 16, 16)
    assert arm['p_stored_vs_third'] > 0.05


def test_none_is_its_own_bucket():
    key = {'a': {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x'},
           'b': {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x'}}
    arm = ta.score({'a': 'B', 'b': 'none'}, key)['arms']['x']
    assert arm['n'] == 2 and arm['stored'] == 1 and arm['none'] == 1
    assert arm['share_stored'] == pytest.approx(0.5)


def test_leak_window_is_the_rig_pixel():
    """The 'leak' centre is tilt_geometry's exact rig pixel to first order, so a sign flip in either
    module fails here rather than silently swapping the two shifted windows."""
    pano_x, pano_y, p, r = 0.5 * W, 600.0, 5.0, 0.0
    b = pano_x / W * 360 - 180
    T = tg.tilt_term_deg(b, p, r)
    _, y_rig = tg.rig_pixel_from_gravity_pixel(pano_x, pano_y, W, H, p, r)
    assert ta.window_centres(pano_y, T, H)['leak'] == pytest.approx(float(y_rig), abs=0.5)


def test_extract_copy_agrees_with_the_cropper():
    """tilt_remote_crop runs on the store host without the repo, so it carries a copy of
    CropRunner.extract_crop; a seam-crossing window is the case the copy must not get wrong."""
    import tilt_remote_crop as trc
    rng = np.random.default_rng(1)
    pano = Image.fromarray(rng.integers(0, 255, (H, W, 3), dtype=np.uint8))
    for left, width in ((10, 300), (W - 100, 300), (W - 1, 50)):
        a = np.asarray(CropRunner.extract_crop(pano, left, 200, width, 200))
        b = np.asarray(trc.extract_crop(pano, left, 200, width, 200))
        assert np.array_equal(a, b)


def test_remote_panels_make_the_same_sheet_as_a_local_cut(tmp_path):
    """The store-side arm is cut remotely from boxes computed here; the sheet it yields must be
    pixel-identical to the one a local cut of the same pano yields."""
    import tilt_remote_crop as trc
    labels, out_local = _synthetic_run(tmp_path, n=2)
    labels[0]['pano_x'] = W - 30.0              # a seam-crossing window
    sel = pd.DataFrame(labels)
    root = tmp_path / 'panos'
    local = tmp_path / 'local'
    ta.build_sheets(sel, str(root), str(local), seed='t')
    jobs = tmp_path / 'jobs.csv'
    ta.crop_jobs(sel, seed='t').to_csv(jobs, index=False)
    panels = tmp_path / 'panels'
    assert trc.main(['--jobs', str(jobs), '--store', str(root), '--out', str(panels)]) == 0
    remote = tmp_path / 'remote'
    ta.build_sheets(sel.assign(source='store'), None, str(remote), seed='t', panel_dir=str(panels))
    for name in os.listdir(local / 'sheets'):
        a = np.asarray(Image.open(local / 'sheets' / name), dtype=int)
        b = np.asarray(Image.open(remote / 'sheets' / name), dtype=int)
        assert np.abs(a - b).max() <= 2        # identical panels; only JPEG re-encode noise
    strip = lambda key: {t: {k: v for k, v in e.items() if k != 'source'} for t, e in key.items()}  # noqa: E731
    assert strip(ta.read_sealed_key(str(local))) == strip(ta.read_sealed_key(str(remote)))


def test_draw_with_fill_tops_up_each_arm_from_the_fill_pool():
    prim = pd.DataFrame([dict(label_uid='a:%d' % i, era_arm='legacy+mid' if i < 20 else 'post179')
                         for i in range(22)])
    fill = pd.DataFrame([dict(label_uid='s:%d' % i, era_arm='legacy+mid' if i < 50 else 'post179')
                         for i in range(100)] + [dict(label_uid='a:0', era_arm='legacy+mid')])
    sel, info = ta.draw_with_fill(prim, fill, 24, 's')
    assert (sel['era_arm'] == 'legacy+mid').sum() == 24 and (sel['era_arm'] == 'post179').sum() == 24
    assert sel['label_uid'].is_unique
    assert (sel['source'] == 'corpus').sum() == 22
    assert info['from_fill'] == {'legacy+mid': 4, 'post179': 22}


# ---- the blind (#158 review item 1) -------------------------------------------------------------

def test_next_and_record_never_open_the_key(tmp_path, monkeypatch):
    """Walks the whole queue with open() guarded: the judge path must never read a key file, and must
    serve tokens in sorted order (a `next` that read the key could serve leak-first sheets first)."""
    _, out = _synthetic_run(tmp_path, n=3)
    real_open = builtins.open

    def guarded(path, *a, **k):
        assert 'key' not in os.path.basename(str(path)).replace('key.sha256', ''), \
            'the judge path opened %s' % path
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, 'open', guarded)
    seen = []
    while True:
        t = ta.next_unjudged(str(out), 'j')
        if t is None:
            break
        seen.append(t)
        ta.record(str(out), t, 'A', 'j')
    monkeypatch.setattr(builtins, 'open', real_open)
    assert seen == sorted(json.load(open(out / 'tasks.json', encoding='utf-8')))


@pytest.mark.parametrize('where', ['key.json', 'old/key.json', 'KEY-backup.json', 'sheets/key.json'])
def test_next_and_record_refuse_while_a_key_is_readable(tmp_path, where):
    _, out = _synthetic_run(tmp_path, n=2)
    token = ta.next_unjudged(str(out), 'j')                 # sealed: runs
    stray = out / where
    stray.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(out / 'sealed' / 'key.json', stray)
    with pytest.raises(ta.BlindBroken):
        ta.next_unjudged(str(out), 'j')
    with pytest.raises(ta.BlindBroken):
        ta.record(str(out), token, 'A', 'j')
    assert ta.load_verdicts(str(out), 'j') == {}             # nothing was recorded
    stray.unlink()
    assert ta.next_unjudged(str(out), 'j') == token


def test_the_cli_refuses_too(tmp_path, capsys):
    _, out = _synthetic_run(tmp_path, n=2)
    shutil.copyfile(out / 'sealed' / 'key.json', out / 'key.json')
    with pytest.raises(ta.BlindBroken):
        ta.main(['next', '--out', str(out), '--judge', 'j'])


def test_a_later_record_supersedes(tmp_path):
    _, out = _synthetic_run(tmp_path, n=2)
    t = ta.next_unjudged(str(out), 'j')
    ta.record(str(out), t, 'A', 'j')
    ta.record(str(out), t, 'none', 'j')
    assert ta.load_verdicts(str(out), 'j') == {t: 'none'}


def test_the_sealed_key_is_checked_against_its_hash(tmp_path):
    _, out = _synthetic_run(tmp_path, n=2)
    key = ta.read_sealed_key(str(out))
    token = sorted(key)[0]
    key[token]['order'] = key[token]['order'][::-1]
    with open(out / 'sealed' / 'key.json', 'w', encoding='utf-8') as f:
        json.dump(key, f, indent=1, sort_keys=True)
    with pytest.raises(ValueError):
        ta.read_sealed_key(str(out))


def test_seal_migrates_an_old_folder(tmp_path):
    """A folder written before sealing (key.json and a judge's verdicts beside the sheets)."""
    _, out = _synthetic_run(tmp_path, n=2)
    key = ta.read_sealed_key(str(out))
    shutil.rmtree(out / 'sealed')
    (out / ta.KEY_HASH).unlink()
    with open(out / 'key.json', 'w', encoding='utf-8') as f:
        json.dump(key, f)
    with open(out / 'verdicts_m.jsonl', 'w', encoding='utf-8') as f:
        f.write(json.dumps({'token': sorted(key)[0], 'choice': 'A', 'judge': 'm'}) + '\n')
    ta.main(['seal', '--out', str(out), '--move-verdicts', 'm'])
    assert not (out / 'key.json').exists() and not (out / 'verdicts_m.jsonl').exists()
    assert ta.read_sealed_key(str(out)) == key
    assert ta.load_verdicts(str(out / 'sealed'), 'm') == {sorted(key)[0]: 'A'}
    ta.next_unjudged(str(out), 'jon')


class TestTheCommittedFolder:
    """The folder Jon judges in, as committed."""

    def test_no_key_outside_sealed(self):
        assert ta.unsealed_key_files(ADJ_DIR) == []
        assert not os.path.exists(os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-26-tilt-adjudication.json'))

    def test_the_hash_matches_the_sealed_key(self):
        """Reads only the committed hash, the sealed salt and the sealed key's bytes."""
        with open(os.path.join(ADJ_DIR, ta.KEY_HASH), encoding='ascii') as f:
            expected = f.read().strip()
        with open(os.path.join(ADJ_DIR, 'sealed', 'salt.txt'), encoding='ascii') as f:
            salt = f.read().strip()
        with open(os.path.join(ADJ_DIR, 'sealed', 'key.json'), 'rb') as f:
            key_bytes = f.read()
        assert len(expected) == 64 and len(salt) == 32
        assert hashlib.sha256(salt.encode('ascii') + b':' + key_bytes).hexdigest() == expected
        assert len(ta.read_sealed_key(ADJ_DIR)) == 48    # and the module's own check agrees

    def test_the_readmes(self):
        with open(os.path.join(ADJ_DIR, 'README.md'), encoding='utf-8') as f:
            judge = f.read()
        assert judge == ta.JUDGE_README.format(out='reports/data/2026-09-26-tilt-adjudication')
        assert 'nothing else in this folder' in judge.splitlines()[0]
        with open(os.path.join(ADJ_DIR, 'sealed', 'README.md'), encoding='utf-8') as f:
            assert f.readline().strip() == 'Do not open until you have recorded all 48 verdicts.'

    def test_the_machine_verdicts_are_sealed(self):
        assert not [n for n in os.listdir(ADJ_DIR) if n.startswith('verdicts_')]
        assert len(ta.load_verdicts(os.path.join(ADJ_DIR, 'sealed'), 'claude-opus-5-5')) == 48

    def test_tasks_json_is_a_whitelist(self):
        with open(os.path.join(ADJ_DIR, 'tasks.json'), encoding='utf-8') as f:
            tasks = json.load(f)
        assert len(tasks) == 48 == len(os.listdir(os.path.join(ADJ_DIR, 'sheets')))
        for t in tasks.values():
            assert set(t) == {'label_type', 'tags'}

    def test_the_report_prints_no_key_and_its_results_follow_the_instructions(self):
        with open(REPORT_MD, encoding='utf-8') as f:
            text = f.read()
        assert 'key:' not in text and 'A=leak' not in text and 'adjudication-sheet.jpg' not in text
        assert text.index('How to adjudicate') < text.index('Preliminary machine pass')


# ---- the tests lens's kill-tests (#158 review): XML-era roll, the |T| boundary, the fill ----------

def test_xml_era_roll_comes_from_the_xml():
    """Roll carries T beside the car, so an XML-era JPEG must not take its roll from a 2026 npz."""
    pose = pd.DataFrame([_pose('abX', npz=(0.0, -6.0), xml=(0.0, 6.0))])
    p = ta.attach_pose(pose).iloc[0]
    assert p['pose_source'] == 'xml' and p['pose_roll_deg'] == pytest.approx(6.0)


def test_threshold_excludes_T_just_under_four_and_keeps_four():
    corpus = _corpus([_label('seattle-wa:1', 'abP', 0.5 * W, 700), _label('seattle-wa:2', 'abQ', 0.5 * W, 700)])
    pose = pd.DataFrame([_pose('abP', npz=(3.99, 0.0)), _pose('abQ', npz=(4.0, 0.0))])
    assert list(ta.candidates(corpus, pose)['label_uid']) == ['seattle-wa:2']


def test_fill_never_repeats_a_primary_label():
    prim = pd.DataFrame([dict(label_uid='a:%d' % i, era_arm='legacy+mid') for i in range(3)])
    fill = pd.DataFrame([dict(label_uid='a:%d' % i, era_arm='legacy+mid') for i in range(3)]
                        + [dict(label_uid='s:%d' % i, era_arm='legacy+mid') for i in range(3)])
    sel, _ = ta.draw_with_fill(prim, fill, 6, 's')
    assert sel['label_uid'].is_unique and len(sel) == 6
