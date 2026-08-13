"""Gold-standard annotation tiles: cut the viewport an annotator sees, and keep the answer out of it.

Phase 2 of the crop-priors work package (reports/2026-08-09-crop-priors-prereg.md §4). Each drawn
corpus label becomes one tile plus one task record; an annotator marks the object's canonical point and
a tight bounding box **in tile coordinates**, and the analysis converts that to pano coordinates using
geometry the annotator never held.

Three design constraints, each because the obvious alternative corrupts the gold standard:

* **The transform is this module's own, and it is verified against the raster rather than against
  another implementation.** Amendment 1(e) forbids porting the webpage's render path: Study 1 compares
  stored `pano_x`/`pano_y` against gold *in pano coordinates*, so a tile→pano mapping that shared the
  projection under test would make the study measure zero by construction. Cutting an axis-aligned
  window from an equirectangular raster involves no projection at all, which is what makes an
  independent implementation cheap here — the only hard parts are the seam and the mapping, and both are
  pinned by round-trip tests against directly-indexed pixels.

* **The tile is a fixed ANGULAR window, not a fixed pixel window.** The corpus spans 8192- and
  6656-height panos, and a fixed pixel window would show an annotator half as much world on the
  low-resolution ones. Resolution-dependence is the defect #32 exists to remove; importing it into the
  gold standard would put it beyond reach of measurement.

* **The annotator package cannot reconstruct the stored point.** §4 says it is never rendered; this goes
  further and never ships it. The task file carries no stored coordinate, no jitter, no tile origin and
  no seed — because each of those recovers the answer (the tile origin is `stored + jitter - size/2`),
  and a UI that merely declines to draw a value it was handed is one edit away from anchoring every
  annotation, with nothing in the output to show it happened.

Usage (offline; needs the corpus panos cached locally):
    python annotation_tiles.py ../data/2026-08-12-crop-corpus-gsv.csv.gz \\
        --pano-root .cache/panos --out-dir .cache/annotation/gsv --seed 20260812
"""

import argparse
import collections
import gzip
import hashlib
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import CropRunner  # noqa: E402
import store_coverage  # noqa: E402
from studyfmt import fmt  # noqa: E402

# Two angular extents, because one number cannot serve both jobs.
#
# CUT is what lands in the JPEG; VIEW is where the annotator's window starts, and they can zoom out to
# the full cut. The reason for the gap is a bound nobody would notice until the annotations were in: a
# tile of angular width F can only ever *measure* a displacement up to F/2, because past that the object
# is off the tile and the only honest response left is the `object-absent` flag. A 20 deg tile therefore
# converts every gross placement error into "absent" and quietly removes the largest errors from the
# distribution Study 1 is estimating -- the one direction of bias the study cannot detect in itself.
#
# Measured motivation, not hypothetical: 45 of the 72 Signal labels in the drawn corpus sit 10-42 deg
# below the horizon, on the pole base rather than the signal head their rubric names, so their canonical
# point is tens of degrees from the stored point. At CUT = 60 deg an annotator can see that and place it;
# at 20 deg they can only report it missing.
#
# VIEW stays at 20 deg because that is the framing question: wide enough not to imply a crop size (Study
# 2 is choosing between sizing rules, and an annotator shown a tight window draws boxes calibrated to
# it), narrow enough that the object is resolvable without panning.
CUT_FOV_DEG = 60.0
VIEW_FOV_DEG = 20.0

# Kept as the name the geometry is denominated in, so a reader of geometry.json is not left guessing
# which of the two a `width` refers to: it is always the cut.
TILE_FOV_DEG = CUT_FOV_DEG

# §4's jitter, read as MAGNITUDE uniform in [40, 80] with a random sign per axis -- not uniform in
# [-80, +80]. The difference is the whole point of the jitter: a symmetric-about-zero draw puts the
# stored point at or near the tile centre for a real share of labels, and "the object is in the middle"
# is precisely the anchor §4 is written to remove.
JITTER_MIN_PX = 40
JITTER_MAX_PX = 80

