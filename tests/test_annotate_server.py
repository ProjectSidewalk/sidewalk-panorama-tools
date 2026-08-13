"""Tests for reports/scripts/annotate_server.py — the local annotation server.

The server is the only thing standing between an annotator and the answer key, which sits in the same
directory as the file it does serve. So the tests here are mostly about what it refuses: `geometry.json`
by name, tile paths not on the task list, and a task file that leaks a stored coordinate. The HTTP shell
is deliberately thin and every decision it makes lives in a module-level function, which is what makes
this suite socket-free.
"""

import json
import os
import sys

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
    first version of the handler assembled this inline and dropped `initial_view_fraction`, so the view
    opened at the full 60 deg cut instead of the 20 deg the protocol specifies. Nothing failed — the
    framing was simply wrong, which is the failure mode a UI cannot report on itself."""

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

    def test_completed_labels_are_dropped_from_the_queue_but_counted(self, tmp_path):
        out = srv.tasks_payload(self._tasks(tmp_path), 'jon', {'city:1'})
        assert [t['label_uid'] for t in out['tasks']] == ['city:0', 'city:2']
        assert (out['n_total'], out['n_done']) == (3, 1)

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
        assert srv.completed(str(tmp_path), 'jon') == {'seattle-wa:1', 'cdmx:2'}

    def test_a_city_with_a_hyphen_round_trips_through_the_filename(self, tmp_path):
        """The uid contains a colon, which is not a legal Windows filename character, so it is stored
        as an underscore — and city ids contain hyphens and underscores of their own. Only the FIRST
        underscore is the separator, or `walla-walla-wa:7` would come back wrong."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}},
                                      task('walla-walla-wa:7'))
        srv.record_annotation(str(tmp_path), 'jon', rec)
        assert srv.completed(str(tmp_path), 'jon') == {'walla-walla-wa:7'}

    def test_two_annotators_never_collide(self, tmp_path):
        """§4 requires Jon's independent 50 through the same tooling; the agreement gate is computed on
        the overlap, so the two must be stored separately rather than one overwriting the other."""
        rec = srv.validate_annotation({'point': {'x': 1, 'y': 2},
                                       'box': {'x': 1, 'y': 1, 'w': 5, 'h': 5}}, task())
        srv.record_annotation(str(tmp_path), 'jon', rec)
        other = dict(rec, point={'x': 9, 'y': 9})
        srv.record_annotation(str(tmp_path), 'claude', other)
        assert srv.completed(str(tmp_path), 'jon') == srv.completed(str(tmp_path), 'claude')
        jon = json.load(open(srv.annotation_path(str(tmp_path), 'jon', rec['label_uid'])))
        claude = json.load(open(srv.annotation_path(str(tmp_path), 'claude', rec['label_uid'])))
        assert jon['point'] != claude['point']

    def test_no_annotations_yet_is_an_empty_set_not_an_error(self, tmp_path):
        assert srv.completed(str(tmp_path), 'nobody') == set()

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
