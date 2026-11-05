"""Project Sidewalk panorama geometry, installable (docs/geometry.md).

Six modules:

* :mod:`sidewalk_panorama_tools.geometry` - a perspective (gnomonic) view of a label from its
  equirectangular panorama, with rig-tilt correction. :func:`~sidewalk_panorama_tools.geometry.label_view`
  is the one call a consumer makes.
* :mod:`sidewalk_panorama_tools.pose` - the #54 frames and the measured pose sign. It re-exports the
  repo-root module ``pano_pose`` (installed beside this package), which stays the one definition.
* :mod:`sidewalk_panorama_tools.store` - the pano store's layout (``<store>/<city>/<id[:2]>/<id>.jpg``, its
  ``.depth.npz`` and ``.w<N>.jpg`` display copy), the one store walker, the JPEG header reader, and
  :func:`~sidewalk_panorama_tools.store.read_pano`, which refuses a stored pano whose frame is not the
  label's (:class:`~sidewalk_panorama_tools.store.PanoDimsMismatch`) before decoding it.
* :mod:`sidewalk_panorama_tools.ps_math` - the Project Sidewalk front end's click and POV math
  (``calculatePovIfCentered``, ``calculatePanoXYFromPov`` and its inverse, ``get3dFov``) and the label
  distance blend, ported once (``reports/scripts/pov_replay.py`` re-exports it).
* :mod:`sidewalk_panorama_tools.labels` - validation-vote arithmetic with every policy explicit:
  ``label_uid`` (the ``city:label_id`` key), ``vote_counts`` (human-only by default), ``majority`` (a required
  tie policy) and ``is_validated`` (required thresholds).
* :mod:`sidewalk_panorama_tools.torch_geometry` - optional (the ``[torch]`` extra): the same sampling grid
  built with torch on any device, and the ``grid_sample`` normalization for a wrap-padded pano.

Usage::

    pip install "sidewalk-panorama-tools @ git+https://github.com/ProjectSidewalk/sidewalk-panorama-tools@<tag>"

    from sidewalk_panorama_tools.geometry import label_view, render_view
    view, tilt = label_view(x_norm, y_norm, fov_h_deg=90.0, size=512, camera_pitch=1.8)
    crop = render_view(pano, view, tilt)
"""

__version__ = '1.1.0'
