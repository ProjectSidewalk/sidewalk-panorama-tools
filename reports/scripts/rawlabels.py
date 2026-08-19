"""Shared loader for /v3/api/rawLabels?filetype=csv exports — the one endpoint that carries every
desk-study input (verified against Columbus and Newberg, 2026-08-09): the full replay tuple
(canvas click + POV + camera metadata + pano dims + stored pano_x/y), time_created for era
assignment, stored lat/lng, and validation counts. No cvMetadata join needed.

The two client-era boundaries (established in label-latlng-estimation,
reports/2026-08-06-pov-inversion.md, and pinned here so every study buckets identically):

- LEGACY_END, 2021-01-01 UTC: before this the client parseInt-truncated its POV inputs; that
  truncation (and the +0.72 deg depth-lookup bearing bias) lives in that era's stored *lat/lng*
  targets, never in pano_x/pano_y, which evolution 179 later recomputed with the exact math.
- EVO179, 2023-03-29 UTC (SidewalkWebpage v7.12.2): from here the front end wrote pano_x/pano_y
  live with the exact projection against real pano dims; earlier rows hold evolution 179's SQL
  recompute, whose camera_heading input was Google's 2022-era metadata.

The heavy text/JSON columns (description, validations, pano_url, region_name) are never loaded — they
are the bulk of the bytes and no desk study reads them. `tags` IS loaded: it is short, and it is the
only field that says whether a label's stored point identifies a specific physical thing or merely a
region that qualifies (see `has_located_referent`).
"""

import pandas as pd

LEGACY_END = pd.Timestamp('2021-01-01', tz='UTC')
EVO179 = pd.Timestamp('2023-03-29', tz='UTC')

# Everything a desk study consumes, and nothing else. label_type arrives as a name string
# (e.g. 'CurbRamp'), not the numeric id the crop store uses.
STUDY_COLUMNS = [
    'label_id', 'user_id', 'pano_id', 'label_type', 'severity', 'time_created', 'tags',
    'correct', 'agree_count', 'disagree_count', 'unsure_count', 'image_capture_date',
    'heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y', 'canvas_width', 'canvas_height',
    'pano_x', 'pano_y', 'pano_width', 'pano_height',
    'camera_heading', 'camera_pitch', 'camera_roll', 'latitude', 'longitude',
]

# Served by rawLabels and authoritative for imagery source ('gsv' / 'mapillary'), but OPTIONAL here:
# a cache or fixture captured before the column existed still loads, and
# mapillary_census.imagery_source reports by_source = None rather than silently falling back to the
# pano-id heuristic. Read when present, absent otherwise — never fabricated.
OPTIONAL_COLUMNS = ['pano_source']

