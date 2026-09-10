"""The re-render probe's measurement functions, on synthetic pairs whose answer is known in advance.

Committed-artifact tests do not test code: pinning a finding against reports/data/*.json proves nothing
about the function that produced it, because the artifact was generated *by* the current code, so a revert
stays green. This is the code-level half that reports/2026-09-06-rerender-probe.md's findings rest on.

The confound this battery exists for is `test_a_pure_tone_gain_is_not_read_as_sharpening`. Laplacian
variance scales with the square of a contrast gain, so a panorama Google merely brightened reads as
"sharpened 2x" under a naive new/old ratio. Two of the 19 have a gain far enough from 1 to matter, and the
whole "sharpened and re-graded" finding would have been an artefact of the grade. Dividing by gain^2 is
what makes the two separable, and this test is what holds that division in place.
"""

import math
import os
import sys

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for path in (REPO_ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import rerender_probe  # noqa: E402
import rerender_reduce  # noqa: E402

BAND_W, BAND_H = 2048, 1152


def textured(width=BAND_W, height=BAND_H, seed=0):
    """A smooth-but-textured field, so phase correlation has an unambiguous peak.

    The double cumsum is the probe's own _selftest trick: white noise correlates against itself as a
    delta and would pass a broken sub-pixel refinement, while this has structure at every scale.
    """
    rng = np.random.default_rng(seed)
    field = np.cumsum(np.cumsum(rng.random((height, width)), 0), 1)
    field -= field.min()
    return (255.0 * field / field.max()).astype(np.float32)


def noisy(width=BAND_W, height=BAND_H, amplitude=25.0, seed=1):
    """A field with real pixel-scale texture, centred so a gain can be applied without clipping.

    The sharpness tests need this rather than `textured`: Laplacian variance measures high-frequency
    energy, and a double-cumsum field has almost none, so its Laplacian is dominated by uint8 rounding
    - which does not scale with a contrast gain and would make the gain-squared confound invisible.
    """
    rng = np.random.default_rng(seed)
    return (128.0 + amplitude * rng.standard_normal((height, width))).astype(np.float32)


def as_image(array):
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode='L')


class TestItRecoversADisplacementItWasGiven:

    def test_a_uniform_shift_is_measured_on_every_locked_window(self):
        base = textured()
        moved = np.roll(base, (3, -5), axis=(0, 1))

        band = rerender_probe.measure_band(as_image(base), as_image(moved), 0, BAND_H)

        assert band['shift']['n_locked'] == band['shift']['n_windows']
        assert band['shift']['median_dy'] == 3 and band['shift']['median_dx'] == -5
        assert abs(band['shift']['median_dy_sub'] - 3) < 0.25
        assert abs(band['shift']['median_dx_sub'] + 5) < 0.25

    def test_removing_the_shift_is_what_proves_it_was_real(self):
        """Every window records its MAE with and without the estimated shift applied. A spurious
        correlation peak does not reduce the MAE, so this is the check that separates a displacement
        from a coincidence - and it is the evidence behind the report's 12.1 -> 6.7 claim."""
        base = textured()
        moved = np.roll(base, (3, -5), axis=(0, 1))

        band = rerender_probe.measure_band(as_image(base), as_image(moved), 0, BAND_H)

        for window in band['windows']:
            assert window['mae_shifted'] < window['mae_unshifted'], window

    def test_an_identical_pair_reports_no_displacement(self):
        base = textured()

        band = rerender_probe.measure_band(as_image(base), as_image(base), 0, BAND_H)

        assert band['shift']['max_abs_dy'] == 0 and band['shift']['max_abs_dx'] == 0
        assert band['mae'] == 0.0
        assert band['shift']['n_locked'] == band['shift']['n_windows']


