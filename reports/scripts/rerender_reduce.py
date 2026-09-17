"""Reduce the re-render probe artifact into the per-panorama table in the #114 write-up.

    python rerender_reduce.py --probe reports/data/2026-09-06-rerender-probe.json \
        --photometa reports/data/2026-09-06-rerender-photometa.json \
        [--figure reports/figures/2026-09-06-rerender-dy-vs-heading.png]

Prints the markdown table that appears in reports/2026-09-06-rerender-probe.md, and with --figure redraws
the per-window vertical-shift figure beside it. It reads only committed artifacts and makes no requests, so
both can be regenerated - and the table is re-derived by tests/test_rerender_probe_report.py, which asserts
every cell it produces appears in the markdown.

Why this is a separate step rather than more summary in rerender_probe.py: the probe measures a *band*
against a band and stores every window it locked. Turning 16 per-window displacements into "the heading
moved by this much and the horizon tilted by that much" is a model of what a re-render did, and models
belong where they can be changed without re-running a measurement that needs both copies of 78
panoramas. The probe's own `summary` deliberately carries only distribution-free medians for that reason.

The fit, `dy ~ c + a*cos(phi) + b*sin(phi)` over the locked windows, has three components and every one of
them is reported, for every panorama:

* **heading offset** - the mean horizontal sub-pixel shift over locked windows. A re-stitch that only
  re-estimated the camera's yaw translates the whole frame sideways by a constant, so the mean is the
  estimate and the spread is the evidence that it really was uniform.
* **vertical offset** - the constant `c`. A uniform vertical shift is not a tilt (a tilt moves the two
  sides of the frame in opposite directions), but it is a displacement of every feature under its stored
  coordinate, which is the question this report exists to answer. The first draft absorbed it into the
  fit and dropped it, and one panorama's largest displacement went unreported.
* **tilt amplitude** - `hypot(a, b)`. A pitch or roll re-estimate cannot translate an equirectangular
  frame; it moves content *up* on one side of the panorama and *down* on the opposite side, so the
  vertical shift runs as one full sinusoid across heading and its amplitude is the size of the tilt.
* **fit residual** - the RMS the model leaves behind. It is what says whether the three numbers describe
  the windows or merely summarise them: on one panorama it exceeds every component.

The probe's `classify` label is reported beside these, not instead of them. Its thresholds were fixed
before the run, so it is the honest count of what the instrument was set to find; but its `shifted` test
keys on the median shift, which a sinusoid zeroes, and its `warped` test on +-1 px agreement, which a
1.2 px amplitude does not break - so it cannot see a small pure tilt. Whether a panorama moved at all is
taken from the probe's own first gate (`rerender_probe.moved`: at least half the locked windows shifted
by a whole pixel), and the fit then says what the movement looked like.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studyfmt  # noqa: E402
from rerender_probe import LOCK_PEAK, MOVED_FRACTION, moved  # noqa: E402,F401  (re-exported for tests)

# A fitted component this large is reported as a displacement the model explains. It is the probe's own
# `shifted` criterion - a sub-pixel median of at least one pixel - applied to the fit rather than to the
# raw median, so the table and the classifier draw the line at the same size of movement.
MODELLED_PX = 1.0

# The fit needs three locked windows for three parameters; below that its output is not a measurement.
MIN_FIT_WINDOWS = 3

# How the probe's machine labels read in a table a person is going to look at. `classify` in
# rerender_probe.py owns the labels themselves; this owns only their English, and says what each test
# actually looked at rather than what it was hoped to mean.
LABEL_TEXT = {
    'shifted': 'uniform shift',
    'warped': 'windows disagree',
    'sharpened': 'sharpened, no shift seen',
    'changed-unclassified': 'no shift seen',
    'same': 'same rendering',
    'no-lock': 'no window locked',
}

# The movement verdict, from the probe's gate plus the fit. `still`: fewer than half the locked windows
# shifted by a pixel. `modelled`: they did, and a fitted component is at least MODELLED_PX. `unmodelled`:
# they did, and the fit cannot name it. `no-lock`: nothing to say.
MOVEMENT_TEXT = {
    'still': 'no',
    'modelled': 'yes',
    'unmodelled': 'yes, no consistent model',
    'no-lock': 'unknown',
}


def locked_windows(record, band='horizon'):
    """The windows of one band that actually found a correspondence.

    An unlocked window's `dy_sub`/`dx_sub` are the argmax of noise, so averaging them in would pull both
    estimates towards zero in proportion to how featureless the band was - which is exactly backwards,
    since a smooth band is where a displacement is hardest to see and least safe to report.
    """
    return [w for w in record[band]['windows'] if w['peak'] >= LOCK_PEAK]


def azimuth_deg_per_px(width):
    """Angular size of one pixel horizontally: 360 degrees over the width."""
    return 360.0 / width


def elevation_deg_per_px(height):
    """Angular size of one pixel vertically: 180 degrees over the height.

    On a 2:1 frame this equals the azimuth scale, which is exactly why converting a vertical quantity
    through the width returns the right answer for the wrong reason - and why the two are kept apart.
    """
    return 180.0 / height


def fit_pose(record, band='horizon'):
    """The three-component fit over a band's locked windows, or None when fewer than MIN_FIT_WINDOWS lock.

    Returns heading_px (mean horizontal shift), vertical_px (the fitted constant), tilt_px (the sinusoid's
    amplitude), residual_px (RMS left by the fit) and n_locked. Below three windows the fit is not a
    measurement, so the answer is undefined rather than zero (the studyfmt rule).
    """
    windows = locked_windows(record, band)
    if len(windows) < MIN_FIT_WINDOWS:
        return None
    phi = np.array([2 * math.pi * w['x'] / record['width'] for w in windows])
    dy = np.array([w['dy_sub'] for w in windows])
    design = np.column_stack([np.ones_like(phi), np.cos(phi), np.sin(phi)])
    constant, cos_c, sin_c = np.linalg.lstsq(design, dy, rcond=None)[0]
    residual = dy - design @ np.array([constant, cos_c, sin_c])
    return {
        'heading_px': float(np.mean([w['dx_sub'] for w in windows])),
        'vertical_px': float(constant),
        'tilt_px': float(math.hypot(cos_c, sin_c)),
        'residual_px': float(math.sqrt(float(np.mean(residual * residual)))),
        'n_locked': len(windows),
    }


def heading_offset_px(record, band='horizon'):
    """Mean horizontal sub-pixel shift over locked windows, or None when the fit is undefined."""
    fit = fit_pose(record, band)
    return None if fit is None else fit['heading_px']


def vertical_offset_px(record, band='horizon'):
    """The fitted uniform vertical shift, or None when the fit is undefined."""
    fit = fit_pose(record, band)
    return None if fit is None else fit['vertical_px']


def tilt_amplitude_px(record, band='horizon'):
    """Amplitude of one sinusoid fitted to vertical shift against heading, or None if underdetermined."""
    fit = fit_pose(record, band)
    return None if fit is None else fit['tilt_px']


def displacement_px(fit):
    """The largest of the fit's three components: the furthest the model says a feature moved."""
    return max(abs(fit['heading_px']), abs(fit['vertical_px']), fit['tilt_px'])


