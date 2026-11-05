"""What the photo host answers for each form of `CAoS` photosphere id, and what the fleet's image ledgers
already say about those ids (#223).

#227 fetches a user photosphere from the photo host in one request. It was merged with the AF1Qip form of photo
id measured at 24 of 24 served, and the older CIHM form at 9 of 9 answering 400, with the 400 left a TRANSIENT
raise because nine is not a measurement to hang a permanent `downloaded=0` on. This study is the measurement:

1. **Every non-AF1Qip photosphere in chicago-il's pano list, asked once at the photo host** (`measure`): the
   same session, URL and headers `_download_photosphere` uses, headers only (`stream=True`, the body never
   read), paced 0.5-1.0 s apart. One CSV row per id. `measure --all-forms` repeats it over every id, the
   AF1Qip form included, into a second file.
2. **One id of each form at three other Google photo hosts**, with an AF1Qip control (`probe`), to tell a
   refusal of the *form* from a refusal of the *host*.
3. **Every `CAoS` row in every city's `pano_id_log.csv`** on the store (`pack-ledger`), to see what the fleet
   had already written about these ids before #227 - which turned out to be the larger finding - and
   chicago-il's `depth_log.csv` rows for them (`pack-depth-ledger`).

`analyze` reads only the committed files under reports/data/ and derives every number in
reports/2026-11-05-photosphere-forms.md; tests/test_photosphere_forms_study.py asserts that the committed JSON
is what it derives and that every table line and prose count it prints appears in the report. Production code
never imports this module; it imports the downloader's own photosphere helpers (`is_photosphere_id`,
`photosphere_photo_id`, `PHOTOSPHERE_URL`, `_photosphere_session`) so the measurement asks exactly the question
the nightly run asks.

Usage, from the repo root (the live steps make requests; `analyze` makes none)::

    # 1. The pano list, saved once so the slice committed here can be re-derived from it:
    #    curl -s https://sidewalk-chicago.cs.washington.edu/adminapi/panos > chicago_panos.json
    python reports/scripts/photosphere_forms.py pack-list chicago_panos.json --city chicago-il
    python reports/scripts/photosphere_forms.py measure              # the non-AF1Qip ids
    python reports/scripts/photosphere_forms.py measure --all-forms  # every id, into the second file
    python reports/scripts/photosphere_forms.py probe
    # 2. On a host that mounts the store (makelab2: /m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas):
    #    for d in */; do c=${d%/}; [ -f "$c/pano_id_log.csv" ] || continue;
    #        grep "^CAoS" "$c/pano_id_log.csv" | sed "s/^/$c,/"; done > fleet_caos_ledger.csv
    #    grep "^CAoS" chicago-il/depth_log.csv > chicago_caos_depth.csv
    python reports/scripts/photosphere_forms.py pack-ledger fleet_caos_ledger.csv
    python reports/scripts/photosphere_forms.py pack-depth-ledger chicago_caos_depth.csv
    # 3. Offline:
    python reports/scripts/photosphere_forms.py analyze --write

Every subcommand takes `--data-dir` (default reports/data/).
"""

import argparse
import collections
import csv
import gzip
import io
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import DownloadRunner                                   # noqa: E402  (after the sys.path bootstrap)
from downloaders import gsv                             # noqa: E402
from studyfmt import display_path, fmt                  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data')
PREFIX = '2026-11-05-photosphere-forms'
LIST_FILE = PREFIX + '-chicago-list.json'
IDS_FILE = PREFIX + '-chicago-caos-ids.csv.gz'
MEASUREMENTS_FILE = PREFIX + '-measurements.csv.gz'
MEASUREMENTS_ALL_FILE = PREFIX + '-measurements-all-forms.csv.gz'
PROBE_FILE = PREFIX + '-host-probe.csv'
LEDGER_FILE = PREFIX + '-fleet-ledger.csv.gz'
DEPTH_LEDGER_FILE = PREFIX + '-chicago-depth-ledger.csv.gz'
LOG_TAIL_FILE = PREFIX + '-chicago-log-tail.csv'
STORE_FILES_FILE = PREFIX + '-chicago-caos-files.txt'
RESULT_FILE = PREFIX + '.json'

