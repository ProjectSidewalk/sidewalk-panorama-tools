# !/usr/bin/python3
"""cron's mail-on-output rule, with the delivery pluggable (#141).

Every alarm in this repo is an exit code that "cron mails": the queue exits nonzero for a failed, hung or
never-reached city (#101) and for a breaker trip (#113), DownloadRunner prints its WARNINGs to stdout, and
scrape_queue orders its summary so the mail leads with what went wrong. The production host has no MTA, so
since the box was built on 2026-09-01 cron has logged `No MTA installed, discarding output` after every
nightly and delivered none of it. This wrapper is the piece between cron and the queue that makes the
channel real:

  python3 cron_notify.py --sink CMD [--name NAME] [--max-bytes N] [--log FILE] -- <command...>

It runs the command with stdout and stderr merged, streams every line to its own stdout as it arrives (so a
hand-run is live, and cron mail would still carry it if an MTA ever appeared), and when the command exits
hands the whole capture to the sink command if - and only if - there was any. That is cron's rule exactly:
mail on any output, not on nonzero exit alone, which is what lets a WARNING on an otherwise clean night
reach anyone. --only-on-failure narrows that to nonzero exits (production's choice: one message on a bad
night, silence on a good one, with the --log line as the proof the wrapper ran); a failure that printed
nothing is still delivered, with a body that says so. The sink is a shell command run with the body on stdin and in the file NOTIFY_BODY_FILE
names, NOTIFY_SUBJECT set to "<name>: exit <code> on <host>" (ASCII, one line, at most 100 characters - the
constraints SNS puts on a subject) and NOTIFY_EXIT to the command's exit code. Production's sink is
`aws sns publish --region us-west-2 --topic-arn ... --subject "$NOTIFY_SUBJECT" --message file://$NOTIFY_BODY_FILE`, which
publishes with the instance role and no secret on the box; nothing here knows or cares what the sink is.

The exit code is the command's own, so the queue's exit 1 still reaches whatever reads it. The one code the
wrapper adds is 4: the command exited 0 and the sink failed. A wrapper that swallowed its own delivery
failure would be #141's bug one layer up - a clean exit and no message, indistinguishable from a clean
night. A command that cannot be started at all is reported THROUGH the sink (exit 127), since that is
exactly the night nobody would otherwise hear about.

Why a wrapper outside the queue rather than a --notify-command inside it: the queue's stdout is only visible
from outside (DownloadRunner's narrative goes to the inherited stdout, not through the queue), and a queue
that crashes before its summary must still be reported. The wrapper is the only process that sees both.

SIGTERM is forwarded to the command exactly once, however many arrive. scrape_queue turns SIGTERM into
sys.exit(143) and, on the way out, stops the city it is supervising; a SECOND SIGTERM landing during that
unwind interrupts the stop and orphans the city. So an operator stopping the queue should signal the queue
process, not this one - see docs/ops.md, "Hearing about a bad night".
"""

import argparse
import os
import signal
import socket
import subprocess
import sys
import tempfile
from datetime import datetime

# SNS caps a message at 256 KB and a backlog night prints one line per pano, so the capture is cut to this
# before delivery. Head and tail are kept - the queue's summary is at the END of its output - with a marker
# between them saying how much went. The full narrative is always in the queue's own log and each city's
# scrape.log; the message is the alarm, not the record.
DEFAULT_MAX_BYTES = 200_000
# Share of the byte budget given to the head; the rest is tail. Tail heavy because the summary lives there.
HEAD_FRACTION = 0.2
_MARKER = '[cron_notify] %d bytes omitted (over --max-bytes %d)\n'

# SNS refuses a subject over 100 characters, containing non-ASCII, or spanning a line - and refuses the whole
# publish with it, so a job name that broke the rule would silence the channel.
SUBJECT_MAX_CHARS = 100

# The command exited 0 and the sink did not deliver. 4 because the queue uses 0/1/2/3 and the two are read
# side by side in the log.
EXIT_SINK_FAILED = 4
# The command itself could not be started (a missing interpreter or script). The shell's own convention.
EXIT_COULD_NOT_START = 127

