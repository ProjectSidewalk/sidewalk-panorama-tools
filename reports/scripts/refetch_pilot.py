"""What a `fover` re-fetch pilot actually recovered, reduced from the pass's own output (#73).

    python reports/scripts/refetch_pilot.py <refetch_log.csv> <refetch_measurements.jsonl> \\
        [--probed YYYY-MM-DD] [--write reports/data/<date>-fover-refetch-pilot.json]

`refetch_panos.py --measure` writes one JSON line per swapped panorama and one ledger row per outcome.
This turns both into the artifact a report cites, and answers the three questions the re-download
decision has been waiting on since 2026-08-07:

1. **Retirement.** What fraction of a work-list Google still serves. The photometa census predicts
   47.9% survival for labelled panoramas; this measures it on the population that would actually be
   re-fetched, and it is the argument the report could not dismiss - for a panorama Google has dropped,
   the choice is half resolution now or nothing later.

2. **Whether the re-fetch is clean.** Whether dropping `fover` still holds against the live endpoint.
   `refetch_panos.py` does not ledger `undersized` - it stops the run after three in a row and exits 1 -
   so a pilot that ran to completion and exited 0 is itself the evidence, and its ledger holds no
   undersized rows. `undersized_pct` below stays for ledgers written before that rule, when every outcome
   was ledgered.

3. **Whether the recovered detail survives our own JPEG.** This is the one nobody has measured, and the
   reason `--measure` reports two bands. CBK served the horizon rows at full size in BOTH eras, so their
   old-vs-new MAE is our re-encode and nothing else; the bottom band's figure minus that is the
   recovered detail. If the difference is at or below zero, the pass is recovering something the store
   cannot hold, and the answer to #73 is "accept and move on" with a number behind it for the first
   time.

Reads the two files as produced; it does not re-fetch anything and needs no network.
"""

import argparse
import collections
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
REPO = os.path.dirname(REPORTS)
sys.path.insert(0, HERE)
from studyfmt import fmt, num  # noqa: E402

# The outcomes that cost a full tile fan-out - i.e. the panoramas whose imagery we actually saw. The
# undersized and clean-refetch rates are about the tiles, so they are measured over these.
FETCHED_OUTCOMES = ('replaced', 'upscaled', 'undersized', 'too_black')

# The outcomes that mean Google still serves this panorama. `frame_grew` belongs here and NOT above: it is a
# pano Google holds - larger than we asked for - so counting it as retired would understate survival, which
# is the one figure the retirement argument rests on. It just never reached a fan-out.
SERVED_OUTCOMES = FETCHED_OUTCOMES + ('frame_grew',)

# A horizon-band old-vs-new MAE above this means Google re-rendered the panorama since it was scraped, not
# that we re-encoded it: see summarise() for the gap in the first pilot's distribution this sits in.
RERENDERED_HORIZON_MAE = 3.0


def _display_path(path):
    """`path` relative to the repo when it is inside it, absolute otherwise.

    Unlike the other study scripts, --write here takes an arbitrary destination: the pilot's raw output
    lives on the pano store, so the natural invocation reduces from there. os.path.relpath raises
    outright when the two are on different Windows drives, which would kill the run on its last line
    after the artifact had already been written - the studyfmt failure mode, in a different disguise.
    """
    try:
        relative = os.path.relpath(path, REPO)
    except ValueError:
        return os.path.abspath(path)
    return path if relative.startswith(os.pardir) else relative


def read_outcomes(ledger_path):
    """Outcome counts from a refetch_log.csv, ignoring the header and any torn line.

    The ledger holds only the outcomes that cost requests (refetch_panos.LEDGERED_OUTCOMES); the five
    zero-request ones are recomputed every run and printed, never written. So `panoramas_considered` in the
    summary is the panoramas that reached Google, plus whatever zero-request rows an older ledger carries.
    """
    counts = collections.Counter()
    with open(ledger_path, newline='') as f:
        for row in csv.reader(f):
            if len(row) != 2 or row[0] == 'pano_id':
                continue
            counts[row[1]] += 1
    return counts


