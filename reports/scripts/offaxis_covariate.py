"""Off-axis click geometry: the zero-annotation covariate that tells a capture-side projection
error apart from rig tilt and from human placement bias.

SidewalkWebpage#4842 ("labels do not always look on target") lost its leading explanation when the
record-staleness study showed both of its example labels replay `exact` -- their stored records
reproduce their own pano_x/pano_y at 0 px. What is left splits three ways, across two frames:

  (i)   the click really was low       -- human placement and/or rig tilt (pano_y carries no tilt
                                          term); visible in Validate AND in stored pano_y
  (ii)  the render lost an offset      -- e.g. the 5 px vertical fudge Validate's projection dropped
                                          in the Jan 2026 pano-code consolidation (865b5b8a8);
                                          visible in Validate, INVISIBLE in stored pano_y
  (iii) capture-side projection error  -- the client's canvas->pano math is off; INVISIBLE in
                                          Validate (which renders from the same record, so it draws
                                          the marker exactly where the user clicked) but it mis-places
                                          every crop this repo cuts

The #54 placement study reads stored pano_x/pano_y against gold, so it sees (i) and (iii) and is
blind to (ii). This module supplies what separates (i) from (iii): the click's angular offset from
the viewport centre. A canvas<->pano projection error produces vertical error that grows with that
offset and vanishes at the canvas centre; rig tilt is bearing-driven and flat in it; placement bias
is constant in both.

Two facts make the covariate usable, and both are measured here rather than assumed:

* **It is identified.** The pre-registration's Study 1 carries depression-band fixed effects, so a
  covariate collinear with depression would be absorbed and estimate nothing. Off-axis offset is
  correlated with depression but far from determined by it -- `identification()` reports how much of
  its variation survives the band means.

* **It is heading-free, hence migration-proof.** pov_pitch depends only on (zoom, pitch, canvas_x,
  canvas_y): in `pov_if_centered`, x^2 + y^2 collapses to A^2 + B^2 with A = f*cos(p0) -
  dv*sin(p0), B = du*sgn, and the heading cancels exactly. So rows are restricted on `exact_y`
  (the record's vertical half reproduces stored pano_y) rather than on both axes. That keeps the
  `x_only` class -- 58% of the staleness misses, whose only stale field is the viewport heading the
  covariate does not read -- and it is exactly invariant to #4842's repair migration: that migration
  rotates heading for x_only rows and leaves pitch/zoom/canvas alone, while every row whose repair
  touches canvas or zoom (dpr2, zoom_desync, multi_field, xy_small) fails `exact_y` and is excluded
  here anyway. Restricting on both axes instead would discard 58% of the eligible rows to guard
  against a field that cannot reach the estimate.

The second registered covariate is the Explore viewport's **pitch floor**: the client cannot pitch
below -35 deg, so labels deeper than that must be clicked off-axis by construction, and the floor
cohort is where off-axis exposure concentrates.

Usage (offline; reads the rawLabels cache the era study already fetched):
    python offaxis_covariate.py <dir-of-city-csvs> --fetched 2026-08-09 \
        --write ../data/2026-08-11-offaxis-covariate.json
"""

import argparse
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import era_replay_study  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

# The Explore viewer's pitch floor. Not a documented constant -- measured: the minimum viewport
# pitch over 438k labels is exactly -35.0000 and a large share sit precisely on it. Compared with a
# tolerance because a handful of rows carry float noise from the legacy client's truncation.
PITCH_FLOOR_DEG = -35.0
PITCH_FLOOR_TOL = 0.01

# The pre-registration's Study 1 depression bands (reports/2026-08-09-crop-priors-prereg.md 2.1).
# Used verbatim so the identification claim is about the strata the study actually fits.
BAND_EDGES = [-90.0, 5.0, 15.0, 30.0, 90.0]
BAND_LABELS = ['<5', '5-15', '15-30', '>30']

# The canvas every in-scope row carries, and the frame Validate renders at.
CANVAS_W, CANVAS_H = 720.0, 480.0


def offaxis_offsets(df):
    """Per row: how far the click sits from the viewport centre, in degrees.

    Returns (vertical, radial). `vertical` is positive when the click is BELOW the viewport centre
    (pitch - pov_pitch); `radial` is the great-circle separation between the click direction and the
    viewport axis. Vertical is the one the elevation endpoint consumes; radial is reported because a
    radially-symmetric projection error (a wrong fov) shows in it while a purely vertical one
    (a lost pixel fudge) does not.
    """
    cw = df['canvas_width'] if 'canvas_width' in df else CANVAS_W
    ch = df['canvas_height'] if 'canvas_height' in df else CANVAS_H
    pov_h, pov_p = pov_replay.pov_if_centered(
        df['canvas_x'], df['canvas_y'], df['heading'], df['pitch'], df['zoom'], cw, ch)

    pitch = np.asarray(df['pitch'], float)
    heading = np.asarray(df['heading'], float)
    vertical = pitch - np.asarray(pov_p, float)

    dh = np.radians(((np.asarray(pov_h, float) - heading + 180.0) % 360.0) - 180.0)
    p1, p2 = np.radians(np.asarray(pov_p, float)), np.radians(pitch)
    cos_sep = np.sin(p1) * np.sin(p2) + np.cos(p1) * np.cos(p2) * np.cos(dh)
    radial = np.degrees(np.arccos(np.clip(cos_sep, -1.0, 1.0)))
    return vertical, radial


