"""Tests for reports/scripts/annotate_server.py — the local annotation server.

The server is the only thing standing between an annotator and the answer key, which sits in the same
directory as the file it does serve. So the tests here are mostly about what it refuses: `geometry.json`
by name, tile paths not on the task list, and a task file that leaks a stored coordinate. The HTTP shell
is deliberately thin and every decision it makes lives in a module-level function, which is what makes
this suite socket-free.
"""

import http.client
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import annotate_server as srv  # noqa: E402
import annotation_tiles as at  # noqa: E402


def task(label_uid='seattle-wa:1', label_type='CurbRamp', w=910, h=910):
    return {'label_uid': label_uid, 'tile': at.tile_name(label_uid), 'tile_width': w,
            'tile_height': h, 'label_type': label_type, 'rubric': at.RUBRIC[label_type]}


def tasks_dir(tmp_path, tasks=None, extra_top=None, tile_bytes=b'\xff\xd8jpeg'):
    tasks = tasks or [task()]
    payload = {'protocol': 'prereg §4', 'flags': list(at.FLAGS), 'n_tasks': len(tasks),
               'tasks': tasks}
    payload.update(extra_top or {})
    d = tmp_path / 'tasksdir'
    d.mkdir(exist_ok=True)
    (d / srv.TASKS_FILE).write_text(json.dumps(payload), encoding='utf-8')
    # The answer key, deliberately present: it is where it really lives, and its being refused is the
    # property under test rather than its being absent.
    (d / 'geometry.json').write_text(json.dumps({'seed': 1, 'geometry': {}}), encoding='utf-8')
    for t in tasks:
        (d / t['tile']).write_bytes(tile_bytes)
    return str(d)


class TestLoadTasks:

    def test_it_loads_an_annotator_safe_task_file(self, tmp_path):
        loaded = srv.load_tasks(tasks_dir(tmp_path))
        assert loaded['n_tasks'] == 1
        assert loaded['tasks'][0]['label_uid'] == 'seattle-wa:1'

    @pytest.mark.parametrize('leak', ['pano_x', 'pano_y', 'left', 'top', 'jitter_x', 'jitter_y'])
    def test_it_refuses_a_task_file_that_leaks_the_stored_point(self, tmp_path, leak):
        """Re-checked at load even though `build_tasks` already withholds these: the two files travel
        together, and a later convenience field would anchor every annotation with nothing failing."""
        t = task()
        t[leak] = 1234
        with pytest.raises(ValueError, match=leak):
            srv.load_tasks(tasks_dir(tmp_path, [t]))

    def test_it_refuses_a_task_file_carrying_the_seed(self, tmp_path):
        """The seed plus the uid recomputes the jitter, which recovers the stored point from the tile
        origin — so it is a leak even though it looks like harmless provenance."""
        with pytest.raises(ValueError, match='seed'):
            srv.load_tasks(tasks_dir(tmp_path, extra_top={'seed': 20260812}))


