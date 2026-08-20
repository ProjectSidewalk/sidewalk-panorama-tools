#!/usr/bin/env python3
"""Regenerate `assets/banner.jpg`, the README's hero figure.

The figure is the repo's own pipeline applied to committed sample data: the equirectangular panorama in
`samples/sample_pano.jpg`, with the crop window that `CropRunner.crop_window_width` +
`CropRunner.compute_crop_box` produce for one label position drawn on it, beside the crop those functions
actually cut. Nothing in it is mocked up - re-run this script and the numbers in the captions move with the
code. The window is 3:2 because the cropper's is: the right-hand panel takes its aspect from
`CropRunner.CROP_ASPECT_W_OVER_H` rather than hardcoding one, so a change to the rule reshapes the figure
instead of quietly letterboxing the crop inside a stale panel.

    python3 assets/make_banner.py

Provenance of the label position: it is hand-picked, not a database row. `samples/sample_label.txt` holds two
legacy `sv_image_x/y` pairs, but they do not belong to this panorama (converted either way, they land on empty
roadway), and `samples/sample_crop.jpg` is a crop of some third, uncommitted pano. So for the figure we pick a
curb ramp visible in this pano by eye and let the code size the window around it - which is the part of the
pipeline the figure is claiming to show.

The output is **not** byte-reproducible across machines: `_font` picks the best face it can find, which is
DejaVu on Linux and Segoe UI on Windows, so the captions render differently. The geometry - the only thing
the figure is asserting - is identical either way. Regenerating on a different OS than the committed image
was built on is therefore a real but purely cosmetic diff.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import CropRunner  # noqa: E402  (needs the repo root on sys.path first)

PANO_PATH = os.path.join(REPO_ROOT, 'samples', 'sample_pano.jpg')
OUT_PATH = os.path.join(REPO_ROOT, 'assets', 'banner.jpg')

# A curb ramp in samples/sample_pano.jpg - see the provenance note in the module docstring.
LABEL_X, LABEL_Y = 1604, 3753

# Dark charcoal reads as deliberate against both GitHub themes; a white panel would glow in dark mode.
BG = (18, 20, 24)
PANEL = (30, 33, 39)
ACCENT = (255, 196, 61)      # crop window + connector
TEXT = (232, 234, 238)
MUTED = (150, 156, 166)

PAD = 22
PANO_W, PANO_H = 1000, 500   # the pano is 2:1 by construction (equirectangular)
CROP_W = 400
CROP_H = round(CROP_W / CropRunner.CROP_ASPECT_W_OVER_H)   # 3:2, from the cropper's own constant
CAPTION_H = 30


def _font(size, bold=False):
    """Best available sans font. The exact face does not matter; falling back must not crash the script."""
    candidates = [
        'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',
        r'C:\Windows\Fonts\segoeuib.ttf' if bold else r'C:\Windows\Fonts\segoeui.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold
        else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)     # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def build(out_path=OUT_PATH):
    """Render the banner to out_path. Parameterised so a test can drive the real thing into a tmp dir - with
    the destination hardcoded there was no way to exercise this without overwriting the committed figure."""
    Image.MAX_IMAGE_PIXELS = None                    # a real pano is well past Pillow's bomb ceiling
    pano = Image.open(PANO_PATH).convert('RGB')
    pano_w, pano_h = pano.size

    crop_width = CropRunner.crop_window_width(LABEL_Y, pano_h)
    box = CropRunner.compute_crop_box(LABEL_X, LABEL_Y, crop_width, pano_w, pano_h)
    crop = CropRunner.extract_crop(pano, box.left, box.top, box.width, box.height)

    width = PAD + PANO_W + PAD + CROP_W + PAD
    height = PAD + max(PANO_H, CROP_H) + CAPTION_H + PAD
    banner = Image.new('RGB', (width, height), BG)
    draw = ImageDraw.Draw(banner)

    # Left panel: the whole panorama, with the crop window on it. The window is ~30 px wide at this scale, so
    # it gets a dark outer stroke as well - a bare yellow box vanishes against sunlit pavement.
    banner.paste(pano.resize((PANO_W, PANO_H), Image.LANCZOS), (PAD, PAD))
    sx, sy = PANO_W / pano_w, PANO_H / pano_h
    x0, y0 = PAD + box.left * sx, PAD + box.top * sy
    x1, y1 = x0 + box.width * sx, y0 + box.height * sy
    draw.rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + 3], outline=BG, width=2)
    draw.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=ACCENT, width=2)
    # Crosshair on the label position itself: the window is centred on it, and that is the whole geometry.
    lx, ly = PAD + LABEL_X * sx, PAD + LABEL_Y * sy
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        draw.line([(lx + dx * 4, ly + dy * 4), (lx + dx * 9, ly + dy * 9)], fill=ACCENT, width=2)

    # Right panel: the crop those coordinates actually produce.
    # Centred against the pano panel rather than top-aligned: a 3:2 crop is shorter than the 2:1 pano, and
    # hanging it from the top leaves the dead space in one lump at the bottom.
    crop_x = PAD + PANO_W + PAD
    crop_y = PAD + (max(PANO_H, CROP_H) - CROP_H) // 2
    draw.rectangle([crop_x - 2, crop_y - 2, crop_x + CROP_W + 1, crop_y + CROP_H + 1], fill=PANEL)
    banner.paste(crop.resize((CROP_W, CROP_H), Image.LANCZOS), (crop_x, crop_y))
    draw.rectangle([crop_x - 2, crop_y - 2, crop_x + CROP_W + 1, crop_y + CROP_H + 1], outline=ACCENT, width=2)
    # No leader line between the two: any route from that window to this panel crosses the panorama itself.
    # The shared accent colour does the linking.

    small, small_b = _font(17), _font(17, bold=True)
    cap_y = PAD + max(PANO_H, CROP_H) + 7

    def caption(x, bold_part, rest):
        draw.text((x, cap_y), bold_part, font=small_b, fill=TEXT)
        draw.text((x + draw.textlength(bold_part + '  ', font=small_b), cap_y), rest, font=small, fill=MUTED)

    caption(PAD, 'DownloadRunner.py', f'stitched panorama - {pano_w} x {pano_h}')
    caption(crop_x, 'CropRunner.py', f'crop - {box.width} x {box.height} px')

    banner.save(out_path, quality=88, optimize=True, progressive=True)
    print(f'wrote {out_path}  ({banner.size[0]}x{banner.size[1]}, '
          f'{os.path.getsize(out_path) / 1024:.0f} KB)\n'
          f'  label ({LABEL_X}, {LABEL_Y}) -> window {crop_width:.1f} px wide -> {box}')


if __name__ == '__main__':
    build()
