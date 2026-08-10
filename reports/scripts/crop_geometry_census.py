"""Crop-geometry census over a six-city label corpus: what the #47 seam fix actually touches, and
what the dims preflight beside it can and cannot see.

Written to settle three questions raised reviewing PR #77, each of which had been argued from the
code rather than from data:

1. **Is `pano_width`/`pano_height` a click-time snapshot or a per-pano join?** The answer decides
   what the dims preflight can catch. If the dims travel with the *label*, a pano re-served at a
   new resolution leaves old rows carrying the old frame and the preflight sees the disagreement.
   If they travel with the *pano*, the dims field is refreshed in place while `pano_x`/`pano_y`
   are not — a stale-coordinate row then presents perfectly consistent dims and no dims comparison
   can separate it.
2. **How many labels does the seam wrap actually rescue?** #47 reasoned from the *x-range* (~9% of
   columns on a 16384-wide pano). Labels are not uniform in x — the seam sits ahead of or behind
   the car, not on the sidewalk — so the label-weighted rate is the number that matters.
3. **How often does the vertical shift fire, and on what?** The shift trades centring for the
   absence of padding. If it fires on ordinary labels that is a systematic any placement study
   must carry as a covariate; if it fires only on corrupt rows, it is a bounds-check signal being
   spent as silent recovery.

Self-contained on purpose: it reads the rawLabels CSV export directly rather than importing the
desk-study loaders, so it runs on this branch without the Phase 1 study machinery (which is
tracked separately under #54/#32). The `predict_crop_size` and `compute_crop_box` replicas below
are pinned test-side against the real CropRunner source (ast-extracted), so a geometry change
breaks CI rather than silently invalidating this census. Unlike a sizing census, this one
deliberately does **not** drop out-of-range `pano_y` rows — those rows are the finding.

Corpus: `/v3/api/rawLabels?filetype=csv` per city, e.g.

    curl -o seattle-wa.csv 'https://sidewalk-sea.cs.washington.edu/v3/api/rawLabels?filetype=csv'

rawLabels is a moving target (labels accrue, gsv_data refreshes), so a fresh fetch will not
reproduce the committed summary bit-for-bit.

Usage:
    python reports/scripts/crop_geometry_census.py <dir-of-rawlabels-csvs> \
        --fetched 2026-08-09 --write reports/data/2026-08-10-crop-geometry-census.json
"""

import argparse
import glob
import json
import os

import numpy as np
import pandas as pd

# Only what the census reads. The text/JSON columns are the bulk of the bytes and none are used.
CENSUS_COLUMNS = ['label_id', 'pano_id', 'time_created',
                  'pano_x', 'pano_y', 'pano_width', 'pano_height']

# Every geometry input as float64: a blank must stay NaN, never read as pixel 0.
_FLOAT_COLUMNS = ['pano_x', 'pano_y', 'pano_width', 'pano_height']

# Panos labelled more than this far apart straddle any plausible re-serve, so they are the
# discriminating population for question 1: if dims were click-time, a pano that changed
# resolution between two of its own labels would show two different frames here.
LONG_SPAN_DAYS = 1460


def load_rawlabels(path):
    """One city's rawLabels CSV, floats for geometry and a UTC datetime for time_created."""
    df = pd.read_csv(path, usecols=CENSUS_COLUMNS,
                     dtype={c: 'float64' for c in _FLOAT_COLUMNS})
    df['time_created'] = pd.to_datetime(df['time_created'], unit='ms', utc=True)
    return df


def predict_crop_size(pano_y, pano_height):
    """Vectorized replica of CropRunner.predict_crop_size (pinned in tests/test_crop_geometry_census.py)."""
    old_pano_y = np.asarray(pano_height, float) / 2 - np.asarray(pano_y, float)
    distance = np.maximum(0.0, 19.80546390 + 0.01523952 * old_pano_y)
    with np.errstate(divide='ignore'):
        crop = np.where(distance > 0, 8725.6 * np.power(np.where(distance > 0, distance, 1.0),
                                                        -1.192), 1500.0)
    crop = np.where(distance == 0, 1500.0, crop)
    return np.clip(crop, 50.0, 1500.0)


