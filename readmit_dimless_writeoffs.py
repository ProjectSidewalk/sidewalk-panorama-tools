"""Re-admit the GSV panos the downloader wrote off for missing dimensions before #184.

Until #184, a GSV record that /adminapi/panos served without width/height was ledgered `downloaded=0` in
pano_id_log.csv before any request to Google, and a ledgered pano is never attempted again. washington-dc
lost 1,349 panos that way on 2026-09-24/25 (1,348 labelled). Since #184 such a pano asks photometa for its
frame, so removing those rows lets the next run download whatever Google still serves - and write the
permanent verdict again, on the usual two black probe tiles, for whatever it does not.

The ledger has no reason column, so a row is re-admitted only when ALL of these hold:
  * it is a three-field row whose verdict is `0`;
  * its `fetched_at` falls on one of the --date days (the incident's nights; no default);
  * its pano is a GSV record WITHOUT width/height in the pano list as served now (--host, or -c CSV).
Every other line - the header, `1` rows, two-field rows, other dates, retired panos the list has dims for,
other sources, torn rows - is kept byte for byte.

Dry run by default: it reads the ledger and the list and reports what it would remove. With --apply it
  1. takes the nightly queue's lock (scrape_queue.exclusive_lock), so it can never race a run appending to
     the same ledger - exit 3 if the queue holds it;
  2. copies the ledger to pano_id_log.csv.bak-<stamp>, and the removed rows to
     pano_id_log.csv.readmitted-<stamp>.csv, so the change can be undone and audited;
  3. writes the kept lines to a temp file beside the ledger and os.replace()s it in.
It never reads or writes imagery. A re-admitted pano whose .jpg is already on the store costs nothing: every
downloader short-circuits on the file and re-registers it as skipped.

Exit codes: 0 done (including "nothing to re-admit"), 2 usage, 3 refused (no ledger, or the queue's lock is
held).

Example (on the production box, outside the nightly window)::

    python3 readmit_dimless_writeoffs.py /mnt/panostore/washington-dc --host sidewalk-dc.cs.washington.edu \\
        --date 2026-09-24 --date 2026-09-25            # dry run: prints the count, expect 1349
    python3 readmit_dimless_writeoffs.py ... --apply
"""

import argparse
import csv
import io
import os
import re
import sys
import tempfile
from datetime import datetime

import DownloadRunner
import scrape_queue

LEDGER_NAME = 'pano_id_log.csv'
_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _day(value):
    """argparse type: a YYYY-MM-DD day, nothing longer - the match below is a prefix match on fetched_at."""
    if not _DAY.match(value):
        raise argparse.ArgumentTypeError('%r is not a YYYY-MM-DD day' % (value,))
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return value


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('storage_dir', help="One city's storage root, holding its pano_id_log.csv.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--host', help='The city deployment whose /adminapi/panos is read for the current list.')
    source.add_argument('-c', '--csv', dest='csv_path', help='A pano-list CSV instead of --host (the -c shape).')
    parser.add_argument('--date', dest='dates', action='append', type=_day, required=True,
                        help='A day (YYYY-MM-DD) whose downloaded=0 rows are candidates. Repeat for several.')
    parser.add_argument('--apply', action='store_true',
                        help='Rewrite the ledger. Without it, nothing is written (dry run).')
    parser.add_argument('--lock', default=None,
                        help="The nightly queue's lock file (default: scrape_queue's default path).")
    return parser


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def dimensionless_gsv_ids(pano_infos):
    """Ids of GSV records with no width or no height - the key absent or the value blank."""
    return {p['pano_id'] for p in pano_infos
            if p.get('source') == 'gsv' and (_blank(p.get('width')) or _blank(p.get('height')))}


def partition_ledger(lines, dimless_ids, dates):
    """(kept lines, removed lines) - every line verbatim, newline included; see the module docstring."""
    kept, removed = [], []
    for line in lines:
        rows = list(csv.reader(io.StringIO(line)))
        row = rows[0] if len(rows) == 1 else None
        if (row is not None and len(row) == 3 and row[1] == '0' and row[2][:10] in dates
                and row[0] in dimless_ids):
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def main(argv=None):
    args = build_parser().parse_args(argv)
    ledger = os.path.join(args.storage_dir, LEDGER_NAME)
    if not os.path.isfile(ledger):
        print('REFUSED: no %s in %s' % (LEDGER_NAME, args.storage_dir))
        return 3
    if args.csv_path is not None:
        pano_infos = DownloadRunner.fetch_pano_ids_csv(args.csv_path)
    else:
        pano_infos = DownloadRunner.fetch_pano_ids_from_webserver(args.host)
    dimless = dimensionless_gsv_ids(pano_infos)
    dates = set(args.dates)

    with open(ledger, newline='') as f:
        lines = f.readlines()
    kept, removed = partition_ledger(lines, dimless, dates)
    print('%s: %d ledger line(s); %d GSV record(s) in the pano list carry no width/height; downloaded=0 rows '
          'on %s that are among them: %d' % (ledger, len(lines), len(dimless), ', '.join(sorted(dates)),
                                             len(removed)))
    if not args.apply:
        print('Dry run: would re-admit %d row(s). Nothing written; pass --apply to rewrite the ledger.'
              % len(removed))
        return 0

    lock_path = args.lock if args.lock is not None else scrape_queue.default_lock_path()
    try:
        with scrape_queue.exclusive_lock(lock_path):
            # Re-read under the lock: a run may have appended since the dry-run read above.
            with open(ledger, newline='') as f:
                lines = f.readlines()
            kept, removed = partition_ledger(lines, dimless, dates)
            if removed:
                _rewrite(ledger, lines, kept, removed)
    except scrape_queue.QueueLocked as e:
        print('REFUSED: the nightly queue holds its lock (%s); nothing written. Run outside the window.' % (e,))
        return 3
    print('Done: re-admitted %d row(s); they are attempted on the next run.' % len(removed))
    return 0


def _rewrite(ledger, lines, kept, removed):
    """Backup, removed-rows file, then an atomic replace of the ledger. Never in place."""
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    with open('%s.bak-%s' % (ledger, stamp), 'w', newline='') as f:
        f.writelines(lines)
    with open('%s.readmitted-%s.csv' % (ledger, stamp), 'w', newline='') as f:
        f.writelines(removed)
    directory = os.path.dirname(os.path.abspath(ledger))
    fd, tmp = tempfile.mkstemp(prefix='.pano_id_log.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', newline='') as f:
            f.writelines(kept)
        try:
            os.chmod(tmp, os.stat(ledger).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, ledger)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


if __name__ == '__main__':
    sys.exit(main())
