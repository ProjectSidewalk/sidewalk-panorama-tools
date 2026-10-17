"""The #54 frames and the measured pose sign, as part of the installable package.

A re-export, not a copy: the definitions live in the repo-root module ``pano_pose`` (read its module
docstring for every convention - bearing, elevation, pixels, the RFU frame, and the measured sign
"pitch > 0 = nose down, roll > 0 = left side up, so a gravity-horizontal direction at bearing b has rig
elevation +T(b) = +(pitch cos b + roll sin b)"). ``pano_pose`` stays the home because it is also uploaded to
makelab2 on its own, beside reports/scripts/tilt_geometry.py, and must import there with nothing else from
the repo; pyproject.toml installs it as a top-level module beside this package, so this import works in a
checkout and in an install alike. tests/test_geometry.py asserts every name here IS pano_pose's.

One difference for an installed consumer: ``resolve_pano_pose`` imports ``downloaders.gsv`` when called
without ``depth_suffix``, and ``downloaders`` is not installed. Pass the suffix (``'.depth.npz'``).

``pano_pose`` installs under a generic top-level name, so this module checks its ``PANO_POSE_FINGERPRINT``
on import and raises ImportError when another ``pano_pose`` shadows the installed one.
"""

import pano_pose as _pano_pose

#: The pano_pose this package was written against (pano_pose.PANO_POSE_FINGERPRINT).
EXPECTED_PANO_POSE_FINGERPRINT = 'sidewalk-panorama-tools/pano_pose/rfu-pitch-nose-down-roll-left-up/1'

if getattr(_pano_pose, 'PANO_POSE_FINGERPRINT', None) != EXPECTED_PANO_POSE_FINGERPRINT:
    raise ImportError(
        'sidewalk_panorama_tools.pose imported a pano_pose that is not this package\'s: %r (fingerprint %r, '
        'expected %r). A stray pano_pose.py earlier on sys.path is shadowing the installed one.'
        % (getattr(_pano_pose, '__file__', None), getattr(_pano_pose, 'PANO_POSE_FINGERPRINT', None),
           EXPECTED_PANO_POSE_FINGERPRINT))

from pano_pose import (  # noqa: E402,F401
    PANO_POSE_FINGERPRINT,
    POSE_SOURCE_NPZ,
    POSE_SOURCE_XML,
    XML_SUFFIX,
    PanoPose,
    artifact_normal_bearing_elevation,
    bearing_elevation,
    bearing_elevation_from_pixel,
    corrected_pixel,
    direction_rfu,
    gravity_pixel_from_rig_pixel,
    gravity_to_rig,
    pitch_roll_to_xml_tilt,
    pixel_from_bearing_elevation,
    pose_from_depth_artifact,
    pose_from_xml,
    resolve_pano_pose,
    rig_from_gravity,
    rig_pixel_from_gravity_pixel,
    rig_to_gravity,
    tilt_term_deg,
    vertical_lean_deg,
    wrap_deg,
    xml_tilt_to_pitch_roll,
)

__all__ = [
    'PANO_POSE_FINGERPRINT', 'POSE_SOURCE_NPZ', 'POSE_SOURCE_XML', 'XML_SUFFIX', 'PanoPose',
    'artifact_normal_bearing_elevation', 'bearing_elevation', 'bearing_elevation_from_pixel', 'corrected_pixel',
    'direction_rfu', 'gravity_pixel_from_rig_pixel', 'gravity_to_rig', 'pitch_roll_to_xml_tilt',
    'pixel_from_bearing_elevation', 'pose_from_depth_artifact', 'pose_from_xml', 'resolve_pano_pose',
    'rig_from_gravity', 'rig_pixel_from_gravity_pixel', 'rig_to_gravity', 'tilt_term_deg', 'vertical_lean_deg',
    'wrap_deg', 'xml_tilt_to_pitch_roll',
]
