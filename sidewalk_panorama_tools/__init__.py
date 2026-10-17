"""Project Sidewalk panorama geometry, installable (docs/geometry.md).

Two modules:

* :mod:`sidewalk_panorama_tools.geometry` - a perspective (gnomonic) view of a label from its
  equirectangular panorama, with rig-tilt correction. :func:`~sidewalk_panorama_tools.geometry.label_view`
  is the one call a consumer makes.
* :mod:`sidewalk_panorama_tools.pose` - the #54 frames and the measured pose sign. It re-exports the
  repo-root module ``pano_pose`` (installed beside this package), which stays the one definition.

Usage::

    pip install "sidewalk-panorama-tools @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"

    from sidewalk_panorama_tools.geometry import label_view, render_view
    view, tilt = label_view(x_norm, y_norm, fov_h_deg=90.0, size=512, camera_pitch=1.8)
    crop = render_view(pano, view, tilt)
"""

__version__ = '1.0.0'
