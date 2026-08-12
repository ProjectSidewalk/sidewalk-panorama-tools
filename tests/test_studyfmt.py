"""Tests for reports/scripts/studyfmt.py and the four scripts that had the bug it exists to prevent.

The defect: an analysis function correctly returns `None` for a quantity that is undefined on its
input, and then `main()` prints it with a format spec. `format(None, '.1f')` raises TypeError, so the
run dies at its summary print — after all the compute, and before `--write`. Found in four scripts at
once (2026-08-11 review); in three of them the crash lands after a live network census.

Two layers here, because either alone would be weak:

1. **The helpers themselves**, including the discrimination that matters — `fmt` must not turn a
   defined value into 'n/a', and `num` must not turn a real 0.0 into None.
2. **Each caller's real print path**, driven with a degenerate input that actually produces the
   `None`. That is the layer that would have caught the bug: `studyfmt` being correct proves nothing
   about whether a script uses it. Each test below reproduces a specific reachable case rather than
   monkeypatching a None into the dict, so it also documents *how* the input gets thin.
"""

import io
import json
import math
import os
import sys
from contextlib import redirect_stdout

import numpy as np
import pandas as pd
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import click_noise_study as cn  # noqa: E402
import photometa_census as pc  # noqa: E402
import rawlabels  # noqa: E402
import store_coverage as sc  # noqa: E402
import studyfmt  # noqa: E402


def write_jpeg(path, size):
    """A real JPEG whose SOF carries `size`, so store_coverage's header scanner reads it. Same helper
    as tests/test_store_coverage.py; kept small rather than imported across test modules."""
    Image.new('RGB', size, (120, 90, 60)).save(str(path), 'JPEG')
    return str(path)


class TestFmt:

    @pytest.mark.parametrize('value,spec,expected', [
        (1.234, '.2f', '1.23'),
        (0.0, '.1f', '0.0'),          # zero is a measurement, not a missing value
        (-0.0, '.1f', '-0.0'),
        (1234567, ',', '1,234,567'),
        (42, '', '42'),
        (0.5, '', '0.5'),
    ])
    def test_defined_values_format_normally(self, value, spec, expected):
        """Discrimination: the guard must be invisible whenever the value exists. A helper that
        answered 'n/a' too eagerly would hide real results instead of crashes."""
        assert studyfmt.fmt(value, spec) == expected

    def test_none_becomes_na_instead_of_raising(self):
        assert studyfmt.fmt(None, '.3f') == 'n/a'

    def test_the_unguarded_form_really_does_raise(self):
        """Guards the guard: pins that this is a TypeError and not something Python tolerates, which
        is the whole premise of the fix."""
        with pytest.raises(TypeError):
            format(None, '.3f')

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
    def test_non_finite_floats_are_missing_too(self, value):
        """A NaN that slipped past `num` must not print as 'nan' beside real numbers — it formats
        happily under any spec, so it is the failure mode that would look like a measurement."""
        assert studyfmt.fmt(value, '.2f') == 'n/a'
        assert format(value, '.2f') == 'nan' or math.isinf(value)

    def test_the_missing_marker_is_overridable(self):
        assert studyfmt.fmt(None, '.1f', missing='--') == '--'


class TestNum:

    @pytest.mark.parametrize('value', [0.0, -0.0, 1.5, -273.15, 1e300])
    def test_finite_values_pass_through_as_floats(self, value):
        """Discrimination for the guard below: a genuine 0.0 must survive. `num` returning None for
        falsy input would silently delete every true-zero percentage in every artifact."""
        got = studyfmt.num(value)
        assert isinstance(got, float) and got == value

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
    def test_non_finite_becomes_none(self, value):
        assert studyfmt.num(value) is None

    def test_it_accepts_numpy_scalars(self):
        """Every caller feeds it numpy output, so this is the real input type."""
        assert studyfmt.num(np.float64(2.5)) == 2.5
        assert studyfmt.num(np.float32('nan')) is None

    def test_its_output_survives_strict_json(self):
        """The reason it exists: study scripts write with allow_nan=False, so a NaN in the dict aborts
        the write on the run's last line."""
        with pytest.raises(ValueError):
            json.dumps({'x': float('nan')}, allow_nan=False)
        assert json.dumps({'x': studyfmt.num(float('nan'))}, allow_nan=False) == '{"x": null}'


