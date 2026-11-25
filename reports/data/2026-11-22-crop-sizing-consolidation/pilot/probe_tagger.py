"""Planner's tagger pilot for #186 (2026-11-22), the input to PR-B's disclosure: fov25/fov50/fov90 mAP by depression
band, from RampNet's rounded per-label probabilities, with an AP that does NOT group tied scores (which is why
its full-test mAPs miss the committed ones at the fourth decimal; see probe_ap_repro.py).

Rewritten from the planner's scratch copy to take the RampNet checkout as an argument; the computation is
unchanged. Imports CropRunner from this checkout. Writes nothing.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_tagger.py --rampnet <RampNet>
"""
import argparse
import collections
import csv
import os
import sys

sys.dont_write_bytecode = True
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 4))
sys.path.insert(0, REPO_ROOT)
import numpy as np  # noqa: E402
import CropRunner  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--rampnet', required=True)
R = os.path.join(parser.parse_args().rampnet, 'analysis_out', 'context_fov_86')
TAGS = ["missing-tactile-warning", "narrow", "not-enough-landing-space", "not-level-with-street",
        "points-into-traffic", "pooled-water", "steep", "surface-problem"]


def ap(y, s):
    order = np.argsort(-s, kind="stable")
    y = y[order]
    tp = np.cumsum(y)
    prec = tp / (np.arange(len(y)) + 1)
    return float((prec * y).sum() / y.sum()) if y.sum() else None


def load(f):
    rows = list(csv.DictReader(open(os.path.join(R, f), encoding="utf-8")))
    return {(r["city"], r["label_id"]): r for r in rows}


def mAP(rows):
    aps = []
    for t in TAGS:
        y = np.array([int(r[t]) for r in rows])
        s = np.array([float(r["prob:" + t]) for r in rows])
        a = ap(y, s)
        if a is not None:
            aps.append(a)
    return sum(aps) / len(aps)


arms = {a: load(f) for a, f in [("control", "control_per_label.csv"), ("fov25", "train_fov25_final_per_label.csv"),
                                ("fov50", "train_fov50_final_per_label.csv"),
                                ("fov90", "train_fov90_final_per_label.csv")]}
for a, d in arms.items():
    print(a, len(d), round(mAP(list(d.values())), 6))
cut = {(r["city"], r["label_id"]): r for r in csv.DictReader(open(os.path.join(R, "cut_input.csv"), encoding="utf-8"))}
print("cut rows", len(cut), "test keys in cut", sum(k in cut for k in arms["fov25"]))
src = collections.Counter(r["pano_source"] for r in cut.values())
print(src)
deps = {}
for k, r in cut.items():
    h = float(r["pano_height"])
    deps[k] = CropRunner.label_depression_deg(float(r["pano_y"]), h)
keys = [k for k in arms["fov25"] if k in cut]
d = np.array([deps[k] for k in keys])
print("test depression p10/50/90", np.percentile(d, [10, 50, 90]).round(1))
for rule in ("v2", "v3"):
    f = np.array([CropRunner.crop_window_fov_deg(float(cut[k]["pano_y"]), float(cut[k]["pano_height"]),
                                                 sizing_rule=rule) for k in keys])
    print(rule, "fov p10/50/90", np.percentile(f, [10, 50, 90]).round(1), "share>=70", (f >= 70).mean().round(3),
          "share>=37.5", (f >= 37.5).mean().round(3), "share<37.5", (f < 37.5).mean().round(3))
for lo, hi in [(-90, 10), (10, 20), (20, 26.55), (26.55, 90)]:
    ks = [k for k in keys if lo <= deps[k] < hi]
    print((lo, hi), len(ks), {a: round(mAP([arms[a][k] for k in ks]), 3) for a in ("fov25", "fov50", "fov90")},
          "positives", {t: sum(int(arms["fov25"][k][t]) for k in ks) for t in TAGS})
nr = sum(1 for r in cut.values() if r["camera_roll"] not in ("", None))
npi = sum(1 for r in cut.values() if r["camera_pitch"] not in ("", None))
print("camera_roll present", nr, "camera_pitch present", npi)
