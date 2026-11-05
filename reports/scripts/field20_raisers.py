"""#182: a 25-night fleet read of `log.csv` field 20 (`image_raised`), the measurement the analyzer's steady-raiser
rule is sized from.

    # 1. On the store host, stream the run logs off the store (read-only; see fleet_logs_pull.sh), decode into <raw-dir>
    # 2. Pack them with the 10-06 report's packer under this pull's prefix (redacts any Mapillary token):
    python reports/scripts/fleet_thresholds.py pack <raw-dir> --prefix 2026-11-05- --since 2026-10-06 --no-dc-ledger
    # 3. Reduce the committed extracts (makes no request, reads nothing but reports/data/):
    python reports/scripts/field20_raisers.py [--write]

Step 3 prints the markdown tables quoted in reports/2026-11-05-field20-fleet-raisers.md, and with --write
rewrites reports/data/2026-11-05-field20-fleet-raisers.json. tests/test_field20_raisers_report.py re-derives
that JSON from the committed extracts and asserts every table cell appears in the report.

Definitions, each the one the analyzer's rule uses (log_analyzer/analyze.py, rule 11):

* **A night is the local date twelve hours before a row's own stamp**, on the runner's clock (the stamp carries
  its offset since #101). The queue starts at 19:00 local and its extra passes run into the small hours, so
  every pass of a night lands on one date. (The analyzer groups by UTC date, which puts the same passes on one
  date for the same reason. They split one night in this window - bayonne-fr's 2026-10-16, an afternoon hand
  run beside the evening's, both 0 - and the test runs the real analyzer over the extract and checks that it
  fires on exactly the cities this script says.)
* **A night's field 20 is the MAX over its rows**, never the sum: extra passes re-attempt the same unledgered
  set, and a pass that stopped on its budget saw only part of it (chicago-il writes 99 then 276 on one night).
  A blank field 20 (the image phase did not finish) is no evidence, and a night with no 20-field row at all is
  **unknown**, never 0.
* **A streak counts calendar nights**; an unknown or missing night breaks it.
"""

import argparse
import collections
import datetime
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_thresholds as ft  # noqa: E402
from studyfmt import fmt  # noqa: E402

DATA_DIR = ft.DATA_DIR
PREFIX = '2026-11-05-'
FILES = ft.extract_names(PREFIX)
RESULT_FILE = PREFIX + 'field20-fleet-raisers.json'

# Field 20 is the 20th field; rows written before #182 have 19 and are not evidence.
FIELD20_INDEX = 19
NIGHT_SHIFT = datetime.timedelta(hours=12)
# The rule as shipped (log_analyzer/analyze.py). Repeated here rather than imported: reports/ never imports
# production into a study's numbers silently - the test pins the two equal.
RULE_MIN_RAISED = 3
RULE_NIGHTS = 7
FLOOR_SWEEP = (1, 2, 3, 4, 5, 10, 16, 17)
WINDOW_SWEEP = (3, 5, 7, 10, 14)
# The nights whose value is called "steady" in the per-city table.
STEADY_NIGHTS = 10
QUEUE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ (\S+): \w+ \(exit \S+\) in [\d.]+ min; '
                      r'conditions: (.*)$')


# A Panoramax picture id, folded so one error text is one bucket (raise_kind folds digits, not hex letters).
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def night_of(ts):
    """The schedule night of a log.csv stamp, on the stamp's own clock (offset-less rows read as UTC)."""
    t = datetime.datetime.fromisoformat(ts.strip())
    return (t.replace(tzinfo=None) - NIGHT_SHIFT).date().isoformat()


def utc_date_of(ts):
    """The UTC date of a stamp: the analyzer's grouping (analyze.read_log, then .dt.normalize())."""
    t = datetime.datetime.fromisoformat(ts.strip())
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).date().isoformat()


