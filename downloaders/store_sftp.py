"""Pull already-scraped imagery off the Project Sidewalk pano store over SFTP (#30).

This is the transport behind `DownloadRunner.py --from-store CITY_ID`; the phase loop, the ledger and the
log.csv row live in DownloadRunner.py. Operator documentation: docs/downloader.md, "Pulling from the Project
Sidewalk pano store".

Two facts an operator must not have to infer:

  * The DEFAULT is to download from the imagery provider yourself. Store mode is an alternative for
    collaborators with a working relationship with the Project Sidewalk team, who issue the SFTP credentials
    it needs. How to obtain them is deliberately not documented in this repo, and no host name lives here.
  * Nothing in this module contacts Google, Mapillary or Panoramax. It imports none of their modules, and a
    test holds it to that.

Shape. One `sftp -b -` session per chunk of BATCH_SIZE pano ids. The batch opens with an UNPREFIXED `cd` into
the city's directory - the probe: sftp's batch mode aborts on the first failing unprefixed command, so a
wrong city id or base fails the session loudly on its first line instead of every pano reading as absent -
and then one `-get` per pano. The `-` prefix is load-bearing the other way: a failing `-get` is reported and
skipped, so one pano the store does not have cannot abort the other 99. Per-pano outcomes are therefore
decided from the LOCAL filesystem after the session, never from sftp's exit status or its stderr.

Each file lands as `<final>.part` (exactly the name common.atomic_output_path yields), is verified, and only
then renamed into place through atomic_output_path itself - a saved `.jpg` is the image loop's resume marker,
so a truncated transfer must never become one.

Known limit of the JPEG verifier: it requires the file to END with the EOI marker. A JPEG carrying trailing
bytes after EOI (possible for Mapillary/Panoramax originals, which are stored verbatim; never for GSV, which
this repo encodes) would be refused every run - a loud transient failure in scrape.log, never corruption.
If that shows up in practice, the fallback is comparing against a remote size from an `ls -l` batch.
"""

import argparse
import enum
import os
import posixpath
import re
import stat
import subprocess
import zipfile
from typing import NamedTuple, Optional

from .common import atomic_output_path, jpeg_dimensions

# The log analyzer's names, copied on purpose: log_analyzer/analyze.py::resolve_sftp is the twin, and
# tests/test_store_sftp.py asserts the two lists agree, so a collaborator's one set of variables serves both.
SFTP_ENV_VARS = ('PS_SFTP_HOST', 'PS_SFTP_BASE', 'PS_SFTP_USER', 'PS_SFTP_PORT', 'PS_SFTP_KEY')

#: What a failed store session is reported as in DownloadRunner's tripped-sources set, and so in its exit
#: code. Not an imagery source: store mode has no entry in MAX_CONSECUTIVE_PERMANENT_FAILURES, and a test
#: says so.
STORE_SOURCE_NAME = 'store'

#: Pano ids per sftp session. One handshake per pano would be ~0.5-1 s of overhead each - a day or two of
#: handshakes on a large city - while one session for the whole corpus would have no budget check and no
#: progress line inside it for hours. 100 GSV panos is roughly 0.6-1 GB, so --max-runtime is checked every
#: minute or two on a decent link. A collaborator on a slow link can lower this constant.
BATCH_SIZE = 100

#: The test seam: tests point this at a Python stand-in so the real subprocess.run path runs.
SFTP_COMMAND = ['sftp']

#: A black-holed host fails in 30 s instead of TCP's ~2 minutes.
CONNECT_TIMEOUT_SECONDS = 30

#: A dead connection mid-transfer is noticed after ServerAliveInterval x ServerAliveCountMax (3) seconds.
SERVER_ALIVE_INTERVAL_SECONDS = 15

STDERR_SUMMARY_LINES = 5
STDERR_SUMMARY_CHARS = 200

#: How the redaction learns what an ~/.ssh/config Host alias resolves to (see ssh_config_values). A test seam
#: too: tests/conftest.py points it at a binary that does not exist, so the suite never reads a real config.
SSH_CONFIG_COMMAND = ['ssh', '-G']
#: `ssh -G` only reads config files, so anything slower than this is something wrong; redaction carries on.
SSH_CONFIG_TIMEOUT_SECONDS = 10

