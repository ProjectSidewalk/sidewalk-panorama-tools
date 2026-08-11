"""Two one-line helpers every study script needs, in one place: `num` for the artifact, `fmt` for
stdout. Both exist because these studies report *undefined* quantities, and undefined is not zero.

The pattern that keeps producing bugs: an analysis function correctly returns `None` for a quantity
that is undefined on its input -- a percentage with a zero denominator, a sample sd of one value, a
correlation against a constant series, a percentile of an empty group -- and then `main()` prints it
with a format spec. `f"{None:.1f}"` raises TypeError, so the run dies *after* all the work and before
`--write`. It was found in four scripts at once (2026-08-11 review):

  * offaxis_covariate.py   -- one thin city aborted a six-city run
  * click_noise_study.py   -- reproduced: crashes on any corpus with no cross-user pairs
  * photometa_census.py    -- crashes when no alive pano carries tilt; after live network sampling
  * store_coverage.py      -- crashes when a coverage denominator is 0; after live store probing

Each of those files was already internally inconsistent about it -- click_noise_study.py's own print
block format-specs two values on one line and prints three more bare on the next -- which is what
made the defect invisible. A shared helper makes the safe form the shorter one to type.

Deliberately not a general utility module: these two functions are the whole surface, and both are
about the same single decision (how a study represents "undefined").
"""

import math


def num(x):
    """A float for a JSON artifact, or None when the value is not a finite number.

    Study scripts write with `allow_nan=False`, so a NaN reaching the result dict aborts the write on
    the last line of the run. And an artifact that *did* accept NaN would be unreadable by jq and
    JSON.parse -- reports/data/2026-08-09-photometa-census.json shipped with 4,916 bare NaN tokens
    once (see tests/test_committed_data_files.py). null is also the honest encoding: a correlation
    against a constant series is undefined, not zero.
    """
    x = float(x)
    return x if math.isfinite(x) else None


def fmt(value, spec='', missing='n/a'):
    """Format a number that may legitimately be None, without raising.

    `format(None, '.2f')` is a TypeError. Every study prints summary lines mixing quantities that are
    always defined with quantities that are only sometimes defined; this makes the second kind
    printable. Non-finite floats are treated as missing too, so a NaN that slipped past `num` shows
    as 'n/a' rather than as the string 'nan' masquerading as a measurement.
    """
    if value is None:
        return missing
    if isinstance(value, float) and not math.isfinite(value):
        return missing
    return format(value, spec)
