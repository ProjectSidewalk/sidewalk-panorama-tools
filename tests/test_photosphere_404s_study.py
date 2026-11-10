"""reports/scripts/photosphere_404s.py and reports/2026-11-09-photosphere-404s-and-trailers.md (#223 follow-up).

Three layers, as in the other report tests (.claude/rules/desk-studies.md): the script's describer, packer and
reducer on synthetic inputs (a committed artifact was produced BY the code it would otherwise pin, so a revert
stays green against it), the committed JSON being exactly what the script derives from the committed files, and
every table line and prose number the report quotes appearing in the script's output. Network-free: the photo
host is a stub.
"""

import csv
import io
import json
import os
import sys

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for path in (REPO_ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import photosphere_404s as ps  # noqa: E402

DATA = os.path.join(REPO_ROOT, 'reports', 'data')
REPORT = os.path.join(REPO_ROOT, 'reports', '2026-11-09-photosphere-404s-and-trailers.md')
AF1QIP_ID = 'CAoSLEFGMVFpcE05ZHg3dFhIa1htX193c1JDUW10UmRKd1J4dDY1Ujk5cDFGYWlu'
AF1QIP_ID_2 = 'CAoSLEFGMVFpcE1ub2YyODJZRzVNY3J0OVJjUHhhSGc5ZXNPNExmTnI0WExqOTBJ'


def jpeg(size=(32, 16)):
    buf = io.BytesIO()
    Image.new('RGB', size, (10, 20, 30)).save(buf, 'jpeg')
    return buf.getvalue()


class Response:
    def __init__(self, status, body, ctype='image/jpeg', clen=None):
        self.status_code, self.content = status, body
        self.headers = {'Content-Type': ctype, 'Content-Length': str(len(body)) if clen is None else clen}


class Session:
    def __init__(self, answers):
        self.answers, self.urls = list(answers), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.urls.append(url)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TestDescribe:

    def test_a_trailer_is_measured_and_the_body_still_decodes(self):
        body = jpeg() + b'\x00\x00Image_UTC_Data1234567890' + b'\x00' * 20
        row = ps.describe_response(200, 'image/jpeg; charset=x', None, body)

        assert row['bytes_after_last_eoi'] == 46 and row['decodes'] == 'yes'
        assert bytes.fromhex(row['after_eoi_head']).startswith(b'\x00\x00Image_UTC_Data')
        assert row['content_type'] == 'image/jpeg' and row['content_length'] == ''

    def test_a_body_that_does_not_decode_says_no_and_does_not_raise(self):
        assert ps.describe_response(200, 'image/jpeg', '9', b'\xff\xd8junk\xff\xd9')['decodes'] == 'no'

    def test_a_non_200_records_the_page_but_no_jpeg_fields(self):
        row = ps.describe_response(404, 'image/png', '897', b'x' * 897)
        assert row['body_len'] == 897 and row['last_eoi'] == '' and row['decodes'] == ''


class TestMeasure:

    def test_one_row_per_id_a_raise_is_a_row_and_the_request_is_the_nightly_one(self, tmp_path):
        session = Session([Response(200, jpeg()), ConnectionError('reset')])
        out = tmp_path / 'm.csv'
        ps.measure([('seattle-wa', AF1QIP_ID), ('zurich', AF1QIP_ID_2)], 'r1', str(out),
                   session_factory=lambda: session, pause=0, now=lambda: 'T')

        rows = list(csv.DictReader(open(out, newline='')))
        assert [r['status'] for r in rows] == ['200', 'raise']
        assert rows[1]['content_type'] == 'connectionerror'
        assert session.urls[0] == ps.gsv.PHOTOSPHERE_URL % ps.gsv.photosphere_photo_id(AF1QIP_ID)

    def test_a_second_round_appends_under_one_header(self, tmp_path):
        out = tmp_path / 'm.csv'
        for label in ('r1', 'r2'):
            ps.measure([('seattle-wa', AF1QIP_ID)], label, str(out), session_factory=lambda: Session(
                [Response(404, b'x', 'image/png')]), pause=0, now=lambda: 'T')
        assert [r['round'] for r in csv.DictReader(open(out, newline=''))] == ['r1', 'r2']


class TestPackHistory:

    def test_kinds_forms_and_the_ledger_join(self):
        rows = ps.pack_history(
            ['seattle-wa %s S404 AF1Qip x4' % AF1QIP_ID, 'zurich %s TRUNC6237508 x3' % AF1QIP_ID_2],
            ['seattle-wa %s none file=0' % AF1QIP_ID, 'zurich %s 1,2026-11-05 19:00:00-08:00 file=1' % AF1QIP_ID_2])

        assert rows == [
            {'city': 'seattle-wa', 'pano_id': AF1QIP_ID, 'failure': 'status-404', 'form': 'AF1Qip',
             'times_logged': 4, 'ledger_now': 'none'},
            {'city': 'zurich', 'pano_id': AF1QIP_ID_2, 'failure': 'truncated-200', 'form': 'AF1Qip',
             'times_logged': 3, 'ledger_now': '1'}]


def write_fixture(data_dir, measurements, history):
    for name, columns, rows in ((ps.MEASUREMENTS, ps.MEASURE_COLUMNS, measurements),
                                (ps.HISTORY, ps.HISTORY_COLUMNS, history)):
        with open(os.path.join(data_dir, name), 'w', newline='') as f:
            w = csv.DictWriter(f, columns, restval='')
            w.writeheader()
            w.writerows(rows)


class TestAnalyze:

    def test_patterns_shapes_flips_and_trailers(self, tmp_path):
        def m(rnd, pid, status, **kw):
            return dict({'round': rnd, 'at': rnd, 'city': 'c', 'pano_id': pid, 'status': status}, **kw)
        ok = dict(content_type='image/jpeg', content_length='10', body_len='10', bytes_after_last_eoi='111',
                  after_eoi_head=b'\x00Image_UTC_Data'.hex(), sha256_16='h', decodes='yes')
        png = dict(content_type='image/png', body_len='897')
        html = dict(content_type='text/html', body_len='1610')
        write_fixture(str(tmp_path), [
            m('r1', 'a', '404', **png), m('r2', 'a', '404', **html),        # 404 both rounds, two shapes
            m('r1', 'b', '200', **ok), m('r2', 'b', '404', **html),         # served, then 404
            m('r1', 'c', '200', **ok), m('r2', 'c', '200', **dict(ok, content_length='9')),
        ], [
            {'city': 'c', 'pano_id': 'a', 'failure': 'status-404', 'form': 'AF1Qip', 'times_logged': 2,
             'ledger_now': 'none'},
            {'city': 'c', 'pano_id': 'a', 'failure': 'truncated-200', 'form': 'AF1Qip', 'times_logged': 1,
             'ledger_now': 'none'},
            {'city': 'c', 'pano_id': 'd', 'failure': 'status-404', 'form': 'AF1Qip', 'times_logged': 2,
             'ledger_now': '1'},
            {'city': 'c', 'pano_id': 'e', 'failure': 'status-404', 'form': 'AF1Qip', 'times_logged': 1,
             'ledger_now': '1'},
            {'city': 'c', 'pano_id': 'f', 'failure': 'status-404', 'form': 'CIHM', 'times_logged': 2,
             'ledger_now': '0'},
        ])
        r = ps.analyze(str(tmp_path))

        assert r['status_patterns'] == {'200 / 200': 1, '200 / 404': 1, '404 / 404': 1}
        assert r['not_found_shapes'] == {'image/png 897': 1, 'text/html 1610': 2}
        assert (r['ids_404_every_round'], r['ids_404_both_shapes'], r['ids_served_then_404']) == (1, 1, 1)
        assert (r['ok_responses'], r['ok_length_matches_content_length'], r['ok_decode']) == (3, 2, 3)
        assert r['trailer_sizes'] == {'111': 2} and r['trailer_ids_samsung'] == 2
        assert r['history_af1qip_ids'] == 3, 'the CIHM id is not counted'
        assert r['history_af1qip_404_twice_then_served'] == 1, 'once is not twice'
        assert r['history_af1qip_unledgered_by_failures'] == {'status-404 + truncated-200': 1}

    def test_tables_print_one_line_per_cell(self, tmp_path):
        r = {'rounds': ['r1'], 'status_patterns': {'404': 2}, 'not_found_shapes': {'image/png 897': 2},
             'trailer_sizes': {'111': 1}}
        assert '| 404 | 2 |' in ps.tables(r) and '| image/png 897 | 2 |' in ps.tables(r)


@pytest.fixture(scope='module')
def result():
    return ps.analyze(DATA)


@pytest.fixture(scope='module')
def report_text():
    with open(REPORT, encoding='utf-8') as f:
        return f.read()


class TestTheCommittedArtifact:

    def test_the_json_is_what_the_committed_files_reduce_to(self, result):
        with open(os.path.join(DATA, ps.RESULT), encoding='utf-8') as f:
            assert json.load(f) == json.loads(json.dumps(result))

    def test_every_served_body_was_complete_and_carried_a_trailer_in_the_window(self, result):
        n = result['ok_responses']
        assert n > 0 and result['ok_length_matches_content_length'] == result['ok_decode'] == n
        assert result['ok_ends_with_eoi'] == 0 and result['ok_within_trailer_window'] == n

    def test_a_404_is_not_one_page_and_not_stable_per_photo(self, result):
        assert len(result['not_found_shapes']) > 1
        assert result['ids_served_then_404'] >= 1 and result['history_af1qip_404_twice_then_served'] >= 1


class TestTheReportMatchesTheArtifact:

    def test_every_table_line_is_in_the_report(self, result, report_text):
        for line in ps.tables(result):
            assert line in report_text, line

    def test_every_prose_number_is_in_the_report(self, result, report_text):
        for key in ('ids_measured', 'responses', 'ids_404_every_round', 'ids_404_both_shapes',
                    'ids_served_then_404', 'ok_responses', 'ok_length_matches_content_length', 'ok_decode',
                    'ok_ends_with_eoi', 'ok_within_trailer_window', 'trailer_ids', 'trailer_ids_samsung',
                    'history_af1qip_ids', 'history_af1qip_404_twice_then_served', 'history_af1qip_unledgered'):
            assert '**%d**' % result[key] in report_text, key
        for city, n in result['ids_by_city'].items():
            assert '%s %d' % (city, n) in report_text, city
        for failures, n in result['history_af1qip_unledgered_by_failures'].items():
            assert '`%s`%s: %d' % (failures, ' only' if '+' not in failures else '', n) in report_text, failures
        for label, at in result['round_times'].items():
            assert '%s at %s' % (label, at) in report_text, label