class TestTasksPayload:
    """What the page is handed. This is the seam a socket-free suite would otherwise miss entirely: the
    first version of the handler assembled this inline and dropped `initial_view_fraction`, so the page
    framed every tile off a default instead of off the protocol. Nothing failed — the framing was
    simply wrong, which is the failure mode a UI cannot report on itself. (The protocol has since moved
    the opening view to the whole cut, which is why the tests below pin the plumbing rather than the
    value: the same drop would be invisible today.)"""

    def _tasks(self, tmp_path, n=3):
        ts = [task(f'city:{i}') for i in range(n)]
        payload = json.load(open(os.path.join(tasks_dir(tmp_path, ts), srv.TASKS_FILE),
                                 encoding='utf-8'))
        payload['initial_view_fraction'] = 1 / 3
        return payload

    def test_it_forwards_the_initial_view_fraction(self, tmp_path):
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', set())
        assert out['initial_view_fraction'] == pytest.approx(1 / 3)

    def test_a_task_file_without_one_defaults_to_the_whole_tile(self, tmp_path):
        """Rather than KeyError-ing the whole session on an older tasks.json."""
        payload = self._tasks(tmp_path)
        del payload['initial_view_fraction']
        assert srv.tasks_payload(payload, 'jon', set())['initial_view_fraction'] == 1.0

    def test_completed_labels_stay_in_the_list_and_are_marked_done(self, tmp_path):
        """Deliberate reversal. The payload used to drop finished labels, which made the queue a one-way
        conveyor: a misplaced point could only be fixed by editing JSON by hand, because the page had no
        record the label existed. Back-navigation needs the whole list plus which of it is finished."""
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', {'city:1'})
        assert [t['label_uid'] for t in out['tasks']] == ['city:0', 'city:1', 'city:2']
        assert out['done'] == ['city:1']
        assert (out['n_total'], out['n_done']) == (3, 1)

    def test_the_done_list_is_sorted_so_the_payload_is_stable(self, tmp_path):
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', {'city:2', 'city:0'})
        assert out['done'] == ['city:0', 'city:2']

    def test_the_flags_flag_help_and_box_rule_all_come_from_code(self, tmp_path):
        """Protocol wording, sent from the module rather than the task file, so a tile directory cut
        before a flag or a convention existed still explains it. A flag with no explanation, or a "tight
        box" instruction that never says tight around WHAT, is per-annotator drift the agreement gate
        reads as noise."""
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', set())
        assert out['flags'] == list(at.FLAGS)
        assert set(out['flag_help']) == set(at.FLAGS), 'every flag needs its line'
        assert all(out['flag_help'][f].strip() for f in at.FLAGS)
        assert out['box_rule'] == at.BOX_RULE

    def test_a_task_file_cut_before_a_flag_existed_still_offers_it(self, tmp_path):
        """The whole reason `flags` moved out of the task file. A rendered tasks.json is a snapshot of
        the protocol as it stood when the tiles were cut; Amendment 3 added `no-extent` after tiles
        existed, and taking the list from the file served three flags to a page whose own server
        accepts four — so the annotator had no key to press for a referent with no extent, and nothing
        failed. `annotation_subset.write_subset` refreshes the copies it writes; this covers every
        directory, including one served straight out of annotation_tiles.py."""
        payload = self._tasks(tmp_path)
        payload['flags'] = ['object-absent', 'ambiguous', 'occluded']       # a pre-Amendment-3 file
        out = srv.tasks_payload(payload, 'jon', set())
        assert out['flags'] == list(at.FLAGS)
        assert 'no-extent' in out['flags']
        # Discrimination: the flag the page offers must be one the server would actually accept.
        for flag in out['flags']:
            srv.validate_annotation({'flags': [flag], 'point': {'x': 1, 'y': 1},
                                     'box': {'x': 0, 'y': 0, 'w': 2, 'h': 2}}, task())

    def test_the_box_rule_says_whole_object(self, tmp_path):
        """The specific ambiguity it exists to close: for a fire hydrant, box the hydrant or the part
        blocking the footway? Both are defensible, they differ by a lot, and the answer is the whole
        object — the POINT carries impedance, the BOX carries extent."""
        rule = srv.tasks_payload(self._tasks(tmp_path), 'jon', set())['box_rule'].lower()
        assert 'whole object' in rule
        assert 'not just the part' in rule

    def test_it_forwards_the_cut_width_the_framing_control_is_labelled_in(self, tmp_path):
        """The page offers 20°/30°/45°/60°, which it cannot label from the fraction alone."""
        payload = self._tasks(tmp_path)
        payload['cut_fov_deg'] = 60.0
        assert srv.tasks_payload(payload, 'jon', set())['cut_fov_deg'] == 60.0

    def test_a_task_file_without_a_cut_width_falls_back_to_the_constant(self, tmp_path):
        """Tile directories rendered before the control existed must keep working rather than labelling
        the framing buttons off a missing value."""
        payload = self._tasks(tmp_path)
        payload.pop('cut_fov_deg', None)
        assert srv.tasks_payload(payload, 'jon', set())['cut_fov_deg'] == at.CUT_FOV_DEG

    def test_the_payload_carries_no_stored_geometry(self, tmp_path):
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', set())
        blob = json.dumps(out)
        for leak in ('pano_x', 'pano_y', 'jitter', 'left', 'top', 'seed'):
            assert leak not in blob, leak


