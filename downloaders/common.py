import collections
import contextlib
import enum
import errno
import importlib
import logging
import os
import tempfile
from datetime import datetime, timezone

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# The longest `detail` a run condition carries (#161). The detail is display-only - it rides into the night's
# cron message once per condition KIND - and an exception's str can be a page of HTML.
CONDITION_DETAIL_MAX = 300


def note_condition(sink, code, detail):
    """Record a run condition (#161) into the run summary `sink`: a shape the runner itself calls a failure
    of the night without changing its own exit code, which scrape_queue reads and books against the night.

    Every condition goes through here so the rules live in one place: no sink (a phase driven directly by a
    test, or by hand) is a no-op; a code is recorded once per run and the FIRST detail wins, because the
    queue's line names the first occurrence; the detail is one line (whitespace runs, newlines included,
    collapse to a space, because the queue's summary line would split on a newline and drop both halves out of
    `grep ERROR scrape_queue.log`) cut to CONDITION_DETAIL_MAX.

    Example::

        >>> summary = {'image_stop': None, 'depth_stop': None}
        >>> note_condition(summary, 'depth-refused', 'HTTP 429')
        >>> summary['conditions']
        [{'code': 'depth-refused', 'detail': 'HTTP 429'}]
    """
    if sink is None:
        return
    conditions = sink.setdefault('conditions', [])
    if any(c.get('code') == code for c in conditions):
        return
    conditions.append({'code': code, 'detail': ' '.join(str(detail).split())[:CONDITION_DETAIL_MAX]})


class HostStateLocked(Exception):
    """Another live process on this host already holds the lock."""


def _lock_module():
    """The platform's advisory-lock module: fcntl on POSIX, msvcrt on Windows.

    Imported by name at call time rather than behind a module-scope try/except ImportError, so this module has
    no line that is dead on the platform it is running on - and so a test can substitute a module-shaped
    object and exercise the arm this platform never takes.

    Both APIs release the lock when the holding process dies, which is the property the callers rest on: a
    lock that outlived a crash would silently disable a feature for ever, which is worse than the overlap it
    prevents. An O_EXCL lock file - the obvious first implementation - has exactly that defect.
    """
    return importlib.import_module('fcntl' if os.name == 'posix' else 'msvcrt')


# The errno a NON-BLOCKING lock attempt returns when someone else holds the lock, and nothing else: flock
# gives EWOULDBLOCK (EAGAIN on Linux), msvcrt.locking gives EACCES (measured) or EDEADLOCK. Any other OSError
# - ENOLCK or EOPNOTSUPP from a filesystem that cannot lock, EBADF - is not a second process and must not be
# reported as one (#125 review, finding 4). Built with getattr because not every platform defines every name.
_LOCK_HELD_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in ('EAGAIN', 'EWOULDBLOCK', 'EACCES', 'EDEADLOCK', 'EDEADLK'))
    if code is not None)


def _try_lock(fd):
    """Take an exclusive advisory lock on fd without blocking.

    Raise HostStateLocked if someone else holds it; let any other OSError through unwrapped, because "another
    process holds this" is the one diagnosis a caller acts on (download_depth_maps prints it to stdout, where
    cron mails it), and a lock that cannot be taken for some other reason - a filesystem without lock support,
    a bad descriptor - would otherwise send the operator looking for a process that does not exist.
    """
    lock_api = _lock_module()
    try:
        if hasattr(lock_api, 'flock'):
            lock_api.flock(fd, lock_api.LOCK_EX | lock_api.LOCK_NB)
        else:
            # One byte at offset 0. Windows allows locking a region past EOF, so this works on the empty file
            # a first run creates.
            os.lseek(fd, 0, os.SEEK_SET)
            lock_api.locking(fd, lock_api.LK_NBLCK, 1)
    except OSError as e:
        if e.errno in _LOCK_HELD_ERRNOS:
            raise HostStateLocked(str(e)) from e
        raise


