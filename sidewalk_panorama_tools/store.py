"""The panorama store's layout, once: where a pano, its depth artifact and its display copy live, which files
are panoramas, and how a stored pano is read and checked against a label's frame (#222, step 5).

The store is the one this repo's downloaders write and every consumer reads (sidewalk-ai-api, RampNet, the
auto-labeler scripts), one directory per city::

    <store>/<city>/<pano_id[:2]>/<pano_id>.jpg          the panorama
    <store>/<city>/<pano_id[:2]>/<pano_id>.depth.npz    its GSV depth artifact (docs/depth.md)
    <store>/<city>/<pano_id[:2]>/<pano_id>.w8192.jpg    a display copy (#115; not a panorama, never cut from)

The shard is the id's first two characters, whatever the source: base64url GSV ids, all-digit Mapillary ids
and UUID Panoramax ids alike. This module is the one definition; ``downloaders/common.py`` re-exports
``walk_store_panos``, ``is_downscaled_sidecar``, ``jpeg_dimensions``, ``black_fraction`` and the
decompression-bomb ceiling from here, so the production tree and every consumer that pins this package read
the store the same way.

Usage::

    from sidewalk_panorama_tools.store import pano_path, read_pano, PanoDimsMismatch

    path = pano_path('/mnt/panostore', pano_id, city='seattle-wa')
    try:
        pano = read_pano(path, expected_width=label['pano_width'], expected_height=label['pano_height'])
    except PanoDimsMismatch as e:
        ...   # the stored file's frame is not the label's: skip, never crop (docs/cropper.md, the preflights)

Pure stdlib except :func:`read_pano`, which needs Pillow (a dependency of the package since 1.1.0).
"""

import errno
import math
import os
import re
import struct

__all__ = ['MAX_PANO_PIXELS', 'PanoDimsMismatch', 'black_fraction', 'depth_path', 'is_downscaled_sidecar',
           'jpeg_dimensions', 'pano_path', 'raise_decompression_bomb_ceiling', 'read_pano', 'shard',
           'sidecar_path', 'walk_store_panos']

# The largest panoramas our own downloader writes are 16384 x 8192 = 134 MP, over Pillow's 89 MP
# DecompressionBombWarning default. A named ceiling rather than None: the store is trusted, but "no limit at
# all" would also swallow a genuinely corrupt header claiming absurd dimensions - and Pillow only hard-fails
# above 2x its threshold, so the untouched default turns a routine modern pano into a warning per file.
MAX_PANO_PIXELS = 16384 * 8192


def shard(pano_id):
    """The shard directory of a pano: its id's first two characters.

    >>> shard('-0BIbaGbb6Ao2ctRBhyX9w'), shard('1234567890'), shard('0a1b2c3d-0000-4000-8000-00000000abcd')
    ('-0', '12', '0a')
    """
    pano_id = str(pano_id)
    if len(pano_id) < 2:
        raise ValueError('a pano id needs at least two characters to shard: %r' % (pano_id,))
    return pano_id[:2]


def _pano_dir(root, pano_id, city):
    base = root if city is None else os.path.join(root, city)
    return os.path.join(base, shard(pano_id))


def pano_path(root, pano_id, city=None):
    """``<root>[/<city>]/<id[:2]>/<id>.jpg``. Pass ``city`` with the store root, or omit it with a city's
    own directory (what ``DownloadRunner``'s storage argument is)."""
    return os.path.join(_pano_dir(root, pano_id, city), '%s.jpg' % pano_id)


def depth_path(root, pano_id, city=None):
    """``<root>[/<city>]/<id[:2]>/<id>.depth.npz``, the GSV depth artifact (docs/depth.md)."""
    return os.path.join(_pano_dir(root, pano_id, city), '%s.depth.npz' % pano_id)


def sidecar_path(pano_file, max_width):
    """``<pano stem>.w<max_width>.jpg``, the display copy beside a pano (#115).

    The width is part of the name, so a consumer must say which copy it means; this repo's cap is
    ``downloaders.common.DOWNSCALED_MAX_WIDTH`` (8192), whose ``downscaled_sidecar_path`` supplies it.
    """
    stem, _ = os.path.splitext(pano_file)
    return '%s.w%d.jpg' % (stem, max_width)


_SIDECAR_SUFFIX = re.compile(r'\.w(\d+)\.jpg$')


def is_downscaled_sidecar(filename):
    """Whether a store filename is a display copy rather than a panorama."""
    return _SIDECAR_SUFFIX.search(os.path.basename(filename)) is not None