def compute_crop_box(pano_x, pano_y, crop_size, pano_width, pano_height):
    """Vectorized replica of CropRunner.compute_crop_box (pinned in tests).

    np.round is round-half-to-even, matching Python's round(), so this reproduces the deployed
    banker's rounding rather than an approximation of it.
    """
    pano_x, pano_y = np.asarray(pano_x, float), np.asarray(pano_y, float)
    pano_width, pano_height = np.asarray(pano_width, float), np.asarray(pano_height, float)
    size = np.minimum(np.round(np.asarray(crop_size, float)), np.minimum(pano_width, pano_height))
    left = np.round(pano_x - size / 2) % pano_width
    top = np.maximum(0.0, np.minimum(np.round(pano_y - size / 2), pano_height - size))
    return left, top, size


def add_geometry_columns(df):
    """Per label: the crop window, and whether cutting it needs the seam wrap or the vertical shift.

    `wraps` and `shifts` are computed from the UNCLAMPED window — they answer "would a naive Pillow
    crop have run off the edge here", which is exactly the pre-#77 black-padding condition.
    """
    out = df.copy()
    x, y = np.asarray(df['pano_x'], float), np.asarray(df['pano_y'], float)
    w, h = np.asarray(df['pano_width'], float), np.asarray(df['pano_height'], float)

    predicted = np.round(predict_crop_size(y, h))
    size = np.minimum(predicted, np.minimum(w, h))
    left_raw = np.round(x - size / 2)
    top_raw = np.round(y - size / 2)

    out['crop_size'] = size
    out['wraps'] = (left_raw < 0) | (left_raw + size > w)
    out['shifts'] = (top_raw < 0) | (top_raw > h - size)
    out['size_capped'] = predicted > np.minimum(w, h)

    # The two "outside the frame" cases have opposite consequences, so they are counted apart.
    # x: benign. Column 0 and column w are the same place in the world, so the seam modulo is the
    #    CORRECT reading of any finite x — a row storing exactly x == w yields the identical window
    #    to x == 0, still centred. A bounds check on x would reject a recoverable label.
    # y: unrecoverable. The poles are not adjacent, there is no wrap to appeal to, and the vertical
    #    clamp silently relocates the window to a place the label is not in.
    out['x_outside_frame'] = (x < 0) | (x >= w)
    out['y_outside_frame'] = (y < 0) | (y >= h)
    return out


def dims_are_per_pano(df):
    """Question 1. If dims were a click-time snapshot, some pano somewhere would carry two."""
    per_pano = df.groupby('pano_id')[['pano_width', 'pano_height']].nunique()
    multi = (per_pano['pano_width'] > 1) | (per_pano['pano_height'] > 1)

    span = df.groupby('pano_id')['time_created'].agg(['min', 'max', 'size'])
    span['days'] = (span['max'] - span['min']).dt.days
    long_span = span[(span['size'] > 1) & (span['days'] > LONG_SPAN_DAYS)]
    long_rows = df[df['pano_id'].isin(long_span.index)]
    long_multi = long_rows.groupby('pano_id')['pano_height'].nunique() > 1

    buckets = {}
    framed = df.dropna(subset=['pano_width', 'pano_height'])
    for (bw, bh), g in framed.groupby(['pano_width', 'pano_height']):
        buckets['%dx%d' % (bw, bh)] = {'n': int(len(g)),
                                       'first_label': str(g['time_created'].min().date()),
                                       'last_label': str(g['time_created'].max().date())}
    return {
        'panos': int(len(per_pano)),
        'panos_with_multiple_dims': int(multi.sum()),
        'panos_labelled_over_4y_apart': int(len(long_span)),
        'labels_on_those_panos': int(len(long_rows)),
        'of_those_with_multiple_dims': int(long_multi.sum()),
        'dims_buckets': dict(sorted(buckets.items(), key=lambda kv: -kv[1]['n'])),
    }