def read_measurements(path):
    """The per-panorama recovery records, one JSON object per line."""
    records = []
    with open(path, encoding='utf8') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def percentile(values, q):
    """The q-th percentile by nearest rank, or None for an empty series.

    None rather than 0.0 deliberately: a pilot that swapped nothing has an undefined median recovery,
    and reporting it as zero would read as "we measured no recovery" rather than "we measured nothing".
    """
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarise(counts, records):
    """The artifact's summary block. Every rate is None when its denominator is zero."""
    considered = sum(counts.values())
    fetched = sum(counts[k] for k in FETCHED_OUTCOMES)
    served = sum(counts[k] for k in SERVED_OUTCOMES)
    # `gone` plus everything Google answered for is the population Google was actually asked about; the
    # store-only outcomes never reached it and would understate survival if counted.
    probed = served + counts['gone']

    recovered = [r['recovered_above_noise'] for r in records if 'recovered_above_noise' in r]
    bottom = [r['bottom']['mae_old_vs_new'] for r in records if 'bottom' in r]
    horizon = [r['horizon']['mae_old_vs_new'] for r in records if 'horizon' in r]
    halving = [r['bottom']['halve_restore_new'] for r in records if 'bottom' in r]

    def rate(n, d):
        return num(100.0 * n / d) if d else None

    # Per frame size, because the two zoom-5 geometries are not the same experiment: the half-resolution
    # band is rows 11-15 of 16 on a 16384x8192 panorama and rows 9-12 of 13 on a 13312x6656 one, so the
    # smaller frame has proportionally more of its height in the band and is where any real gain would
    # concentrate. Keyed by 'WxH'; a pilot drawn at the corpus's own mix has few of the smaller frame, and
    # the n is reported so that a reader does not read a five-panorama median as a finding.
    by_frame = {}
    for frame in sorted({'%sx%s' % (r.get('width'), r.get('height')) for r in records}):
        subset = [r['recovered_above_noise'] for r in records
                  if '%sx%s' % (r.get('width'), r.get('height')) == frame and 'recovered_above_noise' in r]
        by_frame[frame] = {
            'n': len(subset),
            'median': num(percentile(subset, 0.50)) if subset else None,
            'n_positive': sum(1 for v in subset if v > 0),
        }

    # The horizon band's old-vs-new MAE has two populations, not one. Two encodes of the same imagery differ
    # by about a luma level; a panorama Google has RE-RENDERED since it was scraped - re-stitched, re-blurred,
    # re-graded, at the same dimensions - differs by several. The first pilot's horizon MAEs were 0.5-1.8 for
    # three quarters of the panoramas and 7.6-24 for the rest, with nothing in between, so the threshold sits
    # in that gap. Reported separately because a re-rendered panorama's recovery figure compares two different
    # pictures, and the like-for-like core is the population the fover question is actually about.
    rerendered = [r for r in records if 'horizon' in r
                  and r['horizon']['mae_old_vs_new'] > RERENDERED_HORIZON_MAE]
    same = [r for r in records if 'horizon' in r
            and r['horizon']['mae_old_vs_new'] <= RERENDERED_HORIZON_MAE]
    same_recovered = [r['recovered_above_noise'] for r in same if 'recovered_above_noise' in r]

    return {
        'panoramas_considered': considered,
        'outcomes': dict(counts.most_common()),
        'probed_at_google': probed,
        'still_served_pct': rate(served, probed),
        'frame_grew': counts['frame_grew'],
        'clean_refetch_pct': rate(counts['replaced'] + counts['too_black'] + counts['upscaled'],
                                  fetched) if fetched else None,
        'undersized_pct': rate(counts['undersized'], fetched),
        'measured': len(records),
        # The headline. Positive means the bottom band gained detail the horizon band did not, i.e. more
        # than the re-encode accounts for.
        'recovered_above_noise': {
            'p10': num(percentile(recovered, 0.10)) if recovered else None,
            'median': num(percentile(recovered, 0.50)) if recovered else None,
            'p90': num(percentile(recovered, 0.90)) if recovered else None,
            'n_positive': sum(1 for v in recovered if v > 0),
        },
        'bottom_band_mae_median': num(percentile(bottom, 0.50)) if bottom else None,
        'horizon_band_mae_median': num(percentile(horizon, 0.50)) if horizon else None,
        'bottom_band_halving_cost_median': num(percentile(halving, 0.50)) if halving else None,
        'horizon_band_halving_cost_median': num(percentile(
            [r['horizon']['halve_restore_new'] for r in records if 'horizon' in r], 0.50)) if records else None,
        'by_frame': by_frame,
        'rerendered': {
            'threshold_horizon_mae': RERENDERED_HORIZON_MAE,
            'n': len(rerendered),
            'pct_of_measured': rate(len(rerendered), len(records)),
        },
        'same_rendering': {
            'n': len(same),
            'recovered_above_noise_median': num(percentile(same_recovered, 0.50)) if same_recovered else None,
            'n_positive': sum(1 for v in same_recovered if v > 0),
            'bottom_band_mae_median': num(percentile(
                [r['bottom']['mae_old_vs_new'] for r in same], 0.50)) if same else None,
            'horizon_band_mae_median': num(percentile(
                [r['horizon']['mae_old_vs_new'] for r in same], 0.50)) if same else None,
        },
    }


