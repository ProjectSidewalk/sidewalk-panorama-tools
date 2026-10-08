"""Fleet measurements for #178 (the `images-no-success` thresholds), #185 Part 1 (frame-disagreement refusals)
and #184 (washington-dc's `downloaded=0` ledger rows), from the production store's own run logs.

    # 1. On the store host, stream the run logs off the store (read-only; see fleet_logs_pull.sh):
    bash reports/scripts/fleet_logs_pull.sh <store-root> > payload.txt     # then decode into <raw-dir>
    # 2. Pack the raw logs into the committed extracts (redacts any Mapillary token):
    python reports/scripts/fleet_thresholds.py pack <raw-dir>
    # 3. Reduce the committed extracts (makes no request, reads nothing but reports/data/):
    python reports/scripts/fleet_thresholds.py analyze [--write]

Step 3 prints the markdown tables quoted in reports/2026-10-06-fleet-thresholds-and-frame-refusals.md, and
with --write rewrites reports/data/2026-10-06-fleet-thresholds.json. tests/test_fleet_thresholds_report.py
re-derives that JSON from the committed extracts and asserts every table cell appears in the report.

Why the measurement is built this way, since the obvious route does not exist:

* **scrape.log has no timestamps.** Its handler uses logging.BASIC_FORMAT (`DownloadRunner.configure_logging`),
  so a raise line cannot be dated on its own. What dates it is the run it sits in: every image phase that
  finishes writes exactly one `IMAGEDOWNLOAD: Final result: Completed X of Y (s success, f fallback success,
  F failed, K skipped)` DEBUG line, and the same run writes exactly one `log.csv` row whose fields 7-11 are
  those same five counts. So the image runs in scrape.log are paired with the image rows of log.csv from the
  END backwards, and every pair is checked field for field (`align_runs`). A pair that disagrees stops the
  pairing for that city rather than being trusted.
* **There is no per-raise duration anywhere durable** - the point of #178's step 1, which has not landed.
  The bound used here is the image phase's whole duration (`log.csv` field 12, whole minutes) divided by
  its raises, on runs where nothing was answered. It is an upper bound on the mean raise: the phase also
  spends time on the candidate loop's own sshfs round trips.
* **`answered` is not in log.csv either.** Fields 9 and 10 are seeded from the ledger, so a run's own
  permanent verdicts are recovered from consecutive rows: field 9 of run k is seed_k + permanent_k +
  raised_k + frame_refused_k, and seed_{k+1} = seed_k + permanent_k. That needs the previous row of the
  same city to be the previous image run with nothing in between; when it is not, `permanent` is None
  (unknown) and the run's `answered` is None too - never guessed as zero.
"""

import argparse
import collections
import datetime
import gzip
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from studyfmt import fmt, num  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data')

PREFIX = '2026-10-06-'
LOG_CSV_FILE = PREFIX + 'fleet-log-csv.csv.gz'
SCRAPE_LOG_FILE = PREFIX + 'fleet-scrape-log.tsv.gz'
QUEUE_LOG_FILE = PREFIX + 'fleet-scrape-queue-log.txt.gz'
DC_ZERO_ROWS_FILE = PREFIX + 'dc-ledger-downloaded-0.csv.gz'
DC_CENSUS_FILE = PREFIX + 'dc-ledger-census.json'
RESULT_FILE = PREFIX + 'fleet-thresholds.json'

# log.csv rows older than this are not packed: the scraper moved to the venv box on 2026-09-01, and its
# scrape.log (the only source of raise lines) starts there.
LOG_CSV_SINCE = '2026-09-01'
# First night on c9ff03c: photometa zoom (#156) and `images-no-success` (#174) live together.
DEPLOY_NIGHT = '2026-10-05'
# The production schedule's local offset over the whole window (PDT; DST ends 2026-11-01). A night runs from
# 19:00 local into the small hours, so a run's night is its local date twelve hours earlier.
LOCAL_OFFSET = datetime.timedelta(hours=-7)
NIGHT_SHIFT = datetime.timedelta(hours=12)

# DownloadRunner's constants at origin/master ec80c4c, the values being measured.
CURRENT_MIN_RAISED = 10
CURRENT_MIN_MEAN_RAISE_SECONDS = 60.0
FLOOR_SWEEP = (5, 10, 15, 16, 17, 20, 25, 30, 50)
GATE_SWEEP = (30.0, 60.0, 90.0, 120.0, 180.0)

