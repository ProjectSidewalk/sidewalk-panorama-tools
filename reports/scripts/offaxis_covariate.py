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
  here anyway. Restricting on both axes instead would discard 3.1% of the eligible rows (13,485 of
  433,866) to guard against a field that cannot reach the estimate. Do not confuse that cost with
  the 58% above: 58% is the x_only share of the record *misses*, not of the eligible corpus.

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

# The Explore viewer's three zoom stops. Stored zoom is a float and a small tail sits between them;
# see zoom_conversions, which reports that tail instead of letting it fall out of the census.
LADDER_ZOOMS = (1.0, 2.0, 3.0)

# The two labels SidewalkWebpage#4842 was filed with, as stored (records quoted in the issue and in
# the record-staleness study, which found both replay `exact`). They are not in this study's six-city
# corpus, so their off-axis values are computed from these records rather than joined -- committed
# here so the report's section 4 reproduces from the repo like every other number it cites.
SPECIMENS = {
    'teaneck-nj 14955': {'heading': 298.25, 'pitch': -35.0, 'zoom': 1.0,
                         'canvas_x': 451.0, 'canvas_y': 142.0},
    'chicago-il 30652': {'heading': 320.5, 'pitch': -35.0, 'zoom': 1.0,
                         'canvas_x': 361.0, 'canvas_y': 83.0},
}

# The canvas frame is NOT redeclared here. pov_replay.CANVAS_W/CANVAS_H is the one definition, and
# era_replay_study.frame_pov is the one place that applies it -- a local copy meant eligibility (from
# replay_frame) and the covariate (from offaxis_offsets) could be computed against two different
# canvases with no test able to see it.


def offaxis_offsets(df):
    """Per row: how far the click sits from the viewport centre, in degrees.

    Returns (vertical, radial). `vertical` is positive when the click is BELOW the viewport centre
    (pitch - pov_pitch); `radial` is the great-circle separation between the click direction and the
    viewport axis. Vertical is the one the elevation endpoint consumes; radial is reported because a
    radially-symmetric projection error (a wrong fov) shows in it while a purely vertical one
    (a lost pixel fudge) does not.

    Reads the replayed POV from the `pov_heading`/`pov_pitch` columns `era_replay_study.replay_frame`
    writes, and falls back to `era_replay_study.frame_pov` for a bare frame. Both branches call the
    same projection, so the fallback cannot drift from what eligibility was computed against; what it
    does cost is a second projection pass, which is why `prepare` goes through `replay_frame` first.
    """
    if 'pov_heading' in df and 'pov_pitch' in df:
        pov_h = np.asarray(df['pov_heading'], float)
        pov_p = np.asarray(df['pov_pitch'], float)
    else:
        pov_h, pov_p = era_replay_study.frame_pov(df)

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


def deg_per_canvas_px(zoom, canvas_width=None):
    """Degrees of field of view per canvas pixel at a given zoom.

    This is what converts a canvas-frame error into the units every consumer threshold is stated in.
    At zoom 1 -- 64.4% of eligible labels -- the GSV viewer's 89.75 deg fov over a 720 px canvas puts
    a 5 px error at 0.62 deg, already above the 0.5 deg placement threshold the consumer survey set.
    (An earlier revision cited "70% of post-fix labels": that was the abandoned post-fix-only probe's
    population, which the study rejected in favour of all-era `exact_y` rows. Every figure here is
    over the eligible corpus, matching reports/data/2026-08-11-offaxis-covariate.json.)

    The default canvas comes from pov_replay, the module that documents the front end's frame; there
    is deliberately no second copy of that constant in this file.
    """
    if canvas_width is None:
        canvas_width = pov_replay.CANVAS_W
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
    # Two guards that looked necessary are absent, both for the same reason: exact_y already implies
    # them. It requires a finite pov_pitch, finite pano_y and a finite positive pano_height, which is
    # exactly what makes depression finite -- and offaxis_v = pitch - pov_pitch cannot be non-finite
    # while pov_pitch is finite, because a non-finite pitch propagates through cos(p0) into all three
    # of x/y/z and makes pov_pitch non-finite too. So neither isfinite(depression) nor
    # isfinite(offaxis_v) can change this mask; both are omitted rather than kept as terms no test
    # could exercise. TestEligibility pins the *observable* consequence (such rows are ineligible),
    # which is what would actually break if exact_y were ever weakened. The band guard IS load-bearing
    # -- it also rejects depressions outside the banded range.
    out['eligible'] = out['exact_y'].to_numpy() & pd.notna(out['band']).to_numpy()
    return out


