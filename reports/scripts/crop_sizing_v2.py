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
3. What does the stored file cost? Two different questions, kept apart: what CropRunner writes
   (`stored_width_*`, where v2's cap makes near-field crops SMALLER than v1's, because v1 never
   resized anything) and what the webpage's unconditional resize-to-1440 does to a crop it is handed
   (`imagecontroller_*` — no code in this repo performs that resize; it is modelled because it is
   the path a formula cut takes to a Gallery card once SidewalkWebpage#4865 lands).
"""
import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import CropRunner  # noqa: E402
import crop_rule_v1  # noqa: E402

# The fill above which a crop reads as "too tight", from the absolute-judgement round of the human
# study (95% CI 0.46-0.54). Not a property of any rule - it is what the eye reported.
#
# One-sided by construction, and the report says so: the round returned ZERO "too wide" verdicts
# anywhere in the range tested (fills 0.12-1.79), so it bounds crops from below and nothing bounds
# them from above. `frac_clearing_too_tight` is therefore monotone in crop size - a rule returning a
# constant 90 deg window scores 1.00 - which is exactly the failure mode the report's own method
# lesson warns about. What actually bounds v2's window is CROP_MAX_FOV_DEG plus the forced-choice
# rounds' two-sided peak at fill 0.28-0.44; this number selects nothing on its own.
TOO_TIGHT_FILL = 0.49

# What ImageController does to a crop on write: getScaledInstance to 1440x960, unconditionally and
# without preserving aspect. This is NOT CropRunner's write path - CropRunner has never resized, and
# under v1 it wrote the cut window at its own size. It is modelled here because it is the path a
# formula-cut crop takes to a Gallery card once the server-side CropService lands
# (ProjectSidewalk/SidewalkWebpage#4865), which is the consumer that makes the stored width a
# question at all. Every figure derived from it is named `imagecontroller_*` so it cannot be read as
# a property of this repo's output.
IMAGECONTROLLER_STORED_WIDTH = 1440


def v1_side(pano_y, pano_height):
    """Sizing rule v1's size in native pixels. One definition, in crop_rule_v1."""
    return float(crop_rule_v1.predict_crop_size(pano_y, pano_height))


def v1_box(pano_x, pano_y, side, pano_width, pano_height):
    """v1's square window as (left, top, w, h), so it is shape-comparable with `v2_box`."""
    left, top, size = crop_rule_v1.compute_crop_box(pano_x, pano_y, side, pano_width, pano_height)
    return left, top, size, size


def v2_box(pano_x, pano_y, pano_width, pano_height):
    box = CropRunner.compute_crop_box(pano_x, pano_y,
                                      CropRunner.crop_window_width(pano_y, pano_width, pano_height),
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
    """The q-th percentile by nearest rank - an order statistic, with no interpolation.

    So `pct(xs, 50)` is a middle *observation* rather than the mean of the middle two on an even
    count. The difference is under a pixel on every figure here and the report calls these p50 rather
    than "the median" for that reason.
    """
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q / 100.0 * (len(ordered) - 1)))))]


def box_inside_window(ramp, left, top, w, h):
    """Is the gold apron actually inside the window that was cut? Position included, not just size.

    The distinction is not pedantic and it is the one this function exists to make. The window is
    centred on the label's stored POINT (`ramp['x']`, `ramp['y']`), while the box is centred on the
    apron (`box_cx`, `box_cy`), and those are different places — the point is a click at ground
    contact, the box is an extent. A check on dimensions alone ("would the apron fit in a window this
    big") passes for an apron sitting entirely outside its own crop, which is the case a containment
    number is supposed to be reporting.

    x is compared through the seam modulo, exactly as the window was cut: `left` is normalised into
    [0, pano_w) and a window may run off the right edge and continue at column 0, so the box's offset
    from `left` is taken modulo the pano width. That is well-defined as long as the box is not wider
    than the pano, which no apron is. y is a plain interval - the poles are not adjacent.
    """
    rel_left = (ramp['box_cx'] - ramp['box_w'] / 2.0 - left) % ramp['pano_w']
    if rel_left + ramp['box_w'] > w:
        return False
    box_top = ramp['box_cy'] - ramp['box_h'] / 2.0
    return top <= box_top and box_top + ramp['box_h'] <= top + h


