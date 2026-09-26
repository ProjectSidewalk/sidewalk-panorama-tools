"""Tests for reports/scripts/tilt_frame.py — the vertical-edge-lean estimator of which frame the
stored tiles are in (#54, endpoint F2).

The synthetic renderer paints gravity-vertical poles into a rig-frame equirectangular raster through
tilt_geometry. That is not circular: the estimator never calls the rotation, it only measures edge
angles in pixels, so the renderer is the ground truth and the estimator is what is under test.
"""

import glob
import os
import sys

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import tilt_geometry as tg  # noqa: E402
import tilt_frame as tf  # noqa: E402

PROBE_DIR = os.path.join(SCRIPTS, '.cache', 'tilt', 'probe')
W, H = 1440, 720


def render_poles(w, h, pitch, roll, bearings=None, pole_width_deg=3.0, el_max=40.0):
    """Gravity-vertical light poles on a dark background, seen by a rig tilted (pitch, roll).
    Anti-aliased by the signed distance to the pole edge, so edges are sub-pixel exact."""
    if bearings is None:
        bearings = np.arange(-165, 180, 30.0)
    ys, xs = np.mgrid[0:h, 0:w]
    b, el = tg.bearing_elevation_from_pixel(xs + 0.5, ys + 0.5, w, h)
    bg, elg = tg.rig_to_gravity(b, el, pitch, roll)
    px_deg = 360.0 / w
    img = np.zeros((h, w))
    for pb in bearings:
        d = np.abs(tg.wrap_deg(bg - pb)) * np.cos(np.radians(elg))
        img = np.maximum(img, np.clip(0.5 + (pole_width_deg / 2 - d) / px_deg, 0, 1))
    img[np.abs(elg) > el_max] = 0
    return (40 + 180 * img).astype(np.float32)


def _beta(profile, pitch, roll):
    c = np.array([b for b, lean, n in profile if lean is not None])
    m = np.array([lean for b, lean, n in profile if lean is not None])
    pred = tf.predicted_lean(c, pitch, roll)
    return float(np.dot(m, pred) / np.dot(pred, pred))


def test_untilted_poles_measure_zero_lean():
    prof = tf.lean_profile(render_poles(W, H, 0.0, 0.0))
    leans = [lean for _, lean, _ in prof]
    assert all(lean is not None for lean in leans)
    assert max(abs(x) for x in leans) < 0.15


@pytest.mark.parametrize('pitch, roll', [(4.0, 0.0), (0.0, -3.0), (3.0, 2.0)])
def test_tilted_poles_recover_the_predicted_profile(pitch, roll):
    prof = tf.lean_profile(render_poles(W, H, pitch, roll))
    beta = _beta(prof, pitch, roll)
    assert 0.85 <= beta <= 1.3, beta


def test_pitch_and_roll_phases_are_distinct():
    """Pure pitch leans most at b = +-90 and not at all at 0/180; pure roll the reverse."""
    bearings = np.array([-180.0, -90.0, 0.0, 90.0])
    pp = tf.lean_profile(render_poles(W, H, 4.0, 0.0, bearings=bearings + 15), n_bins=12)
    by_centre = {round(c): lean for c, lean, _ in pp}
    assert abs(by_centre[-75]) > 2.5 and abs(by_centre[105]) > 0.8
    assert abs(by_centre[15]) < 1.2 and abs(by_centre[-165]) < 1.2
    assert np.sign(by_centre[-75]) == -np.sign(by_centre[105])
    rp = tf.lean_profile(render_poles(W, H, 0.0, 4.0, bearings=bearings + 15), n_bins=12)
    by_centre = {round(c): lean for c, lean, _ in rp}
    assert abs(by_centre[15]) > 2.5 and abs(by_centre[-75]) < 1.2


def test_warp_then_measure_matches_first_order():
    base = render_poles(W, H, 0.0, 0.0)
    warped = tf.warp_with_extra_tilt(base, 3.0, -2.0)
    direct = render_poles(W, H, 3.0, -2.0)
    band = slice(int(H * (0.5 - 35 / 180)), int(H * (0.5 + 25 / 180)))
    assert np.mean(np.abs(warped[band] - direct[band])) < 2.0
    beta = _beta(tf.lean_profile(warped), 3.0, -2.0)
    assert 0.85 <= beta <= 1.3


def test_calibration_slope_is_one_on_clean_synthetic():
    img = render_poles(W, H, 1.0, 0.5)
    before, after, predicted_delta = tf.calibrate(img, extra=(3.0, 0.0))
    delta = np.array([a - b for (_, a, _), (_, b, _) in zip(after, before)])
    slope = float(np.dot(delta, predicted_delta) / np.dot(predicted_delta, predicted_delta))
    assert 0.85 <= slope <= 1.3


def test_sign_flip_would_be_caught():
    """Mutation guard in the test itself: flipping the lean sign turns beta negative."""
    prof = tf.lean_profile(render_poles(W, H, 4.0, 0.0))
    flipped = [(c, -lean, n) for c, lean, n in prof]
    assert _beta(flipped, 4.0, 0.0) < -0.8


def test_bins_with_too_few_pixels_are_none_not_zero():
    img = render_poles(W, H, 0.0, 0.0, bearings=[15.0])
    prof = tf.lean_profile(img)
    assert sum(1 for _, lean, _ in prof if lean is None) >= 10
    assert any(lean is not None for _, lean, _ in prof)


