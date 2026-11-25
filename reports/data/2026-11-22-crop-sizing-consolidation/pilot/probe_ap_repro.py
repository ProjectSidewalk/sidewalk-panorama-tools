"""Planner's reproduction of plan-review point 1 (2026-11-22). Full-test mAP only; no strata, no tilt.

Rewritten from the planner's scratch copy to take the RampNet checkout as an argument, with two dead lines of the
original (an unused loop and an unused list) dropped; the computation is unchanged. Writes nothing.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_ap_repro.py --rampnet <RampNet>
"""
import argparse
import collections
import csv
import json
import os

import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('--rampnet', required=True)
R = os.path.join(parser.parse_args().rampnet, 'analysis_out', 'context_fov_86')
TAGS = ["missing-tactile-warning", "narrow", "not-enough-landing-space", "not-level-with-street",
        "points-into-traffic", "pooled-water", "steep", "surface-problem"]


def ap_tied(y, s):
    """sklearn.average_precision_score: precision/recall at each DISTINCT threshold, sum (dR * P)."""
    order = np.argsort(-s, kind="stable")
    y = y[order]
    s = s[order]
    last = np.r_[np.where(np.diff(s))[0], len(s) - 1]          # last index of each tie group
    tp = np.cumsum(y)[last]
    n = last + 1
    prec = tp / n
    rec = tp / y.sum()
    return float(np.sum(np.diff(np.r_[0.0, rec]) * prec))


def ap_untied(y, s):
    order = np.argsort(-s, kind="stable")
    y = y[order]
    tp = np.cumsum(y)
    prec = tp / (np.arange(len(y)) + 1)
    return float((prec * y).sum() / y.sum())


ARMS = [("control", "control"), ("fov25", "train_fov25_final"), ("fov50", "train_fov50_final"),
        ("fov90", "train_fov90_final")]
for arm, stem in ARMS:
    pl = list(csv.DictReader(open(os.path.join(R, stem + "_per_label.csv"), encoding="utf-8")))
    lg = {r["filename"]: r for r in csv.DictReader(open(os.path.join(R, stem + "_test_predictions.csv"),
                                                         encoding="utf-8"))}
    assert len(pl) == len(lg) == 2182 and all(r["filename"] in lg for r in pl)
    committed = json.load(open(os.path.join(R, stem + "_scores.json"), encoding="utf-8"))
    res = collections.OrderedDict()
    for kind in ("logit", "prob_tied", "prob_untied"):
        aps = []
        for t in TAGS:
            y = np.array([int(r[t]) for r in pl])
            if kind == "logit":
                z = np.array([float(lg[r["filename"]]["logit:" + t]) for r in pl], dtype=np.float32)
                s = (1.0 / (1.0 + np.exp(-z.astype(np.float32)))).astype(np.float32)
                aps.append(ap_tied(y, s))
            else:
                s = np.array([float(r["prob:" + t]) for r in pl])
                aps.append(ap_tied(y, s) if kind == "prob_tied" else ap_untied(y, s))
        res[kind] = round(sum(aps) / len(aps), 6)
    wrong = sum(1 for r in pl if any((float(r["prob:" + t]) in (0.0, 1.0)) and (float(r["prob:" + t]) != int(r[t]))
                                     for t in TAGS))
    print(arm, dict(res), "rows with a rounded 0/1 prob on the wrong side:", wrong, "| scores.json top keys:",
          list(committed.keys())[:8])