_BODY_FILE_VAR = 'NOTIFY_BODY_FILE'
_SUBJECT_VAR = 'NOTIFY_SUBJECT'
_EXIT_VAR = 'NOTIFY_EXIT'


def _max_bytes(value):
    """argparse type for --max-bytes: an int that leaves room for the marker line plus something to keep."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('%r is not an integer' % (value,))
    floor = len(_MARKER % (10 ** 9, 10 ** 9)) + 2
    if n < floor:
        raise argparse.ArgumentTypeError('%d is too small to hold the omission marker; use at least %d'
                                         % (n, floor))
    return n


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run a command and hand its output to a delivery command when there is any - cron's "
                    'mail-on-output rule, for a host that cannot send mail.',
        epilog='Everything after -- is the command to run.')
    parser.add_argument('--sink', required=True, metavar='CMD',
                        help='Shell command that delivers the output. Run only when the command produced '
                             'any, with the output on stdin and in the file $%s names, $%s set to '
                             '"<name>: exit <code> on <host>" and $%s to the exit code. Deliberately has no '
                             'default: where a host reports to is a deployment fact.'
                             % (_BODY_FILE_VAR, _SUBJECT_VAR, _EXIT_VAR))
    parser.add_argument('--name', default=None, metavar='NAME',
                        help="What the subject calls the job (default: the command's basename).")
    parser.add_argument('--max-bytes', type=_max_bytes, default=DEFAULT_MAX_BYTES, metavar='N',
                        help='Cut the delivered output to this many bytes, keeping the head and the tail '
                             '(default %(default)d; SNS refuses a message over 256 KB).')
    parser.add_argument('--only-on-failure', action='store_true',
                        help='Deliver only when the command exits nonzero, instead of whenever it printed '
                             "anything (cron's rule, the default). A failure that printed nothing is still "
                             'delivered, with a body saying so. The cost is that a WARNING on an otherwise '
                             'clean night is not delivered - it is still in the per-city scrape.log.')
    parser.add_argument('--log', default=None, metavar='FILE',
                        help='Append one line per run - when, the exit code, whether anything was delivered '
                             "- to this file. stderr under cron goes to the same nowhere this wrapper exists "
                             'to fix, so this is where a failed delivery is visible the morning after.')
    parser.add_argument('command', nargs='+', metavar='-- COMMAND',
                        help='The command to run, after --.')
    return parser


def hostname():
    return socket.gethostname()


def subject_for(name, exit_code, host):
    """The one-line, ASCII, <=100-character subject SNS will accept."""
    raw = '%s: exit %s on %s' % (name, exit_code, host)
    one_line = ' '.join(raw.splitlines())
    ascii_only = one_line.encode('ascii', 'replace').decode('ascii')
    return ascii_only[:SUBJECT_MAX_CHARS]


def truncate(body, max_bytes):
    """Cut body to at most max_bytes: the first HEAD_FRACTION of the budget, a marker line, the rest as tail.

    Cuts land on line boundaries - the head ends after a newline, the tail starts after one - so no line is
    split and, as a consequence, no multi-byte character is either. The marker states exactly how many bytes
    are missing between the two.
    """
    if len(body) <= max_bytes:
        return body
    # The marker's own length depends on the number in it; it can only shrink as the count drops, and the
    # widest plausible count is the whole body, so size the budget for that and let it be a byte or two
    # conservative.
    marker_room = len((_MARKER % (len(body), max_bytes)).encode('ascii'))
    budget = max_bytes - marker_room
    head_budget = int(budget * HEAD_FRACTION)
    tail_budget = budget - head_budget

    head = body[:head_budget]
    cut = head.rfind(b'\n')
    head = head[:cut + 1] if cut >= 0 else b''

    tail = body[len(body) - tail_budget:]
    cut = tail.find(b'\n')
    tail = tail[cut + 1:] if cut >= 0 else tail

    omitted = len(body) - len(head) - len(tail)
    marker = (_MARKER % (omitted, max_bytes)).encode('ascii')
    return head + marker + tail


def _echo(chunk):
    """Re-emit a chunk of the command's output on our own stdout, bytes for bytes where possible."""
    out = getattr(sys.stdout, 'buffer', None)
    if out is not None:
        out.write(chunk)
        out.flush()
    else:
        sys.stdout.write(chunk.decode('utf-8', 'replace'))
        sys.stdout.flush()


