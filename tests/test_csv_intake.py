"""The CSV/JSON intake contract for the three file-reading seams, after #72 moved them off pandas.

`fetch_pano_ids_csv` (-c), `fetch_label_ids_csv` (-f *.csv) and `flag_panos/json_to_csv.py` all used
`pandas`, whose dtype inference caused #46 and #55 and needed a `dtype={'pano_id': str}` pin at each
site whose only job was to stop it guessing. This file is the battery that made the swap to `csv`
safe, and it is deliberately one file rather than additions to three: it is a single contract, and
every case here was *measured* against `pd.read_csv` before the conversion rather than predicted.

Five of those measurements are why the change is worth making. Under pandas, with the dtype pin in
place:

* **An extra field on a row silently shifts every column.** `read_csv` consumes the first column as
  the DataFrame index, so a 10-field row under a 9-field header parsed to
  `pano_id='16384', camera_pitch='gsv', source=True` and the real pano id vanished into the index.
  Nothing raised. `csv.DictReader` puts the surplus under `None` and every named field stays right.
* **`has_labels` had no fixed type.** `bool` for `True`/`False`, `int64` for `1`/`0`, `float64` (NaN)
  for a blank, `str` for junk *and for `' True '` with padding*. `select_image_panos` is a plain
  truthiness test, so the two `str` cases are silently truthy — a padded ` True ` or a typo'd
  `maybe` defeats the `--all-panos` split without a word.
* **A single blank cell retyped a whole column.** One blank `width` made every other row's width a
  float, and the blank itself NaN — which `downloaders/gsv.py`'s `int(...) if ... is not None` guard
  cannot see, so it raises where the code reads as "let the tiler decide".
* **`drop_duplicates` deduped on the inferred type**, so label ids `7` and `07` collapsed into one.
* **`read_json` retyped ints via a sibling's absence** — a record missing `width` made every other
  record's `width` a float, and `16384` was written to CSV as `16384.0`.

Blank cells stay `''` in the cropper and become `None` in the downloader. That asymmetry is
deliberate and load-bearing: `gsv.download_single_pano` guards its dims with `is not None`, so a
`''` would sail past the guard into `int('')`, while the crop loop coerces every field inside a
try/except that counts a bad row, where `''` and `None` are equally well handled.
"""

import ast
import csv
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import CropRunner
import DownloadRunner
from flag_panos import json_to_csv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PANO_HEADER = 'pano_id,width,height,lat,lng,camera_heading,camera_pitch,source,has_labels\n'


def pano_row(pano_id='testPanoIdAAAAAAAAAAAA', width='16384', height='8192', source='gsv',
             has_labels='True'):
    return '%s,%s,%s,47.6,-122.3,180.0,0.0,%s,%s\n' % (pano_id, width, height, source, has_labels)


def write_pano_csv(tmp_path, text, name='panos.csv', encoding='utf-8'):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return str(path)


LABEL_HEADER = 'pano_id,pano_x,pano_y,label_type_id,label_id,pano_width,pano_height\n'


def label_csv_row(pano_id='abcdefgh0001', pano_x='100', pano_y='200', label_type_id='1',
                  label_id='1', pano_width='16384', pano_height='8192'):
    return '%s,%s,%s,%s,%s,%s,%s\n' % (pano_id, pano_x, pano_y, label_type_id, label_id,
                                       pano_width, pano_height)


def write_label_csv(tmp_path, text, name='labels.csv', encoding='utf-8'):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding))
    return str(path)


# ---------------------------------------------------------------------------
# DownloadRunner.fetch_pano_ids_csv  (-c)
# ---------------------------------------------------------------------------