@contextlib.contextmanager
def exclusive_host_lock(path):
    """Hold an advisory lock on a HOST-level state file for the duration of the block, or raise.

    For state that belongs to the machine rather than to one city's store, where two processes reading,
    deciding and writing independently would each believe they owned the whole budget. `scrape_queue.py`
    carries its own copy of this idiom for the queue lock; the two are deliberately not shared, because
    collapsing them would make the fleet driver import the image package to borrow four lines of `fcntl`.

    The lock file is never unlinked, deliberately: unlinking races - another process can already have opened
    the same path and be holding a lock on what is now an orphaned inode, so both would believe they hold it.
    It is left behind holding the pid, which costs nothing and tells whoever finds it who to look for. Lock a
    file that is never renamed: an os.replace() swaps the inode out from under any lock held on it.

    @raise HostStateLocked if another process holds it; OSError if the path cannot be opened at all.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    try:
        _try_lock(fd)
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())
    except BaseException:
        os.close(fd)
        raise
    try:
        yield path
    finally:
        # Closing releases the lock under both APIs. Nothing else is needed, and nothing here may raise on the
        # way out and mask the caller's own exception.
        os.close(fd)

# The JPEG header reader, the decompression-bomb ceiling and black_fraction are defined in the installable
# package (sidewalk_panorama_tools/store.py, #222 steps 5 and 2) and re-exported here, so every existing caller,
# test and rules-file sentence that names `common.<name>` stays true and there is still one definition of each.
from sidewalk_panorama_tools.store import (  # noqa: E402,F401  (re-exports)
    MAX_PANO_PIXELS,                    # defined in sidewalk_panorama_tools/store.py
    SOF_MARKERS,                        # defined in sidewalk_panorama_tools/store.py
    STANDALONE_JPEG_MARKERS,            # defined in sidewalk_panorama_tools/store.py
    black_fraction,                     # defined in sidewalk_panorama_tools/store.py
    jpeg_dimensions,                    # defined in sidewalk_panorama_tools/store.py
    raise_decompression_bomb_ceiling,   # defined in sidewalk_panorama_tools/store.py
)


#: How far before the end of a JPEG its end-of-image marker may sit: see jpeg_is_complete (#155 review item 23).
TRAILING_BYTES_WINDOW = 64 * 1024


def _first_scan_offset(f):
    """Offset of the first byte of the main image's entropy-coded data - just past its first SOS header - by
    walking the marker segments from SOI, or None if the header ends (or breaks) before a scan begins.

    Walking by segment LENGTH is the point: an EXIF thumbnail is a whole JPEG, EOI included, inside an APP1
    segment, and the walk steps over it without reading it.
    """
    f.seek(0)
    if f.read(2) != b'\xff\xd8':
        return None
    while True:
        byte = f.read(1)
        if byte != b'\xff':
            return None                     # between segments there must be a marker
        while byte == b'\xff':              # fill bytes
            byte = f.read(1)
        if not byte:
            return None
        marker = byte[0]
        if marker == 0xD9:
            return None                     # EOI before any scan
        if marker in STANDALONE_JPEG_MARKERS:
            continue
        header = f.read(2)
        if len(header) < 2:
            return None
        seglen = int.from_bytes(header, 'big')
        if seglen < 2:
            return None
        start = f.tell()
        f.seek(seglen - 2, os.SEEK_CUR)
        if marker == 0xDA:                  # SOS
            return start + seglen - 2


def jpeg_is_complete(f, size):
    """Whether the JPEG in the seekable binary file `f` (`size` bytes long) ends with an end-of-image
    marker (FF D9) that closes its main image. Header validity is the caller's check.

    Shared by store mode's is_complete_jpeg (a file on disk) and the photosphere download (a response
    body in a BytesIO), so the two cannot drift apart.

    The rule (#155 review item 23). Either the file ENDS with FF D9 - every JPEG this repo writes, so every
    GSV pano - or an FF D9 lies in its last TRAILING_BYTES_WINDOW (64 KB) bytes AND after the start of the
    main image's first scan (_first_scan_offset). The second arm exists because Mapillary and Panoramax
    originals and GSV user photospheres are stored verbatim, and a camera JPEG can carry bytes after its EOI
    (a vendor trailer, padding); under the strict rule such a file was refused, deleted and re-pulled every
    night, for ever.
    Photospheres measured 2026-11-09: a 111-byte Samsung `Image_UTC_Data` trailer, or 130 / 3,257 bytes of
    zero padding (reports/2026-11-09-photosphere-404s-and-trailers.md).

    Why the second arm is still safe against a transfer cut short:
      * Inside entropy-coded data an FF byte is always followed by 00 (stuffing) or an RSTn marker, never by
        D9. So a file cut mid-scan contains no FF D9 after its scan start - there is nothing to find.
      * An FF D9 BEFORE the scan start can exist: an EXIF thumbnail is a complete JPEG inside APP1, and on a
        small file it sits well inside the last 64 KB. The scan-start bound is exactly what refuses it; the
        segment walk steps over the thumbnail by length rather than reading it.
      * The window bounds how much trailing data is believed; beyond it the file is refused as before.
    Residual, accepted: a progressive JPEG's between-scan segments (DHT, COM) could in principle hold the
    bytes FF D9 in their payload; a truncation within 64 KB after one would pass. No standard Huffman
    symbol is 0xFF, and the scraper's own GSV files take the first arm, so this is theoretical. An MPO
    (primary image, then a second JPEG) passes on the primary's EOI - which is complete - whatever state
    the second image is in.
    """
    if size < 4:
        return False
    f.seek(size - 2)
    if f.read(2) == b'\xff\xd9':
        return True
    scan_start = _first_scan_offset(f)
    if scan_start is None:
        return False
    window_start = max(scan_start, size - TRAILING_BYTES_WINDOW)
    f.seek(window_start)
    return b'\xff\xd9' in f.read(size - window_start)



# --- Edge black bands (#179) ----------------------------------------------------------------------------------
#
# black_fraction answers "how much of this is black"; a band detector answers "is there a black band along the
# frame's edge, and how deep". Both known shapes of stored black are bands at the edge: #156's D4 shape (a
# reported frame larger than Google serves leaves the right and bottom 18.75% black, ~34% of the pixels, under
# the stitcher's 0.5 whole-frame limit) and the fallback stitches from before #68. A band depth separates them
# from a dark scene far better than a fraction, and it catches a thin band that a 0.5 fraction never will -
# and that the cropper's window check cannot, below H/6 (docs/cropper.md).
#
# A legitimate black nadir cap is ALSO a bottom band - a spherical cap maps to whole rows of an equirectangular
# frame - so shape cannot tell the two apart, only depth. That is what EDGE_BAND_MAX_FRACTION is for.

#: The frame is judged at 1/8 scale. From a file that is Pillow's JPEG draft mode - libjpeg decodes each 8x8
#: block straight to its DC value, in the luma channel only - so the full raster never exists. A block that is
#: exactly black stays exactly 0 (a uniform block is DC-only), and a block that straddles a band edge rings
#: and is not black, so a measured band is short of the true one by less than one block, never long. In
#: memory (the stitch) it is Image.reduce's box mean, which gives the same answer on the stitcher's exact-zero
#: canvas. 8 is the most draft can reduce, and the cheapest.
EDGE_BAND_SCALE = 8

#: A line (one reduced row or column) belongs to a band when at least this fraction of it is exactly black,
#: by common.black_fraction's definition (luma 0). Not 1.0, so a band survives a stray block - a seam artifact
#: or a straddling corner - and not lower, because a line of a real scene that is 98% exact zero is not a
#: scene: JPEG noise keeps night imagery and near-black nadirs off 0 (tests/fixtures/black_bands).
EDGE_BAND_LINE_MIN_BLACK = 0.98

#: A band deeper than this fraction of the frame's height (bottom) or width (right) is not imagery. Strict
#: `>`. NOT YET CALIBRATED on the store (#179 item 4 asks for that, per source, before relying on it): 0.05 is
#: set from the grid, not from measured nadirs. One zoom-5 tile row is 1/16 = 6.25% of the frame, so every
#: band a tile grid can leave at zoom 5 is over it with margin for the 1/8-scale measurement, and the H/6
#: (16.7%) band the cropper can catch, the D4 shape (18.75%) and the 10% and 15% bands it cannot are all far
#: over it. A genuinely black nadir cap up to 9 degrees of elevation is under it. scan_black_bands.py records
#: every pano's depths, so running it over a store is the calibration measurement.
EDGE_BAND_MAX_FRACTION = 0.05

#: Band depths as fractions of the frame: `bottom` of its height, `right` of its width. 0.0 is no band.
EdgeBands = collections.namedtuple('EdgeBands', ['bottom', 'right'])

#: The sweep's second measurement (#213 review, S4): a band of NEAR black, luma at or under this, beside the
#: exact-black one. Exact black is encoder-dependent: a region written as RGB 0 decodes to luma 1 at 26 of
#: Pillow's 96 JPEG qualities, and with Google's own DC quantiser (10/12) tiles too, so a store of provider
#: bytes - every Mapillary and Panoramax pano, and any GSV pano re-encoded on its way in - can hold a band the
#: exact definition cannot see. 2 is the measured ceiling of that drift; a night scene is luma 1-8 with noise
#: and a near-black nadir written as RGB 2 sits exactly on it (tests/fixtures/black_bands), which is why this
#: is a RECORDED measurement for the calibration (#179 item 4) and not a verdict: the downloader's own
#: in-memory check stays on exact zero, because the stitcher's canvas is exact zero.
EDGE_BAND_NEAR_BLACK_LUMA = 2

#: Both measurements of one decode: `exact` is the band of luma 0 (EdgeBands), `near` the band of luma at or
#: under EDGE_BAND_NEAR_BLACK_LUMA (EdgeBands too). The near band is never shallower than the exact one.
EdgeBandReport = collections.namedtuple('EdgeBandReport', ['exact', 'near'])


def _line_dark_fraction(line, max_luma):
    """The fraction of `line`'s pixels at or under `max_luma` - black_fraction's shape with a ceiling."""
    return sum(line.histogram()[:max_luma + 1]) / float(line.width * line.height)


def _edge_band_lines(luma, lines, line_box, dark_fraction=None):
    """How many of `lines` - ordered from the edge inward - are band lines, counted until the first that is not.

    Contiguous from the edge, deliberately: a black stripe with imagery between it and the edge is not an edge
    band (a tunnel, a shadow), and an edge band is the shape both failures leave. `dark_fraction` is what
    judges one line; the default is black_fraction itself - the one definition of black in this repo - and the
    sweep's near-black pass supplies _line_dark_fraction with its ceiling.
    """
    count = 0
    for line in lines:
        strip = luma.crop(line_box(line))
        fraction = black_fraction(strip) if dark_fraction is None else dark_fraction(strip)
        if fraction < EDGE_BAND_LINE_MIN_BLACK:
            break
        count += 1
    return count


def _edge_bands_of_luma(luma, max_luma=0):
    """EdgeBands of a reduced luma image. `max_luma` 0 is exact black through black_fraction; anything higher
    is the near-black measurement, through _line_dark_fraction."""
    width, height = luma.size
    dark = None if max_luma == 0 else (lambda strip: _line_dark_fraction(strip, max_luma))
    bottom = _edge_band_lines(luma, range(height - 1, -1, -1), lambda y: (0, y, width, y + 1), dark)
    right = _edge_band_lines(luma, range(width - 1, -1, -1), lambda x: (x, 0, x + 1, height), dark)
    return EdgeBands(bottom / float(height), right / float(width))


def edge_black_bands(image):
    """The depth of the exactly-black band along the bottom rows and the right-hand columns of an in-memory frame.

    What gsv.download_single_pano asks of a stitch before saving it. The frame is box-reduced by
    EDGE_BAND_SCALE first (a 16384x8192 stitch becomes 2048x1024 before any luma copy is made), then each line
    from the edge inward is judged with black_fraction until one is not a band line.

    Example::

        >>> frame = Image.new('RGB', (1024, 512))                       # all black
        >>> frame.paste(Image.new('RGB', (832, 416), (90, 90, 90)))     # imagery, top left
        >>> edge_black_bands(frame)
        EdgeBands(bottom=0.1875, right=0.1875)

    @return EdgeBands(bottom, right), each a fraction of the frame.
    """
    return _edge_bands_of_luma(image.reduce(EDGE_BAND_SCALE).convert('L'))


def edge_band_report_from_file(path, dims=None):
    """Both band measurements of a stored JPEG from ONE decode through draft mode: luma only, at
    1/EDGE_BAND_SCALE. `exact` is the band of luma 0, `near` the band of luma <= EDGE_BAND_NEAR_BLACK_LUMA.

    What scan_black_bands.py asks of every stored panorama. The header is read first, with jpeg_dimensions
    (or taken from `dims`, when the caller has already read it), so a file that is not a readable JPEG is
    refused before Pillow is asked to decode it.

    @param dims (width, height) from a jpeg_dimensions call the caller already made; None reads the header.
    @raise ValueError when the file is not a readable JPEG; the caller decides what that means.
    """
    if dims is None:
        dims = jpeg_dimensions(path)
    if dims is None:
        raise ValueError('%s is not a readable JPEG' % path)
    try:
        with Image.open(path) as image:
            image.draft('L', (max(1, dims[0] // EDGE_BAND_SCALE), max(1, dims[1] // EDGE_BAND_SCALE)))
            luma = image.convert('L')
    except (OSError, SyntaxError) as e:
        # A readable header over a truncated or corrupt scan. Pillow raises OSError for most of these and
        # SyntaxError for a few malformed markers.
        raise ValueError('%s is not a readable JPEG: %s' % (path, e)) from e
    return EdgeBandReport(_edge_bands_of_luma(luma), _edge_bands_of_luma(luma, EDGE_BAND_NEAR_BLACK_LUMA))


def edge_black_bands_from_file(path):
    """The exact-black answer alone for a stored JPEG - edge_band_report_from_file(path).exact.

    @raise ValueError when the file is not a readable JPEG; the caller decides what that means.
    """
    return edge_band_report_from_file(path).exact


def deep_edge_bands(bands, limit=None):
    """The sides of `bands` deeper than `limit` (default EDGE_BAND_MAX_FRACTION, read at call time), in the order
    ('bottom', 'right'); an empty tuple when neither is.

    Example::

        >>> deep_edge_bands(EdgeBands(0.1875, 0.0))
        ('bottom',)
    """
    limit = EDGE_BAND_MAX_FRACTION if limit is None else limit
    return tuple(side for side, depth in zip(EdgeBands._fields, bands) if depth > limit)


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


# --- Display copies of wide panoramas (#115), SWITCHED OFF 2026-09-09 --------------------------------------
#
# A display copy is a stored pano re-encoded at the width a viewer can texture, written beside the native
# file as <pano_id>.w<width>.jpg. #115 built it on the premise that Pannellum renders an equirectangular
# image as ONE WebGL texture, so 8192 - a common MAX_TEXTURE_SIZE - was the ceiling and every wider pano
# (newer GSV is 16384 x 8192, Richmond's Mapillary imagery 11000) needed a copy to be displayable at all.
#
# THAT PREMISE IS WRONG BY A FACTOR OF TWO. Pannellum uploads the image as two half-width textures, so its
# own refusal test is max(width/2, height) > MAX_TEXTURE_SIZE and its error reports the maximum as 2*L. A
# device advertising 8192 renders a 16384-wide pano - exactly the widest frame GSV produces and exactly what
# the store holds. Measured on real phones: iPhone 13 Pro reports 16384, Pixel 7 Pro 8192, ceilings 32768 and
# 16384. Over 365 days of production analytics the population that genuinely cannot render 16384 is ~50 users
# a year, all Nexus 5 / 5X. The web app now cuts the copy on demand at the width the client asks for
# (SidewalkWebpage#5256), inside the JPEG decode, at ~105 MB heap and ~2.0 s - which also retires the other
# half of #115's rationale, that ImageIO could not do this without OOM-killing prod JVMs (#5239).
#
# So neither downloader writes one any more; see docs/ops.md for the demand numbers. Two narrow writers
# remain: downscale_panos.py, which runs only when a person runs it, and refetch_panos._refresh_display_copy,
# which rewrites a copy ALREADY on the store after a swap but never creates one - a swap leaves the frame
# unchanged, so a copy left behind would read as current to the sweep for ever. What is kept is deliberate:
# the format, the primitives below, that sweep, and the whole test battery. The margin
# this was built for is real and is exactly ZERO - 16384 = 2 x 8192, and GSV has already widened its frames
# once (13312 -> 16384). The day it widens again, every 8192-class GPU loses native rendering and this
# switch is the answer, so it stays one line away rather than in the history.
#
#: Whether the paths that run on their own - both downloaders, and refetch_panos after a swap - write a
#: display copy. NOT a per-run flag and deliberately not a CLI option: the copy is not an operator's choice
#: run by run, it is a contract with a consumer that no longer wants it. Flipping it back on is a code change
#: that shows up in a diff, and a test asserts the shipped value so the flip cannot be accidental.
#:
#: It does NOT reach the primitives below, and must not: downscale_panos.py is a deliberate, human-invoked
#: sweep and still writes. Guard the policy, not the mechanism.
WRITE_DISPLAY_COPIES = False

# The width is in the file name, so a change of cap is a new sidecar rather than an ambiguous overwrite,
# and the web app looks for exactly the name its own configured cap produces. Every walker that lists the
# store's `*.jpg` has to ask is_downscaled_sidecar(), or a sidecar's stem is taken for a pano id - which
# stays true while any sidecar written before the switch is still on the store.
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

# The store's naming rules live in the installable package (sidewalk_panorama_tools/store.py, #222 step 5),
# which sidewalk-ai-api, RampNet and the auto-labeler pin; these names are re-exported so nothing that says
# `common.walk_store_panos` moved. walk_store_panos stays "the one definition of this file is a panorama".
from sidewalk_panorama_tools.store import (  # noqa: E402,F401  (re-exports)
    is_downscaled_sidecar,              # defined in sidewalk_panorama_tools/store.py
    walk_store_panos,                   # defined in sidewalk_panorama_tools/store.py
)
from sidewalk_panorama_tools.store import sidecar_path as _sidecar_path  # noqa: E402


def downscaled_sidecar_path(pano_path, max_width=None):
    """'<pano stem>.w<max_width>.jpg', beside the pano (`store.sidecar_path` at this repo's cap).

    The default is read from this module's DOWNSCALED_MAX_WIDTH at call time, so the cap stays a common.py
    decision (a disk choice) and a test that moves it moves the name too.
    """
    return _sidecar_path(pano_path, DOWNSCALED_MAX_WIDTH if max_width is None else max_width)


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


#: The widest panorama the viewer fleet renders natively (#121). Pannellum uploads an equirectangular image as
#: two half-width textures, so a device's ceiling is 2 x its MAX_TEXTURE_SIZE; the 8192-class GPUs (a Pixel 7
#: Pro's Mali-G710, measured, and most of the non-Apple fleet with it) therefore stop at 16384 - which is
#: exactly GSV's widest frame today. GSV has widened once already (13312 -> 16384), so the margin is zero.
#:
#: Written from the device's number, NOT as 2 * DOWNSCALED_MAX_WIDTH, although it equals that today and #115's
#: cap was set from the same 8192. The two are different facts: the cap is how wide a copy to write, a choice
#: that can be lowered to save disk, and the ceiling is what the devices can texture, which lowering the cap
#: does not change. Derived from the cap, the tripwire would silently move with it. A test pins that it is not.
VIEWER_MAX_PANO_WIDTH = 2 * 8192


def warn_if_wider_than_viewer_ceiling(pano_id, width, source):
    """Warn, on both channels, when a source reports a frame wider than VIEWER_MAX_PANO_WIDTH (#121).

    A tripwire, never a gate: it does not refuse, alter or delay the download, and it writes nothing to the
    store (in particular it is not a caller of the sidecar primitives above). It should never fire. The day
    it does, the 8192-class devices can no longer render newly stored panoramas natively, and this repo is
    the only place that sees the width at the moment it changes.

    `logging.warning` for scrape.log, which is what is still there next week, and `print` for stdout, which is
    what the alarm wrapper delivers - the repo's rule for a warning that matters. Both, once per wide pano,
    deliberately with no once-per-run latch: the stdout narrative is already one `Processing pano` line per
    pano attempted, so this adds at most one line per NEW pano on the nights it fires; the alarm wrapper cuts
    the middle of a long night's output, so a lone announcement is the line most likely to be cut; and a
    latch would be process-global mutable state for every test to reset. Each line therefore carries the
    whole message, including a pointer to the remedy (not the remedy itself - #153 m6, see the message).

    Every firing also bumps a process-wide count (ceiling_sightings), which is how DownloadRunner.main()
    decides whether tonight is the one run that fails so the alarm is delivered (arm_width_alarm). A count,
    not a flag, so a caller reads the DIFFERENCE across its own run and needs nothing reset between runs.

    Usage, where a downloader has just learned the frame's width::

        warn_if_wider_than_viewer_ceiling(pano_id, width, 'gsv')

    @param width  An int. Callers coerce first: '9000' > '16384' as strings.
    @return Whether it fired.
    """
    global _ceiling_sightings
    if width <= VIEWER_MAX_PANO_WIDTH:
        return False
    _ceiling_sightings += 1
    # The remedy is deliberately a pointer, not the steps (#153 m6): the line is written to be acted on alone,
    # and the steps end in a fleet-wide +63% sweep whose disk budget the runbook puts first.
    message = ("%s pano %s is %d px wide, over the viewer ceiling of %d (#121); 8192-class GPUs cannot "
               "render it natively. Verify the stored file's width, then budget the disk before any sweep: "
               "the remedy writes a copy of nearly every panorama on the store, not only this one. See "
               "docs/ops.md, 'The width tripwire'."
               % (source, pano_id, width, VIEWER_MAX_PANO_WIDTH))
    logging.warning("IMAGEDOWNLOAD: %s", message)
    print("IMAGEDOWNLOAD: WARNING - %s" % message)
    return True


# How many frames wider than the ceiling this process has seen. Only ever read as a difference across one run,
# so it is never reset - see warn_if_wider_than_viewer_ceiling.
_ceiling_sightings = 0


def ceiling_sightings():
    """How many times warn_if_wider_than_viewer_ceiling has fired in this process (#121)."""
    return _ceiling_sightings


# Where the one-time width alarm (#121) is remembered. LOCAL disk, beside the depth block latch, and for the same
# reason: a wider frame is a fact about Google, not about one city, and the storage dir a run is handed belongs
# to one city - a latch there would alarm once per city, up to 53 times for one event.
WIDTH_ALARM_LATCH_FILENAME = 'sidewalk-width-ceiling-alarmed'


def default_width_alarm_latch_path():
    return os.path.join(tempfile.gettempdir(), WIDTH_ALARM_LATCH_FILENAME)


def arm_width_alarm(path):
    """Record that a human has been told the ceiling was crossed; return whether THIS call is the telling.

    The tripwire only warns, and the queue's alarm wrapper delivers only failures, so a warning alone reaches
    no one. Failing every run that sees a wide frame would reach someone and then keep the city red for good,
    because Google does not un-widen - hiding every real failure behind a known one. So the first sighting on
    a host creates this file and that run fails; later runs find it and only warn. Deleting it re-arms.

    Created exclusively and never rewritten, so its content stays the date of the FIRST sighting. A path that
    cannot be written resolves towards alarming (True, with the path logged): a latch nobody can write must
    not swallow the one alarm it exists to deliver. The cost is an alarm every night until the path is fixed.

    Usage, after a run whose sighting count went up::

        if arm_width_alarm(latch_path):
            ...fail this run...

    @return True when this call created the latch, or could not write it; False when it was already there.
    """
    try:
        with open(path, 'x') as f:
            f.write(datetime.now(timezone.utc).isoformat() + chr(10))
        return True
    except FileExistsError:
        return False
    except OSError as e:
        logging.error("IMAGEDOWNLOAD: could not write the width-alarm latch %s (%s); every run that sees a "
                      "frame over the viewer ceiling will fail until it can be written", path, e)
        return True


def retrying_session():
    """A requests.Session with the retry policy both HTTP downloaders use.

    Lives here rather than in one of them because it was about to be a second byte-identical copy, and the
    policy is a decision about how this host treats an imagery API rather than anything Mapillary- or
    Panoramax-specific. What it is NOT is a general-purpose session: 429 and the 5xx family are retried
    inside the adapter, which is exactly why both callers can treat a status that survives to
    raise_for_status() as a condition of the run rather than a verdict on the pano (#41).

    gsv.py deliberately does not use it - the tile fan-out is aiohttp + backoff, a different concurrency
    model with its own retry decisions.
    """
    session = requests.Session()
    retry = Retry(total=5, connect=5, status_forcelist=[429, 500, 502, 503, 504], backoff_factor=1)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session