# §4's flags. An annotator picks one of these instead of guessing, which is what keeps edge cases out of
# the placement distribution rather than in it as noise. The page renders them from this tuple and binds
# them to keys 1..N in order, so adding one here is the whole change.
#
# 'no-extent' is Amendment 3's, and it exists because the tag rule it accompanies is leaky by
# construction: tags are optional in Project Sidewalk, and 14% of Obstacle and 10% of SurfaceProblem
# labels carry none at all, so an untagged label whose referent has no well-defined extent reaches the
# annotator with nothing to catch it. The pre-draw tag rule handles the labels it can see; this handles
# the rest.
#
# It is deliberately separate from 'ambiguous' rather than folded into it. They fail differently and the
# analysis has to tell them apart: 'ambiguous' means "I cannot tell WHICH thing this label is about",
# 'no-extent' means "I know exactly what it is about and it has no particular centre or edge".
#
# The flag must be REPORTED as its own bucket and never silently dropped from the denominator. Excluding
# on annotator judgement removes precisely the labels where placement error is largest, which is the
# same bias direction that cutting tiles at 20 deg would have introduced -- invisible in the estimate
# and impossible to detect from inside it.
FLAGS = ('object-absent', 'ambiguous', 'occluded', 'no-extent')

# The 8 types prereg §3's corpus carries (Occlusion excluded -- it marks the view, not a thing in it).
CORPUS_LABEL_TYPES = frozenset({'CurbRamp', 'NoCurbRamp', 'Obstacle', 'SurfaceProblem', 'Crosswalk',
                                'Signal', 'NoSidewalk', 'Other'})

# §4's canonical-point rubric, verbatim, as data rather than prose in a prompt so that the text an
# annotator is held to is the text under version control. Binding: edge cases go to a FLAG, not to
# judgement drift, which is what makes the agreement gate interpretable.
RUBRIC = {
    'CurbRamp': 'Mark the centre of the ramp where it meets the gutter line.',
    'NoCurbRamp': 'Mark the centre of the would-be ramp where it would meet the gutter line.',
    'Obstacle': 'Mark the centroid of the obstruction at its point of ground contact.',
    'SurfaceProblem': 'Mark the centroid of the surface defect.',
    'Crosswalk': 'Mark the centre of the marked crosswalk area.',
    'Signal': 'Mark the centre of the pedestrian signal head.',
    'NoSidewalk': 'Mark the point on the roadway edge where the sidewalk is absent.',
    'Other': 'Mark the centroid of the feature at its point of ground contact.',
}

TileWindow = collections.namedtuple(
    'TileWindow', 'left top width height shifted wraps')


def tile_extent_px(pano_width, pano_height, fov_deg=TILE_FOV_DEG):
    """(width, height) in pixels of a `fov_deg` square angular window on this pano.

    Per axis, because equal degrees-per-pixel on both axes is a property of the 2:1 equirectangular
    aspect ratio rather than something to assume: a 16384x8192 pano gives a square tile, and a pano
    that was not 2:1 would not.

    Rounded to an EVEN number of pixels, which costs at most half a pixel of angular extent and buys an
    exactly invertible mapping: with an even width the window's centre lands on a pixel boundary, so the
    jittered point sits at exactly (width/2 - jx, height/2 - jy) and tile↔pano is integer arithmetic
    throughout. At 60 deg on a 16384-wide pano the natural extent is 2730.67 px, and the odd rounding put
    the centre half a pixel off — harmless against a 0.34 deg gate (half a pixel is 0.011 deg) but it
    makes every geometry assertion approximate, and an instrument whose invariants are approximate is one
    whose defects hide in the tolerance.
    """
    w = 2 * int(round(fov_deg / 360.0 * float(pano_width) / 2.0))
    h = 2 * int(round(fov_deg / 180.0 * float(pano_height) / 2.0))
    return w, h


