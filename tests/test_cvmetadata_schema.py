"""Tests for check_cvmetadata_schema.py - the live-deployment schema tripwire (#135).

What is under test here is the COMPARISON and the payload reader, never a socket. The check's whole job is
to talk to a live deployment, and this suite is network-free by design - that is the property #135 exists to
preserve, not to erode - so `_open_stream` is the one seam nothing here crosses and every test drives the
logic behind it against bytes written by hand.

Two of these tests are about provenance rather than behaviour: `TestTheFieldListComesFromCropRunner` moves
CropRunner's constants and asserts the verdict moves with them. A tripwire that carries its own copy of the
required fields would pass every other test in this file and still be the bug it is meant to catch - the
16 days of green CI were 16 days of a fixture agreeing with a fixture.
"""

import http.client
import inspect
import io
import json
import logging
import os
import socket
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import CropRunner  # noqa: E402
import check_cvmetadata_schema as schema  # noqa: E402


# The shape sidewalk-sea actually serves, measured 2026-09-18 (samples/cvmetadata-seattle.csv). Written out
# here rather than read from that file because this battery is about what the check does with a field set,
# and the file is the record of one deployment on one day.
SERVED_TODAY = ('label_id', 'pano_id', 'label_type', 'agree_count', 'disagree_count', 'unsure_count',
                'pano_width', 'pano_height', 'pano_x', 'pano_y', 'canvas_width', 'canvas_height',
                'canvas_x', 'canvas_y', 'zoom', 'heading', 'pitch', 'camera_heading', 'camera_pitch',
                'camera_roll')

# What the same endpoint served until SidewalkWebpage#4103 (released 2026-09-02): an integer id, not a name.
SERVED_BEFORE_4103 = tuple('label_type_id' if f == 'label_type' else f for f in SERVED_TODAY)


def payload(fields, count=2):
    """A cvMetadata response body carrying `count` records with these field names."""
    records = [{name: i for i, name in enumerate(fields)} for _ in range(count)]
    return json.dumps(records).encode('utf-8')


def reader_for(body):
    """A (read, counter) pair: `read` behaves like an HTTP response's, `counter` says how much was consumed."""
    stream = io.BytesIO(body)
    consumed = []

    def read(n):
        chunk = stream.read(n)
        consumed.append(len(chunk))
        return chunk

    return read, consumed


class TestTheVerdict:
    """What the check says about a field set. Only a field CropRunner needs and no longer gets is a failure."""

    def test_todays_shape_passes(self):
        result = schema.compare_served_fields(SERVED_TODAY)
        assert result.missing == []
        assert result.missing_groups == []
        assert result.ok

    def test_the_pre_4103_shape_also_passes(self):
        """`label_type_id` is still accepted - every archived export carries it (#123), so an intake that
        refused it would fail on the one shape half the stores on disk were cut from."""
        result = schema.compare_served_fields(SERVED_BEFORE_4103)
        assert result.ok
        assert result.missing_groups == []

    def test_a_required_field_that_stopped_being_served_fails_and_is_named(self):
        served = tuple(f for f in SERVED_TODAY if f != 'pano_x')
        result = schema.compare_served_fields(served)
        assert result.missing == ['pano_x']
        assert not result.ok

    def test_every_missing_required_field_is_named_not_just_the_first(self):
        """The report is what an operator acts on; naming one of three sends them round the loop twice."""
        served = tuple(f for f in SERVED_TODAY if f not in ('pano_x', 'pano_y', 'label_id'))
        result = schema.compare_served_fields(served)
        assert result.missing == ['pano_x', 'pano_y', 'label_id']

    def test_losing_both_label_type_fields_is_the_bug_this_exists_for(self):
        """#4103 renamed one of the pair. Losing the pair entirely is the same failure one step further on,
        and the report has to name BOTH candidates, because which one to restore is the server's choice."""
        served = tuple(f for f in SERVED_TODAY if f != 'label_type')
        result = schema.compare_served_fields(served)
        assert not result.ok
        assert result.missing_groups == [('label_type_id', 'label_type')]
        assert result.missing == []

    def test_a_field_the_server_adds_is_not_a_failure(self):
        """Servers add fields; a check that cried wolf on that is a check nobody reads by the third week."""
        result = schema.compare_served_fields(SERVED_TODAY + ('label_severity', 'label_tags'))
        assert result.ok
        assert result.missing == [] and result.missing_groups == []

    def test_an_added_field_is_still_reported_as_information(self):
        """Not a failure, but worth printing: a new upstream field is usually the first sign of a schema
        move, and the run that prints it is the one already looking at the endpoint."""
        result = schema.compare_served_fields(SERVED_TODAY + ('label_severity',))
        assert 'label_severity' in result.not_required
        # ... and the fields it does read are not listed as not_required.
        assert 'pano_x' not in result.not_required
        assert 'label_type' not in result.not_required

    def test_an_empty_field_set_fails_rather_than_reading_as_clean(self):
        """A degenerate answer must not be the quiet one. Nothing served means nothing required is served."""
        result = schema.compare_served_fields(())
        assert not result.ok
        assert sorted(result.missing) == sorted(CropRunner.REQUIRED_LABEL_COLUMNS)
        assert result.missing_groups == [tuple(CropRunner.LABEL_TYPE_COLUMNS)]