def _num(x):
    """A float for the artifact, or None when the quantity is undefined.

    Every number this module publishes goes through here. `main()` writes with allow_nan=False, so a
    NaN that reaches the dict aborts the run at the last line; and an artifact that *did* accept it
    would be unreadable by jq and JSON.parse (reports/data/2026-08-09-photometa-census.json shipped
    with 4,916 bare NaN tokens once). null is also the honest encoding: undefined is not zero.
    """
    x = float(x)
    return x if np.isfinite(x) else None


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
    dep = g['depression'].astype(float)
    resid = v - v.groupby(g['band'].astype(str)).transform('mean')
    sd_all, sd_in = float(v.std(ddof=1)), float(resid.std(ddof=1))
    # Pearson's r is undefined when either series is constant, and pandas answers NaN (after an
    # `invalid value encountered in divide` warning) rather than raising. A bare NaN here is not a
    # local blemish: main() ends in json.dump(..., allow_nan=False), so one constant subgroup -- a
    # handful of duplicate labels on one pano is enough -- aborts the whole run after all the compute,
    # and without allow_nan it would instead ship a non-standard artifact (the 4,916-NaN incident,
    # tests/test_committed_data_files.py). Reported as null for the same reason
    # pct_surviving_band_fe is: undefined must not read as a measurement.
    sd_dep = float(dep.std(ddof=1))
    corr = float(v.corr(dep)) if sd_all and sd_dep else float('nan')
    identified = np.isfinite(sd_all) and np.isfinite(sd_in) and sd_all != 0.0
    return {
        'n': int(len(g)),
        'sd_overall_deg': _num(sd_all),
        'sd_within_band_deg': _num(sd_in),
        'pct_surviving_band_fe': float(100.0 * sd_in / sd_all) if identified else None,
        'corr_with_depression': _num(corr),
    }


