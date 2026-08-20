import contextlib
import enum
import os


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