def geometry_summary(df):
    """Questions 2 and 3, over the rows that carry a usable frame."""
    frac = (df['pano_y'] / df['pano_height'])[~df['y_outside_frame']]
    return {
        'n': int(len(df)),
        'seam_crossing': {'n': int(df['wraps'].sum()), 'pct': float(100 * df['wraps'].mean())},
        'vertical_shift': {'n': int(df['shifts'].sum()), 'pct': float(100 * df['shifts'].mean())},
        'size_capped_by_pano': {'n': int(df['size_capped'].sum()),
                                'pct': float(100 * df['size_capped'].mean())},
        'outside_frame': {'x_benign_wraps': int(df['x_outside_frame'].sum()),
                          'y_unrecoverable': int(df['y_outside_frame'].sum())},
        'pano_y_over_height': {str(p): float(np.percentile(frac, p)) for p in (0, 1, 50, 99, 100)},
    }


def outside_frame_rows(df):
    """Every row whose stored coordinate falls outside its own frame, verbatim — there are few
    enough to commit, and a consumer needs the ids to exclude the unrecoverable ones."""
    bad = df[df['x_outside_frame'] | df['y_outside_frame']]
    return [{'city': r.city, 'label_id': int(r.label_id), 'pano_id': r.pano_id,
             'pano_x': float(r.pano_x), 'pano_y': float(r.pano_y),
             'pano_width': float(r.pano_width), 'pano_height': float(r.pano_height),
             'crop_size': float(r.crop_size),
             'axis': 'y' if r.y_outside_frame else 'x',
             'recoverable': not bool(r.y_outside_frame),
             'shifts': bool(r.shifts)}
            for r in bad.itertuples()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir')
    ap.add_argument('--fetched', required=True)
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args()

    frames = []
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        df = load_rawlabels(path)
        df['city'] = os.path.splitext(os.path.basename(path))[0]
        frames.append(df)
    if not frames:
        raise SystemExit('no CSVs found in %s' % args.csv_dir)
    allc = pd.concat(frames, ignore_index=True)

    # A frame is needed to say anything about geometry; rows without one are third-party
    # photospheres, counted and set aside rather than silently dropped.
    framed = allc.dropna(subset=['pano_x', 'pano_y', 'pano_width', 'pano_height'])
    geo = add_geometry_columns(framed)

    result = {
        'source': '/v3/api/rawLabels?filetype=csv',
        'fetched': args.fetched,
        'corpus': {'cities': sorted(allc['city'].unique().tolist()),
                   'labels': int(len(allc)),
                   'labels_without_a_frame': int(len(allc) - len(framed))},
        'dims_are_per_pano': dims_are_per_pano(allc),
        'geometry': geometry_summary(geo),
        'geometry_by_city': {c: geometry_summary(g) for c, g in geo.groupby('city')},
        'outside_frame_rows': outside_frame_rows(geo),
    }

    d, g = result['dims_are_per_pano'], result['geometry']
    print(f"corpus: {result['corpus']['labels']} labels, {d['panos']} panos, "
          f"{result['corpus']['labels_without_a_frame']} without a frame")
    print(f"dims per-pano: {d['panos_with_multiple_dims']} panos carry >1 (w,h); "
          f"{d['of_those_with_multiple_dims']} of the {d['panos_labelled_over_4y_apart']} "
          f"panos labelled >4y apart do")
    print(f"seam crossing:  {g['seam_crossing']['n']:6d}  ({g['seam_crossing']['pct']:.2f}%)")
    print(f"vertical shift: {g['vertical_shift']['n']:6d}  ({g['vertical_shift']['pct']:.4f}%)")
    print(f"size capped:    {g['size_capped_by_pano']['n']:6d}")
    print(f"pano_y/height:  p0 {g['pano_y_over_height']['0']:.3f}  "
          f"p100 {g['pano_y_over_height']['100']:.3f}")
    print(f"outside the frame: {g['outside_frame']['x_benign_wraps']} in x (benign, the seam "
          f"modulo is correct), {g['outside_frame']['y_unrecoverable']} in y (unrecoverable)")
    for r in result['outside_frame_rows']:
        print(f"   {r['city']} label {r['label_id']}: ({r['pano_x']:.0f}, {r['pano_y']:.0f}) "
              f"in {r['pano_width']:.0f}x{r['pano_height']:.0f}  axis={r['axis']} "
              f"recoverable={r['recoverable']} shifts={r['shifts']}")

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(result, f, indent=1)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
