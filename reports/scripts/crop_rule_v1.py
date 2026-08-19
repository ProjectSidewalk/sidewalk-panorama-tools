"""Crop sizing rule v1, frozen — the geometry every report before 2026-08-19 was measured under.

`CropRunner` ships rule v2 (resolution-normalised, x2.5, angular clamps, 3:2 window; see
reports/2026-08-19-crop-sizing-v2.md). Three places still need v1: the crop-sizing study compares
against it, and the clamp and crop-geometry censuses were *run* under it, so their replica-fidelity
tests have to pin against the rule that produced their numbers rather than against the deployed
function. Each of those three had its own hand-copied transcription, which is one definition too many
by the standard this repo already applies to `studyfmt` and `region_tag_mask` — three copies of a
frozen constant agree until one of them is edited, and nothing would fail.

They also stay guards. The census replicas in `clamp_census.py` and `crop_geometry_census.py` are
vectorized re-implementations, so pinning them against this module still compares two independent
implementations; what it no longer does is compare either of them against a formula that has moved.

**Nothing here may be "fixed".** It is a record of what was deployed, not a rule anyone runs. The
resolution defect in `_v1_size` — native pixels fed into constants fit on 6656-px panoramas — is the
whole subject of the v2 report and has to stay exactly as it was.

NOTE FOR ANY RE-USE: findings that depend on the window's SIZE are v1 findings and do not carry over
to current crops. The seam-crossing rate in particular is a function of window width, and v2's windows
are ~2.5x wider — re-run the census before citing that number against anything cut today.
"""


def predict_crop_size(pano_y, pano_height):
    """v1's crop size in native pixels: the 2013 regression, fed raw pano pixels at any resolution.

    Transcribed from CropRunner.py as of #77, before rule v2. Two experimentally determined steps —
    a linear map from the label's offset above the horizon to a camera-to-label distance, then a power
    law from that distance to a size, clamped to [50, 1500].
    """
    old_pano_y = pano_height / 2 - pano_y
    crop_size = 0
    distance = max(0, 19.80546390 + 0.01523952 * old_pano_y)
    if distance > 0:
        crop_size = 8725.6 * (distance ** -1.192)
    if crop_size > 1500 or distance == 0:
        crop_size = 1500
    if crop_size < 50:
        crop_size = 50
    return crop_size


def compute_crop_box(pano_x, pano_y, crop_size, pano_width, pano_height):
    """v1's SQUARE window: (left, top, size). The seam wrap and the vertical shift are unchanged in
    v2 — only the shape and the size are — so this differs from the deployed function in exactly the
    two ways the v2 report is about.

    :return: (left, top, size), integers, with left normalised into [0, pano_width).
    """
    size = min(int(round(crop_size)), pano_width, pano_height)
    left = int(round(pano_x - size / 2)) % pano_width
    ideal_top = int(round(pano_y - size / 2))
    return left, max(0, min(ideal_top, pano_height - size)), size
