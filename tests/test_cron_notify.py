"""Tests for cron_notify.py — cron's mail-on-output rule with the delivery pluggable (#141).

The production host has no MTA, so every "cron mails it" in this repo has gone to /dev/null since the box was
built. cron_notify.py sits between cron and the queue: it runs the command, keeps its stdout+stderr, and hands
the lot to a sink command when there is any. The sink is what makes it deliverable (aws sns publish, in
production); here it is a Python script that journals what it was given, so every claim below is an assertion
about bytes that arrived rather than about argv the wrapper built.

Three properties that are cheap to break and expensive to notice:
  - the exit code is the child's (cron's contract; the queue's exit 1 is the alarm the sink carries),
  - a failed delivery is not silent (exit 4 on a clean run, a stderr line and a --log line always),
  - SIGTERM reaches the child exactly once (a second one would interrupt the queue's own stop of the city it
    is supervising and orphan it).
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import cron_notify
from conftest import posix_only

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The stand-in child prints through a text-mode stdout, so its line ending is the platform's. The wrapper is
# byte-faithful by design, so the expectations are built with the same ending rather than normalised.
NL = os.linesep.encode()


def lines(*texts):
    return b''.join(t.encode() + NL for t in texts)


# --- Stand-ins -----------------------------------------------------------------------------------------------
#
# A child that prints what CHILD_SPEC says ('o:text|e:text', stdout or stderr, one line each, flushed in
# order), sleeps CHILD_SLEEP, exits CHILD_EXIT. With CHILD_JOURNAL set it traps SIGTERM: journals it, then
# finishes its sleep before exiting 143 - the window in which a SECOND forwarded SIGTERM would be observable.
# CHILD_SELF_KILL makes it SIGKILL itself after printing, the one exit Popen reports as a negative code.

CHILD = textwrap.dedent('''
    import os, signal, sys, time

    journal = os.environ.get('CHILD_JOURNAL')
    terms = []

    def on_term(*_):
        terms.append(1)
        with open(journal, 'a') as f:
            f.write('TERM\\n')

    if journal:
        signal.signal(signal.SIGTERM, on_term)
        with open(journal, 'a') as f:
            f.write('START\\n')
    for item in filter(None, os.environ.get('CHILD_SPEC', '').split('|')):
        stream, text = item.split(':', 1)
        f = sys.stdout if stream == 'o' else sys.stderr
        f.write(text + '\\n')
        f.flush()
    if os.environ.get('CHILD_SELF_KILL'):
        os.kill(os.getpid(), signal.SIGKILL)
    deadline = time.monotonic() + float(os.environ.get('CHILD_SLEEP', '0'))
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if terms:
            time.sleep(0.5)
            sys.exit(143)
    sys.exit(int(os.environ.get('CHILD_EXIT', '0')))
''')

# A sink that records everything the wrapper hands it: stdin, the NOTIFY_* environment, the body file's
# contents, and how many times it was called. Exits SINK_EXIT.
SINK = textwrap.dedent('''
    import json, os, sys

    d = os.environ['SINK_DIR']
    body = sys.stdin.buffer.read()
    with open(os.path.join(d, 'stdin.bin'), 'ab') as f:
        f.write(body)
    with open(os.environ['NOTIFY_BODY_FILE'], 'rb') as f:
        file_body = f.read()
    with open(os.path.join(d, 'file.bin'), 'ab') as f:
        f.write(file_body)
    env = {k: v for k, v in os.environ.items() if k.startswith('NOTIFY_')}
    with open(os.path.join(d, 'env.json'), 'w') as f:
        json.dump(env, f)
    with open(os.path.join(d, 'calls'), 'a') as f:
        f.write('call\\n')
    sys.exit(int(os.environ.get('SINK_EXIT', '0')))
''')


@pytest.fixture
def child(tmp_path):
    path = tmp_path / 'child.py'
    path.write_text(CHILD)
    return [sys.executable, str(path)]


@pytest.fixture
def sink(tmp_path, monkeypatch):
    """The sink command string plus a reader for what it journalled."""
    path = tmp_path / 'sink.py'
    path.write_text(SINK)
    sink_dir = tmp_path / 'sink'
    sink_dir.mkdir()
    monkeypatch.setenv('SINK_DIR', str(sink_dir))

    class Sink:
        command = subprocess.list2cmdline([sys.executable, str(path)])

        @staticmethod
        def calls():
            f = sink_dir / 'calls'
            return len(f.read_text().splitlines()) if f.exists() else 0

        @staticmethod
        def stdin():
            return (sink_dir / 'stdin.bin').read_bytes()

        @staticmethod
        def file():
            return (sink_dir / 'file.bin').read_bytes()

        @staticmethod
        def env():
            return json.loads((sink_dir / 'env.json').read_text())

    return Sink


def run(child, sink, *extra, spec='', exit_code=0, monkeypatch=None):
    monkeypatch.setenv('CHILD_SPEC', spec)
    monkeypatch.setenv('CHILD_EXIT', str(exit_code))
    return cron_notify.main(['--sink', sink.command, '--name', 'job', *extra, '--', *child])


# --- What the sink receives -----------------------------------------------------------------------------------

class TestTheSinkGetsTheOutput:

    def test_stdout_and_stderr_arrive_once_in_order_on_stdin_and_in_the_body_file(self, child, sink,
                                                                                    monkeypatch, capsysbinary):
        code = run(child, sink, spec='o:first|e:WARNING second|o:third', monkeypatch=monkeypatch)

        assert code == 0
        assert sink.calls() == 1
        assert sink.stdin() == lines('first', 'WARNING second', 'third')
        assert sink.file() == sink.stdin()
        # And the wrapper re-emits the same bytes on its own stdout, so a hand-run is live and cron mail
        # would still carry it if an MTA ever appeared.
        assert capsysbinary.readouterr().out == lines('first', 'WARNING second', 'third')

    def test_the_subject_names_the_job_the_exit_code_and_the_host(self, child, sink, monkeypatch):
        run(child, sink, spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        env = sink.env()
        assert env['NOTIFY_EXIT'] == '1'
        assert env['NOTIFY_SUBJECT'] == 'job: exit 1 on %s' % cron_notify.hostname()

    def test_the_subject_default_name_is_the_commands_basename(self, child, sink, monkeypatch):
        monkeypatch.setenv('CHILD_SPEC', 'o:x')
        monkeypatch.setenv('CHILD_EXIT', '0')
        cron_notify.main(['--sink', sink.command, '--', *child])
        expected = os.path.basename(sys.executable)
        assert sink.env()['NOTIFY_SUBJECT'].startswith(expected + ': exit 0 on ')

    def test_empty_output_publishes_nothing(self, child, sink, monkeypatch):
        """cron's rule: no output, no mail. A sink run on nothing would be one message a night saying
        nothing, and would train everyone to filter the channel."""
        code = run(child, sink, spec='', exit_code=1, monkeypatch=monkeypatch)
        assert code == 1
        assert sink.calls() == 0

    def test_the_body_file_is_removed_afterwards(self, child, sink, monkeypatch):
        run(child, sink, spec='o:x', monkeypatch=monkeypatch)
        assert not os.path.exists(sink.env()['NOTIFY_BODY_FILE'])

    def test_a_body_file_that_cannot_be_removed_does_not_fail_the_delivery(self, child, sink, monkeypatch):
        """A sink that deleted the file itself, or a temp dir that went read-only, is not a failed delivery:
        the message has gone. Exit stays the child's and the sink is not re-run."""
        monkeypatch.setattr(cron_notify.os, 'remove', lambda path: (_ for _ in ()).throw(OSError('gone')))
        code = run(child, sink, spec='o:x', exit_code=0, monkeypatch=monkeypatch)
        assert code == 0
        assert sink.calls() == 1

    def test_the_echo_survives_a_stdout_with_no_binary_buffer(self, child, sink, monkeypatch):
        """Under some captures sys.stdout is a plain text stream; the echo must still land, decoded."""
        import io
        monkeypatch.setattr(sys, 'stdout', io.StringIO())
        run(child, sink, spec='o:hello', monkeypatch=monkeypatch)
        assert sys.stdout.getvalue() == 'hello' + os.linesep