def movement(record, band='horizon'):
    """'still', 'modelled', 'unmodelled' or 'no-lock' - see MOVEMENT_TEXT."""
    shift = record[band]['shift']
    if shift['n_locked'] == 0:
        return 'no-lock'
    if not moved(shift):
        return 'still'
    fit = fit_pose(record, band)
    if fit is not None and displacement_px(fit) >= MODELLED_PX:
        return 'modelled'
    return 'unmodelled'


def _component_deg(value, scale):
    return None if value is None else studyfmt.num(value * scale)


def table_row(record, meta):
    """One panorama's row of the report table, as the values behind the formatted cells."""
    horizon = record['horizon']
    fit = fit_pose(record)
    fit = fit or {'heading_px': None, 'vertical_px': None, 'tilt_px': None, 'residual_px': None}
    az = azimuth_deg_per_px(record['width'])
    el = elevation_deg_per_px(record['height'])
    return {
        'pano_id': record['pano_id'],
        'captured': meta.get(record['pano_id'], {}).get('capture_date'),
        'classifier': LABEL_TEXT.get(record['label'], record['label']),
        'movement': movement(record),
        'horizon_mae': studyfmt.num(horizon['mae']),
        'heading_px': _component_deg(fit['heading_px'], 1.0),
        'heading_deg': _component_deg(fit['heading_px'], az),
        'vertical_px': _component_deg(fit['vertical_px'], 1.0),
        'vertical_deg': _component_deg(fit['vertical_px'], el),
        'tilt_px': _component_deg(fit['tilt_px'], 1.0),
        'tilt_deg': _component_deg(fit['tilt_px'], el),
        'residual_px': _component_deg(fit['residual_px'], 1.0),
        'gain': studyfmt.num(horizon['affine']['gain']),
        'offset': studyfmt.num(horizon['affine']['offset']),
        'sharpness': studyfmt.num(horizon['lap_ratio_gain_corrected']),
        'bottom_mae': studyfmt.num(record['bottom']['mae']),
    }


def _signed(value, decimals):
    """A signed number whose rounded zero prints as +0.0, never -0.0: a component that rounds to nothing
    should read as nothing, and '-0.0' invites a reader to see a direction that is not there."""
    return format(round(value, decimals) + 0.0, '+.%df' % decimals)


