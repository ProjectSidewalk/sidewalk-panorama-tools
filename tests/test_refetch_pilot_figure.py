"""Tests for reports/scripts/refetch_pilot_figure.py, the pilot's before/after sheet (#73).

The figure is the one piece of the report a reader checks with their eyes rather than against an artifact,
so what has to hold is that it shows the two frames honestly: the same window of both, the magnified patch
taken from the *unannotated* crop, and a refusal rather than a silent comparison when the two stores hold
different frames. The committed 2026-09-05 PNG's window origin was never recorded, so nothing here can pin
its bytes; the layout it was measured off is pinned instead.
"""

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

import refetch_pilot_figure as fig  # noqa: E402


def store_with(tmp_path, name, pano_id='aaPano', size=(1024, 6656), seed=1):
    """A one-pano store whose frame is a swept zoom-5 geometry, narrowed so the test decodes megabytes
    rather than hundreds of them."""
    directory = tmp_path / name / pano_id[:2]
    directory.mkdir(parents=True)
    pixels = np.random.default_rng(seed).integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    Image.fromarray(pixels).save(str(directory / (pano_id + '.jpg')))
    return str(tmp_path / name)


class TestBandWindow:
    def test_y_is_measured_from_the_band_top_not_the_frame_top(self):
        """The whole point of expressing the window against the band: the same --y lands in the same place
        on both zoom-5 geometries, whose bands start at different absolute rows."""
        from refetch_panos import band_pixel_rows
        image = Image.new('RGB', (1024, 6656))
        (top, _bottom), _horizon = band_pixel_rows(6656)
        image.putpixel((3, top + 40), (10, 20, 30))

        assert fig.band_window(image, 0, 40, 8, 8).getpixel((3, 0)) == (10, 20, 30)

    def test_an_unswept_geometry_is_refused(self):
        with pytest.raises(SystemExit):
            fig.band_window(Image.new('RGB', (64, 1024)), 0, 0, 8, 8)

    def test_a_window_that_runs_past_the_band_is_refused(self):
        """Rather than silently clipping: a short crop pasted into a fixed cell would put band imagery and
        sheet background side by side and read as black pixels in the panorama."""
        with pytest.raises(SystemExit):
            fig.band_window(Image.new('RGB', (1024, 6656)), 0, 0, 8, 99999)


class TestCompose:
    def sheet(self, zoom=4):
        left = Image.new('RGB', (640, 320), (10, 200, 10))
        right = Image.new('RGB', (640, 320), (10, 200, 10))
        return fig.compose(left, right, (200, 40), (160, 80), zoom,
                           ('a', 'b', 'c', 'd')), left

    def test_the_layout_is_the_committed_figures(self):
        """1298x702 with 640-px columns, measured off reports/figures/2026-09-05-...png. Not decoration:
        the two rows only line up because a 640x320 window and a 160x80 patch at 4x are the same width."""
        sheet, _left = self.sheet()
        assert sheet.size == (1298, 702)

    def test_the_red_box_is_drawn_on_the_copy_and_never_reaches_the_magnified_patch(self):
        """The failure this guards: outline the crop in place and the 4x row shows a red border that is not
        in the imagery, i.e. the figure invents a feature in the exact place it asks the reader to look."""
        sheet, _left = self.sheet()
        pixels = np.asarray(sheet)
        red = (pixels[:, :, 0] > 180) & (pixels[:, :, 1] < 60) & (pixels[:, :, 2] < 60)
        rows = np.nonzero(red.any(axis=1))[0]

        assert red.any(), 'the patch outline must be drawn somewhere'
        assert rows.max() < fig.MARGIN + fig.LABEL_H + fig.MARGIN + 320, 'red leaked into the magnified row'

    def test_the_magnified_row_is_nearest_neighbour(self):
        """A smooth resize would show the reader a smoothing artefact of our own making, in a figure whose
        subject is whether one frame is smoother than the other."""
        left = Image.new('RGB', (640, 320), (0, 0, 0))
        left.putpixel((200, 40), (255, 255, 255))
        sheet = fig.compose(left, left.copy(), (200, 40), (160, 80), 4, ('a', 'b', 'c', 'd'))
        row2 = fig.MARGIN + fig.LABEL_H + fig.MARGIN + 320 + fig.MARGIN + fig.LABEL_H + fig.MARGIN
        block = np.asarray(sheet)[row2:row2 + 4, fig.MARGIN:fig.MARGIN + 4]

        assert (block == 255).all(), 'one source pixel must magnify to a flat 4x4 block'


class TestMain:
    def test_it_writes_a_sheet_from_two_stores(self, tmp_path, capsys):
        old = store_with(tmp_path, 'old', seed=1)
        new = store_with(tmp_path, 'new', seed=2)
        out = tmp_path / 'fig.png'

        assert fig.main(['--old-store', old, '--new-store', new, '--pano-id', 'aaPano',
                         '--x', '100', '--y', '40', '--probed', '2026-09-05', '--write', str(out)]) == 0

        assert Image.open(str(out)).size == (1298, 702)
        assert '640x320 window at (100, 40)' in capsys.readouterr().out

    def test_it_refuses_two_stores_that_disagree_about_the_frame(self, tmp_path):
        """`--old-store` is production. A mismatched pair means the pano was re-framed, not re-fetched, and
        cropping the same window out of both would put two different places side by side."""
        old = store_with(tmp_path, 'old', size=(1024, 6656))
        new = store_with(tmp_path, 'new', size=(1024, 8192))

        with pytest.raises(SystemExit) as excinfo:
            fig.main(['--old-store', old, '--new-store', new, '--pano-id', 'aaPano', '--x', '0', '--y', '0'])

        assert 'like-for-like' in str(excinfo.value)

    def test_the_window_origin_has_no_default(self):
        """The committed figure's origin was not recorded, so a default here would be a made-up provenance
        for the one artifact in this report that has none."""
        with pytest.raises(SystemExit) as excinfo:
            fig.build_parser().parse_args(['--old-store', 'a', '--new-store', 'b', '--pano-id', 'p'])

        assert excinfo.value.code == 2

    def test_it_writes_nothing_without_write(self, tmp_path):
        old = store_with(tmp_path, 'old')
        new = store_with(tmp_path, 'new', seed=2)

        assert fig.main(['--old-store', old, '--new-store', new, '--pano-id', 'aaPano',
                         '--x', '0', '--y', '0']) == 0

        assert sorted(p.name for p in tmp_path.iterdir()) == ['new', 'old']