def score(ramps):
    """Per-rule distributions for one set of gold ramps.

    Three families of key, deliberately named apart because they are about different things:

    * `window_deg_*`, `fill_*`, `frac_clearing_too_tight`, `containment`, `fits_by_size` — the crop
      as cut. These are properties of the sizing rule.
    * `stored_width_*` — what **CropRunner** writes. v1 wrote the window at its own size (it has never
      resized); v2 caps at CROP_MAX_STORED_WIDTH, so for a wide near-field window v2 stores FEWER
      pixels than v1 did, of more world. That trade is the honest cropper-to-cropper comparison.
    * `imagecontroller_*` — what the *webpage's* write path does to a crop, modelled. Nothing in this
      repo performs that resize; see IMAGECONTROLLER_STORED_WIDTH.
    """
    out = {'n': len(ramps)}
    for name in ('v1', 'v2'):
        degs, fills, stored, upsamples = [], [], [], []
        contained = fits_by_size = 0
        for ramp in ramps:
            if name == 'v1':
                left, top, w, h = v1_box(ramp['x'], ramp['y'],
                                         v1_side(ramp['y'], ramp['pano_h']),
                                         ramp['pano_w'], ramp['pano_h'])
                stored_width = w                       # v1 wrote the window, unresized
            else:
                left, top, w, h = v2_box(ramp['x'], ramp['y'], ramp['pano_w'], ramp['pano_h'])
                stored_width = min(w, CropRunner.CROP_MAX_STORED_WIDTH)
            degs.append(w / ramp['pano_h'] * 180.0)
            fills.append(ramp['box_w'] / w)
            stored.append(stored_width)
            upsamples.append(max(1.0, IMAGECONTROLLER_STORED_WIDTH / stored_width))
            contained += bool(box_inside_window(ramp, left, top, w, h))
            fits_by_size += bool(ramp['box_w'] <= w and ramp['box_h'] <= h)
        out[name] = {
            'window_deg_p10': pct(degs, 10), 'window_deg_p50': pct(degs, 50),
            'window_deg_p90': pct(degs, 90),
            'fill_p10': pct(fills, 10), 'fill_p50': pct(fills, 50), 'fill_p90': pct(fills, 90),
            'frac_clearing_too_tight': sum(1 for f in fills if f <= TOO_TIGHT_FILL) / len(fills),
            'containment': contained / len(ramps),
            'fits_by_size': fits_by_size / len(ramps),
            'stored_width_p10': pct(stored, 10), 'stored_width_p50': pct(stored, 50),
            'stored_width_p90': pct(stored, 90),
            'imagecontroller_median_upsample': pct(upsamples, 50),
            'imagecontroller_frac_upsampled_over_2x':
                sum(1 for u in upsamples if u > 2.0) / len(upsamples),
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
            w = 2 * h                                          # every corpus pano is 2:1
            v2_degs.append(CropRunner.crop_window_width(y, w, h) / h * 180.0)
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
                # Clamped to the panel. A box wider than the window runs off the right edge, and
                # Pillow silently drops a rectangle whose x1 < x0 rather than drawing it — so an
                # unclamped overflow makes the WORST case (v1's row-2 apron, fill 1.10) the one with
                # no gold outline at all, which is the opposite of what the figure is for.
                draw.rectangle([max(0, min(box_left, w - 1)), max(0, min(box_top, h - 1)),
                                max(0, min(box_left + example['box_w'], w - 1)),
                                max(0, min(box_top + example['box_h'], h - 1))],
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

    bad = [spec for spec in args.bundle if '=' not in spec]
    if bad:
        parser.error(f'--bundle wants CITY=PATH; got {bad}')
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
        # Per-city pano heights and provider ride along so the report's corpus table is checkable
        # against this artifact like everything else in it. Provider is inferred from the id shape,
        # which is what tells the two apart offline: Mapillary image ids are all-numeric (the #46
        # dtype trap), GSV pano ids are 22-char alphanumeric.
        'cities': {city: dict(score(ramps),
                              # How large the ramps themselves are, in the only unit that compares
                              # across resolutions. This is what a single global scale constant
                              # cannot serve: it is the whole reason Annapolis is under-sized.
                              ramp_width_deg_p50=pct([r['box_w'] / r['pano_w'] * 360.0
                                                      for r in ramps], 50),
                              pano_heights=sorted({r['pano_h'] for r in ramps}),
                              provider=('mapillary' if all(r['pano_id'].isdigit() for r in ramps)
                                        else 'gsv'))
                   for city, ramps in by_city.items() if ramps},
        'pooled': score(pooled),
        'resolution_invariance': resolution_invariance(heights),
        'pano_heights': heights,
    }

    for city, city_score in sorted(summary['cities'].items()):
        print("%-11s n=%3d | v1 %5.1f-%4.1f deg, fill p50 %.2f, contains %.3f "
              "| v2 %5.1f-%4.1f deg, fill p50 %.2f, contains %.3f"
              % (city, city_score['n'],
                 city_score['v1']['window_deg_p10'], city_score['v1']['window_deg_p90'],
                 city_score['v1']['fill_p50'], city_score['v1']['containment'],
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
                 'v1_window_px': e['v1'][2], 'v2_window_px': [e['v2'][2], e['v2'][3]],
                 # The two numbers the caption quotes per row, so the prose is transcribed from the
                 # artifact like every other figure in reports/.
                 'v1_fill': round(e['box_w'] / e['v1'][2], 3),
                 'v2_fill': round(e['box_w'] / e['v2'][2], 3),
                 'v1_window_deg': round(e['v1'][2] / e['pano_h'] * 180, 1),
                 'v2_window_deg': round(e['v2'][2] / e['pano_h'] * 180, 1)}
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
