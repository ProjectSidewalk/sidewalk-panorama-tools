"""Tests for reports/scripts/non_gsv_levelling.py and the report it produced (#190).

Code-level tests on synthetic inputs first (a committed-artifact pin proves nothing about the code that
wrote the artifact), then `TestTheReportMatchesTheArtifact`: every number the report quotes comes out of
reports/data/2026-10-06-non-gsv-levelling.json. Network-free; the images are rendered here.

The synthetic renderer is tests/test_tilt_frame.py's: gravity-vertical poles painted into a rig-frame
equirectangular raster through tilt_geometry. The instrument never calls the rotation, so that is ground
truth, not a circular check.
"""

import json
import math
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
TESTS = os.path.dirname(os.path.abspath(__file__))
for p in (REPO_ROOT, SCRIPTS, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import non_gsv_levelling as ngl  # noqa: E402
from test_tilt_frame import render_poles  # noqa: E402

ARTIFACT = os.path.join(REPO_ROOT, 'reports', 'data', '2026-10-06-non-gsv-levelling.json')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-10-06-non-gsv-levelling.md')
W, H = 1024, 512


# ------------------------------------------------------------------------------------------- the fit

class TestFitSinusoid:
    def test_recovers_a_known_sinusoid(self):
        centres = [(b + 0.5) / 12 * 360 - 180 for b in range(12)]
        prof = [(c, 0.3 + 2.0 * math.cos(math.radians(c)) - 5.0 * math.sin(math.radians(c)), 100)
                for c in centres]
        fit = ngl.fit_sinusoid(prof)
        assert fit['c0'] == pytest.approx(0.3, abs=1e-9)
        assert fit['A'] == pytest.approx(2.0, abs=1e-9)
        assert fit['B'] == pytest.approx(-5.0, abs=1e-9)
        assert fit['amp'] == pytest.approx(math.hypot(2.0, 5.0), abs=1e-9)

    def test_too_few_defined_bins_is_undefined_not_flat(self):
        prof = [(c, (1.0 if i < ngl.MIN_DEFINED_BINS - 1 else None), 10) for i, c in enumerate(range(-165, 180, 30))]
        assert ngl.fit_sinusoid(prof) is None


# ------------------------------------------------------------------------------------- the instrument

class TestMeasurePicture:
    """The rig-frame sinusoid's coefficients are (A, B) = gain * (roll, -pitch) in streetlevel's sign; on
    noiseless synthetic poles the gain is ~1."""

    def test_a_levelled_picture_reads_no_tilt(self):
        m = ngl.measure_picture(render_poles(W, H, 0.0, 0.0))
        assert m['fit']['amp'] < 0.3

    @pytest.mark.parametrize('pitch, roll', [(8.0, 0.0), (0.0, -5.0), (-12.0, 4.0)])
    def test_a_rig_frame_picture_reads_its_tilt_with_the_right_signs(self, pitch, roll):
        fit = ngl.measure_picture(render_poles(W, H, pitch, roll))['fit']
        assert fit['B'] == pytest.approx(-pitch, abs=1.0)
        assert fit['A'] == pytest.approx(roll, abs=1.0)

    def test_the_wide_window_is_what_lets_an_eighteen_degree_pitch_be_read(self):
        """F2's 12-degree window truncates Bayonne's largest pitches; the study's 30-degree one does not."""
        img = render_poles(W, H, 18.0, 0.0)
        wide = ngl.measure_picture(img)['fit']
        narrow = ngl.measure_picture(img, max_abs_lean_deg=12.0)['fit']
        assert wide['B'] == pytest.approx(-18.0, abs=1.0)
        assert narrow is None or abs(narrow['B']) < 0.9 * abs(wide['B'])


class TestLevellingTest:
    """Levelling with the stored pose flattens a rig-frame picture under the right roll sign only, and
    re-tilts a picture that was already level."""

    @pytest.mark.parametrize('true_sign', [1.0, -1.0])
    def test_a_rig_frame_picture_flattens_under_the_true_sign_only(self, true_sign):
        p_sl, r_sl = 10.0, 5.0
        img = render_poles(W, H, p_sl, r_sl)
        pers_pitch, pers_roll = -p_sl, true_sign * r_sl
        out = ngl.levelling_test(img, pers_pitch, pers_roll)
        right, wrong = ('+1', '-1') if true_sign > 0 else ('-1', '+1')
        assert out[right] < 0.5
        assert out[wrong] > 5.0

    def test_the_pitch_sign_is_pinned_too(self):
        """pers:pitch is positive-up; levelling with it read as streetlevel's (nose-down) sign doubles the tilt."""
        img = render_poles(W, H, 10.0, 0.0)
        assert min(ngl.levelling_test(img, -10.0, 0.0).values()) < 0.5
        assert min(ngl.levelling_test(img, 10.0, 0.0).values()) > 15.0

    def test_a_level_picture_gets_worse_under_both_signs(self):
        out = ngl.levelling_test(render_poles(W, H, 0.0, 0.0), -8.0, 4.0)
        assert out['+1'] > 6.0 and out['-1'] > 6.0

    def test_the_summary_counts_flattened_pictures_per_sign(self):
        posed = [{'amp': 10.0, 'amp_levelled': {'+1': 1.0, '-1': 12.0}},
                 {'amp': 8.0, 'amp_levelled': {'+1': 5.0, '-1': 9.0}},
                 {'amp': None, 'amp_levelled': {'+1': 1.0, '-1': 1.0}}]
        lev = ngl.summarise_levelling(posed)
        assert lev['+1']['n'] == 2 and lev['+1']['flattened'] == 1 and lev['-1']['flattened'] == 0
        assert lev['+1']['amp_after_max'] == 5.0


class TestPooledSlopes:
    def _rows(self, roll_sign, levelled=False, gain=0.8, seed=1):
        rng = np.random.default_rng(seed)
        rows = []
        for _ in range(14):
            pp, pr = rng.uniform(-20, 12), rng.uniform(-6, 10)
            A = 0.0 if levelled else gain * roll_sign * pr
            B = 0.0 if levelled else gain * pp
            rows.append({'A': A + rng.normal(0, 0.1), 'B': B + rng.normal(0, 0.1),
                         'pers_pitch': pp, 'pers_roll': pr})
        return rows

    @pytest.mark.parametrize('roll_sign', [1.0, -1.0])
    def test_rig_frame_pictures_give_the_gain_and_the_roll_sign(self, roll_sign):
        out = ngl.pooled_slopes(self._rows(roll_sign), n_boot=200)
        assert out['k_pitch'] == pytest.approx(0.8, abs=0.05)
        assert out['k_roll'] == pytest.approx(0.8 * roll_sign, abs=0.05)
        lo, hi = out['k_roll_ci95']
        assert lo <= out['k_roll'] <= hi

    def test_levelled_pictures_give_zero_slopes(self):
        out = ngl.pooled_slopes(self._rows(1.0, levelled=True), n_boot=200)
        assert abs(out['k_pitch']) < 0.05 and abs(out['k_roll']) < 0.05

    def test_too_few_posed_pictures_is_undefined(self):
        assert ngl.pooled_slopes(self._rows(1.0)[:2]) is None


# ---------------------------------------------------------------------------------------- the readers

class TestReaders:
    def test_stac_pose_absent_is_none_not_zero(self):
        assert ngl.stac_pose({'properties': {}}) == (None, None, None)
        assert ngl.stac_pose({'properties': {'pers:pitch': -3.1, 'pers:roll': 0, 'pers:yaw': True}}) == (-3.1, 0.0, None)

    def test_exif_pose_reads_the_gpano_strings(self):
        item = {'properties': {'exif': {'Xmp.GPano.PosePitchDegrees': '0.0', 'Xmp.GPano.PoseRollDegrees': 'junk'}}}
        assert ngl.exif_pose(item) == (0.0, None)

    def test_the_three_pose_classes(self):
        assert ngl.pose_class({'camera_pitch': 0, 'camera_roll': None}) == 'pose-absent'
        assert ngl.pose_class({'camera_pitch': 0}) == 'pose-absent'
        assert ngl.pose_class({'camera_pitch': 0, 'camera_roll': 0.0}) == 'pose-zero'
        assert ngl.pose_class({'camera_pitch': -2.0, 'camera_roll': 0.0}) == 'pose-nonzero'
        assert ngl.pose_class({'camera_pitch': 0, 'camera_roll': 1.5}) == 'pose-nonzero'
        assert ngl.pose_class({'camera_pitch': -2.0}) == 'pose-nonzero'

    def test_the_sample_is_deterministic_and_stratified(self):
        panos = ([{'pano_id': 'p%02d' % i, 'camera_pitch': -1.0 - i, 'camera_roll': 1.0} for i in range(20)]
                 + [{'pano_id': 'z%02d' % i, 'camera_pitch': 0, 'camera_roll': 0.0} for i in range(20)]
                 + [{'pano_id': 'u%02d' % i, 'camera_pitch': 0} for i in range(20)])
        sizes = {'pose-nonzero': 4, 'pose-zero': 3, 'pose-absent': 2}
        s1 = ngl.select_sample(list(reversed(panos)), sizes)
        s2 = ngl.select_sample(panos, sizes)
        assert s1 == s2
        strata = [s for _, s in s1]
        assert (strata.count('pose-nonzero'), strata.count('pose-zero'), strata.count('pose-absent')) == (4, 3, 2)
        prefix = {'pose-nonzero': 'p', 'pose-zero': 'z', 'pose-absent': 'u'}
        assert all(i.startswith(prefix[s]) for i, s in s1)

    def test_ps_pose_summary_counts(self):
        panos = [{'pano_id': 'a', 'camera_pitch': -10.0, 'camera_roll': 2.0, 'has_labels': True, 'width': 1},
                 {'pano_id': 'b', 'camera_pitch': 0, 'has_labels': True, 'width': 1},
                 {'pano_id': 'd', 'camera_pitch': 0, 'camera_roll': 0.0, 'has_labels': True, 'width': 1},
                 {'pano_id': 'c', 'camera_pitch': 3.0, 'camera_roll': -4.0, 'has_labels': False, 'width': 2}]
        s = ngl.ps_pose_summary(panos)
        assert (s['panos'], s['labelled'], s['with_roll']) == (4, 3, 3)
        assert s['by_pose_class'] == {'pose-nonzero': 2, 'pose-zero': 1, 'pose-absent': 1}
        assert s['labelled_by_pose_class'] == {'pose-nonzero': 1, 'pose-zero': 1, 'pose-absent': 1}
        assert s['abs_pitch_max'] == 10.0 and s['abs_roll_max'] == 4.0


# ------------------------------------------------------------------------------ the committed artifact

@pytest.fixture(scope='module')
def artifact():
    with open(ARTIFACT, encoding='utf-8') as f:
        return json.load(f)


@pytest.fixture(scope='module')
def report_text():
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


class TestTheReportMatchesTheArtifact:
    """Every number in the report's prose is transcribed from the artifact (desk-study convention)."""

    def quoted(self, artifact):
        px = artifact['panoramax']
        sl = px['slopes_posed']
        lev = px['levelling_test']
        amp = px['lean_amplitude_by_class']
        out = {
            'posed n': str(sl['n']),
            'k_pitch': '%.2f' % sl['k_pitch'],
            'k_pitch lo': '%.2f' % sl['k_pitch_ci95'][0],
            'k_pitch hi': '%.2f' % sl['k_pitch_ci95'][1],
            'k_roll': '%.2f' % sl['k_roll'],
            'k_roll lo': '%.2f' % sl['k_roll_ci95'][0],
            'k_roll hi': '%.2f' % sl['k_roll_ci95'][1],
            'a': '%.2f' % px['a_median_all'],
            'sample': str(px['sample_size']),
            'stac == ps': str(px['stac_pose_equals_ps_pose']),
            'stac present': str(px['stac_pose_present']),
            'stac nonzero': str(px['stac_pose_nonzero']),
            'exif zero': str(px['stac_nonzero_but_exif_pose_zero_or_absent']),
        }
        for sign, v in lev.items():
            out['lev %s before' % sign] = '%.2f' % v['amp_before_median']
            out['lev %s after' % sign] = '%.2f' % v['amp_after_median']
            out['lev %s after max' % sign] = '%.2f' % v['amp_after_max']
            out['lev %s flattened' % sign] = '%d of %d' % (v['flattened'], v['n'])
        for k, v in amp.items():
            out['amp %s n' % k] = str(v['n'])
            out['amp %s median' % k] = '%.2f' % v['median']
            out['amp %s min' % k] = '%.2f' % v['min']
            out['amp %s max' % k] = '%.2f' % v['max']
        for city, s in artifact['ps_pose_by_city'].items():
            out[city + ' panos'] = '{:,}'.format(s['panos'])
            out[city + ' labelled'] = '{:,}'.format(s['labelled'])
            out[city + ' null pose'] = '{:,}'.format(s['with_null_pitch_and_roll'])
            for k in ('pose-nonzero', 'pose-zero', 'pose-absent'):
                out['%s %s' % (city, k)] = '{:,}'.format(s['by_pose_class'][k])
                out['%s labelled %s' % (city, k)] = '{:,}'.format(s['labelled_by_pose_class'][k])
            for k in ('abs_pitch_p90', 'abs_roll_p90', 'abs_pitch_max', 'abs_roll_max'):
                if s[k] is not None:
                    out['%s %s' % (city, k)] = '%.1f' % s[k]
        return out

    def test_every_quoted_value_appears_in_the_report(self, artifact, report_text):
        missing = {k: v for k, v in self.quoted(artifact).items() if v not in report_text}
        assert not missing, 'in the artifact but not transcribed into the report: %s' % missing

    def test_the_finding_is_the_one_the_report_states(self, artifact):
        """Posed Panoramax pictures are rig-frame and their stored pose levels them under roll sign +1 only;
        both raw slopes exclude 0. If a regeneration changes any of that, the report's verdict is stale."""
        px = artifact['panoramax']
        lev = px['levelling_test']
        assert px['roll_sign_that_levels'] == '+1'
        assert lev['+1']['flattened'] > lev['+1']['n'] / 2 > lev['-1']['flattened']
        assert lev['+1']['amp_after_median'] < 0.25 * lev['+1']['amp_before_median']
        assert px['slopes_posed']['k_pitch_ci95'][0] > 0 and px['slopes_posed']['k_roll_ci95'][0] > 0
