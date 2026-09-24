"""crop.log under a systemic fault (#139), and the per-crop provenance manifest (#111).

Kept in its own file rather than grown onto tests/test_crop_runner.py: both changes are to what the crop
loop *records* rather than to what it cuts, and the helpers are imported from there so the two files
describe one store layout.

No network anywhere. Panos are synthetic JPEGs in a tmp store; metadata is built in-process, or written
as the -f file / stubbed session the three intakes read.
"""

import csv
import io
import json
import logging
import os
import sys

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace
# is what makes pytest apply them here.
from test_crop_runner import (  # noqa: F401
    PANO_SIZE, _isolate_logging_state, crop_path, crop_runner, label_row, put_pano, reconciles,
    write_labels_csv)


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
        assert '7' in notices[1]

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
                          'errors': 6, 'recut': 0}
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
        GSV row has no licence at all, so a default would be easy to write and would be a claim nobody
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
                          'dims_mismatch': 1, 'out_of_frame': 1, 'shifted_vertically': 0, 'errors': 2, 'recut': 0}
        assert list(manifest_by_label(out, crop_runner)) == ['1']

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
        next crop's row onto the torn one, corrupting a good row as well as the bad."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        os.makedirs(str(out))
        with open(os.path.join(str(out), crop_runner.PROVENANCE_MANIFEST), 'w', newline='',
                  encoding='utf-8') as f:
            f.write(','.join(crop_runner.PROVENANCE_COLUMNS) + '\n' + '99,torn')
        crop_runner.bulk_extract_crops([labelled(1, source='gsv')], str(store), str(out))
        rows = manifest_rows(out, crop_runner)
        assert rows[1] == ['99', 'torn']
        assert rows[2] == ['1', 'testpano0001', 'gsv', '', '', crop_runner.CROP_RULE_VERSION]

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
                          'dims_mismatch': 0, 'out_of_frame': 0, 'shifted_vertically': 0, 'errors': 0, 'recut': 0}
        assert reconciles(counts)
        assert os.path.exists(crop_path(out, 1, 1)) and os.path.exists(crop_path(out, 1, 2))
        # Both channels: the per-label reason in crop.log, the number where the operator reads.
        assert len([m for m in caplog.messages if 'provenance row was not written' in m]) == 2
        assert 'Input/output error' in caplog.text
        printed = capsys.readouterr().out
        assert '2 crops were written without a row in %s' % crop_runner.PROVENANCE_MANIFEST in printed

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


class TestTheMarkerSaysWhetherTheManifestIsComplete:
    """Crops cut before the manifest existed are never re-cut, so they never get a row. A consumer needs
    to know whether a manifest can be read as 'every crop here', and the rule version cannot tell it: a v2
    store cropped before this change and one cropped after both say v2. So crop_rule.json records whether
    the store already held crops when the manifest was started, and keeps that answer on later runs."""

    def test_a_fresh_store_records_a_complete_manifest(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['provenance_manifest'] == crop_runner.PROVENANCE_MANIFEST
        assert marker['provenance_manifest_started_under'] == crop_runner.CROP_RULE_VERSION
        assert marker['provenance_manifest_complete_from_start'] is True

    def test_a_store_with_crops_but_no_manifest_records_a_partial_one(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        write_crop_file(out, 1, 99)
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        assert read_marker(out, crop_runner)['provenance_manifest_complete_from_start'] is False

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
        assert read_marker(out, crop_runner)['provenance_manifest_complete_from_start'] is True

    def test_the_answer_is_kept_on_later_runs(self, crop_runner, tmp_path):
        """The second run finds crops on disk - the first run's - and must not re-derive 'partial' from
        them: the manifest has a row for every one."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, 'testpano0001')
        crop_runner.bulk_extract_crops([labelled(1)], str(store), str(out))
        crop_runner.bulk_extract_crops([labelled(1), labelled(2)], str(store), str(out))
        marker = read_marker(out, crop_runner)
        assert marker['provenance_manifest_complete_from_start'] is True
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
        assert marker['provenance_manifest_complete_from_start'] is None
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
        assert read_marker(out, crop_runner)['provenance_manifest_complete_from_start'] is False


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
