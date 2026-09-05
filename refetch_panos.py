# !/usr/bin/python3
"""Re-fetch panoramas downloaded while the CBK URL still carried `fover`, to recover their polar-band
resolution (#73).

`fover=2` made CBK serve the polar rows of a zoom-5 grid as 256x256 bodies instead of 512x512 - 320 of 512
tiles on a 16384x8192 panorama - and the stitcher upscaled them 2x and pasted them. The parameter is gone
(#68), so new downloads are clean, but the scraper never revisits an existing image: `download_single_pano`
short-circuits on file existence and the ledger row is permanent. So every panorama scraped before the fix
keeps its half-resolution polar caps forever unless something re-fetches it deliberately. This is that
something, on the same model as migrate_depth_artifacts.py: offline, idempotent, and safe to re-run.

**It repairs; it never backfills.** A panorama with no image on disk is skipped, not downloaded -
DownloadRunner.py owns that, with the ledger semantics that go with it. Nothing here writes
`pano_id_log.csv`, `depth_log.csv`, `log.csv`, or any depth artifact.

**It replaces a stored panorama only when the replacement is strictly better**, which is the whole design.
About half of the labelled panoramas in the store no longer exist at Google (47.9% survival, measured in
reports/2026-08-09-photometa-census.md), so for a large fraction of any work-list the file on disk is the
only copy that will ever exist. Every outcome below other than `replaced` leaves those bytes untouched, and
the swap itself goes through common.atomic_output_path.

Outcomes:

    absent        no .jpg on disk - nothing to repair
    unreadable    the stored file is not a readable JPEG; left for a human rather than papered over
    not_affected  the stored frame implies a max zoom below 5, and the band is a zoom-5-only effect
    already_clean the file was written after --fixed-after, so it never carried `fover`
    dims_changed  the work-list's frame disagrees with the stored one; see --allow-dims-change
    gone          Google no longer serves this panorama at any zoom
    frame_grew    Google now serves this panorama larger, so this frame would fetch a CROP of it
    upscaled      only a fallback zoom was available - swapping would be a 4x DOWNGRADE
    undersized    a tile still came back below 512px: the request is costing resolution again (#73). Not
                  swapped, not ledgered, and three in a row stop the run with a nonzero exit
    too_black     the fresh stitch has more black than a real panorama does; refused
    replaced      swapped in

The first five cost zero HTTP requests and are recomputed from the store on every run rather than ledgered.
That is what keeps a pass over a large store cheap, makes a re-run after a completed sweep free even if the
ledger is lost, and means a flag such as --fixed-after can be corrected later without scrubbing anything.
The rest cost requests, and every one of them except `undersized` is remembered in
`<storage_path>/refetch_log.csv` (a row means permanent, exactly as the two nightly ledgers do it; anything
transient is counted and left unledgered so it retries).

Usage:
    python3 refetch_panos.py <storage_path> (--worklist <csv[.gz]> | --from-store) [options]
"""

import argparse
import csv
import gzip
import json
import logging
import logging.handlers
import os
import random
import signal
import sys
import time
from collections import Counter

from downloaders import gsv
from downloaders.common import atomic_output_path, jpeg_dimensions

# The band was only ever a zoom-5 effect: a panorama whose own max zoom is 3 or 4 was served at full size at
# every level (measured on a 2007 DC panorama in reports/2026-08-07-cbk-tile-resolution.md, finding 1). So a
# stored frame narrow enough to have been a max-zoom-3 download has nothing to recover, and is skipped
# without spending a request.
AFFECTED_MIN_ZOOM = 5

# `fover` was dropped from _CBK_BASE_URL on 2026-08-07 (#68, c704b3f). A stored file written at or after the
# date the fix reached the scraper box was fetched without it and is already at full resolution. The default
# is the merge date; --fixed-after exists because deployment is not merge, and only the operator knows when
# the box actually picked the fix up. Erring late costs a wasted re-fetch; erring early would skip files that
# do need repair, so the default is deliberately the EARLIEST defensible date.
DEFAULT_FIXED_AFTER = '2026-08-07'

