"""Cut endpoint C's crop windows on the store host, for labels whose panos are not on this machine.

The boxes are NOT computed here: tilt_adjudicate.crop_jobs computes them locally with CropRunner's
own crop_window_width / compute_crop_box, so the production cut is the one being judged. This file
only extracts the pixels (a copy of CropRunner.extract_crop, which cannot be imported on the store
host - CropRunner pulls in the downloaders package) and downsizes each window to the sheet panel
with tilt_adjudicate.panel_image's rule. tests/test_tilt_adjudicate.py pins both copies: a sheet built
from these panels is pixel-identical to one cut locally.

**Runs on makelab2**: Python 3.9 / PIL 10, standard library + PIL only.

    python3 tilt_remote_crop.py --jobs crop_jobs.csv --store <store-root> --out panels/
"""

import argparse
import csv
import os
import sys

from PIL import Image

Image.MAX_IMAGE_PIXELS = 16384 * 8192
PANEL_W, PANEL_H = 480, 320     # tilt_adjudicate.PANEL_W / PANEL_H


def extract_crop(pano, left, top, width, height):
    """Copy of CropRunner.extract_crop: paste two segments when the window crosses the seam."""
    pano_width = pano.size[0]
    if left + width <= pano_width:
        return pano.crop((left, top, left + width, top + height))
    out = Image.new(pano.mode, (width, height))
    first_width = pano_width - left
    out.paste(pano.crop((left, top, pano_width, top + height)), (0, 0))
    out.paste(pano.crop((0, top, width - first_width, top + height)), (first_width, 0))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--jobs', required=True)
    ap.add_argument('--store', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    with open(args.jobs, newline='', encoding='utf-8') as f:
        jobs = list(csv.DictReader(f))
    by_pano = {}
    for j in jobs:
        by_pano.setdefault((j['city'], j['pano_id']), []).append(j)
    for (city, pid), js in sorted(by_pano.items()):
        with Image.open(os.path.join(args.store, city, pid[:2], pid + '.jpg')) as pano:
            pano.load()
            for j in js:
                crop = extract_crop(pano, int(j['left']), int(j['top']), int(j['width']), int(j['height']))
                panel = crop.convert('RGB').resize((PANEL_W, PANEL_H), Image.LANCZOS)
                panel.save(os.path.join(args.out, '%s_%s.png' % (j['token'], j['window'])))
    sys.stderr.write('done %d windows over %d panos\n' % (len(jobs), len(by_pano)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