# The forms a photo id has been seen in, by its leading letters: the downloader's own list, since the #223
# follow-up keyed its verdict on them (one definition, imported back here). The host's answer turned out to depend
# on nothing else, which is why a form is the unit this study counts in. Anything else decodable is `other`; a
# CAoS id that does not decode to a photo id at all is `undecodable` (none were, in chicago-il).
FORMS = gsv.PHOTOSPHERE_FORMS
MEASURED_CITY = 'chicago-il'

# The hosts asked in `probe`: the production URL first, then the two googleusercontent hosts that serve the
# same `/p/<photo id>` paths for Maps photos, then the production host WITHOUT `/p/` as a control for "any
# URL shape the host does not know answers 400 too".
PROBE_HOSTS = (
    gsv.PHOTOSPHERE_URL,
    'https://lh3.googleusercontent.com/p/%s=s0',
    'https://lh5.googleusercontent.com/p/%s=s0',
    'https://lh3.ggpht.com/%s=s0',
)
PACE_SECONDS = (0.5, 1.0)
IDS_COLUMNS = ['pano_id', 'form', 'photo_id', 'has_labels', 'width', 'height']
MEASUREMENT_COLUMNS = ['pano_id', 'form', 'photo_id', 'has_labels', 'status', 'content_type', 'content_length',
                       'final_url', 'note']
PROBE_COLUMNS = ['form', 'pano_id', 'photo_id', 'host', 'status', 'content_type', 'content_length']
LEDGER_COLUMNS = ['city', 'pano_id', 'downloaded', 'fetched_at', 'row_fields']
DEPTH_LEDGER_COLUMNS = ['pano_id', 'depth']


# ---------------------------------------------------------------------------------------- classification


def form_of(photo_id):
    """The form of a decoded photo id, by its leading letters; 'undecodable' for None, 'other' otherwise."""
    return 'undecodable' if photo_id is None else gsv.photosphere_form(photo_id)


def caos_rows(panos):
    """One IDS_COLUMNS row per `CAoS` record in a pano list, in list order."""
    rows = []
    for record in panos:
        pano_id = record.get('pano_id') or ''
        if not gsv.is_photosphere_id(pano_id):
            continue
        photo_id = gsv.photosphere_photo_id(pano_id)
        rows.append({'pano_id': pano_id, 'form': form_of(photo_id), 'photo_id': photo_id or '',
                     'has_labels': str(bool(record.get('has_labels'))),
                     'width': '' if record.get('width') is None else str(record['width']),
                     'height': '' if record.get('height') is None else str(record['height'])})
    return rows


def ask(session, url):
    """One headers-only GET: (status or 'raise', content type, content length, final url, note).

    `stream=True` and an immediate close, so a 200 costs the host no more than a 400 does - the body is the
    production fetch's business, not this measurement's. A raise is recorded as a row, never allowed to stop
    the sweep: the sweep is the measurement, and a connection error in the middle of it is one of its rows.
    """
    try:
        response = session.get(url, timeout=30, stream=True)
    except Exception as e:      # RetryError, connection errors - recorded, never fatal
        return 'raise', '', '', '', type(e).__name__ + ': ' + str(e)[:120]
    try:
        return (str(response.status_code), response.headers.get('Content-Type', ''),
                response.headers.get('Content-Length', ''), response.url, '')
    finally:
        response.close()


def pace():
    time.sleep(random.uniform(*PACE_SECONDS))


# ------------------------------------------------------------------------------------------------- files