# A stitched frame with more exactly-black pixels than this is refused. Much tighter than the stitcher's own
# STITCH_MAX_BLACK_FRACTION (0.5), and deliberately: that one asks "is this imagery at all", while this one
# asks "is this good enough to overwrite a panorama we already have". The repo's real 13312x6656 sample is
# 0.0% exactly-black. The failure this catches is a reported frame larger than what Google actually serves -
# request a 32x16 grid for a panorama Google holds at 26x13 and the out-of-range tiles come back black,
# which is 34% of the frame and sails under a 50% limit.
MAX_BLACK_FRACTION = 0.02

# Stop after this many consecutive transient failures rather than spending the rest of the budget on a wall -
# the depth phase's breaker (gsv.DEPTH_MAX_CONSECUTIVE_FAILURES), for the same reason. Lower than depth's 25
# because one pano here is ~512 requests rather than one.
MAX_CONSECUTIVE_FAILURES = 5

LEDGER_FILENAME = 'refetch_log.csv'
MEASUREMENTS_FILENAME = 'refetch_measurements.jsonl'
LOG_FILENAME = 'refetch.log'

# Every outcome the pass can reach, in the order the summary prints them. Everything else that can happen is
# an exception, which is transient by construction.
OUTCOMES = ('absent', 'unreadable', 'not_affected', 'already_clean', 'dims_changed',
            'gone', 'frame_grew', 'upscaled', 'undersized', 'too_black', 'replaced')

# The outcomes the ledger remembers, so a later run never re-attempts them: the ones that cost requests to
# reach, and whose answer is a property of the panorama or of Google. The five zero-request outcomes are
# deliberately NOT here. Each is recomputed from the store in microseconds, so remembering one buys nothing -
# and two of them are properties of the FLAGS rather than the panorama: `already_clean` moves with
# --fixed-after, `dims_changed` with --allow-dims-change and whichever work-list was passed. Ledgering them
# would lock a run's flag values in: run once with the default --fixed-after, learn the box picked the fix up
# two weeks later, re-run with the right date - and every file in that window would be skipped for good.
# `too_black` does move with --max-black, but it cost a full fan-out to learn, so it is kept. `undersized` is
# the other exclusion, for the reason MAX_CONSECUTIVE_UNDERSIZED gives.
LEDGERED_OUTCOMES = ('gone', 'frame_grew', 'upscaled', 'too_black', 'replaced')

# `undersized` is #73's tripwire. With `fover` gone no tile should ever come back below 512px, so one that
# does means some request parameter is costing resolution again - a property of the REQUEST, not of the
# panorama. So it is not ledgered (a permanent row per pano would burn the whole work-list against a bug in
# the URL, and exit 0 while doing it), and this many in a row stops the run with a nonzero exit, the way
# consecutive transient failures do. Three rather than one because a single panorama served small for its
# own reasons is not impossible; three consecutive fan-outs is not that. Only outcomes that reached a fan-out
# reset the count - `gone` and `frame_grew` never fetched a tile, so they say nothing either way.
MAX_CONSECUTIVE_UNDERSIZED = 3
FAN_OUT_OUTCOMES = ('upscaled', 'undersized', 'too_black', 'replaced')

# Tile rows CBK served at half size, per pano height: (first full-res row, last full-res row inclusive, rows
# in the grid). Measured, not assumed - the full-grid sweeps in tests/fixtures/tiles/fover_band_map.json,
# which tests/test_gsv_tile_contract.py pins. Same table as reports/scripts/pano_y_histogram.py's BAND_ROWS,
# and a test asserts the two agree: they are the same measurement read for two different purposes, and a
# re-capture that moved the band must not leave one of them quietly stale.
BAND_ROWS = {8192: (5, 10, 16), 6656: (4, 8, 13)}


