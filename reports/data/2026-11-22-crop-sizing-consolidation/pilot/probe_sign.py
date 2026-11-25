"""Plan reviewer's probe for #186 (2026-11-22): does pano_pose put the gravity horizon at
T(b) = pitch cos b + roll sin b ABOVE the rig raster's middle row, as RampNet's pano_tilt_estimate convention
(validated against the same records) does?

Rewritten from the reviewer's scratch copy to import pano_pose from this checkout instead of an absolute path;
the computation is unchanged. Writes nothing.

    python reports/data/2026-11-22-crop-sizing-consolidation/pilot/probe_sign.py
"""
import os
import sys

sys.dont_write_bytecode = True
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *[os.pardir] * 4))
sys.path.insert(0, REPO_ROOT)
import pano_pose  # noqa: E402

W, H = 16384, 8192
for b, p, r in ((0, 2.0, 0.0), (90, 0.0, 2.0), (180, 2.0, 0.0), (-90, 0.0, 2.0), (45, 1.0, 1.0)):
    x = (b + 180.0) / 360.0 * W
    rx, ry = pano_pose.rig_pixel_from_gravity_pixel(x, H / 2, W, H, p, r)
    above_deg = (H / 2 - float(ry)) / H * 180.0
    print("b=%4d pitch=%.1f roll=%.1f  horizon above middle by %.3f deg; T(b)=%.3f" %
          (b, p, r, above_deg, float(pano_pose.tilt_term_deg(b, p, r))))
print("wrap_deg(359.8) =", pano_pose.wrap_deg(359.8))
