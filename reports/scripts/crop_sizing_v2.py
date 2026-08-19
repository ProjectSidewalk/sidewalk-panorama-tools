"""Sizing rule v1 vs v2, scored against hand-drawn curb-ramp extents in four cities.

The gold is whole-apron boxes drawn on adjudicated benchmark ramps (RampNet #114/#116) across two
imagery providers and pano heights from 2048 to 16384 px. It does not live in this repo - it is the
RampNet benchmark's, and the panoramas behind it are archive-anchored - so this script takes the
bundle roots as arguments and commits its summary, which the tests pin.

    python3 reports/scripts/crop_sizing_v2.py \
        --bundle richmond=/path/RampNet/benchmark/richmond \
        --bundle sao_paulo=/path/RampNet/benchmark/sao_paulo \
        --write reports/data/2026-08-19-crop-sizing-v2.json \
        --figure reports/figures/2026-08-19-crop-sizing-examples.jpg

Three questions, in the order the report answers them:

1. Is a window an ANGLE? Under v1 the same ramp at the same depression asks for a window whose
   angular size depends on the pano's pixel height. That is the defect; the spread across heights
   measures it.
2. How much of the frame does the ramp occupy? The "fill" ratio, against the acceptance threshold
   the human study measured.
3. What does the stored file cost? Under the old always-scale-to-1440 write path most crops are
   upscales; v2 caps at 1440 instead.
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import CropRunner  # noqa: E402

# The fill above which a crop reads as "too tight", from the absolute-judgement round of the human
# study (95% CI 0.46-0.54). Not a property of any rule - it is what the eye reported.
TOO_TIGHT_FILL = 0.49


def v1_side(pano_y, pano_height):
    """Sizing rule v1, frozen: native pixels straight into constants fit on 6656-px panos."""
    distance = max(0.0, 19.80546390 + 0.01523952 * (pano_height / 2 - pano_y))
    size = 8725.6 * distance ** -1.192 if distance > 0 else 0.0
    if size > 1500 or distance == 0:
        size = 1500.0
    return max(size, 50.0)


def v1_box(pano_x, pano_y, side, pano_width, pano_height):
    """v1's square window. Same seam wrap and vertical shift as v2 - only the shape differs."""
    size = min(int(round(side)), pano_width, pano_height)
    left = int(round(pano_x - size / 2)) % pano_width
    ideal_top = int(round(pano_y - size / 2))
    return left, max(0, min(ideal_top, pano_height - size)), size, size


def v2_box(pano_x, pano_y, pano_width, pano_height):
    box = CropRunner.compute_crop_box(pano_x, pano_y,
                                      CropRunner.crop_window_width(pano_y, pano_height),
                                      pano_width, pano_height)
    return box.left, box.top, box.width, box.height


def load_bundle(root):
    """The boxed gold ramps of one benchmark bundle, in native pixels."""
    with open(os.path.join(root, 'boxes.json'), encoding='utf-8') as f:
        boxes = json.load(f)
    records = {}
    with open(os.path.join(root, 'records.jsonl'), encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                rec = json.loads(line)
                records[str(rec['pano']['panorama_id'])] = rec['pano']

    ramps = []
    for pano_id, items in boxes.get('panos', {}).items():
        pano = records.get(str(pano_id))
        if pano is None:
            continue
        pano_w, pano_h = int(pano['width']), int(pano['height'])
        for key, item in items.items():
            if item.get('status') != 'boxed' or 'point' not in item:
                continue
            ramps.append({
                'pano_id': str(pano_id), 'key': key, 'pano_w': pano_w, 'pano_h': pano_h,
                'x': item['point']['x'] * pano_w, 'y': item['point']['y'] * pano_h,
                'box_w': item['w'] * pano_w, 'box_h': item['h'] * pano_h,
                'box_cx': item['cx'] * pano_w, 'box_cy': item['cy'] * pano_h,
                'depression_deg': (item['point']['y'] - 0.5) * 180.0,
            })
    return ramps


def pct(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q / 100.0 * (len(ordered) - 1)))))]