def band_pixel_rows(pano_height):
    """((bottom band top, bottom), (horizon band top, bottom)) in pixels, or None for an unswept geometry.

    The bottom band is the half-resolution region that matters: Sidewalk labels are ground features, and the
    top band holds two labels across both cities measured, both of them bad data. The horizon band is the
    control - CBK left those rows alone, so any difference measured there between the stored file and a
    fresh one is our own JPEG round-trip rather than recovered detail.
    """
    rows = BAND_ROWS.get(pano_height)
    if rows is None:
        return None
    first_full, last_full, _n_rows = rows
    return ((last_full + 1) * gsv.TILE_SIZE, pano_height), (first_full * gsv.TILE_SIZE,
                                                            (last_full + 1) * gsv.TILE_SIZE)


def _parse_fixed_after(value):
    """argparse type= for --fixed-after: a YYYY-MM-DD date, as a POSIX timestamp at local midnight."""
    try:
        parsed = time.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise argparse.ArgumentTypeError('expected a YYYY-MM-DD date, got %r' % (value,))
    return time.mktime(parsed)


def build_parser():
    parser = argparse.ArgumentParser(
        description='Re-fetch panoramas downloaded while the CBK URL carried `fover`, recovering the polar-'
                    'band resolution it cost (#73). Idempotent: a panorama with a permanent outcome in '
                    'refetch_log.csv is never re-attempted, and every outcome but `replaced` leaves the '
                    'stored file byte-for-byte untouched.')
    parser.add_argument('storage_path',
                        help='Root of the pano store - the directory holding the 2-char shard dirs.')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--worklist', metavar='CSV',
                        help='CSV (optionally .gz) of panoramas to repair; needs a pano_id column, and uses '
                             'width/height when present. Generate one with '
                             'reports/scripts/pano_y_histogram.py --write-worklist.')
    source.add_argument('--from-store', action='store_true',
                        help='Walk the store instead of reading a work-list - every stored panorama is a '
                             'candidate. Far larger than a work-list; see docs/ops.md for the cost.')
    parser.add_argument('--sample', type=int, default=None, metavar='N',
                        help='Consider a random N of the candidates. For pilots.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report the outcome of every zero-request decision and stop there; makes no '
                             'HTTP requests, writes nothing.')
    parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES',
                        help='Stop starting new panoramas after this many minutes.')
    parser.add_argument('--max-panos', type=int, default=None, metavar='N',
                        help='Stop after fetching N panoramas this run (zero-request decisions do not '
                             'count).')
    parser.add_argument('--min-pano-interval', type=float, default=0.0, metavar='SECONDS',
                        help='Minimum seconds between the START of one panorama fetch and the next. One '
                             'panorama is ~512 tile requests, so this is the throttle that matters.')
    parser.add_argument('--fixed-after', type=_parse_fixed_after, default=DEFAULT_FIXED_AFTER,
                        metavar='YYYY-MM-DD',
                        help='A stored file modified on or after this date was downloaded without `fover` '
                             'and is skipped. Set it to the date the fix reached the scraper box. '
                             'Default %s (the date it was merged).' % DEFAULT_FIXED_AFTER)
    parser.add_argument('--allow-dims-change', action='store_true',
                        help='Fetch at the WORK-LIST\'s frame rather than the stored file\'s, and accept a '
                             'panorama whose dimensions change. Off by default: recovering polar resolution '
                             'and re-framing a panorama are different decisions, and the second one moves '
                             'every label\'s pixel coordinates relative to the image.')
    parser.add_argument('--max-black', type=float, default=MAX_BLACK_FRACTION, metavar='FRACTION',
                        help='Refuse a fresh stitch with more than this fraction of exactly-black pixels. '
                             'Default %g.' % MAX_BLACK_FRACTION)
    parser.add_argument('--measure', action='store_true',
                        help='Record what each swap actually recovered to %s, one line per swapped '
                             'panorama, written as the swap lands. Decodes the stored frame as well as the '
                             'fresh one, so it roughly doubles peak memory - meant for a pilot, not a bulk '
                             'pass.' % MEASUREMENTS_FILENAME)
    return parser