class TestTheFieldListComesFromCropRunner:
    """The required list is READ from CropRunner, never restated (#135's acceptance criterion).

    Two copies of a field list drift, and the drift is silent - which is the entire failure being fixed. So
    these move the constants and assert the verdict moves too; a check carrying its own tuple passes every
    other test in this file.
    """

    def test_a_field_added_to_REQUIRED_LABEL_COLUMNS_is_checked(self, monkeypatch):
        monkeypatch.setattr(CropRunner, 'REQUIRED_LABEL_COLUMNS',
                            CropRunner.REQUIRED_LABEL_COLUMNS + ('sentinel_field',))
        result = schema.compare_served_fields(SERVED_TODAY)
        assert result.missing == ['sentinel_field']
        assert not result.ok

    def test_the_label_type_pair_is_read_from_CropRunner_too(self, monkeypatch):
        monkeypatch.setattr(CropRunner, 'LABEL_TYPE_COLUMNS', ('sentinel_type',))
        result = schema.compare_served_fields(SERVED_TODAY)
        assert result.missing_groups == [('sentinel_type',)]
        assert not result.ok

    def test_a_field_dropped_from_CropRunner_stops_being_required(self, monkeypatch):
        """The other direction: the check must not outlive a requirement CropRunner gave up, or the first
        person to simplify the cropper gets a red cron mail about a field nothing reads."""
        monkeypatch.setattr(CropRunner, 'REQUIRED_LABEL_COLUMNS', ('pano_id', 'label_id'))
        result = schema.compare_served_fields(('pano_id', 'label_id', 'label_type'))
        assert result.ok


class TestReadingTheFieldNamesOffTheWire:
    """cvMetadata is the whole label list - 183,682 rows for seattle-wa - so the check reads a prefix and
    stops at the first record. Anything that pulls the whole body turns a nightly check into a nightly
    transfer of the entire corpus."""

    def test_the_field_names_are_the_first_records_keys_in_order(self):
        read, _ = reader_for(payload(SERVED_TODAY))
        assert schema.served_fields(read) == list(SERVED_TODAY)

    def test_it_stops_after_the_first_record(self):
        body = payload(SERVED_TODAY, count=5000)
        read, consumed = reader_for(body)
        schema.served_fields(read, chunk_bytes=4096)
        assert sum(consumed) < len(body), 'the whole payload was read; this must stop at the first record'

    def test_a_record_split_across_chunks_is_still_read(self):
        """The first record is bigger than one read() on any chunk size the caller happens to pick."""
        read, _ = reader_for(payload(SERVED_TODAY))
        assert schema.served_fields(read, chunk_bytes=7) == list(SERVED_TODAY)

    def test_leading_whitespace_is_not_a_different_payload(self):
        read, _ = reader_for(b'\n  ' + payload(SERVED_TODAY))
        assert schema.served_fields(read) == list(SERVED_TODAY)

    def test_a_non_ascii_value_split_mid_character_does_not_decide_the_verdict(self):
        """The prefix is decoded before it is complete, so a multi-byte character can be cut in half. Only
        the keys matter, and they are ASCII, so a mangled value must not fail the read."""
        body = json.dumps([{'label_id': 1, 'pano_id': 'a', 'pano_x': 'café üñ',
                            'pano_y': 2, 'label_type': 'CurbRamp'}]).encode('utf-8')
        read, _ = reader_for(body)
        assert schema.served_fields(read, chunk_bytes=3)[:2] == ['label_id', 'pano_id']

    def test_an_error_envelope_is_not_a_payload(self):
        """Positive evidence, the #99 rule: only a JSON array of records is a cvMetadata response. A login
        page or an `{"error": ...}` body must read as "could not check", never as "no fields served" -
        which would name every required field as missing and turn a proxy into a schema alarm."""
        read, _ = reader_for(b'{"error": "unauthorized"}')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)

    def test_html_is_not_a_payload(self):
        read, _ = reader_for(b'<!DOCTYPE html><html><body>Sign in</body></html>')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)

    def test_an_empty_label_list_is_unavailable_not_clean(self):
        """A deployment with no labels tells us nothing about the field names. Reporting OK there is a check
        that silently stops checking - the shape of the original failure."""
        read, _ = reader_for(b'[]')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)

    def test_a_truncated_body_is_unavailable_not_clean(self):
        read, _ = reader_for(b'[{"label_id": 1, "pano_')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)

    def test_a_record_that_is_not_an_object_is_unavailable(self):
        read, _ = reader_for(b'[1, 2, 3]')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)

    def test_the_prefix_is_bounded(self):
        """A server that streams one enormous record (or never closes) must not be read for ever."""
        read, consumed = reader_for(b'[' + b' ' * 500000 + b'{"label_id": 1}]')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read, max_bytes=4096, chunk_bytes=1024)
        assert sum(consumed) <= 4096 + 1024