class TestPanoCsvParsesLikeACsv:
    """Cases where the intake must behave exactly as it did under pandas."""

    def test_a_clean_file_round_trips(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row())

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert len(records) == 1
        assert records[0]['pano_id'] == 'testPanoIdAAAAAAAAAAAA'
        assert records[0]['source'] == 'gsv'

    def test_a_quoted_field_containing_a_comma_stays_whole(self, tmp_path):
        """The tempting wrong turn here is `line.split(',')`, which pandas never did."""
        path = write_pano_csv(tmp_path,
                              PANO_HEADER + '"pano,id",16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['pano,id']

    @pytest.mark.parametrize('embedded', ['\n', '\r\n', '\r'])
    def test_a_quoted_field_containing_a_newline_stays_whole(self, tmp_path, embedded):
        """The \\r cases are what newline='' on the reader buys, and they are pandas parity, not an
        invention: measured, read_csv also returns 'pano\\r\\nid' intact. Without newline='' the file
        object's universal-newline translation rewrites both to '\\n' before csv ever sees them, so the
        reader silently edits the data it was asked to read."""
        path = write_pano_csv(tmp_path, PANO_HEADER
                              + '"pano%sid",16384,8192,47.6,-122.3,180.0,0.0,gsv,True\n' % embedded)

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['pano%sid' % embedded]

    def test_crlf_line_endings_parse(self, tmp_path):
        text = (PANO_HEADER + pano_row()).replace('\n', '\r\n')
        path = write_pano_csv(tmp_path, text)

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['testPanoIdAAAAAAAAAAAA']

    def test_a_trailing_blank_line_is_not_a_row(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row() + '\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert len(records) == 1

    def test_a_header_only_file_yields_no_records(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER)

        assert DownloadRunner.fetch_pano_ids_csv(path) == []

    def test_a_utf8_bom_still_finds_pano_id(self, tmp_path):
        """Excel writes a BOM. pandas strips it; a plain `utf-8` open leaves it glued to the first
        fieldname, so the column guard fires on a file that is perfectly well-formed."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(), encoding='utf-8-sig')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['testPanoIdAAAAAAAAAAAA']

    def test_a_short_row_leaves_the_absent_fields_missing_not_shifted(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + 'testPanoIdAAAAAAAAAAAA,16384\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['pano_id'] == 'testPanoIdAAAAAAAAAAAA'
        assert records[0]['width'] == '16384'
        assert records[0]['source'] is None


class TestPanoCsvFailsLoudly:
    def test_a_missing_pano_id_column_names_the_file(self, tmp_path):
        path = write_pano_csv(tmp_path, 'panoid,source\ntestPanoIdRealAAAAAAAA,gsv\n')

        with pytest.raises(ValueError, match='no .pano_id. column'):
            DownloadRunner.fetch_pano_ids_csv(path)

    def test_an_empty_file_raises_the_named_error(self, tmp_path):
        """`reader.fieldnames` is None for an empty file, and `'pano_id' not in None` is a TypeError
        — so the guard has to test for None first or the clear message is replaced by a traceback."""
        path = write_pano_csv(tmp_path, '')

        with pytest.raises(ValueError, match='no .pano_id. column'):
            DownloadRunner.fetch_pano_ids_csv(path)


class TestExtraFieldsDoNotShiftColumns:
    """Measured: `pd.read_csv` consumes the first column as the index when a row is too long, so the
    real pano id disappears and `source` became the boolean `True`. Nothing raised."""

    def test_a_row_with_a_surplus_field_keeps_every_named_field(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row().rstrip('\n') + ',extra\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['pano_id'] == 'testPanoIdAAAAAAAAAAAA'
        assert records[0]['source'] == 'gsv'
        assert records[0]['width'] == '16384'

    def test_the_surplus_field_is_not_carried_as_a_none_key(self, tmp_path):
        """csv.DictReader files the overflow under the key None. Left in, it would reach
        _normalize_pano_records and any dict-walking consumer as a nameless entry."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row().rstrip('\n') + ',extra\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert None not in records[0]


class TestHasLabelsIsParsedNotGuessedAt:
    """`select_image_panos` is `p.get('has_labels', True)` — a plain truthiness test. Every string
    except '' is truthy, so this is where a naive csv port silently turns --all-panos into a no-op."""

    @pytest.mark.parametrize('raw', ['False', 'false', 'FALSE', '0', 'f', 'no', ' False '])
    def test_a_falsey_spelling_is_not_selected_for_images(self, tmp_path, raw):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels=raw))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['has_labels'] is False
        assert DownloadRunner.select_image_panos(records, False) == []

    @pytest.mark.parametrize('raw', ['True', 'true', 'TRUE', '1', 't', 'yes', ' True '])
    def test_a_truthy_spelling_is_selected_for_images(self, tmp_path, raw):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels=raw))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['has_labels'] is True
        assert len(DownloadRunner.select_image_panos(records, False)) == 1

    def test_all_panos_overrides_a_false(self, tmp_path):
        """The flag gates images only; an unlabelled pano is still wanted when --all-panos is on."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels='False'))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert len(DownloadRunner.select_image_panos(records, True)) == 1

    def test_a_blank_cell_counts_as_labelled(self, tmp_path):
        """Parity with the documented "a pano with no has_labels key counts as labelled", and with
        pandas, which read a blank as NaN — truthy."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels=''))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['has_labels'] is True

    def test_a_whitespace_only_cell_counts_as_labelled(self, tmp_path):
        """A cell of spaces is truthy, so it never becomes the blank cell's None and reaches the
        parser as a string — the same padding that made pandas type ' True ' as str, with the value
        taken out. It has to land on 'labelled' with the genuinely blank cell, not opposite it."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels='   '))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['has_labels'] is True

    def test_an_absent_column_counts_as_labelled(self, tmp_path):
        path = write_pano_csv(tmp_path, 'pano_id,source\ntestPanoIdAAAAAAAAAAAA,gsv\n')

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert len(DownloadRunner.select_image_panos(records, False)) == 1

    def test_an_unparseable_value_names_the_file_and_the_value(self, tmp_path):
        """Under pandas this was a str, hence truthy, hence downloaded — the operator never learned
        their CSV was wrong. -c exists for hand-made files, so this has to be loud."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(has_labels='maybe'))

        with pytest.raises(ValueError, match='maybe'):
            DownloadRunner.fetch_pano_ids_csv(path)