FINAL_RE = re.compile(r'IMAGEDOWNLOAD: Final result: Completed (\d+) of (\d+) \((\d+) success, (\d+) fallback '
                      r'success, (\d+) failed, (\d+) skipped\)')
RAISE_RE = re.compile(r'IMAGEDOWNLOAD: Failed to download pano (\S+) (?:due to error |\((.*?)\): refused by Google)')
NO_SUCCESS_RE = re.compile(r'IMAGEDOWNLOAD: WARNING - all (\d+) attempted panos raised and none was answered')
TRIP_RE = re.compile(r'IMAGEDOWNLOAD: (\d+) consecutive permanent failures|IMAGEDOWNLOAD: Google refused \d+ '
                     r'consecutive GSV panos')
FRAME_RE = re.compile(r"frame disagreement: (?:the app's frame is (\d+)x(\d+) but Google serves this pano at "
                      r"(\d+x\d+)|photometa reports this pano \(served at (\d+x\d+)\) in tiles|the app's frame is "
                      r"(\d+)x(\d+) but a tile past its grid)")
# The photometa-arm lines #185's refusals sit beside: what photometa did NOT answer cleanly.
PHOTOMETA_LINE_RES = collections.OrderedDict([
    ('tile_size_warning', re.compile(r'IMAGEDOWNLOAD: pano \S+: photometa reports \S+ tiles, not')),
    ('probe_past_grid_warning', re.compile(r'IMAGEDOWNLOAD: pano \S+: tile \(\d+, \d+\) past a')),
    ('photometa_unavailable', re.compile(r'IMAGEDOWNLOAD: pano \S+: photometa unavailable')),
    ('photometa_refused', re.compile(r'IMAGEDOWNLOAD: pano \S+: Google refused the photometa request')),
    ('photometa_given_up', re.compile(r'IMAGEDOWNLOAD: photometa failed \d+ times in a row')),
])
# A log.csv row starts with its date. Hand-added header rows (`start_time,...`) sort after any date as
# strings, so a bare `>= LOG_CSV_SINCE` let 53 of them through (#208 review).
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}')
TOKEN_RES = (re.compile(r'(access_token=)[^&\s\'"]+'), re.compile(r'MLY\|[^\s&\'"]+'))


def redact(line):
    """A log line with any Mapillary token removed. richmond-va's scrape.log still carries one (#100)."""
    line = TOKEN_RES[0].sub(r'\1<redacted>', line)
    return TOKEN_RES[1].sub('MLY|<redacted>', line)


# ----------------------------------------------------------------------------------------------- pack


def _gz_writer(path):
    """A text writer for a .gz extract with a zero mtime in its header, so re-packing the same logs
    rewrites byte-identical files instead of a binary diff per run."""
    return io.TextIOWrapper(gzip.GzipFile(path, mode='wb', mtime=0), encoding='utf-8', newline='\n')


