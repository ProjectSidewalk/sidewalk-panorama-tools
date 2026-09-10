"""Reduce the re-render probe artifact into the per-panorama table in the #114 write-up.

    python rerender_reduce.py --probe reports/data/2026-09-06-rerender-probe.json \
        --photometa reports/data/2026-09-06-rerender-photometa.json

Prints the markdown table that appears in reports/2026-09-06-rerender-probe.md. It reads only committed
artifacts and makes no requests, so the table in the report can be regenerated - and is re-derived by
tests/test_rerender_probe_report.py, which asserts every cell it produces appears in the markdown.

Why this is a separate step rather than more summary in rerender_probe.py: the probe measures a *band*
against a band and stores every window it locked. Turning 16 per-window displacements into "the heading
moved by this much and the horizon tilted by that much" is a model of what a re-render did, and models
belong where they can be changed without re-running a measurement that needs both copies of 78
panoramas. The probe's own `summary` deliberately carries only distribution-free medians for that reason.

The two quantities, and why a mean and an amplitude rather than one number:

* **heading offset** - the mean horizontal sub-pixel shift over locked windows. A re-stitch that only
  re-estimated the camera's yaw translates the whole frame sideways by a constant, so the mean is the
  estimate and the spread is the evidence that it really was uniform.
* **tilt amplitude** - a pitch or roll re-estimate cannot translate an equirectangular frame; it moves
  content *up* on one side of the panorama and *down* on the opposite side. So the vertical shift runs
  as one full sinusoid across heading, and its amplitude is the size of the tilt. Fitting
  `dy ~ c + a*cos(phi) + b*sin(phi)` and reporting `hypot(a, b)` is that amplitude; the constant `c`
  absorbs a uniform vertical offset, which a tilt is not.
"""
import argparse
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import studyfmt  # noqa: E402

# A window whose phase-correlation peak is below this found no correspondence; must match the probe's own
# LOCK_PEAK, since the two describe the same windows. rerender_probe.LOCK_PEAK is the definition.
LOCK_PEAK = 0.10

# How the probe's machine labels read in a table a person is going to look at. `classify` in
# rerender_probe.py owns the labels themselves; this owns only their English.
LABEL_TEXT = {
    'warped': 're-posed (heading + tilt)',
    'shifted': 're-posed (heading)',
    'sharpened': 'sharpened, no displacement',
    'changed-unclassified': 'no displacement',
    'same': 'same rendering',
}


def locked_windows(record, band='horizon'):
    """The windows of one band that actually found a correspondence.

    An unlocked window's `dy_sub`/`dx_sub` are the argmax of noise, so averaging them in would pull both
    estimates towards zero in proportion to how featureless the band was - which is exactly backwards,
    since a smooth band is where a displacement is hardest to see and least safe to report.
    """
    return [w for w in record[band]['windows'] if w['peak'] >= LOCK_PEAK]


def degrees_per_pixel(width):
    """Angular size of one pixel: 360 degrees over the width.

    For a 2:1 equirectangular frame the vertical scale (180 degrees over the height) is identical, so one
    number serves both axes - which is why a heading offset and a tilt amplitude in pixels convert the
    same way.
    """
    return 360.0 / width


def heading_offset_px(record, band='horizon'):
    """Mean horizontal sub-pixel shift over locked windows, or None when nothing locked."""
    windows = locked_windows(record, band)
    if not windows:
        return None
    return float(np.mean([w['dx_sub'] for w in windows]))


def tilt_amplitude_px(record, band='horizon'):
    """Amplitude of one sinusoid fitted to vertical shift against heading, or None if underdetermined.

    Three locked windows is the minimum for a three-parameter fit; below that the amplitude is not a
    measurement, so it is undefined rather than zero (the studyfmt rule).
    """
    windows = locked_windows(record, band)
    if len(windows) < 3:
        return None
    phi = np.array([2 * math.pi * w['x'] / record['width'] for w in windows])
    dy = np.array([w['dy_sub'] for w in windows])
    design = np.column_stack([np.ones_like(phi), np.cos(phi), np.sin(phi)])
    _, cos_c, sin_c = np.linalg.lstsq(design, dy, rcond=None)[0]
    return float(math.hypot(cos_c, sin_c))


def table_row(record, meta):
    """One panorama's row of the report table, as the values behind the formatted cells."""
    horizon = record['horizon']
    scale = degrees_per_pixel(record['width'])
    heading = heading_offset_px(record)
    tilt = tilt_amplitude_px(record)
    displaced = record['label'] in ('shifted', 'warped')
    return {
        'pano_id': record['pano_id'],
        'captured': meta.get(record['pano_id'], {}).get('capture_date'),
        'what_changed': LABEL_TEXT.get(record['label'], record['label']),
        'horizon_mae': studyfmt.num(horizon['mae']),
        # A panorama the classifier found no displacement on reports 0, not the residual noise of a fit to
        # windows that agree on nothing: the table's job is to say "this did not move".
        'heading_px': studyfmt.num(heading) if displaced and heading is not None else 0.0,
        'heading_deg': studyfmt.num(heading * scale) if displaced and heading is not None else 0.0,
        'tilt_px': studyfmt.num(tilt) if displaced and tilt is not None else 0.0,
        'tilt_deg': studyfmt.num(tilt * scale) if displaced and tilt is not None else 0.0,
        'gain': studyfmt.num(horizon['affine']['gain']),
        'offset': studyfmt.num(horizon['affine']['offset']),
        'sharpness': studyfmt.num(horizon['lap_ratio_gain_corrected']),
        'bottom_mae': studyfmt.num(record['bottom']['mae']),
    }


def format_row(row):
    """The markdown cells for one row, in the report's column order.

    Kept beside `table_row` so the report's formatting has exactly one definition: the transcription test
    asserts these strings appear in the markdown, so a spec changed here changes the report's own contract.
    """
    degree = '°'
    return [
        '`%s`' % row['pano_id'],
        row['captured'] or 'unknown',
        row['what_changed'],
        studyfmt.fmt(row['horizon_mae'], '.1f'),
        '0' if not row['heading_px'] else '%+.1f (%+.3f%s)' % (row['heading_px'], row['heading_deg'], degree),
        '0' if not row['tilt_px'] else '%.1f (%.3f%s)' % (row['tilt_px'], row['tilt_deg'], degree),
        '%s / %+d' % (studyfmt.fmt(row['gain'], '.2f'), round(row['offset'])),
        studyfmt.fmt(row['sharpness'], '.1f'),
        studyfmt.fmt(row['bottom_mae'], '.1f'),
    ]


def rerendered_rows(probe, meta):
    """Every re-rendered panorama's row, worst horizon MAE first - the report's ordering."""
    records = [r for r in probe['records'] if r.get('rerendered') and 'error' not in r]
    records.sort(key=lambda r: -r['horizon']['mae'])
    return [table_row(r, meta) for r in records]


HEADER = ['pano', 'captured', 'what changed', 'horizon MAE', 'heading offset px', 'tilt amplitude px',
          'tone gain / offset', 'sharpness ×', 'bottom-band MAE']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--probe', required=True)
    parser.add_argument('--photometa', required=True)
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
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