class TestBlankNumericCellsReadAsMissing:
    """downloaders/gsv.py: `int(pano_dims[0]) if pano_dims[0] is not None else None`. A '' is not
    None, so it walks straight past the guard into int('')."""

    def test_a_blank_width_is_none(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(width='', height=''))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['width'] is None
        assert records[0]['height'] is None

    def test_a_blank_width_survives_the_gsv_dims_guard(self, tmp_path):
        """The point of the None, stated as the expression gsv actually evaluates."""
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(width='', height=''))

        record = DownloadRunner.fetch_pano_ids_csv(path)[0]
        width = record.get('width')

        assert (int(width) if width is not None else None) is None

    def test_one_blank_does_not_retype_the_other_rows(self, tmp_path):
        """pandas made the whole column float64 for this input, so the populated row's width came
        back as 16384.0. Per-row parsing has no such coupling."""
        path = write_pano_csv(tmp_path,
                              PANO_HEADER
                              + pano_row(pano_id='aaaaaaaaaaaaaaaaaaaaaa', width='')
                              + pano_row(pano_id='bbbbbbbbbbbbbbbbbbbbbb', width='16384'))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['width'] is None
        assert records[1]['width'] == '16384'


class TestPanoIdsAreAlwaysStrings:
    """#46/#55 restated at the seam: the id type must not depend on what the ids happen to look
    like. These duplicate TestNumericPanoIds' intent in test_download_runner.py on purpose — that
    class pins the *pipeline*, this one pins the *reader*."""

    def test_all_numeric_ids_stay_strings(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(pano_id='123456789012345'))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert records[0]['pano_id'] == '123456789012345'
        assert isinstance(records[0]['pano_id'], str)

    def test_a_blank_id_beside_numeric_ids_does_not_mint_a_float_id(self, tmp_path):
        """Measured: even with dtype={'pano_id': str}, a blank cell came back as float nan, whose
        str() is 'nan' — an id that shards to na/nan.jpg."""
        path = write_pano_csv(tmp_path,
                              PANO_HEADER + pano_row(pano_id='123456789012345')
                              + pano_row(pano_id=''))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['123456789012345']

    def test_ids_differing_only_by_a_leading_zero_stay_distinct(self, tmp_path):
        path = write_pano_csv(tmp_path, PANO_HEADER + pano_row(pano_id='7') + pano_row(pano_id='07'))

        records = DownloadRunner.fetch_pano_ids_csv(path)

        assert [r['pano_id'] for r in records] == ['7', '07']


