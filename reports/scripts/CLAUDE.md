# `reports/scripts/` - desk studies and the annotation tool

Loaded by Claude Code when a file under this directory is read. The repo-wide guidance is `CLAUDE.md` at the repo root; the standing rule on where artifacts live ("Artifact storage") applies to everything written from here. The `tilt_*` scripts are also covered by `.claude/rules/tilt.md`.

The six conventions that keep being rediscovered in `reports/scripts/` (undefined is not zero and `main()`
must not format-spec it; `rawlabels` pins `pano_id` to `str`; Mapillary cities cache in a separate
directory; committed-artifact tests do not test code; every number in a report's prose is transcribed from a
committed artifact, and a test says so; two figures in one artifact must each name the frame they were
computed on) and the load-bearing seams of the gold-standard annotation tool (`corpus_sample.py` →
`annotation_tiles.py` → `annotation_subset.py` → `annotate_server.py`: `(city, label_id)` identity,
`tasks.json` against `geometry.json`, angular jitter, `rawlabels.study_measurable` as the one referent rule,
protocol fields from code and pixel fields from the rendered file, the 8-type corpus against the 4-type
measurable set, and Amendment 1(e)'s ban on porting the webpage's render path) are written up in
`reports/README.md` under "Desk-study conventions" and "The gold-standard annotation tool". They are
guidance, not history: read both before touching a study script.

Two more that bind here: `reports/scripts/crop_rule_v1.py` is the one frozen copy of the old sizing rule and must not be "fixed"; and production code never imports from this directory (the coverage set is the production tree), so a definition both sides need lives in production and is imported back from here - `annotation_tiles.py` and `crop_sizing_v*.py` import `CropRunner` that way.