def run_command(command):
    """Run command, streaming its merged stdout+stderr through and keeping a copy.

    Returns (exit_code, captured bytes). A signalled child comes back as 128+n, the way cron and a shell read
    it, rather than Popen's negative code. Installs a SIGTERM handler for the duration that forwards the
    signal to the child ONCE - see the module docstring for why once.
    """
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        message = 'cron_notify: could not start %s: %s\n' % (' '.join(command), e)
        sys.stderr.write(message)
        sys.stderr.flush()
        return EXIT_COULD_NOT_START, message.encode('utf-8')

    forwarded = []

    def forward_once(*_):
        if not forwarded:
            forwarded.append(True)
            proc.terminate()

    previous = signal.signal(signal.SIGTERM, forward_once)
    chunks = []
    try:
        for line in iter(proc.stdout.readline, b''):
            chunks.append(line)
            _echo(line)
        exit_code = proc.wait()
    finally:
        proc.stdout.close()
        signal.signal(signal.SIGTERM, previous)
    if exit_code < 0:
        exit_code = 128 - exit_code
    return exit_code, b''.join(chunks)


def deliver(sink, body, subject, exit_code):
    """Run the sink with the body on stdin and in a temp file. Returns None on success, else a reason."""
    fd, path = tempfile.mkstemp(prefix='cron-notify-', suffix='.txt')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body)
        env = dict(os.environ)
        env[_BODY_FILE_VAR] = path
        env[_SUBJECT_VAR] = subject
        env[_EXIT_VAR] = str(exit_code)
        try:
            result = subprocess.run(sink, shell=True, input=body, env=env)
        except OSError as e:
            return 'sink could not start: %s' % (e,)
        if result.returncode != 0:
            return 'sink failed (exit %d)' % (result.returncode,)
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _log_line(path, exit_code, outcome):
    """One line: a wall-clock stamp WITH its offset (the #101 rule), the exit code, what happened."""
    if path is None:
        return
    stamp = datetime.now().astimezone().isoformat(timespec='seconds')
    with open(path, 'a', encoding='utf-8') as f:
        f.write('%s exit %d %s\n' % (stamp, exit_code, outcome))


def main(argv=None):
    """Run the command, deliver its output, return the exit code cron should see."""
    args = build_parser().parse_args(argv)
    name = args.name if args.name is not None else os.path.basename(args.command[0])

    exit_code, body = run_command(args.command)

    if args.only_on_failure and exit_code == 0:
        _log_line(args.log, exit_code, 'nothing to publish (clean run, --only-on-failure)')
        return exit_code
    if not body:
        if not args.only_on_failure:
            _log_line(args.log, exit_code, 'nothing to publish')
            return exit_code
        # A failure is the event here, output or not: a command that died before printing anything is the
        # one nobody would otherwise hear about.
        body = ('cron_notify: %s exited %d without printing anything\n' % (name, exit_code)).encode('utf-8')

    body = truncate(body, args.max_bytes)
    failure = deliver(args.sink, body, subject_for(name, exit_code, hostname()), exit_code)
    if failure is None:
        _log_line(args.log, exit_code, 'published %d bytes' % len(body))
        return exit_code

    sys.stderr.write('cron_notify: %s; the output above was not delivered\n' % failure)
    sys.stderr.flush()
    _log_line(args.log, exit_code, failure)
    # The command's own nonzero code is the more important fact and is kept; only a clean run is turned into
    # the wrapper's own code, so a delivery failure is never read as a quiet night.
    return exit_code if exit_code != 0 else EXIT_SINK_FAILED


if __name__ == '__main__':
    sys.exit(main())
