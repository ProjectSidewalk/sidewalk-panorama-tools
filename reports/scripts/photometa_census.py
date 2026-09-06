"""Photometa census over a stratified sample of labeled panos: alive-rate, served-vs-stored dims
drift, camera pitch/roll distribution (the #54 tilt prior), and depth-map coverage.

One streetlevel photometa request per sampled pano — the same call path the production depth
phase uses (downloaders/gsv.py), paced by --interval to stay polite. The sample is stratified by
city x label-era (a pano's era is its earliest label's era) with a fixed seed, and the drawn
manifest is embedded in the output JSON so the exact sample can be re-fetched later to measure
decay.

Units: streetlevel serves camera heading/pitch/roll in radians; records store degrees.
Dims drift compares the max-zoom served size against the pano_width/height that rawLabels
carries for the pano's labels (what the client saw at label time / what gsv_data holds now).

Usage:
    python photometa_census.py reports/scripts/.cache/rawlabels --fetched 2026-08-09 \
        --per-stratum 85 --interval 0.25 --write reports/data/2026-08-09-photometa-census.json

    # decay: the same manifest, asked again. No rawLabels cache needed, so this runs anywhere.
    python photometa_census.py --refetch reports/data/2026-08-09-photometa-census.json \
        --fetched 2026-09-06 --since-days 28 --interval 0.25 \
        --write reports/data/2026-09-06-photometa-census.json
"""

import argparse
import glob
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rawlabels  # noqa: E402
from studyfmt import fmt  # noqa: E402

SEED = 20260809


def build_sample(df, per_stratum, seed):
    """One row per pano, stratified by (city, era-of-earliest-label), per_stratum panos each
    (fewer where a stratum is small), deterministic under seed."""
    first = (df.sort_values('time_created')
               .drop_duplicates('pano_id', keep='first')
               .rename(columns={'pano_width': 'stored_width', 'pano_height': 'stored_height'}))
    rng = np.random.default_rng(seed)
    picks = []
    for _, g in first.groupby(['city', 'era'], sort=True):
        take = min(per_stratum, len(g))
        picks.append(g.iloc[np.sort(rng.choice(len(g), size=take, replace=False))])
    return pd.concat(picks, ignore_index=True)[
        ['pano_id', 'city', 'era', 'stored_width', 'stored_height']]


def extract_record(pano):
    """The fields this census reads off a streetlevel StreetViewPanorama (or None if the pano is
    gone). Kept minimal and tolerant: an absent optional field is a None, never a crash."""
    if pano is None:
        return {'found': False, 'served_width': None, 'served_height': None,
                'pitch_deg': None, 'roll_deg': None, 'has_depth': None, 'capture_date': None}
    biggest = pano.image_sizes[-1]
    date = getattr(pano, 'date', None)
    scalar = lambda v: None if v is None else float(np.degrees(float(v)))
    return {
        'found': True,
        'served_width': int(biggest.x),
        'served_height': int(biggest.y),
        'pitch_deg': scalar(getattr(pano, 'pitch', None)),
        'roll_deg': scalar(getattr(pano, 'roll', None)),
        'has_depth': getattr(pano, 'depth', None) is not None,
        'capture_date': f'{date.year:04d}-{date.month:02d}' if date is not None else None,
    }


def run_census(sample, interval_s, progress_every=100):
    """The network loop: one find_panorama_by_id per sampled pano. Any per-pano exception is
    recorded as an error string — a census must never die at pano 1300 of 1400."""
    from streetlevel import streetview
    records = []
    for i, row in enumerate(sample.itertuples(index=False)):
        try:
            pano = streetview.find_panorama_by_id(row.pano_id, download_depth=True)
            rec = extract_record(pano)
        except Exception as e:  # noqa: BLE001 - recorded, counted, reported
            rec = extract_record(None)
            rec['error'] = f'{type(e).__name__}: {e}'
        rec.update(pano_id=row.pano_id, city=row.city, era=row.era,
                   stored_width=row.stored_width, stored_height=row.stored_height)
        records.append(rec)
        if progress_every and (i + 1) % progress_every == 0:
            print(f'  {i + 1}/{len(sample)}', flush=True)
        if interval_s:
            time.sleep(interval_s)
    return pd.DataFrame(records)