def at_pitch_floor(pitch):
    """Is the viewport pitched to the client's floor? NaN pitch is not at the floor."""
    p = np.asarray(pitch, float)
    return np.isfinite(p) & (p <= PITCH_FLOOR_DEG + PITCH_FLOOR_TOL)


def depression_band(depression_deg):
    """Bucket into the pre-registration's Study 1 bands; out-of-range/NaN becomes NaN."""
    return pd.cut(pd.Series(np.asarray(depression_deg, float)),
                  BAND_EDGES, labels=BAND_LABELS)


def deg_per_canvas_px(zoom, canvas_width=CANVAS_W):
    """Degrees of field of view per canvas pixel at a given zoom.

    This is what converts a canvas-frame error into the units every consumer threshold is stated in.
    At zoom 1 -- 70% of post-fix labels -- the GSV viewer's 89.75 deg fov over a 720 px canvas puts a
    5 px error at 0.62 deg, already above the 0.5 deg placement threshold the consumer survey set.
    """
    return np.asarray(pov_replay.get_3d_fov(zoom), float) / float(canvas_width)


def prepare(df):
    """Replay the frame, attach the covariates, and mark the eligible rows.

    Eligibility is `exact_y`: the stored record's vertical half reproduces stored pano_y exactly.
    See the module docstring for why this is the right restriction and not `exact_x & exact_y`.
    """
    out = era_replay_study.replay_frame(df)
    vertical, radial = offaxis_offsets(out)
    out['offaxis_v'] = vertical
    out['offaxis_r'] = radial
    out['at_floor'] = at_pitch_floor(out['pitch'])
    out['depression'] = pov_replay.depression_from_pano_y(out['pano_y'], out['pano_height'])
    out['band'] = depression_band(out['depression']).to_numpy()
    # No separate isfinite(depression) guard: exact_y already requires finite pano_y and a finite
    # positive pano_height, which is exactly what makes depression finite. The band guard is not
    # redundant in the same way -- it also rejects depressions outside the banded range.
    out['eligible'] = (out['exact_y'].to_numpy()
                       & np.isfinite(out['offaxis_v'])
                       & pd.notna(out['band']))
    return out


def identification(df):
    """Does off-axis offset survive the depression-band fixed effects Study 1 already carries?

    The band means are exactly what a band fixed effect removes, so the residual standard deviation
    is the variation left for a coefficient to be estimated from. A ratio near 1 means the covariate
    is essentially orthogonal to the strata; near 0 means the strata already absorb it and no
    coefficient is identifiable.
    """
    g = df[df['eligible']]
    if len(g) < 2:
        return {'n': int(len(g)), 'sd_overall_deg': None, 'sd_within_band_deg': None,
                'pct_surviving_band_fe': None, 'corr_with_depression': None}
    v = g['offaxis_v'].astype(float)
    resid = v - v.groupby(g['band'].astype(str)).transform('mean')
    sd_all, sd_in = float(v.std(ddof=1)), float(resid.std(ddof=1))
    return {
        'n': int(len(g)),
        'sd_overall_deg': sd_all,
        'sd_within_band_deg': sd_in,
        'pct_surviving_band_fe': float(100.0 * sd_in / sd_all) if sd_all else None,
        'corr_with_depression': float(v.corr(g['depression'].astype(float))),
    }


def _spread(series):
    a = np.asarray(series, float)
    if a.size == 0:
        return None
    return {'n': int(a.size), 'p5': float(np.percentile(a, 5)), 'p50': float(np.percentile(a, 50)),
            'p95': float(np.percentile(a, 95)), 'sd': float(a.std(ddof=1)) if a.size > 1 else 0.0}


def by_band(df):
    """Off-axis spread and floor exposure per Study 1 band -- the table that shows the covariate has
    within-stratum contrast rather than just tracking the strata."""
    g = df[df['eligible']]
    out = {}
    for band in BAND_LABELS:
        b = g[g['band'] == band]
        out[band] = {
            'n': int(len(b)),
            'offaxis_v_deg': _spread(b['offaxis_v']),
            'offaxis_r_p95_deg': float(np.percentile(b['offaxis_r'], 95)) if len(b) else None,
            'at_floor_pct': float(100.0 * b['at_floor'].mean()) if len(b) else None,
        }
    return out


