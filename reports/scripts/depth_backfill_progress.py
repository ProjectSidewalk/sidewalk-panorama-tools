#!/usr/bin/env python3
"""How far along is the depth backfill, city by city - and how long will it take under each regime?

Reads what the depth phase itself wrote: the last `DEPTHDOWNLOAD: Final result` line of every city's
scrape.log on the pano store, which carries the corpus size, the cumulative resolved count and that run's
request count. (Since 2026-09-09 log.csv carries the corpus size too, and log_analyzer/analyze.py reports the
same figures nightly; this script is the snapshot the 2026-09-09 report was written from, and the way to take
another one from a store directly.)

Two steps, separable so the reduction is testable without a store:

  --store ROOT     scan <ROOT>/<city>/scrape.log for every city and write the per-city CSV
  --csv PATH       reduce a per-city CSV (default: the committed 2026-09-09 snapshot) to the fleet figures
                   and the per-city nights-left, printing a table and, with --write, a JSON artifact

The three regimes in the reduction are the ones the report compares. "Measured" divides each city's
unresolved count by the requests its last run made - the rate the fleet actually ran at. "Floor, fixed
slot" is what a slot yields once the pacer opens at the floor rather than ramping: SLOT_MINUTES of requests
at FLOOR_GAP_SECONDS each. "Floor, whole window" is the same rate over the whole night's window shared among
whichever cities still have work - the queue's extra passes - and is a fleet figure, since the window is one
resource. Every division by a zero denominator is None, never zero (studyfmt).

Usage:
  python3 reports/scripts/depth_backfill_progress.py --csv reports/data/2026-09-09-depth-backfill-progress.csv \\
      --write reports/data/2026-09-09-depth-backfill-progress.json
  python3 reports/scripts/depth_backfill_progress.py --store /mnt/panostore --csv-out progress.csv
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from studyfmt import fmt, num  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CSV = os.path.join(REPO_ROOT, 'reports', 'data', '2026-09-09-depth-backfill-progress.csv')

# The production line the measurement was taken under (scrape_queue.py --max-runtime 690 --city-max-runtime 12).
SLOT_MINUTES = 12.0
WINDOW_MINUTES = 690.0
# Mean gap between requests once the pacer sits at the 0.25 s floor: the gap is drawn uniform(0.25, 0.5) and
# is measured between request starts, so the 0.077 s median request is absorbed inside it.
FLOOR_GAP_SECONDS = 0.375
# The number of cities in the fleet's manifest that carry GSV panos is read from the CSV; these are not.

FINAL_RESULT = re.compile(
    r'DEPTHDOWNLOAD: Final result: Completed (?P<resolved>\d+) of (?P<eligible>\d+) '
    r'\((?P<saved>\d+) success, (?P<failed>\d+) failed \[(?P<unavailable>\d+) unavailable\], '
    r'(?P<skipped>\d+) skipped, (?P<requests>\d+) requests, stop_reason=(?P<stop>[A-Za-z-]+)\)')

FIELDS = ['city_id', 'eligible', 'resolved', 'last_run_requests', 'last_run_saved', 'last_run_unavailable',
          'last_run_stop']


def parse_final_result(line):
    """The per-run figures out of one `Final result` log line, or None if the line is not one."""
    m = FINAL_RESULT.search(line)
    if not m:
        return None
    return {'eligible': int(m['eligible']), 'resolved': int(m['resolved']),
            'last_run_requests': int(m['requests']), 'last_run_saved': int(m['saved']),
            'last_run_unavailable': int(m['unavailable']), 'last_run_stop': m['stop']}


def scan_store(root):
    """One row per city directory under root that has a scrape.log with a Final result line in it."""
    rows = []
    for city_id in sorted(os.listdir(root)):
        log_path = os.path.join(root, city_id, 'scrape.log')
        if not os.path.isfile(log_path):
            continue
        last = None
        with open(log_path, errors='replace') as f:
            for line in f:
                parsed = parse_final_result(line)
                if parsed:
                    last = parsed
        if last:
            rows.append({'city_id': city_id, **last})
    return rows


def read_csv(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for key in ('eligible', 'resolved', 'last_run_requests', 'last_run_saved', 'last_run_unavailable'):
            row[key] = int(row[key])
    return rows


def write_csv(path, rows):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({k: row[k] for k in FIELDS} for row in rows)


def _ratio(numerator, denominator):
    """A quotient, or None when the denominator is 0: undefined is not zero."""
    return None if not denominator else numerator / denominator


def _scaled(factor, value):
    return None if value is None else factor * value


def _maybe(value):
    """studyfmt.num for a value that may already be None - num() itself takes only numbers."""
    return None if value is None else num(value)


def reduce_progress(rows, slot_minutes=SLOT_MINUTES, window_minutes=WINDOW_MINUTES,
                    floor_gap_seconds=FLOOR_GAP_SECONDS):
    """The fleet figures and the per-city nights-left, under three regimes.

    per_slot_at_floor is what one slot yields at the floor; per_night_at_floor is the whole window's worth,
    which is the fleet's ceiling once the window is spent on whoever has work.
    """
    per_slot_at_floor = slot_minutes * 60.0 / floor_gap_seconds
    per_night_at_floor = window_minutes * 60.0 / floor_gap_seconds

    cities = []
    for row in rows:
        unresolved = max(row['eligible'] - row['resolved'], 0)
        cities.append({
            'city_id': row['city_id'],
            'eligible': row['eligible'],
            'resolved': row['resolved'],
            'unresolved': unresolved,
            'last_run_requests': row['last_run_requests'],
            'complete': unresolved == 0,
            'nights_measured': _maybe(_ratio(unresolved, row['last_run_requests'])) if unresolved else None,
            'nights_floor_slot': num(unresolved / per_slot_at_floor) if unresolved else None,
        })

    working = [c for c in cities if not c['complete']]
    with_gsv = [c for c in cities if c['eligible'] > 0]
    requests_last_night = sum(c['last_run_requests'] for c in cities)
    unresolved_total = sum(c['unresolved'] for c in cities)
    # The slot's yield: the median over runs that stopped on the runtime budget with the whole slot to
    # themselves - a run whose image phase spent half the slot (walla-walla-wa, waltham-ma) is not the slot.
    full_slot_runs = [row['last_run_requests'] for row in rows
                      if row['last_run_stop'] == 'max-runtime' and row['last_run_requests'] >= 500]
    longest = sorted((c for c in working if c['nights_measured'] is not None),
                     key=lambda c: c['nights_measured'], reverse=True)

    return {
        'measured_on': '2026-09-09',
        'parameters': {'slot_minutes': slot_minutes, 'window_minutes': window_minutes,
                       'floor_gap_seconds': floor_gap_seconds,
                       'per_slot_at_floor': num(per_slot_at_floor), 'per_night_at_floor': num(per_night_at_floor)},
        'fleet': {
            'cities': len(cities),
            'cities_with_gsv': len(with_gsv),
            'complete': sum(1 for c in cities if c['complete'] and c['eligible'] > 0),
            'working': len(working),
            'eligible': sum(c['eligible'] for c in cities),
            'resolved': sum(c['resolved'] for c in cities),
            'unresolved': unresolved_total,
            'resolved_pct': _maybe(_scaled(100.0, _ratio(sum(c['resolved'] for c in cities),
                                                      sum(c['eligible'] for c in cities)))),
            'requests_last_night': requests_last_night,
            'requests_per_full_slot_median': num(statistics.median(full_slot_runs)) if full_slot_runs else None,
            'nights_measured_fleet_average': _maybe(_ratio(unresolved_total, requests_last_night)),
            'nights_measured_longest_city': longest[0]['nights_measured'] if longest else None,
            'nights_floor_slot_longest_city': max((c['nights_floor_slot'] for c in working
                                                   if c['nights_floor_slot'] is not None), default=None),
            'nights_floor_whole_window': num(unresolved_total / per_night_at_floor),
        },
        'longest_remaining': [{'city_id': c['city_id'], 'unresolved': c['unresolved'],
                               'nights_measured': c['nights_measured'],
                               'nights_floor_slot': c['nights_floor_slot']} for c in longest[:5]],
        'cities': cities,
    }


def print_summary(summary):
    fleet = summary['fleet']
    print("Depth backfill, measured %s" % summary['measured_on'])
    print("  cities: %d (%d with GSV panos; %d complete, %d with work left)"
          % (fleet['cities'], fleet['cities_with_gsv'], fleet['complete'], fleet['working']))
    print("  resolved %s of %s (%s%%); %s unresolved"
          % (fmt(fleet['resolved'], ','), fmt(fleet['eligible'], ','), fmt(fleet['resolved_pct'], '.1f'),
             fmt(fleet['unresolved'], ',')))
    print("  requests last night: %s (median %s per full slot)"
          % (fmt(fleet['requests_last_night'], ','), fmt(fleet['requests_per_full_slot_median'], '.0f')))
    print("  nights left - measured: fleet average %s, longest city %s; floor + fixed slot: longest city %s; "
          "floor + whole window: %s"
          % (fmt(fleet['nights_measured_fleet_average'], '.0f'), fmt(fleet['nights_measured_longest_city'], '.0f'),
             fmt(fleet['nights_floor_slot_longest_city'], '.0f'), fmt(fleet['nights_floor_whole_window'], '.0f')))
    print("  longest remaining:")
    for c in summary['longest_remaining']:
        print("    %-20s %10s unresolved  %6s nights measured  %6s at the floor per slot"
              % (c['city_id'], fmt(c['unresolved'], ','), fmt(c['nights_measured'], '.0f'),
                 fmt(c['nights_floor_slot'], '.0f')))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--store', metavar='ROOT', help='scan a pano store instead of reading a CSV')
    parser.add_argument('--csv', default=DEFAULT_CSV, metavar='PATH', help='per-city CSV to reduce')
    parser.add_argument('--csv-out', metavar='PATH', help='with --store: where to write the per-city CSV')
    parser.add_argument('--write', metavar='PATH', help='write the reduction as a JSON artifact')
    args = parser.parse_args(argv)

    if args.store:
        rows = scan_store(args.store)
        if args.csv_out:
            write_csv(args.csv_out, rows)
            print("wrote %d cities to %s" % (len(rows), args.csv_out))
    else:
        rows = read_csv(args.csv)

    summary = reduce_progress(rows)
    print_summary(summary)
    if args.write:
        with open(args.write, 'w') as f:
            json.dump(summary, f, indent=2, allow_nan=False)
            f.write('\n')
        print("wrote %s" % args.write)
    return 0


if __name__ == '__main__':
    sys.exit(main())
