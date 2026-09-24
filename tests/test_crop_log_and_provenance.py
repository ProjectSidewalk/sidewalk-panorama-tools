"""crop.log under a systemic fault (#139), and the per-crop provenance manifest (#111).

Kept in its own file rather than grown onto tests/test_crop_runner.py: both changes are to what the crop
loop *records* rather than to what it cuts, and the helpers are imported from there so the two files
describe one store layout.

No network anywhere. Panos are synthetic JPEGs in a tmp store; metadata is built in-process, or written
as the -f file / stubbed session the three intakes read.
"""

import builtins
import csv
import io
import json
import logging
import os
import subprocess
import sys

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace
# is what makes pytest apply them here.
from test_crop_runner import (  # noqa: F401
    PANO_SIZE, _isolate_logging_state, block_scandir, crop_path, crop_runner, label_row, put_pano,
    reconciles, write_labels_csv)


MALFORMED_PREFIX = 'Skipping malformed label row'


def bad_rows(n, first_label_id=1):
    """n rows the up-front parse rejects, the #123 shape: one fault in the metadata, every row fails."""
    return [label_row(label_id=first_label_id + i, pano_x='not-a-number') for i in range(n)]


def warnings_starting(caplog, prefix):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and r.getMessage().startswith(prefix)]


def suppression_records(caplog):
    return [r.getMessage() for r in caplog.records if 'suppressed' in r.getMessage().lower()]


# ---------------------------------------------------------------------------
# #139, axis 1: one malformed-row warning is bounded in size and names the label
# ---------------------------------------------------------------------------

class TestTheMalformedRowWarningIsBoundedAndNamesTheLabel:
    """The warning used to be `"Skipping malformed label row %r: %s" % (row, e)` - the whole row, ~300-500
    B repr'd, so 260,000 of them were ~100 MB through a 10 MB x 3 rotation. The identifying fields are what
    an operator actually needs from each line, so they are named explicitly and the rest is clipped."""

    def test_the_label_and_pano_are_named_explicitly(self, crop_runner, tmp_path, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(bad_rows(1, first_label_id=7), str(store), str(out))
        [message] = warnings_starting(caplog, MALFORMED_PREFIX)
        assert 'label_id=7' in message
        assert "pano_id='testpano0001'" in message
        # ...and the exception text still says what was wrong with it.
        assert 'not-a-number' in message

    def test_an_unreadable_identifier_reads_as_a_question_mark(self, crop_runner, tmp_path, caplog):
        """A row missing its id, one with a blank id, and one that is not a mapping at all: each is still
        one line that says it cannot say which label it was, rather than a second exception inside the
        handler for the first."""
        rows = [{'pano_id': 'testpano0001', 'pano_x': 'x', 'pano_y': 1, 'label_type_id': 1},
                {'pano_id': '', 'pano_x': 1, 'pano_y': 1, 'label_type_id': 1, 'label_id': ''},
                None]
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(rows, str(tmp_path / 'store'),
                                                    str(tmp_path / 'crops'))
        messages = warnings_starting(caplog, MALFORMED_PREFIX)
        assert counts['errors'] == 3 and len(messages) == 3
        assert 'label_id=?' in messages[0] and "pano_id='testpano0001'" in messages[0]
        assert 'label_id=?' in messages[1] and 'pano_id=?' in messages[1]
        assert 'label_id=?' in messages[2] and 'pano_id=?' in messages[2]

    def test_a_huge_row_is_clipped_with_an_ellipsis(self, crop_runner, tmp_path, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        row = dict(bad_rows(1)[0], canvas_notes='z' * 5000)
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([row], str(store), str(out))
        [message] = warnings_starting(caplog, MALFORMED_PREFIX)
        assert '...' in message
        assert 'z' * (crop_runner.LOG_ROW_REPR_MAX_CHARS + 1) not in message
        assert len(message) < 3 * crop_runner.LOG_ROW_REPR_MAX_CHARS

    def test_a_huge_exception_text_is_clipped_too(self, crop_runner, tmp_path, caplog):
        """The row is not the only unbounded part: a 5,000-digit pano_x parses to inf, and the
        non-finite error repr's the cell back into its own message."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([label_row(pano_x='9' * 5000)], str(store), str(out))
        [message] = warnings_starting(caplog, MALFORMED_PREFIX)
        assert 'non-finite' in message
        assert len(message) < 3 * crop_runner.LOG_ROW_REPR_MAX_CHARS

    def test_a_huge_label_id_is_clipped_at_the_front_of_the_line(self, crop_runner, tmp_path, caplog):
        """Review survivor: LOG_ID_MAX_CHARS was never applied in any test, because every 'huge' test
        put its bulk in another field. The id named first is clipped on its own, so the line stays
        bounded even when the id itself is the pathological field."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        row = label_row(label_id='7' * 5000, pano_x='not-a-number')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([row], str(store), str(out))
        [message] = warnings_starting(caplog, MALFORMED_PREFIX)
        named = message.split('label_id=', 1)[1].split(', pano_id=', 1)[0]
        assert named == repr('7' * 5000)[:crop_runner.LOG_ID_MAX_CHARS] + '...'
        assert len(message) < 3 * crop_runner.LOG_ROW_REPR_MAX_CHARS

    def test_a_short_row_is_not_clipped(self, crop_runner, tmp_path, caplog):
        """Discrimination for the two above: clipping is for the long tail, not a blanket truncation
        that would lose the detail of an ordinary bad row."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(bad_rows(1), str(store), str(out))
        [message] = warnings_starting(caplog, MALFORMED_PREFIX)
        assert repr(bad_rows(1)[0]) in message


# ---------------------------------------------------------------------------
# #139, axis 2: a per-run, per-kind cap on the per-label warnings, with the counts exact
# ---------------------------------------------------------------------------

class TestThePerLabelWarningsAreCappedPerRun:
    """After LOG_WARNINGS_PER_KIND lines of one kind, one line says the rest are suppressed, and the end
    of the run says how many were. Suppressing a LINE must never suppress a COUNT: the counts invariant is
    load-bearing, and the #136 alarm and the exit code are both readings of it."""

    def test_the_shipped_cap_is_pinned(self, crop_runner):
        """Pinned so a change is deliberate. 100 lines of one kind is plenty to diagnose a cause from, and
        at ~300 B a line six kinds cannot reach 1 MB, a tenth of one rotation segment."""
        assert crop_runner.LOG_WARNINGS_PER_KIND == 100

    def test_the_real_cap_bounds_a_flood_of_malformed_rows(self, crop_runner, tmp_path, caplog):
        """At the shipped constant, not a monkeypatched one, so the cap is shown to bind on its own."""
        cap = crop_runner.LOG_WARNINGS_PER_KIND
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(bad_rows(cap + 50), str(store), str(out))
        assert len(warnings_starting(caplog, MALFORMED_PREFIX)) == cap
        assert counts['errors'] == counts['total'] == cap + 50
        assert reconciles(counts)

    def test_one_notice_when_the_cap_binds_and_one_total_at_the_end(self, crop_runner, tmp_path,
                                                                    caplog, monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 3)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(bad_rows(10), str(store), str(out))
        assert counts['errors'] == 10
        notices = suppression_records(caplog)
        assert len(notices) == 2, notices
        # The notice when it starts: which kind, and that the summary's numbers are still whole.
        assert 'malformed_row' in notices[0]
        assert 'exact' in notices[0]
        # The total at the end: seven of ten were not logged.
        assert notices[1].startswith('Suppressed 7 ')

    def test_exactly_at_the_cap_nothing_is_said_to_be_suppressed(self, crop_runner, tmp_path, caplog,
                                                                 monkeypatch):
        """The notice is for a line actually dropped. Announcing suppression on the cap-th warning would
        claim a loss that did not happen."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 3)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(bad_rows(3), str(store), str(out))
        assert len(warnings_starting(caplog, MALFORMED_PREFIX)) == 3
        assert suppression_records(caplog) == []

    @pytest.mark.parametrize('cap', [0, 1, 2, 1000])
    def test_suppression_never_changes_a_count(self, crop_runner, tmp_path, monkeypatch, cap):
        """The same mixed run at four caps returns the identical counts dict: successes, a corrupt-row
        flood, a dims flood and an out-of-frame flood all counted whatever was logged."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', cap)
        store, out = tmp_path / 'store', tmp_path / ('crops%d' % cap)
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=i, pano_x=100 + 40 * i) for i in range(1, 4)]
        labels += bad_rows(6, first_label_id=10)
        labels += [dict(label_row(label_id=20 + i), pano_width=4096, pano_height=2048)
                   for i in range(5)]
        labels += [label_row(label_id=30 + i, pano_y=5000) for i in range(4)]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 18, 'success': 3, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 5, 'out_of_frame': 4, 'shifted_vertically': 0,
                          'errors': 6, 'recut': 0, 'stale_kept': 0}
        assert reconciles(counts)

    def test_stdout_is_untouched_by_the_cap(self, crop_runner, tmp_path, capsys, monkeypatch):
        """print is the operator's channel and logging the durable one (CLAUDE.md); the cap is a crop.log
        concern, so the terminal output is byte-identical at any cap."""
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001')

        def run_at(cap, name):
            monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', cap)
            crop_runner.bulk_extract_crops(bad_rows(8), str(store), str(tmp_path / name))
            return capsys.readouterr().out

        assert run_at(1, 'a') == run_at(1000, 'b')

    def test_each_kind_has_its_own_budget(self, crop_runner, tmp_path, caplog, monkeypatch):
        """A flood of one kind must not silence the first lines of another, which may be the real cause."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 2)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = bad_rows(5) + [dict(label_row(label_id=20 + i), pano_width=4096, pano_height=2048)
                                for i in range(5)]
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert len(warnings_starting(caplog, MALFORMED_PREFIX)) == 2
        assert len([m for m in caplog.messages if 'metadata says' in m]) == 2

    def test_the_total_names_every_kind_and_sums_them(self, crop_runner, tmp_path, caplog,
                                                      monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 1)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = bad_rows(4) + [label_row(label_id=30 + i, pano_y=5000) for i in range(3)]
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(labels, str(store), str(out))
        final = suppression_records(caplog)[-1]
        assert 'malformed_row: 3' in final
        assert 'out_of_frame: 2' in final
        assert final.startswith('Suppressed 5 ')

    def test_the_systemic_alarm_is_still_the_last_line_written(self, crop_runner, tmp_path, capsys):
        """#137's contract: the alarm survives the rotation it caused only because it is written last.
        Driven through main() so it is the real rotating crop.log that is read."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, bad_rows(crop_runner.LOG_WARNINGS_PER_KIND + 5))
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 1
        logged = io.open(os.path.join(str(out), 'crop.log'), encoding='utf-8').read().splitlines()
        assert crop_runner.SYSTEMIC_FAILURE_BANNER in logged[-1]
        # ...and the suppression total is in the log, ahead of it.
        assert any(line.split(':', 2)[-1].startswith('Suppressed 5 ') for line in logged[:-1])