class TestTileResolution:
    """Tiles are resolved through the task list, so the allowlist is the data rather than a filter."""

    def test_a_listed_tile_resolves(self, tmp_path):
        d = tasks_dir(tmp_path)
        tasks = srv.load_tasks(d)
        assert srv.resolve_tile(tasks, d, 'seattle-wa_1.jpg') == os.path.join(d, 'seattle-wa_1.jpg')

    def test_the_answer_key_is_refused_by_name(self, tmp_path):
        d = tasks_dir(tmp_path)
        tasks = srv.load_tasks(d)
        assert os.path.isfile(os.path.join(d, 'geometry.json')), 'the key must really be there'
        assert srv.resolve_tile(tasks, d, 'geometry.json') is None

    @pytest.mark.parametrize('name', [
        '../geometry.json', '..%2fgeometry.json', 'sub/../geometry.json',
        '/etc/passwd', 'C:\\Windows\\win.ini', 'tasks.json', 'unlisted.jpg', '',
    ])
    def test_anything_not_on_the_list_is_refused(self, tmp_path, name):
        d = tasks_dir(tmp_path)
        assert srv.resolve_tile(srv.load_tasks(d), d, name) is None

    def test_a_listed_tile_that_is_missing_from_disk_resolves_to_nothing(self, tmp_path):
        d = tasks_dir(tmp_path)
        tasks = srv.load_tasks(d)
        os.remove(os.path.join(d, 'seattle-wa_1.jpg'))
        assert srv.resolve_tile(tasks, d, 'seattle-wa_1.jpg') is None


class TestAnnotatorNames:
    """An annotator id becomes a directory."""

    @pytest.mark.parametrize('name', ['jon', 'claude', 'claude-repeat', 'a', 'x_1'])
    def test_good_names_are_accepted(self, tmp_path, name):
        assert srv.annotator_dir(str(tmp_path), name).endswith(name)

    @pytest.mark.parametrize('name', ['../escape', 'Jon', 'has space', '', None, 'x' * 40, 'a/b'])
    def test_bad_names_are_refused(self, tmp_path, name):
        with pytest.raises(ValueError):
            srv.annotator_dir(str(tmp_path), name)


