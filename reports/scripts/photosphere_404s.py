"""Why AF1Qip photospheres re-admitted on 2026-11-05 still raise every night: their 404s, and their "truncated"
bodies (#223 follow-up).

After the 901 AF1Qip write-offs were re-admitted, 20 ids stayed unledgered across the fleet and raised every
night: seattle-wa 14, chicago-il 1, zurich 5. Seattle's were its only work, so from 2026-11-07 they failed the
whole night through `images-no-success`. Two questions decide what the code should do with them:

1. **Is a 404 from the photo host a verdict?** (Is it stable per id, and does it carry one error page?)
2. **Is a 200 the old check called "not a complete JPEG" really cut short?** (Or is it a complete image with
   bytes after its EOI, as camera originals often have?)

`measure` asks each id once, through the downloader's own `_photosphere_session()`, `_random_header()` and
`PHOTOSPHERE_URL`, so the request is the nightly one, and reads the WHOLE body (unlike the 2026-11-05 study's
headers-only sweep: the body is the question). One CSV row per id per round; the body itself is not kept, only
its length, a hash prefix, where its last FF D9 sits, the first 32 bytes after it and whether PIL decodes it.
Run three times in a day. `pack-history` packs what the production box's scrape.log and pano_id_log.csv said
about every photosphere that raised there (the box-side commands are below). `analyze` reads only the committed
files under reports/data/ and derives every number in reports/2026-11-09-photosphere-404s-and-trailers.md;
tests/test_photosphere_404s_study.py asserts that. Production code never imports this module.

Usage, from the repo root::

    # On the scraper box (read-only), for each city: one line per failing id per log line, then its ledger row:
    #   grep -h 'photo host answered' <city>/scrape.log* | sed -E '...'   > history.txt   (city id kind form xN)
    #   grep -m1 "^<id>," <city>/pano_id_log.csv                          > status.txt    (city id field2,stamp)
    python reports/scripts/photosphere_404s.py pack-history history.txt status.txt
    python reports/scripts/photosphere_404s.py measure --round r1   # asks every unledgered id once
    python reports/scripts/photosphere_404s.py analyze --write

Every subcommand takes `--data-dir` (default reports/data/).
"""

import argparse
import collections
import csv
import datetime
import hashlib
import io
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from downloaders import common, gsv  # noqa: E402

DATA_DIR = os.path.join(REPO_ROOT, 'reports', 'data')
PREFIX = '2026-11-09-photosphere-404s'
MEASUREMENTS = PREFIX + '-measurements.csv'
HISTORY = PREFIX + '-box-history.csv'
RESULT = PREFIX + '.json'

MEASURE_COLUMNS = ['round', 'at', 'city', 'pano_id', 'form', 'status', 'content_type', 'content_length',
                   'body_len', 'sha256_16', 'last_eoi', 'bytes_after_last_eoi', 'after_eoi_head', 'decodes']
HISTORY_COLUMNS = ['city', 'pano_id', 'failure', 'form', 'times_logged', 'ledger_now']
SAMSUNG_MARKER = b'Image_UTC_Data'
PAUSE_SECONDS = 1.5


def describe_response(status, content_type, content_length, body):
    """The measured fields of one answer. A 200's body is described, never kept: the last FF D9, how many bytes
    follow it, the first 32 of those as hex, and whether PIL decodes the whole image."""
    row = {'status': status, 'content_type': (content_type or '').split(';')[0].strip().lower(),
           'content_length': '' if content_length is None else str(content_length), 'body_len': len(body),
           'sha256_16': hashlib.sha256(body).hexdigest()[:16], 'last_eoi': '', 'bytes_after_last_eoi': '',
           'after_eoi_head': '', 'decodes': ''}
    if status != 200:
        return row
    eoi = body.rfind(b'\xff\xd9')
    if eoi >= 0:
        row.update(last_eoi=eoi, bytes_after_last_eoi=len(body) - eoi - 2, after_eoi_head=body[eoi + 2:eoi + 34].hex())
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(body))
        image.load()
        row['decodes'] = 'yes'
    except Exception:            # a measurement row, not a crash: any failure to decode is the answer
        row['decodes'] = 'no'
    return row