class TestTheOtherPerLabelWarningsShareTheCap:
    """A dead mount, a full disk, or Google re-serving a city wider produces one warning per label or per
    pano of exactly the malformed-row flood's shape, so the same cap applies to each of them by kind."""

    def test_a_failed_write_flood_is_capped(self, crop_runner, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 2)

        def full_disk(*args, **kwargs):
            raise OSError('No space left on device')

        monkeypatch.setattr(crop_runner, 'make_single_crop', full_disk)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=i, pano_x=100 + 40 * i) for i in range(1, 7)]
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['errors'] == 6 and reconciles(counts)
        assert len(warnings_starting(caplog, 'Failed to crop')) == 2
        assert 'crop_failed: 4' in suppression_records(caplog)[-1]

    def test_an_unopenable_pano_flood_is_capped(self, crop_runner, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 2)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        labels = []
        for i in range(5):
            pano_id = 'garbage%05d' % i
            os.makedirs(os.path.join(str(store), pano_id[:2]), exist_ok=True)
            with open(os.path.join(str(store), pano_id[:2], pano_id + '.jpg'), 'wb') as f:
                f.write(b'not a jpeg')
            labels.append(label_row(pano_id=pano_id, label_id=i + 1))
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['errors'] == 5 and reconciles(counts)
        assert len([m for m in caplog.messages if 'cannot open' in m]) == 2
        assert 'cannot_open: 3' in suppression_records(caplog)[-1]

    def test_the_out_of_frame_and_dims_floods_are_capped(self, crop_runner, tmp_path, caplog,
                                                         monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 1)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [label_row(label_id=30 + i, pano_y=5000) for i in range(3)]
        labels += [dict(label_row(label_id=20 + i), pano_width=4096, pano_height=2048)
                   for i in range(4)]
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['out_of_frame'] == 3 and counts['dims_mismatch'] == 4
        assert len([m for m in caplog.messages if 'outside the' in m]) == 1
        assert len([m for m in caplog.messages if 'metadata says' in m]) == 1
        final = suppression_records(caplog)[-1]
        assert 'out_of_frame: 2' in final and 'dims_mismatch: 3' in final

    def test_missing_pano_lines_are_not_capped(self, crop_runner, tmp_path, caplog, monkeypatch):
        """Deliberately outside the cap: a lagging scrape is the normal state of a city rather than a
        fault, the line is per pano rather than per label, and which panos are missing is what an
        operator topping up a store wants listed."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 1)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        store.mkdir()
        labels = [label_row(pano_id='gonepano%04d' % i, label_id=i) for i in range(4)]
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert len([m for m in caplog.messages if 'due to missing image' in m]) == 4
        assert suppression_records(caplog) == []



# ---------------------------------------------------------------------------
# #111: the per-crop provenance manifest
# ---------------------------------------------------------------------------

def manifest_rows(out_dir, crop_runner):
    """The manifest as a list of rows, header included, read with csv so quoting is honoured."""
    with open(os.path.join(str(out_dir), crop_runner.PROVENANCE_MANIFEST), newline='',
              encoding='utf-8') as f:
        return list(csv.reader(f))


def manifest_by_label(out_dir, crop_runner):
    header, *rows = manifest_rows(out_dir, crop_runner)
    assert header == list(crop_runner.PROVENANCE_COLUMNS)
    return {row[0]: dict(zip(header, row)) for row in rows}


def read_marker(out_dir, crop_runner):
    with open(os.path.join(str(out_dir), crop_runner.CROP_RULE_MARKER), encoding='utf-8') as f:
        return json.load(f)


def labelled(label_id, **provenance):
    """A label_row carrying whichever provenance fields are given, and no others."""
    row = label_row(label_id=label_id, pano_x=100 + 40 * label_id)
    row.update(provenance)
    return row


def write_crop_file(out_dir, label_type_id, label_id):
    os.makedirs(os.path.join(str(out_dir), str(label_type_id)), exist_ok=True)
    Image.new('RGB', (4, 4)).save(crop_path(out_dir, label_type_id, label_id))


class TestTheProvenanceManifest:
    """#111. Crops are bare JPEGs and every consumer is an ML dataset, so where the pixels came from has to
    travel with the crop. One row per crop, appended as it lands - the ledgers' contract - and a field the
    metadata does not carry is written empty, never inferred (the #99 rule)."""

    def test_the_column_set_is_pinned(self, crop_runner):
        """`copyright`, not `producer`: it is the name the app gives the producer credit
        (pano_data.copyright, which ImageryAttribution.line reads beside pano_data.license), so a value
        that arrives under it flows through with no renaming in between."""
        assert crop_runner.PROVENANCE_MANIFEST == 'crop_provenance.csv'
        assert crop_runner.PROVENANCE_COLUMNS == (
            'label_id', 'pano_id', 'source', 'copyright', 'license', 'crop_rule_version')

    def test_one_row_per_crop_carrying_what_the_metadata_says(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [labelled(1, source='panoramax', copyright='Jane Doe, Bayonne', license='etalab-2.0'),
                  labelled(2, source='mapillary', copyright='someone', license='CC-BY-SA-4.0')]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['success'] == 2
        rows = manifest_by_label(out, crop_runner)
        assert rows == {
            '1': {'label_id': '1', 'pano_id': 'testpano0001', 'source': 'panoramax',
                  'copyright': 'Jane Doe, Bayonne', 'license': 'etalab-2.0',
                  'crop_rule_version': crop_runner.CROP_RULE_VERSION},
            '2': {'label_id': '2', 'pano_id': 'testpano0001', 'source': 'mapillary',
                  'copyright': 'someone', 'license': 'CC-BY-SA-4.0',
                  'crop_rule_version': crop_runner.CROP_RULE_VERSION}}

    def test_a_field_the_metadata_does_not_carry_is_empty_not_guessed(self, crop_runner, tmp_path):
        """Absent, JSON null and a blank cell all mean 'not stated'. Mapillary's licence is uniform and a
        GSV row states no licence, so a default would be easy to write and would be a claim nobody
        made: an empty cell is the honest record, and it fills in when cvMetadata starts sending it."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        labels = [labelled(1),
                  labelled(2, source='mapillary'),
                  labelled(3, source='panoramax', license=None, copyright='   '),
                  labelled(4, source='', license='', copyright=None),
                  labelled(5, source=float('nan'))]
        crop_runner.bulk_extract_crops(labels, str(store), str(out))
        rows = manifest_by_label(out, crop_runner)
        assert [(rows[i]['source'], rows[i]['copyright'], rows[i]['license'])
                for i in ('1', '2', '3', '4', '5')] == [
            ('', '', ''), ('mapillary', '', ''), ('panoramax', '', ''), ('', '', ''), ('', '', '')]

    def test_only_a_crop_that_landed_gets_a_row(self, crop_runner, tmp_path, monkeypatch):
        """Every non-success outcome: a missing pano, a malformed row, a dims mismatch, a label out of
        frame, an existing crop, and a failed write. None of them put a crop on disk this run, so none of
        them may claim one in the manifest."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        write_crop_file(out, 1, 6)
        real = crop_runner.make_single_crop

        def fail_label_7(pano, pano_x, pano_y, output_filename, draw_mark=False):
            if os.path.basename(output_filename) == '7.jpg':
                raise OSError('No space left on device')
            return real(pano, pano_x, pano_y, output_filename, draw_mark=draw_mark)

        monkeypatch.setattr(crop_runner, 'make_single_crop', fail_label_7)
        labels = [labelled(1, source='gsv'),
                  dict(label_row(pano_id='gonepano0001', label_id=2), source='gsv'),
                  dict(bad_rows(1, first_label_id=3)[0], source='gsv'),
                  dict(labelled(4), pano_width=4096, pano_height=2048, source='gsv'),
                  dict(label_row(label_id=5, pano_y=5000), source='gsv'),
                  labelled(6, source='gsv'),
                  labelled(7, source='gsv')]
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts == {'total': 7, 'success': 1, 'skipped_existing': 1, 'missing_pano': 1,
                          'dims_mismatch': 1, 'out_of_frame': 1, 'shifted_vertically': 0, 'errors': 2,
                          'recut': 0, 'stale_kept': 0}
        assert list(manifest_by_label(out, crop_runner)) == ['1']

    def test_a_recut_crop_gets_a_second_row_and_the_last_names_the_current_rule(
            self, crop_runner, tmp_path, monkeypatch):
        """Review survivor: `if not existed: manifest.record(...)` - nothing tested --force and the
        manifest together, since they were built on separate branches. Every re-cut appends a row, and
        the last row for a label is the one that describes the file."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out))
        monkeypatch.setattr(crop_runner, 'CROP_RULE_VERSION', 'v3-test')
        counts = crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out),
                                                force=True)
        assert counts['recut'] == 1
        rows = [row for row in manifest_rows(out, crop_runner)[1:] if row[0] == '1']
        assert [row[-1] for row in rows] == ['v2', 'v3-test']

    def test_a_manifest_that_cannot_be_opened_raises_before_any_crop(self, crop_runner, tmp_path,
                                                                     monkeypatch):
        """Review survivor: a swallowed open failure would let the run cut every crop with no record.
        Opening it is the store saying it cannot record provenance, so it raises before a crop or a type
        directory exists - after crop_rule.json, which is written first and is not a crop."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        real_open = builtins.open

        def refuse_manifest(file, mode='r', *args, **kwargs):
            if os.path.basename(str(file)) == crop_runner.PROVENANCE_MANIFEST:
                raise PermissionError(13, 'Permission denied', str(file))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(crop_runner, 'open', refuse_manifest, raising=False)
        with pytest.raises(PermissionError):
            crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert sorted(os.listdir(str(out))) == [crop_runner.CROP_RULE_MARKER]

    def test_each_row_is_on_disk_as_its_crop_lands(self, crop_runner, tmp_path, monkeypatch):
        """Read through a second handle while the run is still going: the row for the first crop must
        already be there when the second is cut. Kills both a manifest written at the end of the run and
        one appended but left in the writer's buffer."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        real = crop_runner.make_single_crop
        seen = []

        def spy(pano, pano_x, pano_y, output_filename, draw_mark=False):
            if os.path.basename(output_filename) == '2.jpg':
                seen.append(manifest_rows(out, crop_runner))
            return real(pano, pano_x, pano_y, output_filename, draw_mark=draw_mark)

        monkeypatch.setattr(crop_runner, 'make_single_crop', spy)
        crop_runner.bulk_extract_crops([labelled(1, source='gsv'), labelled(2, source='gsv')],
                                       str(store), str(out))
        [during] = seen
        assert during[0] == list(crop_runner.PROVENANCE_COLUMNS)
        assert [row[0] for row in during[1:]] == ['1']

    def test_a_crashed_run_leaves_a_truthful_partial_file_and_a_rerun_completes_it(
            self, crop_runner, tmp_path, monkeypatch):
        """KeyboardInterrupt is not an Exception, so it escapes the loop exactly as a kill would. What is
        on disk afterwards must be one header and one row per crop that exists; the re-run skips those
        crops, appends the rest, and does not write the header again."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        real = crop_runner.make_single_crop

        def killed_at_3(pano, pano_x, pano_y, output_filename, draw_mark=False):
            if os.path.basename(output_filename) == '3.jpg':
                raise KeyboardInterrupt
            return real(pano, pano_x, pano_y, output_filename, draw_mark=draw_mark)

        labels = [labelled(i, source='gsv') for i in range(1, 5)]
        monkeypatch.setattr(crop_runner, 'make_single_crop', killed_at_3)
        with pytest.raises(KeyboardInterrupt):
            crop_runner.bulk_extract_crops(labels, str(store), str(out))
        partial = manifest_rows(out, crop_runner)
        assert partial[0] == list(crop_runner.PROVENANCE_COLUMNS)
        assert [row[0] for row in partial[1:]] == ['1', '2']
        assert all(os.path.exists(crop_path(out, 1, row[0])) for row in partial[1:])

        monkeypatch.setattr(crop_runner, 'make_single_crop', real)
        counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['skipped_existing'] == 2 and counts['success'] == 2
        final = manifest_rows(out, crop_runner)
        assert final.count(list(crop_runner.PROVENANCE_COLUMNS)) == 1
        assert [row[0] for row in final[1:]] == ['1', '2', '3', '4']

    def test_a_torn_last_line_does_not_swallow_the_next_row(self, crop_runner, tmp_path):
        """A crash mid-append can leave a line with no newline. Appending straight after it would glue the
        next crop's row onto the torn one, corrupting a good row as well as the bad - so the torn line
        is cut away (#153 n2) and the next row starts on a line of its own."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        os.makedirs(str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w', newline='',
                  encoding='utf-8') as f:
            f.write(','.join(crop_runner.PROVENANCE_COLUMNS) + '\n' + '99,torn')
        crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out))
        rows = manifest_rows(out, crop_runner)
        assert rows[1:] == [['1', 'testpano0001', 'gsv', '', '', crop_runner.CROP_RULE_VERSION]]

    def test_an_empty_manifest_file_still_gets_its_header(self, crop_runner, tmp_path):
        """A crash between creating the file and writing the header leaves it zero bytes. 'The file
        exists' is therefore not the test for 'the header is written'."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        os.makedirs(str(out))
        open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w').close()
        crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out))
        assert manifest_rows(out, crop_runner)[0] == list(crop_runner.PROVENANCE_COLUMNS)

    def test_a_run_that_cuts_nothing_still_leaves_a_header(self, crop_runner, tmp_path):
        """The manifest exists from the moment the run starts, like crop_rule.json, so its absence can
        never be mistaken for 'this store has no provenance record'."""
        counts = crop_runner.bulk_extract_crops([label_row()], str(tmp_path / 'store'),
                                                str(tmp_path / 'crops'))
        assert counts['missing_pano'] == 1
        assert manifest_rows(tmp_path / 'crops', crop_runner) == [list(crop_runner.PROVENANCE_COLUMNS)]

    def test_rows_use_one_line_ending(self, crop_runner, tmp_path):
        """'\\n', the ledgers' pin: csv.writer's default is '\\r\\n', which would hand every grep a trailing
        carriage return on the last column."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'rb') as f:
            data = f.read()
        assert b'\r' not in data and data.count(b'\n') == 2


class TestAFailedAppendDoesNotLoseTheCrop:
    """The crop is already on disk and is the resume marker, so a manifest write that fails after it
    cannot be retried by a re-run - the label will be skipped_existing. Counting it as an `error` would
    therefore break the documented promise that errors retry, and would put one label in two buckets.
    It is logged (through the #139 budget) and printed in the summary instead, and the counts are
    exactly what they would have been."""

    @pytest.fixture
    def failing_record(self, crop_runner, monkeypatch):
        def refuse(self, *args, **kwargs):
            raise OSError('Input/output error')
        monkeypatch.setattr(crop_runner.ProvenanceManifest, 'record', refuse)

    def test_the_crop_stays_and_the_counts_are_untouched(self, crop_runner, tmp_path,
                                                         failing_record, capsys, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert counts == {'total': 2, 'success': 2, 'skipped_existing': 0, 'missing_pano': 0,
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 0,
                          'recut': 0, 'stale_kept': 0}
        assert reconciles(counts)
        assert os.path.exists(crop_path(out, 1, 1)) and os.path.exists(crop_path(out, 1, 2))
        # Both channels: the per-label reason in crop.log, the number where the operator reads.
        assert len([m for m in caplog.messages if 'provenance row was not written' in m]) == 2
        assert 'Input/output error' in caplog.text
        printed = capsys.readouterr().out
        summary = '2 crops were written without a row in %s' % crop_runner.PROVENANCE_MANIFEST
        assert summary in printed
        # Both channels (review survivor: the logging.warning deleted, with only stdout asserted).
        assert any(m.startswith(summary) for m in caplog.messages)

    def test_the_summary_promises_only_what_is_true(self, crop_runner, tmp_path, failing_record,
                                                    capsys):
        """#153 m4. It said 'see crop.log for which' - but crop.log names at most LOG_WARNINGS_PER_KIND
        of them - and 'a re-run will not record it', which a --force re-run does."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        printed = capsys.readouterr().out
        assert '(crop.log names up to %d of them)' % crop_runner.LOG_WARNINGS_PER_KIND in printed
        assert ('a plain re-run skips existing crops and will not record it; --force re-cuts them and '
                'writes their rows') in printed
        assert 'see crop.log for which' not in printed

    def test_the_exit_code_is_unchanged(self, crop_runner, tmp_path, failing_record):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [label_row()])
        assert crop_runner.main(['-f', str(csv_file), '-s', str(store), '-o', str(out)]) == 0

    def test_the_unrecorded_warnings_are_capped_like_the_rest(self, crop_runner, tmp_path,
                                                              failing_record, caplog, monkeypatch):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 1)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([labelled(i) for i in range(1, 4)], str(store), str(out))
        assert len([m for m in caplog.messages if 'provenance row was not written' in m]) == 1
        assert 'provenance_unrecorded: 2' in suppression_records(caplog)[-1]

    def test_a_clean_run_prints_nothing_about_it(self, crop_runner, tmp_path, capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert 'without a row' not in capsys.readouterr().out


class _FaultyRaw(io.RawIOBase):
    """The raw (unbuffered) layer under a manifest handle: what a write here does is what the OS did.
    Writes and the close fail on the schedule the owning RawWriterFaults sets."""

    def __init__(self, real, faults):
        super().__init__()
        self._real = real
        self._faults = faults

    def writable(self):
        return True

    def seekable(self):
        return self._real.seekable()

    def readable(self):
        return self._real.readable()

    def fileno(self):
        return self._real.fileno()

    def seek(self, *args):
        return self._real.seek(*args)

    def tell(self):
        return self._real.tell()

    def truncate(self, *args):
        return self._real.truncate(*args)

    def write(self, data):
        self._faults.writes += 1
        n = self._faults.writes
        data = bytes(data)
        if n in self._faults.tear:
            # Half the bytes reach the file, then the device gives out: the torn-row shape.
            self._real.write(data[:len(data) // 2])
            raise OSError(28, 'No space left on device (torn)')
        if self._faults.fail(n):
            raise OSError(28, 'No space left on device')
        return self._real.write(data)

    def close(self):
        if self.closed:
            return
        self._real.close()
        super().close()
        if self._faults.close_fails:
            raise OSError(5, 'Input/output error on close')


class RawWriterFaults:
    """Stands between ProvenanceManifest and the file it appends to, at the RAW layer: the injected
    handle is built the way open() builds one - FileIO, then BufferedWriter and TextIOWrapper when the
    mode asks for them - with the fault under the buffer, so a row the manifest's own buffering keeps
    after a failed write is visible here exactly as it would be on a full disk. Only handles opened for
    appending to the manifest are wrapped; crop_rule.json, the panos and every read are untouched.

    `fail(n)` decides, per 1-based raw write across every handle (on a fresh manifest the header is
    write 1), whether that write raises; `tear` names writes that put half their bytes on disk first.
    Every returned handle is kept, so a test can ask whether each was closed."""

    def __init__(self, crop_runner, monkeypatch, fail=lambda n: False, tear=(), close_fails=False):
        self.fail, self.tear, self.close_fails = fail, set(tear), close_fails
        self.writes = 0
        self.handles = []
        real_open = builtins.open

        def fake_open(file, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
            if os.path.basename(str(file)) != crop_runner.PROVENANCE_MANIFEST or 'a' not in mode:
                return real_open(file, mode, buffering, encoding, errors, newline)
            raw = _FaultyRaw(io.FileIO(file, mode.replace('b', '').replace('t', '')), self)
            if buffering == 0:
                handle = raw
            elif 'b' in mode:
                handle = io.BufferedWriter(raw)
            else:
                handle = io.TextIOWrapper(io.BufferedWriter(raw), encoding=encoding, errors=errors,
                                          newline=newline)
            self.handles.append(handle)
            return handle

        monkeypatch.setattr(crop_runner, 'open', fake_open, raising=False)


class TestAFailingManifestCannotTakeTheSummaryDown:
    """#153 M1. A failed append was caught and counted, but its bytes could still be waiting in the
    writer, and manifest.close() ran after the loop unguarded: on the full-store night the close
    flushed, hit ENOSPC again and raised out of bulk_extract_crops, so the summary, the unrecorded total
    and the #136 alarm - the lines that night exists to produce - were never written."""

    def test_a_raw_writer_failing_from_the_second_write_leaves_the_whole_summary(
            self, crop_runner, tmp_path, monkeypatch, capsys, caplog):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n >= 2, close_fails=True)
        # Two crops that land, and three malformed rows so errors dominate and the alarm must fire.
        labels = [labelled(1), labelled(2)] + bad_rows(3, first_label_id=10)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops(labels, str(store), str(out))
        assert counts['success'] == 2 and counts['errors'] == 3 and reconciles(counts)
        printed = capsys.readouterr().out
        assert '2 crops extracted' in printed
        assert '2 crops were written without a row' in printed
        assert crop_runner.SYSTEMIC_FAILURE_BANNER in printed.splitlines()[-1]
        assert any(crop_runner.SYSTEMIC_FAILURE_BANNER in m for m in caplog.messages)

    def test_a_close_that_raises_is_reported_on_both_channels_not_raised(
            self, crop_runner, tmp_path, monkeypatch, capsys, caplog):
        """The close itself failing - an sshfs mount reporting a deferred write error at close(2) - with
        every append having succeeded. The crops are fine; whether their rows reached the store is not
        known, so it is said on both channels and the run still returns its counts."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        real_close = crop_runner.ProvenanceManifest.close

        def close_then_fail(self):
            real_close(self)
            raise OSError(5, 'Input/output error on close')

        monkeypatch.setattr(crop_runner.ProvenanceManifest, 'close', close_then_fail)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert counts['success'] == 1 and reconciles(counts)
        printed = capsys.readouterr().out
        assert 'could not be closed' in printed and 'Input/output error on close' in printed
        assert any('could not be closed' in m for m in caplog.messages)
        assert 'Crop sizing rule' in printed

    def test_the_handle_is_closed_after_a_normal_run(self, crop_runner, tmp_path, monkeypatch):
        """Surviving mutant: `manifest.close()` deleted. Nothing noticed the handle left open."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        faults = RawWriterFaults(crop_runner, monkeypatch)
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert faults.handles and all(h.closed for h in faults.handles)

    def test_the_handle_is_closed_after_a_raise(self, crop_runner, tmp_path, monkeypatch):
        """KeyboardInterrupt escapes the loop as a kill would; the finally still closes the handle."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        faults = RawWriterFaults(crop_runner, monkeypatch)
        real = crop_runner.make_single_crop

        def killed_at_2(pano, pano_x, pano_y, output_filename, draw_mark=False):
            if os.path.basename(output_filename) == '2.jpg':
                raise KeyboardInterrupt
            return real(pano, pano_x, pano_y, output_filename, draw_mark=draw_mark)

        monkeypatch.setattr(crop_runner, 'make_single_crop', killed_at_2)
        with pytest.raises(KeyboardInterrupt):
            crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert faults.handles and all(h.closed for h in faults.handles)


def row_ids(out_dir, crop_runner):
    """The label ids of the manifest's rows, after checking the header and that every row is whole."""
    header, *rows = manifest_rows(out_dir, crop_runner)
    assert header == list(crop_runner.PROVENANCE_COLUMNS)
    assert all(len(row) == len(header) for row in rows), rows
    return [row[0] for row in rows]


class TestAFailedAppendLeavesNoRowBehind:
    """#153 M2. The unrecorded count has to be true in both directions. A failed append used to leave its
    row in the writer's buffer, and the next successful flush wrote it - so rows reported as "not
    recorded" were in the file. And a raw write that failed partway left half a row in the MIDDLE of
    the file, followed by whole ones, where the open-time repair never looks."""

    def test_rows_whose_append_failed_are_not_in_the_file(self, crop_runner, tmp_path, monkeypatch,
                                                          capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        # Raw write 1 is the header, so appends 2 and 3 are writes 3 and 4; 4 and 5 then succeed.
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n in (3, 4))
        counts = crop_runner.bulk_extract_crops([labelled(i) for i in range(1, 6)], str(store), str(out))
        assert counts['success'] == 5
        assert row_ids(out, crop_runner) == ['1', '4', '5']
        assert '2 crops were written without a row' in capsys.readouterr().out

    def test_a_write_torn_partway_leaves_no_half_row_mid_file(self, crop_runner, tmp_path, monkeypatch,
                                                              capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        RawWriterFaults(crop_runner, monkeypatch, tear=(3,))
        crop_runner.bulk_extract_crops([labelled(i, copyright='Doe, J') for i in range(1, 5)],
                                       str(store), str(out))
        assert row_ids(out, crop_runner) == ['1', '3', '4']
        printed = capsys.readouterr().out
        assert '1 crops were written without a row' in printed
        # The fragment this run's own failed append left is cut on the reopen, but it is THIS run's row,
        # already counted as unrecorded - not a previous run killed mid-append, which is what that notice
        # reports.
        assert 'ended in a torn row' not in printed

    def test_a_tear_on_the_last_append_is_cut_by_the_same_run(self, crop_runner, tmp_path, monkeypatch,
                                                              capsys, caplog):
        """#153 final F4. The tear on the LAST append of the run used to stay on disk until the next
        run's first open cut it - which then reported it as "a previous run was killed while appending
        it", about a row the tearing run had already reported as unrecorded. The failed append now
        reopens (and so cuts) at once, so no run ends torn."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        RawWriterFaults(crop_runner, monkeypatch, tear=(3,))
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        monkeypatch.undo()
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'rb') as f:
            assert f.read().endswith(b'\n')
        assert row_ids(out, crop_runner) == ['1']
        assert '1 crops were written without a row' in capsys.readouterr().out

        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([labelled(3)], str(store), str(out))
        assert 'torn row' not in capsys.readouterr().out
        assert not any('torn row' in m for m in caplog.messages)
        assert row_ids(out, crop_runner) == ['1', '3']
        manifest = crop_runner.ProvenanceManifest(str(out))
        manifest.close()
        assert manifest.torn_rows_cut == 0

    def test_a_reopen_that_fails_leaves_the_next_row_to_reopen_it(self, crop_runner, tmp_path,
                                                                  monkeypatch, capsys, caplog):
        """The reopen after a failed append is best-effort: if it cannot open the file, the handle stays
        dropped and the next row's record() reopens - and cuts - instead. The label's warning names the
        append's own failure, not the reopen's."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        faults = RawWriterFaults(crop_runner, monkeypatch, tear=(3,))
        opened = []
        wrapped_open = crop_runner.open

        def second_append_open_fails(file, mode='r', *args, **kwargs):
            if os.path.basename(str(file)) == crop_runner.PROVENANCE_MANIFEST and 'a' in mode:
                opened.append(mode)
                if len(opened) == 2:
                    raise OSError(5, 'Input/output error on reopen')
            return wrapped_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(crop_runner, 'open', second_append_open_fails, raising=False)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([labelled(i) for i in (1, 2, 3)], str(store), str(out))
        assert counts['success'] == 3 and len(opened) == 3
        [unrecorded] = [m for m in caplog.messages if 'provenance row was not written' in m]
        assert '(torn)' in unrecorded and 'on reopen' not in unrecorded
        assert row_ids(out, crop_runner) == ['1', '3']
        assert '1 crops were written without a row' in capsys.readouterr().out
        assert faults.handles and all(h.closed for h in faults.handles)


class TestTheRawWriteHelpers:
    """The two helpers under the manifest's all-or-nothing row, driven directly: the shapes they exist
    for (a short raw write, a torn tail longer than one read) do not occur on a local test disk."""

    class _Trickle:
        """A raw handle that takes at most `per_call` bytes per write, or returns `answer` instead."""

        TRICKLE = object()

        def __init__(self, per_call=3, answer=TRICKLE):
            self.data, self.per_call, self.answer = b'', per_call, answer

        def write(self, view):
            if self.answer is not self.TRICKLE:
                return self.answer
            taken = bytes(view[:self.per_call])
            self.data += taken
            return len(taken)

    def test_a_short_write_is_continued_until_the_row_is_whole(self, crop_runner):
        handle = self._Trickle(per_call=3)
        crop_runner._write_all(handle, b'1,testpano0001,,,,v2\n')
        assert handle.data == b'1,testpano0001,,,,v2\n'

    @pytest.mark.parametrize('answer', [0, None])
    def test_a_write_that_takes_nothing_raises_rather_than_spinning(self, crop_runner, answer):
        with pytest.raises(OSError, match='short write'):
            crop_runner._write_all(self._Trickle(answer=answer), b'row\n')

    @pytest.mark.parametrize('content, expected', [
        (b'', 0),
        (b'header\n', 7),
        (b'header\nrow', 7),
        (b'no newline at all', 0),
        # A tail longer than one 64 KiB read: the scan has to step back a chunk to find the newline.
        (b'header\n' + b'x' * 70000, 7),
        (b'y' * 70000, 0),
    ], ids=['empty', 'whole', 'torn-row', 'torn-header', 'long-torn-row', 'long-torn-header'])
    def test_the_end_of_the_last_whole_line(self, crop_runner, content, expected):
        assert crop_runner._end_of_last_line(io.BytesIO(content), len(content)) == expected

    def test_a_header_that_cannot_be_written_stops_the_run_before_any_crop(self, crop_runner, tmp_path,
                                                                            monkeypatch):
        """The header is the first write, so a store that cannot take it cannot record provenance at
        all: that is the open failing, and it raises before a crop or a type directory exists."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        faults = RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: True)
        with pytest.raises(OSError, match='No space left'):
            crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert not (out / '1').exists()
        assert all(h.closed for h in faults.handles)


class TestTheOpenTimeRepairCutsBackToTheLastWholeLine:
    """#153 n2. A crash mid-append leaves a last line with no newline. The repair used to append a '\\n'
    after it - which, when the tear fell inside a quoted field, sat inside the open quote, so the csv
    reader swallowed every following row into that field. The torn row's crop is on disk and cannot
    be recorded by anything now, so the row is cut back to the last '\\n' rather than closed off."""

    def write_manifest(self, crop_runner, out, text):
        os.makedirs(str(out), exist_ok=True)
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w', newline='',
                  encoding='utf-8') as f:
            f.write(text)

    def test_a_tear_inside_a_quoted_field_does_not_swallow_later_rows(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        header = ','.join(crop_runner.PROVENANCE_COLUMNS)
        good = '98,testpano0001,mapillary,"Doe, J",CC-BY-SA-4.0,v2'
        self.write_manifest(crop_runner, out,
                            header + '\n' + good + '\n' + '99,testpano0001,mapillary,"Roe, R')
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert row_ids(out, crop_runner) == ['98', '1', '2']
        assert manifest_by_label(out, crop_runner)['98']['copyright'] == 'Doe, J'

    def test_a_torn_header_is_rewritten(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        self.write_manifest(crop_runner, out, 'label_id,pano_id,sou')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert manifest_rows(out, crop_runner)[0] == list(crop_runner.PROVENANCE_COLUMNS)
        assert row_ids(out, crop_runner) == ['1']


class TestTheMarkerSaysWhetherTheManifestHasAKnownGap:
    """Crops cut before the manifest existed have no row unless a --force pass re-cuts them. A consumer
    needs to know whether a manifest can be read as 'every crop here', and the rule version cannot tell
    it: a v2 store cropped before this change and one cropped after both say v2. So crop_rule.json
    records whether the store already held crops when the manifest was started, keeps that answer on
    later runs, and turns it false - for good - the first time a run knows it left a crop without a row
    (#153 M3). It is named for what it records: no KNOWN gap, not coverage."""

    def test_the_key_is_named_for_what_it_records(self, crop_runner, tmp_path):
        """#153 M3(b). It was `provenance_manifest_complete_from_start`, which a --force pass that
        re-cut every crop could never make true and which a later lost row never made false."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert 'provenance_manifest_complete_from_start' not in marker
        assert marker['provenance_manifest_no_known_gap'] is True

    def test_a_fresh_store_records_a_complete_manifest(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['provenance_manifest'] == crop_runner.PROVENANCE_MANIFEST
        assert marker['provenance_manifest_started_under'] == crop_runner.CROP_RULE_VERSION
        assert marker['provenance_manifest_no_known_gap'] is True

    def test_a_store_with_crops_but_no_manifest_records_a_partial_one(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        write_crop_file(out, 1, 99)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert read_marker(out, crop_runner)['provenance_manifest_no_known_gap'] is False

    def test_files_that_are_not_crops_do_not_make_it_partial(self, crop_runner, tmp_path):
        """crop.log and a half-written .part sit in the store without being crops, and a stray
        non-numeric directory is not a label-type shard."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        for name in ('crop.log', os.path.join('1', '5.jpg.part'), os.path.join('notes', 'a.jpg')):
            os.makedirs(os.path.dirname(os.path.join(str(out), name)), exist_ok=True)
            with open(os.path.join(str(out), name), 'w') as f:
                f.write('x')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert read_marker(out, crop_runner)['provenance_manifest_no_known_gap'] is True

    def test_the_answer_is_kept_on_later_runs(self, crop_runner, tmp_path):
        """The second run finds crops on disk - the first run's - and must not re-derive 'partial' from
        them: the manifest has a row for every one."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['provenance_manifest_no_known_gap'] is True
        assert marker['provenance_manifest_started_under'] == crop_runner.CROP_RULE_VERSION

    def test_the_starting_rule_is_kept_when_the_rule_moves(self, crop_runner, tmp_path, monkeypatch):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        monkeypatch.setattr(crop_runner, 'CROP_RULE_VERSION', 'v3-test')
        crop_runner.bulk_extract_crops([labelled(2)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['crop_rule_version'] == 'v3-test'
        assert marker['provenance_manifest_started_under'] == 'v2'
        # ...while each row still says which rule cut its own crop.
        rows = manifest_by_label(out, crop_runner)
        assert rows['1']['crop_rule_version'] == 'v2' and rows['2']['crop_rule_version'] == 'v3-test'

    def test_a_manifest_the_marker_knows_nothing_about_is_unknown_not_complete(self, crop_runner,
                                                                              tmp_path):
        """A manifest on disk with no record of how it started (copied in, or a marker rewritten by
        hand) is not evidence of completeness either way, so the answer is null rather than a guess."""
        out = tmp_path / 'crops'
        os.makedirs(str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w') as f:
            f.write(','.join(crop_runner.PROVENANCE_COLUMNS) + '\n')
        crop_runner.write_rule_marker(str(out))
        marker = read_marker(out, crop_runner)
        assert marker['provenance_manifest_no_known_gap'] is None
        assert marker['provenance_manifest_started_under'] is None

    def test_a_marker_that_is_json_but_not_an_object_is_rewritten(self, crop_runner, tmp_path):
        """Reading more than one key out of the marker means reading it as a dict. Valid JSON that is
        not an object (a hand edit, a truncation that happens to parse) used to die on `.get` with an
        AttributeError the OSError/ValueError guard does not catch; it is provenance, not a lock."""
        out = tmp_path / 'crops'
        os.makedirs(str(out))
        with open(os.path.join(str(out), crop_runner.CROP_RULE_MARKER), 'w') as f:
            f.write('[1, 2]')
        assert crop_runner.write_rule_marker(str(out)) is None
        assert read_marker(out, crop_runner)['crop_rule_version'] == crop_runner.CROP_RULE_VERSION

    def test_a_deleted_manifest_is_restarted_as_partial(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        os.remove(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST))
        crop_runner.bulk_extract_crops([labelled(2)], str(store), str(out))
        assert read_marker(out, crop_runner)['provenance_manifest_no_known_gap'] is False


class TestWhetherTheStoreAlreadyHoldsCrops:
    """_store_holds_crops decides the gap flag's starting value, so it has to find a crop in ANY numeric
    shard, and it has to refuse to guess about one it cannot read."""

    @pytest.mark.parametrize('empty, full', [('1', '9'), ('9', '1')])
    def test_an_empty_shard_does_not_hide_a_crop_in_another(self, crop_runner, tmp_path, empty, full):
        """Surviving mutant: returning after the first numeric shard. Both orders, because scandir order
        is the filesystem's - alphabetical on NTFS, hash order on ext4."""
        (tmp_path / empty).mkdir()
        write_crop_file(tmp_path, full, 5)
        assert crop_runner._store_holds_crops(str(tmp_path)) is True

    def test_empty_shards_hold_no_crops(self, crop_runner, tmp_path):
        (tmp_path / '1').mkdir()
        (tmp_path / '9').mkdir()
        assert crop_runner._store_holds_crops(str(tmp_path)) is False

    def test_an_unreadable_shard_stops_the_run_before_any_crop(self, crop_runner, tmp_path, monkeypatch):
        """#153 m1's second half. The production guard never lists numeric shards (they are this tool's
        own ~400k crops), so an unreadable one first meets _store_holds_crops. Skipping it could record a
        pre-manifest store as gap-free; crashing mid-run would be worse. So it raises before anything is
        cut, naming the shard, as a manifest that cannot be opened does."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        (out / '7').mkdir(parents=True)
        block_scandir(crop_runner, monkeypatch, out / '7')
        with pytest.raises(PermissionError) as raised:
            crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert os.path.join(str(out), '7') in str(raised.value)
        assert 'already holds crops' in str(raised.value)
        assert not (out / '1').exists()
        assert not (out / crop_runner.PROVENANCE_MANIFEST).exists()


class TestAKnownGapTurnsTheMarkerFalseForGood:
    """#153 M3(a). The flag used to stay true after a run wrote crops without rows, and every later run
    carried the true forward - so the marker a consumer is told to trust said 'no gap' for good on
    exactly the store that had one."""

    @pytest.fixture
    def store(self, tmp_path):
        store = tmp_path / 'store'
        put_pano(store, 'testpano0001')
        return store

    def gap(self, out, crop_runner):
        return read_marker(out, crop_runner)['provenance_manifest_no_known_gap']

    def test_an_unrecorded_row_turns_it_false(self, crop_runner, tmp_path, store, monkeypatch):
        out = tmp_path / 'crops'
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 3)
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert self.gap(out, crop_runner) is False

    def test_the_flip_keeps_every_other_key(self, crop_runner, tmp_path, store, monkeypatch):
        out = tmp_path / 'crops'
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 2)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['crop_rule_version'] == crop_runner.CROP_RULE_VERSION
        assert marker['provenance_manifest_started_under'] == crop_runner.CROP_RULE_VERSION
        assert marker['crop_max_stored_width'] == crop_runner.CROP_MAX_STORED_WIDTH

    def test_an_absent_marker_is_rebuilt_around_the_flag(self, crop_runner, tmp_path):
        """Nothing is on disk to lose, so the gap is recorded on its own; write_rule_marker restores the
        rule keys on the next run."""
        crop_runner._record_manifest_gap(str(tmp_path))
        assert read_marker(tmp_path, crop_runner) == {'provenance_manifest_no_known_gap': False}

    @pytest.mark.parametrize('content', ['{not json', '[1, 2]'], ids=['unparseable', 'not-an-object'])
    def test_a_marker_it_cannot_read_is_left_as_it_is(self, crop_runner, tmp_path, content):
        """#153 final F2. Rebuilding around the one key threw away every constant, crop_rule_version,
        provenance_manifest_started_under (for good: the next run carries None forward) and
        previous_crop_rule_version. A marker it cannot read is not overwritten; the raise reaches the
        caller's "could not record the gap" message."""
        path = tmp_path / crop_runner.CROP_RULE_MARKER
        path.write_text(content, encoding='utf-8')
        with pytest.raises((OSError, ValueError)):
            crop_runner._record_manifest_gap(str(tmp_path))
        assert path.read_text(encoding='utf-8') == content

    def test_a_transient_read_failure_leaves_the_marker_byte_identical_and_is_said(
            self, crop_runner, tmp_path, store, monkeypatch, capsys, caplog):
        out = tmp_path / 'crops'
        marker_path = os.path.join(str(out), crop_runner.CROP_RULE_MARKER)
        real_gap, real_load = crop_runner._record_manifest_gap, json.load
        seen = {}

        def gap_whose_first_read_fails(destination_dir):
            with open(marker_path, 'rb') as f:
                seen['before'] = f.read()
            calls = []

            def flaky_load(fp, *args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError(5, 'Input/output error (transient)')
                return real_load(fp, *args, **kwargs)

            with monkeypatch.context() as m:
                m.setattr(json, 'load', flaky_load)
                return real_gap(destination_dir)

        monkeypatch.setattr(crop_runner, '_record_manifest_gap', gap_whose_first_read_fails)
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 2)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert counts['success'] == 1
        with open(marker_path, 'rb') as f:
            assert f.read() == seen['before']
        printed = capsys.readouterr().out
        assert 'could not record the gap' in printed and 'transient' in printed
        assert any('could not record the gap' in m and 'transient' in m for m in caplog.messages)

    def test_a_clean_run_leaves_it_true(self, crop_runner, tmp_path, store):
        """Discrimination for the above: the flip is keyed on a gap, not on the run ending."""
        out = tmp_path / 'crops'
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert self.gap(out, crop_runner) is True

    def test_false_is_sticky_across_later_clean_runs(self, crop_runner, tmp_path, store, monkeypatch):
        out = tmp_path / 'crops'
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 3)
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        monkeypatch.undo()
        crop_runner.bulk_extract_crops([labelled(3)], str(store), str(out))
        crop_runner.bulk_extract_crops([labelled(4)], str(store), str(out), force=True)
        assert self.gap(out, crop_runner) is False

    def test_a_close_that_fails_turns_it_false(self, crop_runner, tmp_path, store, monkeypatch):
        """#153 M1's path: whether the last rows reached the store is unknown, so it is not 'no gap'."""
        out = tmp_path / 'crops'
        real_close = crop_runner.ProvenanceManifest.close

        def close_then_fail(self):
            real_close(self)
            raise OSError(5, 'Input/output error on close')

        monkeypatch.setattr(crop_runner.ProvenanceManifest, 'close', close_then_fail)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert self.gap(out, crop_runner) is False

    def test_a_torn_row_cut_at_open_turns_it_false(self, crop_runner, tmp_path, store, capsys, caplog):
        """A torn last row is a previous run killed mid-append: its crop is on disk with no row now.
        Said on both channels, since nothing else will ever mention that crop."""
        out = tmp_path / 'crops'
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'a', encoding='utf-8') as f:
            f.write('2,testpano0001,gs')
        capsys.readouterr()
        with caplog.at_level(logging.WARNING):
            crop_runner.bulk_extract_crops([labelled(3)], str(store), str(out))
        assert self.gap(out, crop_runner) is False
        assert 'ended in a torn row' in capsys.readouterr().out
        assert any('ended in a torn row' in m for m in caplog.messages)

    def plant_torn_row_under_a_true_flag(self, crop_runner, store, out):
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert self.gap(out, crop_runner) is True
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'a', encoding='utf-8') as f:
            f.write('2,testpano0001,gs')

    def test_a_run_killed_outright_after_the_cut_still_leaves_the_gap_recorded(self, crop_runner,
                                                                               tmp_path, store):
        """#153 final F1. The open cuts the torn row - the only evidence of it - so the gap has to be on
        disk before the cut, not in the run's finally: a SIGKILL, an OOM on a 384 MB decode or an
        unhandled SIGTERM runs no finally at all. os._exit is that kill, in a real process."""
        out = tmp_path / 'crops'
        self.plant_torn_row_under_a_true_flag(crop_runner, store, out)
        script = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import CropRunner\n"
            "def killed(*args, **kwargs):\n"
            "    os._exit(9)\n"
            "CropRunner.make_single_crop = killed\n"
            "CropRunner.bulk_extract_crops([{'pano_id': 'testpano0001', 'pano_x': 300, 'pano_y': 512,\n"
            "                                'label_type_id': 1, 'label_id': 3}], sys.argv[2], sys.argv[3])\n"
            "os._exit(0)\n")
        done = subprocess.run([sys.executable, '-c', script, REPO_ROOT, str(store), str(out)],
                              capture_output=True, timeout=120)
        assert done.returncode == 9, done.stderr
        assert self.gap(out, crop_runner) is False
        assert row_ids(out, crop_runner) == ['1']

    def test_an_append_open_that_fails_after_the_cut_still_leaves_the_gap_recorded(
            self, crop_runner, tmp_path, store, monkeypatch):
        out = tmp_path / 'crops'
        self.plant_torn_row_under_a_true_flag(crop_runner, store, out)
        real_open = builtins.open

        def no_append(file, mode='r', *args, **kwargs):
            if os.path.basename(str(file)) == crop_runner.PROVENANCE_MANIFEST and 'a' in mode:
                raise OSError(5, 'Input/output error')
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(crop_runner, 'open', no_append, raising=False)
        with pytest.raises(OSError, match='Input/output error'):
            crop_runner.bulk_extract_crops([labelled(3)], str(store), str(out))
        monkeypatch.undo()
        assert self.gap(out, crop_runner) is False

    def test_a_gap_that_cannot_be_recorded_fails_the_run_and_keeps_the_torn_row(
            self, crop_runner, tmp_path, store, monkeypatch):
        """If the record cannot land, the cut must not happen either: the torn row is then the only
        evidence left, and the run stops before any crop rather than cutting on regardless."""
        out = tmp_path / 'crops'
        self.plant_torn_row_under_a_true_flag(crop_runner, store, out)

        def refuse(destination_dir):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(crop_runner, '_record_manifest_gap', refuse)
        with pytest.raises(OSError, match='No space left'):
            crop_runner.bulk_extract_crops([labelled(3)], str(store), str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), encoding='utf-8') as f:
            assert f.read().endswith('\n2,testpano0001,gs')
        assert not os.path.exists(crop_path(out, 1, 3))

    def test_a_clean_run_says_nothing_about_a_torn_row(self, crop_runner, tmp_path, store, capsys):
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(tmp_path / 'crops'))
        assert 'torn row' not in capsys.readouterr().out

    def test_a_torn_header_is_not_a_gap(self, crop_runner, tmp_path, store):
        """A torn HEADER means no row was ever written after it, so no crop lost one."""
        out = tmp_path / 'crops'
        os.makedirs(str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w', encoding='utf-8') as f:
            f.write('label_id,pa')
        crop_runner.write_rule_marker(str(out))
        marker = read_marker(out, crop_runner)
        marker['provenance_manifest_no_known_gap'] = True
        with open(os.path.join(str(out), crop_runner.CROP_RULE_MARKER), 'w', encoding='utf-8') as f:
            json.dump(marker, f)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert self.gap(out, crop_runner) is True

    def test_the_gap_is_recorded_even_when_the_run_is_killed(self, crop_runner, tmp_path, store,
                                                             monkeypatch):
        out = tmp_path / 'crops'
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 2)
        real = crop_runner.make_single_crop

        def killed_at_2(pano, pano_x, pano_y, output_filename, draw_mark=False):
            if os.path.basename(output_filename) == '2.jpg':
                raise KeyboardInterrupt
            return real(pano, pano_x, pano_y, output_filename, draw_mark=draw_mark)

        monkeypatch.setattr(crop_runner, 'make_single_crop', killed_at_2)
        with pytest.raises(KeyboardInterrupt):
            crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        assert self.gap(out, crop_runner) is False

    def test_a_forced_recut_of_every_crop_does_not_clear_it(self, crop_runner, tmp_path, store):
        """#153 M3(b), decided: the marker records what runs REPORTED, and a forced pass cannot tell
        from the marker that it filled every gap - a pre-manifest crop whose label is not in this run's
        metadata stays row-less. So false stays false; coverage is rows against crops on disk."""
        out = tmp_path / 'crops'
        write_crop_file(out, 1, 1)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out), force=True)
        assert self.gap(out, crop_runner) is False
        assert row_ids(out, crop_runner) == ['1']

    def test_a_marker_that_cannot_be_updated_is_said_not_raised(self, crop_runner, tmp_path, store,
                                                               monkeypatch, capsys, caplog):
        out = tmp_path / 'crops'

        def refuse(destination_dir):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(crop_runner, '_record_manifest_gap', refuse, raising=False)
        RawWriterFaults(crop_runner, monkeypatch, fail=lambda n: n == 2)
        with caplog.at_level(logging.WARNING):
            counts = crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert counts['success'] == 1
        printed = capsys.readouterr().out
        assert 'could not record the gap' in printed and 'No space left' in printed
        assert any('could not record the gap' in m for m in caplog.messages)
        assert 'crops were written without a row' in printed


