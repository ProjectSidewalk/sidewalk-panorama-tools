# !/usr/bin/python3
"""Find every stored panorama with an exactly-black band along its bottom or right edge (#179).

Detection only. **It never writes, moves or deletes a panorama** - the store is an archive (CLAUDE.md, "Storage
layout"). It reads each pano and writes two files of its own:

* the **ledger** (default `<storage_path>/black_bands.csv`): one row per pano scanned, with the bottom and right
  band depths as fractions of the frame, whatever they are - so it is also the calibration measurement for
  `downloaders.common.EDGE_BAND_MAX_FRACTION` (#179 item 4). Rows are appended as each verdict lands, so a run
  that is killed keeps what it did.
* the **work-list** (default `<storage_path>/black_band_worklist.csv`): the panos with a band deeper than
  `--min-band` (default `EDGE_BAND_MAX_FRACTION`), rewritten from the whole ledger at the end of every run.
  Its `pano_id,width,height` columns are what `refetch_panos.py --worklist` reads (the stored frame, so the
  refetch pass sees no `dims_changed`), and the same ids name the crops to delete and re-cut, because an
  existing crop is the cropper's resume marker and is never re-cut on its own.

Why a pano-level check at all: the cropper's window check (`black_content`, #164) withholds a label inside a
bottom band only when the band is deeper than a sixth of the pano, and cannot see the D4 shape (#156) outside
its bands at all. Every crop cut from such a pano inherits the problem, so the pano is where it is decided.

Idempotent and resumable: a pano whose ledger row matches its current size and mtime is not decoded again, so
a re-run over a finished store costs one `stat` per pano, and a run cut short by `--max-runtime` picks up where
it left off. A pano replaced since (a refetch swap) is scanned afresh. A pano that is not a readable JPEG is
reported FAILED and left unledgered, so it is tried again next run; the bytes are left for a human.

The work per pano is a luma-only JPEG decode at 1/8 scale (Pillow's draft mode), not a full decode. Run it on
the host that owns the store rather than over sshfs, for downscale_panos.py's reason: the read is the slow part.

Usage:
    python3 scan_black_bands.py <storage_path> [--max-runtime MINUTES] [--min-band FRACTION]
                                [--ledger CSV] [--worklist CSV]
"""

import argparse
import csv
import os
import time
from collections import namedtuple

from downloaders import common
from downloaders.common import atomic_output_path, jpeg_dimensions, walk_store_panos

LEDGER_FILENAME = 'black_bands.csv'
WORKLIST_FILENAME = 'black_band_worklist.csv'
LEDGER_FIELDS = ['pano_id', 'size', 'mtime_ns', 'width', 'height', 'bottom_band', 'right_band']
WORKLIST_FIELDS = ['pano_id', 'width', 'height', 'bottom_band', 'right_band', 'side']

# 'clean' and 'banded' are panos decoded THIS run, split at --min-band; 'current' is a pano whose ledger row
# still matches the file, so it was not decoded; 'failed' is not a readable JPEG; 'unreached' is a pano the
# runtime budget left unexamined. Every scanned pano is in exactly one of clean/banded/current/failed;
# unreached were not scanned.
Summary = namedtuple('Summary', ['scanned', 'clean', 'banded', 'current', 'failed', 'unreached'])


def _pano_id(path):
    return os.path.basename(path)[:-len('.jpg')]


def load_ledger(ledger_path):
    """{pano_id: row} from the ledger, the LAST row per pano winning (a re-scan appends). {} when absent.

    Read with `csv`, never pandas: a pano id must never take its type from what the ids look like (#46, #72).
    """
    if not os.path.isfile(ledger_path):
        return {}
    with open(ledger_path, newline='', encoding='utf8') as f:
        return {row['pano_id']: row for row in csv.DictReader(f) if row.get('pano_id')}


def _is_current(row, stat):
    return row is not None and row.get('size') == str(stat.st_size) and row.get('mtime_ns') == str(stat.st_mtime_ns)


def _fmt(fraction):
    return '%.4f' % fraction


