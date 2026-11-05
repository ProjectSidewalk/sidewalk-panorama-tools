"""Validation-vote arithmetic for Project Sidewalk labels, with every policy explicit (#222, step 4).

Four consumer repos each decided "which labels count as validated" on their own: human-only or all votes,
majority or thresholds, the AI validator's own votes in or out, and what a tie means. This module is the
arithmetic, not the decision: every policy is a required argument, so a caller states its rule rather than
inheriting one by accident.

The input is the public API's ``validations`` array (``/v3/api/rawLabels``, one element per vote), whose shape
was read from a live Seattle response on 2026-11-05 (``tests/fixtures/rawlabels_validations_seattle.json``)::

    {"validation": "Agree" | "Disagree" | "Unsure", "validator_type": "Human" | "AI", "user_id": "<uuid>"}

Two facts from that response a consumer should know:

* **The served ``agree_count``/``disagree_count``/``unsure_count`` include the AI validator's votes**: on all
  85 labels they equal the tallies of the whole array. ``vote_counts(..., include_ai=True)`` reproduces them;
  the default, ``include_ai=False``, does not.
* **The served ``correct`` is the majority of agree over disagree with a tie as null**, AI included and unsure
  abstaining: ``majority(vote_counts(v, include_ai=True), tie="none")`` reproduces it on every fixture row.

The AI validator is identified by ``validator_type == "AI"`` and nothing else: never by an account name or a
``user_id``, which belong in no code (docs/geometry.md, "labels").

Usage::

    from sidewalk_panorama_tools.labels import label_uid, vote_counts, majority, is_validated

    counts = vote_counts(feature["properties"]["validations"])            # humans only
    verdict = majority(counts, tie="none")                                  # Verdict.AGREE / DISAGREE / NONE
    keep = is_validated(counts, min_votes=2, min_agree_fraction=0.75)
    key = label_uid("seattle-wa", feature["properties"]["label_id"])       # "seattle-wa:12327"
"""

import enum
import operator
from typing import Iterable, Mapping, NamedTuple

__all__ = ['TIE_POLICIES', 'VALIDATION_VALUES', 'VALIDATOR_TYPES', 'Verdict', 'VoteCounts', 'is_validated',
           'label_uid', 'majority', 'vote_counts']

# The values the public API serves (live read, 2026-11-05). Anything else raises: a new validator type or
# vote value must be decided on, never silently counted as a human or dropped.
VALIDATION_VALUES = ('Agree', 'Disagree', 'Unsure')
VALIDATOR_TYPES = ('Human', 'AI')
AI_VALIDATOR_TYPE = 'AI'


def label_uid(city, label_id):
    """``"<city>:<label_id>"``, the one safe cross-city key for a label.

    ``label_id`` is a per-city serial: each city's database schema numbers its labels from 1, and across the
    fleet most ids are shared by two or more cities. A label reference without its city is incomplete, and a
    join, dedup or dict keyed on ``label_id`` alone silently merges different labels. Besides this uid, the
    only globally unique key in the label data is ``pano_id`` (Google's or the imagery provider's).

    >>> label_uid("seattle-wa", 12327)
    'seattle-wa:12327'
    """
    city = str(city)
    if not city or ':' in city:
        raise ValueError('a city id is non-empty and has no colon: %r' % (city,))
    if isinstance(label_id, bool):
        raise ValueError('a label_id is an integer, not %r' % (label_id,))
    try:
        number = int(label_id)
    except (TypeError, ValueError):
        raise ValueError('a label_id is an integer: %r' % (label_id,)) from None
    if number != label_id and str(number) != str(label_id).strip():
        raise ValueError('a label_id is an integer: %r' % (label_id,))
    if number < 1:
        # Every city schema's serial starts at 1, so 0 and negatives are fill values (a pandas fillna(0) or -1),
        # and a uid built from one would collide across every filled row instead of failing.
        raise ValueError('a label_id is a serial starting at 1, not %r' % (label_id,))
    return '%s:%d' % (city, number)


class VoteCounts(NamedTuple):
    """Votes on one label, by value."""
    agree: int
    disagree: int
    unsure: int

    @property
    def total(self):
        return self.agree + self.disagree + self.unsure


