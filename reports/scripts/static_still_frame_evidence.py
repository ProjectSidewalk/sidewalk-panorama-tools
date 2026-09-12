"""SidewalkWebpage#3095: what the Street View Static API returns for a label's still, against the Explore frame.

Fetches a deployment's Gallery payload (the same `POST /label/labels` the Gallery page uses), keeps GSV labels
whose crop is the browser's Explore-canvas snapshot (`cropMarker` equals the canvas fraction, so the crop *is* the
labeling frame), prefers labels near the frame's edges, and for each pulls three images: the still the app's own
URL returns (requested at 720x480, served at 640x480), the same POV at 640x427, and the crop. Then renders two
figures per label: the three side by side with the marker at the canvas fraction on each, and
`ShareController.compositeMarker` emulated on each base (cover-scale onto 1440x960, icon at 4.5% of the width).

    python reports/scripts/static_still_frame_evidence.py --icons <SidewalkWebpage>/public/images/icons/label_type_icons \
        --out reports/scripts/.cache/static-still-3095 [--base https://sidewalk-sea.cs.washington.edu] [--n 6]

The Static API URL is re-requested at 640x427 without its signature (only `size` changes); the imagery is Google's,
so keep the output in the gitignored cache and publish only what a report needs.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from http.cookiejar import CookieJar

from PIL import Image, ImageDraw, ImageFont

CW, CH = 720, 480       # the Explore canvas
SW, SH = 640, 427       # Google's width cap at the canvas's aspect
BAND = (480 - SH) / 2   # extra rows Google adds above and below the frame in a 640x480 still
LABEL_TYPES = ('CurbRamp', 'NoCurbRamp', 'Obstacle', 'SurfaceProblem', 'Crosswalk', 'Signal', 'NoSidewalk')


def gallery_labels(base, n_per_type=60):
    """The Gallery payload for each label type: anon session, CSRF token from the page, then the JSON POST."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    opener.open(f'{base}/anonSignUp?url=%2F', timeout=60).read()
    page = opener.open(f'{base}/gallery', timeout=60).read().decode()
    token = re.search(r'name="csrfToken" value="([^"]+)"', page).group(1)
    seen = {}
    for t in LABEL_TYPES:
        body = json.dumps({'n': n_per_type, 'loaded_labels': [], 'label_types': [t],
                           'validation_options': ['correct', 'unvalidated', 'unsure', 'incorrect'],
                           'static_imagery_only': True}).encode()
        req = urllib.request.Request(f'{base}/label/labels', data=body, method='POST',
                                     headers={'Content-Type': 'application/json', 'Csrf-Token': token})
        for e in json.load(opener.open(req, timeout=120)).get('labelsOfType', []):
            seen[e['label']['label_id']] = e
    return opener, seen


def explore_frame_labels(seen):
    """Labels whose crop is the Explore snapshot: the recorded marker equals the canvas fraction."""
    out = []
    for e in seen.values():
        l, m = e['label'], e.get('cropMarker')
        if not (e.get('gsvImageUrl') and e.get('cropUrl') and m):
            continue
        if abs(m['x'] - l['canvas_x'] / CW) < 1e-6 and abs(m['y'] - l['canvas_y'] / CH) < 1e-6:
            out.append(e)
    # Nearest the top or bottom edge first: that is where the 640x480 still's error is largest.
    return sorted(out, key=lambda e: -abs(e['label']['canvas_y'] - CH / 2))


def download(opener, base, e, out):
    lid = e['label']['label_id']
    still = e['gsvImageUrl']
    unsigned = re.sub(r'size=\d+x\d+', f'size={SW}x{SH}', still.split('&signature=')[0])
    for name, url in ((f'{lid}_as_requested.jpg', still), (f'{lid}_640x427.jpg', unsigned),
                      (f'{lid}_crop.png', base + e['cropUrl'])):
        path = os.path.join(out, name)
        if not os.path.exists(path):
            with open(path, 'wb') as f:
                f.write(opener.open(url, timeout=60).read())


def _fonts():
    try:
        return (ImageFont.truetype('DejaVuSans.ttf', 15), ImageFont.truetype('DejaVuSans-Bold.ttf', 16))
    except OSError:
        return ImageFont.load_default(), ImageFont.load_default()


def panel(im, title, marks, note=None, frame=None, fonts=None):
    font, fontb = fonts
    im = im.convert('RGB').copy()
    dr = ImageDraw.Draw(im)
    if frame:
        dr.rectangle(frame, outline=(255, 255, 0), width=2)
    for x, y, color in marks:
        dr.ellipse([x - 14, y - 14, x + 14, y + 14], outline=color, width=3)
        dr.line([x - 22, y, x + 22, y], fill=color, width=1)
        dr.line([x, y - 22, x, y + 22], fill=color, width=1)
    out = Image.new('RGB', (im.width, im.height + 46), (255, 255, 255))
    out.paste(im, (0, 46))
    dr = ImageDraw.Draw(out)
    dr.text((6, 4), title, fill=(0, 0, 0), font=fontb)
    if note:
        dr.text((6, 24), note, fill=(60, 60, 60), font=font)
    return out


