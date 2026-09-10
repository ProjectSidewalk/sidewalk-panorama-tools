#!/usr/bin/env python3
"""How far along is the depth backfill, city by city - and how long will it take under each regime?

Reads what the depth phase itself wrote: the last `DEPTHDOWNLOAD: Final result` line of every city's
scrape.log on the pano store, which carries the corpus size, the cumulative resolved count and that run's
request count. (Since 2026-09-09 log.csv carries the corpus size too, and log_analyzer/analyze.py reports the
same figures nightly; this script is the snapshot the 2026-09-09 report was written from, and the way to take
another one from a store directly.)

Two steps, separable so the reduction is testable without a store:

  --store ROOT     scan <ROOT>/<city>/scrape.log for every city and write the per-city CSV, reporting what
                   the scan could NOT read alongside it (see scan_store: an absent or stale row moves every
                   fleet total, and both were silent before)
  --csv PATH       reduce a per-city CSV (default: the committed 2026-09-09 snapshot) to the fleet figures
                   and the per-city nights-left, printing a table and, with --write, a JSON artifact

The fleet's "what a slot yields" figure is the median over the runs that stopped on `max-runtime`, and over
nothing else: no condition on the request count itself, because selecting runs by request count and then
reporting the median request count is circular. It is therefore a lower bound - a run whose image phase took
most of the slot is in it, and nothing in the log line says how the slot was split.

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

# `last_run_failed` is the superset `last_run_unavailable` sits inside: `unavailable` is the permanent verdict
# (no depth for this pano, ledgered, never retried) while `failed` also counts the transient ones (network,
# storage, unexpected). They are equal on a clean night, which is exactly why recording only the subset made a
# night of transient failures indistinguishable from a clean one.
FIELDS = ['city_id', 'eligible', 'resolved', 'last_run_requests', 'last_run_saved', 'last_run_failed',
          'last_run_unavailable', 'last_run_stop']
INT_FIELDS = ('eligible', 'resolved', 'last_run_requests', 'last_run_saved', 'last_run_unavailable')


def parse_final_result(line):
    """The per-run figures out of one `Final result` log line, or None if the line is not one."""
    m = FINAL_RESULT.search(line)
    if not m:
        return None
    return {'eligible': int(m['eligible']), 'resolved': int(m['resolved']),
            'last_run_requests': int(m['requests']), 'last_run_saved': int(m['saved']),
            'last_run_failed': int(m['failed']), 'last_run_unavailable': int(m['unavailable']),
            'last_run_stop': m['stop']}


def scan_store(root):
    """`(rows, census)`: one row per city directory whose scrape.log holds a Final result line, plus what the
    scan could NOT read.

    The census is the honest half of a store scan. A city that failed, was skipped, or stood its depth phase
    down writes no Final result line that night, so its last line is an *earlier* night's - and one whose log
    rotated past its last Final result contributes no row at all. Both used to be silent, and both move every
    fleet total the reduction reports. The line carries no timestamp (`DownloadRunner` logs at
    `logging.BASIC_FORMAT`), so a stale row cannot be dated from the file; what can be seen is how many runs
    each city recorded, and a city with fewer than the fleet's busiest is contributing an older row.
    (`log.csv` field 19 and `log_analyzer/analyze.py` are the nightly check that *does* carry a date - #124.)
    """
    rows, without, line_counts = [], [], {}
    for city_id in sorted(os.listdir(root)):
        log_path = os.path.join(root, city_id, 'scrape.log')
        if not os.path.isfile(log_path):
            continue
        last, seen = None, 0
        with open(log_path, errors='replace') as f:
            for line in f:
                parsed = parse_final_result(line)
                if parsed:
                    last = parsed
                    seen += 1
        if last:
            rows.append({'city_id': city_id, **last})
            line_counts[city_id] = seen
        else:
            without.append(city_id)
    most = max(line_counts.values(), default=None)
    census = {
        'cities_in_store': len(rows) + len(without),
        'cities_with_a_final_result': len(rows),
        'cities_without_a_final_result': without,
        'final_result_lines_max': most,
        'cities_behind_the_fleet': sorted(c for c, n in line_counts.items() if n < most),
    }
    return rows, census


def read_csv(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for key in INT_FIELDS:
            row[key] = int(row[key])
        # A snapshot taken before the column existed reads as undefined, never as zero: zero would say
        # "we measured no failures beyond the unavailable ones", which is the claim the column exists to make.
        row['last_run_failed'] = None if row.get('last_run_failed') in (None, '') else int(row['last_run_failed'])
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
                    floor_gap_seconds=FLOOR_GAP_SECONDS, store_census=None):
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
            'last_run_failed': row.get('last_run_failed'),
            'complete': unresolved == 0,
            'nights_measured': _maybe(_ratio(unresolved, row['last_run_requests'])) if unresolved else None,
            'nights_floor_slot': num(unresolved / per_slot_at_floor) if unresolved else None,
        })

    working = [c for c in cities if not c['complete']]
    with_gsv = [c for c in cities if c['eligible'] > 0]
    requests_last_night = sum(c['last_run_requests'] for c in cities)
    unresolved_total = sum(c['unresolved'] for c in cities)
    # `unavailable` is a subset of `failed`, so the difference is the transient failures - the number that
    # says whether a night was clean. Undefined, not zero, if any row predates the column.
    unavailable_last_night = sum(row['last_run_unavailable'] for row in rows)
    failed_values = [row.get('last_run_failed') for row in rows]
    failed_last_night = None if any(v is None for v in failed_values) else sum(failed_values)
    # The slot's yield: the median over the runs that stopped on the runtime budget, and nothing else. It used
    # to also require >= 500 requests, on the reasoning that a run whose image phase spent half the slot
    # (walla-walla-wa at 245, waltham-ma at 271) is not what a slot yields - but selecting runs *by* request
    # count and then reporting the median request count is circular: it can only bias the figure upward, and a
    # night the pacer backed off fleet-wide would put every run under the cut and report None. Nothing in the
    # scanned columns says how much of the slot the image phase took, so the honest filter is the stop reason
    # and the report says so. (log.csv's phase durations would settle it; this snapshot has only the log line.)
    max_runtime_requests = [row['last_run_requests'] for row in rows if row['last_run_stop'] == 'max-runtime']
    slot_median = statistics.median(max_runtime_requests) if max_runtime_requests else None
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
            'unavailable_last_night': unavailable_last_night,
            'failed_last_night': failed_last_night,
            'transient_failures_last_night': (None if failed_last_night is None
                                              else failed_last_night - unavailable_last_night),
            'max_runtime_runs': len(max_runtime_requests),
            'requests_per_max_runtime_run_median': _maybe(slot_median),
            'seconds_per_request_median': _maybe(_ratio(slot_minutes * 60.0, slot_median)),
            'nights_measured_fleet_average': _maybe(_ratio(unresolved_total, requests_last_night)),
            'nights_measured_longest_city': longest[0]['nights_measured'] if longest else None,
            'nights_floor_slot_longest_city': max((c['nights_floor_slot'] for c in working
                                                   if c['nights_floor_slot'] is not None), default=None),
            'nights_floor_whole_window': num(unresolved_total / per_night_at_floor),
        },
        # None when the reduction was run from a CSV: what the scan could not read is knowable only at scan
        # time, and an absent city leaves no trace in the file it never wrote a row to.
        'store_scan': store_census,
        'longest_remaining': [{'city_id': c['city_id'], 'unresolved': c['unresolved'],
                               'nights_measured': c['nights_measured'],
                               'nights_floor_slot': c['nights_floor_slot']} for c in longest[:5]],
        'cities': cities,
    }


def _print_store_scan(census, rows):
    """What the scan could not read, loudly - a fleet total short a city otherwise looks like a fleet total."""
    if census is None:
        print("  store scan: not available (reduced from a CSV; %d rows, completeness unknown)" % (rows,))
        return
    print("  store scan: %d of %d city directories produced a Final result line"
          % (census['cities_with_a_final_result'], census['cities_in_store']))
    if census['cities_without_a_final_result']:
        print("    WARNING - no Final result line, so absent from every fleet total: %s"
              % (', '.join(census['cities_without_a_final_result']),))
    if census['cities_behind_the_fleet']:
        print("    WARNING - fewer runs recorded than the fleet's %s, so their row is from an earlier night: %s"
              % (fmt(census['final_result_lines_max']), ', '.join(census['cities_behind_the_fleet'])))


def print_summary(summary):
    fleet = summary['fleet']
    print("Depth backfill, measured %s" % summary['measured_on'])
    print("  cities: %d (%d with GSV panos; %d complete, %d with work left)"
          % (fleet['cities'], fleet['cities_with_gsv'], fleet['complete'], fleet['working']))
    print("  resolved %s of %s (%s%%); %s unresolved"
          % (fmt(fleet['resolved'], ','), fmt(fleet['eligible'], ','), fmt(fleet['resolved_pct'], '.1f'),
             fmt(fleet['unresolved'], ',')))
    print("  requests last night: %s (median %s over the %d runs that stopped on max-runtime, %s s each)"
          % (fmt(fleet['requests_last_night'], ','), fmt(fleet['requests_per_max_runtime_run_median'], '.1f'),
             fleet['max_runtime_runs'], fmt(fleet['seconds_per_request_median'], '.2f')))
    print("  failures last night: %s (%s unavailable, %s transient)"
          % (fmt(fleet['failed_last_night'], ','), fmt(fleet['unavailable_last_night'], ','),
             fmt(fleet['transient_failures_last_night'], ',')))
    _print_store_scan(summary['store_scan'], len(summary['cities']))
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

    census = None
    if args.store:
        rows, census = scan_store(args.store)
        if args.csv_out:
            write_csv(args.csv_out, rows)
            print("wrote %d cities to %s" % (len(rows), args.csv_out))
    else:
        rows = read_csv(args.csv)

    summary = reduce_progress(rows, store_census=census)
    print_summary(summary)
    if args.write:
        with open(args.write, 'w') as f:
            json.dump(summary, f, indent=2, allow_nan=False)
            f.write('\n')
        print("wrote %s" % args.write)
    return 0


if __name__ == '__main__':
    sys.exit(main())
