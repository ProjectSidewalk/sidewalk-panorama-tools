#!/usr/bin/env python3
"""Calibrate the edge black-band limit on the real pano store (#179 item 4).

The plan is the one agreed on #179 (2026-11-22). This script never measures a band itself: every band depth
in the study comes from the UNMODIFIED production sweep, `scan_black_bands.py`, run over a symlink farm of a
frozen id list. What this script does is everything around that run:

* ``frame STORE_ROOT --farm DIR --out DIR [--roster CSV]``: enumerate every city folder of the store with
  `common.walk_store_panos` (no pano is opened; a ``CAoS`` file is stat'ed for its mtime), classify each id by
  form, freeze the ids to measure (census or a seeded sample of 2,500 ``gsv22`` ids, drawn once), write
  ``frame.json`` and ``ids/<city>.txt.gz`` under --out, and build ``<farm>/<city>/<shard>/<id>.jpg`` as
  symlinks to the stored files. Refuses to run twice.
* ``run --farm DIR --out DIR --store-root DIR``: dispatch the unmodified sweep over each farm, one child
  process per city, at most two at once, under a 60 MB/s aggregate cap, the abort rules fixed before the run
  and a hard stop. It has no code path that draws or enumerates the store.
* ``extras``: for panos whose ledger row shows any band, the measurements the ledger does not carry (top
  band, the line-minimum sensitivity, the JPEG quantisation tables, the downloader's in-memory definition).
* ``nowrites``: the before/after listing of the store root and every city root, and its comparison.
* ``instrument``: the committed band fixtures measured in the run environment, before any store read.
* ``tolerance``: the tile-row tolerance `t` of the F-rows class, measured on a stitch made by the stitcher.
* ``crosscheck``: the optional id-form against `/adminapi/panos` `source` check (paced, serial, one attempt).
* ``analyze DATA_DIR --write JSON``: offline, from committed files only.

Nothing here writes under the store root: every output path is checked by `check_outputs` (non-empty, not
inside the store by `os.path.commonpath` on real paths, not on the store's device) in `frame`, `run` and
`extras`.

Usage (paths are placeholders; see reports/2026-11-22-edge-band-calibration.md):
    python3 reports/scripts/edge_band_calibration.py frame <store-root> --farm <farm> --out <out>
    python3 reports/scripts/edge_band_calibration.py run --farm <farm> --out <out> --store-root <store-root>
    python3 reports/scripts/edge_band_calibration.py analyze reports/data/2026-11-22-edge-band-calibration \
        --write reports/data/2026-11-22-edge-band-calibration.json
"""

import argparse
import csv
import datetime
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, deque

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO_ROOT)
from studyfmt import fmt, num  # noqa: E402

from downloaders import common  # noqa: E402
from downloaders.common import jpeg_dimensions, walk_store_panos  # noqa: E402

SCAN_TOOL = os.path.join(REPO_ROOT, 'scan_black_bands.py')
TILE = 512

# --- The design, fixed before the run (plan section 2) ---------------------------------------------------------
CENSUS_MAX_GSV = 2500           # a city with at most this many gsv22 panos is swept whole
SAMPLE_SIZE = 2500              # otherwise a simple random sample of this many gsv22 ids, drawn once
SEED_PREFIX = '179-calibration:'
#: A CAoS file is the photo host's bytes only when it landed on or after the committed census that found no
#: CAoS pano stored anywhere (reports/2026-11-05-photosphere-forms.md). Earlier, it is `other`.
PHOTOSPHERE_SINCE = datetime.datetime(2026, 11, 5, tzinfo=datetime.timezone.utc)
PHOTOSPHERE_SINCE_NS = int(PHOTOSPHERE_SINCE.timestamp()) * 10 ** 9
P_FORMS = ('numeric', 'uuid', 'photosphere')
FORMS = ('gsv22', 'numeric', 'uuid', 'photosphere', 'other')
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')

# D1 (plan 2.5). The candidates, the pass figures and the fold's cluster size are decision 10 (provisional).
CANDIDATES = (0.05, 0.0605)
PASS_ESTIMATE = 1e-4            # point estimate at most 1 per 10,000 stored gsv22 stitches
PASS_BOUND = 15e-4              # conservative one-sided 95% upper bound at most 15 per 10,000
CONFIDENCE = 0.95
FOLD_MIN = 5
#: The ledger stores 4 decimals (scan_black_bands._fmt), so a depth times the height is known to +-0.5e-4 H.
LEDGER_ROUNDING = 0.5e-4

# --- Load limits (plan 2.3); not to be relaxed -------------------------------------------------------------------
MAX_READERS = 2
RATE_CAP_BYTES = 60e6           # aggregate bytes per second, from the ledgers' `size` column
RATE_WINDOW_S = 10.0
RESUME_FRACTION = 0.8           # a paused reader resumes once the rate is under this share of the cap
D_STATE_LIMIT_S = 30.0          # continuously in state D
STALL_LIMIT_S = 120.0           # unpaused time with no ledger row, no stdout line and no rchar growth
SLOW_FACTOR = 3.0               # last 100 panos of a source slower than 3x the baseline's time per 100
SLOW_WINDOW = 100
SAMPLE_EVERY_S = 1.0            # the D-state rule needs a sample at least every 2 s
TIMING_EVERY_S = 10.0
CHILD_MARGIN_MIN = 3.0          # a child's --max-runtime ends this long before the hard stop
TERM_WAIT_S = 30.0

EXIT_REFUSED = 4                # a guard, a frozen draw that exists, or an earlier session's live child
EXIT_ABORT = 5                  # an abort rule fired
EXIT_CHILD = 6                  # a child exited 3 (refused ledger) or with any code but 0 and 1

#: Names the sweep or this study can create. The no-writes check fails only on these.
STUDY_NAMES = ('frame.json', 'snapshot.json', 'timing.csv', 'extras.csv', 'extras.csv.gz', 'run.pids',
               'abort.json', 'instrument-check.csv', 'no-writes-check.txt', 'unresolved.csv', 'crosscheck.csv')


class GuardError(Exception):
    """An output path that could land on the store; nothing was written."""


class RefusedError(Exception):
    """A frozen draw that already exists, or an earlier session's child still alive; nothing was written."""


# --- Id forms -----------------------------------------------------------------------------------------------------

def id_form(pano_id, mtime_ns=None):
    """The id's form: gsv22, numeric (Mapillary), uuid (Panoramax), photosphere, other.

    A ``CAoS`` id is a photosphere only when its file's mtime is on or after PHOTOSPHERE_SINCE; an earlier or
    unknown one is ``other`` (a CAoS id that went down the tile path before #223 is a stitch, not provider bytes).

    Example::

        >>> id_form('947478730505514'), id_form('a' * 22)
        ('numeric', 'gsv22')
    """
    if pano_id.startswith('CAoS'):
        return 'photosphere' if mtime_ns is not None and mtime_ns >= PHOTOSPHERE_SINCE_NS else 'other'
    if pano_id.isascii() and pano_id.isdigit():
        return 'numeric'
    if _UUID.match(pano_id):
        return 'uuid'
    if len(pano_id) == 22:
        return 'gsv22'
    return 'other'


def source_of(form):
    """'provider' for provider bytes (numeric, uuid, photosphere), 'gsv' for stitches; the 3x rule's key."""
    return 'provider' if form in P_FORMS else 'gsv'


# --- Guards -------------------------------------------------------------------------------------------------------

def _device(path):
    probe = os.path.realpath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return os.stat(probe).st_dev


def check_outputs(store_root, dirs, device_of=None):
    """Raise GuardError unless every dir in `dirs` is a non-empty path, outside the store root by real path, and
    on another device than the store root.

    `os.path.commonpath` on real paths, never a prefix test: `<root>2` is not inside `<root>`, and a symlink
    into the store is. The device test catches a second mount of the same export. `device_of` is for tests,
    whose temporary directories all share one device.
    """
    device_of = device_of or _device
    if not store_root or not str(store_root).strip():
        raise GuardError('no store root given')
    store_real = os.path.realpath(store_root)
    store_dev = device_of(store_real)
    for d in dirs:
        if d is None or not str(d).strip():
            raise GuardError('an output directory is empty; refusing (an empty path falls back to the store)')
        real = os.path.realpath(d)
        try:
            inside = os.path.commonpath([real, store_real]) == store_real
        except ValueError:          # different drives (Windows): not inside
            inside = False
        if inside:
            raise GuardError('%s resolves inside the store root; refusing' % d)
        if device_of(real) == store_dev:
            raise GuardError('%s is on the store root\'s device; refusing' % d)


# --- Redaction ----------------------------------------------------------------------------------------------------

def make_redactor(roots):
    """A function rewriting every path in `roots` [(path, token), ...] to its token, longest first.

    Each root is rewritten in its given, absolute and real forms, so a symlinked scratch dir is covered too.
    """
    pairs = []
    for path, token in roots:
        if not path or len(str(path).strip()) < 2:
            continue
        for form in {str(path), os.path.abspath(path), os.path.realpath(path)}:
            form = form.rstrip('/\\')
            if len(form) >= 2:
                pairs.append((form, token))
    pairs.sort(key=lambda p: -len(p[0]))

    def redact(text):
        for path, token in pairs:
            text = text.replace(path, token)
        return text
    return redact


