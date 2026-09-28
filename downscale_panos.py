# !/usr/bin/python3
"""Write the display copy of every stored panorama wider than the viewer's cap (#115).

A display copy is a stored pano re-encoded at the width a viewer can texture: `<pano_id[:2]>/<pano_id>
.w8192.jpg` beside the native file, for every `<pano_id>.jpg` wider than the cap whose sidecar is absent or
not at the cap.

THIS SCRIPT IS THE ONLY WRITER THAT CREATES ONE. Since 2026-09-09 `downloaders.common.WRITE_DISPLAY_COPIES`
is False, so neither downloader writes a copy and `refetch_panos` only refreshes one that already exists -
deleting it instead when that refresh fails (#122), which leaves an absent copy for this sweep to recreate.
Nothing schedules this script; it writes when a person runs it and not otherwise.

#115 built the feature on the premise that Pannellum renders an equirectangular image as ONE WebGL texture,
making 8192 a hard ceiling. That premise is wrong by a factor of two - Pannellum uploads two half-width
textures, so a device advertising 8192 renders the 16384-wide frames the store actually holds - and the
demand never justified the derivative either: 140,599 wide expired panos were viewed 29 times in ninety
days against a 6.4 TB sweep. The web app cuts the copy on demand now (SidewalkWebpage#5256). Read
docs/ops.md before running this over a whole store; the numbers and the reasoning are there.

Idempotent and resumable: a pano is judged from two JPEG headers (its own, and its sidecar's if there is
one), so re-running over a finished store decodes nothing, and a run cut short by `--max-runtime` picks up
where it left off next time. Run it directly on the host that owns the store rather than over sshfs: the
work is a full JPEG decode per wide pano, ~0.8 s each with Pillow's DCT-domain draft, and the read is the
part sshfs makes slow.

Usage:
    python3 downscale_panos.py <storage_path> [--dry-run] [--max-runtime MINUTES] [--max-width PX]
                               [--min-width PX]
"""

import argparse
import time
from collections import namedtuple

from downloaders.common import (DOWNSCALED_MAX_WIDTH, VIEWER_MAX_PANO_WIDTH, downscaled_sidecar_path,
                                downscaled_size, jpeg_dimensions, raise_decompression_bomb_ceiling,
                                walk_store_panos, write_downscaled_sidecar_from_file)

# 'written' counts sidecars written - or, under --dry-run, sidecars that would have been. 'current' is a
# sidecar already at the cap, 'narrow' a pano the viewer can take as it is, 'unreached' a pano the runtime
# budget left unexamined: the count that says how much a nightly-sized slice has left to do. 'under_min' is a
# pano over the cap but at or below --min-width (#160), left alone without its sidecar being looked at, so
# it is neither current nor written - it is the part of the store a narrower remedy chose not to touch.
# Every scanned pano is in exactly one of written/narrow/current/failed/under_min; unreached were not scanned.
Summary = namedtuple('Summary', ['scanned', 'written', 'narrow', 'current', 'failed', 'unreached', 'under_min'])


def sidecar_is_current(pano_path, pano_dims, max_width):
    """Whether the pano's sidecar exists and is exactly the copy downscaled_size would produce of THIS pano.

    Both numbers, not just the width. The name promises the width, so checking it alone only proves the file
    is not truncated - it says nothing about which panorama the copy belongs to. A 1024x999 sidecar beside a
    2048x1024 panorama passed that test, and so did any sidecar whose aspect was written by an older rule.
    The height is free: jpeg_dimensions already returned it.

    It still cannot see a sidecar that is stale in CONTENT at the right size - the panorama's bytes replaced
    under an unchanged frame. Nothing on disk can, short of a decode per pano, which is the whole cost this
    sweep exists to avoid. The one thing that does that is refetch_panos._refresh_display_copy, which rewrites
    the copy at the moment of the swap; this check is not a substitute for it. When that rewrite fails it
    DELETES the copy (#122) rather than leaving one this check would pass for ever, so the failure reaches
    this sweep as an ABSENT copy - which it recreates from the new panorama like any other missing one, the
    next time a person runs it (nothing schedules it; until then the web app serves the native file).
    """
    expected = downscaled_size(pano_dims[0], pano_dims[1], max_width)
    return jpeg_dimensions(downscaled_sidecar_path(pano_path, max_width)) == expected


