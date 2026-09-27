"""What CropRunner does with the CONTENT of what it reads (#164): a cut window that is mostly black, a pano
whose header opens but whose body cannot be decoded, and a JSON intake whose rows are not what they claim.

Kept apart from tests/test_crop_runner.py because these are verdicts about the imagery and the payload
rather than about the crop geometry. The helpers are imported from there so both files describe one
store, but this file never hard-codes where a crop lands: crops are found by rglob and the provenance
manifest likewise, so the tests hold whatever layout the crop store uses (#159 changes it).

No network anywhere. Panos are synthetic JPEGs in a tmp store.
"""

import csv
import json
import logging
import os
import re
import sys

import pytest
from PIL import Image, ImageFile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# crop_runner and the autouse logging isolation are fixtures: importing them into this module's namespace
# is what makes pytest apply them here.
from test_crop_runner import (  # noqa: F401
    STALE_KEPT_SUMMARY, _isolate_logging_state, crop_runner, label_row, put_pano, reconciles,
    truncate_pano, write_labels_csv)

from downloaders import common  # noqa: E402

# The city every call here names. A real roster city, so the file holds whether or not --city is ever
# validated against the roster; nothing below depends on which one.
CITY = 'seattle-wa'

GREY = (128, 128, 128)
BANDED = 'bandpano0001'

# The 30%-band fixture's labels, with the exactly-black share of each one's cut window as measured when
# the check was designed (plan, "Measurements behind the constants"): interior, inside the band (the window
# shifts up to stay in the pano, so 90% of it is band), and above the band's edge with 24% of it black.
CLEAN_Y, IN_BAND_Y, PARTLY_Y = 512, 900, 650
LABEL_X = 1000


def put_banded_pano(store, pano_id=BANDED, size=(2048, 1024), bottom=0.30, right=0.0, fill=(0, 0, 0),
                    base=GREY):
    """A flat grey pano with black bands along the bottom and/or right edge, saved as a q95 JPEG in the
    store's <pano_id[:2]>/ shard. The shape of the #156 D4 stitch (bottom=right=0.19) and of a pre-#68
    fallback: imagery with a region where Google served nothing.

    fill is the band colour, so a near-black band can be built the same way.
    """
    width, height = size
    image = Image.new('RGB', size, base)
    if bottom:
        image.paste(fill, (0, height - int(round(height * bottom)), width, height))
    if right:
        image.paste(fill, (width - int(round(width * right)), 0, width, height))
    shard = os.path.join(str(store), pano_id[:2])
    os.makedirs(shard, exist_ok=True)
    path = os.path.join(shard, pano_id + '.jpg')
    image.save(path, quality=95)
    return path


def band_label(label_id, pano_y, pano_x=LABEL_X, pano_id=BANDED):
    return label_row(pano_id=pano_id, pano_x=pano_x, pano_y=pano_y, label_id=label_id)


def find_crop(out, label_id):
    """The crop for label_id wherever the store's layout puts it, or None. At most one may exist."""
    found = list(os.path.realpath(p) for p in _rglob(out, '%d.jpg' % label_id))
    assert len(found) <= 1, found
    return found[0] if found else None


def _rglob(root, name):
    import pathlib
    return [str(p) for p in pathlib.Path(str(root)).rglob(name)]


def read_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def provenance_ids(out):
    """The label_ids with a row in the provenance manifest, wherever it lives under out."""
    manifests = _rglob(out, 'crop_provenance.csv')
    assert len(manifests) == 1, manifests
    with open(manifests[0], newline='', encoding='utf-8') as f:
        return {row['label_id'] for row in csv.DictReader(f)}


def run(crop_runner, labels, store, out, **kwargs):
    return crop_runner.bulk_extract_crops(labels, str(store), str(out), city=CITY, **kwargs)


def black_content_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and 'of the cut window is black' in r.getMessage()]


# ---------------------------------------------------------------------------
# A. The content check: a mostly black window is withheld, not cut (black_content)
# ---------------------------------------------------------------------------