# --- frame --------------------------------------------------------------------------------------------------------

def load_roster(path):
    """The analyzer's roster: the city_ids of cities.csv in file order, '#' rows left out (analyze.py:1121)."""
    with open(path, newline='', encoding='utf-8') as f:
        return [r['city_id'].strip() for r in csv.DictReader(f)
                if r.get('city_id') and not r['city_id'].strip().startswith('#')]


def city_folders(store_root):
    """Every folder under the store root that holds at least one 2-char shard directory, sorted."""
    out = []
    for name in sorted(os.listdir(store_root)):
        path = os.path.join(store_root, name)
        if not os.path.isdir(path):
            continue
        if any(len(s) == 2 and os.path.isdir(os.path.join(path, s)) for s in os.listdir(path)):
            out.append(name)
    return out


def enumerate_city(city_root):
    """{pano_id: form} for every stored pano of one city, by walk_store_panos. Only CAoS files are stat'ed."""
    forms = {}
    for path in walk_store_panos(city_root):
        pano_id = os.path.basename(path)[:-len('.jpg')]
        mtime = os.stat(path).st_mtime_ns if pano_id.startswith('CAoS') else None
        forms[pano_id] = id_form(pano_id, mtime)
    return forms


def draw(city, forms):
    """(frozen ids sorted, design) for one city: every non-gsv22 id, and every gsv22 id or a seeded sample."""
    gsv = sorted(i for i, f in forms.items() if f == 'gsv22')
    rest = [i for i, f in forms.items() if f != 'gsv22']
    if len(gsv) <= CENSUS_MAX_GSV:
        return sorted(rest + gsv), 'census'
    sample = random.Random(SEED_PREFIX + city).sample(gsv, SAMPLE_SIZE)
    return sorted(rest + sample), 'sample'


def write_ids(path, ids):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'xb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as f:
            f.write(''.join(i + '\n' for i in ids).encode('utf-8'))


def read_ids(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]


def build_farm(store_root, farm_city, city, ids):
    """<farm>/<city>/<shard>/<id>.jpg as a symlink to the stored file, under real shard directories."""
    os.makedirs(farm_city)
    for pano_id in ids:
        shard = os.path.join(farm_city, pano_id[:2])
        os.makedirs(shard, exist_ok=True)
        os.symlink(os.path.join(store_root, city, pano_id[:2], pano_id + '.jpg'),
                   os.path.join(shard, pano_id + '.jpg'))


def frame(store_root, farm, out, roster=None, device_of=None, now=None):
    """Freeze the draw and build the farms; see the module docstring. Returns the frame dict.

    Refuses (RefusedError, before writing anything) when frame.json, any city's id list or any city's farm
    already exists, so the draw happens once and a resume can never redraw it.
    """
    check_outputs(store_root, [farm, out], device_of)
    if os.path.exists(os.path.join(out, 'frame.json')):
        raise RefusedError('frame.json already exists under the output dir; the draw is frozen')
    cities = city_folders(store_root)
    for city in cities:
        for p in (os.path.join(out, 'ids', city + '.txt.gz'), os.path.join(farm, city)):
            if os.path.exists(p):
                raise RefusedError('%s already exists; the draw is frozen' % city)
    roster = list(roster or [])
    seen = set()          # gsv22 ids of earlier roster folders, in roster order, for the de-duplication
    enumerated = {city: enumerate_city(os.path.join(store_root, city)) for city in cities}
    order = [c for c in roster if c in enumerated] + [c for c in cities if c not in roster]
    result = {}
    for city in order:
        forms = enumerated[city]
        ids, design = draw(city, forms)
        all_sorted = sorted(forms)
        gsv = [i for i in all_sorted if forms[i] == 'gsv22']
        in_roster = city in roster
        dup = sorted(i for i in ids if forms[i] == 'gsv22' and i in seen) if in_roster else []
        dedup = sum(1 for i in gsv if i not in seen) if in_roster else None
        if in_roster:
            seen.update(gsv)
        result[city] = {
            'panos': len(forms),
            'forms': {f: n for f, n in sorted(Counter(forms.values()).items())},
            'ids_sha256': hashlib.sha256(''.join(i + '\n' for i in all_sorted).encode('utf-8')).hexdigest(),
            'design': design,
            'frozen': len(ids),
            'frozen_forms': {f: n for f, n in sorted(Counter(forms[i] for i in ids).items())},
            'gsv22': len(gsv),
            'gsv22_dedup': dedup,
            'frozen_dup_of_earlier': dup,
            'in_roster': in_roster,
            'roster_index': roster.index(city) if in_roster else None,
        }
        write_ids(os.path.join(out, 'ids', city + '.txt.gz'), ids)
        build_farm(store_root, os.path.join(farm, city), city, ids)
    doc = {
        'generated_utc': (now or datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'store_root': '<store-root>',
        'seed_prefix': SEED_PREFIX, 'census_max_gsv22': CENSUS_MAX_GSV, 'sample_size': SAMPLE_SIZE,
        'photosphere_since_utc': PHOTOSPHERE_SINCE.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'excluded_folders': sorted(n for n in os.listdir(store_root)
                                   if os.path.isdir(os.path.join(store_root, n)) and n not in enumerated),
        'roster': [c for c in roster],
        'cities': result,
    }
    with open(os.path.join(out, 'frame.json'), 'x', encoding='utf-8') as f:
        json.dump(doc, f, indent=1, sort_keys=True, allow_nan=False)
    return doc


def run_order(frame_doc):
    """The order cities are swept in: the non-GSV census first (smallest first), then the rest smallest first."""
    cities = frame_doc['cities']

    def provider(c):
        ff = cities[c]['frozen_forms']
        return sum(n for f, n in ff.items() if f in P_FORMS) * 2 > cities[c]['frozen']
    first = sorted((c for c in cities if provider(c)), key=lambda c: (cities[c]['frozen'], c))
    rest = sorted((c for c in cities if not provider(c)), key=lambda c: (cities[c]['frozen'], c))
    return first + rest


# --- run: the abort rules and the rate cap, on samples -------------------------------------------------------------

class Watch:
    """The abort rules for one child, fed one sample at a time (plan 2.3, review E2).

    `sample(t, state, sources, lines, rchar, paused)`: `state` is the /proc state letter, `sources` the source
    of each ledger row landed since the last sample, `lines` the stdout lines since then, `rchar` the child's
    /proc/<pid>/io rchar (None when unreadable), `paused` whether the dispatcher holds the child stopped from
    now until the next sample. Returns the abort reason, or None.

    The stall clock and the 3x rule count only unpaused time: the interval since the previous sample counts
    when the child was NOT held stopped during it. A resume pass writes no rows for panos already ledgered, so
    rows alone never decide a stall; stdout lines and rchar growth count as progress too.
    """

    def __init__(self, baseline=None):
        self.baseline = baseline or {}
        self.active = 0.0
        self.paused_s = 0.0
        self.last_t = None
        self.was_paused = False
        self.d_since = None
        self.quiet = 0.0
        self.rchar = None
        self.windows = defaultdict(deque)

    def sample(self, t, state, sources=(), lines=0, rchar=None, paused=False):
        dt = 0.0
        if self.last_t is not None:
            if self.was_paused:
                self.paused_s += t - self.last_t
            else:
                dt = t - self.last_t
                self.active += dt
        self.last_t = t
        self.was_paused = paused
        if state == 'D':
            if self.d_since is None:
                self.d_since = t
            if t - self.d_since >= D_STATE_LIMIT_S:
                return 'uninterruptible sleep (state D) for %.0f s' % (t - self.d_since)
        else:
            self.d_since = None
        grew = rchar is not None and self.rchar is not None and rchar > self.rchar
        if rchar is not None:
            self.rchar = rchar
        if sources or lines or grew:
            self.quiet = 0.0
        else:
            self.quiet += dt
            if self.quiet >= STALL_LIMIT_S:
                return 'no ledger row, stdout line or read for %.0f s of unpaused time' % self.quiet
        for source in sources:
            window = self.windows[source]
            window.append(self.active)
            if len(window) > SLOW_WINDOW + 1:
                window.popleft()
            per100 = self.baseline.get(source)
            if per100 and len(window) == SLOW_WINDOW + 1 and window[-1] - window[0] > SLOW_FACTOR * per100:
                return ('last %d %s panos took %.1f s, over %.0fx the baseline %.1f s'
                        % (SLOW_WINDOW, source, window[-1] - window[0], SLOW_FACTOR, per100))
        return None


class RateCap:
    """Aggregate bytes per second over a trailing window, from the sizes of ledger rows as they land."""

    def __init__(self, cap=RATE_CAP_BYTES, window=RATE_WINDOW_S, start=0.0):
        self.cap, self.window, self.start = cap, window, start
        self.events = deque()

    def add(self, t, nbytes):
        self.events.append((t, nbytes))

    def rate(self, t):
        while self.events and self.events[0][0] < t - self.window:
            self.events.popleft()
        span = max(1.0, min(self.window, t - self.start))
        return sum(n for _, n in self.events) / span

    def decide(self, t, running, paused):
        """('pause', i) / ('resume', i) / None for one child per sample. `running` and `paused` are child keys
        in start order; the most recently started running child is paused first and resumed last."""
        rate = self.rate(t)
        if rate > self.cap and running:
            return ('pause', running[-1])
        if rate < RESUME_FRACTION * self.cap and paused:
            return ('resume', paused[0])
        return None


SUMMARY_LINE = re.compile(r'Examined (\d+) panorama\(s\): (\d+) with .*?, (\d+) without, (\d+) already ledgered, '
                          r'(\d+) failed, (\d+) unreached\.')


class _Child:
    def __init__(self, key, city, handle, ledger, offset, baseline):
        self.key, self.city, self.handle = key, city, handle
        self.pid = handle.pid
        self.ledger, self.offset, self.partial = ledger, offset, b''
        self.watch = Watch(baseline)
        self.rows = 0
        self.bytes = 0
        self.lines = 0
        self.paused = False
        self.exit = None
        self.summary = None
        self.state = '?'
        self.per_source = Counter()


def read_new_rows(child):
    """Ledger rows appended since the last call: [(pano_id, size, mtime_ns)]. A torn last line waits."""
    try:
        with open(child.ledger, 'rb') as f:
            f.seek(child.offset)
            data = f.read()
    except OSError:
        return []
    child.offset += len(data)
    data = child.partial + data
    lines = data.split(b'\n')
    child.partial = lines.pop()
    rows = []
    for raw in lines:
        fields = next(csv.reader([raw.decode('utf-8', 'replace').rstrip('\r')]), [])
        if len(fields) < 3 or fields[0] == 'pano_id':
            continue
        try:
            rows.append((fields[0], int(fields[1]), int(fields[2])))
        except ValueError:
            continue
    return rows


class RealOps:
    """The POSIX facilities the dispatcher uses; tests inject a fake with the same methods."""

    def now(self):
        return time.monotonic()

    def wall(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)

    def spawn(self, cmd, cwd):
        env = dict(os.environ, PYTHONUNBUFFERED='1')
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, start_new_session=True, text=True, encoding='utf-8', errors='replace')
        proc.lines = deque()

        def pump():
            for line in proc.stdout:
                proc.lines.append(line.rstrip('\n'))
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        proc.pump = thread
        return proc

    def take_lines(self, handle):
        out = []
        while handle.lines:
            out.append(handle.lines.popleft())
        return out

    def poll(self, handle):
        code = handle.poll()
        if code is not None:
            handle.pump.join(timeout=5)
        return code

    def signal_group(self, pid, name):
        sig = getattr(signal, 'SIG' + name)
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def proc_state(self, pid):
        try:
            with open('/proc/%d/stat' % pid) as f:
                return f.read().rsplit(')', 1)[1].split()[0]
        except (OSError, IndexError):
            return '?'

    def rchar(self, pid):
        try:
            with open('/proc/%d/io' % pid) as f:
                for line in f:
                    if line.startswith('rchar:'):
                        return int(line.split()[1])
        except (OSError, ValueError):
            return None
        return None

    def pid_alive(self, pid):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False
        try:
            with open('/proc/%d/cmdline' % pid, 'rb') as f:
                return b'scan_black_bands' in f.read()
        except OSError:
            return True