def pack(raw_dir, out_dir):
    """Write the committed extracts from a directory laid out like the store root (<city>/log.csv, ...)."""
    cities = sorted(d for d in os.listdir(raw_dir) if os.path.isfile(os.path.join(raw_dir, d, 'log.csv')))
    with _gz_writer(os.path.join(out_dir, LOG_CSV_FILE)) as out:
        for city in cities:
            with open(os.path.join(raw_dir, city, 'log.csv'), encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.rstrip('\r\n')
                    if DATE_RE.match(line) and line[:10] >= LOG_CSV_SINCE:
                        out.write('%s,%s\n' % (city, redact(line)))
    with _gz_writer(os.path.join(out_dir, SCRAPE_LOG_FILE)) as out:
        for city in cities:
            # Oldest rotation first, so the city's lines stay in the order they were written.
            names = sorted((n for n in os.listdir(os.path.join(raw_dir, city)) if n.startswith('scrape.log')),
                           key=lambda n: -int(n.rsplit('.', 1)[1]) if n[-1].isdigit() else 0)
            for name in names:
                with open(os.path.join(raw_dir, city, name), encoding='utf-8', errors='replace') as f:
                    for line in f:
                        out.write('%s\t%s\n' % (city, redact(line.rstrip('\r\n'))))
    with _gz_writer(os.path.join(out_dir, QUEUE_LOG_FILE)) as out:
        with open(os.path.join(raw_dir, 'scrape_queue.log'), encoding='utf-8', errors='replace') as f:
            for line in f:
                out.write(redact(line.rstrip('\r\n')) + '\n')
    with open(os.path.join(raw_dir, 'washington-dc', 'log.csv'), encoding='utf-8', errors='replace') as f:
        dc_starts = [ln.split(',', 1)[0] for ln in f if DATE_RE.match(ln) and ln[:10] >= LOG_CSV_SINCE]
    census, zero_rows = dc_ledger_census(os.path.join(raw_dir, 'washington-dc', 'pano_id_log.csv'), dc_starts)
    with _gz_writer(os.path.join(out_dir, DC_ZERO_ROWS_FILE)) as out:
        out.write('pano_id,downloaded,fetched_at\n')
        for r in zero_rows:
            out.write(','.join(r) + '\n')
    with open(os.path.join(out_dir, DC_CENSUS_FILE), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(census, f, indent=1, sort_keys=True, allow_nan=False)
        f.write('\n')
    return cities


def dc_ledger_census(path, run_starts):
    """Counts of an image ledger by verdict and fetched_at date, and its downloaded=0 rows.

    A two-field row (written before #129) has no stamp, and a blank stamp is a `skipped` verdict; both are
    "unknown", kept apart because they mean different things.
    """
    counts = collections.Counter()
    widths = collections.Counter()
    zero_rows = []
    stamped = []
    with open(path, encoding='utf-8') as f:
        for i, line in enumerate(f):
            fields = line.rstrip('\r\n').split(',')
            if i == 0 and fields[0] == 'pano_id':
                continue
            widths[len(fields)] += 1
            verdict = fields[1] if len(fields) > 1 else ''
            stamp = fields[2] if len(fields) > 2 else None
            day = '(two-field row)' if stamp is None else ('(blank stamp)' if stamp == '' else stamp[:10])
            counts['%s|%s' % (verdict, day)] += 1
            if stamp:
                stamped.append((verdict, stamp))
            if verdict == '0':
                zero_rows.append(fields[:3] if len(fields) >= 3 else fields + [''])
    return {'rows': sum(widths.values()), 'row_widths': {str(k): v for k, v in sorted(widths.items())},
            'verdict_by_day': dict(sorted(counts.items())),
            'stamped_streaks': zero_streaks(stamped, run_starts)}, zero_rows


def _instant(ts):
    t = datetime.datetime.fromisoformat(ts.strip())
    return t if t.tzinfo is not None else t.replace(tzinfo=datetime.timezone.utc)


def zero_streaks(stamped, run_starts):
    """Per run: stamped rows, downloaded=0 rows, and the longest run of consecutive downloaded=0 rows in file
    order within that run (#166 section D).

    `stamped` is (verdict, fetched_at) in file order; `run_starts` is the city's log.csv column 1. A row
    belongs to the latest run that started at or before its stamp, so a pass that crosses midnight stays one
    run - grouping by calendar date would split it and under-report its streak (#208 review). A streak never
    spans two runs, and any non-0 row of the same run ends it: the breaker resets only on a success, and a
    stamped row is a success or a 0 (a skip is blank-stamped and so not in this input). Keyed by the run's
    start stamp, with its schedule night beside it.

        >>> zero_streaks([('0', '2026-09-24 20:10:00-07:00'), ('0', '2026-09-25 00:10:00-07:00'),
        ...               ('0', '2026-09-25 01:20:00-07:00')],
        ...              ['2026-09-24 20:04:00-07:00', '2026-09-25 01:11:00-07:00'])[
        ...     '2026-09-24 20:04:00-07:00']['longest_zero_streak']
        2
    """
    starts = sorted((_instant(t), t) for t in run_starts)
    out = {}
    streak, prev_key = 0, None
    for verdict, stamp in stamped:
        at = _instant(stamp)
        owners = [t for inst, t in starts if inst <= at]
        key = owners[-1] if owners else '(before any run)'
        if key != prev_key:
            streak, prev_key = 0, key
        d = out.setdefault(key, {'night': night_of(key) if owners else None, 'rows': 0, 'downloaded_0': 0,
                                 'longest_zero_streak': 0})
        d['rows'] += 1
        if verdict == '0':
            streak += 1
            d['downloaded_0'] += 1
            d['longest_zero_streak'] = max(d['longest_zero_streak'], streak)
        else:
            streak = 0
    return dict(sorted(out.items()))


# -------------------------------------------------------------------------------------------- readers


def read_log_csv(path):
    """{city: [row dict, ...]} in file order. Fields are kept as strings; blank means the phase never ran."""
    rows = collections.defaultdict(list)
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            city, rest = line.rstrip('\n').split(',', 1)
            fields = rest.split(',')
            fields += [''] * (19 - len(fields))
            if not DATE_RE.match(fields[0]):
                continue    # a header row, not a run
            rows[city].append({'ts': fields[0], 'fields': fields})
    return dict(rows)


def read_scrape_logs(path):
    """{city: [line, ...]} in file order."""
    lines = collections.defaultdict(list)
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            city, text = line.rstrip('\n').split('\t', 1)
            lines[city].append(text)
    return dict(lines)


def split_image_runs(lines):
    """The image runs in one city's scrape.log, each closed by its `Final result` line.

    Lines before the first close that never get one (a run that crashed mid-phase) are attached to the next
    run that does close, so a crashed run's raises are not lost - and `align_runs` then refuses the pair,
    because the crash also wrote a log.csv row with blank image fields between them.
    """
    runs, cur = [], None

    def fresh():
        return {'raise_ids': [], 'raise_kinds': collections.Counter(), 'frame': [], 'edge_band': [],
                'tripped': False,
                'no_success_line': None, 'photometa': collections.Counter()}

    cur = fresh()
    for line in lines:
        m = RAISE_RE.search(line)
        if m:
            if 'black band' in line:
                cur['edge_band'].append(m.group(1))     # #213's EdgeBandError: its own bucket, still an answer
            elif 'frame disagreement' in line:
                fm = FRAME_RE.search(line)
                cur['frame'].append({'pano_id': m.group(1), 'line': line, 'kind': frame_kind(fm)})
            else:
                cur['raise_ids'].append(m.group(1))
                cur['raise_kinds'][raise_kind(line)] += 1
            continue
        m = NO_SUCCESS_RE.search(line)
        if m:
            cur['no_success_line'] = int(m.group(1))
            continue
        if TRIP_RE.search(line):
            cur['tripped'] = True
            continue
        for key, rx in PHOTOMETA_LINE_RES.items():
            if rx.search(line):
                cur['photometa'][key] += 1
        m = FINAL_RE.search(line)
        if m:
            completed, total, success, fallback, failed, skipped = (int(g) for g in m.groups())
            cur.update(completed=completed, total=total, success=success, fallback=fallback, failed=failed,
                       skipped=skipped)
            runs.append(cur)
            cur = fresh()
    return runs


def raise_kind(line):
    """A raise's error text with its pano id, addresses and numbers' variable parts folded away."""
    if 'refused by Google' in line:
        return 'refused by Google (push-back)'
    text = line.split(' due to error ', 1)[-1]
    if text.startswith('cannot identify image file'):
        return 'cannot identify image file'
    m = re.match(r'cbk probe answered (\d+), not 200', text)
    if m:
        return 'cbk probe answered %s, not 200' % m.group(1)
    return re.sub(r'\d+', 'N', text)[:80]


def frame_kind(match):
    if match is None:
        return 'unparsed'
    if match.group(1):
        return 'frame'
    if match.group(4):
        return 'tile_size'
    return 'probe'


def image_row(row):
    """True for a log.csv row whose image phase finished (field 7 present)."""
    return row['fields'][6] != ''


def row_counts(row):
    f = row['fields']
    return tuple(int(f[i]) for i in (6, 7, 8, 9, 10))


def run_counts(run):
    return (run['success'], run['fallback'], run['failed'], run['skipped'], run['completed'])


def align_runs(runs, rows):
    """Pair scrape.log image runs with log.csv rows from the end backwards; returns (pairs, mismatch).

    Each pair is (run, row_index). The pairing walks the city's log.csv image rows and scrape.log runs in
    reverse together and stops at the first pair whose five counts disagree, which it reports, so a
    misalignment is never silently absorbed.
    """
    image_idx = [i for i, r in enumerate(rows) if image_row(r)]
    pairs = []
    mismatch = None
    for run, idx in zip(reversed(runs), reversed(image_idx)):
        if run_counts(run) != row_counts(rows[idx]):
            mismatch = {'ts': rows[idx]['ts'], 'row': list(row_counts(rows[idx])), 'run': list(run_counts(run))}
            break
        pairs.append((run, idx))
    pairs.reverse()
    return pairs, mismatch


def shift_identification(runs, rows, max_shift=3):
    """Whether the count check actually pins the pairing, for this city (#208 review).

    A mature city with nothing new writes the same five counts night after night, so a pairing shifted by
    one run can pass every pair and the check proves nothing about the dates. For each shift s in 1..max_shift
    this pairs the k-th run from the end with the (k+s)-th image row from the end and finds the first pair
    that disagrees. Returns:

      * shift1_passes_fully - a one-run shift never disagrees (the check identifies nothing here);
      * raise_runs_identified - every run with a raise lies at or beyond the first disagreement of every
        shift, so no shifted pairing could carry that run's raises to another row. True when there is no
        raise to date.
    """
    image_idx = [i for i, r in enumerate(rows) if image_row(r)]
    rev_runs = list(reversed(runs))
    rev_rows = list(reversed(image_idx))
    raise_positions = [k for k, run in enumerate(rev_runs) if run['raise_ids']]
    shift1_full, identified = False, True
    for shift in range(1, max_shift + 1):
        first_bad = None
        for k, run in enumerate(rev_runs):
            if k + shift >= len(rev_rows):
                break
            if run_counts(run) != row_counts(rows[rev_rows[k + shift]]):
                first_bad = k
                break
        if shift == 1 and first_bad is None:
            shift1_full = True
        # A raise run is mis-datable under this shift when the shifted pairing reaches it (it has a row to
        # land on) before any pair disagrees.
        limit = len(rev_rows) - shift if first_bad is None else first_bad
        if any(k < limit for k in raise_positions):
            identified = False
    return {'shift1_passes_fully': shift1_full, 'raise_runs_identified': identified}


def night_of(ts):
    """The schedule night a log.csv stamp belongs to (local date twelve hours earlier).

    Rows before #101 carry no offset and were written on a UTC host; read them as UTC, as docs/ops.md says.
    """
    t = datetime.datetime.fromisoformat(ts.strip())
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    local = t.astimezone(datetime.timezone.utc).replace(tzinfo=None) + LOCAL_OFFSET
    return (local - NIGHT_SHIFT).date().isoformat()


# -------------------------------------------------------------------------------------------- reduce


def build_runs(city, runs, rows):
    """One record per aligned image run of a city, with the derived fields #178 asks for."""
    pairs, mismatch = align_runs(runs, rows)
    out = []
    prev_idx, prev_rec = None, None
    for run, idx in pairs:
        row = rows[idx]
        raised = len(run['raise_ids'])
        frame = len(run['frame']) + len(run['edge_band'])   # both are answers for images-no-success
        seed = None
        if prev_rec is not None and prev_idx == idx - 1:
            seed = prev_rec['failed'] - prev_rec['raised'] - prev_rec['frame_refused']
        permanent = None if seed is None else run['failed'] - raised - frame - seed
        if permanent is not None and permanent < 0:
            permanent = None    # a ledger edited between runs; unknown, not negative
        successes = run['success'] + run['fallback']
        answered = None if permanent is None else successes + permanent + frame
        if successes > 0 or frame > 0:
            answered = answered if answered is not None else successes + frame   # a floor is enough: > 0
        if run['tripped']:
            stop = 'tripped'
        elif run['completed'] < run['total']:
            stop = 'max-runtime'
        else:
            stop = None
        minutes = row['fields'][11]
        minutes = int(minutes) if minutes != '' else None
        mean_ub = None
        if raised and minutes is not None:
            mean_ub = num((minutes + 0.5) * 60.0 / raised)
        rec = {'city': city, 'ts': row['ts'], 'night': night_of(row['ts']),
               'era': 'post-deploy' if night_of(row['ts']) >= DEPLOY_NIGHT else 'pre-deploy',
               'raised': raised, 'raise_ids': sorted(set(run['raise_ids'])),
               'raise_kinds': dict(sorted(run['raise_kinds'].items())), 'frame_refused': frame,
               'frame_refusals': run['frame'], 'edge_band_refused': len(run['edge_band']), 'success': run['success'], 'fallback': run['fallback'],
               'failed': run['failed'], 'permanent': permanent, 'answered': answered,
               'image_minutes': minutes, 'stop': stop, 'mean_raise_seconds_upper_bound': mean_ub,
               'no_success_line': run['no_success_line'], 'photometa_lines': dict(run['photometa'])}
        out.append(rec)
        prev_idx, prev_rec = idx, rec
    return out, mismatch, len(runs), sum(1 for r in rows if image_row(r))


def count_arm(rec, floor):
    return rec['answered'] == 0 and rec['raised'] >= floor


def budget_arm(rec, gate):
    """Whether the budget arm COULD fire: the mean raise is only bounded from above here."""
    return (rec['answered'] == 0 and rec['stop'] == 'max-runtime' and rec['raised'] >= 1
            and rec['mean_raise_seconds_upper_bound'] is not None
            and rec['mean_raise_seconds_upper_bound'] >= gate)


def sweep(records):
    """Runs and city-nights each candidate threshold fires on, per era."""
    out = {'floor': [], 'gate': []}
    for era in ('pre-deploy', 'post-deploy'):
        recs = [r for r in records if r['era'] == era]
        for floor in FLOOR_SWEEP:
            hits = [r for r in recs if count_arm(r, floor)]
            out['floor'].append({'era': era, 'floor': floor, 'runs': len(hits),
                                 'city_nights': len({(r['city'], r['night']) for r in hits}),
                                 'cities': sorted({r['city'] for r in hits})})
        for gate in GATE_SWEEP:
            hits = [r for r in recs if budget_arm(r, gate)]
            out['gate'].append({'era': era, 'gate_seconds': gate, 'runs': len(hits),
                                'city_nights': len({(r['city'], r['night']) for r in hits}),
                                'cities': sorted({r['city'] for r in hits})})
    return out


def city_summary(records):
    out = []
    for city in sorted({r['city'] for r in records}):
        recs = [r for r in records if r['city'] == city]
        raising = [r for r in recs if r['raised']]
        if not raising:
            continue
        quiet = [r for r in raising if r['answered'] == 0]
        ids = set()
        kinds = collections.Counter()
        for r in raising:
            ids.update(r['raise_ids'])
            kinds.update(r['raise_kinds'])
        post = [r for r in raising if r['era'] == 'post-deploy']
        out.append({'city': city, 'image_runs': len(recs), 'runs_with_raises': len(raising),
                    'max_raised': max(r['raised'] for r in raising),
                    'runs_answered_0_with_raises': len(quiet),
                    'max_raised_answered_0': max((r['raised'] for r in quiet), default=None),
                    'distinct_raising_panos': len(ids), 'raise_kinds': dict(sorted(kinds.items())),
                    'post_deploy_raised': [r['raised'] for r in post],
                    'post_deploy_answered': [r['answered'] for r in post],
                    'mean_raise_upper_bound_answered_0': [r['mean_raise_seconds_upper_bound'] for r in quiet]})
    return out


def queue_conditions(queue_lines):
    """City result lines in scrape_queue.log carrying `conditions:`, by night."""
    found = collections.Counter()
    rx = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \w+ (\S+): \w+ \(exit \S+\) in [\d.]+ min; '
                    r'conditions: (.*)$')
    for line in queue_lines:
        m = rx.match(line)
        if m:
            for code in m.group(3).split(', '):
                found['%s|%s' % (code, m.group(2))] += 1
    return dict(sorted(found.items()))


def frame_summary(records, scrape_lines):
    """#185 Part 1: refusals per city with distinct ids and frames, over every scrape.log line."""
    per_city = {}
    edge_band = {}
    photometa = collections.Counter()
    for city, lines in sorted(scrape_lines.items()):
        hits = [ln for ln in lines if 'frame disagreement' in ln and 'black band' not in ln]
        edge = sum(1 for ln in lines if 'frame disagreement' in ln and 'black band' in ln)
        if edge:
            edge_band[city] = edge
        for ln in lines:
            for key, rx in PHOTOMETA_LINE_RES.items():
                if rx.search(ln):
                    photometa['%s|%s' % (key, city)] += 1
        if hits:
            ids = sorted({RAISE_RE.search(h).group(1) for h in hits if RAISE_RE.search(h)})
            frames = collections.Counter()
            for h in hits:
                fm = FRAME_RE.search(h)
                if fm and fm.group(1):
                    frames['app %sx%s, Google %s' % (fm.group(1), fm.group(2), fm.group(3))] += 1
                else:
                    frames[frame_kind(fm)] += 1
            per_city[city] = {'lines': len(hits), 'distinct_panos': len(ids), 'pano_ids': ids,
                              'frames': dict(sorted(frames.items()))}
    return {'cities_scanned': len(scrape_lines),
            'lines_scanned': sum(len(v) for v in scrape_lines.values()),
            'refusal_lines': sum(c['lines'] for c in per_city.values()), 'per_city': per_city,
            'edge_band_lines': edge_band,
            'photometa_lines': dict(sorted(photometa.items()))}


def analyze(data_dir=DATA_DIR):
    rows = read_log_csv(os.path.join(data_dir, LOG_CSV_FILE))
    scrape = read_scrape_logs(os.path.join(data_dir, SCRAPE_LOG_FILE))
    with gzip.open(os.path.join(data_dir, QUEUE_LOG_FILE), 'rt', encoding='utf-8') as f:
        queue_lines = [ln.rstrip('\n') for ln in f]
    with open(os.path.join(data_dir, DC_CENSUS_FILE), encoding='utf-8') as f:
        dc = json.load(f)

    records, alignment = [], {}
    for city in sorted(set(rows) | set(scrape)):
        recs, mismatch, n_runs, n_rows = build_runs(city, split_image_runs(scrape.get(city, [])),
                                                    rows.get(city, []))
        records.extend(recs)
        alignment[city] = {'scrape_log_runs': n_runs, 'log_csv_image_rows': n_rows, 'paired': len(recs),
                           'mismatch': mismatch}
        alignment[city].update(shift_identification(split_image_runs(scrape.get(city, [])), rows.get(city, [])))
    nights = sorted({r['night'] for r in records})
    unpaired = {c: a['log_csv_image_rows'] - a['paired'] for c, a in alignment.items()
                if a['log_csv_image_rows'] != a['paired']}
    post = [r for r in records if r['era'] == 'post-deploy']
    fired_observed = [r for r in records if r['no_success_line'] is not None]
    dc_zero = {k.split('|', 1)[1]: v for k, v in dc['verdict_by_day'].items() if k.startswith('0|')}
    return {
        'source': {'log_csv_since': LOG_CSV_SINCE, 'deploy_night': DEPLOY_NIGHT,
                   'first_night': nights[0] if nights else None, 'last_night': nights[-1] if nights else None,
                   'nights': len(nights), 'post_deploy_nights': sorted({r['night'] for r in post}),
                   'cities': len(alignment)},
        'constants': {'IMAGE_NO_SUCCESS_MIN_RAISED': CURRENT_MIN_RAISED,
                      'IMAGE_NO_SUCCESS_MIN_MEAN_RAISE_SECONDS': CURRENT_MIN_MEAN_RAISE_SECONDS},
        'alignment': alignment,
        'alignment_summary': {
            'paired': sum(a['paired'] for a in alignment.values()),
            'log_csv_image_rows': sum(a['log_csv_image_rows'] for a in alignment.values()),
            'unpaired_rows': sum(unpaired.values()), 'cities_with_unpaired_rows': len(unpaired),
            'cities_shift1_passes_fully': sorted(c for c, a in alignment.items() if a['shift1_passes_fully']),
            'cities_with_raises_not_identified': sorted(c for c, a in alignment.items()
                                                        if not a['raise_runs_identified'])},
        'totals': {'image_runs': len(records), 'runs_with_raises': sum(1 for r in records if r['raised']),
                   'runs_answered_unknown': sum(1 for r in records if r['answered'] is None),
                   'raise_lines': sum(r['raised'] for r in records),
                   'post_deploy_image_runs': len(post),
                   'post_deploy_runs_with_raises': sum(1 for r in post if r['raised']),
                   'post_deploy_successes': sum(r['success'] + r['fallback'] for r in post),
                   'post_deploy_frame_refused': sum(r['frame_refused'] for r in post),
                   'condition_lines_observed': len(fired_observed)},
        'cities_with_raises': city_summary(records),
        'sweep': sweep(records),
        'queue_conditions': queue_conditions(queue_lines),
        'frame_disagreement': frame_summary(records, scrape),
        'dc_ledger': {'rows': dc['rows'], 'downloaded_0_by_day': dc_zero,
                      'stamped_streaks': dc['stamped_streaks'],
                      'downloaded_0': sum(dc_zero.values()),
                      'downloaded_0_on_or_after_2026_09_10': sum(v for k, v in dc_zero.items()
                                                                  if k[:1].isdigit() and k >= '2026-09-10'),
                      'scrape_log_raise_lines': sum(1 for ln in scrape.get('washington-dc', [])
                                                    if RAISE_RE.search(ln)),
                      'scrape_log_black_stitch_lines': sum(1 for ln in scrape.get('washington-dc', [])
                                                           if '% black' in ln)},
        'runs': [{k: v for k, v in r.items() if k != 'frame_refusals'} for r in records if r['raised']
                 or r['frame_refused']],
    }


# --------------------------------------------------------------------------------------------- print


def tables(result):
    """The markdown tables the report quotes, as one string."""
    out = []
    out.append('| City | Image runs | Runs with raises | Max raises in a run | Runs with raises and 0 answered '
               '| Max raises with 0 answered | Distinct raising panos | Upper bound on mean raise, 0-answered '
               'runs (s) |')
    out.append('|---|---|---|---|---|---|---|---|')
    for c in result['cities_with_raises']:
        bounds = sorted(b for b in c['mean_raise_upper_bound_answered_0'] if b is not None)
        span = '%s-%s' % (fmt(bounds[0], '.0f'), fmt(bounds[-1], '.0f')) if bounds else 'n/a'
        out.append('| %s | %d | %d | %d | %d | %s | %d | %s |' % (
            c['city'], c['image_runs'], c['runs_with_raises'], c['max_raised'],
            c['runs_answered_0_with_raises'], fmt(c['max_raised_answered_0']), c['distinct_raising_panos'], span))
    out.append('')
    out.append('| Era | Floor (raises) | Runs that fire | City-nights | Cities |')
    out.append('|---|---|---|---|---|')
    for s in result['sweep']['floor']:
        out.append('| %s | %d | %d | %d | %s |' % (s['era'], s['floor'], s['runs'], s['city_nights'],
                                                 ', '.join(s['cities']) or '-'))
    out.append('')
    out.append('| Era | Gate (s) | Runs that could fire | City-nights | Cities |')
    out.append('|---|---|---|---|---|')
    for s in result['sweep']['gate']:
        out.append('| %s | %s | %d | %d | %s |' % (s['era'], fmt(s['gate_seconds'], '.0f'), s['runs'],
                                                 s['city_nights'], ', '.join(s['cities']) or '-'))
    out.append('')
    out.append('| City | Night | Era | Raised | Answered | Successes | Image min | Stop | Mean raise upper '
               'bound (s) | Raise kinds |')
    out.append('|---|---|---|---|---|---|---|---|---|---|')
    for r in result['runs']:
        kinds = '; '.join('%s x%d' % (k, v) for k, v in r['raise_kinds'].items())
        out.append('| %s | %s | %s | %d | %s | %d | %s | %s | %s | %s |' % (
            r['city'], r['night'], r['era'], r['raised'], fmt(r['answered']), r['success'] + r['fallback'],
            fmt(r['image_minutes']), r['stop'] or 'none', fmt(r['mean_raise_seconds_upper_bound'], '.0f'), kinds))
    out.append('')
    out.append('| Run (log.csv start) | Night | rows | `downloaded=0` | longest consecutive `downloaded=0` |')
    out.append('|---|---|---|---|---|')
    for run, s in result['dc_ledger']['stamped_streaks'].items():
        out.append('| %s | %s | %d | %d | %d |' % (run, fmt(s['night']), s['rows'], s['downloaded_0'],
                                                 s['longest_zero_streak']))
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('pack', help='raw store logs -> committed extracts')
    p.add_argument('raw_dir')
    p.add_argument('--out-dir', default=DATA_DIR)
    a = sub.add_parser('analyze', help='committed extracts -> tables (and --write the JSON)')
    a.add_argument('--data-dir', default=DATA_DIR)
    a.add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    if args.cmd == 'pack':
        cities = pack(args.raw_dir, args.out_dir)
        print('packed %d cities into %s' % (len(cities), args.out_dir))
        return 0
    result = analyze(args.data_dir)
    print(json.dumps({k: result[k] for k in ('source', 'totals', 'dc_ledger')}, indent=1))
    print(json.dumps({'frame_refusal_lines': result['frame_disagreement']['refusal_lines'],
                      'photometa_lines': result['frame_disagreement']['photometa_lines'],
                      'queue_conditions': result['queue_conditions'],
                      'alignment_mismatches': {c: a['mismatch'] for c, a in result['alignment'].items()
                                               if a['mismatch']}}, indent=1))
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