class TestTheNanGuardStillHasALiveCaller:
    """#72 proposed deleting the float-nan branch in _normalize_pano_records as a pandas artifact.
    It is not one: _normalize_pano_records is shared with the webserver path, and Python's json
    parses a bare NaN literal into float('nan') by default — so response.json() reaches it."""

    def test_json_really_does_produce_a_float_nan(self):
        parsed = json.loads('[{"pano_id": NaN}]')

        assert isinstance(parsed[0]['pano_id'], float)
        assert parsed[0]['pano_id'] != parsed[0]['pano_id']

    def test_a_nan_id_arriving_from_json_is_dropped_not_stringified(self):
        records = DownloadRunner._normalize_pano_records(json.loads(
            '[{"pano_id": NaN, "source": "gsv"}, {"pano_id": "abc", "source": "gsv"}]'))

        assert [r['pano_id'] for r in records] == ['abc']


# ---------------------------------------------------------------------------
# CropRunner.fetch_label_ids_csv  (-f *.csv)
# ---------------------------------------------------------------------------

class TestLabelCsvIntake:
    def test_a_clean_file_round_trips(self, tmp_path):
        path = write_label_csv(tmp_path, LABEL_HEADER + label_csv_row())

        labels = CropRunner.fetch_label_ids_csv(path)

        assert len(labels) == 1
        assert labels[0]['pano_id'] == 'abcdefgh0001'

    def test_a_missing_required_column_names_the_file_and_the_column(self, tmp_path):
        path = write_label_csv(tmp_path, 'panorama,pano_x,pano_y,label_type_id,label_id\na,1,2,1,1\n')

        with pytest.raises(ValueError, match='pano_id'):
            CropRunner.fetch_label_ids_csv(path)

    def test_an_empty_file_raises_the_named_error(self, tmp_path):
        path = write_label_csv(tmp_path, '')

        with pytest.raises(ValueError, match='pano_id'):
            CropRunner.fetch_label_ids_csv(path)

    def test_a_utf8_bom_still_finds_the_required_columns(self, tmp_path):
        path = write_label_csv(tmp_path, LABEL_HEADER + label_csv_row(), encoding='utf-8-sig')

        assert len(CropRunner.fetch_label_ids_csv(path)) == 1

    @pytest.mark.parametrize('embedded', ['\n', '\r\n', '\r'])
    def test_a_quoted_field_containing_a_newline_stays_whole(self, tmp_path, embedded):
        """newline='' on the reader, same as the downloader's. cvMetadata carries free text (copyright),
        so a field with a line break in it is not hypothetical here."""
        path = write_label_csv(tmp_path, 'pano_id,pano_x,pano_y,label_type_id,label_id,copyright\n'
                               + 'abcdefgh0001,100,200,1,1,"a%sb"\n' % embedded)

        labels = CropRunner.fetch_label_ids_csv(path)

        assert labels[0]['copyright'] == 'a%sb' % embedded

    def test_a_surplus_field_does_not_shift_the_named_columns(self, tmp_path):
        path = write_label_csv(tmp_path, LABEL_HEADER + label_csv_row().rstrip('\n') + ',extra\n')

        labels = CropRunner.fetch_label_ids_csv(path)

        assert labels[0]['pano_id'] == 'abcdefgh0001'
        assert labels[0]['pano_x'] == '100'
        assert None not in labels[0]

    def test_dedupe_keeps_the_first_row_per_label_id(self, tmp_path):
        path = write_label_csv(tmp_path,
                               LABEL_HEADER + label_csv_row(label_id='7', pano_x='100')
                               + label_csv_row(label_id='7', pano_x='210'))

        labels = CropRunner.fetch_label_ids_csv(path)

        assert len(labels) == 1
        assert labels[0]['pano_x'] == '100'

    def test_label_ids_differing_only_by_a_leading_zero_stay_distinct(self, tmp_path):
        """drop_duplicates deduped on the inferred int64, so 7 and 07 collapsed into one label."""
        path = write_label_csv(tmp_path,
                               LABEL_HEADER + label_csv_row(label_id='7')
                               + label_csv_row(label_id='07'))

        assert len(CropRunner.fetch_label_ids_csv(path)) == 2

    def test_the_real_sample_file_parses(self, tmp_path):
        """samples/metadata-seattle.csv is the documented -f example: the old export's width/height
        column names, and a trailing blank `copyright` on every row."""
        path = os.path.join(REPO_ROOT, 'samples', 'metadata-seattle.csv')

        labels = CropRunner.fetch_label_ids_csv(path)

        assert len(labels) > 0
        assert all(isinstance(row['pano_id'], str) for row in labels)
        assert CropRunner._metadata_dims(labels[0]) == (16384, 8192)


