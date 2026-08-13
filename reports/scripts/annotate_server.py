"""Local annotation server for the crop-priors gold standard (prereg §4).

Serves one tile at a time to a browser, records each annotation to disk the moment it is submitted, and
resumes where it left off. Stdlib only — no framework, and nothing leaves the machine.

The design is shaped by one hazard: this process is the only thing standing between an annotator and the
answer. So it serves an explicit allowlist and never a directory:

* `tasks.json` is the annotator-facing file `annotation_tiles.build_tasks` produced, which carries no
  stored coordinate, no jitter, no tile origin and no seed.
* `geometry.json` — which carries all four — sits in the same directory and is **refused by name** as
  well as by not being routed, because the natural way to write this server is a static file handler
  over the tile directory, and that would publish the answer key at a guessable URL.
* Tile names are resolved through the task list rather than from the URL, so no path can be traversed.

Annotations are written per label, atomically, under `<out>/<annotator>/<label_uid>.json`. Per-label
rather than one accumulating file so that a crash costs the label in progress and nothing else, and so
two annotators (§4 requires Jon's independent 50 through the same tooling) can never collide.

Usage:
    python annotate_server.py --tasks-dir .cache/annotation/gsv --annotator jon \\
        --out-dir .cache/annotation/annotations
    # then open http://127.0.0.1:8000
"""

import argparse
import functools
import http.server
import json
import os
import re
import socketserver
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import annotation_tiles  # noqa: E402

TASKS_FILE = 'tasks.json'

# Refused by name, not merely unrouted. It lives beside tasks.json and holds the stored coordinates and
# the jitter seed; a static handler over the tasks directory would serve it at a guessable URL and the
# leak would be invisible in the annotations.
FORBIDDEN_FILES = frozenset({'geometry.json'})

# An annotator name becomes a directory, so it is constrained rather than trusted.
ANNOTATOR_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')

PAGE = 'annotate.html'


def load_tasks(tasks_dir):
    """The annotator-facing task list, with a blindness check on the way through.

    Re-verified here rather than assumed from the writer: the two files travel together, and a future
    change to `build_tasks` that added a convenient `pano_x` to the task record would otherwise anchor
    every annotation with nothing failing.
    """
    with open(os.path.join(tasks_dir, TASKS_FILE), encoding='utf-8') as f:
        tasks = json.load(f)
    leaked = sorted({k for t in tasks['tasks'] for k in t} & {
        'pano_x', 'pano_y', 'left', 'top', 'jitter_x', 'jitter_y', 'seed'})
    if leaked or 'seed' in tasks:
        raise ValueError(f'{TASKS_FILE} leaks {leaked or ["seed"]} to the annotator; §4 requires the '
                         f'stored point never be available, and these recover it')
    return tasks


def tasks_payload(tasks, annotator, done):
    """What `GET /api/tasks` returns: the **whole** task list, which of them are already annotated, and
    the constants the page needs.

    A function rather than a dict literal inside the handler, because the handler is the one part of
    this module the socket-free tests could not reach — and the first version of it silently dropped
    `initial_view_fraction`, which made the page open at the full 60 deg cut instead of the 20 deg view
    the protocol specifies. Nothing failed; the framing was just wrong. A smoke test caught it, and now
    a unit test does.

    It used to return only the *pending* tasks, which made the queue a one-way conveyor: a label you had
    already submitted was gone from the page's list, so there was nothing to step back to and a
    misplaced point could only be fixed by editing JSON by hand. Shipping the full list plus `done` is
    what makes back-navigation possible, and it costs no blindness — every task record is already the
    annotator-facing one (`load_tasks` asserts that), and `done` is a list of the annotator's own work.

    `initial_view_fraction` is safe to ship and `n_total`/`n_done` are progress: none of them vary per
    label, so none says anything about where a stored point is.
    """
    return {
        'annotator': annotator,
        'flags': tasks['flags'],
        # From code, not from the file: the help text is protocol wording, and a task file cut before a
        # flag existed carries no help for it. Sending a flag with no explanation is worse than the
        # explanation being slightly newer than the tiles.
        'flag_help': dict(annotation_tiles.FLAG_HELP),
        'box_rule': annotation_tiles.BOX_RULE,
        'initial_view_fraction': tasks.get('initial_view_fraction', 1.0),
        # Falls back to the module constant for task files cut before this was written, so an existing
        # tile directory keeps working rather than labelling its framing control off a missing value.
        'cut_fov_deg': tasks.get('cut_fov_deg', annotation_tiles.CUT_FOV_DEG),
        'n_total': len(tasks['tasks']),
        'n_done': len(done),
        'done': sorted(done),
        'tasks': list(tasks['tasks']),
    }