# What ssh prints that no configured string names (#155 review item 2). Over-redaction is the safe
# direction: this text goes to scrape.log on a shared store and into the night's message.
#   `port 2222`, and the `[host]:2222` form of known_hosts messages.
_PORT_PATTERNS = (re.compile(r'\bport \d+'), re.compile(r'\]:\d+'))
#   An IPv4 literal, and an IPv6 one (two or more colons among hex digits, dots allowed for ::ffff:a.b.c.d).
_IPV4 = re.compile(r'(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\w)(?!\.\d)')
_IPV6 = re.compile(r'(?<![\w:.])(?=[0-9A-Fa-f:.]*[0-9A-Fa-f])[0-9A-Fa-f.]*:[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*(?![\w:])')
#   Any `name@`: ssh embeds the user it authenticated as, which may be one ~/.ssh/config supplied.
_USER_AT = re.compile(r'[^\s@"\'<>\[\]()]+@')

IMAGE_SUFFIX = '.jpg'
#: == gsv.DEPTH_ARTIFACT_SUFFIX; a test asserts the equality rather than this module importing gsv.
DEPTH_SUFFIX = '.depth.npz'

# A pano id goes into a quoted path in the batch AND into a remote path sftp globs, so it is held to the
# union of the three id alphabets rather than to a blacklist: GSV's base64url ids, Mapillary's digits and
# Panoramax's UUIDs are all [A-Za-z0-9_-].
_SAFE_ID = re.compile(r'[A-Za-z0-9_-]+')
# A city id is one path segment on the store: seattle-wa, cdmx, la-piedad. Starts alphanumeric, so it is
# never '.', '..', or something sftp's option parser could read.
_SAFE_CITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
# What the batch's double quoting cannot express, plus sftp's remote glob characters.
_UNQUOTABLE = ('"', '\n', '\r')
_REMOTE_GLOB = ('*', '?', '[', ']')


class StoreSettings(NamedTuple):
    host: str
    base: str
    user: Optional[str]
    port: Optional[str]
    key: Optional[str]
    remote_city: str


class StoreSessionError(RuntimeError):
    """sftp exited nonzero, or could not start: the SESSION failed (auth, host, host key, the cd probe), not
    any pano. Its message is already redacted and capped, so it is safe to log and to print."""


class PullOutcome(enum.Enum):
    #: Verified and renamed into place.
    pulled = 'pulled'
    #: No file arrived: the store does not hold it tonight. The scrape may add it tomorrow, so this is never
    #: a permanent verdict.
    absent = 'absent'
    #: A file arrived and failed verification (a transfer cut short, or a body that is not the format). It
    #: was removed; nothing was placed.
    truncated = 'truncated'
    #: The file verified but could not be renamed into place (a full disk, a dropped mount). The .part was
    #: removed. Local storage trouble, not the store's: retried next run like the others.
    unplaced = 'unplaced'


def remote_city_id(value):
    """argparse type= for --from-store: one path segment on the store, e.g. seattle-wa.

    No default and no derivation from the fqdn, on purpose: nothing maps one to the other (seattle-wa is
    served by sidewalk-sea), and a guessed id would read as "the store has none of this city's panos".
    """
    if not isinstance(value, str) or not _SAFE_CITY.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "not a store city id: %r (one path segment of letters, digits, '.', '_' or '-', e.g. seattle-wa)"
            % (value,))
    return value