# Label types whose stored point does not identify a *particular* place, for two different reasons:
#
# * 'Occlusion' ("Can't see the sidewalk") marks the **view**, not a thing in it. The pre-registration
#   already excludes it corpus-wide as having no crop consumer.
# * 'Crosswalk' and 'NoSidewalk' mark **extended linear features**. A crosswalk label is correctly
#   placed anywhere along the crosswalk — it need not be at any particular point — so two annotators
#   who both place it correctly can be metres apart along its length. Same property as a region tag,
#   but it is inherent to the type rather than conditional on a tag.
#
#   NoSidewalk is the same argument with a worse constant. A crosswalk's extent is bounded by the width
#   of the roadway it crosses; a stretch of missing sidewalk is bounded by nothing in particular and can
#   run the length of a block, so the arbitrary along-feature offset it admits is larger. It is also by
#   far the bigger corpus effect: 82,769 labels across the six GSV cities (81,667 of them eligible under
#   the off-axis replay filter), against Richmond's zero — which is why nothing in the Mapillary census
#   that named this an open question turned on it, and why a GSV placement study must not read these
#   rows as measurable subjects.
#
# * 'Signal' was added 2026-08-13, from annotation QA rather than from the tag vocabulary. §4's rubric
#   says "the signal head centre", but 45 of the 72 drawn Signal labels sit at 10 deg or more below the
#   horizon (10.0 to 42.1 deg, median 15.2 over all 72), on the pole base rather than on the head — two
#   incompatible conventions in one arm. Gold-vs-stored would then book a 30-40
#   deg convention mismatch as placement error, swamping the ~0.5 deg effect Study 1 is estimating, and
#   even a 60 deg tile does not reach the head from a pole-base click. The underlying property is the
#   same as Crosswalk's: a signal *installation* is one vertically extended object, so "the" point on it
#   is a convention, not a fact about the world.
#
# * 'Other' was added at the same time and for a blunter reason: it is a residual category, 73% untagged,
#   and an untagged 'Other' can be any of the things the other eight types are not. There is no rubric
#   that could name its referent, because the type does not name one.
#
# IMPORTANT: this is about **placement-measurability, not crop-corpus membership.** Crosswalk (label
# type 9) and NoSidewalk (type 7) have real crop consumers and stay in the crop corpus; what they cannot
# do is serve as a subject for a stored-vs-gold *displacement*, because there is no displacement-from.
# Do not read this set as "types to drop".
#
# What DID change on 2026-08-13 is whether they are *annotated*: Amendment 3 stopped cutting tiles for
# them. Amendment 2(c) had kept them in the annotation set on the grounds that Study 2 needs a gold box
# even where Study 1 has no gold point — but if the referent has no located centre it has no tight
# extent either, so those boxes were arbitrary in exactly the way the excluded points are. Study 2 sizes
# their crops from the distance prior instead. They remain in the drawn corpus; they are not annotated.
NO_REFERENT_TYPES = frozenset({'Occlusion', 'Crosswalk', 'NoSidewalk', 'Signal', 'Other'})

# (label_type, tag) pairs where the tag names a property of an extended region rather than of a point,
# so the label could have been placed anywhere on any qualifying stretch. A SurfaceProblem tagged
# brick/cobblestone is the case that prompted this: the whole sidewalk is brick, so there is no
# particular spot the click was aiming at, and stored-vs-gold displacement has no referent to measure
# against. This is a different exclusion principle from every other one in the study spec, which
# excludes on *record* quality (does the record replay?) rather than on *referent* quality (is there a
# located thing to be right or wrong about?).
#
# Deliberately a narrow, enumerated set rather than a heuristic over tag text. Extending it is a
# one-line edit plus a decision, which is the right amount of friction for something that changes a
# corpus. It was extended once, on 2026-08-13, and the friction did its job: the trigger was an
# annotator opening the tool and landing on an Obstacle tagged `stairs`, whose extent and centre are
# both a matter of viewing angle. The full tag vocabulary of the five surviving measurable types was
# then put in front of Jon and ruled on pair by pair, rather than derived from a principle here.
#
# The rule is (type, tag) PAIRS, not tags, and `height difference` is why that matters: it is excluded
# under SurfaceProblem, where it names a run of pavement, and kept under Obstacle, where it names a
# discrete step you can stand at. The same word is a region in one type and an object in the other.
#
# Jon's calls that went AGAINST the first proposal, recorded because they are the boundary of the rule
# and a later reader will otherwise re-litigate them: Obstacle + {vegetation, narrow, garage entrance,
# height difference, litter/garbage} are KEPT, as is SurfaceProblem + {cracks, grass, uneven/slanted,
# debris}. The principle that survives is "does the tag change what the referent IS", not "is the tag
# about a defect" and not "is the tag about something big".
#
# Nothing is excluded for CurbRamp or NoCurbRamp under any tag. Every tag those types carry describes a
# property OF the ramp (or of the place a ramp should be) — narrow, steep, points into traffic — and a
# narrow curb ramp is still a curb ramp with a gutter line to be centred on.
#
# A label carrying several tags is excluded if ANY of its pairs is here, which is `region_tag_mask`'s
# `any(...)`: `Obstacle [pole,stairs]` is a real corpus row, and the pole does not rescue it.
#
# ('Crosswalk', 'brick/cobblestone') is absent from this set for a different reason: Crosswalk is
# excluded by NO_REFERENT_TYPES regardless of tag, so a pair here would be dead weight. Same now for
# every ('Other', ...) and ('Signal', ...) pair.
REGION_TAGS = frozenset({
    ('SurfaceProblem', 'brick/cobblestone'),
    ('SurfaceProblem', 'bumpy'),
    ('SurfaceProblem', 'construction'),
    ('SurfaceProblem', 'height difference'),
    ('SurfaceProblem', 'narrow sidewalk'),
    ('SurfaceProblem', 'rail/tram track'),
    ('SurfaceProblem', 'sand/gravel'),
    ('SurfaceProblem', 'very broken'),
    ('Obstacle', 'construction'),
    ('Obstacle', 'outdoor dining area'),
    ('Obstacle', 'stairs'),
})