def _wrap_deg(a):
    """Map degrees to (-180, 180]: Google serves roll (and can serve pitch) in [0, 360), where
    359.9 means a -0.1 deg tilt, not a large one."""
    return -((180.0 - np.asarray(a, float)) % 360.0 - 180.0)


def _tilt_stats(alive):
    pitch = np.abs(_wrap_deg(alive['pitch_deg'].dropna()))
    roll = np.abs(_wrap_deg(alive['roll_deg'].dropna()))
    q = lambda s, p: float(np.percentile(s, p)) if len(s) else None
    return {'n': int(len(pitch)),
            'abs_pitch_p50_deg': q(pitch, 50), 'abs_pitch_p90_deg': q(pitch, 90),
            'abs_pitch_p99_deg': q(pitch, 99),
            'abs_roll_p50_deg': q(roll, 50), 'abs_roll_p90_deg': q(roll, 90),
            'abs_roll_p99_deg': q(roll, 99)}


def summarize(records):
    """Census accounting. Two conventions worth stating because both could silently mislead:

    - `alive_pct` is the share with found == True, and a request that *errored* is recorded as
      not-found, so it counts against alive. That is the conservative direction (a pano we could
      not reach is not a pano we can study), but it means alive_pct is a floor; `errors` /
      `errors_pct` size the ambiguity, and at 3/1360 it is 0.2%.
    - Panos whose rawLabels row carried no stored dims cannot be compared against the served dims.
      They must be excluded rather than compared, because `NaN != x` is True in pandas and would
      book every one of them as drift.
    """
    alive = records[records['found'] & records['served_width'].notna()]
    comparable = alive[alive['stored_width'].notna() & alive['stored_height'].notna()]
    drift = comparable[(comparable['served_width'] != comparable['stored_width'])
                       | (comparable['served_height'] != comparable['stored_height'])]
    n_errors = int(records['error'].notna().sum()) if 'error' in records else 0
    out = {
        'n_sampled': int(len(records)),
        'alive_pct': float(100 * records['found'].mean()),
        'dims_comparable': int(len(comparable)),
        'dims_unknown_stored': int(len(alive) - len(comparable)),
        'dims_drift_pct_of_alive':
            float(100 * len(drift) / len(comparable)) if len(comparable) else None,
        'depth_available_pct_of_alive':
            float(100 * alive['has_depth'].fillna(False).mean()) if len(alive) else None,
        'tilt': _tilt_stats(alive),
        'errors': n_errors,
        'errors_pct': float(100 * n_errors / len(records)) if len(records) else None,
    }
    out['by_era'] = {era: {'n': int(len(g)), 'alive_pct': float(100 * g['found'].mean())}
                     for era, g in records.groupby('era')}
    out['by_city'] = {c: {'n': int(len(g)), 'alive_pct': float(100 * g['found'].mean())}
                      for c, g in records.groupby('city')}
    return out


def json_records(df):
    """`df.to_dict(orient='records')` with every NaN replaced by a real None.

    The obvious `df.where(df.notna(), None)` does NOT do this: on a float64 (or int) column pandas
    coerces the None straight back to NaN, and `json.dump` then writes the bare token `NaN`, which
    is not valid JSON. Python's own decoder accepts it, so the file round-trips locally and fails
    for jq, JavaScript, and most other readers — the 2026-08-09 census shipped 4,916 of them before
    review caught it. Casting to object dtype first is what actually holds; `allow_nan=False` on
    every dump below is the belt to this braces.
    """
    return df.astype(object).where(df.notna(), None).to_dict(orient='records')


def write_json(result, path):
    """One writer for the census: non-standard float tokens refused rather than emitted, and LF
    newlines so a committed artifact is byte-identical whichever platform regenerated it."""
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(result, f, indent=1, allow_nan=False)