def configure_logging(log_path):
    """Rotating log on the store, next to scrape.log - never the CWD, for the reason DownloadRunner gives.

    `None` logs to stderr instead, with no file opened anywhere: --dry-run promises to write nothing to the
    store, and a RotatingFileHandler creates its file the moment it is constructed.
    """
    fallback_error = None
    if log_path is None:
        handler = logging.StreamHandler()
    else:
        try:
            handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024,
                                                           backupCount=3)
        except OSError as e:
            handler = logging.StreamHandler()
            fallback_error = e
    handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    if fallback_error is not None:
        logging.warning("Could not open %s (%s); logging to stderr for this run", log_path, fallback_error)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def read_worklist(path):
    """Panorama dicts from a work-list CSV, which may be gzipped. Dedupes on pano_id, keeping the first.

    Read with `csv`, not pandas, and deliberately: pano_id must never take its type from what the ids happen
    to look like. An all-numeric column infers int64 and a single blank cell infers float64, whose str() mints
    ids like '1.23e+14' - the #46 bug class that both runners are pinned against. `csv` gives strings, always.

    width/height are carried through when present so --allow-dims-change has a frame to fetch at; they are
    optional because the stored file's own header is the default source.
    """
    opener = gzip.open if path.endswith('.gz') else open
    rows, seen = [], set()
    with opener(path, 'rt', newline='', encoding='utf8') as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or 'pano_id' not in reader.fieldnames:
            raise ValueError("%s has no 'pano_id' column; found %r" % (path, reader.fieldnames))
        for row in reader:
            pano_id = (row.get('pano_id') or '').strip()
            if not pano_id or pano_id in seen:
                continue
            seen.add(pano_id)
            record = {'pano_id': pano_id}
            for key in ('width', 'height'):
                value = (row.get(key) or '').strip()
                if value:
                    try:
                        record[key] = int(float(value))
                    except (ValueError, OverflowError):
                        # OverflowError is int(float('inf')): a junk cell, not a reason to strand the rows
                        # after it.
                        pass
            rows.append(record)
    return rows


def walk_store(storage_path):
    """Every stored panorama, as work-list records with no dims (the stored header is the source).

    Only `<storage_path>/<2 chars>/<id>.jpg` with the shard matching id[:2] counts - the layout
    download_single_pano writes and _stored_path reads back. Anything else carrying a .jpg suffix under the
    root (a crops tree, a stray file at the top level) is not a panorama, and listing it would hand
    decide_without_fetching an id whose reconstructed path does not exist: an `absent` for something that
    was never a pano, one summary line of noise per file.
    """
    rows = []
    for shard in sorted(os.listdir(storage_path)):
        shard_path = os.path.join(storage_path, shard)
        if len(shard) != 2 or not os.path.isdir(shard_path):
            continue
        for filename in sorted(os.listdir(shard_path)):
            if filename.endswith('.jpg') and filename[:2] == shard:
                rows.append({'pano_id': filename[:-len('.jpg')]})
    return rows


def load_ledger(ledger_path):
    """Every pano id with a ledgered outcome already recorded.

    Row-tolerant on the model of gsv._load_depth_log and DownloadRunner.progress_check: a line torn by a
    crash mid-append is skipped, so a damaged ledger costs a few re-attempted panoramas rather than crashing
    every future run (#55). A status outside LEDGERED_OUTCOMES is also skipped - an unrecognised one from a
    newer version of this script, or a zero-request outcome from an older version that still wrote them -
    so the ledger degrades to re-examining those panoramas, which is safe because every gate is re-run and
    the zero-request ones cost nothing.
    """
    resolved = set()
    if not os.path.isfile(ledger_path):
        return resolved
    with open(ledger_path, newline='') as f:
        for row in csv.reader(f):
            if len(row) != 2 or row[0] == 'pano_id' or row[1] not in LEDGERED_OUTCOMES:
                continue
            resolved.add(row[0])
    return resolved


def _luma_band(image, top, bottom):
    """The (top, bottom) horizontal strip of `image` as a float32 luma array."""
    import numpy as np
    return np.asarray(image.crop((0, top, image.width, bottom)).convert('L'), dtype=np.float32)


