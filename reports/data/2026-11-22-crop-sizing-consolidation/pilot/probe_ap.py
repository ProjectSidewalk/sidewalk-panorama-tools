"""Plan reviewer's probe for #186 (2026-11-22): can the committed tagger mAPs be reproduced from per_label
probabilities, or only from the logits? Read-only over a RampNet checkout. Reports only full-test mAP (already
committed in RampNet), nothing stratified.

Rewritten from the reviewer's scratch copy to take the RampNet checkout as an argument; the computation is
unchanged. Needs pandas.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_ap.py --rampnet <RampNet>
"""
import argparse
import json
import os

import numpy as np
import pandas as pd


def average_precision_score(y, s):
    """sklearn's definition: step-wise sum over DISTINCT thresholds (ties grouped), sum (R_n - R_{n-1}) P_n."""
    y = np.asarray(y, dtype=float)
    s = np.asarray(s, dtype=float)
    order = np.argsort(-s, kind="mergesort")
    y = y[order]
    s = s[order]
    last = np.r_[np.where(np.diff(s))[0], len(s) - 1]
    tps = np.cumsum(y)[last]
    fps = (last + 1) - tps
    prec = tps / (tps + fps)
    rec = tps / tps[-1]
    rec_prev = np.r_[0.0, rec[:-1]]
    return float(np.sum((rec - rec_prev) * prec))


parser = argparse.ArgumentParser()
parser.add_argument('--rampnet', required=True)
R = os.path.join(parser.parse_args().rampnet, 'analysis_out', 'context_fov_86')
TAGS = ["missing-tactile-warning", "narrow", "not-enough-landing-space", "not-level-with-street",
        "points-into-traffic", "pooled-water", "steep", "surface-problem"]
ARMS = {"control": ("control_per_label.csv", "control_test_predictions.csv", "control_scores.json"),
        "fov25": ("train_fov25_final_per_label.csv", "train_fov25_final_test_predictions.csv",
                  "train_fov25_final_scores.json"),
        "fov90": ("train_fov90_final_per_label.csv", "train_fov90_final_test_predictions.csv",
                  "train_fov90_final_scores.json")}
for arm, (pl, tp, sc) in ARMS.items():
    per = pd.read_csv(os.path.join(R, pl))
    pred = pd.read_csv(os.path.join(R, tp))
    committed = json.load(open(os.path.join(R, sc)))["subsets"]["full"]["fixed_tags"]["mAP"]
    m = per.merge(pred, on="filename", how="left", validate="one_to_one")
    a_prob, a_logit, zero_pos = [], [], 0
    for t in TAGS:
        y = m[t].to_numpy()
        a_prob.append(average_precision_score(y, m["prob:" + t].to_numpy()))
        z = m["logit:" + t].to_numpy(np.float32)
        s = (1.0 / (1.0 + np.exp(-z))).astype(np.float32)
        a_logit.append(average_precision_score(y, s))
        p = m["prob:" + t].to_numpy()
        zero_pos += int(((p == 0) & (y == 1)).sum() + ((p == 1) & (y == 0)).sum())
    print(arm, "rows", len(m), "committed", committed, "sk-from-probs", round(np.mean(a_prob), 6),
          "sk-from-logits", round(np.mean(a_logit), 6), "rows with p in {0,1} on the wrong side", zero_pos,
          "logit missing", int(m["logit:" + TAGS[0]].isna().sum()))
