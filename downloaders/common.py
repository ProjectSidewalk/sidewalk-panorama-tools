import contextlib
import enum
import errno
import importlib
import logging
import os
import re
import struct

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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
    --measure, and DownloadRunner. That last one is currently a policy for a path nothing takes: #115 put an
    Image.open on the Mapillary download path, and WRITE_DISPLAY_COPIES (below) has been False since
    2026-09-09, so nothing reaches it. It is kept because it is correct either way and is needed the moment
    the switch is flipped - see DownloadRunner.main. It lives
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

    Usage, where a downloader has just learned the frame's width::

        warn_if_wider_than_viewer_ceiling(pano_id, width, 'gsv')

    @param width  An int. Callers coerce first: '9000' > '16384' as strings.
    @return Whether it fired.
    """
    if width <= VIEWER_MAX_PANO_WIDTH:
        return False
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
