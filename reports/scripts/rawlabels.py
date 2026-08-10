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

Text/JSON columns (tags, description, validations, pano_url, region_name) are never loaded — they
are the bulk of the bytes and no desk study reads them.
"""

import pandas as pd

LEGACY_END = pd.Timestamp('2021-01-01', tz='UTC')
EVO179 = pd.Timestamp('2023-03-29', tz='UTC')

# Everything a desk study consumes, and nothing else. label_type arrives as a name string
# (e.g. 'CurbRamp'), not the numeric id the crop store uses.
STUDY_COLUMNS = [
    'label_id', 'user_id', 'pano_id', 'label_type', 'severity', 'time_created',
    'correct', 'agree_count', 'disagree_count', 'unsure_count', 'image_capture_date',
    'heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y', 'canvas_width', 'canvas_height',
    'pano_x', 'pano_y', 'pano_width', 'pano_height',
    'camera_heading', 'camera_pitch', 'camera_roll', 'latitude', 'longitude',
]

# Every replay/geometry input as float64: several are blank for labels whose pano metadata never
# resolved, and a blank must stay NaN (a crashed lookup must never read as pixel 0).
_FLOAT_COLUMNS = [
    'heading', 'pitch', 'zoom', 'canvas_x', 'canvas_y', 'canvas_width', 'canvas_height',
    'pano_x', 'pano_y', 'pano_width', 'pano_height',
    'camera_heading', 'camera_pitch', 'camera_roll', 'latitude', 'longitude',
]


def load_rawlabels(path):
    """Read one city's rawLabels CSV into the study frame: floats for geometry, UTC datetimes for
    time_created, and an 'era' column per the module boundaries."""
    df = pd.read_csv(path, usecols=STUDY_COLUMNS,
                     dtype={c: 'float64' for c in _FLOAT_COLUMNS})
    df['time_created'] = pd.to_datetime(df['time_created'], unit='ms', utc=True)
    return add_era(df)


def add_era(df):
    """Bucket rows by time_created: 'legacy' < LEGACY_END <= 'mid' < EVO179 <= 'post179'."""
    df = df.copy()
    t = df['time_created']
    df['era'] = 'mid'
    df.loc[t < LEGACY_END, 'era'] = 'legacy'
    df.loc[t >= EVO179, 'era'] = 'post179'
    return df