def jitter_for(label_uid, seed):
    """This label's viewport offset in pixels, (jx, jy). Deterministic, logged in the geometry file.

    Derived from the label's OWN uid rather than from a position in a shared RNG stream, so that a
    corpus which gains or loses a label reproduces every surviving label's tile exactly. With a shared
    stream, one insertion reshuffles everything after it — and tiles already annotated would no longer
    match the geometry their annotation was recorded against, which silently invalidates the work
    rather than failing.

    `hashlib`, not `hash()`: Python salts string hashing per process, so `hash()` would give a
    different jitter on every run and the "seeded, logged" half of §4 would be a fiction.
    """
    digest = hashlib.blake2b(str(label_uid).encode('utf-8'), digest_size=8,
                             key=str(seed).encode('utf-8')).digest()
    rng = np.random.default_rng(int.from_bytes(digest, 'big'))
    magnitude = rng.integers(JITTER_MIN_PX, JITTER_MAX_PX + 1, size=2)
    sign = rng.choice(np.array([-1, 1]), size=2)
    return int(magnitude[0] * sign[0]), int(magnitude[1] * sign[1])


def tile_window(pano_x, pano_y, pano_width, pano_height, jx, jy, fov_deg=TILE_FOV_DEG):
    """The window to cut, centred on the jittered point. `left` is normalized into [0, pano_width).

    x wraps and y shifts, for the same reasons #47 gave the cropper: column 0 and column pano_width are
    the same place in the world so a straddling window is ordinary and must not be clipped, while the
    poles are not adjacent, so a window running off the top or bottom is slid back inside instead. The
    shift keeps the tile full-size with an exact mapping; clipping would narrow the tile and move its
    origin, and padding would show an annotator a black band they would read as the edge of the world.

    A pole shift cannot arise for a real corpus label — depression p99 is 43.5 deg against a 20 deg
    tile — so the branch exists to make an out-of-range read impossible, not because it is expected.
    """
    width, height = tile_extent_px(pano_width, pano_height, fov_deg)
    W, H = int(pano_width), int(pano_height)

    left = int(round(float(pano_x) + jx - width / 2.0)) % W

    top = int(round(float(pano_y) + jy - height / 2.0))
    shifted = False
    if top < 0:
        top, shifted = 0, True
    elif top + height > H:
        top, shifted = H - height, True

    return TileWindow(left=left, top=top, width=width, height=height,
                      shifted=shifted, wraps=left + width > W)


def window_from_geometry(g):
    """Rebuild a window from a geometry record, for the analysis side."""
    return TileWindow(left=int(g['left']), top=int(g['top']), width=int(g['width']),
                      height=int(g['height']), shifted=bool(g['shifted']), wraps=bool(g['wraps']))


def tile_to_pano(window, tile_x, tile_y, pano_width):
    """Tile pixel -> pano pixel. The conversion every gold annotation goes through."""
    return ((window.left + int(tile_x)) % int(pano_width), window.top + int(tile_y))


def pano_to_tile(window, pano_x, pano_y, pano_width):
    """Pano pixel -> tile pixel; the exact inverse of `tile_to_pano` for points inside the window."""
    return ((int(round(float(pano_x))) - window.left) % int(pano_width),
            int(round(float(pano_y))) - window.top)


def cut_tile(image, window, pano_width):
    """Crop the window out of a decoded pano, pasting the two halves when it straddles the seam.

    Never pads: a seam-crossing tile is assembled from both edges of the raster, so it carries no
    synthetic black. That is the #47 lesson — a black band is not a neutral absence of information, it
    reads as a wall, and an annotator places differently against it.
    """
    from PIL import Image

    W = int(pano_width)
    left, top, bottom = window.left, window.top, window.top + window.height
    right = left + window.width
    if right <= W:
        return image.crop((left, top, right, bottom))

    head = image.crop((left, top, W, bottom))
    tail = image.crop((0, top, right - W, bottom))
    out = Image.new(image.mode, (window.width, window.height))
    out.paste(head, (0, 0))
    out.paste(tail, (head.width, 0))
    return out


def tile_name(label_uid):
    """The tile's filename. Deliberately carries the uid and nothing else — a name like
    `p3_8000_5000.jpg` would hand the annotator the stored coordinate the task file withholds."""
    return f"{str(label_uid).replace(':', '_')}.jpg"


