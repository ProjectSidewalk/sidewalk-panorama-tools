# Plan: the crop-time tilt correction in CropRunner (#191, step 2)

Written 2026-09-29 for the implementing session. It follows the handoff comment on
[#191](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/191#issuecomment-5901083326) and
the study behind it ([#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54),
[#158](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/158),
`reports/2026-09-26-tilt-error-study.md`). Where this plan and the handoff differ, the handoff is the
requirement and this plan is how to meet it; where this plan makes a choice the handoff left open, the
choice is named under "Decisions this plan makes" and must be surfaced in the PR description for Jon.

## What is being built

A GSV label's stored `pano_x`/`pano_y` is in gravity-levelled coordinates, while the stored tiles are in the
rig's own frame (not levelled in any scrape era). Endpoint C found the stored point is off towards the rig
pixel (79 : 0), and the beta batch plus the auto-labeler's independent estimate put the size of the move at
about the full rig transform (beta = 1 by the pre-set rule; 0.936 ± 0.021 for post179 from the auto-labeler).
So, when asked to, CropRunner moves each label's crop centre from the stored pixel to the rig pixel, on both
axes, scaled by a per-era beta.

**Off by default.** Nothing about a run without the flag may change: crops byte-identical, counts identical
apart from one new zero-valued bucket, the summary line identical apart from that bucket's clause, the marker
identical apart from the new keys.

## Out of scope (do not do these)

Choosing the default beta; turning the correction on by default; re-running the sizing fit on corrected
coordinates; regenerating any consumer's crops; the source-side fix (SidewalkWebpage#4784); non-GSV panos
(#190); widening the provenance manifest; a `time_created` field in cvMetadata (a SidewalkWebpage change).

## Design

### 1. A production module for the geometry and the pose: `pano_pose.py` (repo root)

`reports/scripts/tilt_geometry.py` holds the one definition of every frame and sign the study measured.
Production code cannot import from `reports/scripts/` (the coverage set is the production tree, and
`reports/*` is omitted), so the definition moves and the study imports it back.

- **Move, verbatim, every public function of `tilt_geometry.py`** into `pano_pose.py`: `wrap_deg`,
  `direction_rfu`, `bearing_elevation`, `rig_from_gravity`, `gravity_to_rig`, `rig_to_gravity`,
  `tilt_term_deg`, `vertical_lean_deg`, `pixel_from_bearing_elevation`, `bearing_elevation_from_pixel`,
  `rig_pixel_from_gravity_pixel`, `gravity_pixel_from_rig_pixel`, `artifact_normal_bearing_elevation`,
  `xml_tilt_to_pitch_roll`, `pitch_roll_to_xml_tilt`, with the module docstring's conventions (they are the
  measured facts; keep the F1 sign statement and the "z = down" artifact frame). Keep the private helpers.
- **`tilt_geometry.py` becomes a re-export**: it imports the names from `pano_pose` and defines nothing
  itself. It is imported on makelab2 beside `tilt_frame.py` with only the script directory on `sys.path`,
  so it must find `pano_pose` both from the repo (insert the repo root, two levels up, ahead of the script
  dir) and as a sibling file (a plain `import pano_pose` after that insert covers both: the upload puts
  `pano_pose.py` beside it). Update `tilt_frame.py`'s docstring ("imports only tilt_geometry, uploaded beside
  it") to say both files are uploaded. `tilt_pose_scan.py` stays standalone (it inlines its one helper on
  purpose). Add `pano_pose.py` to `tests/test_tilt_pose_scan.py::test_runs_under_python39_syntax`'s list, and
  keep it to Python 3.9 grammar and the 3.9 standard library (no `match`, no `X | None`, no
  `zip(strict=)`); numpy 1.23 is what makelab2 has.
- **Beta.** `corrected_pixel(x, y, w, h, pitch_deg, roll_deg, beta)`: scale the pose, not the pixel move:
  `rig_pixel_from_gravity_pixel(x, y, w, h, beta * pitch_deg, beta * roll_deg)`. Beta 0 is the identity
  (exactly), beta 1 is the full transform, and to first order y moves by `-beta * T(b) * h / 180`. This is
  the choice the handoff asked to pin with a test; the alternative (interpolating between stored and rig
  pixel) agrees to first order and differs only at second order, and scaling the pose is what "a fraction of
  the tilt leaked into the click mapping" means physically.
- **Pose readers**, mirroring `tilt_adjudicate.attach_pose` and `tilt_pose_scan.read_npz`/`read_xml`:
  - `pose_from_xml(path)` reads `<projection_properties pano_yaw_deg tilt_yaw_deg tilt_pitch_deg/>` and
    returns `(pitch_deg, roll_deg)` through `xml_tilt_to_pitch_roll`. Any of the three missing or
    unparseable, or the file unreadable, is `None` with a reason string.
  - `pose_from_depth_artifact(path)` opens the `.depth.npz` with `numpy.load` and reads the scalars `pitch`
    and `roll` (radians; gsv already stores pitch as `90 - raw`), converts to degrees and wraps with
    `wrap_deg`. A missing key, a NaN, or an unreadable file is `None` with a reason. Read only those two
    members (an npz is a zip; do not touch the rasters).
  - `resolve_pano_pose(pano_jpg_path)` applies the scrape-era rule: if `<id>.xml` exists beside the JPEG,
    the xml decides (a 2019-22 stitch), **never falling through to the npz** even when the xml is
    incomplete; else the `.depth.npz` if present; else no pose. Returns a small named tuple
    `(pitch_deg, roll_deg, source)` with `source in ('xml', 'npz')`, or `None` plus the reason. The suffixes
    are `'.xml'` and `downloaders.gsv.DEPTH_ARTIFACT_SUFFIX` (import it; do not restate the string).
- **The era boundary.** `EVO179_UTC = '2023-03-29T00:00:00+00:00'` (SidewalkWebpage v7.12.2) and
  `label_era(time_created)` returning `'post179'`, `'legacy+mid'` or `'unknown'`. Accept what rawLabels
  exports (epoch milliseconds as int, float or digit string) and ISO 8601 (a trailing `Z` included); blank,
  `None`, NaN and anything else is `'unknown'`. Compare in UTC. Pin against `rawlabels.EVO179` in a test that
  imports the study module (the way `tests/test_tilt_geometry.py` imports `reports/scripts`), rather than
  importing it from production.
- Add `pano_pose.py` to `tests/test_coverage_config.py::PRODUCTION_MODULES`, and to `tests/test_docs.py::
  NAMED_SOURCES` if it cites a `docs/` page (it should cite `docs/depth.md` for the artifact frame).

### 2. CropRunner

- **Flag:** `--tilt-correction` (`store_true`), threaded as `tilt_correction=False` through `run()`,
  `bulk_extract_crops()` and `write_rule_marker()`, and echoed in the run summary. Help text says what it
  does, that it is off by default, that it needs a pose beside the pano, and that it changes what a crop is
  (re-cut whole with `--force`).
- **Constants**, beside the sizing constants: `TILT_BETA_BY_ERA = {'post179': 1.0, 'legacy+mid': 1.0,
  'unknown': 1.0}` with a comment saying the values are the pre-set rule's default, that Jon sets them
  once the auto-labeler's per-era estimate is posted on #191 (post179 measured 0.936 ± 0.021 there), and why
  `unknown` exists (below). `TILT_ERAS = ('legacy+mid', 'post179', 'unknown')`.
- **The era of a label** comes from an optional `time_created` column through `pano_pose.label_era`.
  cvMetadata does not serve it today, so on the `-d` path every label is `unknown`; a rawLabels CSV or a
  hand-made file can carry it. Never required (`REQUIRED_LABEL_COLUMNS` does not grow, so the schema
  tripwire does not either). Tally labels per era when the flag is on and print one line naming each era's
  count and the beta applied, so a store cut entirely as `unknown` says so. The tally is NOT a key of the
  counts dict (the dict's key set is asserted exactly by tests).
- **Pose lookup is per pano, once, only when the flag is on**, in the per-pano loop after `Image.open`
  succeeds and before the per-label loop. No pose means every label on that pano is the new bucket:
  - **`no_pose`**, a DISJOINT outcome (goes into `DISJOINT_OUTCOMES`, into the invariant sum, into the
    summary sentence as its own clause, and it is not an error, so the exit code is unchanged). It is a skip
    of the same kind as `missing_pano`: the store lacks what the run needs, and a later run cuts the label
    once the depth phase has written the artifact. Under `--force` a label with a crop on disk also counts
    `stale_kept` (add it to the stale_kept sentence's list). One `crop.log` line per pano under a new
    `WarningBudget` kind `no_pose`, naming the reason (no xml and no npz; xml present but incomplete; npz
    without pitch/roll). One both-channel summary line when the count is nonzero, `black_content`'s pattern,
    because a run that skipped every label for no pose must not complete silently.
- **The corrected point**: `cx, cy = pano_pose.corrected_pixel(pano_x, pano_y, w, h, pitch, roll,
  TILT_BETA_BY_ERA[era])` with `w, h = pano.size`. Then:
  - the `out_of_frame` preflight tests the **corrected** y (that is the row being cut; its message names both
    the stored and the corrected value when they differ);
  - `make_single_crop` gains `centre=None`: the window is centred on `centre` when given, else on
    `(pano_x, pano_y)`. **Sizing stays at the stored `pano_y`**: `crop_window_width(pano_y, ...)`, never the
    corrected y (see Decisions). `label_position_in_crop(cx, cy, box, ...)` positions the mark, and the
    `shifted_vertically` log line reads the corrected y;
  - the per-label `logging.info` line keeps its four fields and, when a correction was applied, appends the
    corrected point, the pose, its source, the era and the beta.
- **CropRunner keeps the #78 token guard**: no literal 360 or 180 outside the four unit primitives. All
  degree-pixel arithmetic for the correction lives in `pano_pose`.
- **The marker.** Add three constants to `RULE_MARKER_CONSTANT_KEYS` for **both** rules:
  `tilt_beta_post179`, `tilt_beta_legacy_mid`, `tilt_beta_unknown_era`, written as the table's values when
  the flag is on and **`0.0` when off** (beta 0 is literally the identity, so "off" and "beta 0" are one
  fact). `_rule_constants(tilt_correction)` takes the flag. The existing machinery then does the rest: a store
  cut off-then-on warns "was cut with tilt_beta_post179=0.0 and this run uses 1.0", `constants_seen` keeps
  the history for good, `--force` does not clear it, and a marker written before these keys existed stays
  silent (only recorded values are compared). Also write `tilt_correction: 'on' | 'off'` as a top-level
  string for readers, added to `RULE_MARKER_SCALAR_KEYS` (strings pass the type check; a bool would not).
  `_record_manifest_gap` carries every key forward already.
- **The provenance manifest is unchanged.** A new column is a header migration (#159's set-aside logic),
  its own change. Say in the docs that a mixed corrected/uncorrected store is visible in `crop_rule.json`
  and not per crop, and name the manifest column as the follow-up.

### 3. Tests

Network-free, in-process, the #52.1 shape. Name them for what they pin.

- `tests/test_pano_pose.py` (new):
  - **The move changed nothing:** a table of `rig_pixel_from_gravity_pixel` and `xml_tilt_to_pitch_roll`
    outputs computed with `git show origin/master:reports/scripts/tilt_geometry.py` over a grid (several
    x, y, w, h, pitch, roll including the seam, both poles, and roll wrapped from 359.6), frozen as literals.
  - Beta: 0 is the identity to 1e-9; 1 equals `rig_pixel_from_gravity_pixel`; 0.5 moves y by about half
    the full move and x the same way; the first-order `-beta * T(b) * h / 180` holds at small tilt.
  - Readers: an xml triple round-trips through `pitch_roll_to_xml_tilt`; xml wins over an npz beside it;
    an incomplete xml is `None` and does NOT fall through to the npz; npz radians become wrapped degrees;
    NaN, a missing key, a corrupt zip and a missing file are `None` with a reason; the suffix is gsv's.
  - `label_era`: ms epoch either side of the boundary, exactly at it (`post179`), ISO with and without
    `Z`, blank/None/NaN/garbage are `unknown`; `rawlabels.EVO179` agrees.
  - `tilt_geometry.<name> is pano_pose.<name>` for every public name, so the shim cannot drift.
- `tests/test_tilt_geometry.py` stays as it is and must still pass through the shim.
- `tests/test_crop_tilt.py` (new), driving `bulk_extract_crops` and `main()`:
  - **Off is identical:** a store cut without the flag, before and after the change, byte for byte (the
    existing `TestTheDefaultWindowIsByteIdentical` covers windows; add a whole-store comparison with a pose
    present beside the pano and the flag off, asserting no lookup happened, e.g. by making the npz
    unreadable).
  - **Registration:** plant a unique pixel at the rounded corrected position in a synthetic pano with a
    known pose (xml on one, npz on another), cut with the flag on, read the pixel back out of the crop at
    `label_position_in_crop(cx, cy, box, w, scale)`, at the horizon, near both poles and across the seam;
    with `--mark-label`, the dot is on the planted pixel. Never compare two derivations.
  - **Sizing at the stored y:** the corrected crop's `box.width` equals the uncorrected crop's for the same
    label (a mutant sizing at the corrected y fails), under both sizing rules.
  - **`no_pose`:** absent both, xml incomplete, npz NaN; counted, not an error, exit 0, the both-channel
    line, `stale_kept` under `--force`, the invariant holding, and the counts dict's key set being exactly
    `{'total'} | DISJOINT_OUTCOMES | COUNT_ANNOTATIONS` (update `tests/test_crop_runner.py`'s own copies of
    those tuples and the summary-string assertions).
  - **Preflight on the corrected y:** a stored y inside the image whose corrected y is outside counts
    `out_of_frame`.
  - **The marker:** the three keys at `0.0` when off and the table's values when on; off-then-on warns on
    both channels naming the key; a pre-existing marker without the keys is silent; `tilt_correction`
    string present; `_read_rule_marker` still reads it.
  - **The era tally line** with a `time_created` column (ms and ISO) and without one (all `unknown`).
- `tests/test_coverage_config.py`, `tests/test_tilt_pose_scan.py` (the 3.9 list) and, if it cites docs,
  `tests/test_docs.py::NAMED_SOURCES` updated.
- Run the whole suite with coverage (`python -m pytest tests --cov --cov-report=term-missing`); the gate is
  `fail_under = 98` and the new module must not drag it. `tests/conftest.py` fails a run that leaves the
  tree changed, so commit or clean up before the final run.

### 4. Docs (`tests/test_docs.py` checks links and cited paths)

- `docs/cropper.md`: a new subsection under "Crop geometry", after "Where the label is inside the crop":
  **"The tilt correction (opt-in, #191)"**: what it corrects and why (two sentences and a link to the
  study), the flag, the pose rule (xml first, then npz, never guessed), `no_pose`, beta per era and where the
  era comes from (and that cvMetadata does not carry it), sizing at the stored y and why, the marker keys
  and the mixed-store warning, `--force` as the re-cut, the manifest-column follow-up. Update: the
  invariant line and the outcomes list under "Outcomes, exit code, and re-runs"; the `--force`/`stale_kept`
  text; the marker paragraph's key list; "Paths and intake" (optional `time_created`); the Usage block's
  command line.
- `docs/depth.md`: one sentence where the artifact's `pitch`/`roll` are described, saying the cropper's
  tilt correction reads them, with a link to the cropper page's subsection.
- `docs/testing.md`: rows for the two new test files.
- `CLAUDE.md`: the "Common Commands" cropper line gains `[--tilt-correction]`; the CropRunner architecture
  entry gets one new bullet (the correction, the pose rule, `no_pose`, beta per era, sizing at stored y,
  the marker keys) and its items 2, 3 and 6 and the "three non-disjoint keys" bullet are updated for the
  new bucket; the "depth planes are in the rig frame" section's "No correction has landed in CropRunner
  yet" becomes a pointer to the new bullet. **Keep the net addition to CLAUDE.md under 1,500 characters**:
  the file is at the tool's 150k-character limit, and PR #192 pins that with a test. Point at
  `docs/cropper.md` for the rest.

### 5. Decisions this plan makes (surface each in the PR description)

1. **Beta scales the pose**, not the pixel move (above).
2. **The window is sized at the stored `pano_y`, and the corrected point only positions it.** v2's
   regression was fit on stored coordinates; v3's depression is the object's depression below the gravity
   horizon, which is what the stored (gravity-frame) y encodes. Re-fitting on corrected coordinates is the
   follow-up the handoff names, once beta is set; a rule change there is its own PR.
3. **`unknown` is a third era with its own beta.** The study's eras are by `time_created`, which cvMetadata
   does not serve, so every label on the production `-d` path is of unknown era. Making that a named entry
   (default 1.0, the pre-set rule) keeps the per-era table honest instead of silently applying one era's
   value to everything. Getting `time_created` into cvMetadata is a SidewalkWebpage request.
4. **`no_pose` is a skip, not an error**, for absent and unreadable pose files alike; the `crop.log` line
   says which.
5. **The marker records the correction as constants (`0.0` when off) plus an `on`/`off` string; the
   manifest is untouched.**

### 6. Branch, commits, PR

- Work only in this worktree (branch `191-tilt-correction`, from `origin/master`). Do not touch
  `191-tilt-beta-batch`, and never `git stash` (the stash list is shared repo-wide and holds Jon's drafts).
- Commits in sensible units (the module move and shim; the CropRunner change; tests; docs), each message
  saying why, ending with the attribution line your instructions give. Commit this plan file with the PR.
- Push and open the PR against `master`: title
  `CropRunner: opt-in tilt correction to the rig pixel, beta per era (#191)`; body with what it does, the
  five decisions above as a list Jon can answer, the test evidence, and `Refs #191, Refs #54, Refs #190`
  (never `Closes`). Give the full GitHub URL for every issue and PR number in the body.
- Report back: the PR URL, the test and coverage numbers, and anything in this plan you could not do or
  did differently, with the reason.