def resummarize(json_path):
    """Recompute the summary from the per-pano records embedded in an existing census JSON —
    offline, so a summarizer fix regenerates committed numbers without a refetch. Also re-scrubs
    the records, so running it over a file written by the pre-fix writer repairs the NaN tokens."""
    with open(json_path) as f:
        result = json.load(f)
    records = pd.DataFrame(result['records'])
    result['summary'] = summarize(records)
    result['records'] = json_records(records)
    write_json(result, json_path)
    return result


def sample_from_census(json_path):
    """Rebuild the drawn sample from a committed census JSON, so the identical panos can be asked again.

    This is what the embedded manifest was for. Re-drawing from rawLabels instead would give a *different*
    sample, and the difference between two samples of a changing corpus is not decay — the strata themselves
    move as labels are added. One population, asked twice, has no such confound.

    Dead panos are replayed too: an id Google dropped can come back, and `resurrected` is only measurable if
    the dead half of the manifest is asked as well.

    @return (sample, prior_records) — sample carries exactly the five columns run_census iterates.
    """
    with open(json_path) as f:
        result = json.load(f)
    prior = pd.DataFrame(result['records'])
    sample = prior[['pano_id', 'city', 'era', 'stored_width', 'stored_height']].copy()
    return sample, prior


def _found_flags(records):
    """`found` as a real bool Series. JSON round-trips it through object dtype and a not-found row can carry
    None, so `~records['found']` on the raw column is a TypeError or, worse, elementwise nonsense."""
    return records['found'].fillna(False).astype(bool)


def confirm_deaths(before, after, interval_s):
    """Ask every alive -> dead transition a second time, and believe the second answer when it finds the pano.

    run_census records a request that ERRORED as found=False. For an alive-rate that is the right conservative
    call (a pano we cannot reach is not a pano we can study). For a *decay* rate it is exactly backwards: it
    books a transient timeout as a permanent death, and it does so in the direction the study would like to
    believe. One re-request per suspected death costs a few hundred requests at most and removes that bias.

    Only the suspects are re-asked. Re-running the whole manifest would nearly double the run's cost to
    re-confirm transitions nobody is claiming, and a second miss on an already-dead pano proves nothing new.

    A miss on the second probe is NOT downgraded any further — a miss is what we already recorded. The
    asymmetry is deliberate: a false death is the error that inflates the finding, a false survival is the
    error that deflates it, and only the first is corrected here.

    @return A copy of `after` with recovered rows replaced by the second observation, plus `reprobe_recovered`
            / `reprobe_confirmed` flags and `reprobe_first_error` so the artifact carries the evidence.
    """
    out = after.copy()
    out['reprobe_recovered'] = False
    out['reprobe_confirmed'] = False
    out['reprobe_first_error'] = None

    joined = before[['pano_id', 'found']].merge(after[['pano_id', 'found']], on='pano_id',
                                                suffixes=('_before', '_after'), validate='one_to_one')
    was = joined['found_before'].fillna(False).astype(bool)
    now = joined['found_after'].fillna(False).astype(bool)
    suspects = set(joined.loc[was & ~now, 'pano_id'])
    if not suspects:
        return out

    sample = out.loc[out['pano_id'].isin(suspects),
                     ['pano_id', 'city', 'era', 'stored_width', 'stored_height']]
    second = {rec['pano_id']: rec for rec in run_census(sample, interval_s).to_dict('records')}

    rows = []
    for rec in out.to_dict('records'):
        pano_id = rec['pano_id']
        probe = second.get(pano_id)
        if probe is not None and bool(probe.get('found')):
            first_error = rec.get('error')
            rec = {**rec, **probe}
            # The record IS the second observation now, so the first probe's error must not ride along as
            # though this row had failed; it moves to its own column instead of being silently dropped.
            rec['error'] = probe.get('error')
            rec['reprobe_first_error'] = first_error
            rec['reprobe_recovered'] = True
        elif pano_id in suspects:
            rec = {**rec, 'reprobe_confirmed': True}
        rows.append(rec)
    return pd.DataFrame(rows)