def write_worklist(worklist_path, ledger, min_band):
    """Rewrite the work-list from the whole ledger: every pano with a band deeper than `min_band`, sorted by id.

    Through atomic_output_path, so a reader never sees half a file. Returns the number of rows written.
    """
    rows = []
    for pano_id in sorted(ledger):
        row = ledger[pano_id]
        bands = common.EdgeBands(float(row['bottom_band']), float(row['right_band']))
        sides = common.deep_edge_bands(bands, min_band)
        if sides:
            rows.append({'pano_id': pano_id, 'width': row['width'], 'height': row['height'],
                         'bottom_band': row['bottom_band'], 'right_band': row['right_band'],
                         'side': '+'.join(sides)})
    with atomic_output_path(worklist_path) as tmp_path:
        with open(tmp_path, 'w', newline='', encoding='utf8') as f:
            writer = csv.DictWriter(f, fieldnames=WORKLIST_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return len(rows)


def scan_store(storage_path, ledger_path=None, worklist_path=None, max_runtime_minutes=None, min_band=None):
    """Sweep storage_path, ledger every pano's band depths, and rewrite the work-list; see the module docstring.

    @param storage_path        Root of one city's pano store (the directory holding the 2-char shard dirs).
    @param ledger_path         Default <storage_path>/black_bands.csv.
    @param worklist_path       Default <storage_path>/black_band_worklist.csv.
    @param max_runtime_minutes Stop examining panos after this long; the rest are counted as unreached.
    @param min_band            The work-list's cut, a fraction of the frame; default EDGE_BAND_MAX_FRACTION.
                               0 lists every pano with any band at all - the calibration view.
    @return                    Summary(scanned, clean, banded, current, failed, unreached).

    Example - one nightly-sized slice of a city:
        scan_store('/store/seattle-wa', max_runtime_minutes=240)
    """
    ledger_path = ledger_path or os.path.join(storage_path, LEDGER_FILENAME)
    worklist_path = worklist_path or os.path.join(storage_path, WORKLIST_FILENAME)
    min_band = common.EDGE_BAND_MAX_FRACTION if min_band is None else min_band
    ledger = load_ledger(ledger_path)
    started = time.monotonic()
    scanned = clean = banded = current = failed = unreached = 0

    new_ledger = not os.path.isfile(ledger_path)
    with open(ledger_path, 'a', newline='', encoding='utf8') as ledger_file:
        writer = csv.DictWriter(ledger_file, fieldnames=LEDGER_FIELDS)
        if new_ledger:
            writer.writeheader()
            ledger_file.flush()
        for path in walk_store_panos(storage_path):
            if max_runtime_minutes is not None and time.monotonic() - started >= max_runtime_minutes * 60:
                unreached += 1
                continue
            scanned += 1
            pano_id = _pano_id(path)
            stat = os.stat(path)
            if _is_current(ledger.get(pano_id), stat):
                current += 1
                continue
            dims = jpeg_dimensions(path)
            try:
                if dims is None:
                    raise ValueError('not a readable JPEG')
                bands = common.edge_black_bands_from_file(path)
            except ValueError as e:
                # Left for a human and unledgered, so the next run tries again. One bad pano must not stop a
                # sweep of a multi-terabyte store.
                failed += 1
                print("FAILED %s: %s" % (path, e))
                continue
            row = {'pano_id': pano_id, 'size': str(stat.st_size), 'mtime_ns': str(stat.st_mtime_ns),
                   'width': str(dims[0]), 'height': str(dims[1]),
                   'bottom_band': _fmt(bands.bottom), 'right_band': _fmt(bands.right)}
            writer.writerow(row)
            ledger_file.flush()
            ledger[pano_id] = row
            if common.deep_edge_bands(bands, min_band):
                banded += 1
            else:
                clean += 1

    write_worklist(worklist_path, ledger, min_band)
    return Summary(scanned, clean, banded, current, failed, unreached)


def _fraction(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('%r is not a number' % value)
    if not 0.0 <= number < 1.0:
        raise argparse.ArgumentTypeError('%r is not a fraction in [0, 1)' % value)
    return number


def build_parser():
    parser = argparse.ArgumentParser(
        description='Ledger the depth of the exactly-black band along the bottom and right edges of every '
                    'stored panorama, and write a work-list of the panos with one over the limit (#179). '
                    'Reads only: no panorama is written, moved or deleted.')
    parser.add_argument('storage_path',
                        help='Root of one city\'s pano store - the directory holding the 2-char shard dirs.')
    parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES',
                        help='Stop examining panos after this many minutes; the rest are reported as unreached '
                             'and picked up by the next run.')
    parser.add_argument('--min-band', type=_fraction, default=None, metavar='FRACTION',
                        help='The work-list lists panos with a band deeper than this fraction of the frame '
                             '(default %g, the downloader\'s own limit). 0 lists every pano with any band at '
                             'all.' % common.EDGE_BAND_MAX_FRACTION)
    parser.add_argument('--ledger', default=None, metavar='CSV',
                        help='Where the per-pano ledger lives (default <storage_path>/%s).' % LEDGER_FILENAME)
    parser.add_argument('--worklist', default=None, metavar='CSV',
                        help='Where the work-list is written (default <storage_path>/%s). '
                             'Feed it to refetch_panos.py --worklist.' % WORKLIST_FILENAME)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # The same process-level policy as every entry point that opens a stored panorama: 16384x8192 is over
    # Pillow's decompression-bomb warning threshold, and draft mode still checks the full frame's size.
    common.raise_decompression_bomb_ceiling()
    min_band = common.EDGE_BAND_MAX_FRACTION if args.min_band is None else args.min_band
    worklist_path = args.worklist or os.path.join(args.storage_path, WORKLIST_FILENAME)

    summary = scan_store(args.storage_path, ledger_path=args.ledger, worklist_path=worklist_path,
                         max_runtime_minutes=args.max_runtime, min_band=min_band)
    print("Examined %d panorama(s): %d with an edge band over %.1f%%, %d without, %d already ledgered, "
          "%d failed, %d unreached. Work-list: %s"
          % (summary.scanned, summary.banded, 100 * min_band, summary.clean, summary.current, summary.failed,
             summary.unreached, worklist_path))
    return 1 if summary.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
