import contextlib
import os


class Enum(object):
    def __init__(self, tuplelist):
        self.tuplelist = tuplelist

    def __getattr__(self, name):
        return self.tuplelist.index(name)


DownloadResult = Enum(('skipped', 'success', 'fallback_success', 'failure'))


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
