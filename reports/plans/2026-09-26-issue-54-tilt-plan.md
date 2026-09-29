> **Committed 2026-09-29 as the record of the design** that [the tilt study](../2026-09-26-tilt-error-study.md)
> was built from, verbatim except that the store host's mount point and home directory are redacted.
> It is the plan as written on 2026-09-26, before any measurement: several of its premises were
> overturned (the pose sign in §0.2/§2.1, the depth z axis, C's draw and decision rule), and each such
> change is listed in the report's "Wrong turns" and "Deviations from the plan" and in
> [the redraw's decision log](../data/2026-09-29-tilt-adjudication-jm/DECISIONS.md). Read the report for results.

# Plan — issue #54: measure the tilt-induced y-error at crop level

Repo: `ProjectSidewalk/sidewalk-panorama-tools`, base `origin/master` @ `c5d2ad7`. Planner: Fable 5.1
(read-only pass, 2026-09-26). Implementer: Opus 5.5, fresh worktree. Issue:
https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54. Upstream diagnosis:
https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4784.

This is a **measurement**, and by the end of planning it is a measurement whose answer is *not* what
either the issue or its two loudest priors assumed. Read §0 first; it changes what the study is for.

---

## 0. What planning found (verify every item; none is a result yet)

Every premise of the issue was checked against master and against the production store. Five things
moved, three of them decisively.

### 0.1 Per-pano tilt is on the store for BOTH eras, not fetchable-for-34%

* **Modern era.** `<pano_id>.depth.npz` (format v3) carries `heading`/`pitch`/`roll` in radians for
  every GSV pano alive when the depth phase ran. Measured on makelab2 (`seattle-wa`): **49,794
  `.depth.npz`** (`depth_log.csv`: 49,794 `saved` / 52,188 `unavailable`, 48.8% alive) against
  169,384 panorama JPEGs.
* **Legacy era — the part nobody knew.** The 2019 scrapes left the dead XML endpoint's metadata
  beside each pano: **77,142 `<pano_id>.xml`** in `seattle-wa` (plus 77,142 `.depth.txt`). Each
  carries `<projection_properties pano_yaw_deg tilt_yaw_deg tilt_pitch_deg/>`, e.g.
  `_03OWjj8xH_WkWLSZHZ0mg.xml`: `pano_yaw_deg="269.22998" tilt_yaw_deg="110.909996"
  tilt_pitch_deg="4.73"`. **This is rig tilt for legacy panos regardless of whether Google still
  serves them**, which the issue's "Honest limits" said was impossible. It is the GSV-viewer-era
  arm's pose source.
* The two conventions agree where both exist. On the two panos in shards `_0`/`_1` with both files:
  `_0BRuxqEWhJEkiQV7ah7ug` npz (heading 15.7°, pitch −0.393°, roll 1.076°) vs xml (`pano_yaw`
  15.72, `tilt_yaw` −53.9, `tilt_pitch` 1.21): magnitude √(0.393²+1.076²)=1.145 vs 1.21, and
  `tilt_yaw − pano_yaw = −69.6°` vs `atan2(−roll, −pitch) = −69.9°`. `_1mGYskQjLo03z9g0yiDvw`: 0.269
  vs 0.37 magnitude, 111.95° vs 109.3°. **Working hypothesis for the XML convention**
  (`tilt_pitch_deg` = total tilt magnitude; `tilt_yaw_deg − pano_yaw_deg = atan2(−roll, −pitch)` in
  streetlevel's radians-converted sign) — to be *re-derived on the full overlap* (§4.3), not trusted
  from n=2.

### 0.2 The depth artifact's frame is the RIG frame, exactly (n=12, to be redone at n≈5,000)

Google's depth model is terrain plus **extruded building footprints** (`docs/depth.md`), so facade
planes are plumb by construction in Google's world frame. Inspecting 12 Seattle artifacts remotely
(script in §11.2): every facade normal with ≥300 px of support is tilted from horizontal by **exactly
the metadata pitch/roll**, resolved by the facade's bearing. Three of twelve, verbatim (elev = asin(n_z/‖n‖)
in degrees, bearing per §2.1's convention):

| pano | npz pitch / roll | facade bearing → normal elevation |
|---|---|---|
| `_1AjTE2xG0H3wnCekkFY1g` | −2.297 / 1.647 | forward −2.361 · backward +2.244 · left +1.719, +1.646 |
| `_1dVBerQJVPT-MDyGw-1kg` | 2.322 / 1.822 | forward +2.376 · backward −2.37 · right −1.738 |
| `_1KKwnoO0oZVxG9DeemA9A` | 4.006 / −0.523 | backward −4.014, −4.032 · right +0.532 |

and the dominant ground plane is at **exactly** (0,0,−1) in several artifacts (`elev −90.0, az 0`,
e.g. `_0awmGFiA5muBkaeXNkgIQ` with pitch 3.1°) — the road is perpendicular to the rig's up, i.e.
the rig sits on the road and the metadata pitch is mostly road grade. So: **the depth planes are
expressed in the rig frame, and the metadata (pitch, roll) is the rig's attitude relative to gravity.**
The relation, in streetlevel's sign (pitch = 90 − raw, roll raw, both wrapped to (−180, 180]):

    elevation_rig(Δb) = −(pitch·cos Δb + roll·sin Δb)  =  −T(Δb)        (Δb = bearing from forward, clockwise +)

which is the prereg's `T` with the `+` relative sign (reports/2026-08-09-crop-priors-prereg.md §2.2
left the relative sign open; this is the first measurement of it). This is zero-gold, exact to ~0.05°,
and costs no pixels. It is endpoint F1.

### 0.3 The stitched tiles look rig-aligned too — in BOTH eras (n=2, prototype estimator)

A ~30-line vertical-edge-lean prototype (§11.3) over the two fetched panos (one 2025 scrape with npz
pose, one 2019 scrape with XML pose), 12 bearing bins, band −25°…+35° elevation, first-order
prediction under "tiles are rig-aligned" `lean(Δb) = pitch·sin Δb − roll·cos Δb`:

```
_1KKwnoO0oZVxG9DeemA9A.jpg (2025 scrape; npz pitch 4.006, roll -0.523)
  bin  measured  predicted        bin  measured  predicted
   0    -0.88     -1.54            6    +0.06     +1.54
   1    -2.63     -3.20            7    +0.89     +3.20
   2    -1.00     -4.00            8    +1.43     +4.00
   3    -3.28     -3.73            9    +1.58     +3.73
   4    -0.30     -2.46           10    +1.35     +2.46
   5    -0.24     -0.53           11    -0.06     +0.53
_03OWjj8xH_WkWLSZHZ0mg.jpg (2019 scrape; xml -> pitch 4.395, roll 1.747)
   0    -0.24     +0.55            6    -0.54     -0.55
   1    -1.67     -1.87            7    +2.33     +1.87
   2    -3.79     -3.79            8    +2.57     +3.79
   3    -2.62     -4.70            9    +3.10     +4.70
   4    -3.64     -4.34           10    +3.27     +4.34
   5    -2.18     -2.82           11    +1.55     +2.82
```

The phase matches in both (zero crossings where predicted, sign pattern exact), amplitude ~0.6–1.0×
(the prototype is deliberately crude and attenuates; the real estimator is calibrated, §5). In a
gravity-rectified equirectangular every world-vertical edge is a straight column and this table would
be noise around 0. **Two panos are not a result**, but they are enough to say that the claim carried
by sidewalk-auto-labeler#42/#52 and `geo._world_ray`'s docstring ("streetlevel's GSV equirectangulars
are already gravity-rectified") and by SidewalkWebpage#5174 ("the stored panoramas are
gravity-aligned") is *not* something this study may assume. Both of those were inferred indirectly
(multi-view raycast agreement; nine labels shifted by `camera_pitch` alone — see 0.5). Endpoint F2
measures it directly on the store's own pixels, per era.

### 0.4 There is no point gold, and the RampNet extent gold cannot serve

* `reports/scripts/.cache/annotation/annotations/jon/` holds **one** file. Study 1's gold does not
  exist.
* RampNet's `boxes.json` `point` is **RampNet's own detection / reviewer point in the pixel frame**,
  not a Project Sidewalk stored click: `records.jsonl` rows carry `detections[].x_normalized` and no
  `label_id` (checked `benchmark/annapolis`). A pixel-frame point vs a pixel-frame box can carry no
  tilt term by construction. Instrument (b) as posed in the brief is void; §3.4 keeps only an
  opportunistic PS-label × gold-box overlap arm, count-reported.
* Richmond's 267 rawLabels rows are **human** clicks (two `user_id`s, `canvas_x` 148–622, not the
  AI's fixed 360/240), with `camera_pitch`/`camera_roll` for 100% — but they went through PS's
  Mapillary viewer, whose frame handling is a SidewalkWebpage question. Mapillary is a desk arm
  (§3.5), not a measurement here.

### 0.5 SidewalkWebpage#5174 is the closest prior test of the crop endpoint, and it has a gap

#5174 (2026-09-04) verified on 9 labels / 5 panos at |camera_pitch| 8.1–9.1° that the labelled feature
sits at the stored `pano_y` in the self-hosted copy, and concluded the panoramas are gravity-aligned.
But it shifted by the **full `camera_pitch`**, while under the rig hypothesis the shift at a label is
`T(Δb) = pitch·cos Δb + roll·sin Δb` — which is ≈0 for a label to the side of the car (Δb ≈ ±90°),
where sidewalk labels mostly are. A side label at 9° pitch can sit exactly at its stored row under
*either* hypothesis. So #5174 bounds the crop endpoint only for labels near Δb≈0/180°, and says
nothing about roll. Endpoint C (§3.3) is that test done right: shift by T(Δb), select on |T|, both
signs, blind.

### 0.6 Consequence for the study's shape

If tiles are rig-aligned (0.3) *and* Google's labelling viewer levelled the horizon, #4784's mechanism
is live and crops are off by `T·h/180` px (p90 ≈ 2–3° → 90–135 px on 8192). If tiles are rig-aligned
and the viewer did **not** level, everything is in one frame, `pano_y` is clean, and the y-error note
in the README is something else (#4842's covariates). If tiles are gravity-rectified, crops are clean
regardless. **Three endpoints, in dependence order:** F1 (depth frame, exact, cheap) → F2 (tile
frame, pixels) → C (object-at-`pano_y`, the crop endpoint, human-adjudicated at extreme |T|). F2
decides whether C can even be non-null; C decides #54.

---

## 1. Scientific design

### 1.1 Estimands

Let `y_obj` be the pixel row of a label's referent in the stored JPEG and `y_st` the stored `pano_y`.

* **E_crop** = `y_obj − y_st` in degrees (`× 180/h`), as a function of `T(Δb)`. The crop is centred
  at `y_st`, so E_crop is the mis-centering. Hypothesis H_leak: `E_crop = β·T` with β≈1 (the
  #4784 leak). H_0: β≈0.
* **E_tile** ("which frame are the tiles in"): the vertical-edge lean of world-vertical structure as a
  function of bearing, `lean(Δb) = β_p·pitch·sin Δb − β_r·roll·cos Δb`. β≈1 → rig-aligned tiles;
  β≈0 → gravity-rectified.
* **E_depth** ("which frame are the planes in"): facade-normal elevation
  `el_f = −β_p·pitch·cos Δb_f − β_r·roll·sin Δb_f`.

`Δb` for a label is **computed from stored pixels alone**: `Δb = (pano_x/pano_width)·360 − 180`
(prereg §2.2 — the raster is heading-centred so `camera_heading` cancels; do not reintroduce a
heading term).

### 1.2 Endpoints, decision rules, samples

All regressions are the prereg's **two-coefficient, sign-robust** form (β_p, β_r separately; the
null is β_p=β_r=0 under either sign convention, and the fitted signs *report* the convention).
Cluster-robust SEs by pano; α = 0.05.

| | Endpoint | Instrument | Sample (available now) | Decision rule |
|---|---|---|---|---|
| **F1** | E_depth | facade normals from `.depth.npz` (§4) | all `seattle-wa` artifacts (49,794) + every corpus-pano artifact (~350) | both CIs ⊂ [0.9, 1.1] → rig frame; ⊂ [−0.1, 0.1] → gravity; else report |
| **F2** (primary for the frame question) | E_tile | calibrated vertical-edge lean (§5), per era arm | (a) 666 cached corpus panos with pose (~350 npz + ~135 xml); (b) store-side: 300 most-tilted + 300 random 2025-scrape Seattle panos with npz; 300 most-tilted + 300 random 2019-scrape panos with xml | calibrated β_p, β_r both ⊂ [0.7, 1.3] → rig-aligned; both ⊂ [−0.3, 0.3] → gravity-rectified; else partial, no code change |
| **C** (primary for #54) | E_crop | blind three-way forced choice at \|T\| ≥ 4° (§6) | 48 corpus labels: 24 legacy/mid (2019-scrape panos, xml pose) + 24 post-179 (npz pose); the pool is the 763-label corpus, whose panos are already local | ≥ 90% "stored" (binomial vs 1/3: p < 10⁻¹⁵) → clean, no correction; ≥ 90% one shifted window → confirmed, sign recorded; else report the split, no code change |
| S1 | tilt prior, by era | the scan CSV (§4) | 49,794 npz + 77,142 xml, Seattle | descriptive: \|pitch\|,\|roll\| p50/p90/p99, sd(T) — replaces the 651-pano sampled prior |
| S2 | XML↔npz convention + re-render detector | overlap panos with both files | every Seattle pano with both (estimate: a few thousand) | fit the mapping (§4.3); report the residual and the share of panos where the two disagree by > 1° (Google re-rendered ~24% of survivors, 2026-09-05 pilot) |
| S3 | the decisive number | arithmetic on C's β and S1's T distribution, through `CropRunner.crop_window_fov_deg` | — | mis-centering as fraction of the v2 window height per depression band, under the fitted β *and* under the worst-case β=1 bound |
| S4 (opportunistic) | E_crop on extent gold | PS human CurbRamp labels on RampNet benchmark panos (sao_paulo, paterson) vs the gold apron | count first; analyse only if ≥ 30 pairs | descriptive; never decision-bearing |

**What a null means, per arm.** F2 null (β≈0) for an era ⇒ tiles gravity-rectified ⇒ crops clean for
that era with no further test (report C anyway as the confirmatory). F2 rig-aligned + C "stored" ⇒
one frame throughout: `pano_y` is pixel-true, Google's viewer did not level, #4784's GSV-viewer-era
mechanism is refuted for GSV, and the README's y-error is placement/off-axis (#4842). F2 rig-aligned +
C "shifted" ⇒ #4784 confirmed at crop level; the correction is `y_rig = y_st + sign·T·h/180`, applied
per pano from the `.npz`/`.xml` beside it (§9.2, a *separate* PR).

**Power.** F1: residual ≈0.05° against sd(T)≈1.5° over thousands of facades — unconstrained. F2: the
prototype shows per-bin agreement to ~1° on single panos; with ≥ 300 panos × 12 bins per arm and pano
fixed effects, SE(β) ≪ 0.1 even at 3× the prototype's noise. C: binomial as above. The prereg's SE
arithmetic (§5) is superseded by the store-wide T distribution from S1 — quote S1, not the census.

### 1.3 Corresponding prereg endpoint

Endpoint 2 of Study 1 (reports/2026-08-09-crop-priors-prereg.md §2.2) is the tilt regression on gold
Δel. This plan runs it with **C as the gold-lite instrument** (forced choice replaces a continuous
Δel — the study reports a *sign and a rate*, not a slope) and adds F1/F2, which §2.2 did not foresee
because the artifact did not exist. Record this in the prereg's §7 decision log? **No — §7 is outside
this implementer's file ownership.** Put the amendment text in the new report's "Consequences for the
older reports" section and list "append a §7 entry" as an open item for Jon (§9.1).

### 1.4 Frames and eras the report must keep apart

* **Scrape era** (what pixels are on the store): 2019 stitch (has `.xml`, JPEG mtime 2019) vs 2025–26
  stitch (`.npz`, no `.xml`). Google re-renders panos; the 2019 file is the only copy of what the
  labeller saw for legacy labels.
* **Label era** (rawlabels `era`: legacy / mid / post179) — the client that wrote `pano_y`.
* **Pose source** (xml vs npz) — must match the scrape era of the JPEG under test; an npz pose fetched
  in 2026 may describe a re-rendered pano (S2 measures how often that bites).

---

## 2. The pure function module — `reports/scripts/tilt_geometry.py`

Placement: `reports/scripts/` (omitted from coverage by `.coveragerc`'s `reports/*`, so **no
`.coveragerc`/`tests/test_coverage_config.py` change**). Nothing in `CropRunner.py` moves until C
confirms. numpy only (no pandas here — the module must also run on makelab2, Python 3.9 / numpy 1.23.5,
so **no `match`, no `|` type unions, no 3.10+ stdlib**; add a module comment saying so).

### 2.1 Conventions (write these into the module docstring verbatim)

* **Bearing Δb**: degrees clockwise from the pano's forward (heading) direction, in (−180, 180].
  **Elevation el**: degrees above the horizon.
* **Pixels** (heading-centred raster, `pov_replay.pano_xy_from_pov`'s convention, continuous):
  `x = ((Δb + 180)/360)·w  mod w`, `y = (0.5 − el/180)·h`; inverse `Δb = (x/w)·360 − 180`,
  `el = 90 − (y/h)·180`. The pixel *centre* convention of `docs/depth.md` (`(c+0.5)/w`) applies when
  converting an integer column of the 512×256 depth raster; document both and test both.
* **Artifact frame (from `_write_depth_artifact`'s ray formula)**: `x = +left, y = +backward,
  z = +up` (derivation: column `w/2` has φ = 3π/2 → `(0,−1,0)`; column `w/4` has φ = 0 → `(1,0,0)` and
  is 90° left of forward). So for a plane normal `n`: `Δb = atan2(−n_x, −n_y)`,
  `el = asin(n_z/‖n‖)`. Pin with a test that reproduces `docs/depth.md`'s `v(r, c)` from
  `direction_rfu` after the axis map (§8, `test_artifact_axes_reproduce_the_documented_ray_formula`).
* **Internal frame RFU** (right, forward, up), right-handed. `direction_rfu(Δb, el) =
  (cos el·sin Δb, cos el·cos Δb, sin el)`.
* **Pose**: `pitch_deg`, `roll_deg` in streetlevel's sign as stored in the `.npz` after wrapping to
  (−180, 180] (`wrap_deg`); the module states the *measured* meaning (0.2): **pitch > 0 = forward axis
  raised (nose up); roll > 0 = left side down**, i.e. a gravity-horizontal direction at bearing Δb has
  rig elevation `−(pitch·cos Δb + roll·sin Δb)`. Write it as "measured on 12 artifacts 2026-09-26,
  re-measured by tests/test_tilt_error_study.py against the committed scan" so the next reader knows
  it is evidence, not convention.

### 2.2 API (signatures; all accept scalars or numpy arrays, degrees in/out)

```python
def wrap_deg(a)                                   # -> (-180, 180]; 359.6 -> -0.4 (photometa roll lives in [0, 360))
def direction_rfu(bearing_deg, elevation_deg)     # -> (..., 3) unit vectors
def bearing_elevation(v)                          # -> (bearing_deg, elevation_deg); inverse of direction_rfu
def rig_from_gravity(pitch_deg, roll_deg)         # -> 3x3 R with v_rig = R @ v_gravity (see order below)
def gravity_to_rig(bearing_deg, elevation_deg, pitch_deg, roll_deg)   # exact -> (bearing_rig, el_rig)
def rig_to_gravity(bearing_deg, elevation_deg, pitch_deg, roll_deg)   # exact inverse
def tilt_term_deg(bearing_deg, pitch_deg, roll_deg)                   # T = pitch cos b + roll sin b (first order)
def vertical_lean_deg(bearing_deg, pitch_deg, roll_deg)               # dT/db = pitch sin b - roll cos b (first order)
def pixel_from_bearing_elevation(bearing_deg, elevation_deg, pano_width, pano_height)   # continuous x, y
def bearing_elevation_from_pixel(x, y, pano_width, pano_height)
def rig_pixel_from_gravity_pixel(x, y, pano_width, pano_height, pitch_deg, roll_deg)    # the correction CropRunner WOULD apply
def gravity_pixel_from_rig_pixel(...)                                                   # its inverse
def artifact_normal_bearing_elevation(n)          # n (..., 3) in the artifact frame -> (Δb, el); zero-norm -> nan
def xml_tilt_to_pitch_roll(pano_yaw_deg, tilt_yaw_deg, tilt_pitch_deg)   # §4.3's fitted mapping; see below
```

`rig_from_gravity`: intrinsic pitch about the right axis, then roll about the (pitched) forward axis —
the same order as auto-labeler `geo._world_ray` (yaw is not needed: bearings are already relative to
forward). Second-order terms are < 0.01° for |tilt| ≤ 3°, so the order is a documentation choice, but
pin it with a test so it cannot silently change. Operational sign pins (tests): `gravity_to_rig(0, 0,
p, 0) == (0, −p)`, `gravity_to_rig(180, 0, p, 0) == (180, +p)`, `gravity_to_rig(−90, 0, 0, r) == (−90,
+r)`, `gravity_to_rig(+90, 0, 0, r) == (90, −r)`.

`xml_tilt_to_pitch_roll`: implement the 0.1 hypothesis `magnitude = tilt_pitch_deg`, `direction =
tilt_yaw_deg − pano_yaw_deg = atan2(−roll, −pitch)` ⇒ `pitch = −m·cos(dir)`, `roll = −m·sin(dir)`,
**but make the study fit it** (§4.3) and pin the function against the committed overlap table: the
test asserts median |Δpitch|, |Δroll| between the function's output and the npz values over the
overlap ≤ 0.3° (mutants: swap sin/cos, drop a minus → medians jump to degrees).

---

## 3. Instruments (what each script does)

### 3.1 `reports/scripts/tilt_pose_scan.py` — the store scan (runs on makelab2, Python 3.9)

Self-contained (`csv`, `numpy`, `xml.etree`, `os`, `argparse`; **no pandas, no repo imports** — it is
`put` to the remote box). Walks `<store>/<city>/<2-char shard>/` and emits three CSVs:

1. `pose_<city>.csv`: `pano_id, jpg_present, jpg_bytes, jpg_mtime_iso, jpg_width, jpg_height, npz_present,
   npz_format_version, heading_deg, pitch_deg, roll_deg, xml_present, xml_pano_yaw_deg, xml_tilt_yaw_deg,
   xml_tilt_pitch_deg, xml_image_width, xml_image_height`. Degrees, wrapped. JPEG dims from the header
   only (reuse the two-marker SOF reader shape of `downloaders.common.jpeg_dimensions` — copy the
   ~20 lines with a comment naming the original, since the remote copy cannot import it; add a test
   that the copy agrees with `downloaders.common.jpeg_dimensions` on `samples/sample_pano.jpg`).
2. `facades_<city>.csv` (only for `--facades` panos, default a seeded random 5,000 + an `--ids` list):
   `pano_id, plane_index, support_px, n_x, n_y, n_z, d`, for planes with `|n_z|/‖n‖ < 0.5` and
   support ≥ 300 px; plus the dominant ground plane per pano (`ground_` prefixed columns in the pose
   CSV: `ground_support, ground_elev_deg, ground_bearing_deg, ground_dist_m` via the same rule as
   `gsv.ground_plane_from_artifact` — copy the rule, cite it).
3. Progress to stderr every 1,000 panos; **resumable** (`--resume` skips ids already in the output);
   `--ids FILE` restricts to a list (the corpus panos in 35 cities).

The "is this file a panorama" rule must match `downloaders.common.is_downscaled_sidecar` (`.w8192.jpg`
is not a pano): a test imports both and compares over a fixture list of names.

**Runtime.** `np.load(...)['pitch']` reads one small zip member; the facade pass decompresses
`plane_indices` (~130 KB) — budget ~5 ms/artifact → Seattle in ≲ 10 min; the JPEG header read is one
`open`+`read(64 KB)` per file, ~170k files → a few minutes on local ZFS. Run with `nohup` and poll
(§7.2).

### 3.2 `reports/scripts/tilt_frame.py` — the tile-frame estimator (runs locally AND on makelab2)

Pure numpy + PIL, 3.9-compatible. Public functions:

```python
def load_reduced_gray(path, factor=4)            # PIL draft/reduce; raises MAX_IMAGE_PIXELS (16384x8192 = 134 MP)
def lean_profile(gray, n_bins=12, el_lo=-25.0, el_hi=35.0, mag_percentile=95.0, max_abs_lean_deg=12.0)
    # -> list of (bin_centre_bearing_deg, weighted_mean_lean_deg | None, n_pixels)
def predicted_lean(bin_centres_deg, pitch_deg, roll_deg)       # tilt_geometry.vertical_lean_deg
def warp_with_extra_tilt(gray, extra_pitch_deg, extra_roll_deg)  # resample: for each rig' pixel, direction -> R_extra^T -> source pixel (bilinear); used ONLY for calibration
def calibrate(gray, pitch_deg, roll_deg, extra=(2.0, 0.0)) -> (measured_before, measured_after, predicted_delta)
def main(argv)  # --panos CSV(pano_id,path,pitch_deg,roll_deg,arm) --out CSV; one row per (pano, bin, before|after-calibration)
```

Estimator (the prototype, made honest): Sobel gradients on the reduced grayscale band; edge
orientation `ang = atan2(gy, gx)` folded to (−90, 90]; keep the top-5% magnitudes with |ang| < 12°;
per bearing bin the magnitude-weighted mean `ang` = lean (deg, positive = top of the edge displaced
toward +x). Known biases: the ±12° window truncates large leans (attenuation), road markings and
perspective-leaning edges add scene noise. **Calibration is part of the instrument**: for every pano
also measure the profile after `warp_with_extra_tilt(+2° pitch)` (and, for a 1-in-4 subsample, +2°
roll); the per-arm attenuation `a = slope(Δmeasured on Δpredicted)` is estimated by regression and
`β_calibrated = β_raw / a`. Report `a` (expect 0.6–0.9) and both βs. The warp is exact geometry through
`tilt_geometry` (test: warping by (p, r) then measuring `predicted_lean` differences reproduces the
first-order formula on a synthetic pole image within 5%).

### 3.3 `reports/scripts/tilt_adjudicate.py` — endpoint C's instrument

Input: the corpus CSV (`reports/data/2026-08-12-crop-corpus-gsv.csv.gz`), the pose CSV, the local pano
cache. Selection: labels with `measurable == True` **recomputed via `rawlabels.study_measurable`**
(never the CSV's stale column — CLAUDE.md), CurbRamp/NoCurbRamp/Obstacle/SurfaceProblem, with a pose
for the pano *matching the JPEG's scrape era* (xml for 2019 files, npz otherwise), `|T(Δb)| ≥ 4°`
(≥ 182 px on 8192; the three windows do not overlap), stratified 24 legacy+mid / 24 post179, ordered by
`label_uid` hash under a fixed seed. If a stratum has fewer than 24 candidates, take all and **report
the shortfall**; widen to |T| ≥ 3° only as a recorded second draw.

Per label it cuts three 3:2 windows of the v2 size (`CropRunner.crop_window_width` + `compute_crop_box`
+ `extract_crop`, exactly the production cut) centred at `y_st`, `y_st + T·h/180`, `y_st − T·h/180`
(same `pano_x`), pastes them side by side in a **random order per label**, with the label type and tags
under the sheet, and writes:

* `.cache/tilt/adjudication/sheets/<uid>.jpg` — annotator-facing, no coordinates in the name;
* `.cache/tilt/adjudication/key.json` — `{uid: {"order": ["stored","plus","minus"], "T_deg": …, "pano_y": …, "pitch": …, "roll": …, "era": …, "pose_source": …}}` — **never shown**;
* `.cache/tilt/adjudication/verdicts.jsonl` — appended by `--record UID A|B|C|none` (or a tiny
  loopback page reusing `annotate_server.py`'s pattern; the CLI is enough for 48).

`score(verdicts, key)` returns counts of stored/plus/minus/none per era arm, the binomial p-value
against 1/3, and the list of "none". Commit the sheets' *thumbnails* (one contact sheet figure, §8)
and the key+verdicts as `reports/data/2026-09-26-tilt-adjudication.json` (after adjudication the key is
no longer secret; the blind is in the *ordering at judgement time*, which the JSON records).

Who judges: **Jon** (open decision §9.1); the implementer may judge a first pass and label it so, but
the committed verdicts should be Jon's or both (agreement reported).

### 3.4 Opportunistic gold arm (S4) — inside `tilt_error_study.py`, no new instrument

For `sao_paulo` and `paterson` (GSV) bundles under `D:\Git\RampNet\benchmark\<city>` (branch note:
Paterson's boxes live on `data/paterson-extent-gold`; **read only, do not check out branches in that
repo** — use `git -C D:\Git\RampNet show data/paterson-extent-gold:benchmark/paterson/boxes.json` into
the cache), join RampNet `panorama_id` to `rawlabels-all/{sao-paulo-brazil,paterson-nj}.csv` CurbRamp
human labels on `pano_id` (str), keep pairs whose stored `pano_x` falls inside a gold box's x-span and
whose `pano_y` is within one box-height of the box bottom, and report **the count**. If ≥ 30, compute
`Δel = (box_bottom_y − pano_y)·180/h` against `T(Δb)` with the pose from the scan (both cities need a
scan pass with `--ids`). Named `populations.s4_extent_gold`, descriptive only.

### 3.5 Mapillary — a desk paragraph, not a measurement

State in the report: Richmond's labels are human; `camera_pitch`/`camera_roll` come from PS's own
`MapillaryViewer.extractPitchRoll`; whether PS's Mapillary viewer renders levelled and whether
`povToPanoCoord` adds a tilt term for that source are SidewalkWebpage facts this repo cannot verify
offline. Cite auto-labeler#42's finding that the *Pannellum* wrapper ignores `cameraPitch`. Leave the
Mapillary crop endpoint **open**, filed as an item under #4784 (§9.1).

---

## 4. Store access at implementation time

Host: makelab2, store root `<store-root>` (per-city dirs,
2-char shards; measured 2026-09-26). Python 3.9.25, numpy 1.23.5, PIL 10.0.1, scipy 1.9.3, 48 cores,
188 GB RAM. **One ssh call at a time; never fan out** (rate-ban). `run` **consumes stdin**, so a
heredoc inside `run` fails (`bash: line 1: ... <<: No such file or directory` — measured); use `put`
for files and `script` for a local `.sh` that itself contains the heredoc.

### 4.1 Uploads (3 `put` calls)

```powershell
& 'wsl-ssh.ps1' makelab2 put '<worktree>\reports\scripts\tilt_geometry.py'  '~/tilt54/tilt_geometry.py'
& 'wsl-ssh.ps1' makelab2 put '<worktree>\reports\scripts\tilt_pose_scan.py' '~/tilt54/tilt_pose_scan.py'
& 'wsl-ssh.ps1' makelab2 put '<worktree>\reports\scripts\tilt_frame.py'     '~/tilt54/tilt_frame.py'
```

plus one `put` of `corpus_pano_ids.csv` (`city,pano_id`, 668 rows, from the corpus CSV). `mkdir -p`
the dir first with `run`.

### 4.2 Runs (background + poll; each poll is one `run`)

```powershell
& 'wsl-ssh.ps1' makelab2 run 'cd ~/tilt54 && nohup python3 tilt_pose_scan.py <store-root> --city seattle-wa --facades 5000 --seed 20260926 --out pose_seattle-wa.csv --facades-out facades_seattle-wa.csv > scan_seattle.log 2>&1 &'
& 'wsl-ssh.ps1' makelab2 run 'cd ~/tilt54 && nohup python3 tilt_pose_scan.py <store-root> --ids corpus_pano_ids.csv --facades all --out pose_corpus.csv --facades-out facades_corpus.csv > scan_corpus.log 2>&1 &'
# poll (no faster than once a minute; use the Monitor tool or a single `run 'tail -2 scan_*.log'`)
```

Then select the store-side F2 panos **from the scan CSV locally** (top-300 |T-amplitude| = √(pitch²+roll²)
and 300 random, per scrape era; JPEG present; 16384×8192 or 13312×6656), `put` the selection CSV, and
run the estimator remotely:

```powershell
& 'wsl-ssh.ps1' makelab2 run 'cd ~/tilt54 && nohup python3 tilt_frame.py --panos frame_selection.csv --store <store-root> --calibrate --out lean_store.csv --workers 8 > frame.log 2>&1 &'
```

Budget: decode-at-1/4 of a 16384×8192 JPEG ≈ 1–2 s; 1,200 panos × 2 profiles / 8 workers ≈ 10 min.
Keep `--workers ≤ 8` (shared box).

### 4.3 Downloads (≤ 6 `get` calls, ≤ ~60 MB total)

`pose_seattle-wa.csv` (~170k rows, ~15 MB), `facades_seattle-wa.csv` (~5k panos × ~6 rows, ~2 MB),
`pose_corpus.csv`, `facades_corpus.csv`, `lean_store.csv` (~30k rows). Land in
`reports/scripts/.cache/tilt/` (gitignored by `.cache/`). **No JPEG fetches** beyond the two already
in the planner's scratchpad (`_1KKwnoO0oZVxG9DeemA9A`, `_03OWjj8xH_WkWLSZHZ0mg`; re-`get` them into
`.cache/tilt/probe/` for the regression check in §8 — 22 MB).

The XML↔npz convention (S2): from `pose_seattle-wa.csv`, take rows with both; fit `direction` and
`magnitude` as in §2.2 (grid over the eight sign/axis alternatives, pick the one minimising median
angular residual); commit the overlap table as
`reports/data/2026-09-26-tilt-xml-npz-overlap.csv.gz` (pano_id, npz pitch/roll, xml triple, jpg_mtime;
expect a few thousand rows, < 200 KB).

### 4.4 Fallbacks

* Scan too slow / box busy → limit to 6 shards per era plus the corpus ids; state the sampling in the
  artifact (`populations`).
* No xml for a corpus legacy pano → that label is ineligible for the legacy C arm; count it.
* If Jon prefers no remote compute: fetch only the corpus artifacts (`tar` of ~350 npz + ~135 xml,
  ~40 MB, one `script` call that tars to `~/tilt54/corpus_pose.tar` then one `get`) and run F1/F2
  locally on the 666 cached panos; drop the store-side arms and say so. F1 and C survive intact; F2's
  extreme-tilt arm is lost.
* Live streetlevel photometa fetch: **not needed** (every pose comes from the store); if the corpus
  scan shows < 200 panos with a pose, fetch ≤ 300 corpus panos via `gsv._fetch_pano_with_depth_planes`
  through a paced session — only with Jon's go-ahead (§9.1).

---

## 5. Study script — `reports/scripts/tilt_error_study.py`

Conventions (CLAUDE.md "Desk studies"): `studyfmt.fmt/num/percentile` only (no local copies — a test
asserts it, same as the others); `label_uid = city + ':' + label_id` with a uniqueness assert;
`pano_id` read as `str` everywhere (`dtype={'pano_id': str}`); every merge with `validate=`; every
emitted figure claimed by exactly one entry in `populations`; `json.dump(..., allow_nan=False,
indent=1, sort_keys=True, newline='\n')`; `print` for the operator summary only.

```
python reports/scripts/tilt_error_study.py \
    --corpus reports/data/2026-08-12-crop-corpus-gsv.csv.gz \
    --pose reports/scripts/.cache/tilt/pose_seattle-wa.csv --pose reports/scripts/.cache/tilt/pose_corpus.csv \
    --facades reports/scripts/.cache/tilt/facades_seattle-wa.csv --facades reports/scripts/.cache/tilt/facades_corpus.csv \
    --lean reports/scripts/.cache/tilt/lean_store.csv --lean reports/scripts/.cache/tilt/lean_corpus.csv \
    --adjudication reports/scripts/.cache/tilt/adjudication \
    [--rampnet-bundle sao_paulo=D:\Git\RampNet\benchmark\sao_paulo ...] \
    --write reports/data/2026-09-26-tilt-error-study.json \
    --figure-dir reports/figures
```

Functions (each unit-tested on synthetic frames, §8):

* `fit_two_coefficient(y, x_p, x_r, cluster)` → `{beta_p, beta_r, se_p, se_r, ci_p, ci_r, n, n_clusters, resid_sd}`
  — OLS with cluster-robust (CR1) SEs; numpy only; a test recovers planted (β_p, β_r) = (1, −1) and
  (0, 0) and detects a wrong-sign mutant.
* `facade_frame_fit(facades, pose)` (F1): y = `el_f`, x_p = `−pitch·cos Δb_f`, x_r = `−roll·sin Δb_f`.
* `tile_frame_fit(lean, pose, arm)` (F2): pano fixed effects (demean within pano), then the
  two-coefficient fit; `calibration_slope(lean)` from the before/after rows; `beta_calibrated`.
* `adjudication_summary(verdicts, key)` (C).
* `tilt_prior(pose, by='scrape_era')` (S1): |pitch|, |roll| p50/p90/p99, `sd_T` computed at the
  corpus's empirical Δb distribution (not at a uniform Δb — state which).
* `xml_npz_convention(pose)` (S2).
* `miscentering_table(beta, sd_T_by_band, T_p90_by_band)` (S3): for bands `<5, 5–15, 15–30, >30` deg
  and the band midpoints' distance via `pov_replay.predict_blend_distance`, window height from
  `CropRunner.crop_window_fov_deg(pano_y_at_mid, 8192) / CropRunner.CROP_ASPECT_W_OVER_H`, report
  `T_p90·β / window_height_deg` and the same with β=1 (the ceiling). Also the RampNet-geometry
  restatement from the issue comment: `T_p90 × (4096/180) px / 12 px σ`.
* `report_numbers(summary)` → the flat dict of every number the markdown quotes (the
  `TestReportMatchesTheArtifact` contract).

Artifact `reports/data/2026-09-26-tilt-error-study.json` top-level keys: `generated_from`
(input file names + row counts + md5s), `conventions` (the sign statements of §2.1 as data),
`f1_depth_frame`, `f2_tile_frame` (per arm: `store_2019_xml`, `store_2025_npz`, `corpus_2019_xml`,
`corpus_npz`, each with raw and calibrated βs and `a`), `c_adjudication` (per era arm),
`s1_tilt_prior`, `s2_xml_npz`, `s3_miscentering`, `s4_extent_gold` (or `{"n_pairs": k, "analysed":
false}`), `populations` (name → keys covered, n, frame), `wrong_turns` (free text list — yes, in the
artifact too, so the report's list is transcribed).

Other committed data: `2026-09-26-tilt-pose-seattle.csv.gz` (per pano: `pano_id, scrape_era,
pose_source, pitch_deg, roll_deg, heading_deg, jpg_width, jpg_height` — ~5 MB gz; if > 10 MB commit
the era-stratified 20k-row sample and push the full table to the `projectsidewalk` HF org with the
revision cited), `2026-09-26-tilt-lean-measurements.csv.gz`, `2026-09-26-tilt-adjudication.json`,
`2026-09-26-tilt-xml-npz-overlap.csv.gz`. Figures: `2026-09-26-tilt-facade-frame.png` (F1 scatter,
el_f vs −T at the facade bearing, coloured by |support|), `2026-09-26-tilt-lean-profiles.png` (F2:
mean measured vs predicted lean per bin for the top-tilt quintile, per arm), `2026-09-26-tilt-horizon-examples.jpg`
(two panos with the rig-frame horizon curve drawn — the figure that makes 0.3 legible),
`2026-09-26-tilt-adjudication-sheet.jpg` (8 example sheets with verdicts). Use the dataviz skill's
palette rules for the two PNGs; matplotlib is available.

---

## 6. Report — `reports/2026-09-26-tilt-error-study.md`

Sections, in this order: the question · what changed since the issue (§0 of this plan, with numbers
from the artifact) · method per endpoint (swept vs sampled stated: F1 is a **sweep** of Seattle
artifacts; F2 store arms are **samples** by tilt rank; C is a stratified draw) · results (F1, F2 per
arm, C, S1–S4) · the decisive table (S3) · **wrong turns** (at minimum: the issue's "only fetchable
for alive panos" premise; instrument (b) on RampNet gold; the planner's first ground-plane frame test,
which is confounded by grade — road normal is vertical *in the rig frame* because the rig sits on the
road, so "does the ground normal track the metadata tilt" cannot distinguish the frames, and facades
were the fix; #5174's shift-by-`camera_pitch` gap; anything the implementer hits) · consequences for
older reports (prereg §2.2 relative sign now measured; the photometa census's tilt prior superseded by
S1; `docs/depth.md` sampling recipe's frame statement) · cross-repo consequences (auto-labeler
`_world_ray` docstring and #52's "gravity-rectified" premise; SidewalkWebpage#5174/#4784;
label-latlng-estimation's depression angle being rig-relative) · open questions.

Every number in prose is transcribed from `report_numbers(summary)`; the test asserts each appears in
the whitespace-collapsed markdown (pattern: `tests/test_crop_sizing_v2.py::report` fixture).

`reports/README.md`: append one index row dated 2026-09-26 (last row; `tests/test_reports_index.py`
enforces order, existence, filename-date match).

`docs/cropper.md`: replace nothing; add **one paragraph** right after the existing "There is small
but real error in the y-position…" paragraph (under "Before you train on these crops") stating the
measured verdict in one sentence per endpoint and linking the report. Keep the old paragraph (it is
history). `tests/test_docs.py` checks the relative link resolves.

`CLAUDE.md`: one new subsection under "Things that are easy to get wrong" — "**The depth artifact's
plane normals are in the rig frame, and so are the tiles (#54, measured 2026-09-26)**" — three
bullets: the frame/sign statement of §2.1, "`pano_y` is/is not in the pixel frame" per C's verdict,
and "the xml files beside 2019 scrapes carry legacy tilt; `tilt_geometry.xml_tilt_to_pitch_roll` is the
one conversion". Write the bullets from the artifact, not from this plan.

---

## 7. Implementation order (top to bottom)

1. **Worktree + venv check.** `git worktree add ../pt-54 origin/master`; confirm `python -m pytest
   tests/test_crop_sizing_v2.py -q` is green on the global 3.12 (per the brief; do not install anything).
2. **`tilt_geometry.py` test-first.** Write `tests/test_tilt_geometry.py` (§8.1) red; implement; green.
   Include the docs/depth.md ray-formula reproduction test *before* writing
   `artifact_normal_bearing_elevation`.
3. **`tilt_pose_scan.py` test-first** against a `tmp_path` store: two shards, one pano with npz (built
   with `conftest.encode_depth_payload` + `gsv._write_depth_artifact`), one with a synthetic xml, one
   `.w8192.jpg` decoy, one truncated npz (must be counted as `npz_error`, not crash).
4. **Remote scan** (§4.1–4.3). While it runs, step 5.
5. **`tilt_frame.py` test-first** (§8.3) with the synthetic pole renderer; then run it on the two probe
   panos and check the profiles reproduce §0.3's table within ±0.5° per bin (a regression check of the
   *prototype*, not a finding; do not commit those numbers as results).
6. **Local F2 corpus arm**: `tilt_frame.py --panos corpus_selection.csv --pano-root
   reports/scripts/.cache/panos --calibrate --out .cache/tilt/lean_corpus.csv` (666 panos × 2
   profiles at 1/4 decode ≈ 20–30 min on the desktop; `--workers 4`).
7. **Store F2 arm** remotely (§4.2), `get` results.
8. **`tilt_adjudicate.py`** test-first; generate the 48 sheets; **hand off to Jon** (or first-pass
   yourself, labelled). Do not look at `key.json` while judging.
9. **`tilt_error_study.py`** test-first on synthetic frames; run; write the artifact + figures; commit
   data; write `TestCommittedFindings` + `TestReportMatchesTheArtifact` **after** the numbers exist.
10. **Report, README row, docs/cropper.md paragraph, CLAUDE.md subsection.**
11. **Verification (§10)**, then PR (§9.3 for the body's shape; attribution per Jon's rules; no
    session link).

---

## 8. Tests — files, classes, and the mutant each must kill

All new test files start with the repo's `REPO_ROOT`/`SCRIPTS` sys.path shim (see
`tests/test_crop_sizing_v2.py`). Network-free; the store-side CSVs are read only through committed
copies under `reports/data/`.

### 8.1 `tests/test_tilt_geometry.py`

* `TestWrap::test_photometa_roll_wraps` — 359.6 → −0.4, 180 → 180, −180 → 180. Mutant: no wrap
  (photometa census wrong turn, |roll| p90 359.6°).
* `TestDirections::test_round_trip` (random bearings/elevations, 1e-9) · `test_forward_is_plus_y_right_is_plus_x`.
* `TestArtifactFrame::test_artifact_axes_reproduce_the_documented_ray_formula` — for a grid of (r, c)
  on a 512×256 raster build `v` from docs/depth.md's θ, φ literally, map through
  `artifact_normal_bearing_elevation`, and assert `(Δb, el)` equals the pixel-centre inverse of
  `pixel_from_bearing_elevation`. Mutant: `atan2(n_x, n_y)` (x sign) or `atan2(−n_x, n_y)` → fails
  at every column.
* `TestPose::test_sign_pins` (the four operational pins of §2.2) · `test_small_angle_agreement` —
  exact vs `tilt_term_deg` within 0.02° for |tilt| ≤ 3°, |el| ≤ 45° · `test_order_is_pitch_then_roll`
  — at (pitch, roll) = (10°, 10°) the exact result differs from the reverse order by a stated amount
  (pins the order) · `test_inverse` (`rig_to_gravity ∘ gravity_to_rig = id`).
* `TestPixels::test_matches_pov_replay_at_zero_tilt` — `pixel_from_bearing_elevation` against
  `pov_replay.pano_xy_from_pov(pov_heading, pov_pitch, camera_heading, w, h)` for random inputs, within
  0.5 px before rounding (mutant: off-by-half-pixel or 180/360 swap). · `test_rig_pixel_moves_by_T_over_180`
  — `rig_pixel_from_gravity_pixel(x, y, …)` − y ≈ `T·h/180` to first order.
* `TestVerticalLean::test_is_the_bearing_derivative_of_the_horizon` — finite difference of
  `gravity_to_rig(b, 0)` elevation in b equals `vertical_lean_deg` (mutant: sin/cos swap).
* `TestXmlConversion::test_synthetic_round_trip` (compose pitch/roll → xml triple → back) ·
  `test_matches_the_committed_overlap` (median residual ≤ 0.3° on
  `2026-09-26-tilt-xml-npz-overlap.csv.gz`; marked to skip with a clear reason until the file exists —
  and **un-skipped before the PR**).

### 8.2 `tests/test_tilt_pose_scan.py`

* `test_reads_pose_scalars_in_degrees_wrapped` (npz built with the real writer) · `test_reads_the_xml_triple`
  · `test_sidecar_is_not_a_pano` (compares against `downloaders.common.is_downscaled_sidecar` over
  `['a.jpg','a.w8192.jpg','a.depth.npz']`) · `test_jpeg_header_copy_agrees_with_common` on
  `samples/sample_pano.jpg` · `test_truncated_npz_is_an_error_row_not_a_crash` · `test_facade_filter`
  (a plane with |n_z|/‖n‖ = 0.6 excluded; 0.4 included; support < 300 excluded) ·
  `test_ground_rule_matches_gsv_helper` (same artifact → same plane index as
  `gsv.ground_plane_from_artifact`) · `test_resume_skips_done_ids` · `test_runs_under_python39_syntax`
  — `ast.parse` the three remote-run modules with `feature_version=(3, 9)` (mutant: a walrus is fine, a
  `match` or `X | None` annotation fails).

### 8.3 `tests/test_tilt_frame.py`

* Synthetic renderer fixture: `render_poles(w, h, pitch, roll, bearings, pole_width_deg)` — for each
  pixel compute the *rig* direction, rotate to gravity, paint if the gravity bearing is within a pole
  and |el| < 40°. Built on `tilt_geometry`, so it is the *same* convention the estimator is tested
  against (the estimator itself never calls the rotation, so this is not circular: it only measures
  edge angles).
* `test_untilted_poles_measure_zero_lean` (|lean| < 0.15° every bin) ·
  `test_tilted_poles_recover_the_predicted_profile` (β_raw ≥ 0.85 on clean synthetic; mutant: sign flip
  in `ang` fold → β ≈ −1) · `test_pitch_and_roll_phases_are_distinct` (pure pitch peaks at Δb=±90°,
  pure roll at 0/180°) · `test_warp_then_measure_matches_first_order` ·
  `test_calibration_slope_is_one_on_clean_synthetic` · `test_bins_with_too_few_pixels_are_none_not_zero`
  (studyfmt rule: undefined ≠ 0) · `test_probe_panos_reproduce_the_planning_profile` — **skipped unless**
  `.cache/tilt/probe/*.jpg` exist (opt-in, like the live-tile tests); asserts the 2019 pano's bin 2 ≈ −3.8 ± 0.6.

### 8.4 `tests/test_tilt_adjudicate.py`

* `test_windows_are_stored_plus_minus_T_in_pixels` (exact centre rows) ·
  `test_order_is_random_per_label_and_kept_only_in_the_key` (sheet filename and task record contain no
  coordinate; key does) · `test_selection_uses_the_live_measurable_rule` (a row whose CSV `measurable`
  is True but `rawlabels.study_measurable` is False is excluded — the CLAUDE.md trap) ·
  `test_selection_threshold_on_T_not_on_pitch` (a label with pitch 9°, Δb = 90°, roll 0 is **not**
  eligible — the #5174 gap, as a test) · `test_pose_source_matches_scrape_era` ·
  `test_score_counts_and_binomial` (48 stored of 48 → p < 1e−15; 16/16/16 → not significant) ·
  `test_none_is_its_own_bucket` (never dropped from the denominator).

### 8.5 `tests/test_tilt_error_study.py`

* `TestFit::test_recovers_planted_coefficients` ((1, −1) and (0, 0), clustered noise) ·
  `test_cluster_robust_se_grows_with_within_cluster_correlation` · `test_wrong_sign_mutant_is_detected`.
* `TestFacadeFrame::test_rig_frame_planes_give_beta_one` / `test_gravity_frame_planes_give_beta_zero`
  (synthetic facades built through `tilt_geometry`).
* `TestTileFrame::test_pano_fixed_effects_remove_a_per_pano_offset` · `test_calibration_divides_out_attenuation`.
* `TestMiscentering::test_fraction_uses_the_v2_window_height` (mutant: width instead of height → 1.5×)
  · `test_ceiling_row_is_beta_one` · `test_bands_are_the_prereg_bands`.
* `TestConventions::test_no_local_fmt_or_percentile` (AST: the study imports them from `studyfmt`) ·
  `test_label_uid_is_city_and_id` · `test_every_merge_validates` (AST scan for `merge(` calls lacking
  `validate=`) · `test_populations_claim_every_figure_exactly_once` · `test_artifact_is_strict_json`.
* `TestCommittedFindings` (after the run): F1 βs; F2 per-arm βs and calibration slopes; C counts; S1
  p50/p90; S2 residual; S3 table — each pinned to the JSON, **beside** the synthetic tests above (a
  committed-artifact test proves nothing about the code; CLAUDE.md).
* `TestReportMatchesTheArtifact::test_every_quoted_number_is_in_the_markdown` over
  `report_numbers(summary)`; `test_wrong_turns_are_listed` (each artifact `wrong_turns` entry's first
  eight words appear in the report).

Existing tests that will exercise the new files automatically: `test_reports_index.py`,
`test_committed_data_files.py`, `test_docs.py` (the docs/cropper.md link; the CLAUDE.md `docs/*.md`
paths), `test_csv_intake.py::test_no_production_module_imports_pandas` (unaffected — new modules are
under `reports/`).

---

## 9. Decisions, non-goals, and what happens on each verdict

### 9.1 Open decisions for Jon (cannot be settled from the repo)

1. **Who adjudicates endpoint C, and how blind.** Recommendation: Jon judges the 48 sheets (≈30 min),
   implementer records; if the implementer also judges, both verdict sets are committed and agreement
   reported.
2. **Remote compute on makelab2** (~15 min I/O scan over 170k files + ~10 min of 8-worker JPEG
   decoding). Recommendation: yes, off-hours; fallback §4.4.
3. **Scope of the scan**: Seattle only (planned) vs all 49 GSV cities' npz for a fleet-wide tilt prior
   (cheap to add, ~1 h). Recommendation: Seattle + the corpus ids now; fleet-wide as a follow-up issue.
4. **Live photometa fallback** if the corpus pose coverage is thin — only with a go-ahead.
5. **Cross-repo write-ups** after the PR: comments on SidewalkWebpage#4784 and #5174, sidewalk-auto-labeler#52,
   RampNet#113; a prereg §7 entry in this repo (outside the implementer's file list — Jon or a follow-up
   PR).
6. **The 77k legacy `.xml` files**: they are the only pose for dead panos and are not ingested anywhere
   — file a separate issue ("legacy tilt is a first-class artifact") rather than folding it in here.
7. If C **confirms** a leak: whether the correction lands in `CropRunner` here or waits for
   Planning#6's shared library (the issue's own open question).

### 9.2 Non-goals (explicitly out of this PR)

* No change to `CropRunner.py`, `downloaders/*`, `DownloadRunner.py`, `.coveragerc`, `config.py`.
* No crop-time correction lands, whatever C says. **If confirmed** (≥ 90% one shifted window in an
  era arm), the PR body states the correction as a sketch — `pano_y_rig = pano_y + s·T(Δb)·h/180` with
  `s` the measured sign, pose from `<pano_id>.depth.npz` or `<pano_id>.xml` beside the pano, applied
  in `label_position_in_crop`/`compute_crop_box` callers behind a flag, off by default — and files it
  as a new issue with the era scope, the regeneration question for validator-ai/tagger-ai/RampNet
  stage one, and the #32 coupling (`predict_crop_size` consumes `pano_y`).
* No Mapillary measurement; no changes to the prereg document; no annotation-tool changes.
* No live Google requests (unless 9.1-4 is granted).

### 9.3 PR shape

Title: `#54: measure the tilt-induced y-error at crop level — frame of the tiles and the depth
planes, adjudicated crop endpoint, store-wide tilt prior`. Body: the three verdicts in a table, the S3
table, links to the report, the list of cross-repo consequences, "what this PR does not change". No
session link. Attribution trailer per Jon's CLAUDE.md with this session's model string.

---

## 10. Risks and the verification the implementer runs before opening the PR

Risks:

* **Pose/pixel era mismatch.** An npz pose (fetched 2026-09) describes today's pano; the store JPEG may
  be a 2019 stitch of a since-re-rendered pano (24.4% of survivors were re-rendered, 2026-09-05 pilot).
  Mitigation: for 2019 files use the xml pose; S2 reports npz-vs-xml disagreement; F2/C never pair an
  npz pose with a 2019 JPEG.
* **JPEG mtime is not scrape date** if files were ever copied without preserving mtimes. Use *xml
  presence* as the primary 2019-scrape marker and mtime as secondary; report both.
* **Estimator scene bias** (leaning trees, perspective edges): pano fixed effects, calibration, and the
  two-coefficient form; report the top-tilt-quintile profiles so a reader can see the sinusoid.
* **makelab2 rate-ban**: one ssh call at a time, `open` first if `check` says no master.
* **Windows**: raise `Image.MAX_IMAGE_PIXELS` (`CropRunner.raise_decompression_bomb_ceiling()`), decode
  with `Image.draft('L', (w//4, h//4))` to keep memory ≈ 8 MB per pano; the heredoc-eats-backslashes
  trap — write scripts with the Write tool.
* **Human step in the loop** (C): schedule it early (step 8 can run before the store arms finish).
* **Overclaiming**: the report must separate "tiles are rig-aligned" (F2) from "pano_y is/isn't in the
  pixel frame" (C); the first without the second does not decide #54.

Verification commands (from the worktree root, global Python 3.12):

```
python -m pytest tests/test_tilt_geometry.py tests/test_tilt_pose_scan.py tests/test_tilt_frame.py tests/test_tilt_adjudicate.py tests/test_tilt_error_study.py -q
python -m pytest tests/test_reports_index.py tests/test_committed_data_files.py tests/test_docs.py tests/test_studyfmt.py tests/test_crop_sizing_v2.py tests/test_pov_replay.py -q
python -m pytest tests -q          # full suite; the 3 known Windows-only failures are pre-existing (memory: windows-test-env)
python -c "import ast,sys; [ast.parse(open(f).read(), feature_version=(3,9)) for f in ['reports/scripts/tilt_geometry.py','reports/scripts/tilt_pose_scan.py','reports/scripts/tilt_frame.py']]; print('py39 ok')"
python reports/scripts/tilt_error_study.py ... --write /tmp/re.json && python - <<'PY'   # re-run reproduces the committed artifact byte-for-byte except generated_at
PY
git diff --stat origin/master -- CropRunner.py DownloadRunner.py downloaders .coveragerc   # must be empty
```

Discrimination evidence to include in the PR (CLAUDE.md test-first rule): for each test class in §8,
one line naming the mutant it was run against and that it failed (e.g. "flip `atan2(−n_x, −n_y)` →
`test_artifact_axes_reproduce_the_documented_ray_formula` fails at 512/512 columns").

---

## 11. Appendix — planning-time probes (reproducible; do not commit as findings)

### 11.1 Store facts measured 2026-09-26

`seattle-wa`: 4,102 entries (4,096 shards + ledgers/logs); `depth_log.csv` 101,983 lines (49,794 saved,
52,188 unavailable); 49,794 `.depth.npz`; 77,142 `.xml`; 77,142 `.depth.txt`; 169,384 pano JPEGs.
`sao-paulo-brazil/depth_log.csv` 757 lines; `paterson-nj` 27,302; `columbus-oh` 21,809. Shard `_0` has
6 npz and 22 xml.

### 11.2 Remote inspection script (worked; call with `wsl-ssh.ps1 makelab2 script <local.sh>`)

The `.sh` cds into the city dir and runs an embedded `python3 - <<'PY' … PY` that loads each npz,
wraps pitch/roll to degrees, and prints facades (`|n_z|/‖n‖ < 0.5`, support > 300) and ground planes
(`≥ 0.7`) as `(idx, support, elev_deg, az_deg)`; `az` there was `atan2(n_y, n_x)` in artifact axes
(x = left, y = back) — convert with §2.1 before comparing to Δb. Full 12-pano output is in the planner's
transcript; the three rows in §0.2 are verbatim.

### 11.3 Prototype lean estimator (30 lines; the seed of `tilt_frame.py`)

Reduce ×4 → grayscale; band rows `h/2 − 35/180·h … h/2 + 25/180·h`; central-difference gradients;
`ang = atan2(gy, gx)` folded to (−90, 90]; select `mag > p95` and `|ang| < 12°`; 12 equal bearing bins
over the width; magnitude-weighted mean `ang` per bin. Prediction `pitch·sin Δb − roll·cos Δb` at bin
centres `Δb = ((b+0.5)/12)·360 − 180`. Output in §0.3.

### 11.4 Two panos fetched (22 MB) — re-fetch into `.cache/tilt/probe/` for the opt-in regression test

`seattle-wa/_1/_1KKwnoO0oZVxG9DeemA9A.jpg` (12,801,068 B; npz heading 274.5°, pitch 4.006°, roll
−0.523°; no xml) and `seattle-wa/_0/_03OWjj8xH_WkWLSZHZ0mg.jpg` (2019 scrape; xml `pano_yaw` 269.23,
`tilt_yaw` 110.91, `tilt_pitch` 4.73 → pitch 4.395°, roll 1.747° under the §0.1 hypothesis; no npz).