def build_parser():
    parser = argparse.ArgumentParser(
        description='Reduce a refetch_panos.py --measure pilot into the artifact a report cites.')
    parser.add_argument('ledger', help='refetch_log.csv from the pilot run.')
    parser.add_argument('measurements', help='refetch_measurements.jsonl from the same run.')
    parser.add_argument('--probed', default=None, metavar='YYYY-MM-DD',
                        help='The date the pilot ran, recorded in the artifact. Passed in rather than '
                             'taken from the clock so re-reducing the same pilot months later does not '
                             'restamp it with today.')
    parser.add_argument('--write', default=None, metavar='PATH',
                        help='Write the artifact here. Without it, nothing is written.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    counts = read_outcomes(args.ledger)
    records = read_measurements(args.measurements)
    s = summarise(counts, records)

    print('%d panorama(s) considered; %d probed at Google, %s%% still served'
          % (s['panoramas_considered'], s['probed_at_google'], fmt(s['still_served_pct'], '.1f')))
    print('%d replaced, %d measured' % (counts['replaced'], s['measured']))
    print('bottom-band MAE  %s   horizon-band MAE (our JPEG round-trip)  %s'
          % (fmt(s['bottom_band_mae_median'], '.3f'), fmt(s['horizon_band_mae_median'], '.3f')))
    print('recovered above noise: p10 %s  median %s  p90 %s  (%d of %d positive)'
          % (fmt(s['recovered_above_noise']['p10'], '.3f'),
             fmt(s['recovered_above_noise']['median'], '.3f'),
             fmt(s['recovered_above_noise']['p90'], '.3f'),
             s['recovered_above_noise']['n_positive'], s['measured']))

    if args.write:
        payload = {
            'question': 'What does re-fetching a fover-era panorama actually recover, and how much of a '
                        'work-list does Google still serve? (#73)',
            'method': 'Reduced from refetch_panos.py --measure: one JSON line per swapped panorama plus '
                      'the pass ledger. The horizon band is the control - CBK served those rows at full '
                      'size both before and after the fix, so their old-vs-new MAE is our own JPEG '
                      're-encode, and the bottom band minus that is recovered detail.',
            'probed': args.probed,
            'summary': s,
            'records': records,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
        with open(args.write, 'w') as f:
            # allow_nan=False: a NaN reaching the artifact aborts the write rather than shipping a token
            # no JSON reader accepts - the 4,916-bare-NaN lesson from the photometa census.
            json.dump(payload, f, indent=1, allow_nan=False)
        print('\nwrote %s' % _display_path(args.write))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