class TestToneAndSharpnessAreSeparated:

    def test_an_affine_regrade_is_recovered_and_leaves_no_residual(self):
        base = textured()
        regraded = 0.80 * base + 20.0

        band = rerender_probe.measure_band(as_image(base), as_image(regraded), 0, BAND_H)

        assert abs(band['affine']['gain'] - 0.80) < 0.01
        assert abs(band['affine']['offset'] - 20.0) < 1.0
        # a re-grade is a change the fit removes; that is exactly what distinguishes it from a re-render
        assert band['affine']['mae_after'] < 0.05 * band['affine']['mae_before']

    def test_a_pure_tone_gain_is_not_read_as_sharpening(self):
        """THE confound. Laplacian variance scales as gain^2, so a 1.5x contrast gain reads as 2.25x
        'sharper' under a naive ratio while not one edge has changed. Dividing by gain^2 is what makes
        the report's 'sharpened 1.6 to 2.2x net of gain' a statement about detail rather than about
        brightness. A mutation dropping the correction makes this fail and nothing else."""
        base = noisy()
        brighter = 1.5 * (base - 128.0) + 128.0      # about the mean, so nothing clips

        band = rerender_probe.measure_band(as_image(base), as_image(brighter), 0, BAND_H)

        assert abs(band['affine']['gain'] - 1.5) < 0.01
        assert abs(band['lap_ratio'] - 1.5 ** 2) < 0.15, 'the naive ratio should show the gain squared'
        assert abs(band['lap_ratio_gain_corrected'] - 1.0) < 0.05, 'net of gain, nothing was sharpened'

    def test_real_added_detail_survives_the_gain_correction(self):
        """The other side of the discrimination: something genuinely sharpened must still read as
        sharpened after the correction, or the correction would just be suppressing the signal.

        Fine detail is *added* to a smooth field rather than removed from a noisy one, so the tone fit
        stays at unit gain and the only thing that changed is high-frequency energy - which is the
        shape of the change the report attributes to the 13.
        """
        smooth = textured() * 0.8 + 25.0
        detailed = smooth + 12.0 * np.random.default_rng(2).standard_normal(smooth.shape)

        band = rerender_probe.measure_band(as_image(smooth), as_image(detailed), 0, BAND_H)

        assert abs(band['affine']['gain'] - 1.0) < 0.02, 'the tone did not change; only the detail did'
        assert band['lap_ratio_gain_corrected'] > 1.2


class TestTheClassifierDiscriminates:
    """`classify` turns a band's numbers into the report table's 'what changed' column, so each label
    needs a case that produces it and would not produce its neighbour."""

    def record(self, **shift):
        base = {'n_locked': 8, 'n_windows': 8, 'n_locked_moved': 8, 'n_locked_agree': 8,
                'median_dy_sub': 0.0, 'median_dx_sub': 0.0}
        base.update(shift)
        return {'rerendered': True, 'horizon': {
            'shift': base,
            'affine': {'gain': 1.0, 'offset': 0.0, 'mae_before': 10.0, 'mae_after': 9.9},
            'lap_ratio_gain_corrected': 1.0}}

    def test_a_uniform_displacement_is_shifted(self):
        assert rerender_probe.classify(self.record(median_dx_sub=-2.6)) == 'shifted'

    def test_windows_that_move_but_disagree_are_warped(self):
        """A pitch/roll re-estimate moves content up on one side and down on the other, so the windows
        lock and move but do not agree - which is a different fact from a uniform yaw change."""
        assert rerender_probe.classify(self.record(median_dx_sub=-2.6, n_locked_agree=2)) == 'warped'

    def test_no_displacement_with_no_tone_or_detail_change_is_unclassified(self):
        assert rerender_probe.classify(self.record(n_locked_moved=0)) == 'changed-unclassified'

    def test_sharpening_without_displacement_is_sharpened(self):
        rec = self.record(n_locked_moved=0)
        rec['horizon']['lap_ratio_gain_corrected'] = 2.2
        assert rerender_probe.classify(rec) == 'sharpened'

    def test_a_band_that_never_locked_says_so_rather_than_guessing(self):
        assert rerender_probe.classify(self.record(n_locked=0)) == 'no-lock'


def windows_with(width, heading_px=0.0, tilt_px=0.0, peak=0.5, n=8):
    """Locked windows carrying a known yaw translation and a known pitch/roll sinusoid."""
    out = []
    for k in range(n):
        x = int(k * width / n)
        phi = 2 * math.pi * x / width
        out.append({'y': 0, 'x': x, 'peak': peak,
                    'dx_sub': heading_px, 'dy_sub': tilt_px * math.sin(phi)})
    return out