def _spread(series):
    """Percentiles and sample sd, or None for an empty group.

    `sd` is null rather than 0.0 for a single-row group: with ddof=1 the sample sd of one value is
    undefined, and publishing 0.0 would make a one-label band indistinguishable in the report's table
    from a band whose off-axis offset genuinely has no spread. Same convention as `identification`.
    """
    a = np.asarray(series, float)
    if a.size == 0:
        return None
    return {'n': int(a.size), 'p5': float(np.percentile(a, 5)), 'p50': float(np.percentile(a, 50)),
            'p95': float(np.percentile(a, 95)),
            'sd': _num(a.std(ddof=1)) if a.size > 1 else None}


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
        # n per type, not just the rate. Pooled the thin types are still thousands of labels, but this
        # function also runs per city, and there a bare percentage reads as a measurement when it is
        # not: oradell-nj 'Other' is 0.0% from 12 labels and newberg-or 'Crosswalk' is 23.8% from 42,
        # printed beside a 12.07% drawn from 148,796. Every other block in this module carries its n;
        # this one did not, so nothing in the artifact let a consumer rank types safely.
        'by_label_type': {str(t): {'n': int(len(sub)),
                                   'at_floor_pct': _num(100.0 * sub['at_floor'].mean())}
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
    """The canvas-px -> degrees ladder, weighted by how the corpus actually distributes over zoom.

    The ladder has three rungs because the Explore viewer's zoom control has three stops -- but stored
    zoom is a float, and 280 of the 433,866 eligible rows (0.065%, 49 distinct values such as 1.6818
    and 2.4689, all strictly inside (1, 3)) carry a *fractional* zoom between the stops, written by
    clients that interpolated it continuously. `get_3d_fov` is continuous too, so those rows have a
    well-defined fov (29.4-88.3 deg); what they lack is a rung. They are reported under `other`
    rather than dropped -- and their n plus the three rungs' n is the eligible count exactly, which is
    the property a test pins. Before this the three ladder shares summed to 99.94% while the report's
    table read as a census, and that gap is exactly how a re-fetch moving a real share of the corpus
    off-ladder would go unnoticed.
    """
    g = df[df['eligible']]
    z = g['zoom'].round(4)
    n = int(len(g))
    share = lambda k: _num(100.0 * k / n) if n else None    # noqa: E731
    out = {'n_eligible': n}
    for zoom in LADDER_ZOOMS:
        k = int((z == zoom).sum())
        dpp = float(deg_per_canvas_px(zoom))
        out[f'zoom{zoom:g}'] = {
            'fov_deg': float(pov_replay.get_3d_fov(zoom)),
            'deg_per_canvas_px': dpp,
            'deg_at_5px': 5 * dpp,
            'deg_at_20px': 20 * dpp,
            'n': k,
            'corpus_share_pct': share(k),
        }
    rest = z[~z.isin(LADDER_ZOOMS)]
    out['other'] = {
        'n': int(len(rest)),
        'corpus_share_pct': share(len(rest)),
        'n_distinct_zooms': int(rest.nunique()),
        'min_zoom': _num(rest.min()) if len(rest) else None,
        'max_zoom': _num(rest.max()) if len(rest) else None,
        # fov falls with zoom, so the widest fov belongs to the smallest zoom.
        'fov_deg_range': [_num(pov_replay.get_3d_fov(rest.max())),
                          _num(pov_replay.get_3d_fov(rest.min()))] if len(rest) else None,
    }
    return out


def specimen_census(by_band_result):
    """Where #4842's two example labels sit in the covariate's distribution.

    Their off-axis values are computed from the records in `SPECIMENS`; `beyond_p5_bands` lists the
    Study 1 bands whose 5th percentile the value falls below, which is the tail-membership claim the
    report makes and which is worth computing rather than asserting -- the report first said both
    specimens were beyond p5 of *every* band, and only one of them is.
    """
    df = pd.DataFrame.from_dict(SPECIMENS, orient='index')
    vertical, radial = offaxis_offsets(df)
    p5 = {band: (stats['offaxis_v_deg'] or {}).get('p5') for band, stats in by_band_result.items()}
    out = {}
    for i, name in enumerate(df.index):
        v = float(vertical[i])
        out[name] = {
            'record': dict(SPECIMENS[name]),
            'offaxis_v_deg': _num(v),
            'offaxis_r_deg': _num(radial[i]),
            'at_pitch_floor': bool(at_pitch_floor([SPECIMENS[name]['pitch']])[0]),
            'beyond_p5_bands': [b for b in BAND_LABELS if p5.get(b) is not None and v < p5[b]],
        }
    return out


def analyze(df):
    """The full analysis dict for one prepared frame -- used for a single city and, on the
    concatenation, for the pooled result, so the two can never drift apart.

    There is deliberately no `identification_post179` key: it was byte-identical to
    by_era_identification['post179'] (same rows, same predicate), cost a second pass over 90k rows,
    and shipped as a second key a consumer could read as a distinct quantity.
    """
    return {
        'eligibility': eligibility(df),
        'identification': identification(df),
        'by_band': by_band(df),
        'floor': floor_census(df),
        'zoom': zoom_conversions(df),
        'by_era_identification': {era: identification(g) for era, g in df.groupby('era')},
    }


def pooled(frames):
    """The pooled analysis -- the numbers the amendment cites. Cities are pooled rather than
    averaged because Study 1's corpus is city-mixed and its strata cross cities."""
    return analyze(pd.concat(frames, ignore_index=True))


def _fmt(value, spec=''):
    """Format a number the analysis may legitimately report as None.

    Every statistic above is null for a degenerate group by design -- `identification` returns nulls
    for fewer than two eligible rows, a contract its own test pins. Format-specing those directly
    (`f"{v:.2f}"`) raises TypeError, which in a per-city loop means one thin city aborts the run after
    every other city has been computed and before --write is reached. Print 'n/a' instead.
    """
    return 'n/a' if value is None else format(value, spec)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--fetched', required=True, metavar='DATE',
                    help='the date the CSVs were fetched (rawLabels is a moving target, and the '
                         '#4842 repair migration will rewrite record fields when it lands)')
    ap.add_argument('--write', metavar='JSON')
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.csv_dir, '*.csv')))
    if not paths:
        # Without this the empty list reaches pd.concat and dies as "No objects to concatenate",
        # which names neither the directory nor the fact that it was the input that was empty.
        ap.error(f'no *.csv rawLabels exports found in {args.csv_dir} '
                 f'(fetch them with fetch_rawlabels.py, which writes to '
                 f'reports/scripts/.cache/rawlabels/)')

    result = {'source': '/v3/api/rawLabels?filetype=csv', 'fetched': args.fetched,
              'restriction': 'exact_y (record vertical half reproduces stored pano_y)',
              'cities': {}}
    frames = []
    for path in paths:
        city = os.path.splitext(os.path.basename(path))[0]
        print(f'-- {city}', flush=True)
        df = prepare(rawlabels.load_rawlabels(path))
        frames.append(df)
        result['cities'][city] = analyze(df)
        ident = result['cities'][city]['identification']
        print(f"   eligible {ident['n']:,}  sd {_fmt(ident['sd_overall_deg'], '.2f')} deg  "
              f"survives band FE {_fmt(ident['pct_surviving_band_fe'], '.0f')}%")

    result['pooled'] = pooled(frames)
    p = result['pooled']
    result['specimens'] = specimen_census(p['by_band'])
    print(f"\npooled: {p['identification']['n']:,} eligible of "
          f"{p['eligibility']['n_labels']:,} labels")
    print(f"  off-axis sd {_fmt(p['identification']['sd_overall_deg'], '.2f')} deg -> within band "
          f"{_fmt(p['identification']['sd_within_band_deg'], '.2f')} deg "
          f"({_fmt(p['identification']['pct_surviving_band_fe'], '.0f')}% survives the band "
          f"fixed effects)")
    print(f"  corr with depression {_fmt(p['identification']['corr_with_depression'], '.3f')}")
    print(f"  pitch floor {_fmt(p['floor']['min_pitch_deg'], '.4f')} deg, "
          f"{_fmt(p['floor']['at_floor_pct'], '.2f')}% of eligible rows on it")
    print(f"  by band: " + '  '.join(
        f"{b}={p['floor']['by_band_pct'][b]:.1f}%" for b in BAND_LABELS
        if p['floor']['by_band_pct'][b] is not None))
    print(f"  zoom ladder {_fmt(p['zoom']['zoom1']['corpus_share_pct'], '.1f')}/"
          f"{_fmt(p['zoom']['zoom2']['corpus_share_pct'], '.1f')}/"
          f"{_fmt(p['zoom']['zoom3']['corpus_share_pct'], '.1f')}%, "
          f"{p['zoom']['other']['n']:,} rows off-ladder at fractional zoom")
    print(f"  exact_y keeps {p['eligibility']['kept_by_using_exact_y_only']:,} rows that "
          f"exact_x AND exact_y would have dropped")
    for name, s in result['specimens'].items():
        print(f"  #4842 {name}: off-axis {_fmt(s['offaxis_v_deg'], '.2f')} deg, beyond p5 of "
              f"{', '.join(s['beyond_p5_bands']) or 'no band'}")

    if args.write:
        with open(args.write, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, allow_nan=False)
        print(f'wrote {args.write}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
