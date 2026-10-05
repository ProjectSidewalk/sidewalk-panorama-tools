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


def test_a_tie_is_its_own_bucket_and_names_its_pair():
    key = {t: {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x'} for t in 'abc'}
    arm = ta.score({'a': 'A=B', 'b': 'B=C', 'c': 'A'}, key)['arms']['x']
    assert arm['n'] == 3 and arm['tie'] == 2 and arm['leak'] == 1
    assert arm['stored'] == 0 and arm['antileak'] == 0 and arm['none'] == 0
    assert arm['tie_pairs'] == {'leak=stored': 1, 'antileak=stored': 1}


def test_pairwise_reads_a_tie_by_the_window_it_left_out():
    """stored=leak says antileak lost; leak=antileak says nothing about leak against antileak."""
    key = {t: {'order': ['leak', 'stored', 'antileak'], 'era_arm': 'x'} for t in 'abcd'}
    pw = ta.score({'a': 'A=B', 'b': 'A=C', 'c': 'C', 'd': 'none'}, key)['arms']['x']['pairwise']
    assert pw.get('leak>antileak') == 1          # only 'a': 'b' holds both, 'd' holds neither
    assert pw.get('antileak>leak') == 1          # 'c'
    assert pw.get('stored>antileak') == 1 and pw.get('leak>stored') == 1


def test_chosen_windows_follows_the_sheet_order():
    order = ['antileak', 'leak', 'stored']
    assert ta.chosen_windows('B', order) == {'leak'}
    assert ta.chosen_windows('A=C', order) == {'antileak', 'stored'}
    assert ta.chosen_windows('none', order) == frozenset()


def _one_task_folder(tmp_path, tokens=('t1',)):
    out = tmp_path / 'adj'
    (out / 'sheets').mkdir(parents=True)
    (out / 'tasks.json').write_text(json.dumps({t: {} for t in tokens}))
    return str(out)


def test_every_tie_choice_is_accepted_by_record(tmp_path):
    out = _one_task_folder(tmp_path)
    for c in ta.TIE_CHOICES:
        ta.record(out, 't1', c, 'jon')
    assert ta.load_verdicts(out, 'jon') == {'t1': 'B=C'}


def test_a_comment_rides_with_the_verdict_and_survives_a_later_bare_record(tmp_path):
    out = _one_task_folder(tmp_path, ('t1', 't2'))
    ta.record(out, 't1', 'A=B', 'jon', comment='  both on the ramp  ')
    ta.record(out, 't1', 'A=B', 'jon')
    ta.record(out, 't2', 'none', 'jon', comment='   ')
    assert ta.load_comments(out, 'jon') == {'t1': 'both on the ramp'}
    with open(os.path.join(out, 'verdicts_jon.jsonl'), encoding='utf-8') as f:
        assert 'comment' not in json.loads(f.read().splitlines()[2])


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
        assert np.abs(a - b).max() == 0        # lossless panels and a deterministic encode: identical
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


def _unsealed_machine_verdicts(folder):
    """Machine-judge verdict files (verdicts_claude-*) outside sealed/: they would anchor a human judge."""
    return sorted(n for n in os.listdir(folder) if n.startswith('verdicts_claude-'))


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
        # Bind the count, not the dict: a failing `len({...}) == 48` would print the key (#158 final review).
        n_key = len(ta.read_sealed_key(ADJ_DIR))       # and the module's own check agrees
        assert n_key == 48, n_key

    def test_the_readmes(self):
        with open(os.path.join(ADJ_DIR, 'README.md'), encoding='utf-8') as f:
            judge = f.read()
        # The folder is a record, written while the tilt rules were still a CLAUDE.md subsection; the template
        # names their new home (.claude/rules/tilt.md, #192) and the record keeps the words its judge read.
        template = ta.JUDGE_README.replace("`.claude/rules/tilt.md`", "CLAUDE.md's tilt subsection")
        assert judge == template.format(out='reports/data/2026-09-26-tilt-adjudication')
        assert 'nothing else in this folder' in judge.splitlines()[0]
        with open(os.path.join(ADJ_DIR, 'sealed', 'README.md'), encoding='utf-8') as f:
            assert f.readline().strip() == 'Do not open until you have recorded all 48 verdicts.'

    def test_the_machine_verdicts_are_sealed(self):
        """Only machine verdicts are banned from the working folder: a human judge's
        verdicts_<name>.jsonl is exactly what the README tells them to commit there (#158 final review)."""
        assert _unsealed_machine_verdicts(ADJ_DIR) == []
        n_verdicts = len(ta.load_verdicts(os.path.join(ADJ_DIR, 'sealed'), 'claude-opus-5-5'))
        assert n_verdicts == 48, n_verdicts             # a count, never the verdicts themselves

    def test_a_human_judges_committed_verdicts_break_no_committed_folder_test(self, tmp_path):
        """Following the README (record as jon, commit) must leave the folder invariants green."""
        d = tmp_path / 'adj'
        d.mkdir()
        (d / 'verdicts_jon.jsonl').write_text('{"token": "t", "choice": "A", "judge": "jon"}\n')
        assert _unsealed_machine_verdicts(str(d)) == []
        (d / 'verdicts_claude-opus-5-5.jsonl').write_text('')
        assert _unsealed_machine_verdicts(str(d)) == ['verdicts_claude-opus-5-5.jsonl']

    def test_step_5_commits_the_verdicts_and_reruns_analyze(self):
        text = ta.JUDGE_README
        step5 = ' '.join(text[text.index('\n5. '):].split('\n\n')[0].split())
        assert 'verdicts_<you>.jsonl' in step5
        assert 'python reports/scripts/tilt_error_study.py analyze' in step5

    def test_tasks_json_is_a_whitelist(self):
        with open(os.path.join(ADJ_DIR, 'tasks.json'), encoding='utf-8') as f:
            tasks = json.load(f)
        assert len(tasks) == 48 == len(os.listdir(os.path.join(ADJ_DIR, 'sheets')))
        for t in tasks.values():
            assert set(t) == {'label_type', 'tags'}

    def test_the_report_prints_no_key_and_leads_with_the_decision_bearing_pass(self):
        """Adjudicated 2026-09-29: the judge-first instructions are gone, the key is still never printed,
        and Jon's pass on the redraw comes before the superseded machine pass."""
        with open(REPORT_MD, encoding='utf-8') as f:
            text = f.read()
        assert 'key:' not in text and 'A=leak' not in text and 'adjudication-sheet.jpg' not in text
        assert text.index("Jon's adjudication, on the redraw") < text.index('Superseded first draw')


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


# ---- #158 final review: the judge's own recording path --------------------------------------------

_KEY1 = {'tok1': {'era_arm': 'post179', 'order': ['stored', 'leak', 'antileak'], 'T_deg': 5.0}}


def _one_sheet_folder(root):
    os.makedirs(os.path.join(root, 'sheets'))
    with open(os.path.join(root, 'draw.json'), 'w') as f:
        json.dump({'x': 1}, f)
    with open(os.path.join(root, 'tasks.json'), 'w') as f:
        json.dump({'tok1': {'label_type': 'CurbRamp', 'tags': []}}, f)
    open(os.path.join(root, 'sheets', 'tok1.jpg'), 'wb').close()
    ta.write_key(root, _KEY1)
    ta.write_judge_readme(root)
    return root


class TestJudgeNames:
    def test_a_judge_name_is_normalised_to_lowercase(self, tmp_path):
        d = _one_sheet_folder(str(tmp_path / 'adj'))
        ta.record(d, 'tok1', 'A', 'Jon')
        assert os.listdir(d).count('verdicts_jon.jsonl') == 1
        assert ta.load_verdicts(d, 'JON') == {'tok1': 'A'} == ta.load_verdicts(d, 'jon')
        assert ta.next_unjudged(d, 'Jon') is None

    @pytest.mark.parametrize('bad', ['/../sealed/verdicts_machine', '../sealed/verdicts_machine',
                                     'sealed/x', 'a' + chr(92) + 'b', '..', '', 'jon.bak', 'jon f', 'jón'])
    def test_a_judge_name_outside_the_alphabet_is_refused(self, tmp_path, bad):
        d = _one_sheet_folder(str(tmp_path / 'adj'))
        with pytest.raises(ValueError):
            ta.record(d, 'tok1', 'A', bad)
        with pytest.raises(ValueError):
            ta.next_unjudged(d, bad)
        assert sorted(os.listdir(os.path.join(d, 'sealed'))) == ['README.md', 'key.json', 'salt.txt']
        assert not [n for n in os.listdir(d) if n.startswith('verdicts_')]

    def test_judge_name_cannot_escape_the_working_folder(self, tmp_path):
        """The reproduced Windows escape: '/../sealed/verdicts_x' resolved lexically into sealed/."""
        d = _one_sheet_folder(str(tmp_path / 'adj'))
        with pytest.raises((ValueError, SystemExit)):
            ta.record(d, 'tok1', 'A', '/../sealed/verdicts_machine')
        assert not os.path.exists(os.path.join(d, 'sealed', 'verdicts_machine.jsonl'))
        with pytest.raises(SystemExit):
            ta.main(['record', '--out', d, '--judge', '/../sealed/verdicts_machine', 'tok1', 'A'])
        assert not os.path.exists(os.path.join(d, 'sealed', 'verdicts_machine.jsonl'))


class TestScoreCli:
    def test_score_reads_a_human_judge_from_the_working_folder(self, tmp_path, capsys):
        d = _one_sheet_folder(str(tmp_path / 'adj'))
        ta.record(d, 'tok1', 'B', 'jon')
        ta.main(['score', '--out', d, '--judge', 'jon'])
        out = json.loads(capsys.readouterr().out)
        assert out['n'] == 1 and out['arms']['post179']['leak'] == 1

    def test_score_reads_sealed_only_for_the_machine_and_never_prints_the_key(self, tmp_path, capsys):
        d = _one_sheet_folder(str(tmp_path / 'adj'))
        for judge in ('claude-opus-5-5', 'jon'):
            with open(os.path.join(d, 'sealed', 'verdicts_%s.jsonl' % judge), 'w') as f:
                f.write(json.dumps({'token': 'tok1', 'choice': 'B'}) + '\n')
        ta.main(['score', '--out', d, '--judge', 'claude-opus-5-5'])
        text = capsys.readouterr().out
        assert json.loads(text)['n'] == 1
        for leak in ('order', 'T_deg', 'antileak"]', '5.0'):
            assert leak not in text
        ta.main(['score', '--out', d, '--judge', 'jon'])          # a human judge never reads sealed/
        assert json.loads(capsys.readouterr().out)['n'] == 0


# ---- #158 final review's surviving mutants M09 and M11 ---------------------------------------------

def test_M09_a_nested_sealed_dir_is_not_a_hiding_place(tmp_path):
    """Only the top-level sealed/ is exempt; a key under sheets/sealed/ is still readable."""
    d = _one_sheet_folder(str(tmp_path / 'adj'))
    os.makedirs(os.path.join(d, 'sheets', 'sealed'))
    with open(os.path.join(d, 'sheets', 'sealed', 'key.json'), 'w') as f:
        f.write('{}')
    with pytest.raises(ta.BlindBroken):
        ta.next_unjudged(d, 'jon')


def test_M11_a_fresh_salt_is_128_bits_and_random(tmp_path):
    ta.write_key(str(tmp_path / 'a'), _KEY1)
    ta.write_key(str(tmp_path / 'b'), _KEY1)
    salts = [open(os.path.join(str(tmp_path / x), 'sealed', 'salt.txt')).read().strip() for x in 'ab']
    assert all(len(s) == 32 and int(s, 16) >= 0 for s in salts) and salts[0] != salts[1]


# The machine's aggregate result is also quoted outside the report (#158 final review item 7). The tilt
# rules moved out of CLAUDE.md into .claude/rules/tilt.md on 2026-09-29 (#192), and the README kept naming
# the old place with this list green, so the list is now also checked against the files themselves.
QUOTES_THE_MACHINE_RESULT = ('reports/README.md', 'docs/cropper.md', '.claude/rules/tilt.md')
MACHINE_RESULT = '79 : 0'


def _markdown_quoting_the_result(root=REPO_ROOT, report=REPORT_MD):
    """Every .md in the checkout that quotes C's result, bar the report itself, the judge folders and the
    plans. reports/plans/ is skipped as records (#192 round-2 review R2): a plan written after the result
    quotes it as a premise, is frozen once its work lands, and is not something a judge would open for the
    answer - #193's plan is the first, and naming it in the README would change the template every committed
    judge folder is compared against."""
    found = set()
    skip = {os.path.join(root, '.claude', 'worktrees'), os.path.join(root, 'reports', 'data'),
            os.path.join(root, 'reports', 'plans')}
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in {'.git', '.venv', 'venv', '__pycache__'}
                   and os.path.join(d, x) not in skip]
        for f in files:
            path = os.path.join(d, f)
            if not f.endswith('.md') or os.path.normcase(path) == os.path.normcase(report):
                continue
            with open(path, encoding='utf-8') as fh:
                if MACHINE_RESULT in fh.read():
                    found.add(os.path.relpath(path, root).replace(os.sep, '/'))
    return found


def test_the_result_scan_skips_the_report_the_judge_folders_and_the_plans(tmp_path):
    files = {'reports/the-report.md': MACHINE_RESULT, 'reports/README.md': MACHINE_RESULT,
             'reports/data/judge/README.md': MACHINE_RESULT, 'reports/plans/a-plan.md': MACHINE_RESULT,
             '.claude/rules/tilt.md': MACHINE_RESULT, 'notes/other.md': 'no result here'}
    for rel, text in files.items():
        path = tmp_path.joinpath(*rel.split('/'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    found = _markdown_quoting_the_result(str(tmp_path), str(tmp_path / 'reports' / 'the-report.md'))
    assert found == {'reports/README.md', '.claude/rules/tilt.md'}


def test_the_judge_readme_names_every_file_that_quotes_a_result():
    """Every judge folder's README (the template) still tells a future judge what not to open - by the
    names of the files that actually quote the result today."""
    readme = ' '.join(ta.JUDGE_README.split())
    for name in QUOTES_THE_MACHINE_RESULT:
        assert name in readme
    assert _markdown_quoting_the_result() == set(QUOTES_THE_MACHINE_RESULT)


def test_c_says_which_contrast_is_primary_and_that_the_rule_was_fixed_blind():
    with open(REPORT_MD, encoding='utf-8') as f:
        report = ' '.join(f.read().split())
    assert '**Primary:** leak against antileak, a one-sided exact sign test' in report
    assert 'fixed before unblinding' in report and 'DECISIONS.md' in report


REDRAW_DIRS = [os.path.join(REPO_ROOT, 'reports', 'data', d) for d in
               ('2026-09-29-tilt-adjudication-jm', '2026-09-30-tilt-adjudication-jm-b2')]


@pytest.mark.parametrize('d', REDRAW_DIRS, ids=['batch1', 'batch2'])
def test_a_redraw_folder_keeps_its_key_selection_and_crop_jobs_sealed(d):
    """selection.csv and crop_jobs.csv name each window's role, so they are key files too."""
    assert ta.unsealed_key_files(d) == []
    for name in ('selection.csv', 'crop_jobs.csv'):
        assert not os.path.exists(os.path.join(d, name)) and os.path.exists(os.path.join(d, 'sealed', name))
    n_key = len(ta.read_sealed_key(d))                  # the hash check, and a count only
    with open(os.path.join(d, 'tasks.json'), encoding='utf-8') as f:
        tasks = json.load(f)
    assert n_key == len(tasks) == 48 and all(set(t) == {'label_type', 'tags'} for t in tasks.values())
    assert len(ta.load_verdicts(d, 'jon')) == 48


# ---- Gemini 3.1 Pro follow-up (#158): the fill's usage error and the tags encoding ----------------

@pytest.mark.parametrize('missing', ['--fill-pose', '--fill-city'])
def test_a_fill_without_its_pose_or_city_is_a_usage_error(tmp_path, capsys, missing):
    argv = ['select', '--corpus', 'c.csv', '--pose', 'p.csv', '--out', str(tmp_path / 'o'),
            '--fill-rawlabels', 'r.csv', '--fill-city', 'seattle-wa', '--fill-pose', 'fp.csv']
    i = argv.index(missing)
    del argv[i:i + 2]
    with pytest.raises(SystemExit) as e:
        ta.main(argv)
    assert e.value.code == 2
    assert '--fill-rawlabels needs --fill-pose and --fill-city' in capsys.readouterr().err


@pytest.mark.parametrize('raw, expected', [
    ('[]', '[]'), ('', '[]'), ('[cracks]', '["cracks"]'), ('["cracks"]', '["cracks"]'),
    ('[grass,debris]', '["grass","debris"]'), ('[trash/recycling can,vegetation]', '["trash/recycling can","vegetation"]'),
    ('["debris / pooled water","narrow"]', '["debris / pooled water","narrow"]'),
    ('[missing tactile warning]', '["missing tactile warning"]')])
def test_tags_are_stored_as_one_json_encoding(raw, expected):
    assert ta.tags_as_json(raw) == expected


def test_sheets_write_json_tags_whatever_the_intake(tmp_path):
    rng = np.random.default_rng(0)
    root = tmp_path / 'panos'
    (root / 'seattle-wa' / 'ab').mkdir(parents=True)
    labels = []
    for i, tags in enumerate(['[cracks]', '["cracks"]', float('nan')]):
        pid = 'ab%04d' % i
        Image.fromarray(rng.integers(0, 255, (H, W, 3), dtype=np.uint8)).save(root / 'seattle-wa' / 'ab' / (pid + '.jpg'))
        labels.append(dict(_label('seattle-wa:%d' % (100 + i), pid, 1500 + i, 600 + i, tags=tags), T_deg=5.0, dbear=0.0,
                           pitch_deg=5.0, roll_deg=0.0, era_arm='post179', pose_source='npz', scrape_era='modern'))
    tasks = ta.build_sheets(pd.DataFrame(labels), str(root), str(tmp_path / 'adj'), seed='t')
    assert sorted(t['tags'] for t in tasks.values()) == ['["cracks"]', '["cracks"]', '[]']


def test_the_committed_tags_are_all_json_lists_of_strings():
    with open(os.path.join(ADJ_DIR, 'tasks.json'), encoding='utf-8') as f:
        tasks = json.load(f)
    for t in tasks.values():
        tags = json.loads(t['tags'])
        assert isinstance(tags, list) and all(isinstance(x, str) and x == x.strip() and x for x in tags)
        assert t['tags'] == ta.tags_as_json(t['tags'])


# ---- #191: the asymmetric-decoy beta batch --------------------------------------------------------------

def test_the_beta_windows_all_sit_on_the_leak_side_at_half_one_and_one_and_a_half_T():
    c = ta.window_centres(600.0, 4.0, 1024, ta.BETA_OFFSETS)
    s = 4.0 * 1024 / 180
    assert c == {'b050': 600.0 - 0.5 * s, 'b100': 600.0 - s, 'b150': 600.0 - 1.5 * s}
    assert ta.window_centres(600.0, 4.0, 1024, ta.BETA_OFFSETS)['b100'] == ta.window_centres(600.0, 4.0, 1024)['leak']


def test_c_is_still_the_default_design():
    assert ta.window_centres(600.0, -5.0, 1024) == ta.window_centres(600.0, -5.0, 1024, ta.C_OFFSETS)
    assert tuple(ta.C_OFFSETS) == ta.WINDOW_NAMES      # build_sheets' permutation indexes this order


def _beta_run(tmp_path, n=6):
    labels, _ = _synthetic_run(tmp_path, n=n)
    out = tmp_path / 'beta'
    ta.build_sheets(pd.DataFrame(labels), str(tmp_path / 'panos'), str(out), seed='t', offsets=ta.BETA_OFFSETS)
    return labels, out


def test_a_beta_key_names_its_design_and_a_c_key_is_unchanged(tmp_path):
    _, out = _beta_run(tmp_path)
    key = ta.read_sealed_key(str(out))
    for v in key.values():
        assert sorted(v['order']) == ['b050', 'b100', 'b150']
        assert v['design'] == 'beta' and v['offsets'] == ta.BETA_OFFSETS
    c_key = ta.read_sealed_key(str(tmp_path / 'adj'))
    assert all('design' not in v and 'offsets' not in v for v in c_key.values())
    tasks = json.load(open(out / 'tasks.json', encoding='utf-8'))
    assert all(set(t) == {'label_type', 'tags'} for t in tasks.values())


def test_beta_remote_panels_make_the_same_sheet_as_a_local_cut(tmp_path):
    import tilt_remote_crop as trc
    labels, out = _beta_run(tmp_path, n=2)
    sel = pd.DataFrame(labels)
    jobs = tmp_path / 'jobs.csv'
    ta.crop_jobs(sel, seed='t', offsets=ta.BETA_OFFSETS).to_csv(jobs, index=False)
    assert set(pd.read_csv(jobs)['window']) == {'b050', 'b100', 'b150'}
    panels = tmp_path / 'panels'
    assert trc.main(['--jobs', str(jobs), '--store', str(tmp_path / 'panos'), '--out', str(panels)]) == 0
    remote = tmp_path / 'remote'
    ta.build_sheets(sel.assign(source='store'), None, str(remote), seed='t', panel_dir=str(panels),
                    offsets=ta.BETA_OFFSETS)
    for name in os.listdir(out / 'sheets'):
        a = np.asarray(Image.open(out / 'sheets' / name), dtype=int)
        b = np.asarray(Image.open(remote / 'sheets' / name), dtype=int)
        assert np.abs(a - b).max() == 0


@pytest.mark.parametrize('choice, expected', [('A', 1.5), ('B', 0.5), ('C', 1.0), ('A=B', 1.0), ('B=C', 0.75),
                                              ('A=C', 1.25), ('none', None)])
def test_a_sheets_beta_score(choice, expected):
    assert ta.beta_sheet_score(choice, ['b150', 'b050', 'b100']) == expected


def _beta_key(arms):
    """{token: key entry}, order b050 / b100 / b150 = A / B / C, for {arm: n}."""
    return {'%s%d' % (arm[0], i): {'order': ['b050', 'b100', 'b150'], 'era_arm': arm}
            for arm, n in arms.items() for i in range(n)}


def test_score_beta_reads_low_against_high_by_what_a_tie_left_out():
    key = _beta_key({'post179': 6})
    v = dict(zip(sorted(key), ['A', 'A=B', 'B', 'A=C', 'B=C', 'none']))
    s = ta.score_beta(v, key)['arms']['post179']
    assert (s['low'], s['high']) == (2, 1)     # A and A=B are low; B=C is high; B, A=C and none are neither
    assert s['n'] == 6 and s['none'] == 1 and s['n_scored'] == 5
    assert s['mean'] == pytest.approx((0.5 + 0.75 + 1.0 + 1.0 + 1.25) / 5)
    assert s['score_counts'] == {'0.50': 1, '0.75': 1, '1.00': 2, '1.25': 1}


def test_score_beta_readings_are_holm_adjusted_over_the_two_arms():
    key = _beta_key({'post179': 12, 'legacy+mid': 12})
    v = {t: ('A' if t.startswith('p') else 'B') for t in key}      # post179 all 0.5 T; legacy+mid all middle
    r = ta.score_beta(v, key)
    p = r['arms']['post179']
    assert p['p_low_vs_high_two_sided'] == pytest.approx(2 * 0.5 ** 12)
    assert p['p_holm'] == pytest.approx(2 * p['p_low_vs_high_two_sided'])
    assert p['reading'] == 'below 1' and r['arms']['legacy+mid']['reading'] == 'consistent with 1'
    assert p['ci95'] == [0.5, 0.5]
    assert r['pooled']['n'] == 24 and r['pooled']['low'] == 12
    assert r['arm_difference']['low_high_fisher_p'] == pytest.approx(1.0)   # legacy+mid has no low/high sheets


def test_an_all_high_arm_reads_above_one():
    key = _beta_key({'post179': 10})
    r = ta.score_beta({t: 'C' for t in key}, key)['arms']['post179']
    assert r['reading'] == 'above 1' and r['mean'] == 1.5


def test_exact_tests_against_known_values():
    assert ta.binom_two_sided(0, 0) == 1.0
    assert ta.binom_two_sided(5, 10) == pytest.approx(1.0)
    assert ta.binom_two_sided(9, 10) == pytest.approx(22 / 1024)
    assert ta.fisher_two_sided(3, 1, 1, 3) == pytest.approx(0.4857142857)     # the tea-tasting table
    assert ta.fisher_two_sided(8, 2, 1, 5) == pytest.approx(0.0349650350, rel=1e-6)
    assert ta.holm({'a': 0.01, 'b': 0.04}) == {'a': pytest.approx(0.02), 'b': pytest.approx(0.04)}
    assert ta.holm({'a': 0.03, 'b': 0.02}) == {'a': pytest.approx(0.04), 'b': pytest.approx(0.04)}


BETA_DIR = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-29-tilt-beta-jm')


class TestTheCommittedBetaFolder:
    def test_its_key_selection_and_crop_jobs_are_sealed_and_hashed(self):
        assert ta.unsealed_key_files(BETA_DIR) == []
        for name in ('selection.csv', 'crop_jobs.csv'):
            assert not os.path.exists(os.path.join(BETA_DIR, name))
            assert os.path.exists(os.path.join(BETA_DIR, 'sealed', name))
        key = ta.read_sealed_key(BETA_DIR)
        assert len(key) == 48 and all(v['design'] == 'beta' for v in key.values())
        with open(os.path.join(BETA_DIR, 'tasks.json'), encoding='utf-8') as f:
            tasks = json.load(f)
        assert set(tasks) == set(key) and all(set(t) == {'label_type', 'tags'} for t in tasks.values())

    def test_the_draw_is_the_one_decisions_md_describes(self):
        with open(os.path.join(BETA_DIR, 'draw.json'), encoding='utf-8') as f:
            d = json.load(f)
        assert d['design'] == 'beta' and d['offsets_T'] == ta.BETA_OFFSETS and d['min_abs_t_deg'] == 5.0
        assert sorted(d['excluded_batches']) == ['2026-09-29-tilt-adjudication-jm', '2026-09-30-tilt-adjudication-jm-b2']
        with open(os.path.join(BETA_DIR, 'DECISIONS.md'), encoding='utf-8') as f:
            text = f.read()
        assert '--seed %s --min-abs-t 5 --cell-cap 2' % d['seed'] in text and 'score_beta' in text

    def test_no_beta_pano_was_in_either_c_batch(self):
        read = lambda d: set(pd.read_csv(os.path.join(d, 'sealed', 'selection.csv'), dtype={'pano_id': str})['pano_id'])  # noqa: E731
        beta = read(BETA_DIR)
        assert len(beta) == 48 and not beta & (read(REDRAW_DIRS[0]) | read(REDRAW_DIRS[1]))


# ---- #194 review fixes (2026-10-05): the unblinding table is a command's output, and a test pins it ------
# Each test below fails with its fix reverted: there was no `score-beta` subcommand or `beta_power`, `score`
# died with KeyError: 'b100' on the beta folder, the beta folder's sealed README was C's text, its step 5
# named C's analysis, and the CLI --design paths and the arm-difference tests were never run.

BETA_SCORE_JSON = os.path.join(BETA_DIR, 'sealed', 'score_beta_jon.json')


def _rounded(x, nd=9):
    """JSON-comparable with float noise below 1e-9 dropped (numpy's summation order may differ by platform)."""
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: _rounded(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v, nd) for v in x]
    return x


def _decisions_section(number):
    """The text of DECISIONS.md's `## <number> ...` section, whitespace-normalised."""
    with open(os.path.join(BETA_DIR, 'DECISIONS.md'), encoding='utf-8') as f:
        text = f.read()
    start = text.index('\n## %s ' % number)
    end = text.find('\n## ', start + 1)
    return ' '.join(text[start:end if end != -1 else len(text)].split())


def _fmt_p(p):
    """A p-value as DECISIONS.md writes it: 3 dp, trailing zeros dropped, 1 as 1.0."""
    return ('%.3f' % p).rstrip('0').rstrip('.') if p < 1 else '1.0'


def test_score_refuses_a_beta_folder_and_names_score_beta(capsys):
    with pytest.raises(SystemExit) as e:
        ta.main(['score', '--out', BETA_DIR, '--judge', 'jon'])
    assert e.value.code == 2
    assert 'score-beta' in capsys.readouterr().err


def test_score_beta_refuses_a_c_folder(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        ta.main(['score-beta', '--out', REDRAW_DIRS[0], '--judge', 'jon', '--json', str(tmp_path / 'x.json')])
    assert e.value.code == 2
    assert "'c' design" in capsys.readouterr().err
    assert not (tmp_path / 'x.json').exists()


def test_score_beta_reproduces_the_committed_score_json(tmp_path, capsys):
    out = tmp_path / 'score.json'
    assert ta.main(['score-beta', '--out', BETA_DIR, '--judge', 'jon', '--json', str(out)]) == 0
    capsys.readouterr()
    with open(out, encoding='utf-8') as f:
        fresh = json.load(f)
    with open(BETA_SCORE_JSON, encoding='utf-8') as f:
        committed = json.load(f)
    assert _rounded(fresh) == _rounded(committed)
    assert committed['judge'] == 'jon' and committed['n_verdicts'] == 48


def test_every_section_4_number_is_the_committed_score():
    """reports/README.md: every number in a report's prose is transcribed from a committed artifact, and a
    test says so. Section 4 of DECISIONS.md is that prose for this batch (#194 review finding 1)."""
    with open(BETA_SCORE_JSON, encoding='utf-8') as f:
        s = json.load(f)['score_beta']
    sec = _decisions_section('4.')
    for arm in ('post179', 'legacy+mid'):
        a = s['arms'][arm]
        row = '| %s | %d | %d | %d | %d : %d | %s (%s) | %s | %.2f [%.2f, %.2f] |' % (
            arm, a['n'], a['none'], a['n_scored'], a['low'], a['high'], _fmt_p(a['p_low_vs_high_two_sided']),
            _fmt_p(a['p_holm']), a['reading'], a['mean'], a['ci95'][0], a['ci95'][1])
        assert row in sec, row
        counts = ', '.join('%s ×%d' % ('1.0' if k == '1.00' else k.rstrip('0'), v)
                           for k, v in a['score_counts'].items())
        assert '%s %s' % (arm, counts) in sec, counts
    p = s['pooled']
    row = '| pooled | %d | %d | %d | %d : %d | %s | - | %.2f [%.2f, %.2f] |' % (
        p['n'], p['none'], p['n_scored'], p['low'], p['high'], _fmt_p(p['p_low_vs_high_two_sided']), p['mean'],
        p['ci95'][0], p['ci95'][1])
    assert row in sec, row
    mid = sum(a['score_counts'].get('1.00', 0) for a in s['arms'].values())
    assert 'takes %d of the %d scored sheets' % (mid, p['n_scored']) in sec
    d = s['arm_difference']
    assert '(Fisher p = %s)' % _fmt_p(d['low_high_fisher_p']) in sec
    assert '(%d of 24 against %d of 24, Fisher p = %.3f)' % (
        s['arms']['legacy+mid']['none'], s['arms']['post179']['none'], d['none_fisher_p']) in sec


def test_the_post_hoc_entry_states_the_power_the_score_reports():
    with open(BETA_SCORE_JSON, encoding='utf-8') as f:
        ph = json.load(f)['post_hoc_power']
    for arm in ('post179', 'legacy+mid'):
        a = ph['per_arm'][arm]
        assert a['n_discordant'] == 5 and not a['could_reject']
        assert a['min_attainable_p'] == pytest.approx(0.0625)
        assert a['min_attainable_p_holm'] == pytest.approx(0.125)
    assert ph['min_discordant_to_reject_holm'] == 7
    sec = _decisions_section('5.')
    assert '2026-10-05' in sec and 'post hoc' in sec.lower()
    for text in ('n = 5 discordant', '0.0625', '0.125', '7 : 0', 'issues/197'):
        assert text in sec, text


def test_beta_power_against_known_values():
    """n discordant sheets all one way is the smallest p a two-sided sign test can give; Holm doubles it."""
    ph = ta.beta_power({'arms': {'a': {'low': 4, 'high': 1}, 'b': {'low': 0, 'high': 0}}})
    assert ph['per_arm']['a'] == {'n_discordant': 5, 'min_attainable_p': pytest.approx(2 / 32),
                                  'min_attainable_p_holm': pytest.approx(4 / 32), 'could_reject': False}
    assert ph['per_arm']['b']['min_attainable_p'] == 1.0
    assert ph['min_discordant_to_reject_holm'] == 7       # 2 * 2/128 = 0.031; n = 6 gives 2 * 2/64 = 0.0625


def _asym_key_and_verdicts():
    """post179: 4 low (A), 1 high (C), 6 middle (B), 1 none; legacy+mid: 1 low, 4 high, 1 middle, 6 none.
    Asymmetric on purpose: the low/high Fisher table is not its own transpose, and `none` is not 0."""
    key = _beta_key({'post179': 12, 'legacy+mid': 12})
    v = {}
    for prefix, choices in (('p', ['A'] * 4 + ['C'] + ['B'] * 6 + ['none']),
                            ('l', ['A'] + ['C'] * 4 + ['B'] + ['none'] * 6)):
        v.update(zip(sorted(t for t in key if t.startswith(prefix)), choices))
    return key, v


def test_score_beta_on_a_non_degenerate_synthetic_batch():
    key, v = _asym_key_and_verdicts()
    r = ta.score_beta(v, key)
    p, lm = r['arms']['post179'], r['arms']['legacy+mid']
    assert (p['low'], p['high'], p['none']) == (4, 1, 1) and (lm['low'], lm['high'], lm['none']) == (1, 4, 6)
    # The bootstrap DECISIONS.md rule 4 fixed: 10,000 resamples over sheets, seed 20260929, the 2.5 / 97.5
    # percentiles. Recomputed from the rule's literals, not the module's constants, so a changed constant fails.
    arr = np.asarray([0.5] * 4 + [1.5] + [1.0] * 6)
    rng = np.random.default_rng(20260929)
    boot = arr[rng.integers(0, len(arr), (10000, len(arr)))].mean(axis=1)
    assert p['ci95'] == pytest.approx([np.percentile(boot, 2.5), np.percentile(boot, 97.5)], abs=1e-12)
    assert p['ci95'][0] < p['mean'] < p['ci95'][1] and p['ci95'][1] - p['ci95'][0] > 0.1
    # References from scipy.stats.fisher_exact (scipy is not a dependency; pasted 2026-10-05):
    # [[1, 4], [4, 1]] -> 0.2063492063 (its transpose [[1, 4], [1, 4]] gives 1.0);
    # [[6, 6], [1, 11]] -> 0.0686498857 (the n-for-n-minus-none slip, [[6, 12], [1, 12]], gives 0.191).
    assert r['arm_difference']['low_high_fisher_p'] == pytest.approx(0.2063492063492064, rel=1e-9)
    assert r['arm_difference']['none_fisher_p'] == pytest.approx(0.06864988558352403, rel=1e-9)


def test_the_reading_at_exactly_alpha_is_consistent_with_one():
    """Rule 3: `Holm-adjusted p >= 0.05` reads consistent with 1, so the boundary itself does."""
    assert ta.beta_reading({'low': 7, 'high': 0}, 0.05) == 'consistent with 1'
    assert ta.beta_reading({'low': 7, 'high': 0}, 0.0499) == 'below 1'


def test_the_sheets_cli_builds_the_design_it_is_given(tmp_path, capsys):
    labels, _ = _synthetic_run(tmp_path, n=3)
    out = tmp_path / 'cli'
    (out / 'sealed').mkdir(parents=True)
    pd.DataFrame(labels).to_csv(out / 'sealed' / 'selection.csv', index=False)
    assert ta.main(['sheets', '--out', str(out), '--pano-root', str(tmp_path / 'panos'), '--seed', 't',
                    '--design', 'beta']) == 0
    capsys.readouterr()
    key = ta.read_sealed_key(str(out))
    assert len(key) == 3 and all(sorted(v['order']) == ['b050', 'b100', 'b150'] for v in key.values())
    with open(out / 'sealed' / 'README.md', encoding='utf-8') as f:
        assert f.read() == ta.SEALED_READMES['beta']
    with open(out / 'README.md', encoding='utf-8') as f:
        assert 'tilt_adjudicate.py score-beta' in f.read()


class TestTheBetaFolderReadmes:
    def test_the_sealed_readme_describes_this_batch(self):
        with open(os.path.join(BETA_DIR, 'sealed', 'README.md'), encoding='utf-8') as f:
            text = f.read()
        assert text == ta.SEALED_READMES['beta']
        assert text.splitlines()[0] == ta.SEALED_README_FIRST_LINE
        assert 'endpoint-C' not in text and 'preliminary pass' not in text and 'by mistake' not in text
        assert 'b050' in text and 'score_beta_<judge>.json' in text

    def test_the_c_folders_keep_the_c_sealed_readme(self):
        for d in REDRAW_DIRS + [ADJ_DIR]:
            with open(os.path.join(d, 'sealed', 'README.md'), encoding='utf-8') as f:
                assert f.read() == ta.SEALED_READMES['c'] == ta.SEALED_README

    def test_step_5_scores_with_score_beta(self):
        with open(os.path.join(BETA_DIR, 'README.md'), encoding='utf-8') as f:
            judge = f.read()
        # A record, like the 2026-09-26 folder's (TestTheCommittedFolder.test_the_readmes): drawn while the tilt
        # rules were a CLAUDE.md subsection, so it keeps the words its judge read, not #192's new path.
        template = ta.judge_readme('beta').replace("`.claude/rules/tilt.md`", "CLAUDE.md's tilt subsection")
        assert template != ta.judge_readme('beta')
        assert judge == template.format(out='reports/data/2026-09-29-tilt-beta-jm')
        step5 = ' '.join(judge[judge.index('\n5. '):].split('\n\n')[0].split())
        assert ('python reports/scripts/tilt_adjudicate.py score-beta --out reports/data/2026-09-29-tilt-beta-jm'
                in step5)
        assert 'tilt_error_study.py' not in step5
        assert ta.judge_readme('c') == ta.JUDGE_README