# ---------------------------------------------------------------------------
# #111: the provenance columns are optional on all three intakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    trust_env = False

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, **kwargs):
        return _FakeResponse(self._payload)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_through_intake(crop_runner, tmp_path, monkeypatch, intake, rows):
    """Drive run() through one of the three intakes, -f CSV, -f JSON or -d, with exactly these rows."""
    store, out = tmp_path / 'store', tmp_path / 'crops'
    put_pano(store, 'testpano0001')
    if intake == 'csv':
        path = tmp_path / 'labels.csv'
        fields = []
        for row in rows:
            fields += [k for k in row if k not in fields]
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        counts = crop_runner.run(None, str(path), str(store), str(out))
    elif intake == 'json':
        path = tmp_path / 'labels.json'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(rows, f)
        counts = crop_runner.run(None, str(path), str(store), str(out))
    else:
        monkeypatch.setattr(crop_runner, 'request_session', lambda: _FakeSession(rows))
        counts = crop_runner.run('sidewalk-test.invalid', None, str(store), str(out))
    return counts, manifest_by_label(out, crop_runner)


INTAKES = ['csv', 'json', 'server']


class TestTheProvenanceColumnsAreOptionalOnEveryIntake:
    """cvMetadata sends none of source, copyright or license today (docs/api-fields.md; the capture in
    samples/cvmetadata-seattle.csv has none of them). A metadata source lacking them has to stay valid,
    and one carrying them has to deliver them to the manifest unchanged, on each of the three intakes."""

    def test_the_required_columns_did_not_grow(self, crop_runner):
        assert crop_runner.REQUIRED_LABEL_COLUMNS == ('pano_id', 'pano_x', 'pano_y', 'label_id')

    @pytest.mark.parametrize('intake', INTAKES)
    def test_metadata_without_them_is_still_valid(self, crop_runner, tmp_path, monkeypatch, intake):
        counts, rows = run_through_intake(crop_runner, tmp_path, monkeypatch, intake,
                                          [label_row(label_id=1)])
        assert counts['success'] == 1 and counts['errors'] == 0
        assert (rows['1']['source'], rows['1']['copyright'], rows['1']['license']) == ('', '', '')

    @pytest.mark.parametrize('intake', INTAKES)
    def test_metadata_with_them_flows_through(self, crop_runner, tmp_path, monkeypatch, intake):
        counts, rows = run_through_intake(
            crop_runner, tmp_path, monkeypatch, intake,
            [dict(label_row(label_id=1), source='panoramax', copyright='Jane Doe',
                  license='CC-BY-SA-4.0')])
        assert counts['success'] == 1
        assert (rows['1']['source'], rows['1']['copyright'], rows['1']['license']) == (
            'panoramax', 'Jane Doe', 'CC-BY-SA-4.0')

    def test_the_current_cvmetadata_capture_crops_with_empty_provenance(self, crop_runner, tmp_path):
        """samples/cvmetadata-seattle.csv IS the 2026-09-18 sidewalk-sea response, so this is what a -d
        run against a deployment produces today: crops, with all three provenance fields empty."""
        sample = os.path.join(REPO_ROOT, 'samples', 'cvmetadata-seattle.csv')
        labels = crop_runner.fetch_label_ids_csv(sample)
        assert not any(k in labels[0] for k in ('source', 'copyright', 'license'))
        # Every column of the capture, with the geometry scaled onto a small pano so the test does not
        # decode a 16384x8192 image.
        first = dict(labels[0], pano_width=str(PANO_SIZE[0]), pano_height=str(PANO_SIZE[1]),
                     pano_x='200', pano_y=str(PANO_SIZE[1] // 2))
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, first['pano_id'])
        counts = crop_runner.bulk_extract_crops([first], str(store), str(out))
        assert counts['success'] == 1
        row = manifest_by_label(out, crop_runner)[first['label_id']]
        assert (row['source'], row['copyright'], row['license']) == ('', '', '')
