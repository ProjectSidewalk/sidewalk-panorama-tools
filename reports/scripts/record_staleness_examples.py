"""Visual examples for the record-staleness report: real pano imagery with three markers per label —
where the stale record renders (what Validate shows), where pano_x/pano_y says the click was
(the truth anchor), and where the repaired record renders (which must coincide with the truth).
The disagreement between the first two IS the detection; the coincidence of the last two IS the fix.

Exemplars are chosen deterministically from the committed repair CSVs (largest Validate-px error
first, round-robin over miss classes and cities), skipping panos Google no longer serves (~half,
per the photometa census) and seam-straddling layouts. Imagery is fetched live via streetlevel, so
this stage needs the network; the annotated crops + reports/data/<date>-record-staleness-examples.json
are committed and replay offline in the report.

Usage:
    python reports/scripts/record_staleness_examples.py reports/scripts/.cache/rawlabels \\
        --fetched <date> [--per-class 3] [--max-examples 10]
"""

import argparse
import glob
import gzip
import json
import os
import sys
import time

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.dirname(HERE)
DATE = '2026-08-10'  # the report family this addendum belongs to; the fetch date is recorded in the JSON
REPAIRS_GLOB = os.path.join(REPORTS, 'data', f'{DATE}-repairs-*.csv.gz')

CLASS_PRIORITY = ['x_only', 'multi_field', 'dpr2', 'zoom_desync', 'xy_small']
STITCH_ZOOM = 3  # streetlevel zoom: ~4096px wide for gen-3 panos — plenty for a marker crop
FETCH_PAUSE_S = 0.25

# CVD-safe, shape-coded: blue circle vs red-orange circle reads for deutan/protan viewers, and the
# repaired marker is a distinct SQUARE that must sit inside the truth circle.
TRUTH = (26, 115, 232)     # blue circle: pano_x/y, the click-time anchor
STALE = (217, 48, 37)      # red-orange circle: where the stale record renders
REPAIRED = (255, 214, 0)   # yellow square: where the repaired record renders

MIN_EXAMPLE_PX = 15  # a visual example must be visibly off; sub-perceptible rows prove nothing


def replay_px(heading, pitch, zoom, canvas_x, canvas_y, camera_heading, pano_w, pano_h):
    """The record's rendered position in pano pixels (the exact production projection)."""
    pov_h, pov_p = pov_replay.pov_if_centered(canvas_x, canvas_y, heading, pitch, zoom)
    px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, camera_heading, pano_w, pano_h)
    return float(px), float(py)


def pick_candidates(csv_dir, per_class, max_examples):
    """Deterministic exemplar queue: per miss class (priority order), the largest old Validate-px
    rows across all cities, interleaved so no city dominates."""
    rows = []
    for path in sorted(glob.glob(REPAIRS_GLOB)):
        city = os.path.basename(path).replace(f'{DATE}-repairs-', '').replace('.csv.gz', '')
        rep = pd.read_csv(gzip.open(path, 'rt'))
        raw_path = os.path.join(csv_dir, f'{city}.csv')
        if not os.path.exists(raw_path):
            continue
        raw = rawlabels.load_rawlabels(raw_path)
        merged = rep.merge(raw[['label_id', 'pano_id', 'label_type', 'pano_width', 'pano_height',
                                'camera_heading', 'pano_x', 'pano_y']], on='label_id', how='inner')
        merged['city'] = city
        rows.append(merged)
    allr = pd.concat(rows, ignore_index=True)

    queue = []
    for klass in CLASS_PRIORITY:
        # Magnitude first (an example must be visibly off), city diversity as tie-spread: rank
        # within city by px, then round-robin cities in descending-px order.
        sub = allr[(allr['klass'] == klass) & (allr['old_validate_px'] >= MIN_EXAMPLE_PX)]
        sub = sub.sort_values('old_validate_px', ascending=False)
        by_city = {c: g.reset_index(drop=True) for c, g in sub.groupby('city')}
        # Deep oversample: pano survival is ~50% overall and worse for the small dpr2/zoom
        # cohorts, whose rows cluster on a handful of sessions.
        for i in range(per_class * 12):
            for c in sorted(by_city, key=lambda c: -float(by_city[c]['old_validate_px'].iloc[0])):
                if i < len(by_city[c]):
                    queue.append(by_city[c].iloc[i])
    return queue, allr