class TestAMostlyBlackWindowIsWithheld:
    """The stitcher rejects a pano more than half black and saves anything under that; CropRunner's "no
    synthetic black" guarantee (#47) is about crop geometry. So a label inside a black band - where
    ground features sit - was cut as a black crop and counted success. The check judges the window about
    to be written."""

    def test_a_label_in_the_black_band_is_withheld_not_cut(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        labels = [band_label(1, CLEAN_Y), band_label(2, IN_BAND_Y), band_label(3, PARTLY_Y)]

        counts = run(crop_runner, labels, store, out)

        assert counts['success'] == 2
        assert counts['black_content'] == 1
        assert counts['errors'] == 0
        assert counts['total'] == 3
        assert reconciles(counts)
        assert find_crop(out, 2) is None
        assert find_crop(out, 1) is not None and find_crop(out, 3) is not None
        assert not _rglob(out, '*.part')
        assert provenance_ids(out) == {'1', '3'}

    def test_the_other_crops_are_byte_identical_to_a_run_without_the_check(self, crop_runner, tmp_path,
                                                                            monkeypatch):
        """The check reads the window and changes nothing about it: a crop that passes is the file the
        run would have written anyway. Kills a check that re-encodes or mutates the window."""
        store, out, out2 = tmp_path / 'store', tmp_path / 'crops', tmp_path / 'crops2'
        put_banded_pano(store)
        labels = [band_label(1, CLEAN_Y), band_label(2, IN_BAND_Y), band_label(3, PARTLY_Y)]
        run(crop_runner, labels, store, out)

        monkeypatch.setattr(crop_runner, 'CROP_MAX_BLACK_FRACTION', 1.0)
        run(crop_runner, labels, store, out2)

        for label_id in (1, 3):
            assert read_bytes(find_crop(out, label_id)) == read_bytes(find_crop(out2, label_id))
        assert find_crop(out2, 2) is not None

    def test_main_exits_zero_and_says_so_on_both_channels(self, crop_runner, tmp_path, capsys, caplog):
        """Not an error - the run refused to trust the imagery, it did not get anything wrong - so the
        exit code stays 0. That is exactly why it must be said: a run that is 100% black_content would
        otherwise complete silently, the #101 shape."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        csv_file = tmp_path / 'labels.csv'
        write_labels_csv(csv_file, [band_label(1, CLEAN_Y), band_label(2, IN_BAND_Y)])

        with caplog.at_level(logging.WARNING):
            assert crop_runner.main(['--city', CITY, '-f', str(csv_file), '-s', str(store),
                                     '-o', str(out)]) == 0

        printed = capsys.readouterr().out
        assert '1 withheld for a mostly black window' in printed
        sentence = '1 labels were withheld because more than 50% of their cut window was black'
        assert sentence in printed
        assert sentence in caplog.text

    def test_the_verdict_is_strictly_more_than_the_fraction(self, crop_runner, tmp_path):
        """Exactly at the limit is written; one row more is not. In memory, so the fractions are exact
        rather than JPEG-rounded. Kills `>=` and any other comparator."""
        size = (2048, 1024)
        box = crop_runner.compute_crop_box(
            LABEL_X, CLEAN_Y, crop_runner.crop_window_width(CLEAN_Y, size[0], size[1]), size[0], size[1])
        bottom = box.top + box.height
        at_limit = bottom - int(box.height * crop_runner.CROP_MAX_BLACK_FRACTION)

        def pano_black_from(row):
            image = Image.new('RGB', size, GREY)
            image.paste((0, 0, 0), (0, row, size[0], size[1]))
            return image

        written = tmp_path / 'written.jpg'
        assert crop_runner.make_single_crop(pano_black_from(at_limit), LABEL_X, CLEAN_Y, str(written)) == box
        assert written.exists()

        withheld = tmp_path / 'withheld.jpg'
        with pytest.raises(crop_runner.CropWindowMostlyBlackError) as e:
            crop_runner.make_single_crop(pano_black_from(at_limit - 1), LABEL_X, CLEAN_Y, str(withheld))
        assert e.value.fraction > crop_runner.CROP_MAX_BLACK_FRACTION
        assert e.value.box == box
        assert not withheld.exists()
        assert not os.path.exists(str(withheld) + '.part')

    def test_the_shipped_fraction_is_pinned(self, crop_runner):
        """A silent retune moves which crops a training set holds, so it is a deliberate edit here."""
        assert crop_runner.CROP_MAX_BLACK_FRACTION == 0.5

    def test_near_black_is_not_black(self, crop_runner, tmp_path):
        """The measure is exact-zero luma, not "dark". A band of (1, 1, 1) is imagery as far as the
        check knows - a night scene, a black car - so it is written. Kills a tolerance."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store, fill=(1, 1, 1))
        counts = run(crop_runner, [band_label(2, IN_BAND_Y)], store, out)
        assert counts['success'] == 1 and counts['black_content'] == 0
        assert find_crop(out, 2) is not None

    @pytest.mark.parametrize('stored_width', [None, 40], ids=['narrow-window', 'downscaled'])
    def test_it_measures_the_raw_cut_window(self, crop_runner, tmp_path, monkeypatch, stored_width):
        """What is judged is the window exactly as extract_crop returns it: before the storage downscale
        and before --mark-label's dot. Kills measuring after either, or measuring the pano."""
        if stored_width is not None:
            monkeypatch.setattr(crop_runner, 'CROP_MAX_STORED_WIDTH', stored_width)
        size = (2048, 1024)
        pano = Image.new('RGB', size)
        pano.putdata([((x * 7) % 256, (y * 3) % 256, (x + y) % 256)
                      for y in range(size[1]) for x in range(size[0])])
        seen = []
        real = crop_runner.black_fraction

        def spy(image):
            seen.append(image.copy())
            return real(image)

        monkeypatch.setattr(crop_runner, 'black_fraction', spy)
        box = crop_runner.make_single_crop(pano, LABEL_X, CLEAN_Y, str(tmp_path / 'c.jpg'), draw_mark=True)

        assert len(seen) == 1
        assert seen[0].size == (box.width, box.height)
        fresh = crop_runner.extract_crop(pano, box.left, box.top, box.width, box.height)
        assert seen[0].tobytes() == fresh.tobytes()

    def test_a_crop_on_disk_is_never_rejudged(self, crop_runner, tmp_path, monkeypatch):
        """Crops already on disk are the resume marker, and the check runs only on a crop about to be
        written. A black crop cut before the check existed stays, as skipped_existing, untouched. Kills
        a check placed before the exists check."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        with monkeypatch.context() as m:
            m.setattr(crop_runner, 'CROP_MAX_BLACK_FRACTION', 1.0)
            run(crop_runner, [band_label(2, IN_BAND_Y)], store, out)
        before = read_bytes(find_crop(out, 2))

        calls = []
        real = crop_runner.black_fraction
        monkeypatch.setattr(crop_runner, 'black_fraction', lambda image: calls.append(1) or real(image))
        counts = run(crop_runner, [band_label(2, IN_BAND_Y)], store, out)

        assert counts['skipped_existing'] == 1 and counts['black_content'] == 0
        assert read_bytes(find_crop(out, 2)) == before
        assert calls == []

    def test_under_force_a_black_window_keeps_the_old_crop(self, crop_runner, tmp_path, monkeypatch,
                                                          capsys):
        """--force re-cuts, but a window that would now be withheld is not written, so the crop already
        there is neither replaced nor removed. It is counted as black_content AND stale_kept, and the
        stale_kept sentence says the content check is among the causes."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        with monkeypatch.context() as m:
            m.setattr(crop_runner, 'CROP_MAX_BLACK_FRACTION', 1.0)
            run(crop_runner, [band_label(2, IN_BAND_Y)], store, out)
        before = read_bytes(find_crop(out, 2))
        capsys.readouterr()

        counts = run(crop_runner, [band_label(2, IN_BAND_Y)], store, out, force=True)

        assert counts['black_content'] == 1 and counts['stale_kept'] == 1
        assert counts['success'] == 0 and counts['recut'] == 0
        assert read_bytes(find_crop(out, 2)) == before
        printed = capsys.readouterr().out
        assert STALE_KEPT_SUMMARY % 1 in printed
        assert 'withheld by the content check (black_content)' in printed

    def test_mark_label_does_not_change_the_verdict(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        labels = [band_label(1, CLEAN_Y), band_label(2, IN_BAND_Y), band_label(3, PARTLY_Y)]
        counts = run(crop_runner, labels, store, out, mark_label=True)
        assert counts['success'] == 2 and counts['black_content'] == 1 and counts['errors'] == 0
        assert find_crop(out, 2) is None

    def test_black_content_lines_have_their_own_budget(self, crop_runner, tmp_path, monkeypatch, caplog):
        """One crop.log line per withheld label, under its own #139 kind, so a store full of black
        bands cannot flood the log or hide another kind's first lines."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 2)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        labels = [band_label(10 + i, IN_BAND_Y, pano_x=100 + 300 * i) for i in range(5)]
        with caplog.at_level(logging.WARNING):
            counts = run(crop_runner, labels, store, out)
        assert counts['black_content'] == 5
        assert len(black_content_lines(caplog)) == 2
        suppressed = [m for m in caplog.messages if m.startswith('Suppressed')]
        assert suppressed and 'black_content: 3' in suppressed[-1]

    @pytest.mark.parametrize('cap', [0, 1, 2, 1000])
    def test_the_budget_never_touches_the_black_content_count(self, crop_runner, tmp_path, monkeypatch, cap):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', cap)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        labels = ([band_label(10 + i, IN_BAND_Y, pano_x=100 + 300 * i) for i in range(5)]
                  + [band_label(1, CLEAN_Y)])
        counts = run(crop_runner, labels, store, out)
        assert counts['black_content'] == 5 and counts['success'] == 1 and reconciles(counts)

    def test_every_disjoint_outcome_once_including_black_content(self, crop_runner, tmp_path):
        """One label per bucket on the banded pano. black_content is a disjoint outcome, not an
        annotation: the withheld label is in no other bucket."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store)
        run(crop_runner, [band_label(9, CLEAN_Y, pano_x=300)], store, out)

        labels = [band_label(1, CLEAN_Y),                                        # success
                  band_label(9, CLEAN_Y, pano_x=300),                            # skipped_existing
                  band_label(2, CLEAN_Y, pano_id='gonepano0001'),                # missing_pano
                  dict(band_label(3, CLEAN_Y), pano_width=4096, pano_height=2048),  # dims_mismatch
                  band_label(4, 5000),                                           # out_of_frame
                  band_label(5, IN_BAND_Y),                                      # black_content
                  band_label(6, 'not-a-number')]                                 # errors
        counts = run(crop_runner, labels, store, out)

        assert set(counts) == ({'total'} | set(crop_runner.DISJOINT_OUTCOMES)
                               | set(crop_runner.COUNT_ANNOTATIONS))
        assert 'black_content' in crop_runner.DISJOINT_OUTCOMES
        assert 'black_content' not in crop_runner.COUNT_ANNOTATIONS
        for bucket in crop_runner.DISJOINT_OUTCOMES:
            assert counts[bucket] == 1, bucket
        assert counts['total'] == 7

    def test_the_d4_shape_is_seen_only_inside_its_bands(self, crop_runner, tmp_path):
        """KNOWN LIMIT, pinned so nobody reads more into the check than it does. The #156 D4 stitch - a
        frame reported larger than Google serves - is ~34% black along its right and bottom 19%, and its
        real imagery sits at the wrong scale and position. A label inside a band is withheld. A label
        outside them gets clean imagery of the wrong place: 0% black, dims agree with the metadata, and
        no crop-level check can see it. That success is real imagery at the wrong scale; a pano-level
        check on the downloader/refetch side is where it can be caught."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store, bottom=0.19, right=0.19)
        labels = [band_label(1, 512, pano_x=500), band_label(2, 512, pano_x=1900),
                  band_label(3, 950, pano_x=500)]
        counts = run(crop_runner, labels, store, out)
        assert counts['success'] == 1 and find_crop(out, 1) is not None
        assert counts['black_content'] == 2
        assert find_crop(out, 2) is None and find_crop(out, 3) is None

    def test_a_seam_crop_on_a_clean_pano_is_not_black(self, crop_runner, tmp_path):
        """A window that wraps the seam is pasted from two segments into a new image; the paste must
        leave no black behind for the check to find."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_banded_pano(store, bottom=0.0)
        counts = run(crop_runner, [band_label(1, CLEAN_Y, pano_x=5)], store, out)
        assert counts['success'] == 1 and counts['black_content'] == 0

    def test_one_black_fraction_primitive(self, crop_runner):
        """The stitcher, the refetch pass and the cropper judge "black" with the same function, not three
        copies that can drift."""
        from downloaders import gsv
        assert crop_runner.black_fraction is common.black_fraction is gsv._black_fraction