def _px_and_deg(px, deg, signed):
    if px is None:
        return studyfmt.fmt(None)
    if signed:
        return '%s (%s°)' % (_signed(px, 1), _signed(deg, 3))
    return '%.1f (%.3f°)' % (px, deg)


def format_row(row):
    """The markdown cells for one row, in the report's column order.

    Kept beside `table_row` so the report's formatting has exactly one definition: the transcription test
    asserts these strings appear in the markdown, so a spec changed here changes the report's own contract.
    """
    return [
        '`%s`' % row['pano_id'],
        row['captured'] or 'unknown',
        row['classifier'],
        MOVEMENT_TEXT[row['movement']],
        studyfmt.fmt(row['horizon_mae'], '.1f'),
        _px_and_deg(row['heading_px'], row['heading_deg'], signed=True),
        _px_and_deg(row['vertical_px'], row['vertical_deg'], signed=True),
        _px_and_deg(row['tilt_px'], row['tilt_deg'], signed=False),
        studyfmt.fmt(row['residual_px'], '.2f'),
        '%s / %+d' % (studyfmt.fmt(row['gain'], '.2f'), round(row['offset'])),
        studyfmt.fmt(row['sharpness'], '.1f'),
        studyfmt.fmt(row['bottom_mae'], '.1f'),
    ]


def rerendered_records(probe):
    """Every re-rendered panorama's record, worst horizon MAE first - the report's ordering."""
    records = [r for r in probe['records'] if r.get('rerendered') and 'error' not in r]
    records.sort(key=lambda r: -r['horizon']['mae'])
    return records


def rerendered_rows(probe, meta):
    """Every re-rendered panorama's row, in the report's ordering."""
    return [table_row(r, meta) for r in rerendered_records(probe)]


HEADER = ['pano', 'captured', 'classifier (pre-run)', 'moved', 'horizon MAE', 'heading offset px',
          'vertical offset px', 'tilt amplitude px', 'fit residual px', 'tone gain / offset',
          'sharpness ×', 'bottom-band MAE']


def _window_heading_deg(window, width):
    """A window's centre as a heading across the panorama, in degrees."""
    from rerender_probe import WIN
    return (window['x'] + WIN / 2) * azimuth_deg_per_px(width)


def draw_figure(probe, path):
    """Per-window vertical shift against heading, one trace per panorama, the null beside it.

    Traces are coloured by the movement verdict rather than by the classifier label, because the
    verdict is what the table reports and the figure exists to show a reader the same thing the table
    says - the sinusoids the classifier misses are the ones this makes visible.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    colours = {'modelled': 'tab:red', 'unmodelled': 'tab:purple', 'still': 'tab:blue', 'no-lock': 'grey'}
    fig, (left, right) = plt.subplots(1, 2, figsize=(13, 4.6), sharey=True)
    groups = (
        (left, rerendered_records(probe), '%d re-rendered panoramas'),
        (right, [r for r in probe['records'] if not r.get('rerendered') and 'error' not in r],
         '%d same-rendering panoramas (null)'),
    )
    for axis, records, title in groups:
        seen = set()
        for record in records:
            verdict = movement(record)
            windows = sorted(locked_windows(record), key=lambda w: w['x'])
            label = 'moved: %s' % MOVEMENT_TEXT[verdict] if verdict not in seen else None
            seen.add(verdict)
            axis.plot([_window_heading_deg(w, record['width']) for w in windows],
                      [w['dy_sub'] for w in windows], marker='o', markersize=3, linewidth=1,
                      color=colours[verdict], alpha=0.8, label=label)
        axis.axhline(0, color='black', linewidth=0.8)
        axis.set_title(title % len(records))
        axis.set_xlabel('heading across the panorama (deg)')
        axis.set_xlim(0, 360)
        if records:
            axis.legend(loc='upper right', fontsize=8)
    left.set_ylabel('vertical shift, re-fetched vs stored (px, horizon band)')
    fig.suptitle('Per-window phase-correlation shift across heading: a pose re-estimate reads as a sinusoid')
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--probe', required=True)
    parser.add_argument('--photometa', required=True)
    parser.add_argument('--figure', metavar='PNG', help='also redraw the vertical-shift figure to this path')
    args = parser.parse_args(argv)

    with open(args.probe, encoding='utf8') as f:
        probe = json.load(f)
    with open(args.photometa, encoding='utf8') as f:
        meta = {r['pano_id']: r for r in json.load(f)}

    rows = rerendered_rows(probe, meta)
    print('| ' + ' | '.join(HEADER) + ' |')
    print('|' + '---|' * len(HEADER))
    for row in rows:
        print('| ' + ' | '.join(format_row(row)) + ' |')
    if args.figure:
        draw_figure(probe, args.figure)
        print('wrote %s' % args.figure)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
