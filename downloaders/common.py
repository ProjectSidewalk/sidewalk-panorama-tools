import contextlib
import enum
import os
import re
import struct

from PIL import Image

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

    Lives here rather than in the script that first needed it (reports/scripts/store_coverage.py, which
    now imports it) because refetch_panos.py needs the same answer for every pano it considers, and a
    second copy of a hand-rolled marker scanner is exactly the kind of duplicate this repo has been bitten
    by before.
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


# The largest panoramas our own downloader writes are 16384 x 8192 = 134 MP, over Pillow's 89 MP
# DecompressionBombWarning default. A named ceiling rather than None: the store is trusted, but "no limit at
# all" would also swallow a genuinely corrupt header claiming absurd dimensions - and Pillow only hard-fails
# above 2x its threshold, so the untouched default turns a routine modern pano into a warning per file.
MAX_PANO_PIXELS = 16384 * 8192


def raise_decompression_bomb_ceiling():
    """Let Pillow decode our own 134 MP panoramas without a DecompressionBombWarning on every one.

    Process-level policy, so a `main()` calls it and no library function does: this rewrites a PIL global
    that belongs to whoever imported us, and since #52.1 that can be another program. A library caller who
    wants the ceiling calls this itself; one who doesn't gets a warning, not a failure.

    Every entry point that opens a stored panorama needs it - CropRunner, downscale_panos, refetch_panos'
    --measure, and (since #115 put an Image.open on the Mapillary download path) DownloadRunner. It lives
    here rather than in CropRunner, where it was written, because that list stopped being one script's
    concern; CropRunner re-exports the name for the two reports/scripts callers that reach for it there.
    """
    if Image.MAX_IMAGE_PIXELS is not None and Image.MAX_IMAGE_PIXELS < MAX_PANO_PIXELS:
        Image.MAX_IMAGE_PIXELS = MAX_PANO_PIXELS


class DownloadResult(enum.Enum):
    """What a downloader decided about one pano. See downloaders/__init__.py for the ledger contract.

    enum.Enum, not IntEnum, and deliberately (#52 item 2). This was a hand-rolled class whose members were
    indices into a tuple, which made `skipped` == 0 and therefore FALSY - one `if result:` anywhere would
    have silently misclassified an already-downloaded pano - and turned a typo'd member into a ValueError
    raised from inside __getattr__, so hasattr() raised instead of answering and the message never named
    the attribute. Nothing compares these to ints or does arithmetic on them (every call site is `==`
    against a symbol), so there is no reason to keep them int-like and every reason not to.
    """

    skipped = 'skipped'
    success = 'success'
    #: The pano was downloaded, but only zoom 3 was available while its reported dimensions need zoom 5, so
    #: the stitch was LANCZOS-upscaled to reach them. Real imagery, materially less of it. This is NOT
    #: simply `zoom == 3`: an old pano whose max zoom IS 3 is downloaded at its native resolution and is a
    #: plain success. See gsv.download_single_pano for the predicate, and log.csv column 8.
    fallback_success = 'fallback_success'
    failure = 'failure'


@contextlib.contextmanager
def atomic_output_path(final_path, mode=0o664):
    """Yield a '<final_path>.part' to write to, then chmod and rename it into place.

    Every artifact this repo writes is also its own resume marker: the image loop treats an existing .jpg as
    a completed download (DownloadResult.skipped) and the depth phase treats an existing .npz the same way.
    Writing straight to the final path therefore turns any mid-write crash - a full store, an sshfs mount
    dropping, a connection reset mid-stream - into a truncated file that every later run reports as a
    success and never revisits. That got sharper with #41: a transient failure is no longer ledgered, so the
    very next run reaches the exists() check and records the stub as downloaded=1.

    The .part is removed on any exception (including SystemExit from the SIGTERM translation), because
    nothing else ever cleans it up and the retry writes to the same name.
    """
    tmp_path = final_path + '.part'
    try:
        yield tmp_path
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, final_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# --- Display copies of wide panoramas (#115) -------------------------------------------------------------
#
# The Project Sidewalk web app shows a stored pano through Pannellum, which renders an equirectangular image
# as ONE WebGL texture, and 8192 px is a common MAX_TEXTURE_SIZE. A wider pano - newer GSV is 16384 x 8192,
# Richmond's Mapillary imagery 11000 - is therefore displayable only through a copy at this width. The copy
# is written here, beside the native file, because this is where the full raster already is: the web app
# tried cutting it nightly from the stored JPEG and OOM-killed its own JVMs (SidewalkWebpage#5239). ImageIO
# has no DCT-domain scaling and re-decodes the file once per strip, ~10 s and ~400 MB of heap per pano,
# inside a 1.5 GB web-app heap; Pillow's draft() decodes straight to half size in 0.8 s and ~150 MB.
#
# The width is in the file name, so a change of cap is a new sidecar rather than an ambiguous overwrite,
# and the web app looks for exactly the name its own configured cap produces. Every walker that lists the
# store's `*.jpg` has to ask is_downscaled_sidecar(), or a sidecar's stem is taken for a pano id.
DOWNSCALED_MAX_WIDTH = 8192