def build_tasks(corpus, seed):
    """Split the corpus into (annotator-facing tasks, private geometry).

    The separation is the blindness mechanism, and it is structural: the analysis needs the tile origin
    to convert an annotation into a pano coordinate, and the tile origin is
    `stored + jitter - size/2`, so anything that can convert can also invert. Hence two files, and the
    seed lives only in the private one — publishing it alongside the uid would let anyone recompute
    the jitter and back out the answer.
    """
    unknown = sorted(set(corpus['label_type']) - set(RUBRIC))
    if unknown:
        raise ValueError(f'no rubric for label type(s) {unknown}: §4\'s rubric is binding, and a task '
                         f'shipped without one asks the annotator to use their judgement, which is '
                         f'what the rubric exists to prevent')

    tasks, geometry = [], {}
    for row in corpus.itertuples():
        jx, jy = jitter_for(row.label_uid, seed)
        win = tile_window(row.pano_x, row.pano_y, row.pano_width, row.pano_height, jx, jy)
        tasks.append({
            'label_uid': row.label_uid,
            'tile': tile_name(row.label_uid),
            'tile_width': win.width,
            'tile_height': win.height,
            'label_type': row.label_type,
            'rubric': RUBRIC[row.label_type],
        })
        geometry[row.label_uid] = {
            'city': row.city, 'pano_id': row.pano_id, 'label_type': row.label_type,
            'left': win.left, 'top': win.top, 'width': win.width, 'height': win.height,
            'shifted': win.shifted, 'wraps': win.wraps,
            'jitter_x': jx, 'jitter_y': jy,
            'pano_width': float(row.pano_width), 'pano_height': float(row.pano_height),
            'pano_x': float(row.pano_x), 'pano_y': float(row.pano_y),
        }

    # Interleaved, not grouped by stratum: §4 runs Jon's 50 through the same tooling, and an order
    # blocked by band or type lets an annotator calibrate within a block and drift between blocks --
    # which shows up as between-annotator disagreement that is really an artefact of the queue.
    order = np.random.default_rng(seed).permutation(len(tasks))
    tasks = [tasks[i] for i in order]

    return ({'protocol': 'reports/2026-08-09-crop-priors-prereg.md §4',
             'flags': list(FLAGS),
             'n_tasks': len(tasks),
             # The fraction of the cut tile the view starts at, and the cut's angular width. Both are
             # safe to ship: they are constants of the protocol, identical for every label, so neither
             # says anything about where any stored point is. `cut_fov_deg` is here as well as in the
             # private geometry because the page labels its framing control in degrees, and a page that
             # has only the fraction can offer "a third of the tile" but not "20°".
             'initial_view_fraction': VIEW_FOV_DEG / CUT_FOV_DEG,
             'cut_fov_deg': CUT_FOV_DEG,
             'tasks': tasks},
            {'seed': seed, 'cut_fov_deg': CUT_FOV_DEG, 'view_fov_deg': VIEW_FOV_DEG,
             'jitter_px': [JITTER_MIN_PX, JITTER_MAX_PX],
             'geometry': geometry})


def pano_path(pano_root, city, pano_id):
    """Where a corpus pano is cached locally: the store's own layout, one definition of it."""
    return store_coverage.store_path(pano_root, city, pano_id)