# Deployments outside the study frame. Not a data-quality rule and not a referent rule: these labels
# replay fine and have perfectly good referents. They are excluded because the gold standard is only as
# good as the annotator's ability to read the scene, and Taiwan's pedestrian infrastructure — arcaded
# walkways, scooter parking on the footway, and markings that do not map onto the North American
# vocabulary the rubric was written against — is not infrastructure this project's annotators can judge
# reliably. An annotator guessing at what counts as the gutter line does not produce a weaker gold
# standard, it produces a confidently wrong one, and the agreement gate cannot see the difference.
#
# The cost is priced rather than assumed: 116 of the drawn corpus's 763 labels (15.2%), 51 of its 368
# measurable ones. Study 1 keeps 317, the thinnest depression band holds 67 against the ~57 §5 needs
# after its 1.3 design effect, and the thinnest type-band cell holds 7 against a target of 6. So the
# draw is FILTERED rather than redrawn — a strict subset of an already-registered corpus, with no new
# selection decisions in it.
#
# Deliberately not widened to "non-US". The same argument would reach Amsterdam, CDMX, SPGG, Cuenca,
# Zurich and the rest, and that cut does not survive its own arithmetic: US-only leaves 201 measurable
# labels, a thinnest band of 36 against the 57 required, and two cells below target. The line is drawn
# at unfamiliarity that would make an annotation unreliable, not at a passport.
EXCLUDED_DEPLOYMENTS = frozenset({'kaohsiung-tw', 'keelung-tw', 'new-taipei-tw', 'taipei'})


def in_study_frame(df):
    """Boolean mask: is this label from a deployment the study draws on?

    Keyed on the `city` column, which is the deployment id. Note `taipei` carries no `-tw` suffix while
    its three siblings do, so a rule written as a suffix match would silently keep the largest of the
    four — 54 of the 116.
    """
    return ~df['city'].isin(EXCLUDED_DEPLOYMENTS)


# Every replay/geometry input as float64: several are blank for labels whose pano metadata never
# resolved, and a blank must stay NaN (a crashed lookup must never read as pixel 0).
_FLOAT_COLUMNS = [
    'heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y', 'canvas_width', 'canvas_height',
    'pano_x', 'pano_y', 'pano_width', 'pano_height',
    'camera_heading', 'camera_pitch', 'camera_roll', 'latitude', 'longitude',
]


def load_rawlabels(path):
    """Read one city's rawLabels CSV into the study frame: floats for geometry, UTC datetimes for
    time_created, and an 'era' column per the module boundaries.

    `pano_id` is dtype-pinned to str for the same reason DownloadRunner and CropRunner pin it (#46):
    Mapillary image ids are all-numeric, so pandas infers int64 for any Mapillary-sourced city and
    every downstream `pano_id[:2]` or string join silently changes meaning or crashes. Found by
    running these studies against Richmond, the first Mapillary deployment — the six GSV cities
    happen to carry 22-char alphanumeric ids, so the omission was invisible for 438k labels.
    """
    dtypes = {c: 'float64' for c in _FLOAT_COLUMNS}
    dtypes['pano_id'] = str
    header = pd.read_csv(path, nrows=0).columns
    columns = STUDY_COLUMNS + [c for c in OPTIONAL_COLUMNS if c in header]
    df = pd.read_csv(path, usecols=columns, dtype=dtypes)
    df['time_created'] = pd.to_datetime(df['time_created'], unit='ms', utc=True)
    return add_era(df)