#: Display copies are looked at in a viewer, never cut from: crops always come from the native file.
#:
#: 85 rather than the 75 the native pano is saved at, and that choice costs real bytes: measured on
#: samples/sample_pano.jpg (13312 x 6656, 6.08 MB), the 8192-wide copy is 3.82 MB at q85 and 2.82 MB at q75 -
#: 63% of the native file against 46%. Some of that extra is spent re-encoding the SOURCE's own q75 artifacts
#: rather than detail, so it is a deliberate trade and not a free one; docs/ops.md carries the store-growth
#: number a backfill across ~50 cities has to budget for. Lower it only together with a re-run of the sweep,
#: since an existing sidecar is its own resume marker and is never re-encoded.
DOWNSCALED_JPEG_QUALITY = 85

_SIDECAR_SUFFIX = re.compile(r'\.w(\d+)\.jpg$')


def downscaled_sidecar_path(pano_path, max_width=None):
    """'<pano stem>.w<max_width>.jpg', beside the pano."""
    if max_width is None:
        max_width = DOWNSCALED_MAX_WIDTH
    stem, _ = os.path.splitext(pano_path)
    return '%s.w%d.jpg' % (stem, max_width)


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
    as jpeg_dimensions above, which moved here the moment it had a second caller. A new store walker calls
    this rather than re-deriving the filter, and shapes the result however it likes.
    """
    for shard in sorted(os.listdir(storage_path)):
        shard_path = os.path.join(storage_path, shard)
        if len(shard) != 2 or not os.path.isdir(shard_path):
            continue
        for filename in sorted(os.listdir(shard_path)):
            if filename.endswith('.jpg') and filename[:2] == shard and not is_downscaled_sidecar(filename):
                yield os.path.join(shard_path, filename)


def downscaled_size(width, height, max_width):
    """The size of a `max_width`-wide copy of a width x height image: aspect kept, never upscaled."""
    if width <= max_width:
        return width, height
    return max_width, max(1, int(round(height * max_width / float(width))))


def _write_reduced(image, size, path, quality):
    reduced = image if image.size == size else image.resize(size, Image.BOX)
    with atomic_output_path(path) as tmp_path:
        reduced.save(tmp_path, 'jpeg', quality=quality)
    return path


def write_downscaled_sidecar(image, pano_path, max_width=None, quality=None):
    """Write the display copy of an in-memory pano, or nothing when it is already narrow enough.

    Area-averaged (Image.BOX): each output pixel is the mean of the source pixels it covers - the right
    filter for a large reduction and, at the 2:1 GSV case, exactly the 2 x 2 block mean.

    @return The sidecar's path, or None when no copy was needed.
    """
    max_width = DOWNSCALED_MAX_WIDTH if max_width is None else max_width
    quality = DOWNSCALED_JPEG_QUALITY if quality is None else quality
    if image.width <= max_width:
        return None
    return _write_reduced(image, downscaled_size(image.width, image.height, max_width),
                          downscaled_sidecar_path(pano_path, max_width), quality)


def write_downscaled_sidecar_from_file(pano_path, max_width=None, quality=None):
    """The same copy, from a pano on disk: what the backfill and the Mapillary path use.

    `Image.draft` asks libjpeg to decode in the DCT domain at the largest power-of-two reduction that still
    meets the target, so a 16384-wide pano is decoded straight to 8192 and the full raster never exists -
    measured on a real 16384 x 8192 pano at 0.8 s and ~150 MB, against 2.3 s and ~900 MB for a full decode.
    BOX then covers whatever draft could not reach exactly (11000 -> 8192 decodes at full size).

    @return The sidecar's path, or None when the pano is not wider than the cap.
    @raise  ValueError when the file is not a readable JPEG; the caller decides what that means.
    """
    max_width = DOWNSCALED_MAX_WIDTH if max_width is None else max_width
    quality = DOWNSCALED_JPEG_QUALITY if quality is None else quality
    dims = jpeg_dimensions(pano_path)
    if dims is None:
        raise ValueError('%s is not a readable JPEG' % pano_path)
    if dims[0] <= max_width:
        return None
    size = downscaled_size(dims[0], dims[1], max_width)
    with Image.open(pano_path) as image:
        image.draft('RGB', size)
        image.load()
        return _write_reduced(image, size, downscaled_sidecar_path(pano_path, max_width), quality)