def measure(ids, round_label, out_path, session_factory=None, pause=PAUSE_SECONDS, now=None):
    """Ask each (city, pano_id) once; append one row per id to `out_path` (header written if new). A raise
    is a row with status 'raise', not a stop."""
    session_factory = session_factory or gsv._photosphere_session
    now = now or (lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'))
    new = not os.path.exists(out_path)
    with open(out_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, MEASURE_COLUMNS)
        if new:
            writer.writeheader()
        for i, (city, pano_id) in enumerate(ids):
            photo_id = gsv.photosphere_photo_id(pano_id)
            row = {'round': round_label, 'at': now(), 'city': city, 'pano_id': pano_id,
                   'form': gsv.photosphere_form(photo_id) if photo_id else ''}
            if photo_id is None:
                row.update(describe_response('undecodable', None, None, b''))
            else:
                try:
                    with session_factory() as session:
                        r = session.get(gsv.PHOTOSPHERE_URL % (photo_id,), headers=gsv._random_header())
                        row.update(describe_response(r.status_code, r.headers.get('Content-Type'),
                                                     r.headers.get('Content-Length'), r.content))
                except Exception as e:
                    row.update(describe_response('raise', type(e).__name__, None, b''))
            writer.writerow(row)
            f.flush()
            if pause and i + 1 < len(ids):
                time.sleep(pause)


def pack_history(history_lines, status_lines):
    """Join the box's per-id failure lines (`city pano_id kind form xN`, kind S<status> or TRUNC<bytes>) with
    each id's ledger row (`city pano_id field2,stamp ...`, or `none`)."""
    ledger = {}
    for line in status_lines:
        parts = line.split()
        if len(parts) >= 3:
            ledger[(parts[0], parts[1])] = 'none' if parts[2] == 'none' else parts[2].split(',')[0]
    rows = []
    for line in history_lines:
        parts = line.split()
        if not parts:
            continue
        city, pano_id, kind = parts[:3]
        form = parts[3] if len(parts) == 5 else ''
        times = int(parts[-1].lstrip('x'))
        failure = 'truncated-200' if kind.startswith('TRUNC') else 'status-' + kind.lstrip('S')
        rows.append({'city': city, 'pano_id': pano_id, 'failure': failure,
                     'form': form or gsv.photosphere_form(gsv.photosphere_photo_id(pano_id) or ''),
                     'times_logged': times, 'ledger_now': ledger.get((city, pano_id), 'none')})
    return sorted(rows, key=lambda r: (r['city'], r['pano_id'], r['failure']))


def _read(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def analyze(data_dir=DATA_DIR):
    rows = _read(os.path.join(data_dir, MEASUREMENTS))
    history = _read(os.path.join(data_dir, HISTORY))
    rounds = sorted({r['round'] for r in rows})
    by_id = collections.defaultdict(dict)
    for r in rows:
        by_id[r['pano_id']][r['round']] = r

    def pattern(answers):
        return ' / '.join(answers[k]['status'] for k in rounds if k in answers)

    patterns = collections.Counter(pattern(a) for a in by_id.values())
    not_found = [r for r in rows if r['status'] == '404']
    shapes = collections.Counter('%s %s' % (r['content_type'], r['body_len']) for r in not_found)
    ids_404_every_round = [p for p, a in by_id.items() if all(a.get(k, {}).get('status') == '404' for k in rounds)]
    ids_404_two_shapes = [p for p in ids_404_every_round
                          if len({(by_id[p][k]['content_type'], by_id[p][k]['body_len']) for k in rounds}) > 1]
    served_then_404 = [p for p, a in by_id.items()
                       if any(x['status'] == '200' for x in a.values())
                       and any(x['status'] == '404' for x in a.values())]

    oks = [r for r in rows if r['status'] == '200']
    trailer_ids = sorted({r['pano_id'] for r in oks})
    trailer_by_id = {}
    for p in trailer_ids:
        sizes = {int(r['bytes_after_last_eoi']) for r in oks if r['pano_id'] == p}
        heads = {r['after_eoi_head'] for r in oks if r['pano_id'] == p}
        hashes = {r['sha256_16'] for r in oks if r['pano_id'] == p}
        trailer_by_id[p] = {'sizes': sorted(sizes), 'stable': len(sizes) == len(heads) == len(hashes) == 1,
                            'samsung': any(SAMSUNG_MARKER in bytes.fromhex(h) for h in heads)}
    trailer_sizes = collections.Counter(str(v['sizes'][0]) for v in trailer_by_id.values() if len(v['sizes']) == 1)

    hist_af1qip = [h for h in history if h['form'] == 'AF1Qip']
    hist_ids = {(h['city'], h['pano_id']) for h in hist_af1qip}
    hist_404_twice_then_served = sorted({(h['city'], h['pano_id']) for h in hist_af1qip
                                         if h['failure'] == 'status-404' and int(h['times_logged']) >= 2
                                         and h['ledger_now'] == '1'})
    hist_unledgered = sorted({(h['city'], h['pano_id']) for h in hist_af1qip if h['ledger_now'] == 'none'})

    return {
        'rounds': rounds,
        'round_times': {k: min(r['at'] for r in rows if r['round'] == k) for k in rounds},
        'ids_measured': len(by_id),
        'ids_by_city': dict(sorted(collections.Counter(a[rounds[0]]['city'] for a in by_id.values()).items())),
        'responses': len(rows),
        'status_patterns': dict(sorted(patterns.items())),
        'not_found_responses': len(not_found),
        'not_found_shapes': dict(sorted(shapes.items())),
        'ids_404_every_round': len(ids_404_every_round),
        'ids_404_both_shapes': len(ids_404_two_shapes),
        'ids_served_then_404': len(served_then_404),
        'ok_responses': len(oks),
        'ok_length_matches_content_length': sum(r['content_length'] == r['body_len'] for r in oks),
        'ok_decode': sum(r['decodes'] == 'yes' for r in oks),
        'ok_ends_with_eoi': sum(r['bytes_after_last_eoi'] == '0' for r in oks),
        'ok_within_trailer_window': sum(0 < int(r['bytes_after_last_eoi']) <= common.TRAILING_BYTES_WINDOW - 2
                                        for r in oks),
        'trailer_ids': len(trailer_ids),
        'trailer_ids_stable': sum(v['stable'] for v in trailer_by_id.values()),
        'trailer_ids_samsung': sum(v['samsung'] for v in trailer_by_id.values()),
        'trailer_sizes': dict(sorted(trailer_sizes.items(), key=lambda kv: int(kv[0]))),
        'history_af1qip_ids': len(hist_ids),
        'history_af1qip_404_twice_then_served': len(hist_404_twice_then_served),
        'history_af1qip_unledgered': len(hist_unledgered),
        # Per id, not per log line: one id can have logged both a 404 and a "truncated" 200.
        'history_af1qip_unledgered_by_failures': dict(sorted(collections.Counter(
            ' + '.join(sorted({h['failure'] for h in hist_af1qip if (h['city'], h['pano_id']) == key}))
            for key in hist_unledgered).items())),
    }


def tables(result):
    """The report's tables, line for line (the test asserts each line is in the report)."""
    lines = ['| Answer across %s | Ids |' % ', '.join(result['rounds']), '|---|---|']
    lines += ['| %s | %d |' % (k, v) for k, v in result['status_patterns'].items()]
    lines += ['', '| 404 body (Content-Type, bytes) | Responses |', '|---|---|']
    lines += ['| %s | %d |' % (k, v) for k, v in result['not_found_shapes'].items()]
    lines += ['', '| Bytes after the last EOI | Ids |', '|---|---|']
    lines += ['| %s | %d |' % (k, v) for k, v in result['trailer_sizes'].items()]
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    commands = {name: sub.add_parser(name) for name in ('pack-history', 'measure', 'analyze')}
    for p in commands.values():
        p.add_argument('--data-dir', default=DATA_DIR)
    commands['pack-history'].add_argument('history')
    commands['pack-history'].add_argument('status')
    commands['measure'].add_argument('--round', required=True)
    commands['analyze'].add_argument('--write', action='store_true')
    args = parser.parse_args(argv)
    common.raise_decompression_bomb_ceiling()      # a zurich photosphere is 14400x7200 = 103.7 MP

    if args.command == 'pack-history':
        with open(args.history, encoding='utf-8') as h, open(args.status, encoding='utf-8') as s:
            rows = pack_history(h.read().splitlines(), s.read().splitlines())
        with open(os.path.join(args.data_dir, HISTORY), 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, HISTORY_COLUMNS, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
        print('%d rows' % len(rows))
    elif args.command == 'measure':
        history = _read(os.path.join(args.data_dir, HISTORY))
        ids = sorted({(h['city'], h['pano_id']) for h in history
                      if h['form'] == 'AF1Qip' and h['ledger_now'] == 'none'})
        measure(ids, args.round, os.path.join(args.data_dir, MEASUREMENTS))
    else:
        result = analyze(args.data_dir)
        print('\n'.join(tables(result)))
        print(json.dumps(result, indent=2))
        if args.write:
            with open(os.path.join(args.data_dir, RESULT), 'w', encoding='utf-8', newline='\n') as f:
                json.dump(result, f, indent=2, allow_nan=False)
                f.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