class TestTheCommandLine:
    """main(argv) end to end against a stubbed fetch. The exit code is the whole unattended interface: cron
    mails on nonzero and on nothing else."""

    @pytest.fixture
    def served(self, monkeypatch):
        """Replace the one function that touches the network; the box it returns is what the deployment said."""
        box = {'fields': list(SERVED_TODAY), 'raise': None, 'asked': []}

        def fake_fetch(fqdn, timeout=None):
            box['asked'].append((fqdn, timeout))
            if box['raise'] is not None:
                raise box['raise']
            return list(box['fields'])

        monkeypatch.setattr(schema, 'fetch_served_fields', fake_fetch)
        return box

    def test_a_healthy_deployment_exits_zero(self, served, capsys):
        assert schema.main(['--host', 'sidewalk-sea.cs.washington.edu']) == 0
        assert 'sidewalk-sea.cs.washington.edu' in capsys.readouterr().out

    def test_a_missing_field_exits_nonzero_and_names_it(self, served, capsys):
        served['fields'] = [f for f in SERVED_TODAY if f != 'label_type']
        code = schema.main(['--host', 'sidewalk-sea.cs.washington.edu'])
        assert code != 0
        out = capsys.readouterr().out
        assert 'label_type' in out and 'label_type_id' in out

    def test_a_missing_required_column_exits_nonzero_and_names_it(self, served, capsys):
        served['fields'] = [f for f in SERVED_TODAY if f != 'pano_y']
        code = schema.main(['--host', 'sidewalk-sea.cs.washington.edu'])
        assert code != 0
        assert 'pano_y' in capsys.readouterr().out

    def test_an_added_field_still_exits_zero(self, served, capsys):
        served['fields'] = list(SERVED_TODAY) + ['label_severity']
        assert schema.main(['--host', 'sidewalk-sea.cs.washington.edu']) == 0
        assert 'label_severity' in capsys.readouterr().out

    def test_a_deployment_that_could_not_be_read_exits_nonzero_with_its_own_code(self, served, capsys):
        """Distinct from a schema failure: "the endpoint is unreachable" and "the endpoint moved" need
        different people. Both are nonzero, because a check that could not run has not passed."""
        served['raise'] = schema.SchemaUnavailable('timed out')
        code = schema.main(['--host', 'sidewalk-sea.cs.washington.edu'])
        assert code == schema.EXIT_UNAVAILABLE
        assert code != 0
        assert 'timed out' in capsys.readouterr().out

    def test_the_host_has_no_default(self, monkeypatch, capsys):
        """A wrong default silently checks the wrong deployment - resolve_sftp's and --cities' rule."""
        monkeypatch.delenv(schema.HOST_ENV, raising=False)
        with pytest.raises(SystemExit) as excinfo:
            schema.main([])
        assert excinfo.value.code != 0
        assert schema.HOST_ENV in str(excinfo.value)

    def test_the_host_may_come_from_the_environment(self, served, monkeypatch):
        monkeypatch.setenv(schema.HOST_ENV, 'sidewalk-cdmx.cs.washington.edu')
        assert schema.main([]) == 0
        assert served['asked'][0][0] == 'sidewalk-cdmx.cs.washington.edu'

    def test_the_flag_wins_over_the_environment(self, served, monkeypatch):
        monkeypatch.setenv(schema.HOST_ENV, 'sidewalk-cdmx.cs.washington.edu')
        schema.main(['--host', 'sidewalk-sea.cs.washington.edu'])
        assert served['asked'][0][0] == 'sidewalk-sea.cs.washington.edu'

    def test_a_scheme_or_path_on_the_host_is_rejected_rather_than_pasted_into_a_url(self, served):
        with pytest.raises(SystemExit):
            schema.main(['--host', 'https://sidewalk-sea.cs.washington.edu/'])

    def test_the_timeout_is_passed_through(self, served):
        schema.main(['--host', 'sidewalk-sea.cs.washington.edu', '--timeout', '5'])
        assert served['asked'][0][1] == 5.0