class DispatcherSignal(BaseException):
    """SIGTERM or SIGHUP to the dispatcher: unwinds to the cleanup that continues and ends every child."""


def _utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def run(farm, out, store_root, cities, deadline, readers=1, baseline=None, ops=None, device_of=None, work=None,
        session=None, frame_doc=None):
    """Dispatch the unmodified sweep over each city's farm; see plan 2.3. Returns (exit code, session dict).

    `deadline` is in ops.now() seconds (the hard stop, or the end of the opening slice). Every exit path -
    normal end, abort, the deadline, a child's refusal, an exception, SIGTERM/SIGHUP - sends SIGCONT and then
    SIGTERM to every child's process group, because a stopped process acts on SIGTERM only once continued.
    """
    ops = ops or RealOps()
    check_outputs(store_root, [farm, out], device_of)
    readers = max(1, min(int(readers), MAX_READERS))
    os.makedirs(out, exist_ok=True)
    pidfile = os.path.join(out, 'run.pids')
    if os.path.exists(pidfile):
        with open(pidfile) as f:
            earlier = [int(x) for x in f.read().split() if x.strip().isdigit()]
        alive = [p for p in earlier if ops.pid_alive(p)]
        if alive:
            raise RefusedError('a child of an earlier session is still alive (pid %s); refusing'
                               % ', '.join(map(str, alive)))
    with open(pidfile, 'w') as f:
        f.write('')
    if work is None:
        try:
            work = os.path.commonpath([os.path.abspath(farm), os.path.abspath(out)])
        except ValueError:
            work = None
        if work and len([p for p in re.split(r'[\\/]', work) if p]) < 3:
            work = None
    redact = make_redactor([(out, '<out>'), (farm, '<farm>'), (store_root, '<store-root>'), (work, '<work>')])
    forms_by_city = {}
    session = session or datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    queue = [c for c in cities if not os.path.exists(os.path.join(out, 'sweep', c, 'done.json'))]
    children, finished = [], []
    started_wall = ops.wall()
    t0 = ops.now()
    rate = RateCap(start=t0)
    stop_reason, abort_reason, code = None, None, 0
    timing_path = os.path.join(out, 'timing.csv')
    new_timing = not os.path.exists(timing_path)
    timing = open(timing_path, 'a', newline='', encoding='utf-8')
    tw = csv.writer(timing)
    if new_timing:
        tw.writerow(['utc', 'session', 'city', 'rows', 'bytes', 'paused', 'state'])
    last_timing = t0
    old_handlers = {}
    if isinstance(ops, RealOps):
        def _on_signal(signum, _frame):
            raise DispatcherSignal('signal %d' % signum)
        for name in ('SIGTERM', 'SIGHUP'):
            sig = getattr(signal, name, None)
            if sig is not None:
                old_handlers[sig] = signal.signal(sig, _on_signal)

    def start(city):
        sweep = os.path.join(out, 'sweep', city)
        os.makedirs(sweep, exist_ok=True)
        ledger = os.path.join(sweep, 'black_bands.csv')
        minutes = max(0.5, (deadline - ops.now()) / 60.0 - CHILD_MARGIN_MIN)
        cmd = [sys.executable, SCAN_TOOL, os.path.join(farm, city), '--ledger', ledger,
               '--worklist', os.path.join(sweep, 'black_band_worklist.csv'), '--max-runtime', '%.2f' % minutes]
        offset = os.path.getsize(ledger) if os.path.exists(ledger) else 0
        handle = ops.spawn(cmd, REPO_ROOT)
        with open(pidfile, 'a') as f:
            f.write('%d\n' % handle.pid)
        child = _Child(len(children) + len(finished), city, handle, ledger, offset, baseline)
        child.started = ops.now()
        children.append(child)
        return child

    def stdout_write(child, lines):
        if lines:
            with open(os.path.join(out, 'sweep', child.city, 'stdout.txt'), 'a', encoding='utf-8') as f:
                for line in lines:
                    f.write(redact(line) + '\n')

    try:
        while True:
            if ops.now() >= deadline:
                stop_reason = 'deadline'
                break
            while queue and len(children) < readers and stop_reason is None:
                start(queue.pop(0))
            if not children:
                stop_reason = 'done'
                break
            ops.sleep(SAMPLE_EVERY_S)
            t = ops.now()
            for child in list(children):
                rows = read_new_rows(child)
                lines = ops.take_lines(child.handle)
                stdout_write(child, lines)
                child.lines += len(lines)
                sources = []
                for pano_id, size, mtime in rows:
                    src = source_of(id_form(pano_id, mtime))
                    sources.append(src)
                    child.per_source[src] += 1
                    child.rows += 1
                    child.bytes += size
                    rate.add(t, size)
                for line in lines:
                    m = SUMMARY_LINE.search(line)
                    if m:
                        child.summary = {k: int(v) for k, v in zip(
                            ('examined', 'banded', 'clean', 'current', 'failed', 'unreached'), m.groups())}
                exit_code = ops.poll(child.handle)
                if exit_code is not None:
                    stdout_write(child, ops.take_lines(child.handle))
                    child.exit = exit_code
                    children.remove(child)
                    finished.append(child)
                    if exit_code in (0, 1):
                        if child.summary and child.summary['unreached'] == 0:
                            with open(os.path.join(out, 'sweep', child.city, 'done.json'), 'w') as f:
                                json.dump({'session': session, 'exit': exit_code, 'summary': child.summary}, f)
                        elif child.summary and child.summary['unreached']:
                            stop_reason = stop_reason or 'deadline (a child reached its --max-runtime)'
                    else:
                        abort_reason = 'child for %s exited %s' % (child.city, exit_code)
                        code = EXIT_CHILD
                    continue
                child.state = ops.proc_state(child.pid)
                reason = child.watch.sample(t, child.state, sources, len(lines), ops.rchar(child.pid), child.paused)
                if reason:
                    abort_reason = '%s: %s' % (child.city, reason)
                    code = EXIT_ABORT
            if abort_reason:
                break
            if stop_reason and not children:
                break
            running = [c for c in children if not c.paused]
            paused = [c for c in children if c.paused]
            decision = rate.decide(t, running, paused)
            if decision:
                action, child = decision
                ops.signal_group(child.pid, 'STOP' if action == 'pause' else 'CONT')
                child.paused = action == 'pause'
                child.watch.was_paused = child.paused
            if t - last_timing >= TIMING_EVERY_S:
                last_timing = t
                for child in children:
                    tw.writerow([_utc(ops.wall()), session, child.city, child.rows, child.bytes, int(child.paused),
                                 child.state])
                timing.flush()
    except BaseException as e:      # an exception, or SIGTERM/SIGHUP to the dispatcher
        abort_reason = abort_reason or 'dispatcher %s: %s' % (type(e).__name__, e)
        code = code or EXIT_ABORT
        raise
    finally:
        still = []
        for child in children:
            ops.signal_group(child.pid, 'CONT')
            ops.signal_group(child.pid, 'TERM')
        waited = ops.now()
        for child in children:
            while ops.poll(child.handle) is None and ops.now() - waited < TERM_WAIT_S:
                ops.sleep(0.5)
            child.exit = ops.poll(child.handle)
            if child.exit is None:
                still.append(child.pid)
            stdout_write(child, ops.take_lines(child.handle))
            finished.append(child)
        timing.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        doc = {
            'session': session, 'start_utc': _utc(started_wall), 'end_utc': _utc(ops.wall()),
            'readers': readers, 'rate_cap_bytes_per_s': RATE_CAP_BYTES,
            'stop_reason': stop_reason, 'abort_reason': abort_reason,
            'children_alive_after_term': still, 'baseline_per100_s': baseline or {},
            'children': [{'city': c.city, 'exit': c.exit, 'rows': c.rows, 'bytes': c.bytes, 'stdout_lines': c.lines,
                          'active_s': round(c.watch.active, 1), 'paused_s': round(c.watch.paused_s, 1),
                          'per_source_rows': dict(c.per_source), 'summary': c.summary} for c in finished],
        }
        os.makedirs(os.path.join(out, 'sessions'), exist_ok=True)
        with open(os.path.join(out, 'sessions', session + '.json'), 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1, sort_keys=True, allow_nan=False)
        if abort_reason:
            with open(os.path.join(out, 'abort.json'), 'w', encoding='utf-8') as f:
                json.dump({'session': session, 'reason': abort_reason, 'utc': _utc(ops.wall())}, f, indent=1)
    return code, doc