# --- --only-on-failure ----------------------------------------------------------------------------------------

class TestOnlyOnFailure:
    """Production's choice (2026-09-18): one message on a bad night, silence on a good one. The rule then is
    the exit code, not the output - a failure that printed nothing is exactly the one nobody would otherwise
    hear about, so it is delivered with a body that says so."""

    def test_a_clean_run_with_output_is_not_delivered(self, child, sink, tmp_path, monkeypatch):
        log = tmp_path / 'notify.log'
        code = run(child, sink, '--only-on-failure', '--log', str(log),
                   spec='o:banner|o:WARNING something', exit_code=0, monkeypatch=monkeypatch)
        assert code == 0
        assert sink.calls() == 0
        assert 'exit 0 nothing to publish (clean run, --only-on-failure)' in log.read_text()

    def test_a_failed_run_is_delivered_with_its_output(self, child, sink, monkeypatch):
        code = run(child, sink, '--only-on-failure', spec='o:banner|o:FAILED', exit_code=1,
                   monkeypatch=monkeypatch)
        assert code == 1
        assert sink.calls() == 1
        assert sink.stdin() == lines('banner', 'FAILED')
        assert sink.env()['NOTIFY_EXIT'] == '1'

    def test_a_failed_run_that_printed_nothing_is_still_delivered(self, child, sink, monkeypatch):
        """Under cron's rule no output means no mail even on failure; here the failure is the event."""
        code = run(child, sink, '--only-on-failure', spec='', exit_code=3, monkeypatch=monkeypatch)
        assert code == 3
        assert sink.calls() == 1
        assert sink.stdin() == b'cron_notify: job exited 3 without printing anything\n'

    def test_a_sink_failure_on_a_failed_run_keeps_the_runs_code(self, child, sink, monkeypatch, capsys):
        monkeypatch.setenv('SINK_EXIT', '1')
        code = run(child, sink, '--only-on-failure', spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        assert code == 1
        assert 'cron_notify: sink failed' in capsys.readouterr().err


# --- The exit code is the child's ----------------------------------------------------------------------------

class TestTheExitCodeIsTheChilds:

    @pytest.mark.parametrize('exit_code', [0, 1, 143])
    def test_passthrough(self, child, sink, monkeypatch, exit_code):
        assert run(child, sink, spec='o:x', exit_code=exit_code, monkeypatch=monkeypatch) == exit_code

    def test_a_sink_failure_after_a_clean_run_exits_4_and_says_so(self, child, sink, monkeypatch, capsys):
        """The one code the wrapper adds. A wrapper that swallowed its own delivery failure would be this
        issue's bug one layer up: a clean exit and no message, indistinguishable from a clean night."""
        monkeypatch.setenv('SINK_EXIT', '1')
        code = run(child, sink, spec='o:x', exit_code=0, monkeypatch=monkeypatch)
        assert code == cron_notify.EXIT_SINK_FAILED == 4
        assert 'cron_notify: sink failed (exit 1)' in capsys.readouterr().err

    def test_a_sink_failure_after_a_failed_run_keeps_the_runs_code(self, child, sink, monkeypatch, capsys):
        """The night's verdict is the more important fact; both are nonzero either way."""
        monkeypatch.setenv('SINK_EXIT', '1')
        code = run(child, sink, spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        assert code == 1
        assert 'cron_notify: sink failed (exit 1)' in capsys.readouterr().err

    def test_a_sink_that_cannot_run_is_a_sink_failure(self, child, monkeypatch, capsys):
        monkeypatch.setenv('CHILD_SPEC', 'o:x')
        monkeypatch.setenv('CHILD_EXIT', '0')
        code = cron_notify.main(['--sink', 'definitely-not-a-command-4f2c', '--', *child])
        assert code == 4
        assert 'cron_notify: sink failed' in capsys.readouterr().err

    def test_a_shell_that_cannot_be_spawned_is_a_sink_failure_too(self, child, sink, monkeypatch, capsys):
        """The shell itself failing to start (fork limits, a missing /bin/sh) is the one OSError the sink
        path can raise; it must land in the same exit-4 arm rather than propagating as a traceback."""
        monkeypatch.setenv('CHILD_SPEC', 'o:x')
        monkeypatch.setenv('CHILD_EXIT', '0')

        def cannot_spawn(*args, **kwargs):
            raise OSError('resource temporarily unavailable')

        monkeypatch.setattr(cron_notify.subprocess, 'run', cannot_spawn)
        code = cron_notify.main(['--sink', sink.command, '--', *child])
        assert code == 4
        assert 'cron_notify: sink could not start: resource temporarily unavailable' in capsys.readouterr().err

    def test_a_child_that_cannot_start_is_reported_through_the_sink(self, sink, tmp_path, capsys):
        missing = str(tmp_path / 'no-such-program')
        code = cron_notify.main(['--sink', sink.command, '--name', 'job', '--', missing])
        assert code == cron_notify.EXIT_COULD_NOT_START == 127
        assert sink.calls() == 1
        body = sink.stdin().decode()
        assert body.startswith('cron_notify: could not start ') and missing in body
        assert sink.env()['NOTIFY_EXIT'] == '127'
        assert 'could not start' in capsys.readouterr().err

    @posix_only
    def test_a_signal_death_reads_as_128_plus_the_signal(self, child, sink, monkeypatch):
        """Popen reports a signalled child as a negative code; cron and shells read 128+n."""
        monkeypatch.setenv('CHILD_SPEC', 'o:x')
        monkeypatch.setenv('CHILD_SELF_KILL', '1')
        code = cron_notify.main(['--sink', sink.command, '--', *child])
        assert code == 137
        assert sink.env()['NOTIFY_EXIT'] == '137'


# --- Truncation ----------------------------------------------------------------------------------------------

class TestTruncation:
    """SNS caps a message at 256 KB and a backlog night prints one line per pano. The summary is at the END
    of the queue's output, so the tail is what must survive."""

    def test_under_the_limit_is_byte_identical(self):
        body = b'a\nbb\nccc\n'
        assert cron_notify.truncate(body, 100) == body
        assert cron_notify.truncate(body, len(body)) == body

    def test_over_the_limit_keeps_head_and_tail_on_line_boundaries_with_a_marker(self):
        lines = [('line %04d\n' % i).encode() for i in range(1000)]
        body = b''.join(lines)
        out = cron_notify.truncate(body, 2000)

        assert len(out) <= 2000
        assert out.startswith(b'line 0000\n')
        assert out.endswith(b'line 0999\n')
        assert b'[cron_notify] ' in out and b'bytes omitted (over --max-bytes 2000)' in out
        # Whole lines only: every line in the result is either a marker or an intact input line.
        for line in out.split(b'\n')[:-1]:
            assert line in (l.rstrip(b'\n') for l in lines) or line.startswith(b'[cron_notify] ')
        # Tail heavy: the summary lives at the end.
        marker_at = out.index(b'[cron_notify] ')
        assert len(out) - marker_at > marker_at

    def test_the_omitted_count_is_exact(self):
        body = b''.join(('%03d\n' % i).encode() for i in range(500))
        out = cron_notify.truncate(body, 600)
        m = re.search(rb'^\[cron_notify\] (\d+) bytes omitted \(over --max-bytes 600\)\n', out, re.M)
        assert m, out
        kept = len(out) - (m.end() - m.start())
        assert int(m.group(1)) == len(body) - kept

    def test_the_limit_is_in_bytes_not_characters(self):
        body = ('é' * 10 + '\n').encode('utf-8') * 100   # 21 bytes a line, 11 characters
        out = cron_notify.truncate(body, 500)
        assert len(out) <= 500
        assert out.decode('utf-8')  # cut on line boundaries, so never mid-codepoint

    def test_main_applies_max_bytes_before_the_sink_sees_it(self, child, sink, monkeypatch):
        spec = '|'.join('o:%s' % ('x' * 50) for _ in range(40))   # ~2 KB
        run(child, sink, '--max-bytes', '600', spec=spec, monkeypatch=monkeypatch)
        assert len(sink.stdin()) <= 600
        assert b'bytes omitted' in sink.stdin()


# --- The subject ----------------------------------------------------------------------------------------------

class TestTheSubject:
    """SNS refuses a subject over 100 characters, with non-ASCII, or with a newline - and refuses the whole
    publish with it, so a long job name would silence the channel."""

    def test_is_ascii_one_line_and_at_most_100_chars(self):
        subject = cron_notify.subject_for('café\nqueue' + 'x' * 200, 1, 'host')
        assert len(subject) <= 100
        subject.encode('ascii')
        assert '\n' not in subject

    def test_the_ordinary_case_is_untouched(self):
        assert cron_notify.subject_for('scrape-queue', 0, 'ip-10-0-0-1') == 'scrape-queue: exit 0 on ip-10-0-0-1'


# --- The log ---------------------------------------------------------------------------------------------------

class TestTheLog:
    """stderr goes to the same /dev/null this issue is about, so --log is where a failed publish is visible the
    morning after."""

    def test_one_line_per_run_for_each_outcome(self, child, sink, tmp_path, monkeypatch):
        log = tmp_path / 'notify.log'
        run(child, sink, '--log', str(log), spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        run(child, sink, '--log', str(log), spec='', exit_code=0, monkeypatch=monkeypatch)
        monkeypatch.setenv('SINK_EXIT', '3')
        run(child, sink, '--log', str(log), spec='o:y', exit_code=0, monkeypatch=monkeypatch)

        logged = log.read_text().splitlines()
        assert len(logged) == 3
        assert ' exit 1 published %d bytes' % len(lines('x')) in logged[0]
        assert ' exit 0 nothing to publish' in logged[1]
        assert ' exit 0 sink failed (exit 3)' in logged[2]
        # A wall-clock stamp WITH its offset, the #101 rule for anything a reader will compare across a
        # timezone change.
        from datetime import datetime
        for line in logged:
            stamp = line.split(' ', 1)[0]
            assert datetime.fromisoformat(stamp).utcoffset() is not None, stamp

    def test_no_log_flag_writes_nothing(self, child, sink, tmp_path, monkeypatch):
        run(child, sink, spec='o:x', monkeypatch=monkeypatch)
        assert list(tmp_path.glob('*.log')) == []


# --- SIGTERM ----------------------------------------------------------------------------------------------------

@posix_only
class TestSigterm:
    """The queue turns SIGTERM into sys.exit(143) and, on the way out, stops the city it is supervising and
    prints its summary. A SECOND SIGTERM landing during that unwind interrupts the stop and orphans the city.
    So the wrapper forwards exactly one, however many it receives, and still delivers afterwards.

    POSIX only: Windows has no SIGTERM delivery to forward.
    """

    def test_one_sigterm_reaches_the_child_and_the_record_is_still_delivered(self, child, sink, tmp_path,
                                                                             monkeypatch):
        journal = tmp_path / 'child.journal'
        env = dict(os.environ, CHILD_SPEC='o:started', CHILD_SLEEP='30', CHILD_JOURNAL=str(journal))
        proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO_ROOT, 'cron_notify.py'), '--sink', sink.command,
             '--name', 'job', '--', *child],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (journal.exists() and 'START' in journal.read_text()):
            time.sleep(0.05)
        assert journal.exists(), 'the child never started'

        proc.send_signal(signal.SIGTERM)
        time.sleep(0.15)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)

        assert proc.returncode == 143, err
        assert journal.read_text().count('TERM') == 1, journal.read_text()
        assert sink.calls() == 1
        assert sink.stdin() == lines('started')
        assert sink.env()['NOTIFY_EXIT'] == '143'
        assert out == lines('started')


