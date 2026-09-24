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
                          'errors': 6}
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
