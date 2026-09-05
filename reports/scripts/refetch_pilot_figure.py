"""The before/after figure for the `fover` re-fetch pilot: one window of one bottom band, twice (#73).

    python reports/scripts/refetch_pilot_figure.py --old-store <production city dir> --new-store <pilot copy> \\
        --pano-id CkUrdiulTbw482CMAkrKyg --x <col> --y <row> \\
        --write reports/figures/<date>-fover-refetch-pilot-<pano_id>.png

Section 4 of the report says the re-fetched polar band has less high-frequency energy than the stored one,
and section 5 says that is because the stored one's extra energy is the stitcher's Lanczos ringing rather
than detail. Both are statistics. This is the one place a reader can look at the two frames and check that
the smoother image is not missing anything - which is the claim, and the only one a number cannot carry.

Two rows: the same `--window` of the bottom band from both stores at 1:1, with the magnified patch outlined
in red; then that patch at `--zoom` with NEAREST scaling, so pixel structure is visible rather than smoothed
by the viewer. Both stores must hold the panorama at the same dimensions, which is what a `replaced` row in
the pilot ledger means.

**The committed 2026-09-05 figure was produced by an ad-hoc equivalent of this script and its window origin
was not recorded**, so re-running this reproduces the figure's recipe, not its bytes. `--x`/`--y` are
required for that reason: there is no defensible default, and a silent one would invent provenance the
committed PNG does not have. Everything else about the layout is measured off that PNG and is the default
here.
"""