def annotator_dir(out_dir, annotator):
    if not ANNOTATOR_RE.match(annotator or ''):
        raise ValueError(f'annotator name {annotator!r} must match {ANNOTATOR_RE.pattern}')
    return os.path.join(out_dir, annotator)


def completed(out_dir, annotator):
    """label_uids this annotator has already submitted — what makes the queue resumable."""
    path = annotator_dir(out_dir, annotator)
    if not os.path.isdir(path):
        return set()
    return {os.path.splitext(f)[0].replace('_', ':', 1)
            for f in os.listdir(path) if f.endswith('.json')}


def annotation_path(out_dir, annotator, label_uid):
    return os.path.join(annotator_dir(out_dir, annotator),
                        str(label_uid).replace(':', '_') + '.json')


def validate_annotation(payload, task):
    """What a submittable annotation looks like. Returns the record to store, or raises ValueError.

    A point and a box are required unless `object-absent` is flagged: `ambiguous` and `occluded` still
    take a best-effort placement, because a flagged-but-placed label is a measurement with a caveat
    while a flagged-and-blank one is a hole, and §4 uses the flags to keep edge cases out of the
    placement distribution rather than to opt out of the task.
    """
    flags = list(payload.get('flags') or [])
    unknown = sorted(set(flags) - set(annotation_tiles.FLAGS))
    if unknown:
        raise ValueError(f'unknown flag(s) {unknown}; expected {list(annotation_tiles.FLAGS)}')

    absent = 'object-absent' in flags
    point, box = payload.get('point'), payload.get('box')
    if not absent:
        if not point:
            raise ValueError('a canonical point is required unless object-absent is flagged')
        if not box:
            raise ValueError('a bounding box is required unless object-absent is flagged')

    def in_tile(x, y):
        return 0 <= float(x) <= task['tile_width'] and 0 <= float(y) <= task['tile_height']

    if point and not in_tile(point['x'], point['y']):
        raise ValueError('the point is outside the tile')
    if box:
        if float(box['w']) <= 0 or float(box['h']) <= 0:
            raise ValueError('the box has no area')
        if not (in_tile(box['x'], box['y'])
                and in_tile(float(box['x']) + float(box['w']), float(box['y']) + float(box['h']))):
            raise ValueError('the box is outside the tile')

    return {
        'label_uid': task['label_uid'],
        'label_type': task['label_type'],
        'tile_width': task['tile_width'],
        'tile_height': task['tile_height'],
        # Tile coordinates, deliberately. The conversion to pano coordinates needs the private
        # geometry, so an annotation file on its own says nothing about where the stored point was --
        # which is also what lets these be reviewed without breaking blindness for a later re-run.
        'point': None if not point else {'x': float(point['x']), 'y': float(point['y'])},
        'box': None if not box else {'x': float(box['x']), 'y': float(box['y']),
                                     'w': float(box['w']), 'h': float(box['h'])},
        'flags': flags,
        'notes': str(payload.get('notes') or '')[:2000],
        'elapsed_ms': int(payload.get('elapsed_ms') or 0),
        'zoom_used': float(payload.get('zoom_used') or 1.0),
        # The angular width the tile was actually FRAMED at when this annotation was made. The protocol
        # opens every tile at 20 deg, but the annotator can change it, and the framing is not cosmetic:
        # §4 picked 20 deg partly so the window would not imply a crop size, since Study 2 is choosing
        # between sizing rules and an annotator shown a tight window draws boxes calibrated to it. If
        # the setting is used, its effect on box size has to be measurable rather than assumed absent —
        # so it is recorded per annotation instead of being a preference the artifact never sees.
        'initial_view_deg': float(payload.get('initial_view_deg') or 0.0) or None,
    }


def load_annotation(out_dir, annotator, label_uid):
    """A previously submitted annotation, or None. What lets the page step back and edit one."""
    path = annotation_path(out_dir, annotator, label_uid)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def record_annotation(out_dir, annotator, record):
    """Write one annotation atomically, preserving any version it replaces.

    Re-submitting used to overwrite outright. That is the wrong default now that the page can navigate
    backwards: a gold standard where an annotation may be silently revised after the annotator has seen
    fifty more tiles is one where "what did they think at the time" is unrecoverable, and revision is
    exactly the behaviour back-navigation invites. So a resubmission keeps the record it displaces in
    `superseded` and bumps `revision`.

    Flattened rather than nested — each entry in `superseded` has its own `superseded` stripped — so the
    file cannot grow quadratically under repeated edits, and reading the history is a list scan.
    """
    path = annotation_path(out_dir, annotator, record['label_uid'])
    prior = load_annotation(out_dir, annotator, record['label_uid'])
    if prior is not None:
        history = list(prior.pop('superseded', []))
        history.append(prior)
        record = dict(record, revision=int(prior.get('revision', 0)) + 1, superseded=history)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.part'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(record, f, indent=1, allow_nan=False)
    os.replace(tmp, path)
    return path