def resolve_settings(remote_city, host=None, base=None, user=None, port=None, key=None, environ=None):
    """Connection settings from flags, each falling back to its PS_SFTP_* variable.

    Host and base are required and have no defaults - the log analyzer's rule, for its reason: they are
    deployment facts, and a wrong default would silently read the wrong store. User, port and key are
    optional so an ~/.ssh/config Host alias can supply them. Raises ValueError naming what is wrong; the
    message carries variable names, never values.
    """
    env = os.environ if environ is None else environ

    def pick(flag, name):
        value = flag if flag else env.get(name)
        return value if value else None

    host = pick(host, 'PS_SFTP_HOST')
    base = pick(base, 'PS_SFTP_BASE')
    user = pick(user, 'PS_SFTP_USER')
    port = pick(port, 'PS_SFTP_PORT')
    key = pick(key, 'PS_SFTP_KEY')

    missing = [name for name, value in (('PS_SFTP_HOST', host), ('PS_SFTP_BASE', base)) if not value]
    if missing:
        raise ValueError("--from-store needs the pano store's connection settings; missing %s. Set them in "
                         "the environment or pass the matching --sftp-* flags. They are issued by the Project "
                         "Sidewalk team - see docs/downloader.md." % ', '.join(missing))
    try:
        remote_city_id(remote_city)
    except argparse.ArgumentTypeError as e:
        raise ValueError(str(e))
    if any(c in base for c in _UNQUOTABLE + _REMOTE_GLOB):
        raise ValueError("PS_SFTP_BASE contains a quote, a line break or a glob character, which the sftp "
                         "batch cannot express")
    if port is not None and not port.isdigit():
        raise ValueError("PS_SFTP_PORT must be a port number")
    if key is not None:
        key = os.path.expanduser(key)
    base = base.rstrip('/') or '/'
    return StoreSettings(host=host, base=base, user=user, port=port, key=key, remote_city=remote_city)


def is_batch_safe_id(pano_id):
    """Whether pano_id can go into a batch line: see _SAFE_ID. An id that cannot is counted failed and logged
    by the phase loop, and never reaches sftp."""
    return isinstance(pano_id, str) and _SAFE_ID.fullmatch(pano_id) is not None


def remote_path(pano_id, suffix):
    """The remote path of one pano's file, RELATIVE to the city directory the batch's `cd` probe entered:
    `./<id[:2]>/<id><suffix>`.

    Never `<base>/<city>/...`: after the `cd`, sftp resolves a relative path against the NEW cwd, so a
    relative PS_SFTP_BASE would send every get to `<base>/<city>/<base>/<city>/...` - every pano "absent",
    exit 0, the silent completion the probe exists to prevent (measured on OpenSSH 9.0p1). The leading `./`
    also keeps an id that begins with '-' from reaching `get`'s option parser: real sftp accepted
    `./-3/-3Kx.jpg` where a bare `-3/...` was `Invalid flag`.
    """
    return './%s/%s%s' % (pano_id[:2], pano_id, suffix)


def local_final_path(storage_path, pano_id, suffix):
    """Where the file lives once placed: the downloaders' own <storage>/<id[:2]>/<id><suffix>. Absolute,
    because the batch line must never start a path with an id (which can begin with '-')."""
    return os.path.join(os.path.abspath(storage_path), pano_id[:2], pano_id + suffix)


def build_batch(settings, storage_path, pano_ids, suffix):
    """The batch text for one session: an unprefixed `cd` probe, then one `-get` per id. See the module
    docstring for why each prefix is what it is. Every path is double-quoted; each remote path is relative to
    the probed directory (see remote_path) and each local one is absolute, the `<final>.part` that
    atomic_output_path(<final>) yields. No user, host, port or key appears."""
    if any(c in str(storage_path) for c in _UNQUOTABLE):
        raise ValueError("the storage path contains a quote or a line break, which the sftp batch cannot "
                         "express")
    # posixpath, not '%s/%s' and not os.path: the remote is POSIX whatever this host is, and a chroot base
    # of '/' must probe "/<city>", not "//<city>".
    lines = ['cd "%s"' % posixpath.join(settings.base, settings.remote_city)]
    for pano_id in pano_ids:
        if not is_batch_safe_id(pano_id):
            raise ValueError("pano id %r cannot be written into an sftp batch" % (pano_id,))
        lines.append('-get "%s" "%s"' % (remote_path(pano_id, suffix),
                                         local_final_path(storage_path, pano_id, suffix) + '.part'))
    return '\n'.join(lines) + '\n'