def floor_census(df):
    """The pitch-floor prior: is -35 a hard floor, and how much of the corpus sits on it."""
    g = df[df['eligible']]
    pitch = g['pitch'].astype(float)
    return {
        'n': int(len(g)),
        'min_pitch_deg': float(pitch.min()) if len(g) else None,
        'max_pitch_deg': float(pitch.max()) if len(g) else None,
        'at_floor_pct': float(100.0 * g['at_floor'].mean()) if len(g) else None,
        'exactly_floor_pct': float(100.0 * (pitch == PITCH_FLOOR_DEG).mean()) if len(g) else None,
        'by_band_pct': {band: (float(100.0 * g.loc[g['band'] == band, 'at_floor'].mean())
                               if (g['band'] == band).any() else None)
                        for band in BAND_LABELS},
        'by_label_type_pct': {str(t): float(100.0 * sub['at_floor'].mean())
                              for t, sub in g.groupby('label_type', observed=True)},
    }


def eligibility(df):
    """How many rows the exact_y restriction keeps, and what requiring both axes would have cost."""
    n = int(len(df))
    ok_y = df['replayable_y'].to_numpy()
    both = (df['exact_x'].to_numpy() & df['exact_y'].to_numpy())
    return {
        'n_labels': n,
        'replayable_y': int(ok_y.sum()),
        'exact_y': int(df['exact_y'].sum()),
        'exact_x_and_y': int(both.sum()),
        'eligible': int(df['eligible'].sum()),
        'kept_by_using_exact_y_only': int(df['exact_y'].sum() - both.sum()),
    }


def zoom_conversions(df):
    """The canvas-px -> degrees ladder, weighted by how the corpus actually distributes over zoom."""
    g = df[df['eligible']]
    shares = g['zoom'].round(4).value_counts(normalize=True)
    out = {}
    for zoom in (1.0, 2.0, 3.0):
        dpp = float(deg_per_canvas_px(zoom))
        out[f'zoom{zoom:g}'] = {
            'fov_deg': float(pov_replay.get_3d_fov(zoom)),
            'deg_per_canvas_px': dpp,
            'deg_at_5px': 5 * dpp,
            'deg_at_20px': 20 * dpp,
            'corpus_share_pct': float(100.0 * shares.get(zoom, 0.0)),
        }
    return out


def analyze(df):
    """The full analysis dict for one prepared frame -- used for a single city and, on the
    concatenation, for the pooled result, so the two can never drift apart."""
    return {
        'eligibility': eligibility(df),
        'identification': identification(df),
        'identification_post179': identification(df[df['era'] == 'post179']),
        'by_band': by_band(df),
        'floor': floor_census(df),
        'zoom': zoom_conversions(df),
        'by_era_identification': {era: identification(g) for era, g in df.groupby('era')},
    }


def pooled(frames):
    """The pooled analysis -- the numbers the amendment cites. Cities are pooled rather than
    averaged because Study 1's corpus is city-mixed and its strata cross cities."""
    return analyze(pd.concat(frames, ignore_index=True))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--fetched', required=True, metavar='DATE',
                    help='the date the CSVs were fetched (rawLabels is a moving target, and the '
                         '#4842 repair migration will rewrite record fields when it lands)')
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args(argv)

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched,
              'restriction': 'exact_y (record vertical half reproduces stored pano_y)',
              'cities': {}}
    frames = []
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        city = os.path.splitext(os.path.basename(path))[0]
        print(f'-- {city}', flush=True)
        df = prepare(rawlabels.load_rawlabels(path))
        frames.append(df)
        result['cities'][city] = analyze(df)
        ident = result['cities'][city]['identification']
        print(f"   eligible {ident['n']:,}  sd {ident['sd_overall_deg']:.2f} deg  "
              f"survives band FE {ident['pct_surviving_band_fe']:.0f}%")

    result['pooled'] = pooled(frames)
    p = result['pooled']
    print(f"\npooled: {p['identification']['n']:,} eligible of "
          f"{p['eligibility']['n_labels']:,} labels")
    print(f"  off-axis sd {p['identification']['sd_overall_deg']:.2f} deg -> within band "
          f"{p['identification']['sd_within_band_deg']:.2f} deg "
          f"({p['identification']['pct_surviving_band_fe']:.0f}% survives the band fixed effects)")
    print(f"  corr with depression {p['identification']['corr_with_depression']:.3f}")
    print(f"  pitch floor {p['floor']['min_pitch_deg']:.4f} deg, "
          f"{p['floor']['at_floor_pct']:.2f}% of eligible rows on it")
    print(f"  by band: " + '  '.join(
        f"{b}={p['floor']['by_band_pct'][b]:.1f}%" for b in BAND_LABELS
        if p['floor']['by_band_pct'][b] is not None))
    print(f"  exact_y keeps {p['eligibility']['kept_by_using_exact_y_only']:,} rows that "
          f"exact_x AND exact_y would have dropped")

    if args.write:
        with open(args.write, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
