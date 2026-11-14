#!/usr/bin/python3
"""Find stored panoramas that are not whole JPEGs, and optionally quarantine them (#239).

For a store assembled partly by another tool (a pre-fetch that found files another downloader left, a copy
that died partway), before or after it is first scraped. The scraper trusted any `.jpg` it found on disk until
#239, and a ledgered file is never looked at again, so a truncated one already ledgered `1` is permanent: it is
the resume marker, nothing refetches it, and the cropper fails on its labels every run. The image phases now
check an unledgered file before ledgering it; this sweep is for every file, ledgered or not.

**Read-only by default.** It walks every stored pano (`common.walk_store_panos`) and judges each with
`common.stored_jpeg_verdict`: a readable header and an end-of-image marker that closes the image, and for a
file that fails that, a full decode as a second opinion. It prints what it found and writes nothing unless
asked:

* `--report CSV`: `pano_id,verdict,bytes` for each file that is not whole.
* `--ledger CSV`: makes a sweep resumable. One row per file judged (`pano_id,size,mtime_ns,verdict`), appended
  as each lands, so a run cut short by `--max-runtime` or a kill picks up where it left off; a file whose row
  matches its current size and mtime is not read again. A `--ledger` this tool did not write is refused (exit
  3): `pano_id_log.csv` is a typo away.

`--quarantine DIR` moves each file that is not whole out of the store, so the next scrape fetches it again.
It takes the nightly queue's lock (exit 3 if the queue holds it), re-judges each file under the lock, and then,
in this order:

1. **Removes every bad pano's `pano_id_log.csv` row in one rewrite** (a backup and a
   `.quarantined-<stamp>.csv` of the removed rows beside it; `readmit_dimless_writeoffs.rewrite_ledger`).
   Row first: in the other order an interrupted run leaves a `1` row with no file, which is never refetched.
   The other order's risk, a scrape between the two steps re-ledgering the file, is closed by the image
   phase's own check (#239), which refuses a file that is not whole.
2. **Moves each file** to `DIR/<id[:2]>/<id>.jpg`: copied, the copy compared byte for byte, and only then the
   original deleted - so DIR may be on another filesystem, and the bytes always exist somewhere. The pano's
   `.w8192.jpg` display copy goes with it (it was made from the bad file); its `.depth.npz` stays (it came from
   photometa, not from the image). Nothing at the destination is ever overwritten: a pano whose destination
   already exists is listed, left in place, and keeps its ledger row.

`DIR` must be outside the storage directory, or the walker would find a quarantined file again (exit 3).

Exit codes: 0 nothing found, or everything found was quarantined; 1 files that are not whole were found (read-
only), or anything was left in place; 2 usage; 3 refused (no storage directory, a foreign `--ledger`, DIR
inside the store, the queue's lock held).

Usage:
    python3 verify_store.py <storage-dir> [--max-runtime MINUTES] [--report CSV] [--ledger CSV]
                            [--quarantine DIR] [--lock PATH]
"""

import argparse
import csv
import filecmp
import os
import shutil
import sys
import time

import scrape_queue
from downloaders import common
from readmit_dimless_writeoffs import LEDGER_NAME, rewrite_ledger

LEDGER_FIELDS = ['pano_id', 'size', 'mtime_ns', 'verdict']
REPORT_FIELDS = ['pano_id', 'verdict', 'bytes']
VERDICTS = (common.STORED_JPEG_COMPLETE, common.STORED_JPEG_DECODED, common.STORED_JPEG_INCOMPLETE)

EXIT_OK, EXIT_FOUND, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


class ForeignLedgerError(Exception):
    """--ledger names a file this tool did not write."""


def _pano_id(path):
    return os.path.basename(path)[:-len('.jpg')]


def pano_path(storage_dir, pano_id):
    """The one layout every downloader writes: <storage>/<id[:2]>/<id>.jpg."""
    return os.path.join(storage_dir, pano_id[:2], pano_id + '.jpg')


