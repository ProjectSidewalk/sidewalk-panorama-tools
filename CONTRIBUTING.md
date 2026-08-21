# Contributing

Thanks for looking. This is research tooling that runs unattended every night across ~50 cities, so the bar
is less "does it work on my machine" and more "will the next person be able to tell what it did at 3 a.m.
three months from now."

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate                                  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests
```

Ubuntu 22.04 / Python 3.10 is the baseline CI enforces; the suite also runs on a Windows dev box, where a
handful of tests that assert POSIX file modes skip themselves. See [docs/testing.md](docs/testing.md) for what
the suite covers and which parts touch the network.

## What CI runs

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) installs `requirements.txt` +
`requirements-dev.txt` on Ubuntu 22.04 / Python 3.10 and runs `pytest tests -v` on every push to `master`,
every pull request, and on demand. There is no linter configured — match the surrounding style.

## Conventions worth knowing before you write code

**Entry points have a fixed shape.** `DownloadRunner.py` and `CropRunner.py` are both
`build_parser()` / `configure_logging()` / `run(...)` / `main(argv=None)` behind an
`if __name__ == '__main__'` guard, so **importing either has no side effects** and tests can drive the real
flow in-process. Keep new entry points that shape.

**Tests come first, and green isn't evidence.** A test that passes against the broken version proves nothing.
When you fix a bug, show the test failing on the old behaviour; when you add a check, make sure a plausible
mutation of the code under it actually turns the suite red. Three separate mutation sweeps in this repo have
turned up assertions that survived their own subject being reverted.

**Commit the data, not just the assertion.** Raw measurements, captured fixtures, and the manifests that say
where they came from belong in the repo beside the test — see `tests/fixtures/tiles/` for the pattern. A
finding that lives only in a paragraph stops being checkable the moment someone doubts it.

**Findings get a write-up.** Anything data-driven — a threshold calibrated against real data, a conclusion
about how an external endpoint behaves — goes in `reports/YYYY-MM-DD-topic.md`, wrong turns included, and gets
a row in [`reports/README.md`](reports/README.md). That file explains what a report should carry; a test
enforces that the index and the directory agree.

**Every number in a report's prose is transcribed from a committed artifact**, and a test asserts the
transcription. A report table is the one place in this repo where a plausible-looking wrong number has no
compiler and no test to catch it — and that has bitten us, by 2× and by 6×, in adjacent sentences that looked
completely normal. State which filter a count is under, or don't quote it.

**Artifacts live in GitHub or the `projectsidewalk` Hugging Face org — nothing else.** Not Drive, not Dropbox,
not an ad-hoc shared link. Those feel accessible in the moment and don't survive people moving on; experiments
here have had to be re-run because of it. Data too large to commit goes to a HF dataset referenced by exact
revision, with the manifest and the regenerating script committed here. The bar: a fresh clone plus the
referenced dataset reproduces every number in `reports/`.

**Desk studies under `reports/scripts/` have their own six conventions** — undefined is not zero, `pano_id` is
pinned to `str`, Mapillary cities cache separately, committed-artifact tests don't test code, prose numbers are
transcribed, and two figures in one artifact must each name the population they were computed on. They are
written out in [`CLAUDE.md`](CLAUDE.md#desk-studies-under-reportsscripts--six-conventions-that-keep-being-rediscovered),
each one because it broke something.

**`label_id` is unique per city, not globally.** One database schema per city, each with its own serial. Key
on `(city, label_id)`, pass `validate=` to every pandas merge involving labels, and write `seattle:12345`
rather than `12345` in issues and filenames. `pano_id` is the imagery source's, and is the safe cross-city key.

## Documentation

The README is a front door: what this is, how to run it, and where to go next. Anything longer than a
paragraph belongs in [`docs/`](docs/), linked from the README's documentation map.

Code comments that point at prose should name the file — `docs/ops.md`, not "the README" — and
`tests/test_docs.py` checks that every relative link in the docs, and every `docs/*.md` path mentioned in the
Python sources, actually resolves. If you move a page, that test tells you what you broke.

`CLAUDE.md` is the same information pitched at coding agents. When behaviour changes, both it and the
relevant `docs/` page need the edit.

## Good first improvements

* **Multi-core cropping.** `CropRunner.py` runs on a single core. Most machines have more, and cropping tens
  of thousands of objects is embarrassingly parallel per pano.
* **A better distance estimator for `predict_crop_size`** ([#32](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/32)).
  Sizing rule v2 made the *window* resolution-independent by normalising into the frame the 2013 constants
  were fit on and clamping as an angle ([the v2 report](reports/2026-08-19-crop-sizing-v2.md)), but the
  estimator underneath is still pano-y → distance → size, and a y-only rule cannot know how large the
  referent actually is. That residual is the open part.

Browse the [open issues](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues) for the rest.
