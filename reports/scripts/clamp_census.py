"""Clamp/truncation census of the deployed crop-size formula over the six-city label corpus.

CropRunner.predict_crop_size maps pano_y -> distance -> crop size with hard clamps at [50, 1500]
px. Three questions the #32 sizing study needs answered from production data before proposing a
replacement:

1. How often does the deployed formula saturate (a clamped crop no longer responds to distance,
   so every saturated label gets the same framing regardless of geometry)?
2. How often does the wanted crop run off the pano's bottom edge (truncation the #77 geometry
   handles by shifting — but a shifted crop de-centres the object, which the placement study must
   treat as a covariate)?
3. How much does the formula's resolution dependence matter in practice? Its distance term is
   linear in PIXELS from the horizon row, so the same physical depression maps to different
   distances at different served pano heights. It was fit in the 13312x6656 era; the corpus now
   mixes 16384x8192 and low-res panos.

The replica of predict_crop_size below is pinned test-side against the real CropRunner source
(ast-extracted), so a formula change breaks CI rather than silently invalidating this census.
Extends the 2026-08-07 pano_y histogram (reports/data/2026-08-07-pano-y-histogram.json), which
answered a different question (polar-band membership) over cvMetadata for two cities.

Usage:
    python clamp_census.py reports/scripts/.cache/rawlabels --fetched 2026-08-09 \
        --write reports/data/2026-08-09-clamp-census.json
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402


def predict_crop_size(pano_y, pano_height):
    """Vectorized replica of CropRunner.predict_crop_size (pinned in tests/test_clamp_census.py).

    distance = max(0, 19.80546390 + 0.01523952 * (h/2 - pano_y)) metres, crop = 8725.6 * d^-1.192
    px, clamped to [50, 1500]; distance == 0 forces 1500.
    """
    old_pano_y = np.asarray(pano_height, float) / 2 - np.asarray(pano_y, float)
    distance = np.maximum(0.0, 19.80546390 + 0.01523952 * old_pano_y)
    with np.errstate(divide='ignore'):
        crop = np.where(distance > 0, 8725.6 * np.power(np.where(distance > 0, distance, 1.0),
                                                        -1.192), 1500.0)
    crop = np.where(distance == 0, 1500.0, crop)
    return np.clip(crop, 50.0, 1500.0)


def add_census_columns(df):
    """crop_size, clamp flags, edge-truncation flags, depression, and the deployed-vs-blend
    distance disagreement, per label."""
    out = df.copy()
    y = np.asarray(df['pano_y'], float)
    h = np.asarray(df['pano_height'], float)
    size = predict_crop_size(y, h)
    out['crop_size'] = size
    out['clamp_1500'] = size == 1500.0
    out['clamp_50'] = size == 50.0
    out['truncated_bottom'] = y + size / 2 > h
    out['truncated_top'] = y - size / 2 < 0
    out['depression_deg'] = pov_replay.depression_from_pano_y(y, h)
    out['deployed_distance_m'] = np.maximum(0.0, 19.80546390 + 0.01523952 * (h / 2 - y))
    out['blend_distance_m'] = pov_replay.predict_blend_distance(out['depression_deg'])
    return out


def summarize_census(df):
    dep = df['depression_deg']
    d_gap = (df['deployed_distance_m'] - df['blend_distance_m']).abs()
    return {
        'n': int(len(df)),
        'clamp_1500_pct': float(100 * df['clamp_1500'].mean()),
        'clamp_50_pct': float(100 * df['clamp_50'].mean()),
        'truncated_bottom_pct': float(100 * df['truncated_bottom'].mean()),
        'truncated_top_pct': float(100 * df['truncated_top'].mean()),
        'depression_deg': {'p10': float(np.percentile(dep, 10)), 'p50': float(np.percentile(dep, 50)),
                           'p90': float(np.percentile(dep, 90)), 'p99': float(np.percentile(dep, 99))},
        'crop_size_px': {'p10': float(np.percentile(df['crop_size'], 10)),
                         'p50': float(np.percentile(df['crop_size'], 50)),
                         'p90': float(np.percentile(df['crop_size'], 90))},
        'deployed_vs_blend_distance_m': {'p50': float(d_gap.median()),
                                         'p90': float(np.percentile(d_gap, 90))},
    }


def resolution_dependence(df):
    """The deployed formula's height sensitivity, measured on the corpus: per common pano height,
    the crop size the formula gives at that height vs what it would give for the SAME depression
    in the 6656 frame it was fit in."""
    out = {}
    for h, g in df.groupby('pano_height'):
        if len(g) < 500:
            continue
        dep = g['depression_deg']
        here = predict_crop_size(h / 2 + dep / 90 * (h / 2), np.full(len(g), h))
        fitted_frame = predict_crop_size(6656 / 2 + dep / 90 * (6656 / 2),
                                         np.full(len(g), 6656.0))
        ratio = here / fitted_frame
        out[f'{int(h)}'] = {'n': int(len(g)),
                            'share_pct': float(100 * len(g) / len(df)),
                            'crop_ratio_vs_6656_p50': float(np.median(ratio)),
                            'crop_ratio_vs_6656_p90': float(np.percentile(ratio, 90))}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir')
    ap.add_argument('--fetched', required=True)
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args()

    frames = []
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        df = rawlabels.load_rawlabels(path)
        df['city'] = os.path.splitext(os.path.basename(path))[0]
        ok = (df['pano_width'].gt(0) & df['pano_height'].gt(0)
              & df['pano_y'].ge(0) & df['pano_y'].le(df['pano_height']))
        frames.append(df[ok])
    allc = add_census_columns(pd.concat(frames, ignore_index=True))

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched,
              'overall': summarize_census(allc),
              'by_label_type': {lt: summarize_census(g)
                                for lt, g in allc.groupby('label_type') if len(g) >= 1000},
              'by_city': {c: summarize_census(g) for c, g in allc.groupby('city')},
              'resolution_dependence': resolution_dependence(allc)}

    o = result['overall']
    print(f"n={o['n']}  clamp1500 {o['clamp_1500_pct']:.2f}%  clamp50 {o['clamp_50_pct']:.3f}%  "
          f"trunc_bottom {o['truncated_bottom_pct']:.3f}%  "
          f"dep p50 {o['depression_deg']['p50']:.2f} deg")
    for lt, s in sorted(result['by_label_type'].items()):
        print(f"  {lt:22s} clamp1500 {s['clamp_1500_pct']:6.2f}%  dep p50 {s['depression_deg']['p50']:6.2f}")
    for hh, s in result['resolution_dependence'].items():
        print(f"  height {hh:6s} share {s['share_pct']:5.1f}%  crop ratio vs 6656: p50 {s['crop_ratio_vs_6656_p50']:.3f}")

    if args.write:
        with open(args.write, 'w') as f:
            json.dump(result, f, indent=1)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