def parse_tags(tags):
    """rawLabels serves `tags` as a bracketed comma-joined string (`[points into traffic,steep]`, or
    `[]`). Returns one frozenset per row; a blank or missing cell becomes an empty set.

    Not a JSON array despite the brackets — the tag text is unquoted, so `json.loads` fails on every
    non-empty value. Split on commas and strip.
    """
    def one(value):
        if not isinstance(value, str):
            return frozenset()
        inner = value.strip()
        if inner.startswith('[') and inner.endswith(']'):
            inner = inner[1:-1]
        return frozenset(t.strip() for t in inner.split(',') if t.strip())
    return pd.Series(tags, dtype=object).map(one)


def has_located_referent(df):
    """Boolean mask: does this label's stored point identify a specific located thing?

    False for `NO_REFERENT_TYPES` and for any `REGION_TAGS` pair. Those labels are perfectly valid
    labels — they are just not measurable subjects for a placement study, because there is no
    particular spot they were aiming at, so a stored-vs-gold displacement has nothing to be a
    displacement *from*. Keeping them would put an arbitrary and unbounded offset into the noise floor
    and into any bias estimate.

    Applies only to the two things the rule names. Every other label passes, including ones with
    severity or validation problems: this is about the referent, not about label quality.
    """
    return ~df['label_type'].isin(NO_REFERENT_TYPES) & ~region_tag_mask(df)


def study_measurable(df):
    """Boolean mask: what Study 1 can read — the live referent rule AND the study frame.

    The one definition of "measurable", and it lives here rather than in a script because three
    callers need it: `annotation_tiles.py --measurable-only`, `annotation_subset.py --measurable-only`
    and the analysis. Each of them previously had, or could have had, its own answer.

    **Never read a stored `measurable` column instead of calling this.** A corpus CSV's `measurable`
    is a snapshot of the rules as they stood when the corpus was drawn, and both arms have moved
    since: on the committed 2026-08-12 GSV corpus the stored column says 584 and this rule says 368.
    A queue drawn from the stale column is 216 labels of perfectly well-formed work on labels that
    have no point to be displaced from, and nothing about it looks wrong.

    The two arms stay separate inside because they are different kinds of exclusion and get revisited
    for different reasons: the referent rule says a label has no point to be displaced from, the frame
    rule says the label is fine and the annotator is not equipped to judge it.
    """
    return has_located_referent(df) & in_study_frame(df)


def region_tag_mask(df):
    """Boolean mask: does this label carry a `(label_type, tag)` pair from `REGION_TAGS`?

    The tag arm of the referent rule, on its own because two callers need it and had a copy each:
    `has_located_referent` *filters* the corpus on it, and `mapillary_census.referent_exclusion`
    *reports* how many labels it removes. Two transcriptions of one list comprehension, so a change
    to either — adding the `SurfaceProblem + bumpy` pair the REGION_TAGS comment says is deliberately
    out, or matching tag text case-insensitively — would leave the published arm count describing a
    rule the study no longer applies, and nothing would fail: the only assertion over those counts
    checks that the two arms sum to the total, which they still would.

    A missing `tags` column is treated as no tags rather than raising. rawLabels always serves it, but
    a study frame assembled by hand need not, and the caller that was reporting the count read
    `df['tags']` directly while the caller that was filtering tolerated its absence — the two copies
    had already drifted on that.
    """
    types = df['label_type']
    tags = parse_tags(df['tags']) if 'tags' in df else parse_tags([None] * len(df))
    tags.index = df.index
    return pd.Series([any((t, tag) in REGION_TAGS for tag in tagset)
                      for t, tagset in zip(types, tags)], index=df.index)


def add_era(df):
    """Bucket rows by time_created: 'legacy' < LEGACY_END <= 'mid' < EVO179 <= 'post179'."""
    df = df.copy()
    t = df['time_created']
    df['era'] = 'mid'
    df.loc[t < LEGACY_END, 'era'] = 'legacy'
    df.loc[t >= EVO179, 'era'] = 'post179'
    return df