class TestTheNetworkSeam:
    """fetch_served_fields composes the URL and the reader. _open_stream is the only thing below it, and it
    is what nothing in this suite calls."""

    def test_it_asks_the_endpoint_CropRunner_asks(self, monkeypatch):
        asked = {}

        class FakeResponse:
            def __init__(self, body):
                self._stream = io.BytesIO(body)

            def read(self, n):
                return self._stream.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_open(url, timeout):
            asked['url'] = url
            asked['timeout'] = timeout
            return FakeResponse(payload(SERVED_TODAY))

        monkeypatch.setattr(schema, '_open_stream', fake_open)
        assert schema.fetch_served_fields('sidewalk-sea.cs.washington.edu', timeout=9) == list(SERVED_TODAY)
        assert asked['url'] == 'https://sidewalk-sea.cs.washington.edu/adminapi/labels/cvMetadata'
        assert asked['timeout'] == 9
        # The path CropRunner itself fetches, so the check cannot drift onto a different endpoint. Read off
        # the cropper's source, because that URL is built inline there and is not a constant to import.
        assert schema.ENDPOINT_PATH in inspect.getsource(CropRunner.fetch_cvMetadata_from_server)

    def test_a_transport_error_becomes_SchemaUnavailable_naming_the_cause(self, monkeypatch):
        """An OSError out of main() is a traceback after the summary; the operator needs the reason on the
        line that says the check did not run."""
        def fake_open(url, timeout):
            raise OSError('connection refused')

        monkeypatch.setattr(schema, '_open_stream', fake_open)
        with pytest.raises(schema.SchemaUnavailable) as excinfo:
            schema.fetch_served_fields('sidewalk-sea.cs.washington.edu')
        assert 'connection refused' in str(excinfo.value)


class TestTheReasonAFetchFailedIsNamed:
    """`_describe_failure` is the difference between "UNAVAILABLE: timed out" and a bare exception class.

    The whole value of exit 3 is that the mail says which half broke, so each shape the fetch can actually
    take is pinned - urllib wraps most of them twice, and `str(URLError(...))` on its own prints
    `<urlopen error [Errno 111] ...>`, which reads as a bug in this script.
    """

    def test_an_http_status_is_named_as_one(self):
        error = urllib.error.HTTPError('https://x/y', 502, 'Bad Gateway', {}, None)
        assert schema._describe_failure(error) == 'HTTP 502'

    def test_a_timeout_is_named_a_timeout(self):
        assert schema._describe_failure(urllib.error.URLError(socket.timeout())) == 'timed out'

    def test_a_protocol_error_names_its_class(self):
        reason = http.client.BadStatusLine('garbage')
        described = schema._describe_failure(urllib.error.URLError(reason))
        assert 'BadStatusLine' in described

    def test_anything_else_falls_back_to_its_text(self):
        assert 'connection refused' in schema._describe_failure(OSError('connection refused'))


class TestTheTwoChannels:
    """print is the verdict cron mails; logging is the detail. The repo's usual split, and neither is the
    other's fallback."""

    def test_a_clean_run_prints_no_not_required_line(self):
        """The one required-fields-only case: nothing served beyond what the cropper reads, so the
        informational line is absent rather than printed empty."""
        result = schema.compare_served_fields(('pano_id', 'pano_x', 'pano_y', 'label_id', 'label_type'))
        assert result.not_required == []

    def test_the_report_of_a_lean_deployment_has_no_not_required_line_at_all(self, capsys):
        """The line is omitted, not printed with an empty list: a report that always prints every line is
        one an operator stops reading, and this one is read by eye once a night at most."""
        lean = schema.compare_served_fields(('pano_id', 'pano_x', 'pano_y', 'label_id', 'label_type'))
        assert schema.report('sidewalk-sea.cs.washington.edu', lean) == schema.EXIT_OK
        out = capsys.readouterr().out
        assert 'does not read' not in out
        assert 'OK:' in out

    def test_the_detail_can_be_written_to_a_file(self, tmp_path):
        log_path = tmp_path / 'schema-check.log'
        schema.configure_logging(str(log_path))
        logging.getLogger().info('hello from the check')
        logging.shutdown()
        assert 'hello from the check' in log_path.read_text()

    def test_an_unopenable_log_path_warns_rather_than_failing_the_run(self, tmp_path, caplog):
        """The log is evidence, not cargo - scrape_queue's rule. A bad --log-file must not cost the check."""
        with caplog.at_level(logging.WARNING):
            schema.configure_logging(str(tmp_path / 'no-such-dir' / 'x.log'))
        assert any('Could not open' in record.message for record in caplog.records)

    def test_a_whitespace_only_body_is_unavailable(self):
        """Not "zero fields served": a body carrying nothing says nothing about the schema."""
        read, _ = reader_for(b'   \n  ')
        with pytest.raises(schema.SchemaUnavailable):
            schema.served_fields(read)
