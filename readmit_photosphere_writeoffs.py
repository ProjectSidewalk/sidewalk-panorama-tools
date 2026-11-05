"""Re-admit the user photospheres the fleet wrote off before #227 could fetch them (#223).

Every `CAoS` row in every city's pano_id_log.csv on the production store was a `downloaded=0` when the
2026-11-05 study read them (reports/2026-11-05-photosphere-forms.md): 1,752 rows in 25 cities, all two-field,
so all written before the `fetched_at` column reached the store on 2026-09-17, under whatever rule then
governed a failed `CAoS` fetch. 901 of them decode to the AF1Qip form of photo id, the form the photo host
serves (24 of 24 in #223, and every AF1Qip id in chicago-il in the study), and a ledgered pano is never
attempted again - so #227 downloads none of them until these rows go. The CIHM and CIAB forms stay: the host
refuses those forms (628 of 628), and since #223's follow-up a 400 for one of them is exactly the `0` row these
already are.

A row is re-admitted only when ALL of these hold:
  * it is a TWO-field `0` row - the shape every one of the 1,752 measured rows has, and the shape no code since
    2026-09-17 can write. A three-field `0` is a verdict the current code wrote with a stamp (since #223's
    follow-up, exactly what a CIHM or CIAB 400 writes), and this tool never eats one;
  * its pano id decodes to a photosphere photo id (gsv.photosphere_photo_id), and
  * that photo id's form (gsv.photosphere_form) is one named by --form (default: AF1Qip, the served form).
Every other line - the header, `1` rows, three-field rows, other forms, non-photosphere ids, torn rows, a line
csv cannot parse - is kept byte for byte. No pano list is read: the decision is a property of the id itself.

Dry run by default: it reads each ledger and reports what it would remove. With --apply it
  1. takes the nightly queue's lock (scrape_queue.exclusive_lock), so it can never race a queue run appending
     to the same ledger - exit 3 if the queue holds it - and re-reads each ledger under it;
  2. copies the ledger to pano_id_log.csv.bak-<stamp>, and the removed rows to
     pano_id_log.csv.readmitted-<stamp>.csv, so the change can be undone and audited;
  3. writes the kept lines to a temp file beside the ledger and os.replace()s it in.
It never reads or writes imagery. A re-admitted pano whose .jpg is already on the store (chicago-il has five,
left by #223's development measurement) costs nothing: every downloader short-circuits on the file and
re-registers it as skipped.

Exit codes: 0 done (including "nothing to re-admit"), 2 usage, 3 refused (a storage dir with no ledger, or the
queue's lock is held). Several storage dirs may be given; the lock is taken once for all of them.

Run it on the production box from the checkout's venv, AS THE CRON USER (never through sudo: a root-created
/tmp/sidewalk-scrape-queue.lock would stop the next night's queue from opening it), outside the nightly window,
once the #223 follow-up build is deployed (an earlier build raises on every re-admitted CIHM/CIAB id rather than
ledgering it, and fetches the AF1Qip ones either way), after checking that the store's sshfs mount does not carry
-o workaround=rename (os.replace is atomic over sshfs only through OpenSSH's posix-rename extension)::

    mount | grep sshfs
    .venv/bin/python readmit_photosphere_writeoffs.py /mnt/panostore/chicago-il /mnt/panostore/knox-oh ...  # dry run
    .venv/bin/python readmit_photosphere_writeoffs.py ... --apply
"""

import argparse
import csv
import io
import os
import sys

import scrape_queue
from downloaders import gsv
from readmit_dimless_writeoffs import LEDGER_NAME, rewrite_ledger


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('storage_dirs', nargs='+', metavar='storage_dir',
                        help="A city's storage root, holding its pano_id_log.csv. Several may be given.")
    parser.add_argument('--form', dest='forms', action='append', choices=gsv.PHOTOSPHERE_FORMS,
                        help='A photo-id form whose downloaded=0 rows are re-admitted. Repeatable. Default: AF1Qip.')
    parser.add_argument('--apply', action='store_true',
                        help='Rewrite the ledgers. Without it, nothing is written (dry run).')
    parser.add_argument('--lock', default=None,
                        help="The nightly queue's lock file (default: scrape_queue's default path).")
    return parser


def is_writeoff_of(line, forms):
    """Whether one ledger line is a two-field `0` row for a photosphere whose photo id is of a form in `forms`.

    A line csv cannot parse (a NUL byte, which Python 3.10's reader refuses outright) is not a write-off: it is
    kept, as progress_check keeps re-attempting the pano behind it, rather than crashing a pass over 25 cities.
    """
    try:
        rows = list(csv.reader(io.StringIO(line)))
    except csv.Error:
        return False
    row = rows[0] if len(rows) == 1 else None
    if row is None or len(row) != 2 or row[1] != '0':
        return False
    photo_id = gsv.photosphere_photo_id(row[0])
    return photo_id is not None and gsv.photosphere_form(photo_id) in forms


def partition_ledger(lines, forms):
    """(kept lines, removed lines) - every line verbatim, newline included; see the module docstring."""
    kept, removed = [], []
    for line in lines:
        (removed if is_writeoff_of(line, forms) else kept).append(line)
    return kept, removed


def _read(ledger):
    with open(ledger, newline='') as f:
        return f.readlines()


def main(argv=None):
    args = build_parser().parse_args(argv)
    forms = tuple(args.forms or ('AF1Qip',))
    ledgers = [os.path.join(d, LEDGER_NAME) for d in args.storage_dirs]
    missing = [ledger for ledger in ledgers if not os.path.isfile(ledger)]
    if missing:
        print('REFUSED: no %s in %s' % (LEDGER_NAME, ', '.join(os.path.dirname(m) for m in missing)))
        return 3

    total = 0
    for ledger in ledgers:
        lines = _read(ledger)
        kept, removed = partition_ledger(lines, forms)
        total += len(removed)
        print('%s: %d ledger line(s); downloaded=0 rows for photospheres of the %s form(s): %d'
              % (ledger, len(lines), '/'.join(forms), len(removed)))
    if not args.apply:
        print('Dry run: would re-admit %d row(s). Nothing written; pass --apply to rewrite the ledger(s).' % total)
        return 0

    lock_path = args.lock if args.lock is not None else scrape_queue.default_lock_path()
    total = 0
    try:
        with scrape_queue.exclusive_lock(lock_path):
            for ledger in ledgers:
                # Re-read under the lock: a run may have appended since the dry-run read above.
                lines = _read(ledger)
                kept, removed = partition_ledger(lines, forms)
                if removed:
                    rewrite_ledger(ledger, lines, kept, removed)
                total += len(removed)
                # One line per ledger as it lands, so a failure over sshfs half way says where it stopped;
                # the .bak-<stamp> beside each rewritten ledger is the way back.
                print('%s: re-admitted %d row(s)' % (ledger, len(removed)))
    except scrape_queue.QueueLocked as e:
        print('REFUSED: the nightly queue holds its lock (%s); nothing written. Run outside the window.' % (e,))
        return 3
    print('Done: re-admitted %d row(s); they are attempted on the next run.' % total)
    return 0


if __name__ == '__main__':
    sys.exit(main())