# --- The CLI surface --------------------------------------------------------------------------------------------

class TestTheCli:

    def test_sink_and_a_command_are_required(self, capsys):
        with pytest.raises(SystemExit) as e:
            cron_notify.main(['--', 'true'])
        assert e.value.code == 2
        with pytest.raises(SystemExit) as e:
            cron_notify.main(['--sink', 'x'])
        assert e.value.code == 2

    @pytest.mark.parametrize('value', ['10', 'lots'])
    def test_max_bytes_must_be_an_integer_with_room_for_the_marker(self, capsys, value):
        with pytest.raises(SystemExit) as e:
            cron_notify.main(['--sink', 'x', '--max-bytes', value, '--', 'true'])
        assert e.value.code == 2

    def test_importing_the_module_has_no_side_effects(self):
        """Same inert-import shape as the runners: nothing runs at import, so this suite can drive main()."""
        result = subprocess.run([sys.executable, '-c', 'import cron_notify; print("imported")'],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0 and result.stdout.strip() == 'imported', result.stderr


# --- The documented probe -----------------------------------------------------------------------------------------

def _probe_argv():
    """The probe recipe from docs/ops.md, "Hearing about a bad night", as argv: everything on its line."""
    with open(os.path.join(REPO_ROOT, 'docs', 'ops.md'), encoding='utf-8') as f:
        text = f.read()
    # Any crontab-shaped line naming both the script and `--name probe`, so flag order is free to change.
    found = [line for line in re.findall(r'^\S+ \S+ \* \* \* [^\n`]*$', text, re.M)
             if 'cron_notify.py' in line and '--name probe' in line]
    assert len(found) == 1, 'expected exactly one probe line in docs/ops.md, found %d' % len(found)
    return shlex.split(found[0])


def _production_line():
    """The production crontab entry from docs/downloader.md, continuations joined, as argv. Matched by what it
    runs (cron_notify.py --name scrape-queue), not by its start time, so rescheduling the night breaks nothing."""
    with open(os.path.join(REPO_ROOT, 'docs', 'downloader.md'), encoding='utf-8') as f:
        text = f.read()
    found = [m.replace('\\\n', ' ') for m in re.findall(r'^\S+ \S+ \* \* \* (.*?)^```', text, re.M | re.S)]
    found = [line for line in found if 'cron_notify.py' in line and '--name scrape-queue' in line]
    assert len(found) == 1, 'expected exactly one production crontab line in docs/downloader.md, found %d' % len(found)
    return shlex.split(found[0])


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def _cron_path(path):
    """A path as cron's shell resolves it: the crontab belongs to `ubuntu`, so `~/x` is `/home/ubuntu/x`."""
    return '/home/ubuntu/' + path[2:] if path.startswith('~/') else path


class TestTheDocumentedProbe:
    """The recipe that proves the channel delivers (#141). Its first version omitted --only-on-failure, so
    `false` - which prints nothing - fell under cron's no-output-no-mail rule and the sink was never called, so
    the probe failed whether or not the channel worked - twice, on 2026-09-19, with the channel suspected.
    Nothing tied the recipe to main(), so these do.
    """

    def test_the_recipe_as_written_calls_the_sink_and_logs_published(self, sink, tmp_path):
        argv = _probe_argv()
        script = next(i for i, a in enumerate(argv) if a.endswith('cron_notify.py'))
        split = argv.index('--')
        assert argv[split + 1:] == ['false'], 'the probe command is supposed to be `false`'
        flags = argv[script + 1:split]
        # The sink and the log are deployment facts; everything else is exactly as documented. `false` becomes
        # its portable equivalent - exit 1, print nothing - since Windows has no `false`.
        flags[flags.index('--sink') + 1] = sink.command
        log = tmp_path / 'probe.log'
        flags[flags.index('--log') + 1] = str(log)

        code = cron_notify.main([*flags, '--', sys.executable, '-c', 'raise SystemExit(1)'])

        assert code == 1
        assert sink.calls() == 1, 'the documented probe never reaches the sink'
        assert sink.env()['NOTIFY_SUBJECT'].startswith('probe: exit 1 on ')
        assert ' exit 1 published ' in log.read_text().splitlines()[-1]

    def test_the_probe_runs_what_the_nightly_runs(self):
        """A throwaway cron line that cannot find the script tests nothing; the interpreter and script are the
        production line's own."""
        argv, prod = _probe_argv(), _production_line()
        script = next(i for i, a in enumerate(prod) if a.endswith('cron_notify.py'))
        assert prod[:script + 1] == argv[argv.index(prod[0]):argv.index(prod[0]) + script + 1]

    def test_the_probe_does_not_write_into_the_nightlys_log(self):
        """The log line carries no job name, and the morning check reads `tail -1` of the nightly's log - so a
        probe's `exit 1 published` there reads as a failed night."""
        assert _cron_path(_flag(_probe_argv(), '--log')) != _cron_path(_flag(_production_line(), '--log'))


# --- The heartbeat gate (#167) -----------------------------------------------------------------------------------

def _heartbeat_gates():
    """Every `find <path> -mmin` gate on the heartbeat, in the production line and in the ops.md runbook."""
    paths = []
    for page in ('downloader.md', 'ops.md'):
        with open(os.path.join(REPO_ROOT, 'docs', page), encoding='utf-8') as f:
            # A real path, not the prose's `find ... -mmin -10`.
            paths += re.findall(r'find ([/~]\S+) -mmin -\d+', f.read())
    return paths


class TestTheHeartbeatGate:
    """The crontab publishes the dead-man heartbeat only if cron_notify.log was written in the last few minutes
    (docs/ops.md, "Hearing about a night that never ran"), so that a wrapper that could not start or died early
    withholds it rather than reporting a healthy night. That makes two things load-bearing that nothing else
    checks: main() logs on EVERY path it returns by, and the gate reads the log the nightly actually writes."""

    def test_every_return_path_appends_exactly_one_log_line(self, child, sink, tmp_path, monkeypatch):
        log = tmp_path / 'notify.log'
        count = [0]

        def one_more(outcome):
            count[0] += 1
            logged = log.read_text().splitlines()
            assert len(logged) == count[0], (outcome, logged)
            assert outcome in logged[-1], (outcome, logged[-1])

        # Production's flags first: a clean night under --only-on-failure is the commonest path of all.
        run(child, sink, '--only-on-failure', '--log', str(log), spec='o:x', exit_code=0, monkeypatch=monkeypatch)
        one_more('exit 0 nothing to publish (clean run, --only-on-failure)')
        run(child, sink, '--only-on-failure', '--log', str(log), spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        one_more('exit 1 published')
        run(child, sink, '--only-on-failure', '--log', str(log), spec='', exit_code=5, monkeypatch=monkeypatch)
        one_more('exit 5 published')
        cron_notify.main(['--sink', sink.command, '--only-on-failure', '--log', str(log), '--',
                          str(tmp_path / 'no-such-program')])
        one_more('exit 127 published')
        monkeypatch.setenv('SINK_EXIT', '3')
        run(child, sink, '--only-on-failure', '--log', str(log), spec='o:x', exit_code=1, monkeypatch=monkeypatch)
        one_more('exit 1 sink failed (exit 3)')
        monkeypatch.delenv('SINK_EXIT')
        # And cron's default rule, for the path production does not take.
        run(child, sink, '--log', str(log), spec='', exit_code=0, monkeypatch=monkeypatch)
        one_more('exit 0 nothing to publish')
        # The sink-failure return under cron's default rule: COMMAND exited 0, so the wrapper's own exit 4.
        monkeypatch.setenv('SINK_EXIT', '3')
        run(child, sink, '--log', str(log), spec='o:x', exit_code=0, monkeypatch=monkeypatch)
        one_more('exit 0 sink failed (exit 3)')
        monkeypatch.delenv('SINK_EXIT')

    def test_the_gate_reads_the_log_the_nightly_writes(self):
        gates = _heartbeat_gates()
        assert len(gates) == 2, 'expected the gate once in downloader.md and once in ops.md, found %r' % gates
        nightly = _cron_path(_flag(_production_line(), '--log'))
        assert all(_cron_path(g) == nightly for g in gates), (gates, nightly)