def strip(panels, header, path, fonts):
    W = sum(p.width for p in panels) + 10 * (len(panels) + 1)
    H = max(p.height for p in panels) + 40
    out = Image.new('RGB', (W, H), (255, 255, 255))
    x = 10
    for p in panels:
        out.paste(p, (x, 34))
        x += p.width + 10
    ImageDraw.Draw(out).text((10, 8), header, fill=(0, 0, 0), font=fonts[1])
    out.save(path, quality=88)


def share(base, icon, fx, fy):
    """ShareController.compositeMarker: cover-scale onto 1440x960, icon at the marker fraction of the scaled base."""
    OW, OH = 1440, 960
    scale = max(OW / base.width, OH / base.height)
    sw, sh = round(base.width * scale), round(base.height * scale)
    ox, oy = (sw - OW) // 2, (sh - OH) // 2
    out = Image.new('RGB', (OW, OH))
    out.paste(base.convert('RGB').resize((sw, sh), Image.BILINEAR), (-ox, -oy))
    w = max(24, int(OW * 0.045))
    ic = icon.resize((w, int(icon.height * w / icon.width)), Image.LANCZOS)
    cx, cy = int(fx * sw) - ox, int(fy * sh) - oy
    out.paste(ic, (cx - ic.width // 2, cy - ic.height // 2), ic)
    return out, (cx, cy)


def render(e, out, city, icons, fonts):
    l = e['label']
    lid, t, cx, cy = l['label_id'], l['label_type'], l['canvas_x'], l['canvas_y']
    fx, fy = cx / CW, cy / CH
    s480 = Image.open(os.path.join(out, f'{lid}_as_requested.jpg'))
    s427 = Image.open(os.path.join(out, f'{lid}_640x427.jpg'))
    crop_full = Image.open(os.path.join(out, f'{lid}_crop.png'))
    crop = crop_full.resize((SW, SH), Image.LANCZOS)
    red, blue = (230, 30, 30), (40, 90, 255)
    naive, truth = (fx * s480.width, fy * s480.height), (fx * SW, BAND + fy * SH)
    frame_err = abs(naive[1] - truth[1])
    strip([
        panel(s480, f'Today: size=720x480 requested, Google returns {s480.width}x{s480.height}',
              [(*naive, red), (*truth, blue)], frame=(0, BAND, SW - 1, BAND + SH), fonts=fonts,
              note='yellow: the Explore frame.  red: marker at the canvas fraction.  blue: the feature'),
        panel(s427, f'Fix: size={SW}x{SH}', [(fx * SW, fy * SH, red)], fonts=fonts,
              note='the same canvas fraction lands on the feature'),
        panel(crop, f'Ground truth: the Explore canvas snapshot (crop_{lid}.png)', [(fx * SW, fy * SH, red)],
              fonts=fonts, note='the frame the labeler saw, same fraction'),
    ], f'{city}:{lid}  {t}  canvas ({cx}, {cy})  zoom {round(l["zoom"], 2)}  --  naive marker on the 640x480 '
       f'still: {frame_err:.1f} px off vertically (of 480)', os.path.join(out, f'fig_frame_{lid}.jpg'), fonts)

    icon = Image.open(os.path.join(icons, f'{t}_small.png')).convert('RGBA')
    before, pb = share(s480, icon, fx, fy)
    after, _ = share(s427, icon, fx, fy)
    from_crop, _ = share(crop_full, icon, fx, fy)
    scale = 1440 / s480.width
    true_y = (BAND + fy * SH) * scale - ((round(s480.height * scale) - 960) // 2)
    share_err = abs(pb[1] - true_y)
    strip([panel(im.resize((720, 480), Image.LANCZOS), title, [], note=note, fonts=fonts) for im, title, note in (
        (before, f'Share image today (still fallback): base {s480.width}x{s480.height}',
         f'icon drawn {share_err:.0f} px from the feature (on the 1440x960 canvas)'),
        (after, f'Share image with the fix: base {SW}x{SH}', ''),
        (from_crop, 'Share image from the Explore crop (ground truth)', ''))],
        f'{city}:{lid}  {t}  canvas ({cx}, {cy})  --  ShareController.compositeMarker, emulated with the same '
        'cover-scale and icon rule', os.path.join(out, f'fig_share_{lid}.jpg'), fonts)
    return frame_err, share_err


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--base', default='https://sidewalk-sea.cs.washington.edu')
    ap.add_argument('--city', default='seattle-wa', help='city id, for the figure headers')
    ap.add_argument('--icons', required=True, help="SidewalkWebpage's public/images/icons/label_type_icons")
    ap.add_argument('--out', required=True)
    ap.add_argument('--n', type=int, default=6, help='how many edge-most labels to render')
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    opener, seen = gallery_labels(args.base)
    picks = explore_frame_labels(seen)[:args.n]
    print(f'{len(seen)} labels in the payload, {len(picks)} rendered', file=sys.stderr)
    fonts = _fonts()
    print('| label | type | canvas (x, y) | naive marker error on the 640x480 still | share-image icon error today |')
    print('|---|---|---|---:|---:|')
    for e in picks:
        download(opener, args.base, e, args.out)
        fe, se = render(e, args.out, args.city, args.icons, fonts)
        l = e['label']
        print(f'| {args.city}:{l["label_id"]} | {l["label_type"]} | ({l["canvas_x"]}, {l["canvas_y"]}) '
              f'| {fe:.1f} px of 480 | {se:.0f} px of 960 |')


if __name__ == '__main__':
    main()