def render(corpus, tasks, geometry, pano_root, out_dir):
    """Cut every task's tile, decoding each pano exactly once.

    Grouped by pano for the reason CropRunner groups: a 16384x8192 equirect is ~400 MB decoded, and the
    corpus deliberately puts up to 3 labels on one pano. Returns per-label outcomes; a pano missing from
    the local cache is reported as unreachable rather than raised, because the store is known not to
    hold every drawn pano and one gap must not discard the rest of the run.
    """
    from PIL import Image

    os.makedirs(out_dir, exist_ok=True)
    by_pano = collections.defaultdict(list)
    for task in tasks['tasks']:
        g = geometry['geometry'][task['label_uid']]
        by_pano[(g['city'], g['pano_id'])].append(task['label_uid'])

    outcomes = {}
    for (city, pano_id), uids in sorted(by_pano.items()):
        path = pano_path(pano_root, city, pano_id)
        if not os.path.isfile(path):
            for uid in uids:
                outcomes[uid] = 'unreachable'
            continue
        try:
            with Image.open(path) as image:
                image.load()
                served = image.size
                for uid in uids:
                    g = geometry['geometry'][uid]
                    # §3's dims-mismatch exclusion, applied against the acquired JPEG rather than
                    # against gsv_data -- the store holds whatever Google served at scrape time, so
                    # this is the comparison #77's preflight is actually about. A tile cut at the
                    # wrong scale would put the object somewhere else entirely.
                    if served != (int(g['pano_width']), int(g['pano_height'])):
                        outcomes[uid] = 'dims_mismatch'
                        continue
                    win = window_from_geometry(g)
                    tile = cut_tile(image, win, g['pano_width'])
                    tile.convert('RGB').save(os.path.join(out_dir, tile_name(uid)), quality=95)
                    outcomes[uid] = 'rendered'
        except OSError:
            for uid in uids:
                outcomes[uid] = outcomes.get(uid, 'unreadable')
    return outcomes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('corpus', help='the drawn corpus CSV from corpus_sample.py')
    ap.add_argument('--pano-root', required=True, help='local cache of corpus panos')
    ap.add_argument('--out-dir', required=True, help='where tiles, tasks.json and geometry.json go')
    ap.add_argument('--seed', type=int, required=True,
                    help='jitter seed, recorded in geometry.json (never in tasks.json)')
    ap.add_argument('--measurable-only', action='store_true',
                    help='restrict to Study 1 subjects (drops Crosswalk/NoSidewalk/region-tagged '
                         'SurfaceProblem, which have no point for a displacement to be measured from)')
    args = ap.parse_args(argv)

    # A 16384x8192 corpus pano is 134 MP, over Pillow's 89 MP DecompressionBombWarning default, so
    # every modern pano warns. Reused from CropRunner rather than re-set here: it owns the constant and
    # the reasoning for why the ceiling is a named number and not None, and it is process-level policy,
    # which is why main() calls it and `render` does not.
    CropRunner.raise_decompression_bomb_ceiling()

    opener = gzip.open if args.corpus.endswith('.gz') else open
    with opener(args.corpus, 'rt', encoding='utf-8') as f:
        corpus = pd.read_csv(f, dtype={'pano_id': str})
    if args.measurable_only:
        corpus = corpus[corpus['measurable']]
    print(f'{len(corpus)} labels over {corpus["pano_id"].nunique()} panos', flush=True)

    tasks, geometry = build_tasks(corpus, args.seed)
    outcomes = render(corpus, tasks, geometry, args.pano_root, args.out_dir)

    counts = collections.Counter(outcomes.values())
    rendered = {uid for uid, o in outcomes.items() if o == 'rendered'}

    # The task file ships only what was actually rendered: a task pointing at a tile that does not
    # exist is a blank screen an annotator has no way to act on, and the flags are for the imagery, not
    # for the pipeline.
    tasks['tasks'] = [t for t in tasks['tasks'] if t['label_uid'] in rendered]
    tasks['n_tasks'] = len(tasks['tasks'])
    geometry['outcomes'] = outcomes

    for name, payload in (('tasks.json', tasks), ('geometry.json', geometry)):
        with open(os.path.join(args.out_dir, name), 'w', encoding='utf-8', newline='\n') as f:
            json.dump(payload, f, indent=1, allow_nan=False)

    total = sum(counts.values())
    print(f'rendered {counts["rendered"]}/{total} tiles into {args.out_dir}')
    for outcome in ('dims_mismatch', 'unreachable', 'unreadable'):
        if counts[outcome]:
            print(f'  {outcome}: {counts[outcome]} '
                  f'({fmt(100.0 * counts[outcome] / total, ".1f")}%)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
