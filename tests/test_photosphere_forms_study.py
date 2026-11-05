"""reports/scripts/photosphere_forms.py and reports/2026-11-05-photosphere-forms.md (#223).

Three layers, as in the other report tests (.claude/rules/desk-studies.md): the script's readers and reducers
on synthetic inputs (a committed artifact was produced BY the code it would otherwise pin, so a revert stays
green against it), the committed JSON being exactly what the script derives from the committed files, and every
table line and prose number the report quotes appearing in the script's output. Network-free: the photo host is
a stub.
"""

import gzip
import json
import os
import re
import sys
import zlib

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for path in (REPO_ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import photosphere_forms as pf  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-05-photosphere-forms.md')

# Real ids of each form (chicago-il's list), so the decode under test is the production one.
AF1QIP_ID = 'CAoSLEFGMVFpcE05ZHg3dFhIa1htX193c1JDUW10UmRKd1J4dDY1Ujk5cDFGYWlu'    # -> AF1QipM9dx7t...
AF1QIP_ID_2 = 'CAoSLEFGMVFpcE1ub2YyODJZRzVNY3J0OVJjUHhhSGc5ZXNPNExmTnI0WExqOTBJ'  # another AF1Qip id
CIHM_ID = 'CAoSF0NJSE0wb2dLRUlDQWdJQ0U0ZXpLOXdF'                                  # -> CIHM0ogKEICAgICE4ezK9wE
CIAB_ID = 'CAoSHENJQUJJaEFEeWNLejZoTFNPbWY2MWNzQUJ6RDA.'                          # -> CIABIhADycKz6hLSOmf61csABzD0
UNDECODABLE_ID = 'CAoSnot-a-protobuf'


class FakeResponse:
    def __init__(self, status, ctype, clen, url):
        self.status_code, self.url, self.closed = status, url, False
        self.headers = {'Content-Type': ctype, 'Content-Length': clen}

    def close(self):
        self.closed = True


class FakeSession:
    """Answers 200 image/jpeg to AF1Qip photo ids and 400 text/html to everything else, as the host does."""

    def __init__(self, raise_for=()):
        self.calls, self.raise_for, self.responses = [], set(raise_for), []

    def get(self, url, timeout=None, stream=False):
        self.calls.append((url, timeout, stream))
        photo = url.rsplit('/', 1)[1].split('=')[0]
        if photo in self.raise_for:
            raise ConnectionError('boom')
        ok = '/p/' in url and photo.startswith('AF1Qip')
        r = FakeResponse(200 if ok else 400, 'image/jpeg' if ok else 'text/html; charset=UTF-8',
                         '3364286' if ok else '1555', url)
        self.responses.append(r)
        return r


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(pf, 'pace', lambda: None)


def panos():
    return [{'pano_id': '---HoC6LAFnqMUB44NkaQw', 'has_labels': True, 'width': 16384, 'height': 8192},
            {'pano_id': AF1QIP_ID, 'has_labels': True, 'width': 8192, 'height': 4096},
            {'pano_id': CIHM_ID, 'has_labels': False, 'width': None, 'height': None},
            {'pano_id': CIAB_ID, 'has_labels': True},
            {'pano_id': UNDECODABLE_ID, 'has_labels': False, 'width': 1, 'height': 2}]


class TestClassification:

    @pytest.mark.parametrize('photo_id, form', [
        ('AF1QipM9dx7tXHkXm__wsRCQmtRdJwRxt65R99p1Fain', 'AF1Qip'), ('CIHM0ogKEICAgICE4ezK9wE', 'CIHM'),
        ('CIABIhADycKz6hLSOmf61csABzD0', 'CIAB'), ('ZZZZsomething', 'other'), (None, 'undecodable')])
    def test_a_form_is_the_photo_ids_leading_letters(self, photo_id, form):
        assert pf.form_of(photo_id) == form

    def test_caos_rows_keep_only_photospheres_and_blank_what_the_list_omits(self):
        rows = pf.caos_rows(panos())
        assert [r['form'] for r in rows] == ['AF1Qip', 'CIHM', 'CIAB', 'undecodable']
        assert rows[0]['photo_id'].startswith('AF1Qip') and rows[0]['has_labels'] == 'True'
        assert rows[0]['width'] == '8192' and rows[1]['width'] == '' and rows[2]['width'] == ''
        assert rows[3]['photo_id'] == '' and rows[3]['has_labels'] == 'False'


class TestAsking:

    def test_ask_reads_headers_only_and_closes(self):
        session = FakeSession()
        status, ctype, clen, final, note = pf.ask(session, pf.PROBE_HOSTS[0] % 'CIHM0ogKEICAgICE4ezK9wE')
        assert (status, ctype, clen, note) == ('400', 'text/html; charset=UTF-8', '1555', '')
        assert final.endswith('/p/CIHM0ogKEICAgICE4ezK9wE=s0')
        assert session.calls[0][2] is True and session.responses[0].closed

    def test_a_raise_is_a_row_not_a_stop(self):
        session = FakeSession(raise_for={'CIHM0ogKEICAgICE4ezK9wE'})
        status, _, _, _, note = pf.ask(session, pf.PROBE_HOSTS[0] % 'CIHM0ogKEICAgICE4ezK9wE')
        assert status == 'raise' and note.startswith('ConnectionError: boom')

    def test_measure_skips_af1qip_by_default_never_asks_for_an_undecodable_id_and_flushes_per_row(self):
        ids, session, written, flushes = pf.caos_rows(panos()), FakeSession(), [], []

        class Out:
            def writerow(self, row):
                written.append(row)

        rows = pf.measure(ids, session, out=Out(), flush=lambda: flushes.append(len(written)),
                          progress=lambda *_: None)
        assert [r['form'] for r in rows] == ['CIHM', 'CIAB', 'undecodable']
        assert [r['status'] for r in rows] == ['400', '400', ''] and rows[2]['note'] == 'undecodable'
        assert len(session.calls) == 2 and written == rows and flushes == [1, 2, 3]

    def test_measure_all_forms_includes_af1qip_and_limit_truncates(self):
        rows = pf.measure(pf.caos_rows(panos()), FakeSession(), all_forms=True, limit=1, progress=lambda *_: None)
        assert [(r['form'], r['status'], r['content_type']) for r in rows] == [('AF1Qip', '200', 'image/jpeg')]

    def test_a_limit_of_zero_measures_nothing(self):
        assert pf.measure(pf.caos_rows(panos()), FakeSession(), limit=0, progress=lambda *_: None) == []

    def test_probe_asks_the_first_id_of_each_form_at_every_host(self):
        session = FakeSession()
        rows = pf.probe(pf.caos_rows(panos()), session, hosts=pf.PROBE_HOSTS[:2])
        assert [(r['form'], r['host']) for r in rows] == [
            (f, h) for f in ('AF1Qip', 'CIAB', 'CIHM') for h in pf.PROBE_HOSTS[:2]]
        assert len(session.calls) == 6


class TestTheLedgerDumps:

    def test_two_and_three_field_rows_keep_their_width_and_crlf_is_tolerated(self):
        rows = pf.pack_ledger_lines(['chicago-il,%s,0\r\n' % CIHM_ID, 'seattle-wa,%s,1,\n' % AF1QIP_ID,
                                     'spgg,%s,1,2026-11-04 19:00:00-08:00\n' % AF1QIP_ID, '\n'])
        assert [(r['city'], r['downloaded'], r['fetched_at'], r['row_fields']) for r in rows] == [
            ('chicago-il', '0', '', '2'), ('seattle-wa', '1', '', '3'), ('spgg', '1', '2026-11-04 19:00:00-08:00', '3')]

    @pytest.mark.parametrize('line', ['chicago-il,notcaos,0', 'chicago-il,%s' % CIHM_ID, 'x,%s,0,a,b' % CIHM_ID])
    def test_anything_else_is_refused(self, line):
        with pytest.raises(ValueError):
            pf.pack_ledger_lines([line])

    def test_depth_rows_are_two_fields_for_a_caos_id(self):
        assert pf.pack_depth_ledger_lines(['%s,unavailable\r\n' % CIHM_ID, '']) == [
            {'pano_id': CIHM_ID, 'depth': 'unavailable'}]
        with pytest.raises(ValueError):
            pf.pack_depth_ledger_lines(['notcaos,unavailable'])
        with pytest.raises(ValueError):
            pf.pack_depth_ledger_lines(['%s,0,2026-11-04' % CIHM_ID])


def synthetic_data_dir(tmp_path):
    """A complete data directory built with the script's own writers, small enough to reason about by hand."""
    d = str(tmp_path)
    ids = pf.caos_rows(panos())
    with open(os.path.join(d, pf.LIST_FILE), 'w', encoding='utf-8') as f:
        json.dump({'city': 'chicago-il', 'pano_count': 5, 'labelled_count': 3, 'caos_count': len(ids)}, f)
    pf.write_csv(os.path.join(d, pf.IDS_FILE), pf.IDS_COLUMNS, ids)
    pf.write_csv(os.path.join(d, pf.MEASUREMENTS_FILE), pf.MEASUREMENT_COLUMNS,
                 pf.measure(ids, FakeSession(), progress=lambda *_: None))
    # The second sweep: the CIAB id raised this time, so one status changed between sweeps.
    pf.write_csv(os.path.join(d, pf.MEASUREMENTS_ALL_FILE), pf.MEASUREMENT_COLUMNS,
                 pf.measure(ids, FakeSession(raise_for={'CIABIhADycKz6hLSOmf61csABzD0'}), all_forms=True,
                            progress=lambda *_: None))
    pf.write_csv(os.path.join(d, pf.PROBE_FILE), pf.PROBE_COLUMNS, pf.probe(ids, FakeSession()))
    ledger = pf.pack_ledger_lines(['chicago-il,%s,0' % AF1QIP_ID, 'chicago-il,%s,0' % AF1QIP_ID_2,
                                   'zurich,%s,0' % AF1QIP_ID_2, 'zurich,%s,1,' % CIHM_ID])
    pf.write_csv(os.path.join(d, pf.LEDGER_FILE), pf.LEDGER_COLUMNS, ledger)
    pf.write_csv(os.path.join(d, pf.DEPTH_LEDGER_FILE), pf.DEPTH_LEDGER_COLUMNS,
                 pf.pack_depth_ledger_lines(['%s,unavailable' % AF1QIP_ID, '%s,saved' % CIHM_ID]))
    # Fields 4 and 5 differ, so an off-by-one on the field-5 index is caught; field 9 is 10 and field 20 is 2.
    full = ['2026-11-04 23:21:28.404563-08:00', '0', '0', '4', '5', '0', '0', '0', '10'] + ['0'] * 10 + ['2']
    short = ['2026-11-03 19:00:14.430318-08:00', '0', '0', '4', '5', '0', '0', '0', '9'] + ['0'] * 9  # 18 fields
    with open(os.path.join(d, pf.LOG_TAIL_FILE), 'w', encoding='utf-8', newline='\n') as f:
        f.write(','.join(short) + '\n' + ','.join(full) + '\n')
    with open(os.path.join(d, pf.STORE_FILES_FILE), 'w', encoding='utf-8') as f:
        f.write('-rw-r--r-- 1 jonf makelab 1 2026-10-20 17:02 %s.jpg\n\n' % AF1QIP_ID)
        f.write('-rw-r--r-- 1 jonf makelab 1 2026-10-20 17:02 %s.jpg\n' % CIHM_ID)
    return d


class TestTheReducer:

    def test_the_join_counts_ledgered_and_unledgered_per_form_and_flags_ids_outside_the_list(self, tmp_path):
        result = pf.analyze(synthetic_data_dir(tmp_path))
        join = result['chicago_join']
        assert join['by_form']['AF1Qip'] == {'ids': 1, 'ledgered': 1, 'ledgered_verdicts': {'0': 1},
                                             'ledgered_labelled': 1, 'unledgered': 0, 'unledgered_labelled': 0}
        assert join['by_form']['CIHM']['unledgered'] == 1 and join['by_form']['CIAB']['unledgered_labelled'] == 1
        assert join['ledgered_total'] == 1 and join['unledgered_total'] == 3
        assert join['ledgered_non_af1qip'] == 0 and join['unledgered_non_af1qip'] == 3
        assert join['labelled_non_af1qip'] == 1
        assert join['ledgered_not_in_list'] == [AF1QIP_ID_2]

    def test_the_fleet_ledger_is_counted_by_form_verdict_width_and_city(self, tmp_path):
        fleet = pf.analyze(synthetic_data_dir(tmp_path))['fleet_ledger']
        assert fleet['rows'] == 4 and fleet['cities'] == 2 and fleet['other_cities'] == 1 and fleet['dated_rows'] == 0
        assert fleet['by_verdict'] == {'0': 3, '1': 1} and fleet['by_row_fields'] == {'2': 3, '3': 1}
        assert fleet['by_form']['AF1Qip'] == {'rows': 3, 'cities': 2, 'by_verdict': {'0': 3}}
        assert fleet['non_af1qip_rows'] == 1
        assert fleet['per_city']['zurich'] == {'AF1Qip': 1, 'CIHM': 1, 'total': 2}

    def test_the_log_tail_reads_fields_5_9_and_20_and_tolerates_rows_older_than_field_20(self, tmp_path):
        tail = pf.analyze(synthetic_data_dir(tmp_path))['chicago_log_tail']
        assert tail['rows'] == 2 and [r['raised'] for r in tail['runs']] == [None, 2]
        assert [r['image_eligible'] for r in tail['runs']] == [5, 5]
        assert [r['image_failures'] for r in tail['runs']] == [9, 10]
        assert tail['max_raised'] == 2 and tail['runs_at_max'] == 1
        assert tail['failures_net_of_raises_at_max'] == [8]
        assert tail['list_snapshot_matches_run'] == '2026-11-03 19:00:14.430318-08:00'

    def test_both_sweeps_the_probe_the_depth_ledger_and_the_store_files_are_summarised(self, tmp_path):
        result = pf.analyze(synthetic_data_dir(tmp_path))
        m = result['measurements']['by_form']
        assert m['CIHM']['status'] == {'400': 1} and m['CIAB']['content_lengths'] == {'1555': 1}
        assert m['undecodable']['status'] == {'': 1} and result['measurements']['unmeasured_non_af1qip'] == []
        assert result['measurements']['labelled'] == 1 and m['CIHM']['redirected'] == 0
        every = result['measurements_all_forms']
        assert every['by_form']['AF1Qip']['status'] == {'200': 1} and every['by_form']['CIAB']['status'] == {'raise': 1}
        assert every['unmeasured'] == [] and every['status_changed_between_sweeps'] == [CIAB_ID]
        assert result['host_probe']['AF1Qip'][pf.PROBE_HOSTS[0]]['status'] == '200'
        assert result['host_probe']['AF1Qip'][pf.PROBE_HOSTS[3]]['status'] == '400'
        assert result['chicago_depth_ledger'] == {'rows': 2, 'by_verdict': {'saved': 1, 'unavailable': 1},
                                                  'ids_in_list_without_a_row': 2}
        files = result['chicago_store_files']
        assert files['count'] == 2 and files['forms'] == {'AF1Qip': 1, 'CIHM': 1}
        assert files['ledgered_0'] == 1 and files['in_list'] == 2

    def test_tables_print_every_section_and_a_missing_field_prints_as_missing(self, tmp_path):
        text = pf.tables(pf.analyze(synthetic_data_dir(tmp_path)))
        assert '| AF1Qip | 1 | 1 | 1 | 1 | 0 | 0 | 0 |' in text
        assert '| zurich | 1 | 1 | 0 | 2 |' in text
        assert '| 2026-11-04 23:21:28.404563-08:00 | 5 | 10 | 2 |' in text
        assert '| 2026-11-03 19:00:14.430318-08:00 | 5 | 9 | n/a |' in text
        assert '| AF1Qip | 200 image/jpeg, 3364286 bytes |' in text

    def test_the_gzip_writer_is_reproducible_owns_its_handles_and_flushes_to_disk(self, tmp_path):
        a, b = str(tmp_path / 'a.csv.gz'), str(tmp_path / 'b.csv.gz')
        for p in (a, b):
            pf.write_csv(p, ['x'], [{'x': '1'}])
        assert open(a, 'rb').read() == open(b, 'rb').read()
        with gzip.open(a, 'rt', encoding='utf-8') as f:
            assert f.read() == 'x\n1\n'
        w = pf._GzipText(str(tmp_path / 'c.csv.gz'))
        w.write('partial\n')
        w.flush()
        # Before close the member has no trailer, so gzip.open() raises EOFError at the end; the rows are on
        # disk all the same, as `gzip -dc` (which prints them, then complains) or a raw inflate shows.
        partial = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(open(str(tmp_path / 'c.csv.gz'), 'rb').read())
        assert partial == b'partial\n', 'a flush reaches the disk before close'
        w.close()
        assert w._raw.closed and w._gz.closed
        with gzip.open(str(tmp_path / 'c.csv.gz'), 'rt', encoding='utf-8') as f:
            assert f.read() == 'partial\n'


@pytest.fixture(scope='module')
def result():
    return pf.analyze(DATA)


@pytest.fixture(scope='module')
def report_text():
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


class TestTheCommittedArtifact:

    def test_the_json_is_what_the_committed_files_reduce_to(self, result):
        with open(os.path.join(DATA, pf.RESULT_FILE), encoding='utf-8') as f:
            assert json.load(f) == json.loads(json.dumps(result))

    def test_every_non_af1qip_id_in_the_list_was_measured_and_none_was_served(self, result):
        assert result['measurements']['unmeasured_non_af1qip'] == []
        for form, m in result['measurements']['by_form'].items():
            assert set(m['status']) == {'400'}, (form, m['status'])
            assert m['measured'] == result['chicago_list']['by_form'][form]['ids']
            assert m['redirected'] == 0

    def test_the_second_sweep_covered_every_id_and_the_refused_forms_answered_the_same(self, result):
        every = result['measurements_all_forms']
        assert every['unmeasured'] == [] and every['status_changed_between_sweeps'] == []
        for form in ('CIHM', 'CIAB'):
            assert set(every['by_form'][form]['status']) == {'400'}

    def test_the_probe_separates_the_form_from_the_host(self, result):
        for host in pf.PROBE_HOSTS[:3]:
            assert result['host_probe']['AF1Qip'][host]['status'] == '200'
            assert result['host_probe']['CIHM'][host]['status'] == '400'
            assert result['host_probe']['CIAB'][host]['status'] == '400'
        assert result['host_probe']['AF1Qip'][pf.PROBE_HOSTS[3]]['status'] == '400'

    def test_every_fleet_ledger_row_is_a_two_field_zero(self, result):
        fleet = result['fleet_ledger']
        assert fleet['by_verdict'] == {'0': fleet['rows']} and fleet['by_row_fields'] == {'2': fleet['rows']}
        assert fleet['dated_rows'] == 0

    def test_chicagos_unledgered_photospheres_are_its_nightly_raise_count(self, result):
        """The 276 raises field 20 reports on every complete pass are exactly the unledgered CIHM/CIAB ids, and
        field 9 net of them is the permanent-failure count the rules file records for 2026-09-06."""
        join = result['chicago_join']
        assert join['by_form']['AF1Qip']['unledgered'] == 0
        assert join['unledgered_non_af1qip'] == join['unledgered_total'] == result['chicago_log_tail']['max_raised']
        assert join['ledgered_not_in_list'] == []
        assert result['chicago_log_tail']['failures_net_of_raises_at_max'] == [22985]
        assert result['chicago_log_tail']['list_snapshot_matches_run'] is not None

    def test_the_depth_ledger_holds_every_id_as_unavailable_and_the_store_files_are_ledgered_af1qip(self, result):
        depth = result['chicago_depth_ledger']
        assert depth['by_verdict'] == {'unavailable': depth['rows']} and depth['ids_in_list_without_a_row'] == 0
        files = result['chicago_store_files']
        assert files['forms'] == {'AF1Qip': files['count']}
        assert files['ledgered_0'] == files['in_list'] == files['count']


class TestTheReportMatchesTheArtifact:

    def test_every_table_line_is_in_the_report(self, result, report_text):
        missing = [line for line in pf.tables(result).splitlines() if line and line not in report_text]
        assert not missing, missing[:5]

    def test_every_prose_number_is_in_the_report(self, result, report_text):
        """The counts the prose quotes, as whole words with thousands separators, which the tables do not carry."""
        tail, join, fleet = result['chicago_log_tail'], result['chicago_join'], result['fleet_ledger']
        numbers = [result['chicago_list']['pano_count'], result['chicago_list']['labelled_count'],
                   result['chicago_list']['caos_count'], fleet['rows'], fleet['cities'], fleet['other_cities'],
                   fleet['non_af1qip_rows'], result['measurements']['rows'], result['measurements']['labelled'],
                   result['measurements_all_forms']['rows'], join['ledgered_total'], join['unledgered_total'],
                   join['ledgered_non_af1qip'], join['labelled_non_af1qip'], result['chicago_store_files']['count'],
                   result['chicago_depth_ledger']['rows'], tail['runs_at_max'], tail['rows'],
                   tail['failures_net_of_raises_at_max'][0], len(pf.read_csv(os.path.join(DATA, pf.PROBE_FILE)))]
        numbers += [fleet['by_form'][form]['rows'] for form in pf.FORMS]
        numbers += [int(result['host_probe']['AF1Qip'][pf.PROBE_HOSTS[0]]['content_length']),
                    int(result['host_probe']['CIHM'][pf.PROBE_HOSTS[0]]['content_length'])]
        missing = [n for n in numbers if not re.search(r'(?<![\d,])%s(?![\d,])' % format(n, ','), report_text)]
        assert not missing, missing

    def test_the_list_snapshot_is_named(self, result, report_text):
        assert result['chicago_log_tail']['list_snapshot_matches_run'][:16] in report_text
