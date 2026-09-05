"""Tests for reports/scripts/refetch_pilot_sharpness.py, the direct resolution measure for the fover pilot (#73).

The synthetic cases are the discrimination the committed artifact cannot provide: a band that IS a 2x upscale
must read as less sharp than its source, and a band that is merely re-encoded must read as unchanged. Without
those, a summarise() that returned 1.0 for everything would pin a committed artifact just as well.
"""

import json
import os
import sys

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, 'reports', 'scripts')
for _p in (REPO_ROOT, SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import refetch_pilot_sharpness as sh  # noqa: E402


def texture(height, width, seed=1973):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(height, width), dtype=np.uint8)


def upscaled_2x(array):
    im = Image.fromarray(array)
    small = im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.LANCZOS)
    return np.asarray(small.resize(im.size, Image.LANCZOS))


class TestLaplacianVariance:
    def test_a_2x_upscale_is_measurably_less_sharp_than_its_source(self):
        """What `fover` did to a polar band, in miniature: the highest octave is gone, so the Laplacian
        variance drops. The ratio is the statistic the whole script rests on."""
        native = texture(256, 512).astype(np.float32)
        soft = upscaled_2x(texture(256, 512)).astype(np.float32)

        assert sh.laplacian_variance(soft) < 0.5 * sh.laplacian_variance(native)

    def test_a_flat_strip_has_no_high_frequency_content(self):
        assert sh.laplacian_variance(np.full((64, 128), 77.0, dtype=np.float32)) == 0.0

    def test_it_is_the_variance_of_the_interior_four_neighbour_laplacian(self):
        """Pinned on a hand-computed 3x4: the interior is one row of two pixels. At (1,1) the Laplacian is
        4*10 - 0 = 40; at (1,2) it is 4*0 - 10 = -10; the variance of {40, -10} is 625. A padded
        implementation would have four more terms and a different number."""
        strip = np.array([[0, 0, 0, 0],
                          [0, 10, 0, 0],
                          [0, 0, 0, 0]], dtype=np.float32)
        assert sh.laplacian_variance(strip) == pytest.approx(625.0)


class TestMeasurePair:
    def pano_pair(self, tmp_path, degrade_bottom):
        """(old, new) 64x6656 panoramas as JPEG files; old's bottom band is a 2x upscale when asked."""
        from refetch_panos import band_pixel_rows
        height, width = 6656, 64
        truth = np.repeat(texture(height, width)[:, :, None], 3, axis=2)
        new = Image.fromarray(truth, 'RGB')
        old = new.copy()
        if degrade_bottom:
            (top, bottom), _ = band_pixel_rows(height)
            strip = new.crop((0, top, width, bottom))
            halved = strip.resize((width // 2, strip.height // 2), Image.LANCZOS).resize(strip.size, Image.LANCZOS)
            old.paste(halved, (0, top))
        old_path, new_path = str(tmp_path / 'old.jpg'), str(tmp_path / 'new.jpg')
        old.save(old_path, 'JPEG', quality=75)
        new.save(new_path, 'JPEG', quality=75)
        return old_path, new_path

    def test_a_degraded_bottom_band_reads_sharper_after_the_refetch_and_the_horizon_does_not(self, tmp_path):
        old_path, new_path = self.pano_pair(tmp_path, degrade_bottom=True)

        m = sh.measure_pair(old_path, new_path)

        assert m['bottom']['ratio_new_over_old'] > 1.5
        assert m['horizon']['ratio_new_over_old'] == pytest.approx(1.0, abs=0.05)
        assert m['bottom']['rows_px'] == [4608, 6656] and m['horizon']['rows_px'] == [2048, 4608]

    def test_an_unchanged_panorama_reads_as_unchanged_in_both_bands(self, tmp_path):
        """Discrimination: two encodes of the same imagery must not register as sharpening."""
        old_path, new_path = self.pano_pair(tmp_path, degrade_bottom=False)

        m = sh.measure_pair(old_path, new_path)

        assert m['bottom']['ratio_new_over_old'] == pytest.approx(1.0, abs=0.05)
        assert m['horizon']['ratio_new_over_old'] == pytest.approx(1.0, abs=0.05)

    def test_a_size_mismatch_or_unswept_geometry_is_not_measured(self, tmp_path):
        a, b = str(tmp_path / 'a.jpg'), str(tmp_path / 'b.jpg')
        Image.new('RGB', (64, 6656)).save(a); Image.new('RGB', (64, 8192)).save(b)
        assert sh.measure_pair(a, b) is None
        Image.new('RGB', (64, 1024)).save(a); Image.new('RGB', (64, 1024)).save(b)
        assert sh.measure_pair(a, b) is None


class TestSummarise:
    def rec(self, pid, bottom, horizon):
        return {'pano_id': pid, 'width': 16384, 'height': 8192,
                'bottom': {'rows_px': [5632, 8192], 'lap_var_old': 1.0, 'lap_var_new': bottom, 'ratio_new_over_old': bottom},
                'horizon': {'rows_px': [2560, 5632], 'lap_var_old': 1.0, 'lap_var_new': horizon, 'ratio_new_over_old': horizon}}

    def test_it_reports_medians_and_the_bottom_versus_horizon_count(self):
        records = [self.rec('a', 1.5, 1.0), self.rec('b', 1.2, 1.1), self.rec('c', 0.9, 1.0)]

        s = sh.summarise(records)

        assert s['measured'] == 3
        assert s['bottom']['ratio_median'] == pytest.approx(1.2)
        assert s['horizon']['ratio_median'] == pytest.approx(1.0)
        assert s['bottom']['n_sharper'] == 2 and s['horizon']['n_sharper'] == 1
        assert s['n_bottom_sharper_than_horizon'] == 2
        assert s['bottom_over_horizon_ratio_median'] == pytest.approx(1.2 / 1.1)

    def test_an_empty_pilot_is_undefined_not_unity(self):
        s = sh.summarise([])
        assert s['bottom']['ratio_median'] is None and s['horizon']['ratio_median'] is None
        assert s['n_bottom_sharper_than_horizon'] == 0

    def test_it_uses_the_shared_formatters(self):
        import studyfmt
        assert sh.fmt is studyfmt.fmt and sh.num is studyfmt.num


class TestMain:
    def test_it_measures_every_replaced_row_and_writes_a_strict_artifact(self, tmp_path, capsys):
        (tmp_path / 'old' / 'aa').mkdir(parents=True); (tmp_path / 'new' / 'aa').mkdir(parents=True)
        truth = np.repeat(texture(6656, 64)[:, :, None], 3, axis=2)
        Image.fromarray(truth, 'RGB').save(str(tmp_path / 'old' / 'aa' / 'aaPano.jpg'))
        Image.fromarray(truth, 'RGB').save(str(tmp_path / 'new' / 'aa' / 'aaPano.jpg'))
        ledger = tmp_path / 'refetch_log.csv'
        ledger.write_text('pano_id,status\naaPano,replaced\nbbGone,gone\n')
        out = tmp_path / 'sharp.json'

        assert sh.main(['--old-store', str(tmp_path / 'old'), '--new-store', str(tmp_path / 'new'),
                        '--ledger', str(ledger), '--write', str(out)]) == 0

        payload = json.loads(out.read_text())
        assert [r['pano_id'] for r in payload['records']] == ['aaPano']
        assert payload['summary']['measured'] == 1
        assert 'NaN' not in out.read_text()
        assert '1 measured' in capsys.readouterr().out
