# Moving `streetlevel` to 0.13.0, the release with the depth-header fix

Issue: [#237](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/237) (Part 2). It follows
[the Part 1 report](2026-11-15-streetlevel-no-deps.md), which is not edited. Measured 2026-11-17 on Windows 11
with uv's CPython 3.10.21 against the two PyPI wheels, both pure-Python `py3-none-any`. 3.10 is the production
minor version; the box and CI run Linux. Everything cited is in
[`data/2026-11-17-streetlevel-0.13.0/`](data/2026-11-17-streetlevel-0.13.0/):

- `planning_win_*`: the planning measurements;
- `review_*`: the plan review's own probe;
- `impl_*`: the implementation's test evidence, with `impl_evidence.sh` recording how it ran;
- `fix1_*`: the PR review's first fix round (see the last section).

The text files were committed as written, except for one scripted substitution of local path prefixes
(`impl_scrub.py`). The measurement folder became `<MEAS>`, any other scratch path `<SCRATCH>`, the temp folder
`<TEMP>`, uv's Python store `<UVPYTHON>` and the home folder `<HOME>`. No wheel is committed; their sha256
hashes are in `planning_win_p2_wheel_diff_0.12.11_0.13.0.out.txt`. **No live photometa request was made on
0.13.0.** The first real one will be on the box.

## The question

Through 0.12.13, `streetlevel` read the depth payload's one-byte `offset` (byte 7) as a uint16 spanning bytes 7-8.
Its parser therefore crashed on the ~1% of panos with a modelled zenith. Since #56 the repo has decoded the payload
itself, and it pinned the misread with a tripwire test built to fail when upstream fixed it. The fix,
[sk-zk/streetlevel#45](https://github.com/sk-zk/streetlevel/pull/45), shipped in 0.13.0 on 2026-11-12. This report
answers three questions: what else 0.13.0 changes on the paths this repo uses, whether Part 1's `--no-deps` split
still holds, and what the box needs to take the new release.

## What changed upstream

- **The fix is in 0.13.0.** #45 merged on 2026-11-04, and `v0.12.13...v0.13.0` is 16 commits that include it
  (`planning_win_p2_upstream_github.out.txt`). In the 0.13.0 wheel, `depth.parse_header` reads
  `"offset": depth_map[7]`. 0.12.11 and 0.12.13 read `get_uint16(depth_map, 7)`
  (`planning_win_p2_wheel_fulldiff_0.12.11_0.13.0.out.txt`, `review_r_wheels.out.txt`).
- **Nothing else the repo calls changed.** These are identical in the two wheels: `api.find_panorama_by_id`,
  `api.build_find_panorama_by_id_request_url`, `parse.parse_panorama_message`, `parse.parse_panorama_id_response`,
  `depth.parse`, `compute_depth_map`, `parse_planes` and `decode_b64`. `util.get_json` differs by one docstring
  line. The planning script (`planning_win_p2_byid_functions.out.txt`) and the review's own probe
  (`review_r_wheels.out.txt`) agree. The other changes upstream:
  - the radius search `find_panorama` moved to a POST endpoint (the repo never calls it);
  - a new `refetch` option and GPano crop tags;
  - a session now closed in a `finally`;
  - Naver and Yandex code, which the repo never loads.
- **The declared requirements are the same 16 in both wheels**, so Part 1's 9/7 split stands. The wheel's
  `Metadata-Version` went from 2.4 to 2.5, and pip 22.0.2 installed it without complaint (see below).

## The tripwire fired, and what replaced it

Master's tests ran unmodified over the 8 streetlevel-touching files, with `SIDEWALK_REQUIRE_STREETLEVEL=1`. The
result was 484 passed, 17 skipped on 0.12.11; 2 failed, 482 passed, 17 skipped on 0.13.0
(`planning_win_p2_ci_like_baseline_0.12.11.out.txt`, `planning_win_p2_ci_like_0.13.0.out.txt`). The two failures
are the ones designed to fail:

- `tests/test_streetlevel_api.py::test_streetlevel_still_misreads_the_depth_offset`
- `tests/test_suite_isolation.py::TestStreetlevelMustImportWhenRequired::test_with_the_variable_the_contract_tests_are_collected_and_run`

The second test runs the first one's module in a child process. The tripwire was replaced by one test,
`test_streetlevels_parser_agrees_with_ours_on_a_modelled_zenith`. It runs upstream's `depth.parse` on a payload
with indices `[1, 1, 0, 0]`, and the result must equal both the expected raster and the in-repo decode, mirrored.
The existing parity test `test_depth_helpers.py::TestComputeDepthRaster::test_matches_streetlevels_own_decode`
moved onto the same class: its first index is now 2. It had been held at 0 so that the old parser could read it.

| Run | Result | File |
|---|---|---|
| Both tests, `streetlevel` 0.12.11 | 2 failed (`IndexError: index 264` and `index 520`) | `impl_E_0.12.11.out.txt` |
| Both tests, `streetlevel` 0.12.13 | 2 failed, the same two errors | `impl_E_0.12.13.out.txt` |
| Both tests, `streetlevel` 0.13.0 | 2 passed | `impl_E_0.13.0.out.txt` |
| M1: our decode reads the offset as `<H` at byte 7, as 0.12.x did | 2 failed | `impl_M1.out.txt` |
| M2: `_compute_depth_raster` returns mirrored columns | 2 failed | `impl_M2.out.txt` |
| M5: one number this report quotes changed | the report's test fails | `impl_M5.out.txt` |

M1 and M2 were each restored with `git checkout --` before the next run. M5 was restored from a copy, because
the report was not yet committed. The planner's prototype of these tests, run on
the three releases, is `planning_win_proto_p2_tests.py`, with its output in `planning_win_p2_proto_tests.out.txt`.
The full suite on the branch's first commit is `impl_full_suite_win_py310.out.txt`. Its one failure, a lock test
for the Windows `msvcrt` lock, fails the same way on `origin/master` under the same interpreter
(`impl_lock_test_master_vs_branch.out.txt`).

## Why the in-repo decode stays

The two decodes agree on the zenith class (above), so routing back through upstream would change no artifact.
Nothing measured argues for dropping the bypass, and two findings argue for keeping it. First, `depth.parse`
still returns only the collapsed raster, so the planes the artifact stores (#56) need the in-repo decode anyway.
Second, upstream still raises a bare `IndexError` on a plane index past the list, where ours raises
`DepthPayloadError` (the `UPSTREAM RAISES` line in `planning_win_p2_proto_tests.out.txt`).

## The box: no rebuild of `.venv-237`

`.venv-237` was built on the box on 2026-11-15 with `streetlevel` 0.12.11 and has not been swapped in. The
rehearsal built a stand-in from master's files with the box's pip 22.0.2, then ran both install lines with the new
pins (`planning_win_p2_box_like_pip22.out.txt`):

- Line 1 was a no-op.
- Line 2 replaced `streetlevel` only: pip replaces an installed version that fails the specifier, even without `-U`.
- Both exited 0, with no resolver `ERROR:` block.

The freeze before and after differs in one line, `streetlevel==0.12.11` to `streetlevel==0.13.0`
(`planning_win_p2_box_like_freeze_before.txt`, `planning_win_p2_box_like_freeze_after.txt`). A rebuild is therefore
unnecessary. The box's own step-1 freeze diff in `docs/ops.md` re-checks this before the swap, while `.venv-237`
serves nothing.

pip 22.0.2 has no `--dry-run` (it arrived in 22.2). The 2026-11-15 issue comment ran `--dry-run` with the old
venv's newer pip, and that must not be repeated in `.venv-237`. The rehearsal ran on Windows only. pip's code
path does not depend on the OS, and the rehearsal was not repeated on Linux.

## The cap and the weekly job

`streetlevel` was the one requirement with an upper bound (`<0.13`, from #39). The weekly unpinned job,
`tests-latest.yml`, ran on 2026-11-16 as run 43980786100, four days after 0.13.0 was uploaded. It logged
`Collecting streetlevel<0.13,>=0.12.11` and installed 0.12.13, so the tripwire passed there and never fired on
its own (`impl_weekly_run_43980786100.out.txt`). The bump drops the cap. The requirement is now
`streetlevel>=0.13.0`, floor only like every other requirement, and `constraints.txt` pins the box and CI exactly.
Whether to keep a cap is left to the PR's reviewer.

## Wrong turns

- The freeze-versus-constraints `diff` in `planning_win_p2_box_like_pip22.out.txt` shows every line differing. That
  comes from Windows pip's CRLF line endings plus uv's seeded `packaging`, and is not a finding. The before/after
  freeze diff is the valid reading.
- The first function-identity script listed a `get_float32`, which exists in neither wheel. It shows as the
  `MISSING` line in `planning_win_p2_byid_functions.out.txt`.
- The first plan's mutation M1 widened the header struct to 9 bytes. That makes our decode refuse the header
  rather than misread it, so M1 was replaced: it now reads the offset as `<H` at byte 7 and leaves the struct
  alone.
- The planning call-path run ends with `r2 exit: 1` and
  `imports of an unreached requirement ATTEMPTED: ['cython']` (`planning_win_p2_ci_like_0.13.0.out.txt`). That
  attempt is scipy's own guarded `import cython` probe, which Part 1 traced
  (`review/r5_who_imports_cython.out.txt` in its data). Nothing unreached was loaded.
- The first scrub script wrote the local account name into itself and rewrote one of its own patterns. The
  committed `impl_scrub.py` reads the name at run time. Rerun on the raw outputs, it reproduces the files as staged,
  byte for byte, before git normalised their Windows line endings to LF on commit.

## Review round 1 (2026-11-21)

The PR review changed two numbers in this report and its test still passed: the release the weekly job installed,
and the `Metadata-Version` move. The test now re-derives both from the files, along with the date #45 merged, the
date 0.13.0 shipped, the weekly run's date and its gap to the upload, and it checks that every data file the report
cites is committed. `fix1_report_mutants.py` makes 11 edits and 3 deletions, one at a time, and each one fails the
test (`fix1_report_mutants.out.txt`). A wider sweep, `fix1_report_number_sweep.py`, bumps every number in the prose
outside backticks and links, one at a time: 78 of 112 still pass (`fix1_report_number_sweep.out.txt`, which lists
each one). Most survivors are names and history: issue and PR numbers, the Part and mutant labels, release numbers
in running text, the wire format's byte offsets, and dates from the box or from Part 1. A few are recorded in this
folder but not re-derived by the test, such as the interpreter and pip versions.