class _GzipText:
    """A gzip text writer that owns both handles, with a fixed mtime and no embedded filename, so re-packing
    identical rows gives identical bytes wherever the file is written (GzipFile otherwise records the path's
    basename in its header, and never closes a `fileobj` it was handed). `flush` reaches the disk: a sweep
    stopped half way leaves every row it measured, in a member with no trailer yet - `gzip -dc` prints them and
    then complains about the end of the file; `gzip.open().read()` raises EOFError after them."""

    def __init__(self, path):
        self._raw = open(path, 'wb')
        self._gz = gzip.GzipFile(filename='', mode='wb', fileobj=self._raw, mtime=0)
        self._text = io.TextIOWrapper(self._gz, encoding='utf-8', newline='\n')

    def write(self, s):
        return self._text.write(s)

    def flush(self):
        self._text.flush()
        self._gz.flush()
        self._raw.flush()

    def close(self):
        self._text.close()      # closes the GzipFile too
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _open_text(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', encoding='utf-8', newline='')
    return open(path, encoding='utf-8', newline='')


def write_csv(path, columns, rows):
    opener = _GzipText if path.endswith('.gz') else (lambda p: open(p, 'w', encoding='utf-8', newline='\n'))
    with opener(path) as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_csv(path):
    with _open_text(path) as f:
        return list(csv.DictReader(f))


def pack_list(panos_path, city, data_dir):
    """The committed slice of a pano list: its `CAoS` rows, plus the two totals the slice cannot carry."""
    with open(panos_path, encoding='utf-8') as f:
        panos = json.load(f)
    rows = caos_rows(panos)
    summary = {'city': city, 'pano_count': len(panos),
               'labelled_count': sum(1 for p in panos if p.get('has_labels')), 'caos_count': len(rows)}
    with open(os.path.join(data_dir, LIST_FILE), 'w', encoding='utf-8', newline='\n') as f:
        json.dump(summary, f, indent=1, sort_keys=True, allow_nan=False)
        f.write('\n')
    write_csv(os.path.join(data_dir, IDS_FILE), IDS_COLUMNS, rows)
    return summary


def measure(ids, session, all_forms=False, limit=None, out=None, flush=lambda: None, progress=print):
    """Ask the photo host once per id (every non-AF1Qip id unless `all_forms`), yielding MEASUREMENT rows.

    `out` is a csv.DictWriter the rows also go to as they arrive, `flush` is called after each, so a sweep
    stopped half way leaves what it measured. Undecodable ids are recorded without a request.
    """
    targets = [r for r in ids if all_forms or r['form'] != 'AF1Qip']
    if limit is not None:
        targets = targets[:limit]
    rows, started = [], time.monotonic()
    for i, r in enumerate(targets, 1):
        row = {k: r[k] for k in ('pano_id', 'form', 'photo_id', 'has_labels')}
        if not r['photo_id']:
            row.update(status='', content_type='', content_length='', final_url='', note='undecodable')
        else:
            status, ctype, clen, final, note = ask(session, gsv.PHOTOSPHERE_URL % (r['photo_id'],))
            row.update(status=status, content_type=ctype, content_length=clen, final_url=final, note=note)
            pace()
        rows.append(row)
        if out is not None:
            out.writerow(row)
            flush()
        if i % 50 == 0 or i == len(targets):
            progress('%d/%d %.0fs %s' % (i, len(targets), time.monotonic() - started,
                                         dict(collections.Counter((x['form'], x['status']) for x in rows))))
    return rows


def probe(ids, session, hosts=PROBE_HOSTS):
    """The first id of each form in `ids`, asked at every host in `hosts`; PROBE rows."""
    first = {}
    for r in ids:
        if r['photo_id'] and r['form'] not in first:
            first[r['form']] = r
    rows = []
    for form in sorted(first):
        r = first[form]
        for host in hosts:
            status, ctype, clen, _, _ = ask(session, host % (r['photo_id'],))
            rows.append({'form': form, 'pano_id': r['pano_id'], 'photo_id': r['photo_id'], 'host': host,
                         'status': status, 'content_type': ctype, 'content_length': clen})
            pace()
    return rows


def pack_ledger_lines(lines):
    """`city,<ledger row>` lines, as the grep in the module docstring emits them, to LEDGER rows.

    Row width is kept (`row_fields`), because it is a finding: a two-field row predates the `fetched_at`
    column (2026-09-17 on the production store, #129) and so predates #166, when a non-200 was still a
    verdict. A blank `fetched_at` on a three-field row is a `skipped` verdict's deliberate blank, which is why
    the width is carried separately rather than inferred from the blank. CRLF tolerated: the dump crosses a
    Windows hop.
    """
    rows = []
    for line in lines:
        line = line.rstrip('\r\n')
        if not line:
            continue
        parts = line.split(',')
        city, ledger = parts[0], parts[1:]
        if len(ledger) not in (2, 3) or not gsv.is_photosphere_id(ledger[0]):
            raise ValueError('not a city,<ledger row> line for a CAoS id: %r' % (line,))
        rows.append({'city': city, 'pano_id': ledger[0], 'downloaded': ledger[1],
                     'fetched_at': ledger[2] if len(ledger) == 3 else '', 'row_fields': str(len(ledger))})
    return rows


def pack_depth_ledger_lines(lines):
    """`pano_id,saved|unavailable` lines of a depth ledger, CAoS ids only, to DEPTH_LEDGER rows."""
    rows = []
    for line in lines:
        line = line.rstrip('\r\n')
        if not line:
            continue
        parts = line.split(',')
        if len(parts) != 2 or not gsv.is_photosphere_id(parts[0]):
            raise ValueError('not a depth ledger row for a CAoS id: %r' % (line,))
        rows.append({'pano_id': parts[0], 'depth': parts[1]})
    return rows


# ----------------------------------------------------------------------------------------------- analyze


def _counts(rows, key):
    c = collections.Counter(key(r) for r in rows)
    return {str(k): v for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))}


