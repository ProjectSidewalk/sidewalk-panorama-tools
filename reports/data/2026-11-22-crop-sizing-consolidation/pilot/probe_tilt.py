"""Planner's pilot for #186 (2026-11-22): is the tilt arm computable on the GSV gold, and how big is it?

Rewritten from the planner's scratch copy to take the RampNet checkout as an argument instead of absolute local
paths; the computation is unchanged. Imports CropRunner and pano_pose from this checkout. Writes nothing.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_tilt.py --rampnet <RampNet>
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 4))
sys.path.insert(0, REPO_ROOT)
import numpy as np  # noqa: E402
import pano_pose  # noqa: E402
import CropRunner  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--rampnet', required=True)
R = os.path.join(parser.parse_args().rampnet, 'benchmark')
for c in ['sao_paulo', 'paterson']:
    recs = {}
    for line in open(os.path.join(R, c, 'records.jsonl'), encoding='utf-8'):
        if line.strip():
            r = json.loads(line)
            recs[str(r['pano']['panorama_id'])] = r['pano']
    b = json.load(open(os.path.join(R, c, 'boxes.json'), encoding='utf-8'))
    T, dv2, dv3 = [], [], []
    for pid, items in b['panos'].items():
        p = recs[pid]
        for k, it in items.items():
            if it.get('status') != 'boxed' or 'point' not in it:
                continue
            w, h = p['width'], p['height']
            x, y = it['point']['x'] * w, it['point']['y'] * h
            pi = p['camera_pitch']
            ro = float(pano_pose.wrap_deg(p['camera_roll']))
            gx, gy = pano_pose.gravity_pixel_from_rig_pixel(x, y, w, h, 0.95 * pi, 0.95 * ro)
            T.append((y - float(gy)) / h * 180)
            for rule, acc in (('v2', dv2), ('v3', dv3)):
                a = CropRunner.crop_window_fov_deg(y, h, sizing_rule=rule)
                g = CropRunner.crop_window_fov_deg(float(gy), h, sizing_rule=rule)
                acc.append(g / a)
    T = np.abs(T)
    print(c, len(T), '|dy| deg p50/p90/max', np.percentile(T, [50, 90, 100]).round(2),
          'v2 window ratio (gravity-y / rig-y) p10/p90', np.percentile(dv2, [10, 90]).round(3),
          'v3', np.percentile(dv3, [10, 90]).round(3),
          'share moved >10% v2', (np.abs(np.array(dv2) - 1) > 0.1).mean().round(3),
          'v3', (np.abs(np.array(dv3) - 1) > 0.1).mean().round(3))