def score(ramps):
    """Per-rule distributions for one set of gold ramps."""
    out = {'n': len(ramps)}
    for name in ('v1', 'v2'):
        degs, fills, upsamples, contained = [], [], [], 0
        for ramp in ramps:
            if name == 'v1':
                _, _, w, h = v1_box(ramp['x'], ramp['y'], v1_side(ramp['y'], ramp['pano_h']),
                                    ramp['pano_w'], ramp['pano_h'])
                # The old write path scaled every crop to 1440 wide, upscaling whatever was narrower.
                upsamples.append(max(1.0, CropRunner.CROP_MAX_STORED_WIDTH / w))
            else:
                _, _, w, h = v2_box(ramp['x'], ramp['y'], ramp['pano_w'], ramp['pano_h'])
                upsamples.append(1.0)  # v2 stores min(window, 1440); it never upscales.
            degs.append(w / ramp['pano_h'] * 180.0)
            fills.append(ramp['box_w'] / w)
            if ramp['box_w'] <= w and ramp['box_h'] <= h:
                contained += 1
        out[name] = {
            'window_deg_p10': pct(degs, 10), 'window_deg_p50': pct(degs, 50),
            'window_deg_p90': pct(degs, 90),
            'fill_p10': pct(fills, 10), 'fill_p50': pct(fills, 50), 'fill_p90': pct(fills, 90),
            'frac_clearing_too_tight': sum(1 for f in fills if f <= TOO_TIGHT_FILL) / len(fills),
            'containment': contained / len(ramps),
            'median_upsample': pct(upsamples, 50),
            'frac_upsampled_over_2x': sum(1 for u in upsamples if u > 2.0) / len(upsamples),
        }
    return out


def resolution_invariance(pano_heights):
    """The headline claim, as a table: one ramp geometry, every pano height in the corpus.

    At a fixed depression angle nothing about the world has changed, so the window should not change
    either. Under v1 it swings with the pano's pixel count - and swings the wrong way, since the
    largest panos get the tightest crops.
    """
    rows = []
    for dep in (5.0, 10.0, 20.0):
        v1_degs, v2_degs = [], []
        for h in pano_heights:
            y = h / 2 + dep / 180.0 * h
            v1_degs.append(v1_side(y, h) / h * 180.0)
            v2_degs.append(CropRunner.crop_window_width(y, h) / h * 180.0)
        rows.append({'depression_deg': dep, 'pano_heights': list(pano_heights),
                     'v1_window_deg': v1_degs, 'v2_window_deg': v2_degs,
                     'v1_spread': max(v1_degs) / min(v1_degs),
                     'v2_spread': max(v2_degs) / min(v2_degs)})
    return rows