def _summarise_measurements(measurements):
    out = {}
    for form in sorted({r['form'] for r in measurements}):
        rows = [r for r in measurements if r['form'] == form]
        lengths = [int(r['content_length']) for r in rows if r['content_length']]
        out[form] = {
            'measured': len(rows),
            'labelled': sum(1 for r in rows if r['has_labels'] == 'True'),
            'status': _counts(rows, lambda r: r['status']),
            'content_types': _counts(rows, lambda r: r['content_type']),
            'content_lengths': _counts(rows, lambda r: r['content_length']),
            'content_length_range': [min(lengths), max(lengths)] if lengths else None,
            'final_hosts': _counts(rows, lambda r: r['final_url'].split('/')[2] if '//' in r['final_url'] else ''),
            'redirected': sum(1 for r in rows if r['final_url'] and r['final_url'] != gsv.PHOTOSPHERE_URL % r['photo_id']),
        }
    return out


def _lengths_cell(m):
    """One length when the host answered one; otherwise how many and their span (a JPEG per photo)."""
    if len(m['content_lengths']) <= 3:
        return ', '.join(m['content_lengths']) or 'n/a'
    return '%d distinct, %s-%s bytes' % (len(m['content_lengths']), fmt(m['content_length_range'][0]),
                                         fmt(m['content_length_range'][1]))