def load_ledger(ledger_path):
    """{pano_id: row} from a resume ledger; {} for a missing or empty file. A row that does not parse (a torn
    last row) is skipped, so its pano is judged again. Raises ForeignLedgerError for a file with another
    header."""
    if not os.path.isfile(ledger_path) or os.path.getsize(ledger_path) == 0:
        return {}
    rows = {}
    with open(ledger_path, newline='', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != LEDGER_FIELDS:
            raise ForeignLedgerError('%s has header %r, not %r' % (ledger_path, header, LEDGER_FIELDS))
        for fields in reader:
            if len(fields) != len(LEDGER_FIELDS) or fields[3] not in VERDICTS:
                continue
            try:
                rows[fields[0]] = {'size': int(fields[1]), 'mtime_ns': int(fields[2]), 'verdict': fields[3]}
            except ValueError:
                continue
    return rows


def _open_ledger_for_append(ledger_path):
    """Append handle, writing the header to a new or empty file and closing a torn last row first."""
    fresh = not os.path.isfile(ledger_path) or os.path.getsize(ledger_path) == 0
    if not fresh:
        with open(ledger_path, 'rb') as f:
            f.seek(-1, os.SEEK_END)
            torn = f.read(1) != b'\n'
    handle = open(ledger_path, 'a', newline='', encoding='utf-8')
    writer = csv.writer(handle)
    if fresh:
        writer.writerow(LEDGER_FIELDS)
    elif torn:
        handle.write('\n')
    return handle, writer


def sweep(storage_dir, ledger_path=None, max_runtime_minutes=None):
    """Judge every stored pano. Returns (bad, counts, unreached): bad is {pano_id: (verdict, bytes)} for the
    files that are not whole (this run's and, with a ledger, earlier runs' still-current ones)."""
    start = time.monotonic()
    prior = load_ledger(ledger_path) if ledger_path else {}
    counts = dict.fromkeys(VERDICTS + ('current',), 0)
    bad = {}
    unreached = 0
    handle = writer = None
    if ledger_path:
        handle, writer = _open_ledger_for_append(ledger_path)
    try:
        for path in common.walk_store_panos(storage_dir):
            pano_id = _pano_id(path)
            try:
                stat = os.stat(path)
            except OSError:
                continue        # vanished between the walk and the stat
            row = prior.get(pano_id)
            if row is not None and row['size'] == stat.st_size and row['mtime_ns'] == stat.st_mtime_ns:
                counts['current'] += 1
                if row['verdict'] == common.STORED_JPEG_INCOMPLETE:
                    bad[pano_id] = (row['verdict'], stat.st_size)
                continue
            if max_runtime_minutes is not None and (time.monotonic() - start) / 60.0 >= max_runtime_minutes:
                unreached += 1
                continue
            verdict = common.stored_jpeg_verdict(path)
            counts[verdict] += 1
            if verdict == common.STORED_JPEG_INCOMPLETE:
                bad[pano_id] = (verdict, stat.st_size)
            if writer is not None:
                writer.writerow([pano_id, stat.st_size, stat.st_mtime_ns, verdict])
                handle.flush()
    finally:
        if handle is not None:
            handle.close()
    return bad, counts, unreached


def write_report(report_path, bad):
    with common.atomic_output_path(report_path) as tmp:
        with open(tmp, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(REPORT_FIELDS)
            for pano_id in sorted(bad):
                writer.writerow([pano_id, bad[pano_id][0], bad[pano_id][1]])


def _inside(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False        # different drives on Windows


def _move_verified(source, destination):
    """Copy, compare byte for byte, then delete the source. Never overwrites `destination`."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    part = destination + '.part'
    shutil.copy2(source, part)
    if not filecmp.cmp(source, part, shallow=False):
        os.remove(part)
        raise OSError('copy of %s did not match; source left in place' % source)
    if os.path.exists(destination):
        os.remove(part)
        raise FileExistsError(destination)
    os.replace(part, destination)
    os.remove(source)


def quarantine(storage_dir, bad_ids, quarantine_dir):
    """Rows first, then files. Run under the queue lock. Returns (moved, left): left is {pano_id: reason}."""
    left = {}
    targets = []
    for pano_id in sorted(bad_ids):
        source = pano_path(storage_dir, pano_id)
        if not os.path.isfile(source):
            left[pano_id] = 'gone since the sweep'
            continue
        if common.stored_jpeg_verdict(source) != common.STORED_JPEG_INCOMPLETE:
            left[pano_id] = 'whole when re-checked under the lock'
            continue
        if os.path.exists(pano_path(quarantine_dir, pano_id)):
            left[pano_id] = 'destination exists'
            continue
        targets.append(pano_id)

    ledger = os.path.join(storage_dir, LEDGER_NAME)
    if targets and os.path.isfile(ledger):
        with open(ledger, newline='') as f:
            lines = f.readlines()
        target_set = set(targets)
        kept = [line for line in lines if line.split(',', 1)[0].strip() not in target_set]
        removed = [line for line in lines if line.split(',', 1)[0].strip() in target_set]
        if removed:
            rewrite_ledger(ledger, lines, kept, removed, removed_name='quarantined')

    moved = []
    for pano_id in targets:
        source = pano_path(storage_dir, pano_id)
        try:
            _move_verified(source, pano_path(quarantine_dir, pano_id))
        except OSError as e:
            # Its row is gone and the file is still here: the next scrape finds it unledgered, refuses it as
            # not whole, and reports it (#239) - never ledgers it unread.
            left[pano_id] = 'not moved: %s' % (e,)
            continue
        moved.append(pano_id)
        sidecar = common.downscaled_sidecar_path(source)
        if os.path.isfile(sidecar):
            try:
                _move_verified(sidecar, common.downscaled_sidecar_path(pano_path(quarantine_dir, pano_id)))
            except OSError as e:
                print('WARNING: %s: display copy %s not moved (%s); delete it by hand' % (pano_id, sidecar, e))
    return moved, left


def _minutes(value):
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('%r is not a number of minutes' % (value,))
    if not minutes > 0:
        raise argparse.ArgumentTypeError('--max-runtime must be positive')
    return minutes


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('storage_dir', help="One city's storage directory (where pano_id_log.csv lives).")
    parser.add_argument('--max-runtime', type=_minutes, default=None, metavar='MINUTES',
                        help='Stop judging new files after this long; the rest are counted unreached.')
    parser.add_argument('--report', default=None, metavar='CSV', help='Write the files that are not whole here.')
    parser.add_argument('--ledger', default=None, metavar='CSV', help='Resume ledger; makes the sweep resumable.')
    parser.add_argument('--quarantine', default=None, metavar='DIR',
                        help='Move files that are not whole here (outside the store), removing their ledger rows.')
    parser.add_argument('--lock', default=None, metavar='PATH',
                        help="The nightly queue's lock file (default: scrape_queue's default path).")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.storage_dir):
        print('REFUSED: %s is not a directory' % args.storage_dir)
        return EXIT_REFUSED
    if args.quarantine is not None and _inside(args.quarantine, args.storage_dir):
        print('REFUSED: --quarantine %s is inside the store; the walker would find its files again'
              % args.quarantine)
        return EXIT_REFUSED
    common.raise_decompression_bomb_ceiling()   # a large pano must not fail the decode for its size

    try:
        bad, counts, unreached = sweep(args.storage_dir, args.ledger, args.max_runtime)
    except ForeignLedgerError as e:
        print('REFUSED: %s; nothing written' % (e,))
        return EXIT_REFUSED
    print('%s: %d whole, %d whole only on a full decode, %d NOT whole, %d already judged (ledger), %d unreached'
          % (args.storage_dir, counts[common.STORED_JPEG_COMPLETE], counts[common.STORED_JPEG_DECODED],
             counts[common.STORED_JPEG_INCOMPLETE], counts['current'], unreached))
    for pano_id in sorted(bad)[:10]:
        print('  not whole: %s (%d bytes)' % (pano_path(args.storage_dir, pano_id), bad[pano_id][1]))
    if len(bad) > 10:
        print('  ... and %d more' % (len(bad) - 10))
    if args.report:
        write_report(args.report, bad)

    if not bad:
        return EXIT_OK
    if args.quarantine is None:
        print('Read-only: nothing moved. Pass --quarantine DIR to move them out of the store.')
        return EXIT_FOUND

    lock_path = args.lock if args.lock is not None else scrape_queue.default_lock_path()
    try:
        with scrape_queue.exclusive_lock(lock_path):
            moved, left = quarantine(args.storage_dir, bad, args.quarantine)
    except scrape_queue.QueueLocked as e:
        print('REFUSED: the nightly queue holds its lock (%s); nothing moved. Run outside the window.' % (e,))
        return EXIT_REFUSED
    print('Quarantined %d file(s) to %s; their ledger rows were removed, so the next run fetches them again.'
          % (len(moved), args.quarantine))
    for pano_id in sorted(left):
        print('  left in place: %s (%s)' % (pano_id, left[pano_id]))
    return EXIT_FOUND if left else EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
