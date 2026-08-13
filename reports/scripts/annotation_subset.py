"""Cut a smaller annotation task set out of an already-rendered one (prereg §4, Amendment 3).

Two jobs, one mechanism:

* **Apply the current referent rule to an existing tile directory.** `annotation_tiles.py` renders the
  whole drawn corpus; Study 1 reads only the measurable subset, and that subset moved on 2026-08-13.
  Re-cutting tiles to change a filter would be wasteful and would also re-jitter every tile, so this
  filters the task list and copies the tiles that survive.
* **Draw an annotator's stratified subset.** §4 gives Jon an independent `n = 50` through the same
  tooling, as a rubric-agreement instrument. His set must be a SUBSET of what Claude annotates or the
  agreement gate has no overlap to compute on, which is why this draws from a rendered task list rather
  than from the corpus.

The filter is recomputed from `rawlabels.has_located_referent`, never read from the corpus CSV's
`measurable` column. That column is a snapshot of the rule as it stood when the corpus was drawn, and
reading it here is the failure this script exists to prevent: after Amendment 3 the stale column says
584 labels and the live rule says 368, and the 216-label difference is silent — every one of them a
valid row with a plausible tile that simply has no point to be displaced from.

Blindness is preserved by construction: `geometry.json` is never read and never copied, so a subset
directory cannot leak an answer key even if it is later served by something less careful than
`annotate_server.py`.

Usage:
    # the corrected full set for the blind annotator
    python annotation_subset.py .cache/annotation/gsv --corpus reports/data/2026-08-12-crop-corpus-gsv.csv.gz \\
        --out-dir .cache/annotation/gsv-measurable --measurable-only

    # Jon's stratified 50, drawn from that
    python annotation_subset.py .cache/annotation/gsv-measurable --corpus ... \\
        --out-dir .cache/annotation/gsv-jon50 --n 50 --seed 20260813
"""

import argparse
import collections
import json
import os
import random
import shutil
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import annotation_tiles  # noqa: E402
import rawlabels  # noqa: E402

TASKS_FILE = 'tasks.json'

# Never read, never copied. `annotate_server` also refuses it by name; this is the other half of that
# guard, at the point where a directory is created rather than where one is served.
FORBIDDEN = frozenset({'geometry.json'})

# The strata for an annotator subset: §3's two corpus axes. Type because the rubric differs per type and
# an agreement gate that never saw a SurfaceProblem says nothing about the SurfaceProblem rubric; band
# because depression is the covariate Study 1 is estimating against, so a subset concentrated in one band
# would agree well and generalise nowhere.
STRATA = ('label_type', 'band')


def measurable_mask(corpus):
    """What Study 1 can read: the live referent rule AND the study frame.

    Applied to `label_type`, `tags` and `city` directly rather than trusting `corpus['measurable']`, for
    the reason in the module docstring — that column is a snapshot of the rules as they stood when the
    corpus was drawn, and both have moved since.

    The frame arm is second and separate because it is a different KIND of exclusion. The referent rule
    says a label has no point to be displaced from; the frame rule says the label is fine and the
    annotator is not equipped to judge it. Collapsing them into one mask would be convenient and would
    lose that, and the two get revisited for entirely different reasons.
    """
    return rawlabels.has_located_referent(corpus) & rawlabels.in_study_frame(corpus)


def allocate(counts, n):
    """Largest-remainder allocation of `n` across strata holding `counts` labels each.

    Proportional, then floor, then hand out what is left by largest fractional part. Ties break on the
    stratum key so two runs of the same input allocate identically — the draw is seeded, but allocation
    runs before the seed is used and would otherwise be a second, unseeded source of variation.

    A stratum never gets more than it holds; the surplus flows to the others, so a small cell cannot
    silently shrink the total below `n`.
    """
    total = sum(counts.values())
    if n >= total:
        return dict(counts)
    if total == 0:
        return {}

    exact = {k: n * v / total for k, v in counts.items()}
    alloc = {k: min(int(v), counts[k]) for k, v in exact.items()}

    # Hand out the remainder one at a time: capacity can bind at any point, so this cannot be a single
    # sort over fractional parts — a cell that is already full has to be skipped and its share passed on.
    while sum(alloc.values()) < n:
        candidates = [k for k in counts if alloc[k] < counts[k]]
        if not candidates:
            break
        k = max(candidates, key=lambda k: (exact[k] - alloc[k], k))
        alloc[k] += 1
    return alloc


def draw(tasks, corpus, n, seed):
    """Choose `n` label_uids from `tasks`, stratified over STRATA, deterministically under `seed`.

    Returns the chosen uids in the task file's original order rather than in draw order: the order tiles
    are presented in is itself an experimental variable (fatigue, and learning the rubric), and a queue
    ordered by stratum would walk an annotator through all the curb ramps and then all the obstacles.
    """
    uids = [t['label_uid'] for t in tasks['tasks']]
    frame = corpus.set_index('label_uid').loc[uids]
    cells = collections.defaultdict(list)
    for uid, row in zip(uids, frame.itertuples()):
        cells[tuple(getattr(row, s) for s in STRATA)].append(uid)

    counts = {k: len(v) for k, v in cells.items()}
    alloc = allocate(counts, n)

    rng = random.Random(seed)
    chosen = set()
    for key in sorted(cells):
        pool = sorted(cells[key])          # sorted first: dict order must not reach the RNG
        chosen.update(rng.sample(pool, alloc.get(key, 0)))
    return [u for u in uids if u in chosen]