def analyze(data_dir=DATA_DIR):
    """Every number the report quotes, from the committed files alone."""
    with open(os.path.join(data_dir, LIST_FILE), encoding='utf-8') as f:
        chicago_list = json.load(f)
    ids = read_csv(os.path.join(data_dir, IDS_FILE))
    measurements = read_csv(os.path.join(data_dir, MEASUREMENTS_FILE))
    measurements_all = read_csv(os.path.join(data_dir, MEASUREMENTS_ALL_FILE))
    probe_rows = read_csv(os.path.join(data_dir, PROBE_FILE))
    ledger = read_csv(os.path.join(data_dir, LEDGER_FILE))
    depth_ledger = read_csv(os.path.join(data_dir, DEPTH_LEDGER_FILE))
    with open(os.path.join(data_dir, LOG_TAIL_FILE), encoding='utf-8', newline='') as f:
        log_rows = [r for r in csv.reader(f) if r]
    with open(os.path.join(data_dir, STORE_FILES_FILE), encoding='utf-8') as f:
        store_listing = [ln for ln in f.read().splitlines() if ln.strip()]

    # 1. The list slice, by form.
    forms = sorted({r['form'] for r in ids} | set(FORMS))
    by_form = {}
    for form in forms:
        rows = [r for r in ids if r['form'] == form]
        by_form[form] = {'ids': len(rows), 'labelled': sum(1 for r in rows if r['has_labels'] == 'True')}
    assert chicago_list['caos_count'] == len(ids), (chicago_list, len(ids))

    # 2. The two sweeps, by form and status.
    measured = _summarise_measurements(measurements)
    measured_ids = {r['pano_id'] for r in measurements}
    unmeasured_non_af1qip = [r['pano_id'] for r in ids if r['form'] != 'AF1Qip' and r['pano_id'] not in measured_ids]
    measured_all = _summarise_measurements(measurements_all)
    unmeasured_all = [r['pano_id'] for r in ids if r['pano_id'] not in {r['pano_id'] for r in measurements_all}]
    # Did any id answer differently between the two sweeps?
    first = {r['pano_id']: r['status'] for r in measurements}
    changed = sorted(r['pano_id'] for r in measurements_all if r['pano_id'] in first and first[r['pano_id']] != r['status'])

    # 3. The host probe.
    probe_table = {}
    for r in probe_rows:
        probe_table.setdefault(r['form'], {})[r['host']] = {'status': r['status'], 'content_type': r['content_type'],
                                                             'content_length': r['content_length']}

    # 4. The fleet ledger.
    for r in ledger:
        r['form'] = form_of(gsv.photosphere_photo_id(r['pano_id']))
    fleet = {
        'rows': len(ledger),
        'cities': len({r['city'] for r in ledger}),
        'other_cities': len({r['city'] for r in ledger}) - 1,
        'by_verdict': _counts(ledger, lambda r: r['downloaded']),
        'by_row_fields': _counts(ledger, lambda r: r['row_fields']),
        'dated_rows': sum(1 for r in ledger if r['fetched_at']),
        'by_form': {form: {'rows': sum(1 for r in ledger if r['form'] == form),
                           'cities': len({r['city'] for r in ledger if r['form'] == form}),
                           'by_verdict': _counts([r for r in ledger if r['form'] == form], lambda r: r['downloaded'])}
                    for form in sorted({r['form'] for r in ledger})},
        'non_af1qip_rows': sum(1 for r in ledger if r['form'] != 'AF1Qip'),
        'per_city': {},
    }
    for city in sorted({r['city'] for r in ledger}):
        rows = [r for r in ledger if r['city'] == city]
        fleet['per_city'][city] = dict(_counts(rows, lambda r: r['form']), total=len(rows))

    # 5. chicago-il: the list joined to its own ledger rows.
    chicago_ledgered = {r['pano_id']: r for r in ledger if r['city'] == MEASURED_CITY}
    join = {'ledgered_not_in_list': sorted(set(chicago_ledgered) - {r['pano_id'] for r in ids}), 'by_form': {}}
    for form in forms:
        rows = [r for r in ids if r['form'] == form]
        led = [r for r in rows if r['pano_id'] in chicago_ledgered]
        join['by_form'][form] = {
            'ids': len(rows),
            'ledgered': len(led),
            'ledgered_verdicts': _counts([chicago_ledgered[r['pano_id']] for r in led], lambda r: r['downloaded']),
            'ledgered_labelled': sum(1 for r in led if r['has_labels'] == 'True'),
            'unledgered': len(rows) - len(led),
            'unledgered_labelled': sum(1 for r in rows if r['pano_id'] not in chicago_ledgered
                                       and r['has_labels'] == 'True'),
        }
    join['ledgered_total'] = sum(v['ledgered'] for v in join['by_form'].values())
    join['unledgered_total'] = sum(v['unledgered'] for v in join['by_form'].values())
    join['ledgered_non_af1qip'] = sum(v['ledgered'] for f, v in join['by_form'].items() if f != 'AF1Qip')
    join['unledgered_non_af1qip'] = sum(v['unledgered'] for f, v in join['by_form'].items() if f != 'AF1Qip')
    join['labelled_non_af1qip'] = sum(by_form[f]['labelled'] for f in forms if f != 'AF1Qip')

    # 6. chicago-il's depth ledger rows for these ids.
    depth = {'rows': len(depth_ledger), 'by_verdict': _counts(depth_ledger, lambda r: r['depth']),
             'ids_in_list_without_a_row': sum(1 for r in ids if r['pano_id'] not in {d['pano_id'] for d in depth_ledger})}

    # 7. chicago-il's last log.csv rows: the image phase's raised count per run (field 20), and the permanent
    #    failures seeded into field 9 net of tonight's raises.
    raised_idx, list_idx, failures_idx = DownloadRunner.IMAGE_RAISED_FIELD - 1, 4, 8
    runs = [{'start': r[0], 'image_eligible': int(r[list_idx]) if r[list_idx] else None,
             'image_failures': int(r[failures_idx]) if r[failures_idx] else None,
             'raised': int(r[raised_idx]) if len(r) > raised_idx and r[raised_idx] else None}
            for r in log_rows]
    raised = [r['raised'] for r in runs if r['raised'] is not None]
    max_raised = max(raised) if raised else None
    at_max = [r for r in runs if r['raised'] == max_raised and max_raised is not None]
    log_tail = {'runs': runs, 'rows': len(runs), 'max_raised': max_raised, 'runs_at_max': len(at_max),
                'failures_net_of_raises_at_max': sorted({r['image_failures'] - r['raised'] for r in at_max
                                                         if r['image_failures'] is not None}),
                'list_snapshot_matches_run': next((r['start'] for r in runs
                                                   if r['image_eligible'] == chicago_list['pano_count']), None)}

    # 8. The photosphere files already on the store.
    store_ids = [ln.rsplit(' ', 1)[-1][:-len('.jpg')] for ln in store_listing]
    store_files = {'count': len(store_ids), 'listing': store_listing,
                   'forms': _counts(store_ids, lambda pid: form_of(gsv.photosphere_photo_id(pid))),
                   'ledgered_0': sum(1 for pid in store_ids if chicago_ledgered.get(pid, {}).get('downloaded') == '0'),
                   'in_list': sum(1 for pid in store_ids if pid in {r['pano_id'] for r in ids})}

    return {
        'source': {
            'measured_city': MEASURED_CITY,
            'photosphere_url': gsv.PHOTOSPHERE_URL,
            'probe_hosts': list(PROBE_HOSTS),
            'pace_seconds': list(PACE_SECONDS),
            'files': [LIST_FILE, IDS_FILE, MEASUREMENTS_FILE, MEASUREMENTS_ALL_FILE, PROBE_FILE, LEDGER_FILE,
                      DEPTH_LEDGER_FILE, LOG_TAIL_FILE, STORE_FILES_FILE],
        },
        'chicago_list': dict(chicago_list, by_form=by_form),
        'measurements': {'rows': len(measurements), 'by_form': measured,
                         'labelled': sum(m['labelled'] for m in measured.values()),
                         'unmeasured_non_af1qip': unmeasured_non_af1qip},
        'measurements_all_forms': {'rows': len(measurements_all), 'by_form': measured_all,
                                   'unmeasured': unmeasured_all, 'status_changed_between_sweeps': changed},
        'host_probe': probe_table,
        'fleet_ledger': fleet,
        'chicago_join': join,
        'chicago_depth_ledger': depth,
        'chicago_log_tail': log_tail,
        'chicago_store_files': store_files,
    }


