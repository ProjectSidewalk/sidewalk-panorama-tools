"""Plan reviewer's probe for #186 (2026-11-22): which depressions do the tagger strata of PR-B (a window below
37.5 deg, 37.5-70 deg, 70 deg and above) correspond to? No tagger outcome is read.

Rewritten from the reviewer's scratch copy to import CropRunner from this checkout instead of an absolute path;
the computation is unchanged. Writes nothing.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_strata.py
"""
import os
import sys

sys.dont_write_bytecode = True
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 4))
sys.path.insert(0, REPO_ROOT)
import CropRunner  # noqa: E402

H = 8192
for rule in ("v2", "v3"):
    edges = {}
    for i in range(0, 9000):
        dep = i / 100.0
        y = H / 2 + CropRunner.elevation_deg_to_px(dep, H)
        f = CropRunner.crop_window_fov_deg(y, H, sizing_rule=rule)
        for t in (37.5, 70.0):
            if t not in edges and f >= t:
                edges[t] = dep
    print(rule, "window >= 37.5 deg from depression", edges.get(37.5), "; >= 70 deg from", edges.get(70.0))
print("v3 fov at 2 m / 2.3412 m camera height pairs, sanity: W/h dof", CropRunner.V3_CONTEXT_WIDTH_M)