def subset_tasks(tasks, keep):
    """`tasks` narrowed to `keep`, with the derived count corrected, and the flag list refreshed from
    `annotation_tiles.FLAGS`.

    The flag list is a property of the *instrument*, not of which labels it is pointed at — which is
    exactly why it is re-read from code here instead of copied from the input file. A rendered
    `tasks.json` is a snapshot of the protocol as it stood when the tiles were cut, and Amendment 3
    added `no-extent` after these tiles existed. Copying the block through verbatim served a 50-label
    queue whose only escape hatch for an unboundable referent was the `ambiguous` flag it is
    deliberately distinct from, and nothing failed: the queue was correct, the tiles were correct, and
    the annotator simply had no key to press.

    Re-rendering to pick the flag up would have been the alternative, and it is the wrong one — it
    re-cuts 358 tiles to change a four-string list, and every tile it produces has to be re-verified.

    The refresh itself lives in `write_subset` so it applies on every path: a run with neither
    `--measurable-only` nor `--n` never calls this function at all, and would otherwise be the one way
    to produce a directory carrying a stale protocol.
    """
    out = dict(tasks)
    out['tasks'] = [t for t in tasks['tasks'] if t['label_uid'] in set(keep)]
    out['n_tasks'] = len(out['tasks'])
    return out


def backfill_tags(tasks, corpus):
    """Add each task's `tags` from the corpus, for tile sets rendered before tasks carried them.

    Same shape as the flag refresh and for the same reason — it costs nothing but a re-read of the
    corpus, where re-rendering 358 tiles to add a metadata field would re-cut every one of them and
    require re-verifying the geometry. `display_tags` is shared with `build_tasks` rather than
    reimplemented here, so a freshly rendered set and a backfilled one cannot disagree about what a
    label's tags are.

    Tags are annotator-safe: they say what the label is about, never where it is. The reasoning is in
    `annotation_tiles.display_tags`.
    """
    by_uid = dict(zip(corpus['label_uid'], corpus.get('tags', pd.Series(dtype=object))))
    out = dict(tasks)
    out['tasks'] = [dict(t, tags=annotation_tiles.display_tags(by_uid.get(t['label_uid'])))
                    for t in tasks['tasks']]
    return out


def write_subset(tasks, src_dir, out_dir):
    """Write `tasks.json` and copy exactly the tiles it names into `out_dir`.

    Copies rather than links so the directory is self-contained and can be handed to an annotator whole,
    and so deleting it can never reach back into the rendered set.
    """
    os.makedirs(out_dir, exist_ok=True)
    for task in tasks['tasks']:
        shutil.copyfile(os.path.join(src_dir, task['tile']), os.path.join(out_dir, task['tile']))
    # Both refreshed from code, for the same reason: they are properties of the INSTRUMENT, not of the
    # pixels, and a rendered tasks.json is a snapshot of the protocol as it stood when the tiles were
    # cut. `cut_fov_deg` is deliberately NOT in this list — that one describes the pixels, so it has to
    # come from whatever actually produced them.
    tasks = dict(tasks,
                 flags=list(annotation_tiles.FLAGS),
                 initial_view_fraction=annotation_tiles.VIEW_FOV_DEG / annotation_tiles.CUT_FOV_DEG)
    path = os.path.join(out_dir, TASKS_FILE)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(tasks, f, indent=1, allow_nan=False)
    assert not (set(os.listdir(out_dir)) & FORBIDDEN), 'subset directory holds an answer key'
    return path


def compose(tasks, corpus):
    """Counts by stratum, for the run summary. Reported by both axes separately rather than as the full
    cross-tab: the cross-tab is what the draw balances, but what a reader checks is that no type and no
    band vanished."""
    frame = corpus.set_index('label_uid').loc[[t['label_uid'] for t in tasks['tasks']]]
    return {s: frame[s].value_counts().sort_index().to_dict() for s in STRATA}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('tasks_dir', help='a rendered tile directory holding tasks.json')
    ap.add_argument('--corpus', required=True, help='the drawn corpus CSV from corpus_sample.py')
    ap.add_argument('--out-dir', required=True, help='where the subset tasks.json and tiles go')
    ap.add_argument('--measurable-only', action='store_true',
                    help='apply the current referent rule (rawlabels.has_located_referent), '
                         'recomputed rather than read from the corpus CSV')
    ap.add_argument('--n', type=int, help='stratified subsample size; omit to keep everything')
    ap.add_argument('--seed', type=int, help='draw seed; required with --n')
    args = ap.parse_args(argv)

    if args.n is not None and args.seed is None:
        ap.error('--n requires --seed: an unseeded draw cannot be re-derived from the artifact')

    with open(os.path.join(args.tasks_dir, TASKS_FILE), encoding='utf-8') as f:
        tasks = json.load(f)
    corpus = pd.read_csv(args.corpus, dtype={'pano_id': str})

    known = set(corpus['label_uid'])
    missing = [t['label_uid'] for t in tasks['tasks'] if t['label_uid'] not in known]
    if missing:
        ap.error(f'{len(missing)} tasks are not in this corpus (e.g. {missing[0]}); wrong --corpus?')

    tasks = backfill_tags(tasks, corpus)

    start = len(tasks['tasks'])
    if args.measurable_only:
        keep = set(corpus.loc[measurable_mask(corpus), 'label_uid'])
        tasks = subset_tasks(tasks, [u for u in keep])
        print(f'referent rule: {start} -> {len(tasks["tasks"])} tasks')

    if args.n is not None:
        tasks = subset_tasks(tasks, draw(tasks, corpus, args.n, args.seed))
        print(f'stratified draw (seed {args.seed}): {len(tasks["tasks"])} tasks')

    write_subset(tasks, args.tasks_dir, args.out_dir)
    for axis, counts in compose(tasks, corpus).items():
        print(f'  {axis}: ' + ', '.join(f'{k} {v}' for k, v in counts.items()))
    print(f'wrote {len(tasks["tasks"])} tasks + tiles to {args.out_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