def pick_examples(by_city, bundles, per_city=2):
    """Gold ramps spanning the depression range, restricted to panos actually on disk."""
    chosen = []
    for city in sorted(by_city):
        pano_dir = os.path.join(bundles[city], 'panos')
        have = [r for r in by_city[city]
                if os.path.exists(os.path.join(pano_dir, r['pano_id'] + '.jpg'))]
        if not have:
            continue
        have.sort(key=lambda r: r['depression_deg'])
        picks, seen = [], set()
        for idx in (len(have) // 6, len(have) - 1 - len(have) // 8):
            if 0 <= idx < len(have) and idx not in seen:
                seen.add(idx)
                picks.append(have[idx])
        for ramp in picks[:per_city]:
            example = dict(ramp, city=city,
                           pano_path=os.path.join(pano_dir, ramp['pano_id'] + '.jpg'))
            example['v1'] = v1_box(ramp['x'], ramp['y'], v1_side(ramp['y'], ramp['pano_h']),
                                   ramp['pano_w'], ramp['pano_h'])
            example['v2'] = v2_box(ramp['x'], ramp['y'], ramp['pano_w'], ramp['pano_h'])
            chosen.append(example)
    return chosen


def render_examples(examples, path, panel_w=380):
    """One row per ramp: what v1 cut, what v2 cuts, the gold apron outlined in both.

    Both panels are drawn at the same width on purpose. A Gallery card is a fixed box, so a wider
    window is not "a bigger picture" - it is more context at lower magnification, and that trade is
    the thing to look at.
    """
    from PIL import Image, ImageDraw

    CropRunner.raise_decompression_bomb_ceiling()
    label_h, pad = 30, 10
    rows = []
    for example in examples:
        pano = Image.open(example['pano_path'])
        try:
            panels = []
            for left, top, w, h in (example['v1'], example['v2']):
                crop = CropRunner.extract_crop(pano, left, top, w, h)
                draw = ImageDraw.Draw(crop)
                # The gold apron in crop coordinates - x through the seam, as the window was cut.
                box_left = ((example['box_cx'] - example['box_w'] / 2) - left) % example['pano_w']
                box_top = (example['box_cy'] - example['box_h'] / 2) - top
                draw.rectangle([box_left, box_top,
                                box_left + example['box_w'], box_top + example['box_h']],
                               outline=(255, 205, 0), width=max(2, w // 200))
                scale = panel_w / crop.size[0]
                panels.append(crop.resize((panel_w, max(1, int(round(crop.size[1] * scale)))),
                                          Image.LANCZOS))
            rows.append((example, panels))
        finally:
            pano.close()

    # Row heights follow their own panels: v1's square window is taller than v2's 3:2 one at the
    # same display width, and a uniform row height would pad every short row with dead space.
    row_heights = [max(p.size[1] for p in panels) + label_h for _, panels in rows]
    sheet = Image.new('RGB', (panel_w * 2 + pad * 3, sum(row_heights) + pad), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for i, (example, panels) in enumerate(rows):
        top = sum(row_heights[:i]) + pad
        for j, panel in enumerate(panels):
            sheet.paste(panel, (pad + j * (panel_w + pad), top))
        draw.text(
            (pad, top + max(p.size[1] for p in panels) + 6),
            "%s %s  %dx%d  %.0f deg below horizon   v1: %d px square, %.1f deg, fill %.2f   "
            "v2: %dx%d px, %.1f deg, fill %.2f"
            % (example['city'], example['pano_id'][:12], example['pano_w'], example['pano_h'],
               example['depression_deg'],
               example['v1'][2], example['v1'][2] / example['pano_h'] * 180,
               example['box_w'] / example['v1'][2],
               example['v2'][2], example['v2'][3], example['v2'][2] / example['pano_h'] * 180,
               example['box_w'] / example['v2'][2]),
            fill=(20, 20, 20))
    sheet.save(path, quality=88)
    return sheet.size


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--bundle', action='append', required=True, metavar='CITY=PATH',
                        help='benchmark bundle with boxes.json + records.jsonl (repeatable)')
    parser.add_argument('--write', help='where to write the summary JSON')
    parser.add_argument('--figure', help='where to write the example contact sheet')
    args = parser.parse_args(argv)

    bundles = dict(spec.split('=', 1) for spec in args.bundle)
    by_city = {city: load_bundle(path) for city, path in bundles.items()}
    pooled = [ramp for ramps in by_city.values() for ramp in ramps]
    if not pooled:
        parser.error('no boxed gold ramps found in the given bundles')
    heights = sorted({ramp['pano_h'] for ramp in pooled})

    summary = {
        'rule_version': CropRunner.CROP_RULE_VERSION,
        'too_tight_fill': TOO_TIGHT_FILL,
        'constants': {'scale': CropRunner.CROP_SIZE_SCALE,
                      'min_fov_deg': CropRunner.CROP_MIN_FOV_DEG,
                      'max_fov_deg': CropRunner.CROP_MAX_FOV_DEG,
                      'aspect_w_over_h': CropRunner.CROP_ASPECT_W_OVER_H,
                      'max_stored_width': CropRunner.CROP_MAX_STORED_WIDTH},
        'cities': {city: score(ramps) for city, ramps in by_city.items() if ramps},
        'pooled': score(pooled),
        'resolution_invariance': resolution_invariance(heights),
        'pano_heights': heights,
    }

    for city, city_score in sorted(summary['cities'].items()):
        print("%-11s n=%3d | v1 %5.1f-%4.1f deg, fill p50 %.2f, %3.0f%% upscaled >2x "
              "| v2 %5.1f-%4.1f deg, fill p50 %.2f, contains %.3f"
              % (city, city_score['n'],
                 city_score['v1']['window_deg_p10'], city_score['v1']['window_deg_p90'],
                 city_score['v1']['fill_p50'], 100 * city_score['v1']['frac_upsampled_over_2x'],
                 city_score['v2']['window_deg_p10'], city_score['v2']['window_deg_p90'],
                 city_score['v2']['fill_p50'], city_score['v2']['containment']))
    print("\nresolution invariance (same ramp geometry at every pano height in the corpus):")
    for row in summary['resolution_invariance']:
        print("  %2.0f deg below horizon: v1 spans %.2fx across heights, v2 spans %.2fx"
              % (row['depression_deg'], row['v1_spread'], row['v2_spread']))

    if args.figure:
        examples = pick_examples(by_city, bundles)
        if examples:
            size = render_examples(examples, args.figure)
            summary['figure_examples'] = [
                {'city': e['city'], 'pano_id': e['pano_id'], 'key': e['key'],
                 'pano_w': e['pano_w'], 'pano_h': e['pano_h'],
                 'depression_deg': round(e['depression_deg'], 2),
                 'v1_window_px': e['v1'][2], 'v2_window_px': [e['v2'][2], e['v2'][3]]}
                for e in examples]
            print("\nwrote %s %sx%s" % (args.figure, size[0], size[1]))
        else:
            print("\nno panos on disk in the given bundles; skipping the figure")

    if args.write:
        with open(args.write, 'w', encoding='utf-8') as f:
            json.dump(summary, f, indent=1, sort_keys=True)
        print("wrote %s" % args.write)
    return 0


if __name__ == '__main__':
    sys.exit(main())