def _halve_and_restore(strip):
    """`strip` at half scale and back, as an array - the detail halving that region would destroy.

    This is the metric the fover investigation used per tile row (fover_band_map.json's halving_cost_by_row),
    applied to a whole band so the two are directly comparable.
    """
    import numpy as np
    from PIL import Image
    small = Image.fromarray(strip.astype('uint8')).resize(
        (max(1, strip.shape[1] // 2), max(1, strip.shape[0] // 2)), Image.LANCZOS)
    restored = small.resize((strip.shape[1], strip.shape[0]), Image.LANCZOS)
    return np.asarray(restored, dtype=np.float32)


def measure_recovery(old_image, new_image):
    """What a re-fetch actually recovered, in both bands, or None for an unswept geometry.

    Three numbers per band, and the interpretation depends on all three together:

      mae_old_vs_new       how much the stored frame and the fresh one differ.
      halve_restore_new    how much detail halving THIS band would destroy - the ceiling on what `fover`
                           could have cost here, measured on this panorama rather than assumed.
      (the horizon band)   the control. CBK served those rows at full size in both eras, so whatever
                           mae_old_vs_new reads there is our own JPEG round-trip and nothing else.

    So the recovered detail is the bottom band's mae_old_vs_new MINUS the horizon band's - which is the one
    number the re-download decision actually turns on, and the one nobody has measured. Reported per panorama
    rather than aggregated here: the aggregation is a study (reports/scripts/), not an ops tool's job.
    """
    bands = band_pixel_rows(new_image.height)
    if bands is None or old_image.size != new_image.size:
        return None
    out = {}
    for name, (top, bottom) in zip(('bottom', 'horizon'), bands):
        old_strip = _luma_band(old_image, top, bottom)
        new_strip = _luma_band(new_image, top, bottom)
        out[name] = {
            'rows_px': [top, bottom],
            'mae_old_vs_new': float(abs(old_strip - new_strip).mean()),
            'halve_restore_new': float(abs(new_strip - _halve_and_restore(new_strip)).mean()),
        }
    out['recovered_above_noise'] = round(
        out['bottom']['mae_old_vs_new'] - out['horizon']['mae_old_vs_new'], 6)
    return out


def _stored_path(storage_path, pano_id):
    return os.path.join(storage_path, pano_id[:2], pano_id + '.jpg')


def decide_without_fetching(storage_path, record, fixed_after, allow_dims_change):
    """Everything knowable from the store alone: (outcome, fetch_dims).

    outcome is one of the five zero-request statuses when the panorama needs no request at all, and None
    when it does; in that case fetch_dims is the (width, height) frame to fetch at. None of the five is
    ledgered - see LEDGERED_OUTCOMES - so this runs again, for free, on every pass.

    The default frame is the STORED file's, not the work-list's. That is what keeps this pass to the thing
    that was actually agreed - recovering polar-band resolution inside the frame the store already has.
    Re-framing a panorama moves every label's pixel coordinates relative to the image and is a separate
    decision with its own evidence (the 4.6% measured in reports/2026-08-10-store-coverage.md), so a
    work-list frame that disagrees stops the panorama here unless the operator opted in.
    """
    path = _stored_path(storage_path, record['pano_id'])
    if not os.path.isfile(path):
        return 'absent', None

    stored_dims = jpeg_dimensions(path)
    if stored_dims is None:
        return 'unreadable', None

    if gsv._pano_max_zoom(stored_dims[0]) < AFFECTED_MIN_ZOOM:
        return 'not_affected', None

    if os.path.getmtime(path) >= fixed_after:
        return 'already_clean', None

    listed_dims = (record.get('width'), record.get('height'))
    if listed_dims[0] is not None and listed_dims[1] is not None and tuple(listed_dims) != stored_dims:
        if not allow_dims_change:
            return 'dims_changed', None
        return None, tuple(listed_dims)

    return None, stored_dims


def refetch_pano(storage_path, record, fetch_dims, max_black, measure, measurements):
    """Fetch one panorama and swap it in if the replacement is strictly better.

    Returns an outcome string. Raises for anything transient - a failed tile, a mostly-black stitch, a
    storage error - so the caller counts it and leaves it unledgered, and it retries next run.

    With `measure`, the recovery record is computed against the stored bytes before the swap and appended
    to `measurements` only AFTER the swap has landed. A save that raises therefore leaves no record behind:
    the panorama is unledgered, the next run fetches and measures it again, and a record appended before the
    save would have made that panorama count twice in the pilot.
    """
    pano_id = record['pano_id']
    width, height = fetch_dims

    resolved = gsv.resolve_zoom_and_dims({'pano_id': pano_id, 'width': width, 'height': height})
    if resolved is None:
        # Google answers a black tile at every zoom for an id it has retired. Permanent, and the number the
        # whole retirement argument rests on - ~52% of labelled panoramas, per the photometa census.
        return 'gone'
    _width, _height, zoom = resolved

    if not gsv.frame_covers_pano(pano_id, width, height, zoom):
        # Google now serves this pano larger than the frame we are fetching at, so that grid would return
        # the top-left corner of it - at the right dimensions, with no undersized tile and no black. The
        # only gate that can see a silently cropped panorama, and it runs before the fan-out so it costs
        # two requests. Permanent: the frame is a property of what Google holds, not of the network.
        return 'frame_grew'

    stitched = gsv.fetch_pano_image(pano_id, width, height, zoom)

    # Three refusals, in the order of how much damage swapping would do. Each leaves the stored bytes exactly
    # as they were; only the code path below all three touches the file.
    if stitched.upscaled:
        # Only a fallback zoom was available for this frame, so the fresh stitch was LANCZOS'd up to reach
        # it. The stored file may well be a genuine zoom-5 stitch, and nothing in the image can tell us -
        # both are at the reported dims. Swapping risks trading full resolution for a 4x upscale, to recover
        # a band worth a fraction of that. Never worth it.
        return 'upscaled'
    if stitched.undersized_tiles:
        # Some request parameter is costing resolution again (#73's tripwire). Whatever came back is no
        # better than what is on disk, and swapping would spend a JPEG generation for nothing.
        return 'undersized'
    black = gsv._black_fraction(stitched.image)
    if black > max_black:
        logging.warning("REFETCH: pano %s: fresh stitch is %.1f%% black (limit %.1f%%); refusing to swap",
                        pano_id, 100 * black, 100 * max_black)
        return 'too_black'

    recovery = None
    if measure:
        # Measured before the swap, because the comparison needs the bytes the swap is about to replace;
        # recorded after it, for the reason the docstring gives.
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None       # a real pano is past Pillow's decompression-bomb ceiling
        with Image.open(_stored_path(storage_path, pano_id)) as old_image:
            recovery = measure_recovery(old_image, stitched.image)
        if recovery is not None:
            recovery.update({'pano_id': pano_id, 'zoom': zoom,
                             'width': width, 'height': height,
                             'old_bytes': os.path.getsize(_stored_path(storage_path, pano_id))})

    with atomic_output_path(_stored_path(storage_path, pano_id)) as tmp_path:
        stitched.image.save(tmp_path, 'jpeg')
    if recovery is not None:
        measurements.append(recovery)
    return 'replaced'


def refetch_store(storage_path, records, dry_run=False, fixed_after=None, allow_dims_change=False,
                  max_black=MAX_BLACK_FRACTION, measure=False, sample=None,
                  max_runtime_minutes=None, max_panos=None, min_pano_interval=0.0):
    """Work through `records`, repairing what needs it. Returns a Counter of outcomes.

    Candidates are shuffled, for the reason the depth phase shuffles (gsv.download_depth_maps): a transient
    failure leaves no ledger row, so a stable order re-attempts the same failing head block every run - and
    with the consecutive-failure breaker below, five persistently failing panoramas at the head of a stable
    list would stop every run before it reached any new work. That deliberately discards whatever order a
    work-list was written in; --sample needs a random draw anyway, and a budget-cut pass therefore did a
    random slice of the work rather than a prioritised one.

    @return Counter with one key per outcome, plus 'transient_failures' and (when a budget cut the run short)
            'stop_reason' - whose value is a STRING, not a count, so `sum(counts.values())` is not safe on
            this return value. Read outcomes by key. reports/scripts/refetch_pilot.py sums the ledger file
            instead, which carries outcomes only.
    """
    if fixed_after is None:
        fixed_after = _parse_fixed_after(DEFAULT_FIXED_AFTER)

    ledger_path = os.path.join(storage_path, LEDGER_FILENAME)
    measurements_path = os.path.join(storage_path, MEASUREMENTS_FILENAME)
    resolved_ids = load_ledger(ledger_path)
    candidates = [r for r in records if r['pano_id'] not in resolved_ids]
    counts = Counter({'skipped_ledgered': len(records) - len(candidates)})

    random.shuffle(candidates)
    if sample is not None:
        candidates = candidates[:sample]

    measurements = []
    run_start = time.monotonic()
    fetched = consecutive_failures = consecutive_undersized = 0
    last_fetch_at = None
    stop_reason = None

    ledger_file = None
    ledger = None
    if not dry_run:
        ledger_existed = os.path.isfile(ledger_path)
        ledger_file = open(ledger_path, 'a', newline='')
        # lineterminator='\n' and the group-writable mode, for the reasons the image ledger gives: other lab
        # users' runs append to the same store, and a mixed line ending hands ops a trailing '\r' to grep.
        ledger = csv.writer(ledger_file, lineterminator='\n')
        if not ledger_existed:
            ledger.writerow(['pano_id', 'status'])
            ledger_file.flush()
            try:
                os.chmod(ledger_path, 0o664)
            except OSError:
                pass    # lost the race to another user's run; their file, their modes

    try:
        for record in candidates:
            pano_id = record['pano_id']
            outcome, fetch_dims = decide_without_fetching(storage_path, record, fixed_after,
                                                          allow_dims_change)

            if outcome is None:
                if dry_run:
                    # The point of --dry-run is the shape of the work: how many panoramas a real run would
                    # actually fetch, without asking Google anything.
                    counts['would_fetch'] += 1
                    continue
                if max_runtime_minutes is not None:
                    # time.monotonic, never the wall clock: an NTP step must not stretch the budget (#51).
                    elapsed = (time.monotonic() - run_start) / 60.0
                    if elapsed >= max_runtime_minutes:
                        stop_reason = 'max-runtime'
                        print("REFETCH: max runtime of %.1f minutes reached (%.1f elapsed). Stopping."
                              % (max_runtime_minutes, elapsed))
                        break
                if max_panos is not None and fetched >= max_panos:
                    stop_reason = 'max-panos'
                    print("REFETCH: max panos (%d) reached. Stopping." % (max_panos,))
                    break
                if min_pano_interval and last_fetch_at is not None:
                    remaining = min_pano_interval - (time.monotonic() - last_fetch_at)
                    if remaining > 0:
                        time.sleep(remaining)
                last_fetch_at = time.monotonic()
                fetched += 1
                try:
                    outcome = refetch_pano(storage_path, record, fetch_dims, max_black, measure,
                                           measurements)
                    consecutive_failures = 0
                    if measurements:
                        # As each swap lands, not at the end of the run: --measure is the mode that doubles
                        # peak memory, so an OOM kill is the likeliest way this run dies, and a record held
                        # until `finally` would die with it.
                        _append_measurements(measurements_path, measurements)
                        measurements.clear()
                except Exception as e:
                    # Transient by construction: everything permanent is a returned outcome. Counted but NOT
                    # ledgered, so it retries next run - the #41 semantics both nightly ledgers use.
                    counts['transient_failures'] += 1
                    consecutive_failures += 1
                    logging.error("REFETCH: pano %s failed: %s", pano_id, str(e))
                    print("REFETCH: %s failed (%s)" % (pano_id, e))
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        stop_reason = 'consecutive-failures'
                        print("REFETCH: %d consecutive failures. Stopping rather than spending the rest of "
                              "the budget on a wall." % (consecutive_failures,))
                        break
                    continue

            counts[outcome] += 1
            if ledger is not None and outcome in LEDGERED_OUTCOMES:
                ledger.writerow([pano_id, outcome])
                ledger_file.flush()
            if outcome == 'replaced':
                print("REFETCH: %s replaced" % (pano_id,))
                logging.info("REFETCH: pano %s replaced at %dx%d", pano_id, *fetch_dims)
            if outcome in FAN_OUT_OUTCOMES:
                consecutive_undersized = consecutive_undersized + 1 if outcome == 'undersized' else 0
            if outcome == 'undersized':
                # Both channels, as the depth phase does: stdout is how someone hears about it tonight, the
                # log is what is still there next week.
                logging.error("REFETCH: pano %s: tiles came back undersized; the CBK request is costing "
                              "resolution again (#73). Not swapped, not ledgered.", pano_id)
                print("REFETCH: %s came back undersized - not swapped, not ledgered" % (pano_id,))
                if consecutive_undersized >= MAX_CONSECUTIVE_UNDERSIZED:
                    stop_reason = 'undersized'
                    print("REFETCH: %d consecutive undersized fetches. The CBK request is costing resolution "
                          "again (#73); check its parameters before re-running. Stopping."
                          % (consecutive_undersized,))
                    break
    finally:
        if ledger_file is not None:
            ledger_file.close()
        if measurements:
            _append_measurements(measurements_path, measurements)

    if stop_reason:
        counts['stop_reason'] = stop_reason
    return counts


def _append_measurements(path, measurements):
    """One JSON object per line, appended. A pilot's raw data - reduced into a report by a study script, not
    here."""
    with open(path, 'a', encoding='utf8') as f:
        for record in measurements:
            f.write(json.dumps(record, allow_nan=False) + '\n')


def _print_summary(counts):
    print("\nOutcomes:")
    for key in OUTCOMES:
        if counts.get(key):
            print("  %-14s %d" % (key, counts[key]))
    for key in ('would_fetch', 'skipped_ledgered', 'transient_failures'):
        if counts.get(key):
            print("  %-14s %d" % (key, counts[key]))
    if counts.get('stop_reason'):
        print("  stopped early: %s" % (counts['stop_reason'],))
    if counts.get('transient_failures'):
        print("Transient failures are not ledgered and retry on the next run; no panorama was lost.")
    if counts.get('undersized'):
        print("Undersized fetches are not ledgered: check the CBK request parameters (#73) before re-running.")


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not os.path.isdir(args.storage_path):
        raise SystemExit("No such store: %s" % (args.storage_path,))

    # A dry run promises to write nothing to the store, and that includes its log file.
    configure_logging(None if args.dry_run else os.path.join(args.storage_path, LOG_FILENAME))
    # A stop must run the finally that closes the ledger and flushes the measurements, rather than discarding
    # them - DownloadRunner translates SIGTERM for the same reason.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    records = walk_store(args.storage_path) if args.from_store else read_worklist(args.worklist)
    print("Considering %d panorama(s) from %s"
          % (len(records), 'the store' if args.from_store else args.worklist))

    counts = refetch_store(args.storage_path, records, dry_run=args.dry_run, fixed_after=args.fixed_after,
                           allow_dims_change=args.allow_dims_change, max_black=args.max_black,
                           measure=args.measure, sample=args.sample,
                           max_runtime_minutes=args.max_runtime, max_panos=args.max_panos,
                           min_pano_interval=args.min_pano_interval)
    _print_summary(counts)

    # Exit 1 on transient failures (something is wrong with the network or the store) and on any undersized
    # fetch (the CBK request is costing resolution again, #73) - both are things cron should say. Every other
    # outcome, `gone` included, is this tool working correctly.
    return 1 if counts.get('transient_failures') or counts.get('undersized') else 0


if __name__ == '__main__':
    raise SystemExit(main())
