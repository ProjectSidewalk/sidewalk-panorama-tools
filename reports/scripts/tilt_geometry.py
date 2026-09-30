"""The #54 tilt study's name for the one definition of every frame it uses - which lives in the production
module pano_pose.py at the repo root since #191, because CropRunner's tilt correction needs it and production
code cannot import from reports/scripts/. This file defines nothing: it re-exports pano_pose's geometry under
the names the study scripts and their tests have always used, so there is one definition and not a copy
(tests/test_pano_pose.py asserts every name here IS pano_pose's).

The conventions (bearing, elevation, pixels, the RFU frame, the depth artifact's z = DOWN frame, and the
measured F1 sign of pitch and roll) are stated in pano_pose's docstring.

**Runs on makelab2 as well as here**: upload pano_pose.py beside this file. With only the script directory
on sys.path the sibling copy is found; from the repo, the root (two levels up) is put ahead of it. Python 3.9
grammar (tests/test_tilt_pose_scan.py parses both files with `feature_version=(3, 9)`).
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.isfile(os.path.join(_REPO_ROOT, 'pano_pose.py')) and _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pano_pose import (  # noqa: E402,F401
    artifact_normal_bearing_elevation,
    bearing_elevation,
    bearing_elevation_from_pixel,
    direction_rfu,
    gravity_pixel_from_rig_pixel,
    gravity_to_rig,
    pitch_roll_to_xml_tilt,
    pixel_from_bearing_elevation,
    rig_from_gravity,
    rig_pixel_from_gravity_pixel,
    rig_to_gravity,
    tilt_term_deg,
    vertical_lean_deg,
    wrap_deg,
    xml_tilt_to_pitch_roll,
)

