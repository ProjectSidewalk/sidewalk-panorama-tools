---
paths:
  - "reports/**"
  - "tests/test_annotat*.py"
  - "tests/test_*_census.py"
  - "tests/test_*_study.py"
  - "tests/test_*_report.py"
  - "tests/test_click_noise_*.py"
  - "tests/test_corpus_*.py"
  - "tests/test_crop_sizing_v*.py"
  - "tests/test_off_target_markers_*.py"
  - "tests/test_refetch_pilot*.py"
  - "tests/test_rerender_probe*.py"
  - "tests/test_tilt_*.py"
  - "tests/test_committed_data_files.py"
  - "tests/test_reports_index.py"
  - "tests/test_studyfmt.py"
  - "tests/test_fetch_rawlabels.py"
  - "tests/test_rawlabels_mapillary.py"
  - "tests/test_margin_convention.py"
  - "tests/test_offaxis_covariate.py"
  - "tests/test_store_coverage.py"
  - "tests/test_pov_replay.py"
  - "tests/test_refetch_panos.py"
  - "tests/test_pano_pose.py"
---
# Desk studies: `reports/`, its scripts, its tests and its reports

Loaded by Claude Code when a file matching the globs above is read: anything under `reports/` (a study script, a report, a plan, a committed artifact) and every test whose module scope reaches into `reports/` (`tests/test_docs.py` finds those by their code and fails if one is not matched here). The repo-wide guidance is `CLAUDE.md` at the repo root; read it first, in particular the standing rule on where artifacts live ("Artifact storage"), which applies to everything written from here. `reports/README.md` is the index of the reports and says what a report should carry. The `tilt_*` scripts and the tilt study are also covered by `.claude/rules/tilt.md`.

These are guidance, not history: read them before touching a study script, a study test or a report's numbers.

Two rules beyond the conventions below: `reports/scripts/crop_rule_v1.py` is the one frozen copy of the old sizing rule and must not be "fixed"; and production code never imports from this directory (the coverage set is the production tree), so a definition both sides need lives in production and is imported back from here - `annotation_tiles.py` and `crop_sizing_v*.py` import `CropRunner` that way.

## Desk-study conventions — six that keep being rediscovered