def resolve_tile(tasks, tasks_dir, name):
    """Map a requested tile name to a path, via the task list only.

    The task list is the allowlist: a name it does not contain has no path, so `..%2f` and friends have
    nothing to traverse and `geometry.json` is not a tile.
    """
    if name in FORBIDDEN_FILES:
        return None
    allowed = {t['tile'] for t in tasks['tasks']}
    if name not in allowed:
        return None
    path = os.path.join(tasks_dir, name)
    return path if os.path.isfile(path) else None


class Handler(http.server.BaseHTTPRequestHandler):
    """Thin HTTP shell. Every decision it makes lives in a function above, so the logic is tested
    without a socket and this class stays small enough to read in one go."""

    server_version = 'annotate/1.0'

    def _send(self, code, body, content_type='application/json'):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):        # one line per submit is enough; the rest is noise
        if 'POST' in (args[0] if args else ''):
            super().log_message(fmt, *args)

    def do_GET(self):
        cfg = self.server.cfg
        if self.path in ('/', '/index.html'):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), PAGE), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if self.path == '/api/tasks':
            done = completed(cfg['out_dir'], cfg['annotator'])
            return self._send(200, tasks_payload(cfg['tasks'], cfg['annotator'], done))
        if self.path.startswith('/api/annotation/'):
            uid = urllib.parse.unquote(self.path[len('/api/annotation/'):])
            # Resolved through the task list, exactly like a tile: `annotation_path` builds a filename
            # from the uid, so an unchecked one is a path to anywhere.
            if uid not in {t['label_uid'] for t in cfg['tasks']['tasks']}:
                return self._send(404, {'error': 'no such label'})
            saved = load_annotation(cfg['out_dir'], cfg['annotator'], uid)
            return self._send(200, saved) if saved else self._send(404, {'error': 'not annotated'})
        if self.path.startswith('/tiles/'):
            path = resolve_tile(cfg['tasks'], cfg['tasks_dir'], self.path[len('/tiles/'):])
            if not path:
                return self._send(404, {'error': 'no such tile'})
            with open(path, 'rb') as f:
                return self._send(200, f.read(), 'image/jpeg')
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        cfg = self.server.cfg
        if self.path != '/api/annotation':
            return self._send(404, {'error': 'not found'})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            task = next(t for t in cfg['tasks']['tasks'] if t['label_uid'] == body.get('label_uid'))
        except (ValueError, StopIteration):
            return self._send(400, {'error': 'unknown or malformed label_uid'})
        try:
            record = validate_annotation(body, task)
        except ValueError as e:
            return self._send(400, {'error': str(e)})
        record_annotation(cfg['out_dir'], cfg['annotator'], record)
        return self._send(200, {'ok': True, 'label_uid': record['label_uid']})


def serve(tasks_dir, out_dir, annotator, port, host='127.0.0.1'):
    tasks = load_tasks(tasks_dir)
    done = completed(out_dir, annotator)
    os.makedirs(annotator_dir(out_dir, annotator), exist_ok=True)
    print(f'{len(tasks["tasks"])} tasks, {len(done)} already done by {annotator!r}')
    print(f'  http://{host}:{port}')

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = Server((host, port), Handler)
    httpd.cfg = {'tasks': tasks, 'tasks_dir': tasks_dir, 'out_dir': out_dir,
                 'annotator': annotator}
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tasks-dir', required=True, help='directory holding tasks.json and the tiles')
    ap.add_argument('--out-dir', required=True, help='where annotations are written')
    ap.add_argument('--annotator', required=True,
                    help='annotator id; becomes a subdirectory, so [a-z0-9_-] only')
    ap.add_argument('--port', type=int, default=8000)
    # Bound to loopback and not configurable to a wildcard: the tiles are unpublished research imagery
    # and there is no reason for this to be reachable off the machine.
    args = ap.parse_args(argv)
    annotator_dir(args.out_dir, args.annotator)          # validate before binding a port
    return serve(args.tasks_dir, args.out_dir, args.annotator, args.port)


if __name__ == '__main__':
    raise SystemExit(main())