class TestMetadataDimsTreatsBlankAsAbsent:
    """`_metadata_dims` returning None means "this row does not claim dimensions", which skips the
    dims preflight and still crops. Raising instead makes the row a counted error — and main() exits
    1 on errors, so a blank dims column would turn a clean run into a failing one."""

    def test_blank_pano_dims_are_absent_not_an_error(self):
        assert CropRunner._metadata_dims(
            {'pano_id': 'a', 'pano_width': '', 'pano_height': ''}) is None

    def test_a_blank_pano_width_falls_back_to_the_old_width_column(self):
        """The half a naive fix misses: '' is not None, so the fallback branch never fires and the
        populated width/height pair is never consulted."""
        assert CropRunner._metadata_dims(
            {'pano_id': 'a', 'pano_width': '', 'pano_height': '',
             'width': '16384', 'height': '8192'}) == (16384, 8192)

    def test_blanks_in_both_column_pairs_are_absent_not_an_error(self):
        """The other half a naive fix misses: the check after the fallback needs the blank rule too.
        A CSV carrying both column pairs, all four blank, falls back to '' — and a bare `is None`
        there hands that '' to float(), so the row is a counted error rather than one that simply
        does not claim dimensions."""
        assert CropRunner._metadata_dims(
            {'pano_id': 'a', 'pano_width': '', 'pano_height': '',
             'width': '', 'height': ''}) is None

    def test_absent_keys_are_still_absent(self):
        assert CropRunner._metadata_dims({'pano_id': 'a'}) is None

    def test_a_json_null_is_still_absent(self):
        """cvMetadata sends null for third-party photospheres; json.load gives None."""
        assert CropRunner._metadata_dims(
            {'pano_id': 'a', 'pano_width': None, 'pano_height': None}) is None

    def test_populated_dims_are_ints(self):
        assert CropRunner._metadata_dims(
            {'pano_id': 'a', 'pano_width': '16384', 'pano_height': '8192'}) == (16384, 8192)

    def test_a_non_numeric_value_still_raises_for_the_callers_error_handling(self):
        with pytest.raises(ValueError):
            CropRunner._metadata_dims({'pano_id': 'a', 'pano_width': 'wide', 'pano_height': '8192'})


# ---------------------------------------------------------------------------
# flag_panos/json_to_csv.py
# ---------------------------------------------------------------------------

def read_csv_rows(path):
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.reader(f))