def _rawlabels_csv(tmp_path, rows, name='city.csv'):
    """Write a rawLabels-shaped CSV carrying only the columns the shared loader reads."""
    df = pd.DataFrame(rows)
    for col in rawlabels.STUDY_COLUMNS:
        if col not in df:
            df[col] = np.nan
    path = tmp_path / name
    df[rawlabels.STUDY_COLUMNS].to_csv(path, index=False)
    return path


class TestClickNoiseStudyMain:
    """The one that was caught by running it, not by reading it: Richmond has 2 cross-user pairs, and
    every per-type group has none, so `sigma_from_pairs` returns its documented nulls."""

    @staticmethod
    def _one_label(tmp_path):
        _rawlabels_csv(tmp_path, [{
            'label_id': 1, 'user_id': 'u1', 'pano_id': 'p1', 'label_type': 'CurbRamp',
            'time_created': int(pd.Timestamp('2025-01-01', tz='UTC').value // 10 ** 6),
            'heading': 0.0, 'pitch': -20.0, 'zoom': 1.0,
            'canvas_x': 360.0, 'canvas_y': 240.0, 'canvas_width': 720.0, 'canvas_height': 480.0,
            'pano_x': 4096.0, 'pano_y': 2048.0, 'pano_width': 8192.0, 'pano_height': 4096.0,
            'camera_heading': 0.0,
        }])
        return tmp_path

    def test_a_corpus_with_no_cross_user_pairs_prints_instead_of_crashing(self, tmp_path, capsys):
        """One label cannot pair with anything, so sigma is undefined by construction — the same state
        a single-labeller city reaches."""
        cn.main([str(self._one_label(tmp_path)), '--fetched', '2026-08-11'])
        out = capsys.readouterr().out
        assert 'pairs 0' in out
        assert 'n/a' in out, 'undefined sigma must print as n/a'

    def test_sigma_from_pairs_still_reports_its_nulls(self):
        """The contract the print path has to tolerate, pinned at the source."""
        empty = cn.sigma_from_pairs(pd.DataFrame(
            columns=['cluster_id', 'pano_id', 'label_type', 'el_mean', 'd_az', 'd_el', 'd_total']))
        assert empty['n_pairs'] == 0
        assert empty['sigma_az_deg'] is None and empty['sigma_el_deg'] is None

    def test_a_real_pair_still_prints_a_number(self, tmp_path, capsys):
        """Discrimination: two users on one object must yield a printed sigma, not 'n/a'."""
        base = {
            'label_type': 'CurbRamp',
            'time_created': int(pd.Timestamp('2025-01-01', tz='UTC').value // 10 ** 6),
            'heading': 0.0, 'pitch': -20.0, 'zoom': 1.0,
            'canvas_x': 360.0, 'canvas_y': 240.0, 'canvas_width': 720.0, 'canvas_height': 480.0,
            'pano_id': 'p1', 'pano_width': 8192.0, 'pano_height': 4096.0, 'camera_heading': 0.0,
        }
        _rawlabels_csv(tmp_path, [
            dict(base, label_id=1, user_id='u1', pano_x=4096.0, pano_y=2048.0),
            dict(base, label_id=2, user_id='u2', pano_x=4100.0, pano_y=2052.0),
        ])
        cn.main([str(tmp_path), '--fetched', '2026-08-11'])
        overall = capsys.readouterr().out.splitlines()[0]
        # Scoped to the overall line: the per-band and validated-only groups are legitimately empty
        # on a two-label fixture, and 'n/a' is the right answer for them.
        assert overall.startswith('pairs 1 ')
        assert 'n/a' not in overall, overall


class TestPhotometaCensusMain:
    """Its two crashing prints are the summary lines — one after a live network census, one on the
    offline --resummarize path. Only the offline path is drivable in a network-free suite, and it is
    the same summarize() output either way."""

    @staticmethod
    def _census(tmp_path, records, name='census.json'):
        path = tmp_path / name
        path.write_text(json.dumps({'records': records}), encoding='utf-8')
        return path

    @staticmethod
    def _record(**over):
        """A census record with the keys `summarize` actually reads (served_*, not width/height)."""
        rec = {'pano_id': 'p1', 'city': 'c', 'era': 'post179', 'found': True,
               'served_width': 16384, 'served_height': 8192,
               'stored_width': 16384, 'stored_height': 8192,
               'has_depth': True, 'pitch_deg': 1.0, 'roll_deg': 0.5, 'capture_date': '2021-06'}
        rec.update(over)
        return rec

    def test_a_census_where_nothing_is_alive_resummarizes_instead_of_crashing(self, tmp_path, capsys):
        """No alive pano ⇒ nothing comparable and no tilt, so dims_drift, depth and all six tilt
        quantiles are None at once. This is the all-dead-legacy-sample case."""
        census = self._census(tmp_path, [
            self._record(pano_id='p1', era='legacy', found=False, served_width=None,
                         served_height=None, pitch_deg=None, roll_deg=None, has_depth=None),
            self._record(pano_id='p2', era='legacy', found=False, served_width=None,
                         served_height=None, pitch_deg=None, roll_deg=None, has_depth=None),
        ])
        pc.main(['--resummarize', str(census)])
        out = capsys.readouterr().out
        assert 'alive 0.0%' in out
        assert out.count('n/a') >= 3, out

    def test_the_tilt_quantiles_are_null_when_no_alive_pano_carries_tilt(self, tmp_path, capsys):
        """The subtler reachable case: panos are alive, so dims and depth resolve, but photometa
        returned no pitch/roll. `_tilt_stats`' `q` then returns None six times while the other two
        percentages are real numbers — so this is the case a blanket 'everything is None' fixture
        would miss."""
        census = self._census(tmp_path, [self._record(pitch_deg=None, roll_deg=None)])
        pc.main(['--resummarize', str(census)])
        out = capsys.readouterr().out
        assert 'alive 100.0%' in out
        assert 'dims-drift 0.0%' in out, 'the defined percentages must still print'
        assert 'n/a' in out

    def test_a_healthy_census_prints_real_tilt_numbers(self, tmp_path, capsys):
        """Discrimination: the guard must not blank a census that did resolve tilt."""
        census = self._census(tmp_path, [
            self._record(pano_id=f'p{i}', pitch_deg=1.0 + i, roll_deg=0.5 * i) for i in range(5)
        ])
        pc.main(['--resummarize', str(census)])
        out = capsys.readouterr().out
        assert 'n/a' not in out, out
        assert 'alive 100.0%' in out


class TestStoreCoverageSummaryPrint:
    """Every percentage it prints comes from `_pct`, which returns None on a zero denominator. The
    print sits after the live store probe, so the whole probe is what a TypeError discards."""

    def test_pct_returns_none_on_a_zero_denominator(self):
        assert sc._pct(0, 0) is None
        assert sc._pct(1, 2) == 50.0

    @staticmethod
    def _census_rec(pid, alive, dims=(16384, 8192)):
        """A photometa-census record, the shape `sample_from_census` reads — it keys on `found` and
        derives `alive_at_google` itself."""
        return {'pano_id': pid, 'city': 'c', 'era': 'mid', 'found': alive,
                'stored_width': dims[0], 'stored_height': dims[1],
                'served_width': dims[0] if alive else None,
                'served_height': dims[1] if alive else None}

    @classmethod
    def _run(cls, tmp_path, census_records, on_store=()):
        """Drive the REAL `main()`, not a copy of its print block.

        Reimplementing the print here is what would make this class useless against the defect: a
        revert of store_coverage.py's own f-strings would leave a hand-copied print green. `probe`
        touches only the local filesystem, so the whole run is offline — an absent file simply is a
        store miss, which is also the state that produces the nulls.
        """
        census = tmp_path / 'census.json'
        census.write_text(json.dumps({'records': census_records}), encoding='utf-8')
        store_root = tmp_path / 'store'
        store_root.mkdir(exist_ok=True)
        for pid, dims in on_store:
            d = store_root / 'c' / pid[:2]
            d.mkdir(parents=True, exist_ok=True)
            write_jpeg(d / (pid + '.jpg'), dims)
        buf = io.StringIO()
        with redirect_stdout(buf):
            sc.main([str(store_root), '--census', str(census), '--probed', '2026-08-11'])
        summary = sc.summarize(
            sc.probe(str(store_root), sc.sample_from_census({'records': census_records})))
        return summary, buf.getvalue()

    def test_a_sample_with_no_dead_panos_yields_a_null_coverage(self, tmp_path):
        """Reachable the moment a census sample happens to be all-alive — and `summarize` is right to
        answer null: 0 of 0 is not 0%."""
        s, out = self._run(tmp_path, [self._census_rec('p1', True)],
                           on_store=[('p1', (2048, 1024))])
        assert s['dead_at_google']['n'] == 0
        assert s['dead_at_google']['on_store_pct'] is None
        assert 'n/a' in out, out

    def test_an_empty_census_yields_a_null_overall_coverage(self, tmp_path):
        """`overall.on_store_pct` has the whole sample as its denominator, so it is null only when the
        census itself is empty — reachable by pointing --census at a census that filtered to nothing.
        Without this case the overall line's guard is the one the battery cannot kill."""
        s, out = self._run(tmp_path, [])
        assert s['n_sampled'] == 0
        assert s['overall']['on_store_pct'] is None
        assert 'on store n/a%' in out, out

    def test_a_sample_with_no_readable_frames_yields_a_null_match_rate(self, tmp_path):
        """The other reachable null on the same line: nothing on the store means no JPEG header was
        read, so the frame comparison has a zero denominator while coverage is a real 0.0%."""
        s, out = self._run(tmp_path, [self._census_rec('p1', False)])
        assert s['overall']['on_store_pct'] == 0.0, 'coverage is defined and zero'
        assert s['frame_vs_gsv_data']['match_pct'] is None, 'the match rate is undefined'
        assert 'on store 0.0%' in out and 'n/a% match' in out, out

    def test_a_sample_with_both_halves_prints_real_percentages(self, tmp_path):
        """Discrimination: nothing here is undefined, so nothing may print as n/a."""
        s, out = self._run(tmp_path,
                           [self._census_rec('p1', True, (2048, 1024)),
                            self._census_rec('p2', False, (2048, 1024))],
                           on_store=[('p1', (2048, 1024)), ('p2', (2048, 1024))])
        assert s['dead_at_google']['on_store_pct'] == 100.0
        assert 'n/a' not in out, out


class TestOneDefinition:
    """The review's finding #11 applied to itself: `offaxis_covariate` grew a local copy of both
    helpers, and the fix would have added two more copies to the other three scripts."""

    def test_every_study_script_shares_one_implementation(self):
        import offaxis_covariate as oc
        assert oc.fmt is studyfmt.fmt
        assert oc.num is studyfmt.num
        assert cn.fmt is studyfmt.fmt
        assert pc.fmt is studyfmt.fmt
        assert sc.fmt is studyfmt.fmt

    def test_no_script_redeclares_them(self):
        """Every script in reports/scripts/, discovered from the directory rather than listed.

        The list was four filenames typed by hand, and it went stale on the very next script
        added: mapillary_census.py imports studyfmt and calls num() eleven times, and neither
        assertion here touched it — a local `def num(...)` there passed the suite, and so would
        any future script. CLAUDE.md states the rule as "there is one definition of each and no
        script may grow a local copy (a test asserts that)", so the test has to hold for scripts
        nobody has written yet.

        Scope is every script, not just current studyfmt importers: a script that declares its
        own _num WITHOUT importing studyfmt is precisely the violation, and filtering to importers
        would make it invisible.
        """
        scripts = sorted(f for f in os.listdir(SCRIPTS)
                         if f.endswith('.py') and f != 'studyfmt.py')
        assert len(scripts) >= 10, f'expected the study scripts, found {scripts}'
        assert 'mapillary_census.py' in scripts, 'the script the hardcoded list missed'
        for name in scripts:
            with open(os.path.join(SCRIPTS, name), encoding='utf-8') as f:
                text = f.read()
            for decl in ('def _fmt(', 'def _num(', 'def fmt(', 'def num('):
                assert decl not in text, f'{name} declares its own {decl}'
