"""reports/2026-11-22-edge-band-calibration.md and reports/scripts/edge_band_calibration.py (#179 item 4).

Two halves, as .claude/rules/desk-studies.md requires. The code half drives the study script on synthetic stores
in tmp_path: the id forms, the frozen draw and the symlink farm (scanned by the real scan_black_bands), the
output-path guards, the dispatcher's abort rules on injected samples, its signals, child exit codes, the rate
cap and the stdout redaction, the class rules at their boundaries on ledger-formatted values, D1 and its
sensitivity fold, the stratum weights and bounds, and the paced cross-check. The committed-run half re-derives
what the report says from the committed raw files, and checks no committed file carries a host path, a host
name or an account.
"""

import csv
import datetime
import gzip
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import deque

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import edge_band_calibration as ebc  # noqa: E402
import scan_black_bands  # noqa: E402
from downloaders import common  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, 'tests', 'fixtures', 'black_bands')
DATA = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-edge-band-calibration')
SUMMARY = os.path.join(REPO_ROOT, 'reports', 'data', '2026-11-22-edge-band-calibration.json')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-22-edge-band-calibration.md')

GSV = ['d4PanoAAAAAAAAAAAAAAAA', 'clPanoAAAAAAAAAAAAAAAA', 'dkPanoAAAAAAAAAAAAAAAA', 'thPanoAAAAAAAAAAAAAAAA']
NUMERIC = '947478730505514'
UUID = '0b7c62a5-3e2f-4c6b-9d4e-0f1a2b3c4d5e'