class TestValidateAnnotation:

    def test_a_complete_annotation_is_stored_in_tile_coordinates(self):
        rec = srv.validate_annotation(
            {'point': {'x': 400.5, 'y': 500.25},
             'box': {'x': 380, 'y': 480, 'w': 60, 'h': 40},
             'flags': [], 'notes': 'clear', 'elapsed_ms': 8123, 'zoom_used': 3.2}, task())
        assert rec['point'] == {'x': 400.5, 'y': 500.25}
        assert rec['box']['w'] == 60.0
        assert rec['label_type'] == 'CurbRamp'
        assert rec['elapsed_ms'] == 8123
        assert 'pano_x' not in rec and 'left' not in rec

    def test_sub_pixel_precision_survives(self):
        """A zoomed annotator can place better than a whole pixel, and 1 px is 0.022 deg against a
        0.34 deg agreement gate — rounding here would throw away real precision for nothing."""
        rec = srv.validate_annotation(
            {'point': {'x': 400.37, 'y': 500.62}, 'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        assert rec['point']['x'] == pytest.approx(400.37)

    def test_a_point_is_required(self):
        with pytest.raises(ValueError, match='point'):
            srv.validate_annotation({'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())

    def test_a_box_is_required(self):
        with pytest.raises(ValueError, match='box'):
            srv.validate_annotation({'point': {'x': 5, 'y': 5}}, task())

    def test_object_absent_excuses_both(self):
        """The one flag that means there is nothing to place."""
        rec = srv.validate_annotation({'flags': ['object-absent']}, task())
        assert rec['point'] is None and rec['box'] is None
        assert rec['flags'] == ['object-absent']

    def test_ambiguous_and_occluded_still_require_a_placement(self):
        """§4 uses these to keep edge cases out of the placement distribution, not to opt out: a
        flagged-but-placed label is a measurement with a caveat, a flagged-and-blank one is a hole."""
        for flag in ('ambiguous', 'occluded'):
            with pytest.raises(ValueError, match='point'):
                srv.validate_annotation({'flags': [flag]}, task())

    def test_an_unknown_flag_is_refused(self):
        with pytest.raises(ValueError, match='unknown flag'):
            srv.validate_annotation({'flags': ['probably-fine'],
                                     'point': {'x': 1, 'y': 1},
                                     'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())

    @pytest.mark.parametrize('point', [{'x': -1, 'y': 5}, {'x': 5, 'y': -1},
                                       {'x': 911, 'y': 5}, {'x': 5, 'y': 911}])
    def test_a_point_outside_the_tile_is_refused(self, point):
        with pytest.raises(ValueError, match='outside the tile'):
            srv.validate_annotation({'point': point,
                                     'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())

    @pytest.mark.parametrize('box', [{'x': 0, 'y': 0, 'w': 0, 'h': 10},
                                     {'x': 0, 'y': 0, 'w': 10, 'h': 0}])
    def test_a_zero_area_box_is_refused(self, box):
        with pytest.raises(ValueError, match='no area'):
            srv.validate_annotation({'point': {'x': 5, 'y': 5}, 'box': box}, task())

    def test_a_box_running_off_the_tile_is_refused(self):
        with pytest.raises(ValueError, match='outside the tile'):
            srv.validate_annotation({'point': {'x': 5, 'y': 5},
                                     'box': {'x': 900, 'y': 900, 'w': 50, 'h': 50}}, task())

    def test_notes_are_bounded(self):
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 1},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5},
                                       'notes': 'z' * 5000}, task())
        assert len(rec['notes']) == 2000


class TestRecordAndResume:

    def test_an_annotation_round_trips_to_disk(self, tmp_path):
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        path = srv.record_annotation(str(tmp_path), 'jon', rec)
        assert json.load(open(path, encoding='utf-8')) == rec

    def test_completed_uids_are_what_resumes_the_queue(self, tmp_path):
        for uid in ('seattle-wa:1', 'cdmx:2'):
            rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                           'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task(uid))
            srv.record_annotation(str(tmp_path), 'jon', rec)
        assert srv.completed(str(tmp_path), 'jon',
                             ['seattle-wa:1', 'cdmx:2', 'cdmx:3']) == {'seattle-wa:1', 'cdmx:2'}

    def test_a_uid_whose_city_holds_an_underscore_still_resumes(self, tmp_path):
        """Why `completed` resolves forward instead of inverting the filename.

        The uid's colon is not a legal Windows filename character, so it is stored as an underscore —
        and the inverse is not a function: `sao_paulo:12` and `sao:paulo_12` write the same file, so
        recovering the uid means guessing which underscore was the colon. Every deployment id in the
        corpus is hyphenated today, which is what made the inverse look correct; the failure mode is a
        silently broken resume that re-serves finished work as a revision, so it is asserted against
        rather than left to a naming convention holding."""
        for uid in ('sao_paulo:12', 'walla-walla-wa:7'):
            rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                           'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task(uid))
            srv.record_annotation(str(tmp_path), 'jon', rec)
        assert srv.completed(str(tmp_path), 'jon',
                             ['sao_paulo:12', 'walla-walla-wa:7']) == {'sao_paulo:12',
                                                                       'walla-walla-wa:7'}

    def test_a_stray_file_cannot_inflate_the_done_count(self, tmp_path):
        """Discrimination for the above: a directory scan would count anything ending in .json."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        with open(os.path.join(srv.annotator_dir(str(tmp_path), 'jon'), 'notes.json'), 'w') as f:
            f.write('{}')
        assert srv.completed(str(tmp_path), 'jon', [rec['label_uid']]) == {rec['label_uid']}

    def test_two_annotators_never_collide(self, tmp_path):
        """§4 requires Jon's independent 50 through the same tooling; the agreement gate is computed on
        the overlap, so the two must be stored separately rather than one overwriting the other."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        other = dict(rec, point={'x': 9, 'y': 9})
        srv.record_annotation(str(tmp_path), 'claude', other)
        uids = [rec['label_uid']]
        assert (srv.completed(str(tmp_path), 'jon', uids)
                == srv.completed(str(tmp_path), 'claude', uids))
        jon = json.load(open(srv.annotation_path(str(tmp_path), 'jon', rec['label_uid'])))
        claude = json.load(open(srv.annotation_path(str(tmp_path), 'claude', rec['label_uid'])))
        assert jon['point'] != claude['point']

    def test_no_annotations_yet_is_an_empty_set_not_an_error(self, tmp_path):
        assert srv.completed(str(tmp_path), 'nobody', ['seattle-wa:1']) == set()

    def test_resubmitting_overwrites_rather_than_duplicating(self, tmp_path):
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        srv.record_annotation(str(tmp_path), 'jon', dict(rec, point={'x': 3, 'y': 4}))
        assert len(os.listdir(srv.annotator_dir(str(tmp_path), 'jon'))) == 1
        stored = json.load(open(srv.annotation_path(str(tmp_path), 'jon', rec['label_uid'])))
        assert stored['point'] == {'x': 3.0, 'y': 4.0}

    def test_a_partial_write_leaves_no_readable_file(self, tmp_path):
        """Atomic via os.replace: a crash mid-write must not leave a truncated JSON that later reads as
        a completed annotation."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        files = os.listdir(srv.annotator_dir(str(tmp_path), 'jon'))
        assert files == ['seattle-wa_1.json'], files


class TestOverARealSocket:
    """The handler end to end. Everything else in this file calls the functions directly, which is what
    keeps the suite fast — but the handler is glue, and glue is where a field gets dropped without any
    function being wrong. It already happened once: the first handler assembled the task payload inline
    and omitted `initial_view_fraction`, so every tile opened at 3x the intended scale.

    Bound to port 0 and a tmp_path out-dir, deliberately. The by-hand version of this check was pointed
    at a live annotation directory and overwrote 32 seconds of real work with its test fixture; the
    revision history added in the same change is the only reason it came back.
    """

    @pytest.fixture
    def live(self, tmp_path):
        ts = [task('city:0'), task('city:1')]
        tdir = tasks_dir(tmp_path, ts)
        out = tmp_path / 'out'
        server = socketserver.TCPServer(('127.0.0.1', 0), srv.Handler)
        server.cfg = {'tasks': srv.load_tasks(tdir), 'tasks_dir': tdir,
                      'out_dir': str(out), 'annotator': 'tester'}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f'http://127.0.0.1:{server.server_address[1]}'
        server.shutdown()
        server.server_close()

    def _get(self, base, path):
        with urllib.request.urlopen(base + path) as r:
            return json.load(r)

    def _post(self, base, path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_the_task_payload_survives_the_handler(self, live):
        got = self._get(live, '/api/tasks')
        assert got['n_total'] == 2 and len(got['tasks']) == 2
        assert got['done'] == []
        assert 'initial_view_fraction' in got and 'cut_fov_deg' in got
        assert got['flags'] == list(at.FLAGS)

    def test_submit_then_reload_round_trips(self, live):
        self._post(live, '/api/annotation', {
            'label_uid': 'city:0', 'point': {'x': 5, 'y': 6},
            'box': {'x': 1, 'y': 1, 'w': 9, 'h': 9}, 'initial_view_deg': 45})
        back = self._get(live, '/api/annotation/city%3A0')
        assert back['point'] == {'x': 5.0, 'y': 6.0}
        assert back['initial_view_deg'] == 45.0
        assert self._get(live, '/api/tasks')['done'] == ['city:0']

    def test_a_revision_over_the_wire_keeps_the_original(self, live):
        for x in (5, 50):
            self._post(live, '/api/annotation', {
                'label_uid': 'city:0', 'point': {'x': x, 'y': 6},
                'box': {'x': 1, 'y': 1, 'w': 9, 'h': 9}})
        back = self._get(live, '/api/annotation/city%3A0')
        assert back['point']['x'] == 50.0
        assert [h['point']['x'] for h in back['superseded']] == [5.0]

    def test_an_unannotated_label_is_404_not_an_empty_record(self, live):
        """An empty record would render as a blank annotation the page then re-submits over the top."""
        with pytest.raises(urllib.error.HTTPError) as e:
            self._get(live, '/api/annotation/city%3A1')
        assert e.value.code == 404

    @pytest.mark.parametrize('method', ['HEAD', 'PUT', 'DELETE'])
    def test_an_unsupported_method_gets_a_response_instead_of_a_dropped_connection(self, live,
                                                                                   method):
        """`log_message` reads args[0] to decide whether a line is worth printing, and it has two
        callers with different shapes: `log_request` passes the request line, `log_error` passes an
        HTTPStatus. `send_error` — which the stdlib reaches on every unsupported method and every
        malformed request line — goes through the second, so an unguarded `'POST' in args[0]` raised
        TypeError *inside* the handler and the client got a dropped connection rather than the 501
        the stdlib was midway through sending. A bare HEAD from any link-checker did it."""
        host, port = live.rsplit(':', 1)
        conn = http.client.HTTPConnection(host.removeprefix('http://'), int(port), timeout=5)
        conn.request(method, '/')
        assert conn.getresponse().status == 501
        conn.close()

    def test_a_query_string_does_not_hide_a_tile(self, live):
        """Routing splits the query off first, so a cache-busting suffix cannot turn a valid tile into
        a 404. Every path here is matched against an allowlist, which is what made this a real risk
        rather than a cosmetic one."""
        for path in ('/tiles/city_0.jpg', '/tiles/city_0.jpg?v=2'):
            with urllib.request.urlopen(live + path) as r:
                assert r.status == 200 and r.read()[:2] == b'\xff\xd8'

    def test_a_label_not_in_the_task_list_cannot_name_a_file(self, live):
        """`annotation_path` builds a filename from the uid, so an unchecked one is a path to anywhere."""
        with pytest.raises(urllib.error.HTTPError) as e:
            self._get(live, '/api/annotation/' + urllib.parse.quote('../../etc/passwd', safe=''))
        assert e.value.code == 404

    def test_the_answer_key_is_still_refused_over_the_wire(self, live):
        with pytest.raises(urllib.error.HTTPError) as e:
            self._get(live, '/geometry.json')
        assert e.value.code == 404


class TestThePageItself:
    """`annotate.html` is a real source file with no compiler and no linter, and the Python suite reads
    it only as bytes to serve. A syntax error in its script block is a blank screen at annotation time
    with nothing in this suite to catch it — which is exactly what nearly shipped when the queue was
    rewritten for back-navigation."""

    @pytest.fixture(scope='class')
    def script(self):
        page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'reports', 'scripts', srv.PAGE)
        with open(page, encoding='utf-8') as f:
            html = f.read()
        m = re.search(r'<script>(.*?)</script>', html, re.S)
        assert m, 'the page must have exactly one inline script block'
        return m.group(1)

    def test_the_script_parses(self, script, tmp_path):
        node = shutil.which('node')
        if not node:
            pytest.skip('node not available to parse the page script')
        js = tmp_path / 'page.js'
        js.write_text(script, encoding='utf-8')
        proc = subprocess.run([node, '--check', str(js)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    def test_no_shortcut_key_is_bound_twice(self, script):
        """The action table drives both the keyboard and the sidebar buttons, so a duplicate key means
        one of the two buttons silently does the other's job."""
        keys = re.findall(r"\{key: '(.+?)'", script)
        assert keys, 'the action table must be findable'
        assert len(keys) == len(set(keys)), keys

    def test_no_shortcut_collides_with_a_flag_digit(self, script):
        """Flags are bound to 1..N by index. A digit in the action table would shadow a flag, and the
        annotator would think they had flagged something."""
        keys = re.findall(r"\{key: '(.+?)'", script)
        assert not [k for k in keys if k.isdigit()], keys
        assert len(at.FLAGS) <= 9, 'flags past 9 would need a different binding than a single digit'


class TestRevisionsAreKept:
    """Back-navigation invites revision, and silent revision is the thing that would quietly cost this
    corpus its meaning: an annotation edited after the annotator has seen fifty more tiles is no longer
    an independent judgement of that tile, and if the edit overwrites in place there is nothing left to
    say it happened. Overwriting was the old behaviour and was correct while the queue was one-way.
    """

    def _rec(self, x, y):
        return srv.validate_annotation({'point': {'x': x, 'y': y},
                                        'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())

    def test_a_first_submission_carries_no_history(self, tmp_path):
        srv.record_annotation(str(tmp_path), 'jon', self._rec(1, 2))
        stored = srv.load_annotation(str(tmp_path), 'jon', 'seattle-wa:1')
        assert 'superseded' not in stored and 'revision' not in stored

    def test_a_revision_keeps_what_it_replaced(self, tmp_path):
        srv.record_annotation(str(tmp_path), 'jon', self._rec(1, 2))
        srv.record_annotation(str(tmp_path), 'jon', self._rec(3, 4))
        stored = srv.load_annotation(str(tmp_path), 'jon', 'seattle-wa:1')
        assert stored['point'] == {'x': 3.0, 'y': 4.0}
        assert stored['revision'] == 1
        assert [h['point'] for h in stored['superseded']] == [{'x': 1.0, 'y': 2.0}]

    def test_history_accumulates_flat_rather_than_nesting(self, tmp_path):
        """Nesting each prior record inside the next makes the file grow quadratically under repeated
        edits and turns reading the history into a recursive walk."""
        for i in range(4):
            srv.record_annotation(str(tmp_path), 'jon', self._rec(i, i))
        stored = srv.load_annotation(str(tmp_path), 'jon', 'seattle-wa:1')
        assert stored['revision'] == 3
        assert [h['point']['x'] for h in stored['superseded']] == [0.0, 1.0, 2.0]
        assert not any('superseded' in h for h in stored['superseded'])

    def test_a_revision_is_still_one_file(self, tmp_path):
        srv.record_annotation(str(tmp_path), 'jon', self._rec(1, 2))
        srv.record_annotation(str(tmp_path), 'jon', self._rec(3, 4))
        assert len(os.listdir(srv.annotator_dir(str(tmp_path), 'jon'))) == 1

    def test_another_annotators_history_is_untouched(self, tmp_path):
        srv.record_annotation(str(tmp_path), 'jon', self._rec(1, 2))
        srv.record_annotation(str(tmp_path), 'claude', self._rec(7, 8))
        srv.record_annotation(str(tmp_path), 'jon', self._rec(3, 4))
        claude = srv.load_annotation(str(tmp_path), 'claude', 'seattle-wa:1')
        assert claude['point'] == {'x': 7.0, 'y': 8.0} and 'revision' not in claude


class TestLoadAnnotation:
    """What lets the page step back onto a finished label and show what was submitted, rather than a
    blank tile that re-submits as nothing."""

    def test_it_returns_none_when_nothing_was_submitted(self, tmp_path):
        assert srv.load_annotation(str(tmp_path), 'jon', 'seattle-wa:1') is None

    def test_it_round_trips_the_record(self, tmp_path):
        rec = srv.validate_annotation({'point': {'x': 1.5, 'y': 2.5},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5},
                                       'flags': ['occluded'], 'notes': 'hi'}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        got = srv.load_annotation(str(tmp_path), 'jon', 'seattle-wa:1')
        assert (got['point'], got['flags'], got['notes']) == ({'x': 1.5, 'y': 2.5}, ['occluded'], 'hi')


class TestInitialViewIsRecorded:
    """§4 opens every tile at 20°, chosen partly so the window would not imply a crop size — Study 2 is
    choosing between sizing rules, and an annotator shown a tight window draws boxes calibrated to it.
    The page can now widen that. Recording the framing per annotation is what keeps the effect
    measurable instead of an assumption; a preference the artifact never sees is a confound."""

    def test_the_framing_is_stored(self):
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5},
                                       'initial_view_deg': 45}, task())
        assert rec['initial_view_deg'] == 45.0

    def test_an_absent_framing_is_none_not_zero(self):
        """Zero degrees is not a framing anyone used; it is the absence of a record, and a study that
        averaged it would silently pull the mean toward a value that never happened."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        assert rec['initial_view_deg'] is None