def _measurement_table(by_form):
    out = ['| Form | Measured | Labelled | 200 | 400 | Other status | Raised | Content types | Content lengths |',
           '|---|---|---|---|---|---|---|---|---|']
    for form, m in by_form.items():
        other = sum(n for s, n in m['status'].items() if s not in ('200', '400', 'raise'))
        out.append('| %s | %d | %d | %d | %d | %d | %d | %s | %s |' % (
            form, m['measured'], m['labelled'], m['status'].get('200', 0), m['status'].get('400', 0), other,
            m['status'].get('raise', 0), ', '.join('`%s`' % k for k in m['content_types']) or 'n/a',
            _lengths_cell(m)))
    return out


def tables(result):
    """The markdown tables the report quotes, as one string."""
    out = []
    forms = [f for f in FORMS if f in result['chicago_list']['by_form']] + \
            [f for f in result['chicago_list']['by_form'] if f not in FORMS]
    out.append('| Form | Ids in the list | Labelled | Ledgered | Ledgered `0` | Ledgered `1` | Unledgered | '
               'Unledgered, labelled |')
    out.append('|---|---|---|---|---|---|---|---|')
    for form in forms:
        j = result['chicago_join']['by_form'][form]
        out.append('| %s | %d | %d | %d | %d | %d | %d | %d |' % (
            form, j['ids'], result['chicago_list']['by_form'][form]['labelled'], j['ledgered'],
            j['ledgered_verdicts'].get('0', 0), j['ledgered_verdicts'].get('1', 0), j['unledgered'],
            j['unledgered_labelled']))
    out.append('')
    out.extend(_measurement_table(result['measurements']['by_form']))
    out.append('')
    out.extend(_measurement_table(result['measurements_all_forms']['by_form']))
    out.append('')
    hosts = result['source']['probe_hosts']
    out.append('| Form | ' + ' | '.join('`%s`' % h for h in hosts) + ' |')
    out.append('|---|' + '---|' * len(hosts))
    for form, answers in result['host_probe'].items():
        out.append('| %s | ' % form + ' | '.join(
            '%s %s, %s bytes' % (answers[h]['status'], answers[h]['content_type'].split(';')[0],
                                 answers[h]['content_length']) if h in answers else 'n/a'
            for h in hosts) + ' |')
    out.append('')
    out.append('| Form | Ledger rows | Cities | `downloaded=0` | `downloaded=1` |')
    out.append('|---|---|---|---|---|')
    for form, f in result['fleet_ledger']['by_form'].items():
        out.append('| %s | %d | %d | %d | %d |' % (form, f['rows'], f['cities'], f['by_verdict'].get('0', 0),
                                                 f['by_verdict'].get('1', 0)))
    out.append('')
    out.append('| City | AF1Qip | CIHM | CIAB | Total |')
    out.append('|---|---|---|---|---|')
    for city, c in sorted(result['fleet_ledger']['per_city'].items(), key=lambda kv: (-kv[1]['total'], kv[0])):
        out.append('| %s | %d | %d | %d | %d |' % (city, c.get('AF1Qip', 0), c.get('CIHM', 0), c.get('CIAB', 0),
                                                 c['total']))
    out.append('')
    out.append('| Run start (`log.csv` field 1) | Image-eligible panos (field 5) | Image failures (field 9) | '
               'Raised (field 20) |')
    out.append('|---|---|---|---|')
    for r in result['chicago_log_tail']['runs']:
        out.append('| %s | %s | %s | %s |' % (r['start'], fmt(r['image_eligible']), fmt(r['image_failures']),
                                             fmt(r['raised'])))
    return '\n'.join(out)


