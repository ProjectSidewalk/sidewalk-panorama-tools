"""assets/make_banner.py renders the README's hero figure by calling the real cropper on the committed sample
pano, and nothing ran it.

That matters because the figure's whole claim is that it shows what the code currently does - "re-run this
script and the numbers in the captions move with the code". Three things can quietly falsify that:

1. **The script stops running at all.** It calls `predict_crop_size`, `compute_crop_box` and `extract_crop`
   directly, so a signature change to any of them breaks the documented regeneration path (`CLAUDE.md` tells
   the next person to run it after a crop-geometry change) with no signal until someone tries.
2. **The geometry moves and the committed image doesn't.** Then `assets/banner.jpg` is a picture of the old
   behaviour, captioned as the new one - the exact failure the script was written to prevent.
3. **The sample data goes missing**, which turns the figure into a broken image on the front page.

Deliberately not asserted: the rendered bytes. `_font` resolves DejaVu on Linux and Segoe UI on Windows, so
the captions differ by platform while the geometry - the only thing the figure asserts - does not.
"""

import importlib.util
import os

import pytest
from PIL import Image

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNER = os.path.join(REPO_ROOT, 'assets', 'banner.jpg')
SAMPLE_PANO = os.path.join(REPO_ROOT, 'samples', 'sample_pano.jpg')


@pytest.fixture(scope='module')
def make_banner():
    """Imported by path: assets/ is not a package, and the script is meant to be run, not imported."""
    spec = importlib.util.spec_from_file_location(
        'make_banner', os.path.join(REPO_ROOT, 'assets', 'make_banner.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_sample_pano_the_figure_is_built_from_exists():
    assert os.path.exists(SAMPLE_PANO), 'the hero figure has no source image'


def test_the_committed_banner_exists_and_is_a_readable_image():
    assert os.path.exists(BANNER), 'README.md renders this at the top of the front page'
    with Image.open(BANNER) as im:
        assert im.format == 'JPEG'
        assert im.size[0] > im.size[1], 'the banner is a wide two-panel figure'


def test_the_script_still_runs_against_the_current_cropper(make_banner, tmp_path):
    """The real end-to-end path, into a tmp dir. A signature change in any of the three CropRunner functions
    it calls fails here rather than the next time someone regenerates the figure by hand."""
    out = tmp_path / 'banner.jpg'
    make_banner.build(out_path=str(out))

    assert out.exists()
    with Image.open(str(out)) as im:
        assert im.format == 'JPEG'
        expected = (make_banner.PAD * 3 + make_banner.PANO_W + make_banner.CROP_W,
                    make_banner.PAD * 2 + max(make_banner.PANO_H, make_banner.CROP_W) + make_banner.CAPTION_H)
        assert im.size == expected


def test_it_does_not_write_to_the_committed_path_when_given_one(make_banner, tmp_path):
    """out_path exists so a test can run the real thing without clobbering the figure in the repo. If build()
    ever goes back to hardcoding OUT_PATH, this catches it instead of the next `git status`."""
    before = os.path.getmtime(BANNER)
    make_banner.build(out_path=str(tmp_path / 'elsewhere.jpg'))
    assert os.path.getmtime(BANNER) == before


def test_the_committed_figure_is_not_stale(make_banner):
    """The geometry the committed image was drawn from, pinned to the label position the script uses.

    This is the staleness guard: if predict_crop_size or compute_crop_box changes, assets/banner.jpg is now a
    picture of the old behaviour and the fix is to re-run `python3 assets/make_banner.py` and commit the
    result - not to edit these numbers.
    """
    import CropRunner

    with Image.open(SAMPLE_PANO) as im:
        pano_w, pano_h = im.size

    size = CropRunner.predict_crop_size(make_banner.LABEL_Y, pano_h)
    box = CropRunner.compute_crop_box(make_banner.LABEL_X, make_banner.LABEL_Y, size, pano_w, pano_h)

    assert (pano_w, pano_h) == (13312, 6656)
    assert size == pytest.approx(398.2, abs=0.1)
    assert (box.left, box.top, box.size, box.shifted) == (1405, 3554, 398, False)
