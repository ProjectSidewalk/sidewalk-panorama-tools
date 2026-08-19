"""Tests for reports/scripts/corpus_sample.py — the Phase 2 corpus draw.

The draw is the one artifact in this work package that annotation labour is spent against, and a
defect in it is not recoverable by re-running: whatever it selects is what gets annotated, and a
stratum it silently under-fills is a study column that turns out not to be estimable *after* the
gold standard exists. So the invariants pinned here are mostly about what the draw must never do —
straddle the tune/eval split across a pano, exceed the per-pano cap, pool two imagery rigs, or
report a population weight for a cell it drew nothing from.

Rows are synthesized THROUGH the real projection (`pov_replay`) rather than hand-written: with the
click at canvas centre, `pov_if_centered` returns (heading, pitch) exactly, so feeding the result
back through `pano_xy_from_pov` produces a row whose stored pano_x/pano_y replay exactly by
construction. That matters because `exact_y` is the corpus's sharpest exclusion — a fixture with
hand-typed pixels would fail it for reasons unrelated to what each test is about, and the natural
fix (relaxing the exclusion in the fixture) would quietly test a corpus nobody draws.
"""

import hashlib
import os
import sys

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for p in (REPO_ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import corpus_sample as cs  # noqa: E402
import era_replay_study  # noqa: E402
import pov_replay  # noqa: E402
import rawlabels  # noqa: E402

CANVAS = (720.0, 480.0)
STD_W, STD_H = 16384.0, 8192.0

# Interior band values, deliberately away from the cut edges (5/15/30): a fixture sitting exactly on
# a boundary would make these tests assert pd.cut's inclusivity rather than the draw's behaviour.
BAND_DEPRESSION = {'<5': 2.0, '5-15': 10.0, '15-30': 20.0, '>30': 40.0}

# One timestamp strictly inside each era-quality stratum.
QUALITY_DATE = {
    'legacy': '2020-06-01',
    'mid': '2022-06-01',
    'window': '2024-01-15',
    'post_fix': '2025-06-01',
}


def synth_row(label_id, pano_id, label_type, depression, dbear, quality,
              height=STD_H, width=STD_W, zoom=1.0, x_mismatch=False, tags='[]',
              camera_pitch=0.5, camera_roll=np.nan):
    """One rawLabels-shaped row that replays exactly, at a chosen band/bearing/era-quality.

    `dbear` is Δb, and it lands there by construction: with the canvas centred the replayed POV
    heading IS the stored viewport heading, and with camera_heading 0 the pano raster's centre column
    looks along it, so Δb = heading. `x_mismatch` perturbs camera_heading only — that breaks exact_x
    while leaving exact_y intact, which is the `x_only` staleness class the corpus keeps.
    """
    cw, ch = CANVAS
    pitch = -float(depression)
    pov_h, pov_p = pov_replay.pov_if_centered(cw / 2, ch / 2, float(dbear), pitch, zoom, cw, ch)
    px, py = pov_replay.pano_xy_from_pov(pov_h, pov_p, 0.0, width, height)
    return {
        'label_id': int(label_id), 'user_id': f'u{label_id % 7}', 'pano_id': str(pano_id),
        'label_type': label_type, 'severity': 3.0,
        'time_created': pd.Timestamp(QUALITY_DATE[quality], tz='UTC'),
        'tags': tags, 'correct': True, 'agree_count': 2, 'disagree_count': 0, 'unsure_count': 0,
        'image_capture_date': '2019-06', 'heading': float(dbear), 'pitch': pitch, 'zoom': zoom,
        'canvas_x': cw / 2, 'canvas_y': ch / 2, 'canvas_width': cw, 'canvas_height': ch,
        'pano_x': float(px), 'pano_y': float(py), 'pano_width': width, 'pano_height': height,
        'camera_heading': 1.0 if x_mismatch else 0.0,
        'camera_pitch': camera_pitch, 'camera_roll': camera_roll,
        'latitude': 47.6, 'longitude': -122.3, 'pano_source': 'gsv',
    }


def frame(rows, city='testville'):
    """A prepared-ready frame. `city` is not optional in the real pipeline and is not optional here:
    label_id restarts at 1 in every deployment, so there is no scalar label identity without it."""
    df = pd.DataFrame(rows)
    if len(df) and 'city' not in df:
        df['city'] = city
    return df


def wide_frame(n_per_cell=12, types=('CurbRamp', 'NoCurbRamp', 'Obstacle', 'SurfaceProblem',
                                     'Signal', 'Other', 'Crosswalk', 'NoSidewalk')):
    """A frame with every (band x quality x type) cell populated `n_per_cell` deep.

    Each label gets its own pano by default, so the per-pano cap is not what limits any draw here;
    tests that care about the cap build panos explicitly.
    """
    rows, lid = [], 1
    for band, dep in BAND_DEPRESSION.items():
        for quality in cs.QUALITY_LEVELS:
            for ltype in types:
                for k in range(n_per_cell):
                    rows.append(synth_row(lid, f'p{lid:05d}', ltype, dep,
                                          dbear=(k * 37) % 350 - 175, quality=quality))
                    lid += 1
    return frame(rows)


def write_city_csv(path, rows):
    """A rawLabels CSV the real loader will accept: every STUDY_COLUMNS field, header included.

    Handles the zero-row case, because one real deployment (winterthur-infra3d) serves exactly that
    and the frame builder has to survive it.
    """
    cols = rawlabels.STUDY_COLUMNS + ['pano_source']
    df = pd.DataFrame(rows, columns=cols)
    if rows:
        df['time_created'] = pd.to_datetime(df['time_created']).astype('int64') // 10 ** 6
    df.to_csv(path, index=False)
    return path


class TestSpecConstants:
    """Every number here is REGISTERED in prereg §3, so it is pinned to its literal value.

    Not ceremony: the tests below that check "every cell reaches the target" read `cs.CELL_TARGET`,
    so they pass for any value of it — a mutation battery confirmed that editing the constant from 6
    to 5 left the whole suite green. A registered quantity needs one assertion that does not go
    through the constant, or the draw can silently stop matching the document it is implementing.
    """

    def test_the_registered_targets(self):
        assert cs.CELL_TARGET == 6
        assert cs.MAX_LABELS_PER_PANO == 3
        assert cs.RESOLUTION_TARGET == 60
        assert cs.MISMATCH_TARGET == 30
        assert cs.STANDARD_HEIGHT == 8192.0

    def test_the_contrast_target_exceeds_the_estimability_gate(self):
        """§3 draws 80 panos; §2.3 declares the column not estimable below 60. The margin is what
        absorbs labels lost at annotation to object-absent/ambiguous flags — drawing exactly to the
        gate would mean any annotation loss makes the robustness column unrunnable."""
        import mapillary_census
        assert cs.CONTRAST_PANOS_TARGET == 80
        assert mapillary_census.WITHIN_PANO_PANOS_REQUIRED == 60
        assert cs.CONTRAST_PANOS_TARGET > mapillary_census.WITHIN_PANO_PANOS_REQUIRED


class TestEraQuality:
    """The four-level stratum prereg §3 draws on, which is NOT the three-level `era`: it adds the
    placement-record bug window as its own level, because that window is where the canvas/POV record
    is untrustworthy and the draw has to be able to over- or under-weight it deliberately."""

    def test_each_level_gets_its_own_dates(self):
        t = pd.Series([pd.Timestamp(QUALITY_DATE[q], tz='UTC') for q in cs.QUALITY_LEVELS])
        assert list(era_replay_study.era_quality(t)) == list(cs.QUALITY_LEVELS)

    def test_boundaries_are_lower_inclusive(self):
        """Each boundary belongs to the era it opens. An off-by-one here silently moves thousands of
        labels between strata and nothing else would notice."""
        edges = [(rawlabels.LEGACY_END, 'mid'),
                 (rawlabels.EVO179, 'window'),
                 (era_replay_study.BUG_WINDOW_END, 'post_fix')]
        for edge, opens in edges:
            t = pd.Series([edge - pd.Timedelta(microseconds=1), edge])
            got = list(era_replay_study.era_quality(t))
            assert got[1] == opens, (edge, got)
            assert got[0] != opens, (edge, got)

    def test_a_missing_timestamp_is_not_a_quality_level(self):
        """Blank must stay blank. Every comparison against NaT is False, so seeding the series with
        'mid' and overwriting the other three files a label with no `time_created` into a real
        stratum — where it fills a corpus cell it does not belong in and is counted in `by_quality`.
        Same rule as rawlabels._FLOAT_COLUMNS: a lookup that never resolved must never read as an
        answer."""
        t = pd.Series(pd.to_datetime([None, '2025-01-01'], utc=True))
        got = era_replay_study.era_quality(t)
        assert got.iloc[0] is None
        assert got.iloc[1] == 'post_fix', 'discrimination: a real timestamp still buckets'

    def test_an_undated_label_is_not_corpus_eligible(self):
        """The other half: `era_quality` reporting None only helps if the draw refuses the row. A
        label with no timestamp replays fine and lands in a band, so nothing else in `corpus_eligible`
        would catch it — it simply has no answer for one of the three axes the draw stratifies on."""
        rows = [synth_row(1, 'p1', 'CurbRamp', 10.0, 0.0, 'post_fix'),
                synth_row(2, 'p2', 'CurbRamp', 10.0, 0.0, 'post_fix')]
        rows[0]['time_created'] = pd.NaT
        prepared = cs.prepare(frame(rows))
        assert list(prepared['corpus_eligible']) == [False, True]
        # pd.isna rather than `is None`: the column round-trips through to_numpy() on the way into the
        # frame, which normalises None to NaN. The guard in corpus_eligible is pd.notna for exactly
        # that reason — "blank" has two spellings here and both have to mean blank.
        assert pd.isna(prepared['quality'].iloc[0])
        assert prepared['exact_y'].iloc[0], 'discrimination: nothing else in the mask rejects it'
        assert pd.notna(prepared['band'].iloc[0]), '...including the band, which does have a guard'

    def test_the_window_is_a_strict_subset_of_post179(self):
        """`window` and `post_fix` must partition what `add_era` calls post179 — if they did not, the
        two era columns would disagree about the same label and every cross-tab would be wrong."""
        t = pd.Series(pd.to_datetime(['2019-01-01', '2022-01-01', '2023-06-01', '2025-01-01'],
                                     utc=True))
        era = rawlabels.add_era(pd.DataFrame({'time_created': t}))['era']
        quality = era_replay_study.era_quality(t)
        assert list(era[quality.isin(['window', 'post_fix'])]) == ['post179', 'post179']
        assert set(era[quality == 'mid']) == {'mid'}
        assert set(era[quality == 'legacy']) == {'legacy'}


class TestProvenance:
    """rawLabels is a moving target and the six-city snapshot differs from the current cache on every
    file, so a draw that does not record exactly which bytes it read cannot be audited later."""

    def test_records_hash_and_row_count_per_city(self, tmp_path):
        p = write_city_csv(tmp_path / 'testville.csv',
                           [synth_row(i, f'p{i}', 'CurbRamp', 10.0, 0.0, 'mid') for i in range(5)])
        prov = cs.provenance([str(p)])
        assert len(prov) == 1
        rec = prov[0]
        assert rec['city'] == 'testville'
        assert rec['n_rows'] == 5, 'the header must not be counted as a label'
        assert rec['sha256'] == hashlib.sha256(open(p, 'rb').read()).hexdigest()
        assert rec['bytes'] == os.path.getsize(p)

    def test_hash_changes_when_a_single_field_changes(self, tmp_path):
        """Discrimination: provenance is only worth recording if it can tell two fetches apart."""
        rows = [synth_row(i, f'p{i}', 'CurbRamp', 10.0, 0.0, 'mid') for i in range(5)]
        a = cs.provenance([str(write_city_csv(tmp_path / 'a.csv', rows))])[0]
        rows[2]['pano_x'] += 1.0
        b = cs.provenance([str(write_city_csv(tmp_path / 'b.csv', rows))])[0]
        assert a['sha256'] != b['sha256']
        assert a['n_rows'] == b['n_rows']


class TestFramePurity:
    """Every study in this repo globs a directory, and `fetch_rawlabels.py` keeps three cache trees
    apart by hand for exactly that reason. The sampler is the first consumer that can *check* it, so
    it does: pooling a Mapillary or infra3d deployment into the GSV frame would change the rig under
    the corpus and silently move every population weight."""

    def _city(self, tmp_path, name, source, n=4):
        rows = [synth_row(i, f'{name}{i}', 'CurbRamp', 10.0, 0.0, 'mid') for i in range(n)]
        for r in rows:
            r['pano_source'] = source
        return str(write_city_csv(tmp_path / f'{name}.csv', rows))

    def test_loads_a_single_source_frame(self, tmp_path):
        paths = [self._city(tmp_path, 'alpha', 'gsv'), self._city(tmp_path, 'beta', 'gsv')]
        df, prov, excluded = cs.load_frame(paths, source='gsv')
        assert len(df) == 8
        assert set(df['city']) == {'alpha', 'beta'}
        assert len(prov) == 2
        assert excluded == []

    def test_a_foreign_rig_is_excluded_and_recorded(self, tmp_path):
        """The rig comes from `pano_source`, not from which directory the file sits in — so pointing
        this at the all-deployments cache draws the GSV arm instead of silently pooling Richmond's
        Mapillary rig into it. Excluded deployments are recorded, because the failure mode this
        guards against is a corpus that quietly contains a second camera, and a dropped file that
        appears nowhere in the artifact is the same failure with the evidence removed.
        """
        paths = [self._city(tmp_path, 'alpha', 'gsv'),
                 self._city(tmp_path, 'richmond', 'mapillary'),
                 self._city(tmp_path, 'zurich-infra3d', 'infra3d')]
        df, prov, excluded = cs.load_frame(paths, source='gsv')
        assert set(df['city']) == {'alpha'}
        assert {r['city'] for r in prov} == {'alpha'}
        assert {e['city']: e['rig'] for e in excluded} == {'richmond': 'mapillary',
                                                           'zurich-infra3d': 'infra3d'}
        assert all(e['n_rows'] == 4 for e in excluded), 'the artifact must say how much was dropped'

    def test_refuses_a_deployment_that_mixes_rigs(self, tmp_path):
        """One city carrying two rigs means pano_source is unreliable there, and no per-row filter can
        be trusted to repair it — so this raises rather than guessing."""
        rows = [synth_row(i, f'mix{i}', 'CurbRamp', 10.0, 0.0, 'mid') for i in range(4)]
        rows[0]['pano_source'] = 'mapillary'
        path = str(write_city_csv(tmp_path / 'mixed.csv', rows))
        with pytest.raises(ValueError, match='mapillary'):
            cs.load_frame([path], source='gsv')

    def test_selects_the_requested_rig(self, tmp_path):
        """The Mapillary arm is drawn by the same code path — asking for it must not require a
        second implementation, since a divergent copy is how the two arms would stop being
        comparable."""
        paths = [self._city(tmp_path, 'alpha', 'gsv'),
                 self._city(tmp_path, 'richmond', 'mapillary')]
        df, prov, excluded = cs.load_frame(paths, source='mapillary')
        assert set(df['city']) == {'richmond'}
        assert [e['city'] for e in excluded] == ['alpha']

    def test_excludes_deployments_that_are_not_populations(self, tmp_path):
        """validation-study is a research deployment and la-piedad-old is superseded by la-piedad;
        including either would put non-city labels into a population weight, and la-piedad twice."""
        paths = [self._city(tmp_path, 'alpha', 'gsv'),
                 self._city(tmp_path, 'validation-study', 'gsv'),
                 self._city(tmp_path, 'la-piedad-old', 'gsv')]
        df, prov, excluded = cs.load_frame(paths, source='gsv')
        assert set(df['city']) == {'alpha'}
        assert {r['city'] for r in prov} == {'alpha'}, 'excluded files must not be in provenance'
        assert {e['city'] for e in excluded} == {'validation-study', 'la-piedad-old'}
        assert cs.NOT_A_POPULATION == frozenset({'validation-study', 'la-piedad-old'})

    def test_an_empty_export_is_dropped_not_fatal(self, tmp_path):
        """winterthur-infra3d serves a 0-label export. A frame builder that dies on it cannot be run
        over the full deployment roster at all."""
        paths = [self._city(tmp_path, 'alpha', 'gsv'), self._city(tmp_path, 'empty', 'gsv', n=0)]
        df, prov, excluded = cs.load_frame(paths, source='gsv')
        assert set(df['city']) == {'alpha'}
        assert {r['city'] for r in prov} == {'alpha'}


class TestEligibility:
    """§3's pre-specified exclusions, and the distinction the pre-registration predates: the CORPUS is
    8 types (Study 2 sizes crops for Crosswalk and NoSidewalk, which have real consumers), while
    Study 1's MEASURABLE set applies the referent rule on top and keeps 6."""

    def test_a_clean_row_is_both_corpus_and_measurable(self):
        p = cs.prepare(frame([synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid')]))
        assert bool(p['corpus_eligible'].iloc[0])
        assert bool(p['measurable'].iloc[0])
        assert p['band'].iloc[0] == '5-15'
        assert p['quality'].iloc[0] == 'mid'

    def test_occlusion_is_out_of_the_corpus_entirely(self):
        p = cs.prepare(frame([synth_row(1, 'pa', 'Occlusion', 10.0, 0.0, 'mid')]))
        assert not bool(p['corpus_eligible'].iloc[0])

    def test_extended_features_are_in_the_corpus_but_not_measurable(self):
        """The referent rule is about placement-measurability, not crop-corpus membership. Reading it
        as "types to drop" would remove 22.6% of the population from the sizing study."""
        for ltype in ('Crosswalk', 'NoSidewalk'):
            p = cs.prepare(frame([synth_row(1, 'pa', ltype, 10.0, 0.0, 'mid')]))
            assert bool(p['corpus_eligible'].iloc[0]), ltype
            assert not bool(p['measurable'].iloc[0]), ltype

    def test_a_region_tagged_surface_problem_is_not_measurable(self):
        p = cs.prepare(frame([
            synth_row(1, 'pa', 'SurfaceProblem', 10.0, 0.0, 'mid', tags='[brick/cobblestone]'),
            synth_row(2, 'pb', 'SurfaceProblem', 10.0, 0.0, 'mid', tags='[cracks]')]))
        assert list(p['measurable']) == [False, True]
        assert list(p['corpus_eligible']) == [True, True]

    def test_a_stale_vertical_record_is_excluded(self):
        """§3 excludes rows whose recorded frame is not the click-time frame. exact_y is the sharp
        version of that check; perturbing stored pano_y by one pixel must fail it."""
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid'),
                synth_row(2, 'pb', 'CurbRamp', 10.0, 0.0, 'mid')]
        rows[1]['pano_y'] += 1.0
        p = cs.prepare(frame(rows))
        assert list(p['corpus_eligible']) == [True, False]

    def test_an_x_only_mismatch_stays_in(self):
        """58% of record misses are stale only in viewport heading, which the corpus does not read.
        Excluding them would discard the whole replay-mismatch stratum §3 forces — the stratum would
        be defined over rows the exclusion had already removed."""
        p = cs.prepare(frame([synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid', x_mismatch=True)]))
        assert bool(p['corpus_eligible'].iloc[0])
        assert bool(p['exact_y'].iloc[0]) and not bool(p['exact_x'].iloc[0])
        assert bool(p['replay_mismatch'].iloc[0])

    def test_a_pano_y_outside_the_frame_is_excluded(self):
        """The two corrupt negative-pano_y rows found corpus-wide (Seattle 231546/233419) must not
        reach an annotator: there is no imagery at a negative row."""
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid')]
        rows[0]['pano_y'] = -5.0
        assert not bool(cs.prepare(frame(rows))['corpus_eligible'].iloc[0])

    def test_the_band_guard_is_what_rejects_the_frame_edge(self):
        """The load-bearing half of why `prepare` needs no in-frame guard.

        A label at pano_y == 0 replays exactly — pitch +90 gives pov_pitch 90 and replay_y 0 — so
        exact_y does NOT exclude it. What excludes it is the band: depression is then exactly -90,
        and BAND_EDGES is left-exclusive there, so the row has no band. Discovered by a mutation
        battery, which found that removing the frame check changed nothing while removing the band
        check would have admitted rows carrying a NaN stratum straight into the draw's cell keys.
        """
        rows = [synth_row(1, 'pa', 'CurbRamp', -90.0, 0.0, 'mid'),
                synth_row(2, 'pb', 'CurbRamp', 10.0, 0.0, 'mid')]
        p = cs.prepare(frame(rows))
        assert p['pano_y'].iloc[0] == 0.0
        assert bool(p['exact_y'].iloc[0]), 'the top row replays exactly; exact_y cannot reject it'
        assert pd.isna(p['band'].iloc[0])
        assert list(p['corpus_eligible']) == [False, True]

    def test_exact_y_implies_the_pano_y_bounds(self):
        """The other half: no row outside the frame can ever satisfy exact_y.

        The replay maps an arcsin-bounded pitch onto [0, H], so a stored pano_y outside the frame can
        never equal its own replay. Pinned as an implication rather than left implicit: it is part of
        the justification for the absent term, and if a future change to the projection ever let
        replay_y leave [0, H], the guard would have to come back and this is the test that would say
        so.
        """
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid'),
                synth_row(2, 'pb', 'CurbRamp', 10.0, 0.0, 'mid'),
                synth_row(3, 'pc', 'CurbRamp', 10.0, 0.0, 'mid')]
        rows[0]['pano_y'] = -1.0
        rows[1]['pano_y'] = rows[1]['pano_height'] + 1.0
        p = cs.prepare(frame(rows))
        assert list(p['exact_y']) == [False, False, True]
        y, h = p['pano_y'].to_numpy(float), p['pano_height'].to_numpy(float)
        out_of_frame = (y < 0) | (y > h)
        assert not (out_of_frame & p['exact_y'].to_numpy()).any()
        assert list(p['corpus_eligible']) == [False, False, True]

    def test_tutorial_panos_are_excluded(self):
        p = cs.prepare(frame([synth_row(1, 'tutorial', 'CurbRamp', 10.0, 0.0, 'mid')]))
        assert not bool(p['corpus_eligible'].iloc[0])

    def test_disagreed_labels_are_kept_and_flagged(self):
        """§3: consumer pipelines do not filter these, so the study measures them rather than
        sanitizing them away."""
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid')]
        rows[0].update(agree_count=0, disagree_count=3)
        p = cs.prepare(frame(rows))
        assert bool(p['corpus_eligible'].iloc[0])
        assert bool(p['disputed'].iloc[0])


class TestDraw:

    def test_respects_the_per_pano_cap(self):
        """One pano decoded for many labels is cheap for the cropper but ruinous for a study: it
        concentrates the corpus on a handful of scenes and collapses the effective sample size."""
        rows = [synth_row(i, 'shared', 'CurbRamp', 10.0, (i * 40) % 350 - 175, 'mid')
                for i in range(20)]
        drawn = cs.draw(cs.prepare(frame(rows)), seed=1)
        assert len(drawn) <= cs.MAX_LABELS_PER_PANO
        assert cs.MAX_LABELS_PER_PANO == 3

    def test_no_label_is_drawn_twice(self):
        drawn = cs.draw(cs.prepare(wide_frame()), seed=1)
        assert drawn['label_uid'].is_unique

    def test_two_deployments_sharing_label_ids_both_get_drawn(self):
        """Regression, and the most expensive bug in this module's history.

        `label_id` restarts at 1 in every deployment: across just seattle-wa, columbus-oh and
        oradell-nj, 90,369 of 316,735 rows share a label_id with a different city. The draw keyed its
        selection on the bare integer, so one city's label displaced another's — it drew 449 labels
        instead of 763, 314 lost, and left 50 of the 98 occupied strata cells short (22 more never
        occupied at all) while the frame held thousands of candidates for every one of them. Nothing
        failed: the corpus was simply smaller and thinner than the spec, which is exactly the kind of
        defect that is only visible once the annotation is spent.

        (Those are the figures in reports/2026-08-12-corpus-assembly.md §3, over the frame this module
        actually draws — all 49 GSV deployments. This docstring and the comment in `corpus_sample.
        prepare` both used to quote the smaller pre-widening draw instead, so the code and the report
        described the same defect with different numbers and nothing covered the code side.
        `TestTheDefectsAreRecorded.test_the_code_quotes_the_same_cost_as_the_report` pins them
        together now, which is also why the superseded pair is described here rather than restated.)

        The fixture has to be built with care to discriminate at all: two cities sharing label ids
        *within one cell* cannot show the bug, because six distinct ids are drawn either way. What
        exposes it is the same ids in DIFFERENT cells, with exactly the cell target available in each.
        Then the collapsed identity marks alpha's 1-6 as taken, beta's 1-6 all test as already drawn
        and get skipped, and the second cell fills with nothing.
        """
        rows = []
        for city, ltype in (('alpha', 'CurbRamp'), ('beta', 'Obstacle')):
            for lid in range(1, cs.CELL_TARGET + 1):
                r = synth_row(lid, f'{city}-p{lid}', ltype, 10.0, 0.0, 'mid')
                r['city'] = city
                rows.append(r)
        p = cs.prepare(pd.DataFrame(rows))
        assert p['label_uid'].nunique() == 2 * cs.CELL_TARGET, 'the composite identity separates them'
        assert p['label_id'].nunique() == cs.CELL_TARGET, 'the bare integer collides — that is the bug'

        drawn = cs.draw(p, seed=1)
        assert drawn.groupby('city').size().to_dict() == {'alpha': cs.CELL_TARGET,
                                                          'beta': cs.CELL_TARGET}
        assert drawn.groupby('label_type').size().to_dict() == {'CurbRamp': cs.CELL_TARGET,
                                                                'Obstacle': cs.CELL_TARGET}
        assert drawn['label_uid'].is_unique

    def test_prepare_refuses_a_frame_with_no_city(self):
        """Discrimination for the guard: without it, a caller assembling a frame by hand gets the
        collision back silently, and a silently-thin corpus is unrecoverable after annotation."""
        rows = pd.DataFrame([synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid')])
        with pytest.raises(ValueError, match='city'):
            cs.prepare(rows)

    def test_is_deterministic_under_a_seed(self):
        p = cs.prepare(wide_frame())
        a = cs.draw(p, seed=20260812)
        b = cs.draw(p, seed=20260812)
        assert list(a['label_id']) == list(b['label_id'])

    def test_a_different_seed_draws_differently(self):
        """Discrimination: if the seed were ignored, determinism above would pass trivially."""
        p = cs.prepare(wide_frame())
        a = cs.draw(p, seed=1)
        b = cs.draw(p, seed=2)
        assert list(a['label_id']) != list(b['label_id'])

    def test_fills_every_occupied_cell_to_the_target(self):
        p = cs.prepare(wide_frame(n_per_cell=12))
        drawn = cs.draw(p, seed=1)
        counts = drawn.groupby(['band', 'quality', 'label_type'], observed=True).size()
        assert set(counts) == {cs.CELL_TARGET}
        assert len(counts) == 4 * 4 * 8

    def test_a_thin_cell_contributes_what_it_has(self):
        """A cell with fewer labels than the target must not fail the draw, and must not be topped up
        from a neighbouring cell — that would misreport the stratum it was drawn from."""
        rows = [synth_row(i, f'p{i}', 'CurbRamp', BAND_DEPRESSION['5-15'], 0.0, 'mid')
                for i in range(2)]
        rows += [synth_row(100 + i, f'q{i}', 'Obstacle', BAND_DEPRESSION['5-15'], 0.0, 'mid')
                 for i in range(9)]
        drawn = cs.draw(cs.prepare(frame(rows)), seed=1)
        by_type = drawn.groupby('label_type').size()
        assert by_type['CurbRamp'] == 2
        assert by_type['Obstacle'] == cs.CELL_TARGET

    def test_forces_the_resolution_stratum(self):
        """§3's resolution oversample exists because the deployed sizing formula is
        resolution-dependent (1.198x on 8192-height panos). Drawn proportionally, non-8192 panos are
        ~3% of the corpus and the stratum would land at ~20 labels, too few to see it."""
        rows = []
        lid = 1
        for k in range(400):
            rows.append(synth_row(lid, f'std{lid}', 'CurbRamp', 10.0, (k * 31) % 350 - 175, 'mid'))
            lid += 1
        for k in range(90):
            rows.append(synth_row(lid, f'low{lid}', 'CurbRamp', 10.0, (k * 31) % 350 - 175, 'mid',
                                  width=13312.0, height=6656.0))
            lid += 1
        drawn = cs.draw(cs.prepare(frame(rows)), seed=3)
        assert int((drawn['pano_height'] != cs.STANDARD_HEIGHT).sum()) >= cs.RESOLUTION_TARGET

    def test_the_resolution_tally_counts_only_nonstandard_rows(self):
        """The forced strata run in sequence and share one budget of already-selected rows, so the
        resolution phase has to count the *non-8192* rows specifically — not every row taken so far.

        The distinction is invisible in the test above, where every row that phase could take is
        already non-standard. Here the contrast stratum takes 160 standard-height labels first: a
        tally that counts all selections is over target before the resolution phase begins and draws
        none of the low-resolution panos the stratum exists for. The deployed version of this bug was
        subtler — a per-candidate rescan of the selected rows — and it showed up as the resolution
        stratum overshooting to 96/60 on the real frame.
        """
        rows, lid = [], 1
        for pano in range(100):                        # standard height, two separated labels each
            for dbear in (-70.0, 70.0):
                rows.append(synth_row(lid, f'pair{pano}', 'CurbRamp', 10.0, dbear, 'post_fix'))
                lid += 1
        for k in range(90):                            # the low-resolution supply
            rows.append(synth_row(lid, f'low{lid}', 'Obstacle', 20.0, (k * 31) % 350 - 175, 'mid',
                                  width=13312.0, height=6656.0))
            lid += 1
        drawn = cs.draw(cs.prepare(frame(rows)), seed=11)
        assert int((drawn['pano_height'] != cs.STANDARD_HEIGHT).sum()) >= cs.RESOLUTION_TARGET
        assert int((drawn['pano_height'] == cs.STANDARD_HEIGHT).sum()) >= 100, \
            'the contrast stratum must still have run first, or this fixture proves nothing'

    def test_forces_the_replay_mismatch_stratum(self):
        rows = []
        lid = 1
        for k in range(400):
            rows.append(synth_row(lid, f'ok{lid}', 'CurbRamp', 10.0, (k * 31) % 350 - 175, 'mid'))
            lid += 1
        for k in range(50):
            rows.append(synth_row(lid, f'bad{lid}', 'CurbRamp', 10.0, (k * 31) % 350 - 175,
                                  'window', x_mismatch=True))
            lid += 1
        drawn = cs.draw(cs.prepare(frame(rows)), seed=4)
        assert int(drawn['replay_mismatch'].sum()) >= cs.MISMATCH_TARGET

    def test_forces_the_within_pano_contrast_stratum(self):
        """§2.3's robustness column is only estimable if enough panos carry two study labels at
        separated bearings; §3 forces the stratum precisely so the check cannot turn out to be
        unrunnable after the annotation is paid for."""
        rows, lid = [], 1
        for pano in range(120):
            for dbear in (-70.0, 70.0):
                rows.append(synth_row(lid, f'pair{pano}', 'CurbRamp', 10.0, dbear, 'post_fix'))
                lid += 1
        for k in range(200):
            rows.append(synth_row(lid, f'solo{lid}', 'Obstacle', 20.0, 0.0, 'mid'))
            lid += 1
        drawn = cs.draw(cs.prepare(frame(rows)), seed=5)
        assert cs.contrast_pano_count(drawn) >= cs.CONTRAST_PANOS_TARGET

    def test_a_pair_across_the_seam_is_not_separated(self):
        """Δb wraps, and a pair straddling the seam is the case that exposes it.

        Two labels at Δb -170 and +170 are 20 deg apart in the world — column 0 and column
        pano_width are the same place — so they carry almost no within-pano contrast. A separation
        computed as a plain difference calls them 340 deg apart and admits the pano, which would fill
        §2.3's stratum with pairs that cannot identify the tilt term at all. The stratum would look
        provisioned and estimate nothing.
        """
        rows, lid = [], 1
        for pano, bearings in enumerate([(-170.0, 170.0), (-70.0, 70.0)]):
            for dbear in bearings:
                rows.append(synth_row(lid, f'p{pano}', 'CurbRamp', 10.0, dbear, 'mid'))
                lid += 1
        p = cs.prepare(frame(rows))
        seam, wide = p[p['pano_id'] == 'p0'], p[p['pano_id'] == 'p1']
        assert cs.separated_pairs(seam) == []
        assert len(cs.separated_pairs(wide)) == 1
        assert set(cs.separated_pairs(wide)[0]) == set(wide['label_uid'])
        assert cs.contrast_pano_count(p) == 1

    def test_reports_a_shortfall_instead_of_inventing_one(self):
        """If the frame cannot supply a forced stratum, the draw must say so. Silently returning a
        short corpus is how §2.3 would be reported as "underpowered" when it was never provisioned."""
        rows, lid = [], 1
        for pano in range(10):
            for dbear in (-70.0, 70.0):
                rows.append(synth_row(lid, f'pair{pano}', 'CurbRamp', 10.0, dbear, 'post_fix'))
                lid += 1
        drawn = cs.draw(cs.prepare(frame(rows)), seed=6)
        short = cs.shortfalls(drawn)
        assert short['contrast_panos']['achieved'] == 10
        assert short['contrast_panos']['required'] == cs.CONTRAST_PANOS_TARGET
        assert short['contrast_panos']['shortfall'] == cs.CONTRAST_PANOS_TARGET - 10

    def test_only_draws_corpus_eligible_rows(self):
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid'),
                synth_row(2, 'pb', 'Occlusion', 10.0, 0.0, 'mid'),
                synth_row(3, 'pc', 'CurbRamp', 10.0, 0.0, 'mid')]
        rows[2]['pano_y'] += 1.0
        drawn = cs.draw(cs.prepare(frame(rows)), seed=1)
        assert list(drawn['label_id']) == [1]

    def test_every_drawn_row_carries_its_stratum_roles(self):
        """The manifest has to be able to say why each label is in the corpus; a row drawn to satisfy
        a forced stratum is not interchangeable with one drawn to fill a cell."""
        drawn = cs.draw(cs.prepare(wide_frame()), seed=1)
        assert 'roles' in drawn
        assert all(isinstance(r, str) and r for r in drawn['roles'])
        assert any('cell' in r for r in drawn['roles'])


class TestDrawSummary:
    """The manifest block. Covered at code level as well as pinned against the committed artifact,
    because a committed artifact was generated BY this code — a revert would keep the pin green."""

    def test_it_reports_per_band_counts_under_both_filters(self):
        """Study 1 and Study 2 read different populations, so one per-band breakdown is not enough:
        quoting the corpus figure at a Study 1 power claim overstates every band. Crosswalk is in the
        corpus and out of the measurable set, which is what makes the two differ here."""
        rows, lid = [], 1
        for ltype in ('CurbRamp', 'Crosswalk'):
            for _ in range(9):
                rows.append(synth_row(lid, f'p{lid}', ltype, BAND_DEPRESSION['5-15'], 0.0, 'mid'))
                lid += 1
        p = cs.prepare(frame(rows))
        drawn = cs.assign_split(cs.draw(p, seed=1), seed=1)
        s = cs.draw_summary(drawn, cs.population_weights(p))

        assert s['by_band']['5-15'] == 2 * cs.CELL_TARGET
        assert s['by_band_measurable']['5-15'] == cs.CELL_TARGET, 'Crosswalk is not measurable'
        assert sum(s['by_band'].values()) == s['n_labels']
        assert sum(s['by_band_measurable'].values()) == s['n_measurable']

    def test_the_two_breakdowns_are_equal_when_every_label_is_measurable(self):
        """Discrimination: the split above must come from the referent rule, not from an unrelated
        difference in how the two keys are computed."""
        p = cs.prepare(wide_frame(types=('CurbRamp', 'Obstacle')))
        drawn = cs.draw(p, seed=1)
        s = cs.draw_summary(drawn, cs.population_weights(p))
        assert s['by_band'] == s['by_band_measurable']
        assert s['n_labels'] == s['n_measurable']


class TestContrastCountReplica:
    """`contrast_panos_available` is a vectorized replica of the canonical §2.3 pano predicate, kept
    because the canonical one takes minutes over the frame's 564,337 panos. Same arrangement as
    `clamp_census.predict_crop_size` and CropRunner's scalar original, so it gets the same treatment:
    the replica is pinned against the real thing rather than trusted."""

    @staticmethod
    def _random_frame(rng, n_panos=60):
        rows, lid = [], 1
        for pano in range(n_panos):
            for _ in range(int(rng.integers(1, 5))):
                rows.append(synth_row(lid, f'p{pano}', 'CurbRamp', 10.0,
                                      float(rng.uniform(-180, 180)), 'mid'))
                lid += 1
        return cs.prepare(frame(rows))

    def test_the_replica_matches_the_canonical_predicate(self):
        """Random bearings, many panos, repeated draws — including the seam-straddling and
        near-threshold configurations that a hand-built fixture would never think to include."""
        rng = np.random.default_rng(20260812)
        for _ in range(25):
            p = self._random_frame(rng)
            assert cs.contrast_panos_available(p) == cs.contrast_pano_count(p)

    def test_the_replica_matches_on_clustered_bearings(self):
        """The interesting case for the span argument is bearings bunched near the gate, where the
        widest-gap logic and the pairwise logic could disagree if the equivalence were only
        approximate."""
        rng = np.random.default_rng(7)
        rows, lid = [], 1
        for pano in range(80):
            base = float(rng.uniform(-180, 180))
            for offset in (0.0, float(rng.choice([59.0, 59.9, 60.0, 60.1, 61.0]))):
                rows.append(synth_row(lid, f'p{pano}', 'CurbRamp', 10.0, base + offset, 'mid'))
                lid += 1
        p = cs.prepare(frame(rows))
        assert cs.contrast_panos_available(p) == cs.contrast_pano_count(p)

    def test_the_replica_refuses_a_separation_it_cannot_answer(self):
        """Above 90 deg the span test stops being equivalent: three labels at Δb 0/120/240 occupy a
        240 deg arc while their widest pair is only 120 apart. Raising beats answering wrongly."""
        rows = [synth_row(i + 1, 'pa', 'CurbRamp', 10.0, dbear, 'mid')
                for i, dbear in enumerate((-179.0, -59.0, 61.0))]
        p = cs.prepare(frame(rows))
        assert cs.contrast_pano_count(p, 150.0) == 0
        with pytest.raises(ValueError, match='90'):
            cs.contrast_panos_available(p, 150.0)

    def test_a_single_label_pano_never_counts(self):
        p = cs.prepare(frame([synth_row(1, 'solo', 'CurbRamp', 10.0, 0.0, 'mid')]))
        assert cs.contrast_panos_available(p) == 0
        assert cs.contrast_pano_count(p) == 0


class TestSplit:
    """Tune/eval is split by PANO, never by label. Two labels on one pano share imagery, placement
    culture and camera metadata, so a label-wise split leaks the eval set into tuning and Study 2's
    candidate comparison would be scored on data it was fitted against."""

    def test_a_pano_never_straddles_the_split(self):
        rows, lid = [], 1
        for pano in range(40):
            for _ in range(3):
                rows.append(synth_row(lid, f'p{pano}', 'CurbRamp', 10.0, (lid * 47) % 350 - 175,
                                      'mid'))
                lid += 1
        drawn = cs.assign_split(cs.draw(cs.prepare(frame(rows)), seed=1), seed=7)
        per_pano = drawn.groupby('pano_id')['split'].nunique()
        assert set(per_pano) == {1}

    def test_is_roughly_balanced(self):
        drawn = cs.assign_split(cs.draw(cs.prepare(wide_frame()), seed=1), seed=7)
        share = (drawn['split'] == 'tune').mean()
        assert 0.35 <= share <= 0.65, share
        assert set(drawn['split']) == {'tune', 'eval'}

    def test_is_deterministic_and_seed_sensitive(self):
        drawn = cs.draw(cs.prepare(wide_frame()), seed=1)
        a = cs.assign_split(drawn, seed=7)['split'].tolist()
        b = cs.assign_split(drawn, seed=7)['split'].tolist()
        c = cs.assign_split(drawn, seed=8)['split'].tolist()
        assert a == b
        assert a != c


class TestFrameComparison:
    """The evidence for amendment 2's frame widening. It is computed in the module and committed to the
    manifest rather than worked out in a one-off script, because the report quotes it — and a number in
    a report is the one place in this repo with no compiler and no test behind it."""

    @staticmethod
    def _two_city_frame():
        """`ref` is 60 CurbRamp; `other` is 60 Signal. So the reference half is 100% CurbRamp and the
        whole frame is 50/50 — a comparison with arithmetic simple enough to check by hand."""
        rows, lid = [], 1
        for city, ltype in (('amsterdam', 'CurbRamp'), ('elsewhere', 'Signal')):
            for _ in range(60):
                r = synth_row(lid, f'{city}-p{lid}', ltype, 10.0, 0.0, 'mid')
                r['city'] = city
                rows.append(r)
                lid += 1
        return cs.prepare(pd.DataFrame(rows))

    def test_distribution_is_a_percentage_of_the_corpus_population(self):
        p = self._two_city_frame()
        d = cs.distribution(p, ('label_type',))
        assert d == pytest.approx({'CurbRamp': 50.0, 'Signal': 50.0})
        assert sum(d.values()) == pytest.approx(100.0)

    def test_total_variation_is_half_the_summed_absolute_difference(self):
        """Pinned against a hand-computed value, not against the implementation: TV distance is the
        quantity the report calls "the most any reweighted claim could move", and a sum-not-halved
        version would overstate every one of those claims by 2x."""
        assert cs.total_variation_pct({'a': 100.0}, {'a': 50.0, 'b': 50.0}) == pytest.approx(50.0)
        assert cs.total_variation_pct({'a': 60.0, 'b': 40.0},
                                      {'a': 40.0, 'b': 60.0}) == pytest.approx(20.0)
        assert cs.total_variation_pct({'a': 100.0}, {'a': 100.0}) == pytest.approx(0.0)

    def test_the_comparison_reports_shares_and_ratios_against_the_reference(self):
        p = self._two_city_frame()
        fc = cs.frame_comparison(p, reference=('amsterdam',))
        assert fc['population'] == 'corpus_eligible'
        assert fc['n_reference_corpus_eligible'] == 60
        assert fc['n_frame_corpus_eligible'] == 120
        assert fc['reference_share_pct'] == pytest.approx(50.0)
        assert fc['by_label_type']['CurbRamp']['reference_pct'] == pytest.approx(100.0)
        assert fc['by_label_type']['CurbRamp']['frame_pct'] == pytest.approx(50.0)
        assert fc['by_label_type']['CurbRamp']['ratio'] == pytest.approx(0.5)
        assert fc['total_variation_pct']['label_type'] == pytest.approx(50.0)

    def test_counts_and_shares_are_under_the_same_filter(self):
        """Discrimination for the note above: add rows that are NOT corpus-eligible and the counts must
        move with the shares, not independently of them. A raw count printed beside an eligible-only
        percentage is how a report ends up comparing two populations in one sentence."""
        p = self._two_city_frame()
        extra = [synth_row(9001 + i, f'occ{i}', 'Occlusion', 10.0, 0.0, 'mid') for i in range(40)]
        for r in extra:
            r['city'] = 'amsterdam'
        p2 = cs.prepare(pd.concat([p, pd.DataFrame(extra)], ignore_index=True))
        fc = cs.frame_comparison(p2, reference=('amsterdam',))
        assert int(p2['corpus_eligible'].sum()) == 120, 'the 40 Occlusion rows are not in the corpus'
        assert fc['n_reference_corpus_eligible'] == 60
        assert fc['n_frame_corpus_eligible'] == 120
        assert fc['reference_share_pct'] == pytest.approx(50.0)

    def test_a_stratum_absent_from_the_reference_reports_no_ratio(self):
        """A ratio against a zero reference share is undefined, and 0.0 or inf would both read as
        measurements. This is the case that matters most — a cell the old frame could not have drawn
        at all is exactly what would break reweighting a six-city draw to a wider population."""
        p = self._two_city_frame()
        fc = cs.frame_comparison(p, reference=('amsterdam',))
        assert fc['by_label_type']['Signal']['reference_pct'] == pytest.approx(0.0)
        assert fc['by_label_type']['Signal']['ratio'] is None

    def test_a_disjoint_reference_is_reported_as_not_applicable(self):
        """The failure here is plausible, not loud: against a reference sharing no rows, every
        total-variation distance comes out at exactly 50.00 pp — a clean-looking number that means
        nothing. The Mapillary manifest shipped 50.00 pp for all four marginals before this guard, and
        it would have gone into a report as a finding about the Mapillary rig.
        """
        p = self._two_city_frame()
        fc = cs.frame_comparison(p, reference=('nowhere-at-all',))
        assert fc['applicable'] is False
        assert 'total_variation_pct' not in fc
        assert fc['n_reference_corpus_eligible'] == 0
        assert 'rig' in fc['reason']

    def test_an_overlapping_reference_is_applicable(self):
        """Discrimination for the guard: the real GSV case must still compute."""
        fc = cs.frame_comparison(self._two_city_frame(), reference=('amsterdam',))
        assert fc['applicable'] is True
        assert set(fc['total_variation_pct']) == set(cs.COMPARISON_KEYS)

    def test_it_reports_which_strata_cells_the_wider_frame_adds(self):
        p = self._two_city_frame()
        fc = cs.frame_comparison(p, reference=('amsterdam',))
        assert fc['strata_cells']['reference'] == 1
        assert fc['strata_cells']['frame'] == 2
        assert fc['strata_cells']['only_in_frame'] == ['5-15|mid|Signal']
        assert fc['strata_cells']['only_in_reference'] == []


class TestPopulationWeights:
    """§3 reweights corpus claims to the label population. The weights therefore describe the FRAME,
    not the draw — computing them on the draw would make every reweighted number a tautology."""

    def test_weights_come_from_the_frame_not_the_draw(self):
        rows, lid = [], 1
        for _ in range(300):
            rows.append(synth_row(lid, f'a{lid}', 'CurbRamp', 10.0, 0.0, 'mid'))
            lid += 1
        for _ in range(30):
            rows.append(synth_row(lid, f'b{lid}', 'Obstacle', 10.0, 0.0, 'mid'))
            lid += 1
        p = cs.prepare(frame(rows))
        w = cs.population_weights(p)
        assert w['5-15|CurbRamp'] == pytest.approx(300 / 330, rel=1e-9)
        assert w['5-15|Obstacle'] == pytest.approx(30 / 330, rel=1e-9)

    def test_weights_sum_to_one(self):
        w = cs.population_weights(cs.prepare(wide_frame()))
        assert sum(w.values()) == pytest.approx(1.0)

    def test_weights_ignore_rows_outside_the_corpus(self):
        rows = [synth_row(1, 'pa', 'CurbRamp', 10.0, 0.0, 'mid'),
                synth_row(2, 'pb', 'Occlusion', 10.0, 0.0, 'mid')]
        w = cs.population_weights(cs.prepare(frame(rows)))
        assert set(w) == {'5-15|CurbRamp'}

    def test_unsupported_population_share_is_zero_when_every_cell_is_drawn(self):
        """This is the invariant that licenses reweighting a narrow draw to a wide population: every
        cell carrying weight must have drawn support. The six-city frame passes it against all 49
        GSV deployments (92 of 92 cells occupied), which is *why* the wider reweighting is legitimate
        rather than an extrapolation."""
        p = cs.prepare(wide_frame())
        drawn = cs.draw(p, seed=1)
        cov = cs.weight_coverage(cs.population_weights(p), drawn)
        assert cov['unsupported_population_pct'] == pytest.approx(0.0)
        assert cov['cells_unsupported'] == 0

    def test_unsupported_population_share_is_reported_not_hidden(self):
        """Discrimination: a population cell with weight but no drawn label makes the reweighted
        estimate undefined for that share of the population, and it must surface as a number."""
        p = cs.prepare(wide_frame())
        drawn = cs.draw(p, seed=1)
        weights = cs.population_weights(p)
        weights['>30|Nonexistent'] = 0.25
        cov = cs.weight_coverage(weights, drawn)
        assert cov['cells_unsupported'] == 1
        assert cov['unsupported_population_pct'] > 0