def sftp_argv(settings):
    """The sftp command line. Never interactive: BatchMode=yes makes a missing key or a passphrase fail in
    about a second under cron instead of waiting on a prompt nothing can answer.

    StrictHostKeyChecking=accept-new, not the log analyzer's `no`: `no` silently accepts a CHANGED host key,
    which on a credentialed channel writing imagery into someone's store is an invitation; accept-new trusts
    an unknown host once and refuses a change (OpenSSH >= 7.6).
    """
    argv = list(SFTP_COMMAND)
    if settings.port:
        argv += ['-P', settings.port]
    if settings.key:
        argv += ['-i', settings.key]
    argv += ['-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new',
             '-o', 'ConnectTimeout=%d' % CONNECT_TIMEOUT_SECONDS,
             '-o', 'ServerAliveInterval=%d' % SERVER_ALIVE_INTERVAL_SECONDS,
             '-b', '-',
             '%s@%s' % (settings.user, settings.host) if settings.user else settings.host]
    return argv


def ssh_config_values(settings):
    """What ssh will actually connect as, from `ssh -G [-p PORT] [-l USER] HOST`: {'hostname', 'user', 'port'}.

    PS_SFTP_HOST may be an ~/.ssh/config alias - the docs invite one - and then ssh's messages carry the
    alias's resolved HostName (or an IP), User and Port, none of which the configured strings name. `ssh -G`
    prints the resolved configuration without connecting. Every failure - ssh missing, too old for -G, a
    nonzero exit, a timeout, undecodable output - returns {} and the redaction carries on with what it has:
    this is a best effort on top of the patterns in redact(), never a reason to lose the error message.
    """
    argv = list(SSH_CONFIG_COMMAND)
    if settings.port:
        argv += ['-p', settings.port]
    if settings.user:
        argv += ['-l', settings.user]
    argv.append(settings.host)
    try:
        result = subprocess.run(argv, capture_output=True, encoding='utf-8', errors='replace',
                                timeout=SSH_CONFIG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    values = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ('hostname', 'user', 'port'):
            values[parts[0].lower()] = parts[1].strip()
    return values


def _replace_word(text, value, placeholder):
    """Replace value only where it stands alone - not inside an id, a longer name or a longer number."""
    return re.sub(r'(?<![\w.-])%s(?![\w-])' % re.escape(value), placeholder, text)


def redact(text, settings, resolved=None):
    """Replace connection details in ssh/sftp output with placeholders. Four layers, in this order:

    1. The configured strings, wherever they appear: the key path (<key>) first, because it often contains
       the user name; then the host (<host>), the user (<user>) and the port (<port>).
    2. `resolved` - ssh_config_values()'s hostname, user and port, what an ~/.ssh/config alias expands to -
       replaced only as whole words, since `ssh -G` always reports SOME user and port (on a plain host, the
       local login and 22) and those must not be carved out of pano ids.
    3. Patterns for what neither names: `port N` and `]:N`, IPv4 and IPv6 literals (<ip>), and any `name@`.

    NOT redacted: a host name that ssh prints on its own and that neither the settings nor `ssh -G` know
    (e.g. a ProxyJump hop's), and paths other than the key. Empty values are skipped: ''.replace would put
    the placeholder between every character.
    """
    for value, placeholder in ((settings.key, '<key>'), (settings.host, '<host>'), (settings.user, '<user>'),
                               (settings.port, '<port>')):
        if value:
            text = text.replace(value, placeholder)
    for name, placeholder in (('hostname', '<host>'), ('user', '<user>'), ('port', '<port>')):
        value = (resolved or {}).get(name)
        if value:
            text = _replace_word(text, value, placeholder)
    text = _PORT_PATTERNS[0].sub('port <port>', text)
    text = _PORT_PATTERNS[1].sub(']:<port>', text)
    text = _IPV6.sub('<ip>', text)   # first, so ::ffff:a.b.c.d goes as one address
    text = _IPV4.sub('<ip>', text)
    return _USER_AT.sub('<user>@', text)


def summarize_stderr(text, settings):
    """One redacted, capped line for a log or a cron mail: the first STDERR_SUMMARY_LINES non-blank lines,
    each at most STDERR_SUMMARY_CHARS, joined with ' | '. Asks `ssh -G` what the host resolves to (see
    ssh_config_values) - once per failed session, which stops the pass, so at most twice a run."""
    redacted = redact(text or '', settings, ssh_config_values(settings))
    lines = [line.strip() for line in redacted.splitlines() if line.strip()]
    lines = [line[:STDERR_SUMMARY_CHARS] for line in lines[:STDERR_SUMMARY_LINES]]
    return ' | '.join(lines) if lines else '(no error output)'


def run_sftp_batch(settings, batch_text):
    """THE one subprocess call in store mode. The batch goes in on stdin, which is what `-b -` reads."""
    return subprocess.run(sftp_argv(settings), input=batch_text, capture_output=True, text=True)


def is_complete_jpeg(path):
    """A readable SOF header AND a trailing EOI (FF D9).

    The header alone is not enough here. The HTTP downloaders never see a short file (a short read raises
    out of iter_content before any check), but a `get` that dies mid-transfer leaves a half file whose SOF
    header is intact and reports full dimensions. A false refusal is the harmless direction: a transient
    failure, retried next run, never a resume marker.
    """
    if jpeg_dimensions(path) is None:
        return False
    # A readable SOF means the file is far longer than two bytes, so the seek cannot fail on a short file.
    with open(path, 'rb') as f:
        f.seek(-2, os.SEEK_END)
        return f.read(2) == b'\xff\xd9'


def is_complete_npz(path):
    """A .npz is a zip, and the End-of-Central-Directory record zipfile looks for is the last thing written,
    so a truncated artifact fails this. is_zipfile answers False for a missing file rather than raising."""
    return zipfile.is_zipfile(path)


def ensure_shard_dir(storage_path, pano_id):
    """Create <storage>/<id[:2]> the way the downloaders do: sftp will not create a local directory."""
    destination_dir = os.path.join(os.path.abspath(storage_path), pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent runs race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_parts(finals):
    for final in finals.values():
        _remove_quietly(final + '.part')


def pull_batch(settings, storage_path, pano_ids, suffix, verifier):
    """Pull one chunk in one session and return {pano_id: PullOutcome}.

    Before the session: every shard dir exists, and any `<final>.part` left by a run killed mid-session is
    removed - otherwise a complete-looking stale file from last week would be "verified" and placed tonight
    although tonight's get failed.

    A nonzero exit (a negative one included: death by signal) raises StoreSessionError (redacted, capped)
    after removing whatever .part files the session left, so no half-done state survives it; a stop raised
    while the session runs removes them too, then propagates unchanged. Otherwise each id is decided from the local filesystem:
    no .part -> absent; .part failing `verifier` -> truncated (removed); else renamed into place through
    atomic_output_path -> pulled, or unplaced if the chmod/rename itself failed (the .part is removed).
    """
    if not pano_ids:
        return {}
    finals = {pano_id: local_final_path(storage_path, pano_id, suffix) for pano_id in pano_ids}
    batch_text = build_batch(settings, storage_path, pano_ids, suffix)
    for pano_id, final in finals.items():
        ensure_shard_dir(storage_path, pano_id)
        _remove_quietly(final + '.part')

    try:
        result = run_sftp_batch(settings, batch_text)
    except OSError as e:
        # sftp not installed, or not executable. strerror only: the exception's own text can carry argv.
        raise StoreSessionError("could not start %s: %s" % (os.path.basename(SFTP_COMMAND[0]), e.strerror))
    except BaseException:
        # A stop mid-session (SIGTERM's SystemExit, Ctrl-C): subprocess.run has killed sftp and re-raised.
        # Leave nothing half-done behind, and let the stop reach the runner as itself.
        _remove_parts(finals)
        raise
    # Nonzero, not > 0: subprocess reports a death by signal as a negative returncode.
    if result.returncode != 0:
        _remove_parts(finals)
        raise StoreSessionError("sftp exited %d: %s"
                                % (result.returncode, summarize_stderr(result.stderr, settings)))

    outcomes = {}
    for pano_id, final in finals.items():
        part = final + '.part'
        if not os.path.isfile(part):
            outcomes[pano_id] = PullOutcome.absent
            continue
        try:
            with atomic_output_path(final) as tmp:
                if not verifier(tmp):
                    raise _Incomplete()
        except _Incomplete:
            outcomes[pano_id] = PullOutcome.truncated
            continue
        except OSError:
            outcomes[pano_id] = PullOutcome.unplaced
            continue
        outcomes[pano_id] = PullOutcome.pulled
    return outcomes


class _Incomplete(Exception):
    """Raised inside atomic_output_path so it removes the .part instead of renaming it."""
