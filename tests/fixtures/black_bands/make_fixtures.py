"""Regenerate the black-band fixtures (#179). Deterministic: a fixed seed, and Pillow's default JPEG quality,
which is what gsv.download_single_pano saves with.

    python3 tests/fixtures/black_bands/make_fixtures.py

Every fixture is a 1024x512 equirectangular-shaped frame of low-frequency "imagery" (a random 64x32 field
upsampled bicubically, plus pixel noise), so the JPEG has real texture next to every band edge and the
ringing the detector must tolerate is the ringing a real stitch has. Bands are written as exact RGB 0 before
encoding, as the stitcher's black canvas is.

| file                  | what                                                                  |
|-----------------------|-----------------------------------------------------------------------|
| clean.jpg             | imagery edge to edge                                                  |
| d4.jpg                | #156's D4 shape: right 18.75% (192 cols) and bottom 18.75% (96 rows)  |
| thin_bottom_10.jpg    | a bottom band 10% deep (51 rows), not aligned to the 8-row JPEG block |
| thin_bottom_15.jpg    | a bottom band 15% deep (77 rows), not aligned either                  |
| dark_scene.jpg        | a night-dark frame, luma ~1-8, never encoded as exact 0               |
| nadir_cap_3.jpg       | a genuinely black nadir cap 3% deep (15 rows): must not be flagged    |
| near_black_nadir.jpg  | a nadir 15% deep at RGB (2,2,2): dark, not black, must not be flagged |
"""

import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT = 1024, 512


def imagery(seed, low=40, high=220):
    rng = np.random.default_rng(seed)
    field = rng.uniform(low, high, size=(32, 64, 3)).astype(np.uint8)
    smooth = np.asarray(Image.fromarray(field, 'RGB').resize((WIDTH, HEIGHT), Image.BICUBIC), dtype=np.int16)
    noisy = smooth + rng.integers(-6, 7, size=smooth.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def save(array, name):
    Image.fromarray(array, 'RGB').save(os.path.join(HERE, name), 'jpeg')


def main():
    save(imagery(1), 'clean.jpg')

    d4 = imagery(2)
    d4[HEIGHT - 96:, :] = 0
    d4[:, WIDTH - 192:] = 0
    save(d4, 'd4.jpg')

    for depth, name in ((51, 'thin_bottom_10.jpg'), (77, 'thin_bottom_15.jpg')):
        thin = imagery(3)
        thin[HEIGHT - depth:, :] = 0
        save(thin, name)

    rng = np.random.default_rng(4)
    dark = np.clip(4 + rng.integers(-3, 5, size=(HEIGHT, WIDTH, 3)), 1, 255).astype(np.uint8)
    save(dark, 'dark_scene.jpg')

    cap = imagery(5)
    cap[HEIGHT - 15:, :] = 0
    save(cap, 'nadir_cap_3.jpg')

    near = imagery(6)
    near[HEIGHT - 77:, :] = 2
    save(near, 'near_black_nadir.jpg')


if __name__ == '__main__':
    main()