# -------------------------------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    commands = {}
    for name, help_ in (('pack-list', 'a saved /adminapi/panos list -> the committed CAoS slice and totals'),
                        ('measure', 'ask the photo host once per non-AF1Qip id in the committed slice'),
                        ('probe', 'one id per form at every PROBE_HOSTS entry'),
                        ('pack-ledger', 'city,<ledger row> dump of every CAoS ledger row -> committed CSV'),
                        ('pack-depth-ledger', "chicago-il's CAoS depth ledger rows -> committed CSV"),
                        ('analyze', 'committed files -> tables (and --write the JSON)')):
        commands[name] = sub.add_parser(name, help=help_)
        commands[name].add_argument('--data-dir', default=DATA_DIR)
    commands['pack-list'].add_argument('panos_json')
    commands['pack-list'].add_argument('--city', default=MEASURED_CITY)
    commands['measure'].add_argument('--all-forms', action='store_true',
                                     help='every id, the AF1Qip form included, into %s' % MEASUREMENTS_ALL_FILE)
    commands['measure'].add_argument('--limit', type=int)
    commands['pack-ledger'].add_argument('dump')
    commands['pack-depth-ledger'].add_argument('dump')
    commands['analyze'].add_argument('--write', action='store_true')
    args = parser.parse_args(argv)

    if args.cmd == 'pack-list':
        summary = pack_list(args.panos_json, args.city, args.data_dir)
        print(json.dumps(summary, indent=1))
        return 0
    if args.cmd == 'measure':
        ids = read_csv(os.path.join(args.data_dir, IDS_FILE))
        path = os.path.join(args.data_dir, MEASUREMENTS_ALL_FILE if args.all_forms else MEASUREMENTS_FILE)
        with _GzipText(path) as f, gsv._photosphere_session() as session:
            writer = csv.DictWriter(f, fieldnames=MEASUREMENT_COLUMNS, lineterminator='\n')
            writer.writeheader()
            rows = measure(ids, session, all_forms=args.all_forms, limit=args.limit, out=writer, flush=f.flush)
        print('wrote %d rows to %s' % (len(rows), display_path(path, os.getcwd())))
        return 0
    if args.cmd == 'probe':
        ids = read_csv(os.path.join(args.data_dir, IDS_FILE))
        with gsv._photosphere_session() as session:
            rows = probe(ids, session)
        path = os.path.join(args.data_dir, PROBE_FILE)
        write_csv(path, PROBE_COLUMNS, rows)
        print('wrote %d rows to %s' % (len(rows), display_path(path, os.getcwd())))
        return 0
    if args.cmd in ('pack-ledger', 'pack-depth-ledger'):
        with open(args.dump, encoding='utf-8') as f:
            if args.cmd == 'pack-ledger':
                rows, path, columns = pack_ledger_lines(f), os.path.join(args.data_dir, LEDGER_FILE), LEDGER_COLUMNS
            else:
                rows, path, columns = (pack_depth_ledger_lines(f), os.path.join(args.data_dir, DEPTH_LEDGER_FILE),
                                       DEPTH_LEDGER_COLUMNS)
        write_csv(path, columns, rows)
        print('wrote %d rows to %s' % (len(rows), display_path(path, os.getcwd())))
        return 0
    result = analyze(args.data_dir)
    print(json.dumps({k: result[k] for k in ('chicago_list', 'chicago_join', 'chicago_log_tail',
                                             'chicago_depth_ledger', 'chicago_store_files')}, indent=1))
    print(tables(result))
    if args.write:
        path = os.path.join(args.data_dir, RESULT_FILE)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(result, f, indent=1, sort_keys=True, allow_nan=False)
            f.write('\n')
        print('wrote %s' % display_path(path, os.getcwd()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