def test_load_reduced_gray_normalises_width(tmp_path):
    img = Image.fromarray(render_poles(2880, 1440, 0, 0).astype(np.uint8))
    path = tmp_path / 'p.jpg'
    img.save(path, quality=95)
    g = tf.load_reduced_gray(str(path), width=720)
    assert g.shape == (360, 720)


def test_main_writes_one_row_per_bin_and_variant(tmp_path):
    img = Image.fromarray(render_poles(W, H, 2.0, 1.0).astype(np.uint8))
    (tmp_path / 'ab').mkdir()
    img.save(tmp_path / 'ab' / 'abPANO.jpg', quality=95)
    panos = tmp_path / 'sel.csv'
    panos.write_text('city,pano_id,pitch_deg,roll_deg,arm\n,abPANO,2.0,1.0,test\n', encoding='utf-8')
    out = tmp_path / 'lean.csv'
    rc = tf.main(['--panos', str(panos), '--pano-root', str(tmp_path), '--calibrate', '--calibrate-roll-every', '1',
                  '--width', str(W), '--out', str(out)])
    assert rc == 0
    import csv
    rows = list(csv.DictReader(open(out, newline='', encoding='utf-8')))
    assert {r['variant'] for r in rows} == {'base', 'cal_pitch', 'cal_roll'}
    assert len(rows) == 3 * 12
    # resume: nothing re-run
    tf.main(['--panos', str(panos), '--pano-root', str(tmp_path), '--calibrate', '--width', str(W),
             '--out', str(out), '--resume'])
    assert len(list(csv.DictReader(open(out, newline='', encoding='utf-8')))) == 3 * 12


@pytest.mark.skipif(not glob.glob(os.path.join(PROBE_DIR, '*.jpg')), reason='probe panos not fetched (opt-in)')
def test_probe_panos_reproduce_the_planning_profile():
    """A regression check of the planning prototype, not a finding: the 2019 pano's bin 2 read
    -3.79 there, in the prototype's clockwise-positive sign; this estimator is counter-clockwise
    positive (tilt_geometry.vertical_lean_deg's sign), so the same edge reads +. Measured 2026-09-26:
    +3.09. The estimator is not the prototype (Sobel vs central differences, a fixed working width),
    so the tolerance is loose."""
    path = os.path.join(PROBE_DIR, '_03OWjj8xH_WkWLSZHZ0mg.jpg')
    prof = tf.lean_profile(tf.load_reduced_gray(path))
    assert prof[2][1] == pytest.approx(3.8, abs=1.0)


def _render_leaning(w, h, pitch, roll, poles, width_deg=3.0):
    """Poles at gravity bearing pb that lean by lam degrees in the world (clutter when lam != 0)."""
    ys, xs = np.mgrid[0:h, 0:w]
    b, el = tg.bearing_elevation_from_pixel(xs + 0.5, ys + 0.5, w, h)
    bg, elg = tg.rig_to_gravity(b, el, pitch, roll)
    img = np.zeros((h, w))
    for pb, lam in poles:
        centre = pb + elg * np.tan(np.radians(lam))
        d = np.abs(tg.wrap_deg(bg - centre)) * np.cos(np.radians(elg))
        img = np.maximum(img, np.clip(0.5 + (width_deg / 2 - d) / (360.0 / w), 0, 1))
    img[np.abs(elg) > 40] = 0
    return (40 + 180 * img).astype(np.float32)


def test_world_leaning_clutter_adds_noise_not_a_shortfall():
    """What is true of clutter, pooled over 12 scenes: the real tilt rotates world-leaning edges along
    with plumb ones, just as the calibration warp does, so the calibrated slope (raw / a) stays near 1.
    The first version of this test asserted a shortfall on four scenes of one seed and passed only on
    that seed (#158 review). A probe over six seeds of 12 scenes gave pooled ratios 0.87-1.18 (mean
    1.01); the bounds below would fail on the 20-37% shortfall the report used to attribute to clutter.
    Pooled sums, not a mean of per-scene ratios: a scene with little tilt has a tiny denominator."""
    rng = np.random.default_rng(3)
    sums = np.zeros(4)
    for _ in range(12):
        p, r = rng.normal(0, 2.5), rng.normal(0, 1.5)
        poles = [(b, 0.0) for b in np.arange(-165, 180, 30)]
        poles += [(b, rng.normal(0, 5)) for b in rng.uniform(-180, 180, 12)]
        img = _render_leaning(W, H, p, r, poles)
        prof = tf.lean_profile(img)
        c = np.array([x[0] for x in prof])
        m = np.array([x[1] for x in prof])
        pred = tf.predicted_lean(c, p, r)
        m, pred = m - m.mean(), pred - pred.mean()
        before, after, pd_ = tf.calibrate(img, extra=(2.0, 0.0))
        d = np.array([a[1] - b[1] for a, b in zip(after, before)])
        sums += (m @ pred, pred @ pred, d @ pd_, pd_ @ pd_)
    raw, a = sums[0] / sums[1], sums[2] / sums[3]
    assert 0.8 <= raw / a <= 1.25, (raw, a)
