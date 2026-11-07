---
paths:
  - "sidewalk_panorama_tools/*.py"
  - "tests/test_store.py"
  - "tests/test_ps_math.py"
  - "tests/test_labels.py"
---
# The installable package: `sidewalk_panorama_tools` (store, ps_math, labels)

Loaded by Claude Code when a file matching the globs above is read. The repo-wide guidance is `CLAUDE.md` at the repo root; read it first. `docs/geometry.md` is the consumer-facing page. The geometry modules (`geometry.py`, `pose.py`, `torch_geometry.py`) and the #54 frames are `.claude/rules/tilt.md`; the store walker's history and the display-copy rules are `.claude/rules/store-repair.md`.

**What the package is for.** Consumers outside this repo (sidewalk-ai-api, sidewalk-validator-ai, RampNet, the auto-labeler scripts) pin it by tag instead of copying code. Every module here is the one definition; anything in the repo that needs the same thing imports it back, never the reverse.

- **Re-exports, never second copies.** `downloaders/common.py` re-exports `store`'s walker, sidecar predicate, `jpeg_dimensions`, `MAX_PANO_PIXELS` and `raise_decompression_bomb_ceiling`; `reports/scripts/pov_replay.py` re-exports all of `ps_math`. Tests assert identity (`common.X is store.X`, `pov_replay.X is ps_math.X`), so a "local fix" in the re-exporting module is a second definition and fails. `common.downscaled_sidecar_path` is the one wrapper, because the cap is a common.py decision that tests move.
- **Releases.** Bump `version` in `pyproject.toml` and `__version__` together (a test checks), record it in `docs/geometry.md` "Releases", and say what a re-pinning consumer will see change. 1.1.0 added these three modules and `pillow`.
- **Coverage.** Every module here is measured (`tests/test_coverage_config.py`'s exact set) and listed in `tests/test_csv_intake.py`'s `PRODUCTION_MODULES` (no pandas in production). A new module is a deliberate measure-or-omit decision.

**store.py** (#222 step 5). `read_pano` checks the header with `jpeg_dimensions` (stdlib) before Pillow is touched, reads the expected dims as `CropRunner._metadata_dims` does (`int(float(x))`), and raises `PanoDimsMismatch` only for a real mismatch; a bad argument is a plain `ValueError`. **No `warnings.catch_warnings` and no write to `Image.MAX_IMAGE_PIXELS`**: both are process-wide, and the first is not thread-safe in a server; a consumer reading 134 MP panos calls `raise_decompression_bomb_ceiling()` once. CropRunner makes the same comparison but does not call `read_pano` (one header for all of a pano's labels, lazy decode, #164).

**ps_math.py** (#222 step 6). Moved verbatim from `pov_replay.py`; `pov_from_pano_xy` is the new, unrounded inverse. `tests/test_ps_math.py` pins it against a line-by-line transcription of `UtilitiesPanomarker.js` at SidewalkWebpage v7.19.10; change the transcription only against a newer tag, and cite it. `viewport_view` stays in RampNet. CropRunner's three distance constants are still a pinned copy (`.claude/rules/cropper.md`).

**labels.py** (#222 step 4). The arithmetic, not the decision: `majority`'s `tie` and `is_validated`'s two thresholds are required keywords, so no caller inherits a policy. **The AI validator is identified by `validator_type == "AI"` and nothing else** - never a username or `user_id` in code, tests or fixtures (the fixture drops every `user_id`). An unknown `validation` or `validator_type` raises, even with AI excluded. Unsure abstains in both `majority` and `is_validated`; the threshold compares the quotient `agree / decisive`, never `fraction * decisive` (the product failed 7 of 25 at 0.28). `label_uid` refuses ids below 1 (fill values). Measured on the live API: the served `*_count` fields include AI votes, and `correct` is `majority(..., include_ai=True, tie="none")`; tests pin both on the fixture.