class TestTheReduceRecoversAKnownPose:
    """rerender_reduce turns 16 per-window displacements into the table's two pose numbers. It is new
    code with no upstream check, so the recovery is verified against poses put in by hand."""

    def record(self, width=16384, **kwargs):
        return {'width': width, 'label': 'warped', 'horizon': {'windows': windows_with(width, **kwargs)}}

    def test_a_pure_yaw_change_is_all_heading_and_no_tilt(self):
        rec = self.record(heading_px=2.5)

        assert abs(rerender_reduce.heading_offset_px(rec) - 2.5) < 1e-6
        assert rerender_reduce.tilt_amplitude_px(rec) < 1e-6

    def test_a_pure_tilt_is_all_tilt_and_no_heading(self):
        rec = self.record(tilt_px=7.2)

        assert abs(rerender_reduce.heading_offset_px(rec)) < 1e-6
        assert abs(rerender_reduce.tilt_amplitude_px(rec) - 7.2) < 1e-6

    def test_the_two_do_not_contaminate_each_other(self):
        rec = self.record(heading_px=3.0, tilt_px=5.2)

        assert abs(rerender_reduce.heading_offset_px(rec) - 3.0) < 1e-6
        assert abs(rerender_reduce.tilt_amplitude_px(rec) - 5.2) < 1e-6

    def test_a_uniform_vertical_offset_is_not_a_tilt(self):
        """A tilt moves content up on one side and down on the opposite side. A constant vertical offset
        is not that, and the fitted constant must absorb it - otherwise a re-stitch that merely moved the
        horizon would be reported as a camera-tilt correction.

        Only half the circle locks here, and that is the point rather than incidental colour. Over
        *evenly spaced* windows covering a full turn, cos and sin are orthogonal to a constant, so the
        fitted constant is inert and dropping it changes nothing - a version of this test on full
        coverage passes against a fit that has no constant term at all. Coverage goes partial exactly
        when windows fail to lock, which is the low-evidence case where a spurious tilt would be least
        likely to be questioned: without the constant, this flat 4 px offset reads as a 5.2 px tilt.
        """
        rec = self.record(tilt_px=0.0)
        for index, window in enumerate(rec['horizon']['windows']):
            window['dy_sub'] = 4.0
            if index >= 4:
                window['peak'] = 0.01

        assert rerender_reduce.tilt_amplitude_px(rec) < 1e-6

    def test_unlocked_windows_are_not_averaged_in(self):
        """An unlocked window's shift is the argmax of noise. Including it would pull both estimates
        toward zero in proportion to how featureless the band was."""
        rec = self.record(heading_px=2.0)
        for window in rec['horizon']['windows'][4:]:
            window['peak'] = 0.01
            window['dx_sub'] = -40.0

        assert abs(rerender_reduce.heading_offset_px(rec) - 2.0) < 1e-6

    @pytest.mark.parametrize('n_locked', [0, 1, 2])
    def test_too_few_windows_is_undefined_not_zero(self, n_locked):
        """studyfmt's rule: a quantity that cannot be measured is None, never 0. Three windows is the
        minimum for a three-parameter fit, and reporting 0 below that would read as 'we measured no
        tilt' rather than 'we could not measure'."""
        rec = self.record(tilt_px=5.0)
        for window in rec['horizon']['windows'][n_locked:]:
            window['peak'] = 0.01

        assert rerender_reduce.tilt_amplitude_px(rec) is None

    def test_degrees_per_pixel_is_the_same_on_both_axes(self):
        """A 2:1 equirectangular frame has square pixels, which is why one scale converts a heading
        offset and a tilt amplitude alike."""
        assert rerender_reduce.degrees_per_pixel(16384) == pytest.approx(180.0 / 8192)

    def test_a_panorama_with_no_displacement_reports_zero_not_fit_noise(self):
        """The table's job on those six rows is to say 'this did not move', so an unclassified record
        reports a flat 0 rather than the residual of a fit to windows that agree on nothing."""
        rec = self.record(heading_px=1.5, tilt_px=2.0)
        rec['label'] = 'changed-unclassified'

        row = rerender_reduce.table_row({**rec, 'pano_id': 'x', 'bottom': {'mae': 1.0},
                                         'horizon': {**rec['horizon'], 'mae': 5.0,
                                                     'affine': {'gain': 1.0, 'offset': 0.0},
                                                     'lap_ratio_gain_corrected': 1.0}}, {})

        assert row['heading_px'] == 0.0 and row['tilt_px'] == 0.0
