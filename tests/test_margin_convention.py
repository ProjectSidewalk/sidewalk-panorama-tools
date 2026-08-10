"""Pins for reports/scripts/margin_convention.py and for the documents that quote it.

The failure this guards against already happened once (pre-merge review 2026-08-10): a margin-based
restatement of the sizing convention was published with the object's FULL extent in the denominator
and the words "half-extent" on the label, and the pre-registration then carried that number into
Study 2's primary endpoint under a third definition. The band came out ~2x too tight and the
consumer that anchors the convention scored outside it.

So this file pins two things: the arithmetic, and the fact that the two documents still quote the
band the arithmetic produces. A doc edit that silently re-fumbles the factor of two fails here.
"""

import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import margin_convention as mc  # noqa: E402

PREREG = os.path.join(REPO_ROOT, 'reports', '2026-08-09-crop-priors-prereg.md')
CONSUMERS = os.path.join(REPO_ROOT, 'reports', '2026-08-09-cropper-consumer-requirements.md')

# sidewalk-validator-ai frames a constant 11.9 m physical footprint; the reference object is a
# 1.5 m curb ramp. This is the anchor the whole convention was read off.
VALIDATOR_FOOTPRINT_M = 11.9
REFERENCE_OBJECT_M = 1.5


class TestTheRatioIsHalfFullInvariant:
    """The property that makes R the safe quantity to pre-register."""

    def test_full_extents_and_half_extents_give_the_same_ratio(self):
        crop_side, obj = 11.9, 1.5
        assert (crop_side / obj) == pytest.approx((crop_side / 2) / (obj / 2))

    def test_crop_ratio_round_trips_through_object_fraction(self):
        for r in (3.0, 6.7, 7.9, 10.0):
            assert mc.crop_ratio(mc.object_fraction(r)) == pytest.approx(r)


class TestTheBandMatchesTheConsumerRange:

    def test_ten_to_fifteen_percent_is_six_point_seven_to_ten(self):
        lo_phi, hi_phi = mc.OBJECT_FRACTION_BAND
        assert mc.crop_ratio(hi_phi) == pytest.approx(mc.CROP_RATIO_BAND[0], abs=0.05)
        assert mc.crop_ratio(lo_phi) == pytest.approx(mc.CROP_RATIO_BAND[1], abs=0.05)

    def test_the_validator_anchor_lands_inside_the_band(self):
        """The published band must contain the consumer it was derived from. The retired
        [3, 4.5] band did not — that is what made it detectably wrong."""
        r = (VALIDATOR_FOOTPRINT_M / 2) / (REFERENCE_OBJECT_M / 2)
        assert r == pytest.approx(7.93, abs=0.01)
        assert mc.CROP_RATIO_BAND[0] <= r <= mc.CROP_RATIO_BAND[1]
        assert not 3.0 <= r <= 4.5, 'the retired band would have excluded its own anchor'


class TestTheLegacyRestatementsAreDistinct:
    """Each conversion must give a materially different number, or the pins below prove nothing."""

    def test_the_three_definitions_do_not_coincide(self):
        r = 7.93
        half = mc.margin_over_half_extent(r)
        full = mc.margin_over_full_extent(r)
        assert half == pytest.approx(6.93)
        assert full == pytest.approx(3.465)
        assert r != pytest.approx(half) and half != pytest.approx(full)

    def test_the_published_three_to_four_point_five_was_margin_over_full_extent(self):
        """Reconstructs the original slip exactly: (R-1)/2 at the band edges reads 2.8-4.5."""
        lo = mc.margin_over_full_extent(mc.crop_ratio(mc.OBJECT_FRACTION_BAND[1]))
        hi = mc.margin_over_full_extent(mc.crop_ratio(mc.OBJECT_FRACTION_BAND[0]))
        assert (lo, hi) == (pytest.approx(2.83, abs=0.01), pytest.approx(4.5, abs=0.01))


class TestTheDocumentsStillQuoteTheDerivedBand:
    """Enforcement, not decoration: the reports are the deliverable, so they are pinned too."""

    @staticmethod
    def _read(path):
        with open(path, encoding='utf-8') as f:
            return f.read()

    def test_the_prereg_endpoint_uses_the_derived_band(self):
        txt = self._read(PREREG)
        assert re.search(r'R\s*∈\s*\[6\.7,\s*10\]', txt), \
            'prereg Study 2 endpoint no longer quotes R in [6.7, 10]'

    def test_the_consumer_report_states_the_object_fraction_and_the_ratio(self):
        txt = self._read(CONSUMERS)
        assert '10–15% of crop side' in txt
        assert re.search(r'crop ratio R\s*=\s*6\.7–10', txt)

    def test_no_document_still_asserts_the_retired_equivalence(self):
        """The exact retired phrasing must not come back. It may still be *named* as a correction,
        so this looks for the 'i.e.' equivalence claim, not the digits alone."""
        for path in (PREREG, CONSUMERS):
            txt = self._read(path)
            assert not re.search(r'i\.e\.\s*\*\*margin per side\s*≈\s*\n?3–4\.5×', txt), path
            assert 'crop = 3–4.5× object half-extent' not in txt, path