import argparse
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
REPO = os.path.dirname(REPORTS)
for _p in (HERE, REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from studyfmt import display_path  # noqa: E402
from refetch_panos import band_pixel_rows  # noqa: E402

Image.MAX_IMAGE_PIXELS = None       # a real pano is past Pillow's decompression-bomb ceiling

# Layout, in pixels, measured off the committed figure rather than chosen: 6-px margins and gutter, a 16-px
# label band above each row. A 640x320 window at 4x fills the second row at exactly the first row's width,
# which is why those are the defaults - the two rows line up only for that pairing.
MARGIN = 6
LABEL_H = 16
BOX_COLOUR = (220, 20, 20)


def band_window(image, x, y, width, height):
    """The `width` x `height` window at (x, y) of `image`'s bottom band, in the band's own coordinates.

    Expressed against the band rather than the frame because that is the region under discussion, and
    because the two zoom-5 geometries put the band at different absolute rows - the same --y means the same
    place in the band on a 6656 and on an 8192 panorama.
    """
    bands = band_pixel_rows(image.height)
    if bands is None:
        raise SystemExit('%dx%d is not a swept zoom-5 geometry' % (image.width, image.height))
    (top, bottom), _horizon = bands
    if y + height > bottom - top or x + width > image.width:
        raise SystemExit('window %dx%d at (%d, %d) does not fit the %d-row bottom band'
                         % (width, height, x, y, bottom - top))
    return image.crop((x, top + y, x + width, top + y + height)).convert('RGB')


def compose(left, right, patch_xy, patch_size, zoom, labels):
    """The two-row sheet: both windows at 1:1 with the patch outlined, then both patches at `zoom`."""
    win_w, win_h = left.size
    px, py = patch_xy
    pw, ph = patch_size
    cell_w = max(win_w, pw * zoom)
    row1 = MARGIN + LABEL_H + MARGIN
    row2 = row1 + win_h + MARGIN + LABEL_H + MARGIN
    sheet = Image.new('RGB', (MARGIN * 3 + cell_w * 2, row2 + ph * zoom + MARGIN), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for column, (image, label) in enumerate(zip((left, right), labels[:2])):
        x0 = MARGIN + column * (cell_w + MARGIN)
        draw.text((x0, MARGIN), label, fill=(0, 0, 0), font=font)
        outlined = image.copy()
        # Drawn on the copy, never on the crop that feeds the magnified row: an outline burnt into the
        # patch would then appear inside the 4x view as evidence of something that is not in the imagery.
        ImageDraw.Draw(outlined).rectangle([px, py, px + pw, py + ph], outline=BOX_COLOUR)
        sheet.paste(outlined, (x0, row1))
    for column, (image, label) in enumerate(zip((left, right), labels[2:])):
        x0 = MARGIN + column * (cell_w + MARGIN)
        draw.text((x0, row2 - LABEL_H - MARGIN), label, fill=(0, 0, 0), font=font)
        patch = image.crop((px, py, px + pw, py + ph)).resize((pw * zoom, ph * zoom), Image.NEAREST)
        sheet.paste(patch, (x0, row2))
    return sheet


def scrape_month(path):
    """The stored file's mtime as YYYY-MM - the same reading `--fixed-after` gates on, so the label says
    which era the left-hand frame is from rather than asserting it."""
    return time.strftime('%Y-%m', time.localtime(os.path.getmtime(path)))


def stored_path(store, pano_id):
    return os.path.join(store, pano_id[:2], pano_id + '.jpg')


def build_parser():
    parser = argparse.ArgumentParser(description='Before/after figure for one re-fetched panorama (#73).')
    parser.add_argument('--old-store', required=True, help='The store holding the ORIGINAL frame (production).')
    parser.add_argument('--new-store', required=True, help='The pilot copy, holding the re-fetched frame.')
    parser.add_argument('--pano-id', required=True, help='A pano with a `replaced` row in the pilot ledger.')
    parser.add_argument('--x', type=int, required=True, help='Window left edge, in frame columns.')
    parser.add_argument('--y', type=int, required=True, help='Window top edge, in rows BELOW the band top.')
    parser.add_argument('--window', default='640x320', help='Window size. Default 640x320.')
    parser.add_argument('--patch', default='160x80', help='Magnified patch size. Default 160x80.')
    parser.add_argument('--patch-at', default='200,40', help='Patch origin within the window. Default 200,40.')
    parser.add_argument('--zoom', type=int, default=4, help='Magnification of the second row. Default 4.')
    parser.add_argument('--probed', default=None, metavar='YYYY-MM-DD',
                        help='The date the re-fetch ran, for the right-hand label. Passed in rather than '
                             'taken from the clock, for the reason refetch_pilot.py --probed gives.')
    parser.add_argument('--write', default=None, metavar='PATH', help='Write the figure here.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    win_w, win_h = (int(v) for v in args.window.lower().split('x'))
    patch_w, patch_h = (int(v) for v in args.patch.lower().split('x'))
    patch_at = tuple(int(v) for v in args.patch_at.split(','))
    old_path = stored_path(args.old_store, args.pano_id)
    new_path = stored_path(args.new_store, args.pano_id)

    with Image.open(old_path) as old, Image.open(new_path) as new:
        if old.size != new.size:
            raise SystemExit('%s is %dx%d stored and %dx%d re-fetched; nothing to compare like-for-like'
                             % (args.pano_id, old.width, old.height, new.width, new.height))
        left = band_window(old, args.x, args.y, win_w, win_h)
        right = band_window(new, args.x, args.y, win_w, win_h)

    labels = ('stored (fover era, %s scrape)  1:1' % scrape_month(old_path),
              're-fetched %s  1:1' % (args.probed or 'now'),
              'stored, %dx%d patch at %dx (nearest)' % (patch_w, patch_h, args.zoom),
              're-fetched, same patch at %dx (nearest)' % args.zoom)
    sheet = compose(left, right, patch_at, (patch_w, patch_h), args.zoom, labels)
    print('%s: %dx%d window at (%d, %d) of the bottom band; sheet %dx%d'
          % (args.pano_id, win_w, win_h, args.x, args.y, sheet.width, sheet.height))
    if args.write:
        os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
        sheet.save(args.write)
        print('wrote %s' % display_path(args.write, REPO))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