def queue_night_of(stamp):
    """A scrape_queue.log stamp is local and offset-less; its night is the local date twelve hours earlier."""
    return (datetime.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S') - NIGHT_SHIFT).date().isoformat()


def int_or_none(text):
    text = text.strip()
    return int(text) if text else None


def night_values(rows):
    """{night: {'raised': max field 20 or None, 'downloaded': fields 7+8 summed or None, 'rows': n}} over the
    rows that carry field 20. A night whose 20-field rows all have a blank field 20 is present with None."""
    nights = {}
    for row in rows:
        fields = row['fields']
        if len(fields) <= FIELD20_INDEX:
            continue
        night = nights.setdefault(night_of(row['ts']), {'raised': None, 'downloaded': None, 'rows': 0})
        night['rows'] += 1
        raised = int_or_none(fields[FIELD20_INDEX])
        if raised is not None:
            night['raised'] = raised if night['raised'] is None else max(night['raised'], raised)
        success, fallback = int_or_none(fields[6]), int_or_none(fields[7])
        if success is not None and fallback is not None:
            night['downloaded'] = (night['downloaded'] or 0) + success + fallback
    return nights


def calendar(first, last):
    d0, d1 = datetime.date.fromisoformat(first), datetime.date.fromisoformat(last)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def streak_ending(nights, end, floor):
    """Calendar nights ending at `end` whose known field 20 is >= floor; an unknown night stops the count."""
    count = 0
    day = datetime.date.fromisoformat(end)
    while True:
        value = nights.get(day.isoformat(), {}).get('raised')
        if value is None or value < floor:
            return count
        count += 1
        day -= datetime.timedelta(days=1)


def fires(nights, end, floor, window):
    """The rule on the night `end`: each of the last `window` calendar nights known and >= floor."""
    return streak_ending(nights, end, floor) >= window


def longest_streak(nights, days, floor=1):
    best = run = 0
    for day in days:
        value = nights.get(day, {}).get('raised')
        run = run + 1 if value is not None and value >= floor else 0
        best = max(best, run)
    return best


def queue_condition_nights(queue_lines, code):
    """{city: sorted nights on which the queue reported `code` for it}."""
    found = collections.defaultdict(set)
    for line in queue_lines:
        m = QUEUE_RE.match(line)
        if m and code in m.group(3).split(', '):
            found[m.group(2)].add(queue_night_of(m.group(1)))
    return {city: sorted(n) for city, n in found.items()}


def attribution(lines):
    """Raise lines in one city's retained scrape.log, bucketed by error kind and by pano-id prefix."""
    buckets = collections.defaultdict(lambda: {'lines': 0, 'ids': set()})
    for line in lines:
        m = ft.RAISE_RE.search(line)
        if not m or 'frame disagreement' in line:
            continue    # a frame disagreement is an answer, not a raise (fleet_thresholds.split_image_runs)
        pano = m.group(1)
        key = (ft.raise_kind(UUID_RE.sub('<uuid>', line)), 'CAoS' if pano.startswith('CAoS') else 'other')
        buckets[key]['lines'] += 1
        buckets[key]['ids'].add(pano)
    return [{'kind': kind, 'prefix': prefix, 'lines': b['lines'], 'distinct_panos': len(b['ids'])}
            for (kind, prefix), b in sorted(buckets.items(), key=lambda kv: (-kv[1]['lines'], kv[0]))]


def analyze(data_dir=DATA_DIR):
    rows = ft.read_log_csv(os.path.join(data_dir, FILES['log_csv']))
    scrape = ft.read_scrape_logs(os.path.join(data_dir, FILES['scrape_log']))
    with gzip.open(os.path.join(data_dir, FILES['queue_log']), 'rt', encoding='utf-8') as f:
        queue_lines = [ln.rstrip('\n') for ln in f]
    no_success = queue_condition_nights(queue_lines, 'images-no-success')

    per_city = {city: night_values(city_rows) for city, city_rows in rows.items()}
    known = sorted({n for nights in per_city.values() for n, v in nights.items() if v['raised'] is not None})
    first, last = known[0], known[-1]
    days = calendar(first, last)
    steady_days = days[-STEADY_NIGHTS:]

    # Does the runner-clock night agree with the analyzer's UTC date on every 20-field row's grouping?
    split_nights = 0
    for city_rows in rows.values():
        by_night = collections.defaultdict(set)
        for row in city_rows:
            if len(row['fields']) > FIELD20_INDEX:
                by_night[night_of(row['ts'])].add(utc_date_of(row['ts']))
        split_nights += sum(1 for dates in by_night.values() if len(dates) > 1)

    cities = {}
    for city, nights in sorted(per_city.items()):
        values = {d: nights[d]['raised'] for d in days if d in nights and nights[d]['raised'] is not None}
        nonzero = [d for d, v in values.items() if v > 0]
        steady = [nights[d]['raised'] for d in steady_days if d in nights and nights[d]['raised'] is not None]
        cities[city] = {
            'first_night': min(values) if values else None,
            'nights_known': len(values),
            'nights_nonzero': len(nonzero),
            'max': max(values.values()) if values else None,
            'first_nonzero': nonzero[0] if nonzero else None,
            'last_nonzero': nonzero[-1] if nonzero else None,
            'steady_nights_known': len(steady),
            'steady_min': min(steady) if steady else None,
            'steady_max': max(steady) if steady else None,
            'longest_nonzero_streak': longest_streak(nights, days),
            'nonzero_nights_with_downloads': sum(1 for d in nonzero if (nights[d]['downloaded'] or 0) > 0),
            'images_no_success_nights': len([n for n in no_success.get(city, []) if first <= n <= last]),
            'rule_fires_nights': sum(1 for d in days if fires(nights, d, RULE_MIN_RAISED, RULE_NIGHTS)),
            'rule_fires_last_night': fires(nights, last, RULE_MIN_RAISED, RULE_NIGHTS),
        }

    raisers = sorted((c for c, r in cities.items() if r['nights_nonzero']),
                     key=lambda c: (-(cities[c]['max'] or 0), c))
    sweep = {}
    for window in WINDOW_SWEEP:
        for floor in FLOOR_SWEEP:
            sweep['%d|%d' % (floor, window)] = {
                'cities_ever': sorted(c for c in cities if any(fires(per_city[c], d, floor, window) for d in days)),
                'cities_last_night': sorted(c for c in cities if fires(per_city[c], last, floor, window)),
            }

    return {
        'source': {'log_csv': FILES['log_csv'], 'scrape_log': FILES['scrape_log'], 'queue_log': FILES['queue_log'],
                   'first_night': first, 'last_night': last, 'nights': len(days), 'cities': len(cities),
                   'cities_with_field20': sum(1 for r in cities.values() if r['nights_known']),
                   'nights_split_across_utc_dates': split_nights},
        'rule': {'min_raised': RULE_MIN_RAISED, 'nights': RULE_NIGHTS},
        'cities': cities,
        'raisers': raisers,
        'zero_every_night': sorted(c for c, r in cities.items() if r['nights_known'] and not r['nights_nonzero']),
        'attribution': {c: attribution(scrape.get(c, [])) for c in raisers},
        'sweep': sweep,
    }


def yes_no(flag):
    return 'yes' if flag else 'no'


def tables(result):
    out = []
    src = result['source']
    out.append('Window: %s to %s, %d nights; %d cities, %d with a 20-field row.' % (
        src['first_night'], src['last_night'], src['nights'], src['cities'], src['cities_with_field20']))
    out.append('')
    out.append('Runner-clock nights whose rows fall on two UTC dates (the analyzer groups by UTC date): %d.'
               % src['nights_split_across_utc_dates'])
    out.append('')
    out.append('| City | nights with field 20 | nights nonzero | first-last nonzero | max | last %d nights (min-max) '
               '| longest nonzero streak | nonzero nights with downloads | `images-no-success` nights '
               '| rule fires (nights) |' % STEADY_NIGHTS)
    out.append('|---|---|---|---|---|---|---|---|---|---|')
    for city in result['raisers']:
        r = result['cities'][city]
        out.append('| %s | %d | %d | %s to %s | %s | %s-%s | %d | %d | %d | %d |' % (
            city, r['nights_known'], r['nights_nonzero'], r['first_nonzero'], r['last_nonzero'], fmt(r['max']),
            fmt(r['steady_min']), fmt(r['steady_max']), r['longest_nonzero_streak'],
            r['nonzero_nights_with_downloads'], r['images_no_success_nights'], r['rule_fires_nights']))
    out.append('')
    out.append('Zero on every night: %d cities.' % len(result['zero_every_night']))
    out.append('')
    out.append('| City | raise kind | pano id | lines | distinct panos |')
    out.append('|---|---|---|---|---|')
    for city in result['raisers']:
        for b in result['attribution'][city]:
            out.append('| %s | %s | %s | %d | %d |' % (city, b['kind'], b['prefix'], b['lines'], b['distinct_panos']))
    out.append('')
    floors = FLOOR_SWEEP
    out.append('| window (nights) | ' + ' | '.join('floor %d' % f for f in floors) + ' |')
    out.append('|---|' + '---|' * len(floors))
    for window in WINDOW_SWEEP:
        cells = [str(len(result['sweep']['%d|%d' % (f, window)]['cities_ever'])) for f in floors]
        out.append('| %d | %s |' % (window, ' | '.join(cells)))
    out.append('')
    chosen = result['sweep']['%d|%d' % (RULE_MIN_RAISED, RULE_NIGHTS)]
    out.append('Floor %d over %d nights: fires at some point for %s; on the last night for %s.' % (
        RULE_MIN_RAISED, RULE_NIGHTS, ', '.join(chosen['cities_ever']) or 'none',
        ', '.join(chosen['cities_last_night']) or 'none'))
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--data-dir', default=DATA_DIR)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    result = analyze(args.data_dir)
    print(json.dumps(result['source'], indent=1))
    print(tables(result))
    if args.write:
        path = os.path.join(args.data_dir, RESULT_FILE)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, sort_keys=True, allow_nan=False)
            f.write('\n')
        print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