def _font(size):
    """Pillow's bundled font at a readable size, falling back to the tiny bitmap default."""
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def add_caption_bar(crop, row):
    """A self-contained legend under the crop, so every image stays interpretable when pasted
    alone into an issue or slide: marker glyphs with their meanings, plus the label's identity
    and how far off its record renders. Text is measured and wrapped so nothing clips on
    narrow crops."""
    pad = 12
    title = (f"{row['city']} · label {int(row['label_id'])} ({row['label_type']}) · "
             f"{row['klass']} · record renders {row['old_validate_px']:.0f} px off in Validate")
    entries = [(STALE, 'ellipse', 'stale record (what Validate shows)'),
               (TRUTH, 'ellipse', 'pano_x/y (click-time truth)'),
               (REPAIRED, 'rect', 'after repair')]

    probe = ImageDraw.Draw(crop)
    avail = crop.width - 2 * pad

    # Title font: shrink until the whole line fits.
    title_size = 22
    while title_size > 10 and probe.textlength(title, font=_font(title_size)) > avail:
        title_size -= 1
    title_f = _font(title_size)

    # Legend entries: fixed readable size, wrapped onto as many rows as the width demands.
    leg_f = _font(16)
    r = 8
    glyph_w = 2 * r + 8
    rows, x = [[]], 0
    for entry in entries:
        w = glyph_w + probe.textlength(entry[2], font=leg_f) + 2 * pad
        if x + w > avail and rows[-1]:
            rows.append([])
            x = 0
        rows[-1].append(entry)
        x += w

    row_h = 30
    bar_h = pad + (title_size + 8) + len(rows) * row_h + pad // 2
    canvas = Image.new('RGB', (crop.width, crop.height + bar_h), (24, 24, 24))
    canvas.paste(crop, (0, 0))
    d = ImageDraw.Draw(canvas)
    d.text((pad, crop.height + pad), title, fill=(255, 255, 255), font=title_f)

    y = crop.height + pad + title_size + 8 + row_h // 2
    for line in rows:
        x = pad
        for color, shape, text in line:
            box = [x, y - r, x + 2 * r, y + r]
            if shape == 'rect':
                d.rectangle(box, outline=color, width=3)
            else:
                d.ellipse(box, outline=color, width=3)
            x += glyph_w
            d.text((x, y - 9), text, fill=(224, 224, 224), font=leg_f)
            x += probe.textlength(text, font=leg_f) + 2 * pad
        y += row_h
    return canvas