def decay(before, after):
    """Per-pano transitions between two censuses of the same manifest.

    `died_pct_of_alive_before` divides by what was alive, not by the sample: a pano already gone cannot die
    again, so including it would halve the rate and describe nothing.

    `depth_lost` is the quantity #43 actually turns on. A depth map exists only for a pano Google still
    serves, so a pano that dies takes its depth with it permanently — but only if it had one, which is why
    this counts panos that carried depth rather than panos that died.

    A pano in `before` and missing from `after` (an interrupted re-fetch) shrinks the denominator via
    `n_unfetched`; it is never counted as a death.
    """
    joined = before.merge(after, on='pano_id', suffixes=('_before', '_after'), validate='one_to_one')
    was = joined['found_before'].fillna(False).astype(bool)
    now = joined['found_after'].fillna(False).astype(bool)
    died = was & ~now
    had_depth = joined['has_depth_before'].fillna(False).astype(bool)

    out = {
        'n_before': int(len(before)),
        'n_matched': int(len(joined)),
        'n_unfetched': int(len(before) - len(joined)),
        'alive_before': int(was.sum()),
        'alive_after': int(now.sum()),
        'still_alive': int((was & now).sum()),
        'died': int(died.sum()),
        'still_dead': int((~was & ~now).sum()),
        'resurrected': int((~was & now).sum()),
        'depth_lost': int((died & had_depth).sum()),
        'errors_before': int(before['error'].notna().sum()) if 'error' in before else 0,
        'errors_after': int(after['error'].notna().sum()) if 'error' in after else 0,
        'reprobe_recovered': int(after['reprobe_recovered'].fillna(False).astype(bool).sum())
                             if 'reprobe_recovered' in after else 0,
    }
    alive_before = out['alive_before']
    out['died_pct_of_alive_before'] = float(100 * out['died'] / alive_before) if alive_before else None
    out['depth_lost_pct_of_alive_before'] = \
        float(100 * out['depth_lost'] / alive_before) if alive_before else None
    out['by_era'] = {
        era: {'alive_before': int(_found_flags(g.rename(columns={'found_before': 'found'})).sum()),
              'died': int((g['found_before'].fillna(False).astype(bool)
                           & ~g['found_after'].fillna(False).astype(bool)).sum())}
        for era, g in joined.groupby('era_before')}
    return out


def print_decay(d, days=None):
    """The one rendering of a decay result, on print_summary's model."""
    per_month = None
    if days and d['died_pct_of_alive_before'] is not None and days > 0:
        per_month = d['died_pct_of_alive_before'] * 30.0 / days
    print(f"matched {d['n_matched']} of {d['n_before']} "
          f"(unfetched {d['n_unfetched']}, errors {d['errors_after']}, "
          f"reprobe-recovered {d['reprobe_recovered']})")
    print(f"alive {d['alive_before']} -> {d['alive_after']}   "
          f"died {d['died']} ({fmt(d['died_pct_of_alive_before'], '.1f')}% of those alive)   "
          f"resurrected {d['resurrected']}")
    print(f"depth maps now unobtainable: {d['depth_lost']} "
          f"({fmt(d['depth_lost_pct_of_alive_before'], '.1f')}% of those alive)"
          + (f"   ~= {fmt(per_month, '.1f')}%/30d" if per_month is not None else ''))
    print('died by era:', {k: v['died'] for k, v in d['by_era'].items()})