class TestJsonToCsvConversion:
    def test_records_round_trip(self, tmp_path):
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc', 'image_width': 16384}, {'pano_id': 'def', 'image_width': 13312}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        assert read_csv_rows(tmp_path / 'amsterdam_pano_image_data.csv') == [
            ['pano_id', 'image_width'], ['abc', '16384'], ['def', '13312']]

    def test_a_record_missing_a_key_does_not_retype_its_siblings(self, tmp_path):
        """Measured under pandas: the absent `image_width` made the column float64, so the record
        that *did* carry 16384 was written as 16384.0."""
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc', 'image_width': 16384}, {'pano_id': 'def', 'copyright': 'x'}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        assert read_csv_rows(tmp_path / 'amsterdam_pano_image_data.csv') == [
            ['pano_id', 'image_width', 'copyright'], ['abc', '16384', ''], ['def', '', 'x']]

    def test_a_numeric_pano_id_stays_as_written(self, tmp_path):
        """No dtype pin ever existed here, so a Mapillary-shaped id column inferred int64."""
        (tmp_path / 'amsterdam_unretrievable_panos.json').write_text(json.dumps(
            [{'pano_id': '123456789012345'}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        assert read_csv_rows(tmp_path / 'amsterdam_unretrievable_panos.csv') == [
            ['pano_id'], ['123456789012345']]

    def test_a_null_becomes_an_empty_cell(self, tmp_path):
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc', 'copyright': None}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        assert read_csv_rows(tmp_path / 'amsterdam_pano_image_data.csv') == [
            ['pano_id', 'copyright'], ['abc', '']]

    def test_a_value_containing_a_comma_is_quoted(self, tmp_path):
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc', 'copyright': 'a,b'}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        assert read_csv_rows(tmp_path / 'amsterdam_pano_image_data.csv') == [
            ['pano_id', 'copyright'], ['abc', 'a,b']]

    def test_no_index_column_is_written(self, tmp_path):
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc', 'image_width': 16384}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        header = read_csv_rows(tmp_path / 'amsterdam_pano_image_data.csv')[0]
        assert header == ['pano_id', 'image_width']

    def test_lines_are_not_doubled(self, tmp_path):
        """A csv.writer emits \\r\\n itself; without newline='' the file object translates the \\n
        again and every row gains a blank line after it."""
        (tmp_path / 'amsterdam_pano_image_data.json').write_text(json.dumps(
            [{'pano_id': 'abc'}, {'pano_id': 'def'}]))

        json_to_csv.convert(str(tmp_path), 'amsterdam')

        raw = (tmp_path / 'amsterdam_pano_image_data.csv').read_bytes()
        assert b'\r\r\n' not in raw and b'\n\n' not in raw


# ---------------------------------------------------------------------------
# The pin that keeps it gone
# ---------------------------------------------------------------------------

# Everything the scraper or the cropper runs. log_analyzer/ and reports/scripts/ are deliberately
# absent: both still use pandas, and both are dev/ops tools rather than production code.
PRODUCTION_MODULES = ['DownloadRunner.py', 'CropRunner.py', 'config.py', 'scrape_queue.py',
                      'migrate_depth_artifacts.py', 'refetch_panos.py', 'flag_panos/json_to_csv.py',
                      'downloaders/__init__.py', 'downloaders/common.py',
                      'downloaders/gsv.py', 'downloaders/mapillary.py']


def imported_names(source):
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split('.')[0])
    return names


class TestPandasStaysOutOfProduction:
    """pandas is still installed in CI — it moved to requirements-dev.txt, not out of the repo — so
    an accidental re-import would not fail the build. Read, rather than imported, for the same
    reason test_log_analyzer.py reads DownloadRunner.py with ast."""

    @pytest.mark.parametrize('module', PRODUCTION_MODULES)
    def test_no_production_module_imports_pandas(self, module):
        with open(os.path.join(REPO_ROOT, module), encoding='utf-8') as f:
            assert 'pandas' not in imported_names(f.read())

    def test_the_module_list_matches_what_is_on_disk(self):
        """A new production module added without a line here would be silently unguarded."""
        on_disk = {name for name in os.listdir(REPO_ROOT) if name.endswith('.py')}
        on_disk |= {'downloaders/' + name for name in os.listdir(os.path.join(REPO_ROOT, 'downloaders'))
                    if name.endswith('.py')}
        on_disk |= {'flag_panos/' + name for name in os.listdir(os.path.join(REPO_ROOT, 'flag_panos'))
                    if name.endswith('.py')}

        assert on_disk == set(PRODUCTION_MODULES)

    def test_requirements_txt_does_not_pin_pandas(self):
        with open(os.path.join(REPO_ROOT, 'requirements.txt'), encoding='utf-8') as f:
            pins = [line.split('#')[0].strip() for line in f]
        assert not any(pin.startswith('pandas') for pin in pins)