def walk_store_panos(storage_path):
    """Yield the path of every stored panorama under `storage_path`, in a stable order.

    Only `<storage_path>/<2 chars>/<id>.jpg` with the shard matching id[:2] is a panorama - the layout
    gsv.download_single_pano writes. Anything else carrying a .jpg suffix is not: a crops tree, a stray file
    at the top level, a `.part` left by a crashed writer (not a .jpg at all), and above all a `.w8192.jpg`
    display copy, whose name shares the pano's first two characters and whose stem would otherwise be handed
    on as a pano id like `<real id>.w8192`.

    ONE definition, deliberately (#115). This predicate was written twice - refetch_panos.walk_store and the
    backfill's own sweep - and the sidecar exclusion is the kind of clause a third copy forgets: a walker
    that skips it does not fail, it invents pano ids, which is silent everywhere it matters. Same reasoning
    as jpeg_dimensions below, which moved into common the moment it had a second caller and into this
    package when consumers outside the repo needed it (#222). A new store walker calls this rather than
    re-deriving the filter, and shapes the result however it likes.
    """
    for shard_name in sorted(os.listdir(storage_path)):
        shard_path = os.path.join(storage_path, shard_name)
        if len(shard_name) != 2 or not os.path.isdir(shard_path):
            continue
        for filename in sorted(os.listdir(shard_path)):
            if filename.endswith('.jpg') and filename[:2] == shard_name and not is_downscaled_sidecar(filename):
                yield os.path.join(shard_path, filename)


# Start-of-frame markers whose payload carries the image dimensions. DHT/DAC/RST/SOS are excluded;
# 0xC4/0xC8/0xCC look like SOF numerically and are not.
SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                         0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})

# Markers that stand alone: no length field follows, so the scanner must not try to skip a segment.
STANDALONE_JPEG_MARKERS = frozenset({0x01, 0xD8, 0xD9}) | frozenset(range(0xD0, 0xD8))


def jpeg_dimensions(path):
    """(width, height) from a JPEG's SOF header, or None if the file is not a readable JPEG.

    Header-only: a 10 MB equirectangular pano costs a few reads instead of a full decode, which is what
    makes sweeping a whole store practical - Pillow would decode 16384 x 8192 x 3 = 384 MB to answer the
    same question. Returns None rather than raising, because every caller is a sweep that must survive a
    truncated file at pano 1300 of 1400 rather than dying on it.

    One hand-rolled marker scanner for the whole repo (reports/scripts/store_coverage.py, refetch_panos.py,
    downscale_panos.py and the black-band sweep all reach it through downloaders.common).
    """
    try:
        with open(path, 'rb') as f:
            if f.read(2) != b'\xff\xd8':
                return None
            while True:
                byte = f.read(1)
                while byte and byte != b'\xff':
                    byte = f.read(1)
                while byte == b'\xff':          # fill bytes: 0xFF may repeat before the marker
                    byte = f.read(1)
                if not byte:
                    return None
                marker = byte[0]
                if marker in STANDALONE_JPEG_MARKERS:
                    continue
                header = f.read(2)
                if len(header) < 2:
                    return None
                seglen = struct.unpack('>H', header)[0]
                if marker in SOF_MARKERS:
                    body = f.read(5)
                    if len(body) < 5:
                        return None
                    height, width = struct.unpack('>HH', body[1:5])
                    return width, height
                if seglen < 2:
                    return None
                f.seek(seglen - 2, os.SEEK_CUR)
    except OSError:
        return None


def raise_decompression_bomb_ceiling():
    """Let Pillow decode our own 134 MP panoramas without a DecompressionBombWarning on every one.

    Process-level policy, so a `main()` calls it and no library function does: this rewrites a PIL global
    that belongs to whoever imported us, and since #52.1 that can be another program. A library caller who
    wants the ceiling calls this itself; one who doesn't gets a warning, not a failure. (:func:`read_pano`
    does not call it, and installs no warnings filter either: a consumer reading 134 MP panos calls this once.)

    Every entry point that opens a stored panorama needs it - CropRunner, downscale_panos, refetch_panos'
    --measure, scan_black_bands, and DownloadRunner. That last one is currently a policy for a path nothing
    takes: #115 put an Image.open on the Mapillary download path, and downloaders.common.WRITE_DISPLAY_COPIES
    has been False since 2026-09-09, so nothing reaches it. It is kept because it is correct either way and
    is needed the moment the switch is flipped - see DownloadRunner.main. CropRunner and downloaders.common
    re-export the name, for the reports/scripts callers that reach for it there.
    """
    from PIL import Image
    if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < MAX_PANO_PIXELS:
        Image.MAX_IMAGE_PIXELS = MAX_PANO_PIXELS