def print_summary(summary):
    """The one rendering of a census summary. Both entry points call it.

    There were two copies of these three lines, and they drifted exactly where it costs most.
    `--resummarize` formats the tilt quantiles through `fmt`; the live path interpolated them raw --
    so the *same records* printed `n/a` on one path and `None` on the other when no alive pano
    carried tilt, and `1.23` versus `1.2345678901` when they did. The unfixed copy was the one that
    had just spent a live network census, under a comment saying so.

    Every value below is optional by construction: `dims_drift_pct_of_alive` and
    `depth_available_pct_of_alive` are None when nothing was comparable or nothing was alive, and
    every tilt quantile is None when no alive pano carried pitch/roll. `by_era`'s `alive_pct` is the
    one that cannot be None -- a group only exists if it has rows -- so it stays a rounded number
    rather than becoming a quoted string in a dict of numbers.
    """
    t = summary['tilt']
    print(f"alive {fmt(summary['alive_pct'], '.1f')}%  "
          f"dims-drift {fmt(summary['dims_drift_pct_of_alive'], '.1f')}%  "
          f"depth {fmt(summary['depth_available_pct_of_alive'], '.1f')}%  "
          f"errors {summary['errors']}")
    print(f"tilt |pitch| p50/p90/p99: {fmt(t['abs_pitch_p50_deg'], '.2f')}/"
          f"{fmt(t['abs_pitch_p90_deg'], '.2f')}/{fmt(t['abs_pitch_p99_deg'], '.2f')}"
          f"  |roll|: {fmt(t['abs_roll_p50_deg'], '.2f')}/"
          f"{fmt(t['abs_roll_p90_deg'], '.2f')}/{fmt(t['abs_roll_p99_deg'], '.2f')}")
    print('alive by era:', {k: round(v['alive_pct'], 1) for k, v in summary['by_era'].items()})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', nargs='?')
    ap.add_argument('--fetched')
    ap.add_argument('--per-stratum', type=int, default=85)
    ap.add_argument('--interval', type=float, default=0.25)
    ap.add_argument('--write', metavar='JSON')
    ap.add_argument('--resummarize', metavar='JSON',
                    help='no network: recompute the summary from an existing census JSON')
    ap.add_argument('--refetch', metavar='JSON',
                    help='re-request the manifest embedded in an existing census JSON and report decay '
                         'against it (the same panos, so no strata confound)')
    ap.add_argument('--since-days', type=float, metavar='N',
                    help='days between the two censuses; only used to express the death rate per 30 days')
    args = ap.parse_args(argv)

    if args.resummarize:
        print_summary(resummarize(args.resummarize)['summary'])
        return

    if args.refetch:
        sample, prior = sample_from_census(args.refetch)
        with open(args.refetch) as f:
            prior_meta = json.load(f)
        print(f'refetching {len(sample)} panos from {args.refetch}', flush=True)
        records = run_census(sample, args.interval)
        # Before anything is summarised: an errored request reads as found=False, which would book a
        # network blip as a permanent death (see confirm_deaths).
        records = confirm_deaths(prior, records, args.interval)
        result = {'source': 'streetlevel photometa (find_panorama_by_id, download_depth=True)',
                  'refetch_of': os.path.basename(args.refetch),
                  'rawlabels_fetched': prior_meta.get('rawlabels_fetched'),
                  'refetched': args.fetched, 'since_days': args.since_days, 'seed': prior_meta.get('seed'),
                  'summary': summarize(records),
                  'decay': decay(prior, records),
                  'records': json_records(records)}
        print_summary(result['summary'])
        print_decay(result['decay'], days=args.since_days)
        if args.write:
            write_json(result, args.write)
            print(f'wrote {args.write}')
        return

    if not args.csv_dir or not args.fetched:
        ap.error('csv_dir and --fetched are required unless --resummarize is used')

    frames = []
    for path in sorted(glob.glob(os.path.join(args.csv_dir, '*.csv'))):
        df = rawlabels.load_rawlabels(path)
        df['city'] = os.path.splitext(os.path.basename(path))[0]
        frames.append(df[['pano_id', 'city', 'era', 'time_created', 'pano_width', 'pano_height']])
    sample = build_sample(pd.concat(frames, ignore_index=True), args.per_stratum, SEED)
    print(f'sample: {len(sample)} panos, '
          f'{sample.groupby(["city", "era"]).size().to_dict()}', flush=True)

    records = run_census(sample, args.interval)
    result = {'source': 'streetlevel photometa (find_panorama_by_id, download_depth=True)',
              'rawlabels_fetched': args.fetched, 'seed': SEED,
              'summary': summarize(records),
              'records': json_records(records)}

    print_summary(result['summary'])

    if args.write:
        write_json(result, args.write)
        print(f'wrote {args.write}')


if __name__ == '__main__':
    main()