def put(store, city, pano_id, fixture='clean.jpg'):
    shard = os.path.join(str(store), city, pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    path = os.path.join(shard, pano_id + '.jpg')
    shutil.copy(os.path.join(FIXTURES, fixture), path)
    return path


def make_store(tmp_path):
    store = tmp_path / 'store'
    for pid, fx in zip(GSV, ['d4.jpg', 'clean.jpg', 'dark_scene.jpg', 'thin_bottom_10.jpg']):
        put(store, 'alpha', pid, fx)
    put(store, 'beta', NUMERIC, 'nadir_cap_3.jpg')
    put(store, 'beta', UUID, 'clean.jpg')
    os.makedirs(str(store / 'old_scrapes' / 'scrapes'))         # no shard dirs: not a city folder
    (store / 'scrape_queue.log').write_text('x')
    return store


def devices(store):
    """A device map for tests: everything under the store root is one device, everything else another."""
    real = os.path.realpath(str(store))

    def device_of(path):
        p = os.path.realpath(path)
        try:
            return 1 if os.path.commonpath([p, real]) == real else 2
        except ValueError:
            return 2
    return device_of


def tree(path):
    out = {}
    for root, dirs, files in os.walk(str(path)):
        for name in dirs + files:
            p = os.path.join(root, name)
            st = os.lstat(p)
            out[os.path.relpath(p, str(path))] = (st.st_size, st.st_mtime_ns, os.path.islink(p))
    return out


class TestIdForms:

    @pytest.mark.parametrize('pano_id, form', [
        ('_-DnYqNdzw5vYJgUe0IfoA', 'gsv22'), ('947478730505514', 'numeric'), ('2576203436081781', 'numeric'),
        (UUID, 'uuid'), ('abc', 'other'), ('x' * 23, 'other')])
    def test_real_id_shapes(self, pano_id, form):
        assert ebc.id_form(pano_id) == form

    def test_a_caos_file_is_a_photosphere_only_from_the_census_date(self):
        caos = 'CAoSLEFGMVFpcE1fN1hPTWZfaHdR'
        before = int(datetime.datetime(2026, 11, 4, 23, 59, tzinfo=datetime.timezone.utc).timestamp()) * 10 ** 9
        after = int(datetime.datetime(2026, 11, 5, 0, 0, tzinfo=datetime.timezone.utc).timestamp()) * 10 ** 9
        assert ebc.id_form(caos, after) == 'photosphere'
        assert ebc.id_form(caos, before) == 'other', 'a prefix-only rule would call this provider bytes'
        assert ebc.id_form(caos) == 'other', 'an unknown mtime is not evidence of the photo host'


class TestTheFrameAndTheFarm:

    def test_a_farm_scanned_by_the_real_sweep_gives_the_stores_rows(self, tmp_path):
        store = make_store(tmp_path)
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        ebc.frame(str(store), str(farm), str(out), roster=['alpha', 'beta'], device_of=devices(store))
        for city in ('alpha', 'beta'):
            for shard in os.listdir(str(farm / city)):
                assert not os.path.islink(str(farm / city / shard)), 'shard dirs must be real directories'
                for name in os.listdir(str(farm / city / shard)):
                    assert os.path.islink(str(farm / city / shard / name)), 'each pano is a symlink, never a copy'
            scan_black_bands.scan_store(str(farm / city), ledger_path=str(tmp_path / ('f_%s.csv' % city)),
                                        worklist_path=str(tmp_path / ('fw_%s.csv' % city)))
            scan_black_bands.scan_store(str(store / city), ledger_path=str(tmp_path / ('s_%s.csv' % city)),
                                        worklist_path=str(tmp_path / ('sw_%s.csv' % city)))
            farm_rows, _ = scan_black_bands.load_ledger(str(tmp_path / ('f_%s.csv' % city)))
            store_rows, _ = scan_black_bands.load_ledger(str(tmp_path / ('s_%s.csv' % city)))
            assert farm_rows == store_rows and farm_rows
            for pid, row in farm_rows.items():
                st = os.stat(str(store / city / pid[:2] / (pid + '.jpg')))
                assert row['size'] == str(st.st_size) and row['mtime_ns'] == str(st.st_mtime_ns)

    def test_frame_writes_ids_and_counts_and_excludes_folders_without_shards(self, tmp_path):
        store = make_store(tmp_path)
        doc = ebc.frame(str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'), roster=['alpha'],
                        device_of=devices(store))
        assert sorted(doc['cities']) == ['alpha', 'beta']
        assert doc['excluded_folders'] == ['old_scrapes']
        assert doc['cities']['beta']['forms'] == {'numeric': 1, 'uuid': 1}
        assert doc['cities']['beta']['in_roster'] is False
        assert ebc.read_ids(str(tmp_path / 'out' / 'ids' / 'alpha.txt.gz')) == sorted(GSV)

    def test_frame_twice_changes_nothing_and_refuses(self, tmp_path):
        store = make_store(tmp_path)
        args = (str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'))
        ebc.frame(*args, roster=['alpha'], device_of=devices(store))
        before = (tree(tmp_path / 'farm'), tree(tmp_path / 'out'))
        put(store, 'alpha', 'nwPanoAAAAAAAAAAAAAAAA')
        with pytest.raises(ebc.RefusedError):
            ebc.frame(*args, roster=['alpha'], device_of=devices(store))
        assert (tree(tmp_path / 'farm'), tree(tmp_path / 'out')) == before

    def test_the_cli_refuses_with_a_nonzero_exit(self, tmp_path):
        store = make_store(tmp_path)
        out = tmp_path / 'out'
        os.makedirs(str(out))
        (out / 'frame.json').write_text('{}')
        # Same device as the store in a real run too (one tmp dir), so this also proves the guard comes first.
        assert ebc.main(['frame', str(store), '--farm', str(tmp_path / 'farm'), '--out', str(out)]) == ebc.EXIT_REFUSED

    def test_a_large_city_is_sampled_once_with_the_published_seed(self):
        forms = {'%022d' % i: 'gsv22' for i in range(3000)}
        forms['947478730505514'] = 'numeric'
        ids, design = ebc.draw('somecity', forms)
        assert design == 'sample' and len(ids) == ebc.SAMPLE_SIZE + 1 and '947478730505514' in ids
        expected = random.Random('179-calibration:somecity').sample(sorted(i for i in forms if forms[i] == 'gsv22'),
                                                                     2500)
        assert sorted(expected + ['947478730505514']) == ids

    def test_a_pano_in_two_roster_folders_is_counted_once(self, tmp_path):
        store = make_store(tmp_path)
        put(store, 'gamma', GSV[0], 'd4.jpg')                       # also in alpha
        put(store, 'gamma', 'zzPanoAAAAAAAAAAAAAAAA')
        doc = ebc.frame(str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'), roster=['alpha', 'gamma'],
                        device_of=devices(store))
        assert doc['cities']['alpha']['gsv22_dedup'] == 4
        assert doc['cities']['gamma']['gsv22_dedup'] == 1
        assert doc['cities']['gamma']['frozen_dup_of_earlier'] == [GSV[0]]

    def test_an_existing_frame_json_alone_refuses(self, tmp_path):
        store = make_store(tmp_path)
        out = tmp_path / 'out'
        out.mkdir()
        (out / 'frame.json').write_text('{}')
        with pytest.raises(ebc.RefusedError):
            ebc.frame(str(store), str(tmp_path / 'farm'), str(out), roster=['alpha'], device_of=devices(store))
        assert not (tmp_path / 'farm').exists() and not (out / 'ids').exists()

    def test_an_existing_id_list_alone_refuses(self, tmp_path):
        store = make_store(tmp_path)
        args = (str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'))
        ebc.frame(*args, roster=['alpha'], device_of=devices(store))
        os.remove(str(tmp_path / 'out' / 'frame.json'))
        shutil.rmtree(str(tmp_path / 'farm' / 'beta'))
        before = (tree(tmp_path / 'farm'), tree(tmp_path / 'out'))
        with pytest.raises(ebc.RefusedError):
            ebc.frame(*args, roster=['alpha'], device_of=devices(store))
        assert (tree(tmp_path / 'farm'), tree(tmp_path / 'out')) == before


class SyncToolOps:
    """Runs each child to completion at spawn time (the real scan_black_bands), so `run` can be driven on any
    platform. Signals are recorded, never sent."""

    def __init__(self):
        self.t = 0.0
        self.signals = []
        self.spawned = []

    def now(self):
        return self.t

    def wall(self):
        return 1.8e9 + self.t

    def sleep(self, s):
        self.t += s

    def spawn(self, cmd, cwd):
        self.spawned.append(cmd)
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

        class H:
            pass
        h = H()
        h.pid = 10000 + len(self.spawned)
        h.code = done.returncode
        h.lines = deque((done.stdout + done.stderr).splitlines())
        return h

    def take_lines(self, h):
        out = list(h.lines)
        h.lines.clear()
        return out

    def poll(self, h):
        return h.code

    def signal_group(self, pid, name):
        self.signals.append((pid, name))

    def proc_state(self, pid):
        return 'S'

    def rchar(self, pid):
        return None

    def pid_alive(self, pid):
        return False


class TestRunMeasuresTheFrozenIdsOnly:

    def test_panos_added_after_frame_are_not_measured(self, tmp_path):
        store = make_store(tmp_path)
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        doc = ebc.frame(str(store), str(farm), str(out), roster=['alpha', 'beta'], device_of=devices(store))
        put(store, 'alpha', 'nwPanoAAAAAAAAAAAAAAAA', 'd4.jpg')       # the nightly scrape adds a pano
        ops = SyncToolOps()
        code, session = ebc.run(str(farm), str(out), str(store), ebc.run_order(doc), deadline=3600, ops=ops,
                                device_of=devices(store))
        assert code == 0
        for city in ('alpha', 'beta'):
            rows, _ = scan_black_bands.load_ledger(str(out / 'sweep' / city / 'black_bands.csv'))
            assert sorted(rows) == ebc.read_ids(str(out / 'ids' / (city + '.txt.gz')))
            assert (out / 'sweep' / city / 'done.json').exists()
        assert ebc.run_order(doc) == ['beta', 'alpha'], 'the non-GSV census goes first'

    def test_the_child_is_the_unmodified_tool_on_the_farm_with_explicit_outputs(self, tmp_path):
        store = make_store(tmp_path)
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        ebc.frame(str(store), str(farm), str(out), roster=['alpha'], device_of=devices(store))
        ops = SyncToolOps()
        ebc.run(str(farm), str(out), str(store), ['alpha'], deadline=3600, ops=ops, device_of=devices(store))
        cmd = ops.spawned[0]
        assert cmd[1] == ebc.SCAN_TOOL and cmd[2] == os.path.join(str(farm), 'alpha')
        assert cmd[3] == '--ledger' and cmd[5] == '--worklist' and cmd[7] == '--max-runtime'
        assert all(not os.path.realpath(p).startswith(os.path.realpath(str(store))) for p in (cmd[4], cmd[6]))

    def test_redaction_rewrites_failed_lines_and_the_worklist_path(self, tmp_path):
        store = make_store(tmp_path)
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        (store / 'alpha' / 'zz').mkdir()
        (store / 'alpha' / 'zz' / 'zzBrokenAAAAAAAAAAAAAA.jpg').write_bytes(b'\xff\xd8 truncated')
        ebc.frame(str(store), str(farm), str(out), roster=['alpha'], device_of=devices(store))
        ebc.run(str(farm), str(out), str(store), ['alpha'], deadline=3600, ops=SyncToolOps(), device_of=devices(store))
        text = (out / 'sweep' / 'alpha' / 'stdout.txt').read_text(encoding='utf-8')
        assert 'FAILED <farm>/alpha/zz/zzBrokenAAAAAAAAAAAAAA.jpg' in text.replace('\\', '/')
        assert 'Work-list: <out>/sweep/alpha/black_band_worklist.csv' in text.replace('\\', '/')
        assert str(tmp_path) not in text and os.path.realpath(str(tmp_path)) not in text

    def test_the_redactor_covers_store_out_farm_and_work(self, tmp_path):
        red = ebc.make_redactor([(str(tmp_path / 'w' / 'out'), '<out>'), (str(tmp_path / 'w' / 'farm'), '<farm>'),
                                 (str(tmp_path / 'store'), '<store-root>'), (str(tmp_path / 'w'), '<work>')])
        line = 'a %s b %s c %s d %s' % (tmp_path / 'w' / 'out' / 'x', tmp_path / 'w' / 'farm' / 'y',
                                        tmp_path / 'store' / 'z', tmp_path / 'w' / 'other')
        assert str(tmp_path) not in red(line)
        assert '<out>' in red(line) and '<farm>' in red(line) and '<store-root>' in red(line) and '<work>' in red(line)


class TestTheGuards:

    def test_an_output_dir_that_is_a_symlink_into_the_store_is_refused(self, tmp_path):
        store = make_store(tmp_path)
        link = tmp_path / 'out'
        os.symlink(str(store / 'alpha'), str(link))
        store_real = os.path.realpath(str(store))
        # Only the store root itself is "the store's device", so the device test cannot rescue a path test that
        # fails to resolve the link: the refusal has to come from commonpath on the real path.
        with pytest.raises(ebc.GuardError, match='inside the store'):
            ebc.check_outputs(str(store), [str(link)], device_of=lambda p: 1 if p == store_real else 2)

    def test_a_sibling_store_root2_is_not_inside(self, tmp_path):
        store = make_store(tmp_path)
        sibling = tmp_path / 'store2'
        sibling.mkdir()
        ebc.check_outputs(str(store), [str(sibling)], device_of=devices(store))     # a prefix test would refuse

    @pytest.mark.parametrize('bad', ['', '   ', None])
    def test_an_empty_path_is_refused(self, tmp_path, bad):
        store = make_store(tmp_path)
        with pytest.raises(ebc.GuardError):
            ebc.check_outputs(str(store), [str(tmp_path / 'farm'), bad], device_of=devices(store))

    def test_the_same_device_under_another_path_is_refused(self, tmp_path):
        store = make_store(tmp_path)
        (tmp_path / 'elsewhere').mkdir()
        with pytest.raises(ebc.GuardError):
            ebc.check_outputs(str(store), [str(tmp_path / 'elsewhere')], device_of=lambda p: 7)

    def test_run_checks_the_guard_itself_before_starting_anything(self, tmp_path):
        store = make_store(tmp_path)
        ops = SyncToolOps()
        with pytest.raises(ebc.GuardError):
            ebc.run(str(tmp_path / 'farm'), str(store / 'alpha' / 'out'), str(store), ['alpha'], deadline=60,
                    ops=ops, device_of=devices(store))
        assert ops.spawned == [] and not (store / 'alpha' / 'out').exists()

    def test_extras_checks_the_guard_too(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ebc.GuardError):
            ebc.extras(str(tmp_path / 'farm'), str(store / 'beta'), str(store), ['beta'], 8.0, 0,
                       device_of=devices(store))


# --- the dispatcher on injected samples -------------------------------------------------------------------------

class FakeProc:
    def __init__(self, pid, city, ledger, behave):
        self.pid, self.city, self.ledger, self.behave = pid, city, ledger, behave
        self.code = None
        self.stopped = False
        self.term_pending = False
        self.state = 'R'
        self.rchar = 0
        self.lines = deque()
        self.n = 0
        with open(ledger, 'a', newline='') as f:
            if os.path.getsize(ledger) == 0:
                f.write(','.join(scan_black_bands.LEDGER_FIELDS) + '\r\n')

    def row(self, pano_id, size):
        with open(self.ledger, 'a', newline='') as f:
            f.write('%s,%d,1,16384,8192,0.0000,0.0000,0.0000,0.0000\r\n' % (pano_id, size))
        self.n += 1


class FakeOps:
    """Simulated children: each city's `behave(proc, t)` runs every simulated second the child is not stopped."""

    def __init__(self, behaviours, alive=()):
        self.t = 0.0
        self.behaviours = behaviours
        self.procs = {}
        self.signals = []
        self.spawned = []
        self.alive = set(alive)

    def now(self):
        return self.t

    def wall(self):
        return 1.8e9 + self.t

    def sleep(self, s):
        self.t += s
        for p in self.procs.values():
            if p.code is None and not p.stopped:
                p.behave(p, self.t)

    def spawn(self, cmd, cwd):
        city = os.path.basename(cmd[2])
        pid = 500 + len(self.spawned)
        self.spawned.append(city)
        p = FakeProc(pid, city, cmd[4], self.behaviours[city])
        self.procs[pid] = p
        return p

    def take_lines(self, h):
        out = list(h.lines)
        h.lines.clear()
        return out

    def poll(self, h):
        return h.code

    def signal_group(self, pid, name):
        self.signals.append((pid, name))
        p = self.procs[pid]
        if name == 'STOP':
            p.stopped = True
        elif name == 'CONT':
            p.stopped = False
            if p.term_pending and p.code is None:
                p.code = -15
        elif name == 'TERM':
            if p.stopped:
                p.term_pending = True           # a stopped process acts on SIGTERM only once continued
            elif p.code is None:
                p.code = -15

    def proc_state(self, pid):
        p = self.procs[pid]
        return 'T' if p.stopped else p.state

    def rchar(self, pid):
        return self.procs[pid].rchar

    def pid_alive(self, pid):
        return pid in self.alive


def steady(size=1000, every=1, finish_after=None):
    def behave(p, t):
        if int(t) % every == 0:
            p.row('%s%06d' % (p.city[:2], p.n), size)
            p.rchar += size
        if finish_after is not None and t >= finish_after:
            p.lines.append('Examined %d panorama(s): 0 with an edge band over 5.0%%, %d without, 0 already ledgered, '
                           '0 failed, 0 unreached. Work-list: x' % (p.n, p.n))
            p.code = 0
    return behave


def finishing(code, after=3):
    def behave(p, t):
        if t >= after:
            p.lines.append('Examined 1 panorama(s): 0 with an edge band over 5.0%%, 0 without, 0 already ledgered, '
                           '%d failed, 0 unreached. Work-list: x' % (1 if code == 1 else 0))
            p.code = code
    return behave


def dispatch(tmp_path, cities, behaviours, deadline=10000, readers=1, baseline=None, alive=()):
    store = tmp_path / 'store'
    store.mkdir(exist_ok=True)
    ops = FakeOps(behaviours, alive)
    out = tmp_path / 'out'
    code, doc = ebc.run(str(tmp_path / 'farm'), str(out), str(store), cities, deadline=deadline, readers=readers,
                        baseline=baseline, ops=ops, device_of=devices(store))
    return code, doc, ops, out


class TestTheAbortRulesOnSamples:

    def test_a_resume_pass_with_no_rows_does_not_trip(self):
        w = ebc.Watch()
        for t in range(0, 110):
            assert w.sample(float(t), 'S', (), 0, 5000, False) is None

    def test_a_paused_child_does_not_trip_however_long(self):
        w = ebc.Watch()
        for t in range(0, 600):
            assert w.sample(float(t), 'T', (), 0, 5000, True) is None
        assert w.active == 0.0

    def test_no_progress_for_120_unpaused_seconds_trips(self):
        w = ebc.Watch()
        reasons = [w.sample(float(t), 'S', (), 0, 5000, False) for t in range(0, 125)]
        assert reasons[119] is None and reasons[120] is not None

    def test_paused_time_stops_the_stall_clock(self):
        w = ebc.Watch()
        for t in range(0, 100):
            assert w.sample(float(t), 'S', (), 0, 5000, False) is None
        for t in range(100, 400):
            assert w.sample(float(t), 'T', (), 0, 5000, True) is None
        assert w.sample(400.0, 'S', (), 0, 5000, False) is None
        assert w.quiet < ebc.STALL_LIMIT_S

    def test_rchar_growth_and_stdout_count_as_progress(self):
        w = ebc.Watch()
        for t in range(0, 300):
            assert w.sample(float(t), 'R', (), 0, 5000 + t, False) is None
        w = ebc.Watch()
        for t in range(0, 300):
            assert w.sample(float(t), 'R', (), 1 if t % 60 == 0 else 0, 5000, False) is None

    def test_thirty_seconds_continuously_in_d_trips_and_twenty_eight_does_not(self):
        w = ebc.Watch()
        for t in range(0, 29):
            assert w.sample(float(t), 'D', ('gsv',), 0, 0, False) is None
        assert w.sample(29.0, 'S', ('gsv',), 0, 0, False) is None
        for t in range(30, 59):
            assert w.sample(float(t), 'D', ('gsv',), 0, 0, False) is None
        w = ebc.Watch()
        reasons = [w.sample(float(t), 'D', ('gsv',), 0, 0, False) for t in range(0, 32)]
        assert reasons[29] is None and reasons[30] is not None

    def test_a_slowed_child_trips_the_three_times_rule(self):
        w = ebc.Watch(baseline={'gsv': 30.0})
        t = 0.0
        for _ in range(200):                       # 0.3 s per pano: 30 s per 100, at the baseline
            t += 0.3
            assert w.sample(t, 'R', ('gsv',), 0, 0, False) is None
        reason = None
        for _ in range(200):                       # 1.0 s per pano: 100 s per 100 > 3 x 30
            t += 1.0
            reason = reason or w.sample(t, 'R', ('gsv',), 0, 0, False)
        assert reason and '3x' in reason
        w = ebc.Watch(baseline={'gsv': 30.0})
        t = 0.0
        for _ in range(300):                       # 0.85 s per pano: 85 s per 100 < 90 s
            t += 0.85
            assert w.sample(t, 'R', ('gsv',), 0, 0, False) is None

    def test_the_run_ends_with_the_marker_and_starts_nothing_more(self, tmp_path):
        def wedged(p, t):
            p.state = 'D'
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': wedged, 'beta': steady()})
        assert code == ebc.EXIT_ABORT
        assert ops.spawned == ['alpha'], 'nothing more is started after an abort'
        assert ops.now() < ebc.D_STATE_LIMIT_S + 5, 'the dispatcher exits at the abort, not at the deadline'
        marker = json.loads((out / 'abort.json').read_text())
        assert 'state D' in marker['reason']
        assert doc['abort_reason'] == marker['reason']


class TestSignals:

    def test_a_paused_child_is_continued_then_terminated_at_abort(self, tmp_path):
        state = {}

        def big_then_wedged(p, t):
            if t < 5:
                p.row('aa%06d' % p.n, 400 * 10 ** 6)      # far over the cap: the dispatcher pauses a child
            state.setdefault('t', t)

        def wedged(p, t):
            p.state = 'D'
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': wedged, 'beta': big_then_wedged},
                                       readers=2)
        assert code == ebc.EXIT_ABORT
        beta = [p for p in ops.procs.values() if p.city == 'beta'][0]
        seq = [name for pid, name in ops.signals if pid == beta.pid]
        assert 'STOP' in seq, 'the rate cap paused beta'
        assert seq[-2:] == ['CONT', 'TERM']
        assert beta.code == -15 and not beta.stopped, 'no stopped child is left behind'

    def test_a_paused_child_is_continued_then_terminated_at_the_hard_stop(self, tmp_path):
        def big(p, t):
            p.row('aa%06d' % p.n, 400 * 10 ** 6)
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': steady(), 'beta': big}, readers=2,
                                       deadline=60)
        assert doc['stop_reason'] == 'deadline'
        for p in ops.procs.values():
            seq = [name for pid, name in ops.signals if pid == p.pid]
            assert seq[-2:] == ['CONT', 'TERM'] and p.code == -15 and not p.stopped

    def test_run_refuses_while_an_earlier_sessions_child_is_alive(self, tmp_path):
        out = tmp_path / 'out'
        out.mkdir()
        (out / 'run.pids').write_text('4242\n')
        with pytest.raises(ebc.RefusedError):
            dispatch(tmp_path, ['alpha'], {'alpha': steady()}, alive={4242})
        code, doc, ops, out = dispatch(tmp_path, ['alpha'], {'alpha': steady(finish_after=3)}, alive=set())
        assert ops.spawned == ['alpha'], 'a dead pid does not block'


class TestChildExitCodes:

    def test_exit_1_is_counted_and_the_run_continues(self, tmp_path):
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': finishing(1), 'beta': finishing(0)})
        assert code == 0 and ops.spawned == ['alpha', 'beta']
        assert [c['exit'] for c in doc['children']] == [1, 0]

    def test_exit_3_stops_the_run(self, tmp_path):
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': finishing(3), 'beta': finishing(0)})
        assert code == ebc.EXIT_CHILD and ops.spawned == ['alpha']
        assert (out / 'abort.json').exists()


class TestTheRateCap:

    def test_readers_are_paused_when_ledger_growth_exceeds_the_cap(self, tmp_path):
        def fast(p, t):
            p.row('aa%06d' % p.n, 100 * 10 ** 6)           # 100 MB/s from one reader
        code, doc, ops, out = dispatch(tmp_path, ['alpha'], {'alpha': fast}, deadline=120)
        assert ('STOP' in [n for _, n in ops.signals])
        child = doc['children'][0]
        assert child['paused_s'] > 0
        assert child['bytes'] / (child['active_s'] + child['paused_s']) <= ebc.RATE_CAP_BYTES * 1.25

    def test_under_the_cap_nothing_is_paused(self, tmp_path):
        code, doc, ops, out = dispatch(tmp_path, ['alpha'], {'alpha': steady(size=10 ** 6, finish_after=50)})
        assert 'STOP' not in [n for _, n in ops.signals]

    def test_the_cap_decision_unit(self):
        cap = ebc.RateCap(cap=100, window=10, start=0)
        cap.add(5, 2000)
        assert cap.decide(10, ['a', 'b'], []) == ('pause', 'b')
        assert cap.decide(30, ['a'], ['b']) == ('resume', 'b')

    def test_never_more_than_two_readers(self, tmp_path):
        code, doc, ops, out = dispatch(tmp_path, ['a1', 'a2', 'a3'],
                                       {c: steady(size=10, finish_after=5) for c in ('a1', 'a2', 'a3')}, readers=8)
        assert doc['readers'] == 2


# --- classes and D1 ------------------------------------------------------------------------------------------------

def f4(lines, reduced_height):
    """A depth as the ledger writes it: a whole number of reduced lines, formatted '%.4f'."""
    return float('%.4f' % (lines / float(reduced_height)))


class TestTheClassRules:

    @pytest.mark.parametrize('height, reduced', [(8192, 1024), (6656, 832)])
    def test_a_whole_tile_row_is_f_rows_and_a_line_past_it_is_not(self, height, reduced):
        for k in (1, 2, 3):
            lines = 64 * k
            assert ebc.classify('gsv22', f4(lines, reduced), 0.0, 0.0, 0.0, height, 8.0) == 'F-rows'
            assert ebc.classify('gsv22', f4(lines + 1, reduced), 0.0, 0.0, 0.0, height, 8.0) == 'U', 'one-sided'
            assert ebc.classify('gsv22', f4(lines - 1, reduced), 0.0, 0.0, 0.0, height, 8.0) == 'F-rows', 'short by t'
            assert ebc.classify('gsv22', f4(lines - 2, reduced), 0.0, 0.0, 0.0, height, 8.0) == 'U'
            assert ebc.classify('gsv22', f4(lines - 1, reduced), 0.0, 0.0, 0.0, height, 0.0) == 'U'

    def test_a_rounded_value_just_over_the_boundary_still_aligns(self):
        """192/832 formats as 0.2308, and 0.2308 x 6656 = 1536.2: over 1536 only by the ledger's rounding."""
        assert '%.4f' % (192 / 832.0) == '0.2308' and 0.2308 * 6656 > 1536
        assert ebc.tile_aligned(0.2308, 6656, 0.0)
        assert ebc.tile_aligned(0.0769, 6656, 0.0), '64/832 = 0.0769 x 6656 = 511.85'

    def test_the_other_classes(self):
        assert ebc.classify('numeric', 0.174, 0.0, 0.174, 0.0, 4000, 8.0) == 'P'
        assert ebc.classify('photosphere', 0.0, 0.0, 0.0, 0.0, 4000, 8.0) == 'P'
        assert ebc.classify('gsv22', 0.1875, 0.1875, 0.1875, 0.1875, 8192, 8.0) == 'F-frame'
        assert ebc.classify('other', 0.0, 0.0625, 0.0, 0.0625, 8192, 8.0) == 'F-frame'
        assert ebc.classify('gsv22', 0.0, 0.0, 0.0137, 0.0, 8192, 8.0) == 'N'
        assert ebc.classify('gsv22', 0.0, 0.0, 0.0, 0.0, 8192, 8.0) == 'clean'


def stratum(city, N, n, u=()):
    return {'city': city, 'N': N, 'n': n, 'u': list(u)}


class TestD1:

    def test_zero_events_confirm_005(self):
        d = ebc.d1_decide([stratum('a', 300000, 2500), stratum('b', 5000, 2500), stratum('c', 2000, 2000)])
        assert (d['limit'], d['outcome']) == (0.05, 'confirmed')

    def test_values_between_the_candidates_raise_it_provisionally(self):
        u = [0.0601] * 3                                         # 50/832 on a 6656 frame, three in the heavy city
        d = ebc.d1_decide([stratum('a', 300000, 2500, u), stratum('b', 5000, 2500)])
        assert (d['limit'], d['outcome']) == (0.0605, 'raised-provisional')
        assert d['cleared'] == u

    def test_values_over_both_leave_it_to_the_owner(self):
        d = ebc.d1_decide([stratum('a', 300000, 2500, [0.07] * 3), stratum('b', 5000, 2500)])
        assert (d['limit'], d['outcome']) == (0.05, 'neither-passes')

    def test_the_limit_is_never_lowered(self):
        d = ebc.d1_decide([stratum('a', 300000, 2500, [0.04] * 50)])
        assert d['limit'] in ebc.CANDIDATES and d['limit'] >= 0.05

    def test_property_a_returned_0605_clears_every_value_it_claims_to(self):
        rng = random.Random(179)
        seen_raised = 0
        values = [f4(n, 832) for n in range(40, 60)] + [f4(n, 1024) for n in range(50, 70)]
        for _ in range(400):
            strata = [stratum('s%d' % i, rng.randint(2600, 300000), 2500,
                              [rng.choice(values) for _ in range(rng.randint(0, 4))]) for i in range(rng.randint(1, 5))]
            d = ebc.d1_decide(strata)
            if d['limit'] == 0.0605:
                seen_raised += 1
                for v in d['cleared']:
                    assert not ebc.is_over(v, d['limit'])
                assert ebc.passes(ebc.rate_at(strata, d['limit'])), 'the returned limit itself passes'
        assert seen_raised > 10, 'the property was exercised'


class TestTheFold:

    def test_a_five_pano_identical_depth_group_flips_and_four_does_not(self):
        strata = [stratum('a', 300000, 2500), stratum('b', 5000, 2500)]
        five = {'a': [(16384, 8192, '0.0625')] * 5}
        four = {'a': [(16384, 8192, '0.0625')] * 4}
        assert ebc.d1_with_sensitivity(strata, four)['outcome'] == 'confirmed'
        flipped = ebc.d1_with_sensitivity(strata, five)
        assert flipped['outcome'] == 'fold-flips' and flipped['limit'] == 0.05
        assert flipped['fold_groups'] == [{'city': 'a', 'frame': '16384x8192', 'bottom_band': '0.0625', 'n': 5}]

    def test_a_group_spread_over_frames_does_not_fold(self):
        strata = [stratum('a', 300000, 2500)]
        mixed = {'a': [(16384, 8192, '0.0625')] * 3 + [(13312, 6656, '0.0625')] * 3}
        assert ebc.d1_with_sensitivity(strata, mixed)['fold_groups'] == []


class TestWeightsAndBounds:

    def test_the_exact_bound_at_zero_and_one_event(self):
        assert ebc.upper_bound(0, 2500) == pytest.approx(1 - 0.05 ** (1 / 2500.0), rel=1e-6)
        assert ebc.upper_bound(1, 2500) == pytest.approx(0.0018967, rel=1e-3)

    def test_stratum_weights_and_a_census_stratums_exact_share(self):
        r = ebc.rate_at([stratum('a', 90, 9, [0.07]), stratum('b', 10, 10, [0.07])], 0.05)
        assert r['estimate'] == pytest.approx(0.9 * 1 / 9 + 0.1 * 1 / 10)
        assert r['bound'] == pytest.approx(0.9 * ebc.upper_bound(1, 9) + 0.1 * 0.1), 'census b contributes 1/10'
        pooled = 2 / 19.0
        assert r['estimate'] != pytest.approx(pooled), 'not unweighted pooling'

    def test_a_zero_denominator_is_undefined_and_prints(self):
        r = ebc.rate_at([stratum('a', 100, 0)], 0.05)
        assert r['estimate'] is None
        assert ebc.fmt(r['estimate'], '.2f') == 'n/a'
        assert ebc.d1_decide([stratum('a', 100, 0)])['outcome'] == 'undefined'

    def test_what_would_fail_from_the_frames_weights(self):
        strata = [stratum('heavy', 180000, 2500), stratum('mid', 20000, 2500)] + \
            [stratum('s%d' % i, 20000, 2500) for i in range(39)] + [stratum('census', 1000, 1000)]
        e5 = ebc.d1_what_would_fail(strata)
        assert e5['zero_event_bound'] == pytest.approx(ebc.upper_bound(0, 2500) * 980000 / 981000.0, rel=1e-9)
        assert e5['heaviest']['city'] == 'heavy' and e5['heaviest']['events_that_fail'] == 2
        assert e5['median']['events_that_fail'] == 13


class TestTheCrossCheck:

    def test_paced_serial_and_it_stops_at_the_first_timeout(self):
        clock = {'t': 0.0}
        calls, sleeps = [], []

        def fetch(url):
            calls.append((url, clock['t']))
            clock['t'] += 2.0
            if len(calls) == 2:
                raise TimeoutError('timed out')
            return 200, [{'pano_id': '947478730505514', 'source': 'mapillary'}]

        def sleep(s):
            sleeps.append(s)
            clock['t'] += s
        rows, stop = ebc.crosscheck([('a', 'u1'), ('b', 'u2'), ('c', 'u3')], fetch, sleep, lambda: clock['t'])
        assert [u for u, _ in calls] == ['u1', 'u2'], 'no request after the timeout, and no retry'
        assert calls[1][1] - calls[0][1] >= 10.0
        assert 'timeout' in stop
        assert ('a', 'numeric', 'mapillary', 1) in rows

    def test_a_refusal_ends_the_step(self):
        rows, stop = ebc.crosscheck([('a', 'u1'), ('b', 'u2')], lambda url: (403, []), lambda s: None, lambda: 0.0)
        assert 'refused' in stop and len(rows) == 1


class TestTheExtrasInstrument:

    @pytest.mark.parametrize('name', sorted(n for n in os.listdir(FIXTURES) if n.endswith('.jpg')))
    def test_band_lines_at_098_is_the_production_detector(self, name):
        from PIL import Image
        path = os.path.join(FIXTURES, name)
        with Image.open(path) as im:
            w, h = im.size
            im.draft('L', (w // 8, h // 8))
            luma = im.convert('L')
        lw, lh = luma.size
        prod = common._edge_bands_of_luma(luma)
        assert ebc.band_lines(luma, range(lh - 1, -1, -1), lambda y: (0, y, lw, y + 1)) / float(lh) == prod.bottom
        assert ebc.band_lines(luma, range(lw - 1, -1, -1), lambda x: (x, 0, x + 1, lh)) / float(lw) == prod.right

    def test_a_stitch_of_ours_carries_pillows_default_tables(self, tmp_path):
        from PIL import Image
        p = tmp_path / 'x.jpg'
        Image.new('RGB', (64, 64), (10, 20, 30)).save(str(p), 'jpeg')
        assert ebc.pano_extras(str(p))['pillow_default_tables'] == 1
        Image.new('RGB', (64, 64), (10, 20, 30)).save(str(p), 'jpeg', quality=90)
        assert ebc.pano_extras(str(p))['pillow_default_tables'] == 0

    def test_the_in_memory_definition_is_the_downloaders(self):
        ex = ebc.pano_extras(os.path.join(FIXTURES, 'd4.jpg'), inmem=True)
        from PIL import Image
        with Image.open(os.path.join(FIXTURES, 'd4.jpg')) as im:
            b = common.edge_black_bands(im.convert('RGB'))
        assert (ex['inmem_bottom'], ex['inmem_right']) == ('%.4f' % b.bottom, '%.4f' % b.right)

    def test_the_committed_tile_tolerance_is_what_the_stitcher_gives(self, tmp_path):
        with open(os.path.join(DATA, 'tile-tolerance.json'), encoding='utf-8') as f:
            committed = json.load(f)
        fresh = ebc.tile_row_tolerance(str(tmp_path))
        assert fresh['cases'] == committed['cases'] and fresh['tolerance_px'] == committed['tolerance_px']


class TestAnalyzeEndToEnd:

    def test_a_synthetic_run_classifies_decides_and_tabulates(self, tmp_path):
        store = make_store(tmp_path)
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        doc = ebc.frame(str(store), str(farm), str(out), roster=['alpha', 'beta'], device_of=devices(store))
        ebc.run(str(farm), str(out), str(store), ebc.run_order(doc), deadline=3600, ops=SyncToolOps(),
                device_of=devices(store))
        n = ebc.extras(str(farm), str(out), str(store), ebc.run_order(doc), 8.0, 4e9, device_of=devices(store))
        assert n == 3, 'd4, thin_bottom_10 and the nadir cap carry a band; clean and dark do not'
        shutil.copy(os.path.join(DATA, 'tile-tolerance.json'), str(out / 'tile-tolerance.json'))
        result, unresolved = ebc.analyze(str(out))
        assert result['classes']['gsv22'] == {'F-frame': 1, 'U': 1, 'clean': 2}
        assert result['classes']['numeric'] == {'P': 1}
        assert [u[1] for u in unresolved] == ['thPanoAAAAAAAAAAAAAAAA']
        # alpha is a census stratum of 4 with one U band of 0.0938 over both candidates: neither passes
        assert result['decision']['outcome'] == 'neither-passes' and result['decision']['limit'] == 0.05
        assert result['decision']['primary']['0.05']['estimate'] == pytest.approx(0.25)
        assert '| alpha | fleet | census | 4 | 4 |' in result['tables']['reach']
        assert any(r.startswith('| gsv22 | yes |') for r in result['tables']['witness'])
        json.dumps(result, allow_nan=False)


class TestReviewOneSurvivors:
    """Synthetic tests for the mutants review 1 found surviving every test (S4); each docstring names its mutant."""

    def test_a_candidate_can_fail_on_the_bound_alone(self):
        """Kills PASS_BOUND raised tenfold: no event, estimate 0, bound about 50 per 10,000 (n = 600)."""
        r = ebc.rate_at([stratum('a', 300000, 600)], 0.05)
        assert r['estimate'] == 0 and ebc.PASS_BOUND < r['bound'] < 10 * ebc.PASS_BOUND
        assert not ebc.passes(r)
        assert ebc.d1_decide([stratum('a', 300000, 600)])['outcome'] == 'neither-passes'

    def test_the_estimate_alone_can_fail_a_candidate(self):
        """Kills `passes` joined with `or`: 2 events in the heavy stratum fail the estimate, the bound still passes."""
        strata = [stratum('heavy', 180000, 2500, [0.07, 0.07])] + \
            [stratum('s%d' % i, 20000, 2500) for i in range(40)] + [stratum('census', 1000, 1000)]
        r = ebc.rate_at(strata, 0.05)
        assert r['estimate'] > ebc.PASS_ESTIMATE and r['bound'] <= ebc.PASS_BOUND
        assert not ebc.passes(r)

    def test_the_fold_stops_a_raise_to_0605(self):
        """Kills fold-flips returning the primary limit: the one path where the fold blocks 0.0605."""
        strata = [stratum('a', 300000, 2500, [0.0601] * 3), stratum('b', 5000, 2500)]
        d = ebc.d1_with_sensitivity(strata, {'a': [(16384, 8192, '0.0625')] * 5})
        assert d['primary']['limit'] == 0.0605 and d['folded']['outcome'] == 'neither-passes'
        assert (d['limit'], d['outcome']) == (0.05, 'fold-flips')

    def test_a_value_exactly_on_a_candidate_is_not_over_it(self):
        """Kills a non-strict is_over: production's `>` is the comparison."""
        assert ebc.rate_at([stratum('a', 1000, 1000, [0.05, 0.0605])], 0.05)['events'] == 1
        assert ebc.rate_at([stratum('a', 1000, 1000, [0.05, 0.0605])], 0.0605)['events'] == 0

    def test_a_child_that_left_panos_unreached_is_not_done(self, tmp_path):
        """Kills done.json written whatever `unreached` says: a part-swept folder must resume next session."""
        def partial(p, t):
            if t >= 3:
                p.lines.append('Examined 10 panorama(s): 0 with an edge band over 5.0%, 7 without, 0 already '
                               'ledgered, 0 failed, 3 unreached. Work-list: x')
                p.code = 0
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': partial, 'beta': steady()})
        assert not (out / 'sweep' / 'alpha' / 'done.json').exists()
        assert ops.spawned == ['alpha'] and 'deadline' in doc['stop_reason']

    def test_a_folder_with_done_json_is_not_swept_again(self, tmp_path):
        """Kills `run` ignoring done.json."""
        (tmp_path / 'out' / 'sweep' / 'alpha').mkdir(parents=True)
        (tmp_path / 'out' / 'sweep' / 'alpha' / 'done.json').write_text('{}')
        code, doc, ops, out = dispatch(tmp_path, ['alpha', 'beta'], {'alpha': finishing(0), 'beta': finishing(0)})
        assert ops.spawned == ['beta']

    def test_de_duplication_sees_the_whole_earlier_folder(self, tmp_path, monkeypatch):
        """Kills a de-duplication that remembers only an earlier folder's FROZEN ids: alpha is sampled (2 of 4),
        and gamma holds one of alpha's unsampled ids."""
        monkeypatch.setattr(ebc, 'CENSUS_MAX_GSV', 2)
        monkeypatch.setattr(ebc, 'SAMPLE_SIZE', 2)
        store = make_store(tmp_path)
        drawn = random.Random('179-calibration:alpha').sample(sorted(GSV), 2)
        unsampled = sorted(set(GSV) - set(drawn))[0]
        put(store, 'gamma', unsampled)
        put(store, 'gamma', 'zzPanoAAAAAAAAAAAAAAAA')
        doc = ebc.frame(str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'), roster=['alpha', 'gamma'],
                        device_of=devices(store))
        assert doc['cities']['alpha']['design'] == 'sample'
        assert doc['cities']['gamma']['gsv22_dedup'] == 1
        assert doc['cities']['gamma']['frozen_dup_of_earlier'] == [unsampled]

    def test_de_duplication_follows_the_roster_order(self, tmp_path):
        """Kills de-duplication in alphabetical order: gamma comes first in the roster, so alpha holds the dup."""
        store = make_store(tmp_path)
        put(store, 'gamma', GSV[0], 'd4.jpg')
        doc = ebc.frame(str(store), str(tmp_path / 'farm'), str(tmp_path / 'out'), roster=['gamma', 'alpha'],
                        device_of=devices(store))
        assert doc['cities']['gamma']['frozen_dup_of_earlier'] == []
        assert doc['cities']['gamma']['gsv22_dedup'] == 1
        assert doc['cities']['alpha']['frozen_dup_of_earlier'] == [GSV[0]]
        assert doc['cities']['alpha']['gsv22_dedup'] == 3

    def test_the_draw_sorts_the_ids_first(self):
        """Kills a draw from the ids in walk/insertion order: the published formula is sample(sorted_ids, 2500)."""
        forms = {'%022d' % i: 'gsv22' for i in reversed(range(3000))}
        ids, design = ebc.draw('somecity', forms)
        assert ids == sorted(random.Random('179-calibration:somecity').sample(sorted(forms), 2500))

    def test_the_census_cut_includes_its_own_size(self, monkeypatch):
        """Kills a strict census cut: a folder with exactly CENSUS_MAX_GSV gsv22 panos is a census."""
        monkeypatch.setattr(ebc, 'CENSUS_MAX_GSV', 3)
        monkeypatch.setattr(ebc, 'SAMPLE_SIZE', 2)
        assert ebc.draw('c', {'a' * 22: 'gsv22', 'b' * 22: 'gsv22', 'c' * 22: 'gsv22'})[1] == 'census'
        assert ebc.draw('c', {x * 22: 'gsv22' for x in 'abcd'})[1] == 'sample'

    def test_the_roster_leaves_out_hash_rows(self, tmp_path):
        """Kills a roster that keeps '#' rows (the analyzer drops them)."""
        p = tmp_path / 'cities.csv'
        p.write_text('city_id,display_name\nseattle-wa,Seattle\n#zurich-infra3d,Zurich\n', encoding='utf-8')
        assert ebc.load_roster(str(p)) == ['seattle-wa']

    def test_planned_strata_drop_the_duplicates(self):
        """Kills the what-would-fail strata keeping ids already counted in an earlier folder."""
        doc = {'cities': {'a': {'in_roster': True, 'gsv22_dedup': 4000, 'frozen_forms': {'gsv22': 2500},
                                'frozen_dup_of_earlier': ['x', 'y']}}}
        assert ebc.planned_strata(doc) == [{'city': 'a', 'N': 4000, 'n': 2498, 'u': []}]

    @pytest.mark.parametrize('args, cls', [
        (('gsv22', 0.0, 0.001, 0.0, 0.001, 8192, 8.0), 'F-frame'),     # any right band, not only a deep one
        (('gsv22', 0.0, 0.0, 0.0, 0.0137, 8192, 8.0), 'N'),           # near band on the right alone
    ])
    def test_class_boundaries_review_one_found_untested(self, args, cls):
        assert ebc.classify(*args) == cls

    def test_a_uuid_with_a_suffix_is_not_a_uuid(self):
        assert ebc.id_form(UUID + 'x') == 'other'

    def test_analyze_strata_are_deduplicated_roster_gsv22_and_frozen_only(self, tmp_path):
        """Kills, in analyze: measured rows not de-duplicated; `other` ids joining the strata; N taken from the raw
        gsv22 count; non-roster folders joining D1; ledger rows outside the frozen list counted; D2 ignoring
        the exact band."""
        store = make_store(tmp_path)
        put(store, 'gamma', 'thPanoAAAAAAAAAAAAAAAA', 'thin_bottom_10.jpg')    # alpha's U pano, again
        put(store, 'gamma', 'zzPanoAAAAAAAAAAAAAAAA', 'clean.jpg')
        put(store, 'gamma', 'nbPanoAAAAAAAAAAAAAAAA', 'near_black_band_q40.jpg')
        put(store, 'gamma', 'otPanoAAAAAAAAAAAAAAA', 'thin_bottom_15.jpg')     # 21 characters: `other`
        put(store, 'delta', 'dlPanoAAAAAAAAAAAAAAAA', 'thin_bottom_10.jpg')    # a U pano outside the roster
        farm, out = tmp_path / 'farm', tmp_path / 'out'
        doc = ebc.frame(str(store), str(farm), str(out), roster=['alpha', 'gamma'], device_of=devices(store))
        ebc.run(str(farm), str(out), str(store), ebc.run_order(doc), deadline=3600, ops=SyncToolOps(),
                device_of=devices(store))
        ledger = out / 'sweep' / 'alpha' / 'black_bands.csv'
        rows = ledger.read_text().splitlines()
        # D1 must read the EXACT bottom band: give alpha's U pano a (crafted) near band under 0.05, so a classifier
        # fed the near column would see no event.
        rows = [r.rsplit(',', 2)[0] + ',0.0400,0.0000' if r.startswith('thPano') else r for r in rows]
        rows.append('xxPanoAAAAAAAAAAAAAAAA,1,1,1024,512,0.0938,0.0000,0.0938,0.0000')   # not frozen
        ledger.write_text('\r\n'.join(rows) + '\r\n')
        shutil.copy(os.path.join(DATA, 'tile-tolerance.json'), str(out / 'tile-tolerance.json'))
        result, unresolved = ebc.analyze(str(out))
        # alpha: N 4, n 4, one U (0.0938). gamma: N 2 (zz, nb; th is alpha's), n 2, no U. 4/6 x 1/4 = 1/6.
        assert result['decision']['primary']['0.05']['estimate'] == pytest.approx(1 / 6.0)
        assert result['reached']['alpha'] == 4
        assert result['tables']['d2'] == ['| N | gsv22 | 1 |']


    def test_a_tool_name_fails_and_a_nightly_writer_is_listed(self, tmp_path):
        store = make_store(tmp_path)
        (store / 'alpha' / 'scrape.log').write_text('a')
        before = {k[:2]: k[2:] for k in ebc.store_listing(str(store))}
        (store / 'alpha' / 'scrape.log').write_text('ab')
        (store / 'alpha' / 'black_bands.csv').write_text('x')
        after = {k[:2]: k[2:] for k in ebc.store_listing(str(store))}
        failures, others = ebc.compare_listings(before, after)
        assert len(failures) == 1 and 'black_bands.csv' in failures[0]
        assert any('scrape.log' in o for o in others)

    def test_the_listing_round_trips(self, tmp_path):
        store = make_store(tmp_path)
        p = tmp_path / 'l.txt'
        ebc.write_listing(str(p), ebc.store_listing(str(store)), 'before')
        assert ebc.read_listing(str(p)) == {k[:2]: k[2:] for k in ebc.store_listing(str(store))}


# --- the committed run -------------------------------------------------------------------------------------------

#: Generic leak patterns, so this test names no real path: an absolute path token outside the standard system
#: directories a probe script legitimately names, a Windows drive path, a university host suffix, an '@'.
_ABS = re.compile(r'(?:^|[\s\'"(=:,])(/(?!usr/|dev/null|proc/)[A-Za-z][\w.-]*/)')
_LEAKS = [_ABS, re.compile(r'\b[A-Za-z]:\\'), re.compile(r'\.washington\.edu', re.I), re.compile(r'@')]


def _committed_text_files():
    out = []
    for root, _dirs, files in os.walk(DATA):
        for name in files:
            out.append(os.path.join(root, name))
    out.append(SUMMARY)
    return [p for p in out if os.path.exists(p)]


def _text(path):
    opener = gzip.open if path.endswith('.gz') else open
    with opener(path, 'rt', encoding='utf-8', errors='replace') as f:
        return f.read()


class TestTheCommittedRun:

    def test_the_leak_patterns_catch_what_they_are_for(self):
        for bad in ('FAILED /data/someone/x.jpg', 'ls C:\\Users\\x', 'host.washington.edu', 'a@b'):
            assert any(p.search(bad) for p in _LEAKS), bad
        for fine in ('ls /usr/bin/python3', '2>/dev/null', 'FAILED <farm>/alpha/zz/x.jpg', 'https://pypi.org/simple/'):
            assert not any(p.search(fine) for p in _LEAKS), fine

    @pytest.mark.parametrize('path', _committed_text_files(), ids=lambda p: os.path.relpath(p, REPO_ROOT))
    def test_no_committed_file_names_a_path_a_host_or_an_account(self, path):
        text = _text(path)
        for pattern in _LEAKS:
            m = pattern.search(text)
            assert m is None, '%s matches %s at %r' % (os.path.relpath(path, REPO_ROOT), pattern.pattern,
                                                         text[max(0, m.start() - 20):m.end() + 20])

    def test_the_instrument_check_equals_the_fixtures_measured_here(self):
        path = os.path.join(DATA, 'instrument-check.csv')
        if not os.path.exists(path):
            pytest.skip('no instrument check committed yet (the run has not started)')
        with open(path, newline='', encoding='utf-8') as f:
            assert list(csv.DictReader(f)) == ebc.instrument_rows()

    def test_every_frozen_id_is_measured_and_nothing_else(self):
        """Review 1, N7: the run is complete, so each folder's ledger holds EXACTLY its frozen list, every folder has
        done.json, and the total is 133,919 of 133,919, from the raw files alone."""
        with open(os.path.join(DATA, 'frame.json'), encoding='utf-8') as f:
            frame_doc = json.load(f)
        total = 0
        for city in frame_doc['cities']:
            ids = ebc.read_ids(os.path.join(DATA, 'ids', city + '.txt.gz'))
            assert len(ids) == frame_doc['cities'][city]['frozen']
            rows = ebc.load_ledger_rows(os.path.join(DATA, 'sweep', city, 'black_bands.csv.gz'))
            assert set(rows) == set(ids), city
            assert os.path.exists(os.path.join(DATA, 'sweep', city, 'done.json')), city
            total += len(rows)
        assert total == 133919 == sum(c['frozen'] for c in frame_doc['cities'].values())

    def test_every_prose_figure_is_in_the_report(self):
        """Review 1, S5: the numbers the report quotes in prose are derived by analyze (`prose`, re-derived from the
        raw files by the summary test above), and each is quoted in the report in its own phrase."""
        with open(SUMMARY, encoding='utf-8') as f:
            prose = json.load(f)['prose']
        text = ' '.join(open(REPORT, encoding='utf-8').read().split())
        phrases = {}
        for key, v in prose.items():
            parts = key.split('.')
            if key.endswith('.panos') and parts[0] in ('opening', 'main1', 'main2'):
                phrases[key] = '| %s |' % v
            elif key.endswith('.gb') and parts[0] in ('opening', 'main1', 'main2'):
                phrases[key] = '| %s |' % v
            elif key == 'main1.mb_per_s':
                phrases[key] = '%s MB/s' % v
            elif key == 'main1.abort_last100_s':
                phrases[key] = '%s s' % v
            elif parts[:2] == ['main1', 'reached']:
                phrases[key] = '%s at %s of' % (parts[2], v)
            elif key == 'gsv22.measured':
                phrases[key] = '%s measured' % v
            elif key == 'gsv22.with_exact_band':
                phrases[key] = '%s of %s carry one' % (v, prose['gsv22.measured'])
            elif key == 'gsv22.shallowest_exact_bottom':
                phrases[key] = 'shallower than %s' % v
            elif parts[0] == 'F-frame':
                phrases[key] = '%s %s stitch' % (v, parts[1])
            elif parts[0] == 'U':
                city, frame, depth = parts[1], parts[2], '.'.join(parts[3:])
                phrases[key] = '%s on %s frames' % (depth, frame)
                phrases[key + '#n'] = '%s %s' % (city, v)
            elif parts[0] == 'weight':
                phrases[key] = '%s (%s)' % (v, parts[1])
            elif parts[:2] == ['D2', 'near']:
                phrases[key] = '%s (%s' % ('.'.join(parts[2:]), v)
            elif key == 'extras.panos':
                phrases[key] = '%s banded panos' % v
            elif key == 'extras.gb':
                phrases[key] = '%s GB' % v
            elif key == 'absorbed.primary_bound_per_10000':
                phrases[key] = 'bound %s per 10,000' % v
            elif key == 'absorbed.folded_events':
                phrases[key] = '%s events, an estimate of %s per 10,000' % (v, prose['absorbed.folded_estimate_per_10000'])
            elif key == 'absorbed.folded_estimate_per_10000':
                continue
            else:
                raise AssertionError('no phrase rule for prose figure %s' % key)
        missing = {k: p for k, p in phrases.items() if p not in text}
        assert not missing

    def test_the_summary_is_what_the_raw_files_reduce_to(self):
        if not os.path.exists(SUMMARY):
            pytest.skip('the measurement is not analysed yet')
        with open(SUMMARY, encoding='utf-8') as f:
            committed = json.load(f)
        fresh, _ = ebc.analyze(DATA)
        assert json.loads(json.dumps(fresh)) == committed

    def test_every_table_row_is_in_the_report(self):
        if not os.path.exists(SUMMARY):
            pytest.skip('the measurement is not analysed yet')
        with open(SUMMARY, encoding='utf-8') as f:
            committed = json.load(f)
        report = open(REPORT, encoding='utf-8').read()
        for name, rows in committed['tables'].items():
            for row in rows:
                assert row in report, (name, row)

    def test_every_figure_is_claimed_by_exactly_one_population(self):
        if not os.path.exists(SUMMARY):
            pytest.skip('the measurement is not analysed yet')
        with open(SUMMARY, encoding='utf-8') as f:
            committed = json.load(f)
        claimed = [k for keys in committed['populations'].values() for k in keys]
        assert len(claimed) == len(set(claimed))
        figures = [k for k in committed if k not in ('populations', 'tables')] + \
            ['tables.' + t for t in committed['tables']]
        assert sorted(figures) == sorted(claimed)

    def test_the_decision_entry_precedes_the_results_in_the_report(self):
        report = open(REPORT, encoding='utf-8').read()
        assert 'Decision entry' in report and 'D1' in report