def downscale_store(storage_path, dry_run=False, max_width=None, max_runtime_minutes=None, min_width=None):
    """Sweep storage_path and write every missing display copy; see the module docstring.

    @param storage_path        Root of one city's pano store (the directory holding the 2-char shard dirs).
    @param dry_run             Report what would be written without decoding or writing anything.
    @param max_width           The viewer's cap; DOWNSCALED_MAX_WIDTH in production, smaller in a test.
    @param max_runtime_minutes Stop examining panos after this long; the rest are counted as unreached.
    @param min_width           Only panos WIDER than this get a copy (#160); one over the cap but at or below
                               it is counted as under_min and its sidecar is never read. None, the default,
                               is the unfiltered sweep. The #121 remedy passes VIEWER_MAX_PANO_WIDTH (16384).
    @return                    Summary(scanned, written, narrow, current, failed, unreached, under_min).

    Example - the width tripwire's narrow remedy, counting first:
        downscale_store('/store/seattle-wa', dry_run=True, min_width=16384)
    """
    max_width = DOWNSCALED_MAX_WIDTH if max_width is None else max_width
    started = time.monotonic()
    scanned = written = narrow = current = failed = unreached = under_min = 0
    for path in walk_store_panos(storage_path):
        if max_runtime_minutes is not None and time.monotonic() - started >= max_runtime_minutes * 60:
            unreached += 1
            continue
        scanned += 1
        dims = jpeg_dimensions(path)
        if dims is None:
            # A truncated or foreign file: report it and leave the bytes for a human, and keep going - one bad
            # pano must not stop a sweep of a multi-terabyte store.
            failed += 1
            print("FAILED %s: not a readable JPEG" % path)
            continue
        if dims[0] <= max_width:
            narrow += 1
            continue
        # Before the sidecar is read, not after: a filtered pano costs one header, and a copy under the floor
        # - missing, stale or current alike - is never reported as current, which would claim it was checked.
        if min_width is not None and dims[0] <= min_width:
            under_min += 1
            continue
        if sidecar_is_current(path, dims, max_width):
            current += 1
            continue
        try:
            if not dry_run:
                write_downscaled_sidecar_from_file(path, max_width)
            written += 1
            print("%s %s" % ('Would write' if dry_run else 'Wrote', downscaled_sidecar_path(path, max_width)))
        except Exception as e:
            failed += 1
            print("FAILED %s: %s" % (path, e))
    return Summary(scanned, written, narrow, current, failed, unreached, under_min)


def build_parser():
    parser = argparse.ArgumentParser(
        description='Write the %d px display copy of every stored panorama wider than that, beside it as '
                    '<pano_id>.w%d.jpg. Idempotent: a pano whose copy is already at the cap is skipped from '
                    'its headers alone, so re-running on a finished store changes nothing.'
                    % (DOWNSCALED_MAX_WIDTH, DOWNSCALED_MAX_WIDTH))
    parser.add_argument('storage_path',
                        help='Root of one city\'s pano store - the directory holding the 2-char shard dirs.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Only report which copies would be written; decode and write nothing.')
    parser.add_argument('--max-runtime', type=float, default=None, metavar='MINUTES',
                        help='Stop examining panos after this many minutes; the rest are reported as unreached '
                             'and picked up by the next run.')
    parser.add_argument('--max-width', type=int, default=DOWNSCALED_MAX_WIDTH, metavar='PX',
                        help='The viewer\'s cap (default %d). The web app looks for exactly the width its own '
                             'configuration names, so change this only together with it.' % DOWNSCALED_MAX_WIDTH)
    parser.add_argument('--min-width', type=int, default=None, metavar='PX',
                        help='Only write copies for panos WIDER than this; the rest over the cap are reported as '
                             'under --min-width and their copies are not looked at (#160). Must be above '
                             '--max-width. %d, the viewer ceiling, limits the sweep to the frames 8192-class '
                             'GPUs cannot render - the width tripwire\'s remedy (#121). Default: every pano over '
                             'the cap.' % VIEWER_MAX_PANO_WIDTH)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    # A pano can never be both under the cap and over the floor, so a floor at or below the cap filters
    # nothing: it is the unfiltered sweep spelled confusingly (8192), or a misreading of which bound this is
    # (4096). Either way it would silently do the fleet-wide sweep the option exists to avoid - refuse it.
    if args.min_width is not None and args.min_width <= args.max_width:
        parser.error('--min-width %d is at or below --max-width %d, so it would filter nothing and this would '
                     'be the full sweep. Omit --min-width for that, or give a width above the cap (%d is the '
                     'viewer ceiling).' % (args.min_width, args.max_width, VIEWER_MAX_PANO_WIDTH))
    # Process-level policy, the shared one every entry point that opens a stored panorama sets: 16384 x 8192
    # is 134 MP, over Pillow's 89 MP decompression-bomb warning threshold, and this script exists to open
    # exactly those files. A ceiling rather than None, so a corrupt header claiming absurd dimensions is
    # still refused - this sweep, unlike the others, opens whatever the store happens to hold.
    raise_decompression_bomb_ceiling()

    summary = downscale_store(args.storage_path, dry_run=args.dry_run, max_width=args.max_width,
                              max_runtime_minutes=args.max_runtime, min_width=args.min_width)
    # The #160 clause is printed even without --min-width (as 0), so the line has one shape to grep and a
    # --dry-run of the narrow remedy is one number on one line.
    print("Examined %d panorama(s): %d %s, %d under the cap, %d already had a copy, %d failed, %d unreached, "
          "%d under --min-width."
          % (summary.scanned, summary.written, 'would be written' if args.dry_run else 'written',
             summary.narrow, summary.current, summary.failed, summary.unreached, summary.under_min))
    return 1 if summary.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
