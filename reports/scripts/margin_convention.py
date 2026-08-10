"""The one sizing quantity the cropper work package uses, and the conversions that keep tripping it.

Study 2 (#32) scores every candidate sizing rule on **crop ratio R**:

    R = crop side / object extent   ==   crop half-side / object half-extent

R is a ratio of like quantities, so it is invariant to whether both terms are full extents or both
are half-extents. That invariance is the whole point of preferring it: the consumer-requirements
survey originally published a margin-based restatement of the same convention with the object's
full extent in the denominator but the words "half-extent" on the label, and the pre-registration
then carried that number into Study 2's primary endpoint under a *third* definition. Two
factor-of-two slips compounded into an acceptance band ~2x too tight, one that the very consumer
anchoring the convention (sidewalk-validator-ai, R = 7.9) fell outside of. Found in pre-merge review
2026-08-10, before registration; see reports/2026-08-10-crop-priors-review.md.

The margin-based forms below exist only so that a reader meeting one in an old document can convert
it. Do not introduce them into new specs.

Consumer band (reports/2026-08-09-cropper-consumer-requirements.md, section "Decision thresholds",
item (ii)): the consumers converge on the object occupying 10-15% of the crop side, i.e.
R in [6.7, 10].
"""

# Object extent as a fraction of the crop side, across the surveyed consumers.
OBJECT_FRACTION_BAND = (0.10, 0.15)

# The pre-registered Study 2 acceptance band, rounded to the published precision. Derived from
# OBJECT_FRACTION_BAND by crop_ratio(); test_margin_convention.py pins that they agree.
CROP_RATIO_BAND = (6.7, 10.0)


def crop_ratio(object_fraction):
    """R from the object's share of the crop side. R = 1/phi."""
    return 1.0 / object_fraction


def object_fraction(crop_ratio_r):
    """The inverse of crop_ratio: what share of the crop side the object occupies at ratio R."""
    return 1.0 / crop_ratio_r


def margin_over_half_extent(crop_ratio_r):
    """Legacy restatement: margin per side divided by the object's HALF extent = R - 1.

    (crop_side - object) / 2 / (object / 2) = R - 1.
    """
    return crop_ratio_r - 1.0


def margin_over_full_extent(crop_ratio_r):
    """Legacy restatement: margin per side divided by the object's FULL extent = (R - 1) / 2.

    This is the quantity the original survey computed while calling it "half-extent"; at
    R in [6.7, 10] it reads 2.8-4.5, which is where the published "3-4.5x" came from.
    """
    return (crop_ratio_r - 1.0) / 2.0