def render_example(row, img):
    """Annotated crop with the three markers; returns (PIL image, marker metadata dict)."""
    s = img.width / float(row['pano_width'])
    truth = (float(row['pano_x']) * s, float(row['pano_y']) * s)
    stale = replay_px(row['old_heading'], row['old_pitch'], row['old_zoom'],
                      row['old_canvas_x'], row['old_canvas_y'],
                      row['camera_heading'], row['pano_width'], row['pano_height'])
    repaired = replay_px(row['new_heading'], row['new_pitch'], row['new_zoom'],
                         row['new_canvas_x'], row['new_canvas_y'],
                         row['camera_heading'], row['pano_width'], row['pano_height'])
    stale = (stale[0] * s, stale[1] * s)
    repaired = (repaired[0] * s, repaired[1] * s)

    # Skip seam-straddling layouts; a wrapped crop would misread.
    if abs(truth[0] - stale[0]) > img.width / 4:
        return None, None

    # Crop framing: cover both markers with generous context, 3:2-ish, clamped to the image.
    cx = (truth[0] + stale[0]) / 2
    cy = (truth[1] + stale[1]) / 2
    span = max(abs(truth[0] - stale[0]), abs(truth[1] - stale[1]))
    half_w = min(max(span * 1.1, 340), img.width / 3)
    half_h = half_w * 2 / 3
    x0, x1 = int(max(0, cx - half_w)), int(min(img.width, cx + half_w))
    y0, y1 = int(max(0, cy - half_h)), int(min(img.height, cy + half_h))
    crop = img.crop((x0, y0, x1, y1)).convert('RGB')

    d = ImageDraw.Draw(crop)
    r = max(9, int((x1 - x0) * 0.014))

    def marker(pt, color, square=False):
        x, y = pt[0] - x0, pt[1] - y0
        box = [x - r, y - r, x + r, y + r]
        if square:
            d.rectangle(box, outline=color, width=3)
        else:
            d.ellipse(box, outline=color, width=3)
        d.line([x - r * 1.6, y, x + r * 1.6, y], fill=color, width=1)
        d.line([x, y - r * 1.6, x, y + r * 1.6], fill=color, width=1)

    d.line([truth[0] - x0, truth[1] - y0, stale[0] - x0, stale[1] - y0], fill=STALE, width=2)
    marker(stale, STALE)
    marker(repaired, REPAIRED, square=True)  # drawn under truth's circle; coincident by design
    marker(truth, TRUTH)

    meta = {'truth_xy': [round(v, 1) for v in truth], 'stale_xy': [round(v, 1) for v in stale],
            'repaired_xy': [round(v, 1) for v in repaired],
            'crop_box': [x0, y0, x1, y1], 'stitch_width': img.width}
    return add_caption_bar(crop, row), meta


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('csv_dir', help='directory of <city>.csv rawLabels exports')
    ap.add_argument('--fetched', metavar='DATE', required=True,
                    help='date of THIS imagery fetch (pano availability is a moving target)')
    ap.add_argument('--per-class', type=int, default=3)
    ap.add_argument('--max-examples', type=int, default=10)
    args = ap.parse_args()

    from streetlevel import streetview  # deferred: import needs the exiv2 native lib

    queue, _ = pick_candidates(args.csv_dir, args.per_class, args.max_examples)
    print(f'candidate queue: {len(queue)} rows')

    examples, per_class = [], {}
    pano_cache = {}
    for row in queue:
        if len(examples) >= args.max_examples:
            break
        if per_class.get(row['klass'], 0) >= args.per_class:
            continue
        pano_id = row['pano_id']
        if pano_id not in pano_cache:
            time.sleep(FETCH_PAUSE_S)
            try:
                pano = streetview.find_panorama_by_id(pano_id)
                pano_cache[pano_id] = streetview.get_panorama(pano, zoom=STITCH_ZOOM) if pano else None
            except Exception as e:
                print(f'  {pano_id}: fetch failed ({e})')
                pano_cache[pano_id] = None
        img = pano_cache[pano_id]
        if img is None:
            continue
        crop, meta = render_example(row, img)
        if crop is None:
            continue
        name = f"{DATE}-example-{row['city']}-{int(row['label_id'])}.jpg"
        out = os.path.join(REPORTS, 'figures', name)
        crop.save(out, quality=82)
        print(f'wrote {out} ({row["klass"]}, {row["old_validate_px"]:.0f} px off)')
        per_class[row['klass']] = per_class.get(row['klass'], 0) + 1
        examples.append({
            'figure': f'figures/{name}', 'city': row['city'], 'label_id': int(row['label_id']),
            'pano_id': pano_id, 'label_type': row['label_type'], 'klass': row['klass'],
            'old_validate_px': float(row['old_validate_px']),
            'old_record': {k: float(row[f'old_{k}']) for k in
                           ('heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y')},
            'new_record': {k: float(row[f'new_{k}']) for k in
                           ('heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y')},
            'stored_pano_xy': [float(row['pano_x']), float(row['pano_y'])],
            'camera_heading': float(row['camera_heading']),
            'pano_dims': [float(row['pano_width']), float(row['pano_height'])],
            **meta,
        })

    out_json = os.path.join(REPORTS, 'data', f'{DATE}-record-staleness-examples.json')
    with open(out_json, 'w') as f:
        json.dump({'fetched_imagery': args.fetched, 'stitch_zoom': STITCH_ZOOM,
                   'examples': examples}, f, indent=1, allow_nan=False)
    print(f'wrote {out_json} ({len(examples)} examples: {per_class})')


if __name__ == '__main__':
    main()