class PanoDimsMismatch(ValueError):
    """A stored pano whose frame is not the one the label records (``pano_width``/``pano_height``).

    The stored pixel coordinates would land in the wrong frame - most often a file stitched at the 13312x6656
    the app hardcoded for every pano until 2022-03 (#217) - so the caller skips the label rather than cutting
    a mis-centred crop. ``expected`` and ``stored`` are ``(width, height)`` tuples, ``path`` the file.
    CropRunner's ``dims_mismatch`` preflight makes the same comparison (``meta_dims != pano.size``).
    """

    def __init__(self, path, expected, stored):
        self.path = path
        self.expected = expected
        self.stored = stored
        super().__init__('%s: the label says %dx%d but the stored image is %dx%d'
                         % (path, expected[0], expected[1], stored[0], stored[1]))


def read_pano(path, expected_width=None, expected_height=None):
    """Open a stored pano and decode it, after checking its header against the label's frame.

    With ``expected_width``/``expected_height`` (both or neither; a label's ``pano_width``/``pano_height``),
    a header whose ``(width, height)`` differs raises :class:`PanoDimsMismatch` **before** anything is
    decoded: the check is :func:`jpeg_dimensions`, stdlib only, so refusing a mis-framed 16384x8192 pano costs
    a few header reads, not 384 MB, and never touches Pillow. The comparison is CropRunner's dims preflight:
    the expected pair read as ``int(float(value))``, as ``CropRunner._metadata_dims`` reads it (so ``'64.0'``
    from a pandas-written CSV is 64), against the stored header. An expected value that is not a positive
    finite number raises ``ValueError`` - a bad argument, never a ``PanoDimsMismatch``.

    Returns a loaded ``PIL.Image.Image``; the caller closes it. A header claiming more than MAX_PANO_PIXELS
    raises ``Image.DecompressionBombError`` before decoding. Below it, the decode is Pillow's own: a pano over
    Pillow's default threshold (89 MP; a 16384x8192 one is 134 MP) warns unless the process called
    :func:`raise_decompression_bomb_ceiling` once at startup. This function deliberately installs no
    per-call warnings filter: ``warnings.catch_warnings`` rewrites the process-wide filter list and is not
    thread-safe, and a server reading panos on several threads is exactly the consumer this is for.

    A missing file raises ``FileNotFoundError``; a file whose JPEG header cannot be read raises ``OSError``.
    """
    if (expected_width is None) != (expected_height is None):
        raise ValueError('give both expected_width and expected_height, or neither')
    expected = None
    if expected_width is not None:
        expected = (_frame_dimension(expected_width), _frame_dimension(expected_height))
    stored = jpeg_dimensions(path)
    if stored is None:
        if not os.path.exists(path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
        raise OSError('%s: not a readable JPEG header' % (path,))
    if expected is not None and expected != stored:
        raise PanoDimsMismatch(path, expected, stored)

    from PIL import Image
    if stored[0] * stored[1] > MAX_PANO_PIXELS:
        raise Image.DecompressionBombError(
            '%s: %dx%d is over the %d-pixel ceiling for a stored pano' % (path, stored[0], stored[1],
                                                                          MAX_PANO_PIXELS))
    image = Image.open(path)
    try:
        image.load()
    except BaseException:
        image.close()
        raise
    return image


def black_fraction(image):
    """Exact fraction of black pixels in the frame, via the luma histogram.

    Counted over every pixel rather than a downsampled probe, and by histogram rather than a numpy array so
    it stays a C-level pass with no numpy RGB copy of the frame. It still makes one single-band L copy, which
    for a whole 16384x8192 frame is 134 MB; on a cut window it is a few MB. Both alternatives to an exact count
    are wrong in a way that matters here: an averaging downscale blends a black region into its neighbours
    and reports "slightly dark" for a frame that is three-quarters missing, while a NEAREST probe aliases on
    exactly the sort of regular black/imagery pattern a tiling bug produces.

    One primitive, three callers: gsv's stitch guard (as gsv._black_fraction), refetch_panos' too_black
    gate, and CropRunner's black_content check on a cut window (#164). It is defined here, in the package
    but apart from the tile fetcher, so a consumer judging a crop or a stitch shares the scraper's definition
    of black with no network dependency at all (#222 step 2); downloaders/common.py re-exports it, and its
    edge-band detector (#179) reads it there. Needs nothing but the image's own methods, so no Pillow import.
    """
    luma = image.convert('L')
    return luma.histogram()[0] / float(luma.width * luma.height)


def _frame_dimension(value):
    """A label's pano_width/pano_height as CropRunner._metadata_dims reads it: float, then int."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError('a frame dimension is a number, not %r' % (value,)) from None
    if not (math.isfinite(number) and number > 0):
        raise ValueError('a frame dimension is a positive finite number, not %r' % (value,))
    return int(number)