Moved from CLAUDE.md on 2026-09-29 (by way of `reports/README.md`, #192). These bit four scripts at once in the 2026-08-11 review; see `reports/2026-08-11-mapillary-census.md`.

- **Undefined is not zero, and `main()` must not format-spec it.** Analysis functions correctly return
  `None` for a percentage with a zero denominator, a sample sd of one value, a correlation against a
  constant series. `f"{None:.1f}"` then raises `TypeError` at the summary print — after all the compute
  and before `--write`. Print through `studyfmt.fmt` and build artifact values with `studyfmt.num`;
  there is one definition of each and no script may grow a local copy (a test asserts that). Writes use
  `allow_nan=False`, so a NaN reaching the dict aborts the run on its last line.
- **`rawlabels` pins `pano_id` to `str`, and must.** Mapillary image ids are all-numeric, so pandas
  infers `int64` for any Mapillary city — the #46 bug class the two runners already pin against. A
  merge between an int-keyed and a str-keyed frame matches nothing and reports zero coverage rather
  than failing.
- **Mapillary cities live in a separate cache directory.** Every study globs `*.csv` over a directory,
  so a Mapillary city dropped into `.cache/rawlabels/` silently joins the six-city GSV corpus and moves
  every committed artifact. `fetch_rawlabels.py` writes them to `.cache/rawlabels-mapillary/`.
- **Committed-artifact tests do not test code.** Pinning a finding against `reports/data/*.json` proves
  nothing about the function that produced it: the artifact was generated *by* the current code, so a
  revert stays green. Every finding needs a synthetic code-level test beside its corpus pin. Three
  mutation batteries in a row surfaced survivors of exactly this shape.
- **Every number in a report's prose is transcribed from a committed artifact, and a test says so.**
  Two counts hand-typed into `reports/2026-08-11-mapillary-census.md` §6 were wrong by 2× and 6×, and
  nothing about the surrounding sentences looked different for it — a report table is the one place in
  this repo where a plausible number has no compiler and no test. So the script computes the whole
  table, and `TestReportMatchesTheArtifact` asserts each value appears in the markdown. The same round
  found a filtered count (81,667 eligible) quoted as a raw one (82,769): **state which filter a count is
  under, or don't quote it.** Corollary for exclusion rules: **size the rule against the corpus it will
  be applied to, not only the one it was derived from.** `NoSidewalk` was left as an open question after
  a rule was derived on 267 Richmond labels that contain none of it; it is 82,769 labels and the largest
  arm in the six-city corpus the rule actually governs.
- **Two figures in one artifact must each name the frame they were computed on.**
  `click_noise_study.study()` put a matched sigma (referent-filtered, 335,712 labels) and every clustered
  sigma (all 436,348) in one dict and printed them in one column, and nothing recorded that they were
  100,636 labels apart — so a docstring, a test docstring and a report all quoted one against the other
  as a single comparison. The fix is not prose: `populations` names each side, lists the keys it covers,
  and a test asserts every emitted figure is claimed by exactly one side, so the *next* figure added
  cannot land unclaimed. Where the comparison is the point, compute the like-for-like cell too
  (`comparable_only`) rather than leaving a reader to assume the difference is the estimator — here it
  mostly was, but that was a measurement, not a given.

## The gold-standard annotation tool (`corpus_sample.py` → `annotation_tiles.py` → `annotation_subset.py` → `annotate_server.py`)

Four scripts feed each other, and the seams between them are load-bearing:

- **`corpus_sample.py`** draws the study corpus from a rawLabels frame. **Label identity is
  `(city, label_id)`, carried as `label_uid`** — `label_id` restarts at 1 in every deployment, and keying
  on it silently cost 314 labels of a 763-label draw. `pano_id` does *not* collide across cities, which is
  why the per-pano cap and the pano-wise tune/eval split are sound on it.
- **`annotation_tiles.py`** cuts one tile per label and emits **two** files. `tasks.json` is
  annotator-facing and carries no stored coordinate, no jitter, no tile origin and **no seed** — each of
  those recovers the answer, since the tile origin is `stored + jitter - size/2`. `geometry.json` carries
  all of them and is what the analysis uses to map a tile-space annotation back to pano coordinates.
  Tiles are cut at **60°** and the view opens at the whole cut: a tile of angular width F can only
  measure a displacement up to F/2, so a tight tile converts gross errors into `object-absent` and
  deletes the largest errors from the distribution being estimated.
- **Everything angular on that instrument must be angular, including the jitter.** It was `±40–80 px`
  until 2026-08-19, which is 1.5–2.9% of the tile on the 8192-height panos 641 of the 763 drawn labels
  sit on and 7.2–14.4% on the 1664s — so the one device whose job is to keep the stored point off the
  tile centre varied ~5× with resolution and was weakest on 84% of the corpus. It is now
  `JITTER_MIN_FRAC`/`JITTER_MAX_FRAC` of the tile (§4's numbers at the 20° cut §4 was written for),
  which also means changing `CUT_FOV_DEG` can no longer dilute it.
- **`measurable` has exactly one definition, `rawlabels.study_measurable`,** and **no script may read
  the corpus CSV's `measurable` column** — that is a snapshot of the rule at draw time and says 584
  where the live rule says 368. `annotation_subset.py --measurable-only` and
  `annotation_tiles.py --measurable-only` both call it; the latter used to read the column, which is
  the failure the former was written to prevent, one script upstream.
- **Protocol fields come from code, pixel fields come from the rendered file.** `FLAGS`, `FLAG_HELP`,
  `BOX_RULE` and `initial_view_fraction` are properties of the instrument, so
  `annotate_server.tasks_payload` sends them from `annotation_tiles` on every request and
  `annotation_subset.write_subset` refreshes them in the copies it writes. `cut_fov_deg` is the one
  exception: it describes the pixels, so it comes from whatever produced them. Taking the flag *list*
  from the file while taking its *help text* from code is the specific half-measure that shipped a
  queue offering three flags to a server that accepted four — the annotator had no key to press.
- **`annotation_subset.py`** narrows an already-rendered tile set, and is where the *current* referent
  rule is applied. Both its filters fail silently: a queue drawn from the wrong population or missing a
  flag looks perfectly well-formed.
- **`annotate_server.py`** serves tiles to `annotate.html` on loopback and writes one JSON per label per
  annotator. It refuses `geometry.json` **by name** — it sits beside the file that is served, and the
  natural static-file handler would publish the answer key at a guessable URL.

Amendment 1(e) forbids porting the webpage's render path into any of this: Study 1 compares stored
`pano_x`/`pano_y` against gold *in pano coordinates*, so a mapping sharing the projection under test would
make the study measure zero by construction. The tile transform is verified by round-trip against
directly-indexed pixels, never against another implementation.

**The corpus is 8 types; Study 1's measurable set is 4.** The referent rule (2026-08-13) excludes
Occlusion, Crosswalk, NoSidewalk, **Signal** and **Other** by type, plus eleven `(label_type, tag)`
pairs — leaving CurbRamp, NoCurbRamp, Obstacle and SurfaceProblem, 368 of the 763-label corpus. It is a
**placement-measurability** rule, not a corpus rule: the excluded types have real crop consumers and
Study 2 still sizes crops for them. What changed on 2026-08-13 is that they are no longer *annotated* —
if a referent has no located centre it has no tight extent either, so a gold box on one is as arbitrary
as a gold point. The rule is keyed on **pairs, not tags**: `height difference` is excluded under
SurfaceProblem (a run of pavement) and kept under Obstacle (a discrete step). Tags are optional, so the
rule is leaky by construction — 14% of Obstacle labels carry none — which is what the `no-extent` flag
is for, and that flag is **reported as its own bucket, never dropped from a denominator**.

The prereg's §7 is a **decision log**, not an amendment log — plain dated entries recording what changed
and *what was known at the time*, since the ordering (a filter fixed before any gold existed) is the only
part that cannot be reconstructed later. Old references resolve as Amendment 1/2/3 = 2026-08-11/12/13.
Note that changing the referent rule invalidates published artifacts computed under the old one: the
Mapillary census is deliberately **not** regenerated, and `TestTheCommittedRuleIsCurrentOrSuperseded`
fails if the live rule diverges from a committed artifact's recorded rule without the report saying so.
