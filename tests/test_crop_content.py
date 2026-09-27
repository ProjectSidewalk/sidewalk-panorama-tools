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


# ---------------------------------------------------------------------------
# B. An undecodable pano is decoded once, not once per label
# ---------------------------------------------------------------------------

CUT = 'cutpano00001'
GOOD = 'goodpano0001'


@pytest.fixture
def decode_attempts(monkeypatch):
    """Count real decode attempts per pano file: calls to ImageFile.load while the image still has tiles
    to decode. Pillow keeps `tile` after a failed load, so every crop() of a truncated pano re-decodes
    the whole file - that repetition is what this counts. A Counter keyed by file basename."""
    import collections
    attempts = collections.Counter()
    real_load = ImageFile.ImageFile.load

    def load(self):
        if getattr(self, 'tile', None):
            attempts[os.path.basename(getattr(self, 'filename', '') or '')] += 1
        return real_load(self)

    monkeypatch.setattr(ImageFile.ImageFile, 'load', load)
    return attempts


def decode_lines(caplog):
    return [m for m in caplog.messages if 'cannot decode' in m]


class TestAnUndecodablePanoIsDecodedOnce:
    """Image.open is lazy, so a truncated pano gets past the cannot_open branch and used to fail inside
    make_single_crop for every label - re-decoding the whole file each time (Pillow keeps im.tile after
    the failure) and spending the crop_failed budget, which then hid real write failures."""

    def test_a_truncated_pano_is_decoded_once_not_per_label(self, crop_runner, tmp_path, decode_attempts):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        truncate_pano(store, CUT)
        put_pano(store, GOOD)
        labels = ([label_row(pano_id=CUT, label_id=i, pano_x=100 * i) for i in range(1, 6)]
                  + [label_row(pano_id=GOOD, label_id=6)])

        counts = run(crop_runner, labels, store, out)

        assert counts['errors'] == 5 and counts['success'] == 1 and reconciles(counts)
        assert decode_attempts[CUT + '.jpg'] == 1

    def test_one_line_per_pano_and_a_real_write_failure_still_shows(self, crop_runner, tmp_path,
                                                                    monkeypatch, caplog):
        """The decode failure is one cannot_open line for the pano, not a crop_failed line per label, so
        a real write failure elsewhere in the run still gets its crop_failed line under a tight cap."""
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 2)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        truncate_pano(store, CUT)
        put_pano(store, GOOD)
        real = crop_runner.make_single_crop

        def full_store_for_the_good_pano(pano, *args, **kwargs):
            if os.path.basename(getattr(pano, 'filename', '') or '') == GOOD + '.jpg':
                raise OSError('No space left on device')
            return real(pano, *args, **kwargs)

        monkeypatch.setattr(crop_runner, 'make_single_crop', full_store_for_the_good_pano)
        labels = ([label_row(pano_id=CUT, label_id=i, pano_x=100 * i) for i in range(1, 4)]
                  + [label_row(pano_id=GOOD, label_id=4)])
        with caplog.at_level(logging.WARNING):
            counts = run(crop_runner, labels, store, out)

        assert counts['errors'] == 4 and reconciles(counts)
        lines = decode_lines(caplog)
        assert len(lines) == 1 and '3 labels' in lines[0] and CUT in lines[0]
        assert not [m for m in caplog.messages if 'Failed to crop label' in m and CUT in m]
        assert 'No space left on device' in caplog.text

    def test_the_decode_line_is_the_cannot_open_kind(self, crop_runner, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(crop_runner, 'LOG_WARNINGS_PER_KIND', 1)
        store, out = tmp_path / 'store', tmp_path / 'crops'
        truncate_pano(store, CUT)
        truncate_pano(store, 'cutpano00002')
        labels = [label_row(pano_id=CUT, label_id=1), label_row(pano_id='cutpano00002', label_id=2)]
        with caplog.at_level(logging.WARNING):
            run(crop_runner, labels, store, out)
        suppressed = [m for m in caplog.messages if m.startswith('Suppressed')]
        assert suppressed and 'cannot_open: 1' in suppressed[-1]

    def test_a_resumed_pano_is_never_decoded(self, crop_runner, tmp_path, decode_attempts):
        """Preflights and skipped_existing read only the header, so a finished store never decodes a
        pano - including one that has since been truncated. Kills an eager load() at open."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, CUT)
        labels = [label_row(pano_id=CUT, label_id=1), label_row(pano_id=CUT, label_id=2, pano_x=600)]
        run(crop_runner, labels, store, out)
        truncate_pano(store, CUT)
        decode_attempts.clear()

        labels.append(dict(label_row(pano_id=CUT, label_id=3), pano_width=4096, pano_height=2048))
        counts = run(crop_runner, labels, store, out)

        assert counts['skipped_existing'] == 2 and counts['dims_mismatch'] == 1 and counts['errors'] == 0
        assert decode_attempts[CUT + '.jpg'] == 0

    def test_the_buckets_do_not_depend_on_label_order(self, crop_runner, tmp_path):
        """A label's bucket is decided by the label, not by whether an earlier label on the same pano hit
        the decode failure first. Kills routing every later label to errors after the first failure."""
        store = tmp_path / 'store'
        put_pano(store, CUT)
        write = label_row(pano_id=CUT, label_id=1)
        existing = label_row(pano_id=CUT, label_id=2, pano_x=600)
        dims = dict(label_row(pano_id=CUT, label_id=3, pano_x=1000), pano_width=4096, pano_height=2048)
        forward, backward = tmp_path / 'forward', tmp_path / 'backward'
        for out in (forward, backward):
            run(crop_runner, [existing], store, out)
        truncate_pano(store, CUT)

        a = run(crop_runner, [write, existing, dims], store, forward)
        b = run(crop_runner, [dims, existing, write], store, backward)

        assert a == b
        assert a['errors'] == 1 and a['skipped_existing'] == 1 and a['dims_mismatch'] == 1

    def test_under_force_an_undecodable_pano_keeps_the_old_crop(self, crop_runner, tmp_path, capsys):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, CUT)
        run(crop_runner, [label_row(pano_id=CUT, label_id=1)], store, out)
        before = read_bytes(find_crop(out, 1))
        truncate_pano(store, CUT)
        capsys.readouterr()

        counts = run(crop_runner, [label_row(pano_id=CUT, label_id=1),
                                   label_row(pano_id=CUT, label_id=2, pano_x=600)], store, out, force=True)

        assert counts['errors'] == 2 and counts['stale_kept'] == 1 and counts['success'] == 0
        assert read_bytes(find_crop(out, 1)) == before
        assert STALE_KEPT_SUMMARY % 1 in capsys.readouterr().out


# ---------------------------------------------------------------------------
# C. The JSON intake: one bad row is one bad label, never a dead run
# ---------------------------------------------------------------------------

def without(row, key):
    return {k: v for k, v in row.items() if k != key}


def write_json(path, payload):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    return str(path)


class TestTheJsonIntakeCountsABadRowAsOneBadLabel:
    """json_to_list indexed value["label_id"] outside any try, so one row without it (KeyError), a null
    element (TypeError) or a 200 carrying an error object (its keys iterated) crashed the whole run - while
    the module comment promised "a row missing any of them as one bad label". The CSV intake already
    behaved that way; this is the same contract for JSON."""

    def test_a_row_without_label_id_is_one_error(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        ok = label_row(pano_id=GOOD, label_id=1)
        rows = crop_runner.json_to_list([ok, without(label_row(pano_id=GOOD, label_id=2), 'label_id')])
        assert len(rows) == 2

        counts = run(crop_runner, rows, store, out)
        assert counts['total'] == 2 and counts['errors'] == 1 and counts['success'] == 1
        assert reconciles(counts)

    def test_through_main_the_run_finishes_and_exits_one(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        path = write_json(tmp_path / 'labels.json',
                          [label_row(pano_id=GOOD, label_id=1),
                           without(label_row(pano_id=GOOD, label_id=2), 'label_id')])
        assert crop_runner.main(['--city', CITY, '-f', path, '-s', str(store), '-o', str(out)]) == 1
        assert find_crop(out, 1) is not None

    def test_a_null_element_is_one_error(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        rows = crop_runner.json_to_list([label_row(pano_id=GOOD, label_id=1), None])
        counts = run(crop_runner, rows, store, out)
        assert counts['total'] == 2 and counts['errors'] == 1 and counts['success'] == 1

    def test_a_row_that_is_not_an_object_is_one_error(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        rows = crop_runner.json_to_list([label_row(pano_id=GOOD, label_id=1), 'label', 7, [1, 2]])
        counts = run(crop_runner, rows, store, out)
        assert counts['total'] == 4 and counts['errors'] == 3 and counts['success'] == 1
        assert reconciles(counts)

    @pytest.mark.parametrize('payload', [{'error': 'unauthorized'}, None, 3, 'x'],
                             ids=['object', 'null', 'number', 'string'])
    def test_a_top_level_that_is_not_an_array_is_refused_naming_the_file(self, crop_runner, tmp_path,
                                                                        payload):
        """A dict would otherwise be iterated as its keys - every key a 'row'. One error naming the file,
        the CSV intake's header-typo rule, rather than N bogus labels or a TypeError."""
        path = write_json(tmp_path / 'labels.json', payload)
        with pytest.raises(ValueError, match=re.escape(path)) as e:
            crop_runner.fetch_cvMetadata_from_file(path)
        assert 'JSON array' in str(e.value)

    def test_the_server_path_refuses_a_non_array_and_exits_one(self, crop_runner, monkeypatch, caplog):
        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {'error': 'unauthorized'}

        class FakeSession:
            trust_env = False

            def get(self, url, **kwargs):
                return FakeResponse()

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

        monkeypatch.setattr(crop_runner, 'request_session', lambda: FakeSession())
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as e:
                crop_runner.fetch_cvMetadata_from_server('sidewalk-test.invalid')
        assert e.value.code == 1
        assert 'https://sidewalk-test.invalid/adminapi/labels/cvMetadata' in caplog.text
        assert 'JSON array' in caplog.text


class TestTheJsonIntakeDedupesOnTheIdTheLoopFilesUnder:
    """The loop files a crop under int(label_id). Deduping on the raw value let 1 and "1" both through,
    and under --force both re-cut the same file. The key is now that same int()."""

    def test_spellings_of_one_id_are_one_label(self, crop_runner):
        rows = crop_runner.json_to_list([label_row(label_id=1, pano_x=100), label_row(label_id='1', pano_x=200),
                                         label_row(label_id=1.0, pano_x=300),
                                         label_row(label_id=' 1', pano_x=400)])
        assert len(rows) == 1 and rows[0]['pano_x'] == 100

    def test_under_force_one_id_is_cut_once(self, crop_runner, tmp_path):
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        rows = crop_runner.json_to_list([label_row(pano_id=GOOD, label_id=1),
                                         label_row(pano_id=GOOD, label_id='1', pano_x=600)])
        counts = run(crop_runner, rows, store, out, force=True)
        assert counts['total'] == 1 and counts['success'] == 1

    def test_unusable_ids_are_never_collapsed(self, crop_runner, tmp_path):
        """A row whose id cannot be an int is its own bad label: collapsing two of them would hide one."""
        store, out = tmp_path / 'store', tmp_path / 'crops'
        put_pano(store, GOOD)
        base = label_row(pano_id=GOOD)
        rows = crop_runner.json_to_list([dict(base, label_id=None), dict(base, label_id=None),
                                         dict(base, label_id='abc'), dict(base, label_id='abc'),
                                         without(base, 'label_id'), without(base, 'label_id')])
        assert len(rows) == 6
        counts = run(crop_runner, rows, store, out)
        assert counts['errors'] == 6 and reconciles(counts)