def vote_counts(validations: Iterable[Mapping], *, include_ai: bool = False) -> VoteCounts:
    """Tally a label's ``validations`` array into :class:`VoteCounts`.

    ``include_ai=False`` (the default) counts human votes only; ``True`` counts every vote, which is what the
    API's own ``*_count`` fields do. An element whose ``validation`` or ``validator_type`` is not one of the
    values the API serves raises ``ValueError``, as does a missing key: a vote this code does not understand is
    never counted as some other kind.
    """
    agree = disagree = unsure = 0
    for vote in validations:
        try:
            value, kind = vote['validation'], vote['validator_type']
        except (KeyError, TypeError):
            raise ValueError('a validation needs "validation" and "validator_type": %r' % (vote,)) from None
        if kind not in VALIDATOR_TYPES:
            raise ValueError('unknown validator_type %r (known: %s)' % (kind, ', '.join(VALIDATOR_TYPES)))
        if value not in VALIDATION_VALUES:
            raise ValueError('unknown validation %r (known: %s)' % (value, ', '.join(VALIDATION_VALUES)))
        if kind == AI_VALIDATOR_TYPE and not include_ai:
            continue
        if value == 'Agree':
            agree += 1
        elif value == 'Disagree':
            disagree += 1
        else:
            unsure += 1
    return VoteCounts(agree, disagree, unsure)


class Verdict(str, enum.Enum):
    """The outcome of :func:`majority`. ``NONE`` is "no verdict", which only a tie policy can produce."""
    AGREE = 'agree'
    DISAGREE = 'disagree'
    UNSURE = 'unsure'
    NONE = 'none'


TIE_POLICIES = tuple(v.value for v in Verdict)


def majority(counts: VoteCounts, *, tie: str) -> Verdict:
    """Agree against disagree, with unsure abstaining; a tie resolved by the caller's stated ``tie`` policy.

    ``tie`` is required and is one of ``"agree"``, ``"disagree"``, ``"unsure"`` or ``"none"``. A label with no
    decisive vote (0 agree, 0 disagree; unsure-only, or no votes at all) is a tie too: to tell "unvalidated"
    from "contested", check ``counts.total`` or use :func:`is_validated`'s ``min_votes``.
    ``tie="none"`` reproduces the API's ``correct`` field (null on a tie) when the counts include AI votes.
    """
    if tie not in TIE_POLICIES:
        raise ValueError('tie must be one of %s, not %r' % (', '.join(TIE_POLICIES), tie))
    if counts.agree > counts.disagree:
        return Verdict.AGREE
    if counts.disagree > counts.agree:
        return Verdict.DISAGREE
    return Verdict(tie)


def is_validated(counts: VoteCounts, *, min_votes: int, min_agree_fraction: float) -> bool:
    """Whether a label passes a threshold rule: at least ``min_votes`` decisive votes (agree + disagree; unsure
    abstains, as in :func:`majority`) and an agree share of them at or above ``min_agree_fraction``.

    Both are required. ``min_votes`` is an integer of at least 1, so a label nobody judged never passes;
    ``min_agree_fraction`` is in [0, 1]. ``min_agree_fraction=0.5`` with ``min_votes=1`` passes a 1-1 tie, which
    :func:`majority` would leave to its tie policy: a threshold is not a majority, and that is the caller's call.
    """
    # operator.index accepts any integer type (numpy's included, which is what a pandas consumer passes) and
    # refuses floats; bool is an int subclass and is refused by name.
    try:
        votes_floor = None if isinstance(min_votes, bool) else operator.index(min_votes)
    except TypeError:
        votes_floor = None
    if votes_floor is None or votes_floor < 1:
        raise ValueError('min_votes is an integer of at least 1, not %r' % (min_votes,))
    if not 0.0 <= float(min_agree_fraction) <= 1.0:
        raise ValueError('min_agree_fraction is in [0, 1], not %r' % (min_agree_fraction,))
    decisive = counts.agree + counts.disagree
    if decisive < votes_floor:
        return False
    # The quotient, not `fraction * decisive`: a correctly rounded agree / decisive is the same double as the
    # threshold the caller typed whenever the share is exactly that value (7/25 and 0.28), while the product
    # rounds up (0.28 * 25 == 7.000000000000001) and failed a label sitting exactly on the threshold.
    return counts.agree / decisive >= float(min_agree_fraction)
