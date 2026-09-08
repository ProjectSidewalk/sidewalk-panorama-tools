# !/usr/bin/python3
"""Write the display copy of every stored panorama wider than the viewer's cap (#115).

The Project Sidewalk web app shows a stored pano through Pannellum, which renders it as one WebGL texture,
and 8192 px is a common MAX_TEXTURE_SIZE; a wider pano is displayable only through a copy at that width.
The nightly scraper writes that copy beside every new wide pano it stores (`downloaders.common`), but the
store predates the sidecar, so this sweep writes the missing ones: `<pano_id[:2]>/<pano_id>.w8192.jpg` for
every `<pano_id>.jpg` wider than the cap whose sidecar is absent or not at the cap.

Idempotent and resumable: a pano is judged from two JPEG headers (its own, and its sidecar's if there is
one), so re-running over a finished store decodes nothing, and a run cut short by `--max-runtime` picks up
where it left off next time. Run it directly on the host that owns the store rather than over sshfs: the
work is a full JPEG decode per wide pano, ~0.8 s each with Pillow's DCT-domain draft, and the read is the
part sshfs makes slow.

Usage:
    python3 downscale_panos.py <storage_path> [--dry-run] [--max-runtime MINUTES] [--max-width PX]
"""

import argparse
import os
import time
from collections import namedtuple

from PIL import Image

from downloaders.common import (DOWNSCALED_MAX_WIDTH, downscaled_sidecar_path, is_downscaled_sidecar,
                                jpeg_dimensions, write_downscaled_sidecar_from_file)

# 'written' counts sidecars written - or, under --dry-run, sidecars that would have been. 'current' is a
# sidecar already at the cap, 'narrow' a pano the viewer can take as it is, 'unreached' a pano the runtime
# budget left unexamined: the count that says how much a nightly-sized slice has left to do.
Summary = namedtuple('Summary', ['scanned', 'written', 'narrow', 'current', 'failed', 'unreached'])


def find_panos(storage_path):
    """Yield every stored panorama's path, in a stable order.

    Same shape as refetch_panos.walk_store: only `<2 chars>/<id>.jpg` with the shard matching id[:2] is a
    pano. A sidecar beside it carries the same prefix and suffix, so it is excluded by name, and a `.part`
    left by a crashed writer is not a JPEG at all.
    """
    for shard in sorted(os.listdir(storage_path)):
        shard_path = os.path.join(storage_path, shard)
        if len(shard) != 2 or not os.path.isdir(shard_path):
            continue
        for filename in sorted(os.listdir(shard_path)):
            if filename.endswith('.jpg') and filename[:2] == shard and not is_downscaled_sidecar(filename):
                yield os.path.join(shard_path, filename)


def sidecar_is_current(pano_path, max_width):
    """Whether the pano's sidecar exists and is at the cap. The name promises the width; the header proves it,
    so a truncated or mis-sized copy is rewritten rather than trusted."""
    dims = jpeg_dimensions(downscaled_sidecar_path(pano_path, max_width))
    return dims is not None and dims[0] == max_width


def downscale_store(storage_path, dry_run=False, max_width=None, max_runtime_minutes=None):
    """Sweep storage_path and write every missing display copy; see the module docstring.

    @param storage_path        Root of one city's pano store (the directory holding the 2-char shard dirs).
    @param dry_run             Report what would be written without decoding or writing anything.
    @param max_width           The viewer's cap; DOWNSCALED_MAX_WIDTH in production, smaller in a test.
    @param max_runtime_minutes Stop examining panos after this long; the rest are counted as unreached.
    @return                    Summary(scanned, written, narrow, current, failed, unreached).
    """
    max_width = DOWNSCALED_MAX_WIDTH if max_width is None else max_width
    started = time.monotonic()
    scanned = written = narrow = current = failed = unreached = 0
    for path in find_panos(storage_path):
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
        if sidecar_is_current(path, max_width):
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
    return Summary(scanned, written, narrow, current, failed, unreached)


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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Process-level policy, as in CropRunner.main: a 16384 x 8192 pano is 134 MP, over Pillow's 89 MP
    # decompression-bomb warning threshold, and this script exists to open exactly those files.
    Image.MAX_IMAGE_PIXELS = None

    summary = downscale_store(args.storage_path, dry_run=args.dry_run, max_width=args.max_width,
                              max_runtime_minutes=args.max_runtime)
    print("Examined %d panorama(s): %d %s, %d under the cap, %d already had a copy, %d failed, %d unreached."
          % (summary.scanned, summary.written, 'would be written' if args.dry_run else 'written',
             summary.narrow, summary.current, summary.failed, summary.unreached))
    return 1 if summary.failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