def baseline_from_session(doc):
    """Active seconds per 100 rows, per source, over a session's children (the opening slice's baseline)."""
    rows, secs = Counter(), Counter()
    for c in doc['children']:
        total = sum(c['per_source_rows'].values())
        for src, n in c['per_source_rows'].items():
            rows[src] += n
            secs[src] += c['active_s'] * n / total if total else 0
    return {src: round(100.0 * secs[src] / rows[src], 2) for src in rows if rows[src]}


def deadline_from(stop_at, ops, minutes=None, tz='America/Los_Angeles'):
    """ops.now() of the hard stop `stop_at` ('HH:MM', Pacific) today, or of `minutes` from now if sooner."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(tz)
    now_local = datetime.datetime.now(zone)
    hh, mm = (int(x) for x in stop_at.split(':'))
    stop = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    left = (stop - now_local).total_seconds()
    if minutes is not None:
        left = min(left, minutes * 60.0)
    return ops.now() + left, left


# --- extras ---------------------------------------------------------------------------------------------------------

def band_lines(luma, lines, box, max_luma=0, min_fraction=common.EDGE_BAND_LINE_MIN_BLACK):
    """common._edge_band_lines with the line minimum and the luma ceiling as parameters (D5's sensitivity).
    At (0, 0.98) it must equal the production detector; a test says so."""
    count = 0
    for line in lines:
        strip = luma.crop(box(line))
        if sum(strip.histogram()[:max_luma + 1]) / float(strip.width * strip.height) < min_fraction:
            break
        count += 1
    return count


_DEFAULT_TABLES = None


def pillow_default_tables():
    """The quantisation tables of a Pillow save at its default quality: what a stitch of ours carries."""
    global _DEFAULT_TABLES
    if _DEFAULT_TABLES is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (16, 16), (90, 90, 90)).save(buf, 'jpeg')
        buf.seek(0)
        with Image.open(buf) as im:
            _DEFAULT_TABLES = {k: list(v) for k, v in im.quantization.items()}
    return _DEFAULT_TABLES


def pano_extras(path, inmem=False):
    """The extras of one stored pano (plan 1.1): top band, bottom at line minimums 0.90 and 1.00, the file
    witness, and (when asked) the downloader's in-memory definition on the fully decoded image."""
    from PIL import Image
    dims = jpeg_dimensions(path)
    if dims is None:
        raise ValueError('not a readable JPEG')
    with Image.open(path) as image:
        tables = {k: list(v) for k, v in image.quantization.items()}
        image.draft('L', (max(1, dims[0] // common.EDGE_BAND_SCALE), max(1, dims[1] // common.EDGE_BAND_SCALE)))
        luma = image.convert('L')
    w, h = luma.size
    rows_top = range(h)
    rows_bottom = range(h - 1, -1, -1)
    row = lambda y: (0, y, w, y + 1)  # noqa: E731
    out = {
        'width': dims[0], 'height': dims[1],
        'top_band': '%.4f' % (band_lines(luma, rows_top, row) / float(h)),
        'near_top_band': '%.4f' % (band_lines(luma, rows_top, row, common.EDGE_BAND_NEAR_BLACK_LUMA) / float(h)),
        'bottom_band_090': '%.4f' % (band_lines(luma, rows_bottom, row, 0, 0.90) / float(h)),
        'bottom_band_100': '%.4f' % (band_lines(luma, rows_bottom, row, 0, 1.00) / float(h)),
        'pillow_default_tables': int(tables == pillow_default_tables()),
        'inmem_bottom': '', 'inmem_right': '',
    }
    if inmem:
        with Image.open(path) as image:
            bands = common.edge_black_bands(image.convert('RGB'))
        out['inmem_bottom'] = '%.4f' % bands.bottom
        out['inmem_right'] = '%.4f' % bands.right
    return out


EXTRAS_FIELDS = ['city', 'pano_id', 'form', 'class', 'width', 'height', 'top_band', 'near_top_band',
                 'bottom_band_090', 'bottom_band_100', 'pillow_default_tables', 'inmem_bottom', 'inmem_right']


def load_ledger_rows(path):
    """{pano_id: row} of a sweep ledger (plain or .gz), the last sound row winning, as scan_black_bands does."""
    opener = gzip.open if path.endswith('.gz') else open
    rows = {}
    with opener(path, 'rt', newline='', encoding='utf-8') as f:
        for r in csv.DictReader(f):
            try:
                for k in ('size', 'mtime_ns', 'width', 'height'):
                    int(r[k])
                for k in ('bottom_band', 'right_band', 'near_bottom_band', 'near_right_band'):
                    float(r[k])
            except (KeyError, TypeError, ValueError):
                continue
            rows[r['pano_id']] = r
    return rows


def has_band(r):
    return any(float(r[k]) > 0 for k in ('bottom_band', 'right_band', 'near_bottom_band', 'near_right_band'))


def extras(farm, out, store_root, cities, tolerance_px, deadline_epoch, device_of=None, rate_cap=RATE_CAP_BYTES):
    """Write <out>/extras.csv (appended, resumable) for every banded pano; paced under the rate cap."""
    check_outputs(store_root, [farm, out], device_of)
    path = os.path.join(out, 'extras.csv')
    done = set()
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8') as f:
            done = {(r['city'], r['pano_id']) for r in csv.DictReader(f)}
    new = not os.path.exists(path)
    started, nbytes, count = time.monotonic(), 0, 0
    with open(path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=EXTRAS_FIELDS)
        if new:
            writer.writeheader()
        for city in cities:
            ledger = os.path.join(out, 'sweep', city, 'black_bands.csv')
            if not os.path.exists(ledger):
                continue
            for pano_id, r in sorted(load_ledger_rows(ledger).items()):
                if (city, pano_id) in done or not has_band(r):
                    continue
                if time.time() >= deadline_epoch:
                    return count
                form = id_form(pano_id, int(r['mtime_ns']))
                cls = classify_row(form, r, tolerance_px)
                pano = os.path.join(farm, city, pano_id[:2], pano_id + '.jpg')
                try:
                    ex = pano_extras(pano, inmem=(form == 'photosphere' or cls == 'U'))
                except (ValueError, OSError) as e:
                    print('extras FAILED %s/%s: %s' % (city, pano_id, type(e).__name__))
                    continue
                writer.writerow(dict(ex, city=city, pano_id=pano_id, form=form, **{'class': cls}))
                f.flush()
                count += 1
                nbytes += int(r['size']) * (2 if ex['inmem_bottom'] else 1)
                ahead = nbytes / rate_cap - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(ahead)
    return count


# --- no-writes check ------------------------------------------------------------------------------------------------

def store_listing(store_root):
    """(folder, name, size, mtime_ns) of every entry of the store root and of each folder's root; shard dirs are
    counted per folder rather than listed. lstat only: nothing is opened or followed."""
    rows = []
    for name in sorted(os.listdir(store_root)):
        path = os.path.join(store_root, name)
        st = os.lstat(path)
        rows.append(('.', name, st.st_size, st.st_mtime_ns))
        if os.path.isdir(path) and not os.path.islink(path):
            shards = 0
            for entry in sorted(os.scandir(path), key=lambda e: e.name):
                if len(entry.name) == 2 and entry.is_dir(follow_symlinks=False):
                    shards += 1
                    continue
                s = entry.stat(follow_symlinks=False)
                rows.append((name, entry.name, s.st_size, s.st_mtime_ns))
            rows.append((name, '<shard-dirs>', shards, 0))
    return rows


def write_listing(path, rows, label):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# %s\n' % label)
        for r in rows:
            f.write('%s\t%s\t%d\t%d\n' % r)


def read_listing(path):
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.startswith('#') or not line.strip():
                continue
            folder, name, size, mtime = line.rstrip('\n').split('\t')
            out[(folder, name)] = (int(size), int(mtime))
    return out


def tool_creatable(name):
    return (name.startswith('black_band') or name.endswith('.part') or '.stale-' in name or name in STUDY_NAMES)


def compare_listings(before, after):
    """(failures, other changes): a failure is a tool-creatable name added, removed or changed; every other
    difference is a nightly writer's, listed but not failed."""
    failures, others = [], []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        kind = 'added' if key not in before else 'removed' if key not in after else 'changed'
        entry = '%s %s/%s %s -> %s' % (kind, key[0], key[1], before.get(key), after.get(key))
        (failures if tool_creatable(key[1]) else others).append(entry)
    return failures, others


# --- instrument check and tile tolerance ------------------------------------------------------------------------

FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'black_bands')
TILE_FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'tiles')
INSTRUMENT_FIELDS = ['fixture', 'bottom_band', 'right_band', 'near_bottom_band', 'near_right_band']


def instrument_rows(fixture_dir=FIXTURES):
    """Each committed band fixture measured by the production file path, formatted as the ledger formats."""
    rows = []
    for name in sorted(n for n in os.listdir(fixture_dir) if n.endswith('.jpg')):
        r = common.edge_band_report_from_file(os.path.join(fixture_dir, name))
        rows.append({'fixture': name, 'bottom_band': '%.4f' % r.exact.bottom, 'right_band': '%.4f' % r.exact.right,
                     'near_bottom_band': '%.4f' % r.near.bottom, 'near_right_band': '%.4f' % r.near.right})
    return rows


#: The real tile bodies the tolerance stitch is made of: three imagery tiles and Google's out-of-range black.
TOLERANCE_IMAGERY = ('z5_full_8_10.jpg', 'z5_full_9_10.jpg', 'z5_nofover_4_2.jpg')
TOLERANCE_BLANK = 'z3_blank_out_of_range.jpg'


def tile_row_tolerance(workdir, tiles_dir=TILE_FIXTURES):
    """Measure how short the detector reads a black tile row our stitcher leaves (plan 2.5, F-rows `t`).

    For each frame (16384x8192, 13312x6656), zoom 3/4/5 and k = 1, 2 missing bottom tile rows, a one-tile-wide
    strip is stitched by the production `gsv_tiles._stitch_tiles` (real tile bodies, Google's out-of-range
    black tile for the missing rows), LANCZOS-upscaled by it to the frame's height, saved as gsv.py saves a
    stitch (Pillow's default JPEG quality) and measured by `common.edge_band_report_from_file`. A strip is
    exact for this: the upscale and the JPEG blocks are separable, so a row's vertical profile does not
    depend on the frame's width. Returns {'cases': [...], 'tolerance_px': max shortfall}.
    """
    from sidewalk_panorama_tools import gsv_tiles
    imagery = []
    for name in TOLERANCE_IMAGERY:
        with open(os.path.join(tiles_dir, name), 'rb') as f:
            imagery.append(f.read())
    with open(os.path.join(tiles_dir, TOLERANCE_BLANK), 'rb') as f:
        blank = f.read()
    cases = []
    for width, height in ((16384, 8192), (13312, 6656)):
        for zoom in (3, 4, 5):
            zw, zh = gsv_tiles._dims_at_zoom(width, height, zoom)
            rows = int(math.ceil(zh / float(TILE)))
            scale = height // zh
            for k in (1, 2):
                tiles = [(0, y, blank if y >= rows - k else imagery[y % len(imagery)]) for y in range(rows)]
                image = gsv_tiles._stitch_tiles(tiles, (TILE, zh), (TILE * scale, height))
                path = os.path.join(workdir, 'strip_%d_%d_%d.jpg' % (height, zoom, k))
                image.save(path, 'jpeg')
                measured = common.edge_band_report_from_file(path).exact.bottom * height
                expected = (zh - (rows - k) * TILE) * scale
                os.remove(path)
                cases.append({'frame': '%dx%d' % (width, height), 'zoom': zoom, 'missing_rows': k,
                              'expected_px': expected, 'measured_px': round(measured, 3),
                              'shortfall_px': round(expected - measured, 3)})
    return {'cases': cases, 'tolerance_px': max(c['shortfall_px'] for c in cases)}


# --- classes ------------------------------------------------------------------------------------------------------

def tile_aligned(bottom, height, t):
    """Whether `bottom` x `height` lies in [512k - t, 512k] for a whole k >= 1, widened on both sides only by the
    ledger's rounding (+-0.5e-4 x height): the detector reads short, never long, so the window is one-sided."""
    d = bottom * height
    eps = LEDGER_ROUNDING * height + 1e-9
    k = int(math.ceil((d - eps) / TILE))
    return k >= 1 and TILE * k - t - eps <= d <= TILE * k + eps


def classify(form, bottom, right, near_bottom, near_right, height, t):
    """The class of one ledger row (plan 2.5): P, F-frame, F-rows, U, N or clean."""
    if form in P_FORMS:
        return 'P'
    if right > 0:
        return 'F-frame'
    if bottom > 0:
        return 'F-rows' if tile_aligned(bottom, height, t) else 'U'
    if near_bottom > 0 or near_right > 0:
        return 'N'
    return 'clean'


def classify_row(form, r, t):
    return classify(form, float(r['bottom_band']), float(r['right_band']), float(r['near_bottom_band']),
                    float(r['near_right_band']), int(r['height']), t)


# --- D1: rates, bounds, decision ------------------------------------------------------------------------------------

def _binom_cdf(k, n, p):
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(0, k + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(1.0, total)


def upper_bound(k, n, confidence=CONFIDENCE):
    """The exact (Clopper-Pearson) one-sided upper bound on a binomial proportion with k events in n.

    Example: zero events in 2,500 gives 1 - 0.05 ** (1 / 2500), about 12.0 per 10,000.
    """
    if n <= 0:
        return None
    if k >= n:
        return 1.0
    lo, hi = k / float(n), 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _binom_cdf(k, n, mid) > 1 - confidence:
            lo = mid
        else:
            hi = mid
    return hi


def is_over(value, limit):
    """The production comparison: common.deep_edge_bands on a bottom band (strict >)."""
    return bool(common.deep_edge_bands(common.EdgeBands(value, 0.0), limit))


def rate_at(strata, limit):
    """{estimate, bound, events} of the stratum-weighted share of gsv22 stitches with a class-U band over
    `limit`. Each stratum: {'N': population (de-duplicated), 'n': measured, 'u': [U bottom values]}. A stratum
    measured whole contributes its exact share to both; any other its exact binomial upper bound. None when
    a stratum with panos has none measured (a zero denominator is undefined, not zero)."""
    total = sum(s['N'] for s in strata)
    if total <= 0:
        return {'estimate': None, 'bound': None, 'events': 0}
    est = bound = 0.0
    events = 0
    for s in strata:
        if s['N'] <= 0:
            continue
        if s['n'] <= 0:
            return {'estimate': None, 'bound': None, 'events': None}
        k = sum(1 for v in s['u'] if is_over(v, limit))
        events += k
        w = s['N'] / float(total)
        p = k / float(s['n'])
        est += w * p
        bound += w * (p if s['n'] >= s['N'] else upper_bound(k, s['n']))
    return {'estimate': est, 'bound': bound, 'events': events}


def passes(r):
    return r['estimate'] is not None and r['estimate'] <= PASS_ESTIMATE and r['bound'] <= PASS_BOUND


def d1_decide(strata):
    """D1 on one set of strata: {'limit', 'outcome', 'rates', 'cleared'}.

    0.05 passes -> confirmed; else 0.0605 passes -> 0.0605 (provisional); else 0.05 stays, the owner's choice.
    `cleared` is every U value over 0.05 that the returned limit no longer flags.
    """
    rates = {repr(c): rate_at(strata, c) for c in CANDIDATES}
    lo, hi = CANDIDATES
    if rates[repr(lo)]['estimate'] is None:
        return {'limit': lo, 'outcome': 'undefined', 'rates': rates, 'cleared': []}
    if passes(rates[repr(lo)]):
        limit, outcome = lo, 'confirmed'
    elif passes(rates[repr(hi)]):
        limit, outcome = hi, 'raised-provisional'
    else:
        limit, outcome = lo, 'neither-passes'
    cleared = sorted(v for s in strata for v in s['u'] if is_over(v, lo) and not is_over(v, limit))
    return {'limit': limit, 'outcome': outcome, 'rates': rates, 'cleared': cleared}


def fold_groups(frows):
    """Groups of F-rows panos folded into U by the sensitivity rule: FOLD_MIN or more with one identical bottom
    depth (as ledgered), right band 0, in one (city, frame). `frows`: [(city, width, height, bottom_str)]."""
    counts = Counter(frows)
    return {key: n for key, n in counts.items() if n >= FOLD_MIN}


def d1_with_sensitivity(strata, frows_by_city):
    """D1, then D1 again with the fold applied; if the outcome differs, 0.05 stays and the choice is the owner's.
    `frows_by_city`: {city: [(width, height, bottom_str)]} of measured, de-duplicated gsv22 F-rows panos."""
    primary = d1_decide(strata)
    folded_strata = []
    groups = {}
    for s in strata:
        items = [(s['city'], w, h, b) for (w, h, b) in frows_by_city.get(s['city'], [])]
        g = fold_groups(items)
        groups.update(g)
        extra = [float(key[3]) for key, n in g.items() for _ in range(n)]
        folded_strata.append(dict(s, u=list(s['u']) + extra))
    folded = d1_decide(folded_strata)
    if (folded['limit'], folded['outcome']) != (primary['limit'], primary['outcome']):
        limit, outcome = CANDIDATES[0], 'fold-flips'
    else:
        limit, outcome = primary['limit'], primary['outcome']
    return {'limit': limit, 'outcome': outcome, 'primary': primary, 'folded': folded,
            'fold_groups': [{'city': k[0], 'frame': '%dx%d' % (k[1], k[2]), 'bottom_band': k[3], 'n': n}
                            for k, n in sorted(groups.items())]}


def d1_what_would_fail(strata):
    """E5: from the planned strata (n = frozen), the zero-event bound and, for the heaviest and the median
    sampled stratum, the fewest U events over 0.05 that fail the rule (all other strata at zero)."""
    total = float(sum(s['N'] for s in strata if s['N'] > 0))
    sampled = [s for s in strata if s['N'] > 0 and s['n'] < s['N']]
    zero = sum((s['N'] / total) * upper_bound(0, s['n']) for s in sampled)

    def fewest(s):
        w = s['N'] / total
        for k in range(1, s['n'] + 1):
            est = w * k / s['n']
            bound = zero - w * upper_bound(0, s['n']) + w * upper_bound(k, s['n'])
            if est > PASS_ESTIMATE or bound > PASS_BOUND:
                return k
        return None
    out = {'zero_event_bound': zero, 'heaviest': None, 'median': None}
    if sampled:
        heavy = max(sampled, key=lambda s: (s['N'], s['city']))
        ordered = sorted(sampled, key=lambda s: (s['N'], s['city']))
        med = ordered[(len(ordered) - 1) // 2]
        for key, s in (('heaviest', heavy), ('median', med)):
            out[key] = {'city': s['city'], 'weight': s['N'] / total, 'n': s['n'], 'events_that_fail': fewest(s)}
    return out


def planned_strata(frame_doc):
    """The strata as planned (n = frozen gsv22 ids not in an earlier roster folder), for E5."""
    strata = []
    for city, c in frame_doc['cities'].items():
        if not c['in_roster'] or not c['gsv22_dedup']:
            continue
        frozen_gsv = c['frozen_forms'].get('gsv22', 0) - len(c['frozen_dup_of_earlier'])
        strata.append({'city': city, 'N': c['gsv22_dedup'], 'n': frozen_gsv, 'u': []})
    return strata


# --- crosscheck -----------------------------------------------------------------------------------------------------

def crosscheck(targets, fetch, sleep, now, spacing=10.0, max_requests=12):
    """Ask each city server for its pano list, strictly one at a time and at least `spacing` s apart; one attempt
    each. The step ends at the first timeout or connection error (no retry), and at a 401/403.

    `targets`: [(city, url)]. `fetch(url)` -> (status, list of records) or raises OSError/TimeoutError.
    Returns (rows, stop_reason): rows are (city, form, source, count) plus one ('<city>', 'status', ...) row each.
    """
    rows, last = [], None
    for city, url in targets[:max_requests]:
        if last is not None:
            wait = spacing - (now() - last)
            if wait > 0:
                sleep(wait)
        last = now()
        try:
            status, records = fetch(url)
        except (OSError, TimeoutError) as e:
            rows.append((city, 'status', type(e).__name__, 0))
            return rows, 'stopped at the first timeout or connection error (%s)' % city
        rows.append((city, 'status', str(status), 0))
        if status in (401, 403):
            return rows, 'refused (%s) at %s; step skipped' % (status, city)
        if status != 200:
            return rows, 'HTTP %s at %s; step ended' % (status, city)
        counts = Counter((id_form(str(r.get('pano_id', ''))), str(r.get('source'))) for r in records)
        for (form, source), n in sorted(counts.items()):
            rows.append((city, form, source, n))
    return rows, None


# --- analyze ----------------------------------------------------------------------------------------------------------

def load_data(data_dir):
    with open(os.path.join(data_dir, 'frame.json'), encoding='utf-8') as f:
        frame_doc = json.load(f)
    ids = {c: read_ids(os.path.join(data_dir, 'ids', c + '.txt.gz')) for c in frame_doc['cities']}
    ledgers = {}
    for c in frame_doc['cities']:
        for name in ('black_bands.csv.gz', 'black_bands.csv'):
            p = os.path.join(data_dir, 'sweep', c, name)
            if os.path.exists(p):
                ledgers[c] = load_ledger_rows(p)
                break
    extras_rows = {}
    for name in ('extras.csv.gz', 'extras.csv'):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            opener = gzip.open if name.endswith('.gz') else open
            with opener(p, 'rt', newline='', encoding='utf-8') as f:
                extras_rows = {(r['city'], r['pano_id']): r for r in csv.DictReader(f)}
            break
    with open(os.path.join(data_dir, 'tile-tolerance.json'), encoding='utf-8') as f:
        tolerance = json.load(f)
    return frame_doc, ids, ledgers, extras_rows, tolerance


def prose_figures(data_dir, frame_doc, measured, extras_rows, strata, absorbed):
    """The numbers the report quotes in its prose, each derived here from the committed raw files, formatted as the
    report prints them (the desk-study rule: a prose number is transcribed from an artifact and a test says so)."""
    def n(x):
        return '{:,}'.format(x)
    out = {}
    sessions = {}
    for name in sorted(os.listdir(os.path.join(data_dir, 'sessions'))):
        with open(os.path.join(data_dir, 'sessions', name), encoding='utf-8') as f:
            sessions[name[:-len('.json')]] = json.load(f)
    for name, s in sessions.items():
        rows = sum(c['rows'] for c in s['children'])
        gb = sum(c['bytes'] for c in s['children']) / 1e9
        out['%s.panos' % name] = n(rows)
        out['%s.gb' % name] = '{:,.1f}'.format(gb)
        start = datetime.datetime.strptime(s['start_utc'], '%Y-%m-%dT%H:%M:%SZ')
        end = datetime.datetime.strptime(s['end_utc'], '%Y-%m-%dT%H:%M:%SZ')
        if name == 'main1':
            out['main1.mb_per_s'] = '%.1f' % (gb * 1e3 / (end - start).total_seconds())
            for c in s['children']:
                if c['exit'] != 0:
                    out['main1.reached.%s' % c['city']] = n(c['rows'])
    # Review 2, B2: the remaining session figures the report quotes.
    def gsv_children(s, min_rows=1):
        return [c for c in s['children'] if c['rows'] >= min_rows
                and c['per_source_rows'].get('gsv', 0) * 2 > sum(c['per_source_rows'].values())]
    baseline_path = os.path.join(data_dir, 'baseline.json')
    if os.path.exists(baseline_path):
        with open(baseline_path, encoding='utf-8') as f:
            for source, v in json.load(f).items():
                out['baseline.%s' % source] = '%.2f' % v
    if 'opening' in sessions:
        rates = [c['bytes'] / 1e6 / (c['active_s'] + c['paused_s']) for c in gsv_children(sessions['opening'], 101)]
        if rates:
            out['opening.gsv_mb_per_s.lo'], out['opening.gsv_mb_per_s.hi'] = '%.1f' % min(rates), '%.1f' % max(rates)
    if 'main1' in sessions:
        kids = gsv_children(sessions['main1'])
        paused = [c['paused_s'] for c in kids]
        per100 = [100.0 * c['active_s'] / c['rows'] for c in kids]
        out['main1.gsv_paused_s.lo'], out['main1.gsv_paused_s.hi'] = '%.1f' % min(paused), '%.1f' % max(paused)
        out['main1.gsv_per100_s.lo'], out['main1.gsv_per100_s.hi'] = '%.1f' % min(per100), '%.1f' % max(per100)
        before = [s for name, s in sessions.items() if name in ('opening', 'main1')]
        out['after_main1.measured'] = n(sum(c['rows'] for s in before for c in s['children']))
        out['after_main1.folders_complete'] = str(len(frame_doc['cities']) - sum(
            1 for c in sessions['main1']['children'] if c['exit'] not in (0, 1)))
        out['frozen.total'] = n(sum(c['frozen'] for c in frame_doc['cities'].values()))
    if 'main2' in sessions:
        for c in sessions['main2']['children']:
            out['main2.per100_s.%s' % c['city']] = '%.1f' % (100.0 * c['active_s'] / c['rows'])
    abort_path = os.path.join(data_dir, 'abort-main1.json')
    if os.path.exists(abort_path):
        with open(abort_path, encoding='utf-8') as f:
            m = re.search(r'took ([0-9.]+) s', json.load(f)['reason'])
        out['main1.abort_last100_s'] = m.group(1)
    gsv = [(c, p, cls, r) for c, p, form, cls, r in measured if form == 'gsv22']
    out['gsv22.measured'] = n(len(gsv))
    exact = [r['bottom_band'] for c, p, cls, r in gsv if float(r['bottom_band']) > 0 or float(r['right_band']) > 0]
    out['gsv22.with_exact_band'] = n(len(exact))
    out['gsv22.shallowest_exact_bottom'] = min(b for b in exact if float(b) > 0)
    lo = repr(CANDIDATES[0])
    out['absorbed.primary_bound_per_10000'] = '%.2f' % (1e4 * absorbed['primary']['rates'][lo]['bound'])
    out['absorbed.folded_events'] = str(absorbed['folded']['rates'][lo]['events'])
    out['absorbed.folded_estimate_per_10000'] = '%.2f' % (1e4 * absorbed['folded']['rates'][lo]['estimate'])
    for city, k in sorted(Counter(c for c, p, cls, r in gsv if cls == 'F-frame').items()):
        out['F-frame.%s' % city] = str(k)
    for (city, frame, depth), k in sorted(Counter((c, '%sx%s' % (r['width'], r['height']), r['bottom_band'])
                                                  for c, p, cls, r in gsv if cls == 'U').items()):
        out['U.%s.%s.%s' % (city, frame, depth)] = str(k)
    total = float(sum(s['N'] for s in strata))
    for s in strata:
        if s['u']:
            out['weight.%s' % s['city']] = '%.4f' % (s['N'] / total)
    for depth, k in sorted(Counter(r['near_bottom_band'] for c, p, form, cls, r in measured
                                   if float(r['near_bottom_band']) > CANDIDATES[0]
                                   and not is_over(float(r['bottom_band']), CANDIDATES[0])).items()):
        out['D2.near.%s' % depth] = n(k)
    out['extras.panos'] = n(len(extras_rows))
    by_id = {(c, p): r for c, p, form, cls, r in measured}
    nbytes = sum(int(by_id[key]['size']) * (2 if ex['inmem_bottom'] != '' else 1)
                 for key, ex in extras_rows.items() if key in by_id)
    out['extras.gb'] = '{:,.1f}'.format(nbytes / 1e9)
    return out


def _pct(k, n):
    return None if not n else 100.0 * k / n


def analyze(data_dir):
    """Every table the report prints, the decision, and the populations each figure belongs to."""
    frame_doc, ids, ledgers, extras_rows, tolerance = load_data(data_dir)
    t = tolerance['tolerance_px']
    cities = frame_doc['cities']
    reached = {}
    classes = defaultdict(Counter)            # form -> class -> n  (all measured rows)
    near_over = defaultdict(Counter)          # D2: class -> n with near over 0.05 and exact not
    measured = []                             # (city, pano_id, form, class, row)
    for city in sorted(cities):
        frozen = set(ids[city])
        rows = {k: v for k, v in ledgers.get(city, {}).items() if k in frozen}
        reached[city] = len(rows)
        for pano_id, r in sorted(rows.items()):
            form = id_form(pano_id, int(r['mtime_ns']))
            cls = classify_row(form, r, t)
            classes[form][cls] += 1
            exact_over = bool(common.deep_edge_bands(common.EdgeBands(float(r['bottom_band']), float(r['right_band']))))
            near_over_ = bool(common.deep_edge_bands(common.EdgeBands(float(r['near_bottom_band']),
                                                                      float(r['near_right_band']))))
            if near_over_ and not exact_over:
                near_over[cls][form] += 1
            measured.append((city, pano_id, form, cls, r))

    # D1 strata: measured, de-duplicated gsv22 panos of roster folders
    strata, frows_by_city, u_rows_by_city = [], defaultdict(list), defaultdict(list)
    for city, c in cities.items():
        if not c['in_roster'] or not c['gsv22_dedup']:
            continue
        dup = set(c['frozen_dup_of_earlier'])
        mine = [(p, cls, r) for (cc, p, form, cls, r) in measured if cc == city and form == 'gsv22' and p not in dup]
        strata.append({'city': city, 'N': c['gsv22_dedup'], 'n': len(mine),
                       'u': [float(r['bottom_band']) for p, cls, r in mine if cls == 'U']})
        frows_by_city[city] = [(int(r['width']), int(r['height']), r['bottom_band'])
                               for p, cls, r in mine if cls == 'F-rows']
        u_rows_by_city[city] = [(int(r['width']), int(r['height']), r['bottom_band'])
                                for p, cls, r in mine if cls == 'U']
    decision = d1_with_sensitivity(strata, frows_by_city)
    # Review 1, S1: what the committed rules give if every class-U pano were read as F-rows instead. Not a rule:
    # the report quotes it to show the pre-committed fold would still keep 0.05.
    absorbed = d1_with_sensitivity([dict(s, u=[]) for s in strata],
                                   {c: frows_by_city[c] + u_rows_by_city[c] for c in frows_by_city})
    would_fail = d1_what_would_fail(planned_strata(frame_doc))

    tables = {}
    tables['reach'] = ['| %s | %s | %s | %d | %d |' % (c, 'fleet' if cities[c]['in_roster'] else 'other',
                                                         cities[c]['design'], cities[c]['frozen'], reached[c])
                       for c in sorted(cities)]
    tables['classes'] = ['| %s | %s |' % (form, ' | '.join(str(classes[form][k]) for k in
                                                         ('P', 'F-frame', 'F-rows', 'U', 'N', 'clean')))
                         for form in FORMS if classes[form]]
    # D3: depth distribution per form and per (city, frame) for provider forms
    d3 = defaultdict(Counter)
    for city, p, form, cls, r in measured:
        if form in P_FORMS:
            d3[(city, '%sx%s' % (r['width'], r['height']), form)][r['bottom_band']] += 1
    tables['d3'] = []
    for key in sorted(d3):
        n = sum(d3[key].values())
        mode, mode_n = sorted(d3[key].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        over = sum(v for b, v in d3[key].items() if is_over(float(b), CANDIDATES[0]))
        tables['d3'].append('| %s | %s | %s | %d | %s | %d | %d |' % (key[0], key[1], key[2], n, mode, mode_n, over))
    # D4: photospheres, sweep vs in-memory
    ph = [(city, p, r) for city, p, form, cls, r in measured if form == 'photosphere']
    ph_sweep = sum(1 for city, p, r in ph if common.deep_edge_bands(
        common.EdgeBands(float(r['bottom_band']), float(r['right_band']))))
    ph_inmem = ph_agree = ph_measured = ph_inferred = 0
    for city, p, r in ph:
        ex = extras_rows.get((city, p))
        sweep_over = bool(common.deep_edge_bands(common.EdgeBands(float(r['bottom_band']), float(r['right_band']))))
        if ex and ex['inmem_bottom'] != '':
            mem_over = bool(common.deep_edge_bands(common.EdgeBands(float(ex['inmem_bottom']), float(ex['inmem_right']))))
            ph_measured += 1
        elif not has_band(r):
            # No exact or near band in the file: the in-memory reading is NOT computed, and is inferred "not over"
            # (a line that is not near-black at 1/8 scale cannot be exact black after a box mean).
            mem_over = False
            ph_inferred += 1
        else:
            continue
        ph_inmem += mem_over
        ph_agree += mem_over == sweep_over
    tables['d4'] = ['| %d | %d | %d | %d | %d | %d |' % (len(ph), ph_sweep, ph_measured, ph_inferred, ph_inmem,
                                                          ph_agree)]
    # D5: verdict changes at line minimums 0.90 and 1.00
    d5 = Counter()
    for city, p, form, cls, r in measured:
        ex = extras_rows.get((city, p))
        if not ex:
            continue
        base = is_over(float(r['bottom_band']), CANDIDATES[0])
        for col in ('bottom_band_090', 'bottom_band_100'):
            if is_over(float(ex[col]), CANDIDATES[0]) != base:
                d5[col] += 1
    tables['d5'] = ['| 0.90 | %d |' % d5['bottom_band_090'], '| 1.00 | %d |' % d5['bottom_band_100']]
    # F-rows by depth, frame, city, mtime year
    fr = Counter()
    for city, p, form, cls, r in measured:
        if cls == 'F-rows':
            year = datetime.datetime.fromtimestamp(int(r['mtime_ns']) / 1e9, datetime.timezone.utc).year
            fr[(r['bottom_band'], '%sx%s' % (r['width'], r['height']), city, year)] += 1
    tables['frows'] = ['| %s | %s | %s | %d | %d |' % (k + (n,)) for k, n in sorted(fr.items())]
    # id form x file witness, banded panos
    wit = Counter()
    for (city, p), ex in extras_rows.items():
        wit[(ex['form'], ex['pillow_default_tables'])] += 1
    tables['witness'] = ['| %s | %s | %d |' % (f, 'yes' if w == '1' else 'no', n) for (f, w), n in sorted(wit.items())]
    tables['d2'] = ['| %s | %s | %d |' % (cls, form, n) for cls in sorted(near_over)
                    for form, n in sorted(near_over[cls].items())]
    unresolved = [(city, p, r['width'], r['height'], r['bottom_band'], r['near_bottom_band'])
                  for city, p, form, cls, r in measured if cls == 'U']

    def rate_row(label, d):
        return ['| %s | %s | %s | %s | %s |' % (label, repr(c), d['rates'][repr(c)]['events'],
                                                fmt(None if d['rates'][repr(c)]['estimate'] is None
                                                    else 1e4 * d['rates'][repr(c)]['estimate'], '.2f'),
                                                fmt(None if d['rates'][repr(c)]['bound'] is None
                                                    else 1e4 * d['rates'][repr(c)]['bound'], '.2f'))
                for c in CANDIDATES]
    tables['d1'] = rate_row('primary', decision['primary']) + rate_row('folded', decision['folded'])
    doc = {
        'tolerance_px': t,
        'reached': reached,
        'frozen': {c: cities[c]['frozen'] for c in cities},
        'classes': {f: dict(classes[f]) for f in classes},
        'decision': {'limit': decision['limit'], 'outcome': decision['outcome'],
                     'fold_groups': decision['fold_groups'],
                     'primary': {k: {kk: (num(vv) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                 for k, v in decision['primary']['rates'].items()},
                     'folded': {k: {kk: (num(vv) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                for k, v in decision['folded']['rates'].items()}},
        'u_read_as_f_rows': {'limit': absorbed['limit'], 'outcome': absorbed['outcome'],
                             'fold_groups': absorbed['fold_groups'],
                             'primary': {k: {kk: (num(vv) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                         for k, v in absorbed['primary']['rates'].items()},
                             'folded': {k: {kk: (num(vv) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                        for k, v in absorbed['folded']['rates'].items()}},
        'what_would_fail': {'zero_event_bound': num(would_fail['zero_event_bound']),
                            'heaviest': would_fail['heaviest'], 'median': would_fail['median']},
        'photospheres': {'n': len(ph), 'sweep_over': ph_sweep, 'inmem_measured': ph_measured,
                         'inmem_inferred': ph_inferred, 'inmem_over': ph_inmem, 'agree': ph_agree},
        'prose': prose_figures(data_dir, frame_doc, measured, extras_rows, strata, absorbed),
        'unresolved': len(unresolved),
        'tables': tables,
        'populations': {
            'census-and-sample-as-measured': ['reached', 'classes', 'tables.classes', 'tables.d2', 'tables.reach'],
            'gsv22-fleet-strata-deduplicated': ['decision', 'tables.d1', 'u_read_as_f_rows'],
            'gsv22-fleet-strata-as-planned': ['what_would_fail'],
            'provider-forms-census': ['tables.d3'],
            'photosphere-census': ['photospheres', 'tables.d4'],
            'banded-panos-with-extras': ['tables.d5', 'tables.witness'],
            'class-F-rows-as-measured': ['tables.frows'],
            'class-U-as-measured': ['unresolved'],
            'instrument': ['tolerance_px', 'frozen'],
            'sessions-and-prose-as-measured': ['prose'],
        },
    }
    return doc, unresolved


# --- CLI --------------------------------------------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('frame')
    f.add_argument('store_root')
    f.add_argument('--farm', required=True)
    f.add_argument('--out', required=True)
    f.add_argument('--roster', default=os.path.join(REPO_ROOT, 'log_analyzer', 'cities.csv'))
    r = sub.add_parser('run')
    r.add_argument('--farm', required=True)
    r.add_argument('--out', required=True)
    r.add_argument('--store-root', required=True)
    r.add_argument('--readers', type=int, default=1)
    r.add_argument('--stop-at', default='17:00', help='hard stop, HH:MM Pacific')
    r.add_argument('--minutes', type=float, default=None, help='end sooner (the opening slice)')
    r.add_argument('--baseline', default=None, help='JSON {source: seconds per 100 panos}')
    r.add_argument('--cities', default=None, help='comma-separated subset, in run order')
    r.add_argument('--session', default=None)
    e = sub.add_parser('extras')
    e.add_argument('--farm', required=True)
    e.add_argument('--out', required=True)
    e.add_argument('--store-root', required=True)
    e.add_argument('--tolerance', required=True, help='tile-tolerance.json')
    e.add_argument('--stop-at', default='17:00')
    n = sub.add_parser('nowrites')
    n.add_argument('--store-root')
    n.add_argument('--write')
    n.add_argument('--label', default='')
    n.add_argument('--compare', nargs=2, metavar=('BEFORE', 'AFTER'))
    i = sub.add_parser('instrument')
    i.add_argument('--write', required=True)
    t = sub.add_parser('tolerance')
    t.add_argument('--write', required=True)
    c = sub.add_parser('crosscheck')
    c.add_argument('--targets', required=True, help='CSV city,url')
    c.add_argument('--write', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('data_dir')
    a.add_argument('--write', required=True)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    common.raise_decompression_bomb_ceiling()
    try:
        if args.cmd == 'frame':
            doc = frame(args.store_root, args.farm, args.out, roster=load_roster(args.roster))
            print('framed %d folders, %d frozen ids' % (len(doc['cities']),
                                                        sum(c['frozen'] for c in doc['cities'].values())))
        elif args.cmd == 'run':
            with open(os.path.join(args.out, 'frame.json'), encoding='utf-8') as f:
                frame_doc = json.load(f)
            cities = run_order(frame_doc) if not args.cities else args.cities.split(',')
            ops = RealOps()
            deadline, left = deadline_from(args.stop_at, ops, args.minutes)
            if left <= 60:
                print('less than a minute before the stop; nothing started')
                return EXIT_REFUSED
            baseline = None
            if args.baseline:
                with open(args.baseline, encoding='utf-8') as f:
                    baseline = json.load(f)
            code, doc = run(args.farm, args.out, args.store_root, cities, deadline, readers=args.readers,
                            baseline=baseline, ops=ops, session=args.session)
            print('session %s: stop %s, abort %s, %d rows, %.1f GB' % (
                doc['session'], doc['stop_reason'], doc['abort_reason'], sum(c['rows'] for c in doc['children']),
                sum(c['bytes'] for c in doc['children']) / 1e9))
            print('baseline per 100 panos (s): %s' % json.dumps(baseline_from_session(doc), sort_keys=True))
            return code
        elif args.cmd == 'extras':
            with open(os.path.join(args.out, 'frame.json'), encoding='utf-8') as f:
                frame_doc = json.load(f)
            with open(args.tolerance, encoding='utf-8') as f:
                tol = json.load(f)['tolerance_px']
            deadline, _left = deadline_from(args.stop_at, RealOps())
            n = extras(args.farm, args.out, args.store_root, run_order(frame_doc), tol,
                       time.time() + (deadline - time.monotonic()))
            print('extras: %d panos' % n)
        elif args.cmd == 'nowrites':
            if args.compare:
                failures, others = compare_listings(read_listing(args.compare[0]), read_listing(args.compare[1]))
                for line in failures:
                    print('FAIL ' + line)
                for line in others:
                    print('nightly-writer ' + line)
                print('no-writes check: %s (%d tool-creatable change(s), %d other)' % (
                    'FAILED' if failures else 'passed', len(failures), len(others)))
                return 1 if failures else 0
            write_listing(args.write, store_listing(args.store_root), args.label)
        elif args.cmd == 'instrument':
            with open(args.write, 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=INSTRUMENT_FIELDS)
                w.writeheader()
                w.writerows(instrument_rows())
        elif args.cmd == 'tolerance':
            with tempfile.TemporaryDirectory() as d:
                doc = tile_row_tolerance(d)
            from PIL import __version__ as pil
            doc['generator'] = 'reports/scripts/edge_band_calibration.py tolerance'
            doc['pillow'] = pil
            with open(args.write, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=1, sort_keys=True, allow_nan=False)
                f.write('\n')
            print('tile-row tolerance t = %s px' % fmt(doc['tolerance_px']))
        elif args.cmd == 'crosscheck':
            import urllib.request
            with open(args.targets, newline='', encoding='utf-8') as f:
                targets = [(r['city'], r['url']) for r in csv.DictReader(f)]

            def fetch(url):
                with urllib.request.urlopen(url, timeout=30) as resp:
                    return resp.status, json.loads(resp.read().decode('utf-8'))
            rows, stop = crosscheck(targets, fetch, time.sleep, time.monotonic)
            with open(args.write, 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['city', 'form', 'source', 'count'])
                w.writerows(rows)
            print('crosscheck: %d row(s); %s' % (len(rows), stop or 'ran for every target'))
        elif args.cmd == 'analyze':
            doc, unresolved = analyze(args.data_dir)
            with open(args.write, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=1, sort_keys=True, allow_nan=False)
                f.write('\n')
            with open(os.path.join(args.data_dir, 'unresolved.csv'), 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f)
                w.writerow(['city', 'pano_id', 'width', 'height', 'bottom_band', 'near_bottom_band'])
                w.writerows(unresolved)
            print('D1: limit %s (%s)' % (doc['decision']['limit'], doc['decision']['outcome']))
    except (GuardError, RefusedError) as e:
        print('REFUSED: %s' % e)
        return EXIT_REFUSED
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
