# !/usr/bin/python3
"""Run the nightly scrape as one serialised queue instead of 53 hardcoded crontab slots (#101).

The fleet used to be one crontab line per city, staggered every 15-30 minutes across the whole UTC day.
Nothing was broken, but the ring had wrapped: 32 of 53 cities ran between 07:00 and 19:00 Pacific, which is
the working day on the hosts the runs actually load - the pano store and the SidewalkWebpage app servers.
Because each slot was picked by hand at onboarding, the ring also developed gaps and could develop
collisions; and a fixed UTC crontab drifts an hour against Seattle twice a year.

This driver walks a city list in order and starts the next city as soon as the previous one exits, which:

  - serialises by construction. The stagger existed to keep two cities off /adminapi/panos and the store at
    once; a queue enforces that without anyone maintaining 53 slot numbers.
  - has one start time to pin to a timezone (CRON_TZ=America/Los_Angeles - see docs/downloader.md), so the
    whole fleet moves with DST instead of drifting against it.
  - packs the ring into the idle hours. The measured fleet total is ~16 minutes of real work per day, so
    the queue finishes long before the window closes on an ordinary night.
  - cannot overlap itself. There was no lock anywhere in this repo before: a slow run and the next slot
    could put two processes on one city's pano_id_log.csv, log.csv and scrape.log at the same time.

Deliberately NOT parallel. The politeness constraint the stagger encoded is real, and the whole point here
is that exactly one city is talking to the APIs and the store at any moment.

Usage:
  python3 scrape_queue.py --cities <manifest.csv> --store-root <dir> [options] -- [runner args...]

See docs/downloader.md, "Nightly deployment".
"""

import argparse
import csv
import importlib
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime


# Seconds to wait after asking a city to stop before killing it outright. DownloadRunner translates SIGTERM
# into sys.exit(143) so its finally blocks run and this run's log.csv evidence row still lands (#49); that
# needs long enough to finish the pano in flight and append one line over sshfs, not long enough to matter.
TERM_TO_KILL_SECONDS = 30

# Grace added to a city's own --max-runtime before the queue stops waiting for it. The runner's budget stops
# it STARTING new panos, so it can legitimately overrun by one pano's download plus the log write; this is
# the margin for that, not a second budget. A city that blows through it is hung, and holds up every city
# behind it until it is killed.
DEFAULT_KILL_GRACE_MINUTES = 5.0

# Manifest columns. fqdn is the SidewalkWebpage host the pano list comes from; city_id is both the store
# subdirectory and the name used in every report, so the two must be carried together and never derived from
# each other (columbus-oh lives at sidewalk-columbus.cs.washington.edu - there is no rule to apply).
REQUIRED_CITY_COLUMNS = ('city_id', 'fqdn')

# Lock file name, in the system temp directory. See --lock for why it is local disk and not the store.
_DEFAULT_LOCK_NAME = 'sidewalk-scrape-queue.lock'

City = namedtuple('City', 'city_id fqdn')

# outcome is one of: 'ok', 'failed', 'timed_out', 'skipped_deadline'. exit_code and seconds are None for a
# city that never started.
CityResult = namedtuple('CityResult', 'city_id outcome exit_code seconds')


class QueueLocked(Exception):
    """Another queue run holds the lock. Raised rather than returned so no caller can ignore it."""


def _positive_minutes(value):
    """argparse type= for the two budgets: a finite, strictly positive float.

    DownloadRunner's own _reservation_minutes exists for the same reason (#52): a budget that is nan, inf or
    negative does not fail, it misbehaves quietly. Here a zero or negative queue budget would skip every city
    while exiting like a completed run, which is indistinguishable from a healthy quiet night in log.csv.
    """
    try:
        minutes = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid float value: %r" % (value,))
    if not (minutes > 0) or minutes == float('inf'):
        raise argparse.ArgumentTypeError("must be a finite, positive number of minutes: %r" % (value,))
    return minutes


def build_parser():
    parser = argparse.ArgumentParser(
        description='Run the nightly pano scrape for every city in a manifest, one at a time.',
        epilog='Arguments after -- are passed through to every DownloadRunner invocation, e.g. '
               '`-- --all-panos --skip-depth`.')
    parser.add_argument('--cities', required=True, metavar='CSV',
                        help='Manifest of cities to run: a CSV with city_id and fqdn columns. Deliberately '
                             'has no default - which cities a host scrapes and which servers they come from '
                             'are deployment facts, and a wrong default would quietly scrape the wrong '
                             'fleet. A row whose city_id starts with # is skipped, so a city can be taken '
                             'out for a night the way a crontab line used to be commented out.')
    parser.add_argument('--store-root', required=True, metavar='DIR',
                        help='Root of the pano store. Each city is scraped into <DIR>/<city_id>.')
    parser.add_argument('--max-runtime', type=_positive_minutes, default=None, metavar='MINUTES',
                        help='Stop STARTING new cities once this many minutes have elapsed. The queue window '
                             '- size it to the night, not to the work. Cities not reached are reported and '
                             'make the run exit nonzero, so a fleet that stops completing is visible.')
    parser.add_argument('--city-max-runtime', type=_positive_minutes, default=None, metavar='MINUTES',
                        help="Passed to each city as DownloadRunner's own --max-runtime, and enforced with a "
                             'hard kill %g minutes later. Without it one slow city can hold the whole queue '
                             'past the window, so the production line should always set it.'
                             % (DEFAULT_KILL_GRACE_MINUTES,))
    parser.add_argument('--kill-grace', type=_positive_minutes, default=DEFAULT_KILL_GRACE_MINUTES,
                        metavar='MINUTES',
                        help='How long past its own budget a city may run before the queue stops waiting and '
                             'kills it (default %(default)g). Only meaningful with --city-max-runtime.')
    parser.add_argument('--only', action='append', default=None, metavar='CITY_ID',
                        help='Run only this city (repeatable). For re-running one city through the same '
                             'machinery - the lock, the budgets, the summary - rather than by hand.')
    parser.add_argument('--no-rotate', action='store_true',
                        help='Keep the manifest order every night instead of rotating the starting point. '
                             'Rotation only matters on a night the window truncates the queue: it moves '
                             'which cities lose rather than always losing the same tail.')
    parser.add_argument('--lock', default=None, metavar='PATH',
                        help='Lock file guaranteeing one queue run at a time (default: %s in the system temp '
                             'directory). It defaults to LOCAL disk, not the store: the store is a network '
                             'mount whose advisory-lock semantics are not guaranteed, and the overlap being '
                             'prevented is between runs on this host.' % (_DEFAULT_LOCK_NAME,))
    parser.add_argument('--python', default=None, metavar='EXE',
                        help='Interpreter to run DownloadRunner with (default: this one, %s).' % sys.executable)
    parser.add_argument('--runner', default=None, metavar='PATH',
                        help='DownloadRunner.py to run (default: the one beside this script).')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print the order the queue would run in, and the exact command for each city, '
                             'without running anything or taking the lock.')
    parser.add_argument('runner_args', nargs='*', metavar='-- RUNNER ARGS',
                        help='Passed through to every city, after --.')
    return parser


def default_lock_path():
    return os.path.join(tempfile.gettempdir(), _DEFAULT_LOCK_NAME)


def read_city_list(path):
    """Parse the manifest into City records, preserving file order.

    Fails loudly on a missing column rather than reading every row's city_id as blank: this is the one input
    that decides which 50-odd cities get scraped tonight, and the failure mode of a quiet misparse is a fleet
    that silently stops. Same reasoning as fetch_pano_ids_csv's header guard (#72).

    utf-8-sig because a manifest edited in Excel carries a BOM, which would otherwise glue itself to the
    first fieldname and fire the guard on a perfectly good file.
    """
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_CITY_COLUMNS if not reader.fieldnames or c not in reader.fieldnames]
        if missing:
            raise ValueError("%s is missing required column(s) %s; found %r"
                             % (path, ', '.join(missing), reader.fieldnames))
        cities, seen = [], set()
        for row in reader:
            city_id = (row.get('city_id') or '').strip()
            fqdn = (row.get('fqdn') or '').strip()
            # '#' disables a row, replacing the "comment the crontab line out" affordance the queue removes.
            if not city_id or city_id.startswith('#'):
                continue
            if not fqdn:
                raise ValueError("%s: city %r has no fqdn" % (path, city_id))
            # A duplicate would scrape one city twice in a night and, with two processes never overlapping,
            # look exactly like a healthy run in every log. Cheaper to refuse than to explain later.
            if city_id in seen:
                raise ValueError("%s: city %r appears more than once" % (path, city_id))
            seen.add(city_id)
            cities.append(City(city_id, fqdn))
    if not cities:
        raise ValueError("%s lists no cities" % (path,))
    return cities


def plan_order(cities, only=None, rotation_ordinal=None):
    """The order tonight's queue runs in: the manifest order, optionally narrowed and rotated.

    Rotation matters only when the window truncates the queue, which is exactly the night it matters most:
    without it the same tail cities are dropped every time, indefinitely and invisibly, and they are the ones
    nobody is watching. Keyed on the date ordinal so it is deterministic - two runs on one day agree, and an
    operator can reproduce any night's order - and so that over N days every city leads once.

    --only skips rotation entirely, so a single-city re-run is the same command whatever day it is
    run on - the operator asked for those cities, in that order.
    """
    if only:
        wanted = list(dict.fromkeys(only))  # de-duplicated, caller's order preserved
        by_id = {c.city_id: c for c in cities}
        unknown = [c for c in wanted if c not in by_id]
        if unknown:
            raise ValueError("not in the manifest: %s" % ', '.join(unknown))
        return [by_id[c] for c in wanted]
    if rotation_ordinal is None or not cities:
        return list(cities)
    offset = rotation_ordinal % len(cities)
    return list(cities[offset:]) + list(cities[:offset])


def _lock_module():
    """The platform's advisory-lock module: fcntl on POSIX, msvcrt on Windows.

    Imported by name at call time rather than with a try/except ImportError pair at module scope, so the
    module has no line that is dead on the platform it is running on - and so a test can substitute a
    module-shaped object and exercise the arm of _try_lock that this platform never takes.

    Both APIs release the lock when the holding process dies, which is the property the whole design rests
    on: a lock that outlived a crash would silently stop the entire fleet, which is a worse failure than the
    overlap it prevents. An O_EXCL lock file - the obvious first implementation - has exactly that defect.
    """
    return importlib.import_module('fcntl' if os.name == 'posix' else 'msvcrt')


def _try_lock(fd):
    """Take an exclusive advisory lock on fd without blocking. Raise QueueLocked if someone else holds it."""
    lock_api = _lock_module()
    try:
        if hasattr(lock_api, 'flock'):
            lock_api.flock(fd, lock_api.LOCK_EX | lock_api.LOCK_NB)
        else:
            # One byte at offset 0. Windows allows locking a region past EOF, so this works on the empty
            # file a first run creates.
            os.lseek(fd, 0, os.SEEK_SET)
            lock_api.locking(fd, lock_api.LK_NBLCK, 1)
    except OSError as e:
        raise QueueLocked(str(e)) from e


@contextmanager
def exclusive_lock(path):
    """Hold the queue lock for the duration of the block, or raise QueueLocked.

    The lock file is never unlinked, deliberately: unlinking races - another process can already have opened
    the same path and be holding a lock on what is now an orphaned inode, so both would believe they hold it.
    It is left behind holding the pid, which costs nothing and tells whoever finds it who to look for.
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
        # Closing releases the lock under both APIs. Nothing else is needed, and nothing here may raise on
        # the way out and mask the queue's own exception.
        os.close(fd)


def configure_logging(log_path):
    """Send the queue's own narrative to a rotating log beside the per-city stores.

    Same reasoning as DownloadRunner's scrape.log (#49): under cron the CWD is wherever the process happened
    to start, which is nowhere anyone looks. This log answers "what ran last night, in what order, and how
    long did each city take" - a question the per-city logs cannot, because none of them can see the ring.
    A failure to open it is a warning, not a fatal: the log is evidence, not cargo.
    """
    try:
        handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=3)
        fallback_error = None
    except OSError as e:
        handler = logging.StreamHandler()
        fallback_error = e
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if fallback_error is not None:
        logging.warning("Could not open %s (%s); logging to stderr for this run", log_path, fallback_error)


def strip_separator(runner_args):
    """Drop a leading '--' from the pass-through arguments.

    argparse already consumes the separator it uses to end its own options, so on the Pythons this runs on
    the list arrives without one - but that handling has changed across 3.x releases, and a stray '--'
    reaching DownloadRunner is an argparse error that fails the city rather than the flag. Kept as a named
    function so the behaviour is pinned by a test instead of resting on a detail of the stdlib.
    """
    return runner_args[1:] if runner_args and runner_args[0] == '--' else runner_args


def build_command(city, store_root, python_exe, runner_path, city_budget_minutes, runner_args):
    """The exact argv for one city.

    The city's budget is passed as DownloadRunner's own --max-runtime rather than enforced only by killing
    it: a runner that stops itself writes its log.csv row and leaves a clean ledger, where a kill leaves the
    row to the SIGTERM handler and anything in flight unfinished. The kill is the backstop, not the mechanism.
    """
    cmd = [python_exe, runner_path, city.fqdn, os.path.join(store_root, city.city_id)]
    if city_budget_minutes is not None:
        cmd += ['--max-runtime', '%g' % city_budget_minutes]
    return cmd + list(runner_args)


def _city_budget(city_max_runtime, remaining_minutes):
    """What to give one city: its own cap, further clamped by what is left of the queue window.

    Composing the two is the point. Passing the city cap alone lets the last city of the night run an hour
    past the window; passing the remaining window alone lets the FIRST city eat the whole night, which is the
    head-of-line problem a serialised queue would otherwise introduce.
    """
    budgets = [b for b in (city_max_runtime, remaining_minutes) if b is not None]
    return min(budgets) if budgets else None


def stop_process(proc, city_id):
    """Ask a running city to stop, then insist. Returns its exit code.

    SIGTERM first (terminate() is SIGTERM on POSIX), because DownloadRunner translates it into
    sys.exit(143) so its finally blocks run and this run's log.csv evidence row still lands (#49). Killing
    outright would trade a few seconds' wait for a missing row on the one night someone wants it.

    But asking has to have a deadline, or a wedged process holds every city behind it for the rest of the
    night exactly as if nothing had been sent - so SIGKILL follows TERM_TO_KILL_SECONDS later.
    """
    proc.terminate()
    try:
        return proc.wait(timeout=TERM_TO_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        logging.error("%s: did not exit %s s after SIGTERM; killing", city_id, TERM_TO_KILL_SECONDS)
        print("[queue] %s: did not stop; killing" % (city_id,))
        proc.kill()
        return proc.wait()


def run_city(city, store_root, python_exe, runner_path, city_budget_minutes, kill_grace_minutes, runner_args,
             env=None):
    """Run one city to completion and return a CityResult. Never raises for the city's own failure.

    A city that crashes, hangs or exits nonzero is one recorded result and the queue moves on: the fleet's
    availability must not depend on its worst member, which is the same reason nothing in the cropper's crop
    loop is fatal (#48). The result is what makes it visible.
    """
    cmd = build_command(city, store_root, python_exe, runner_path, city_budget_minutes, runner_args)
    hard_timeout = None if city_budget_minutes is None else (city_budget_minutes + kill_grace_minutes) * 60.0

    print("[queue] %s: starting %s" % (city.city_id, ' '.join(cmd)))
    logging.info("%s: starting: %s", city.city_id, ' '.join(cmd))
    started = time.monotonic()
    outcome = 'ok'
    try:
        proc = subprocess.Popen(cmd, env=env)
    except OSError as e:
        # A missing interpreter or runner path is a property of the deployment, not of this city - but it
        # would fail identically for all 53, so report it per city and let the summary make the pattern
        # obvious rather than dying on the first one.
        elapsed = time.monotonic() - started
        logging.error("%s: could not start (%s)", city.city_id, e)
        print("[queue] %s: FAILED to start (%s)" % (city.city_id, e))
        return CityResult(city.city_id, 'failed', None, elapsed)

    try:
        exit_code = proc.wait(timeout=hard_timeout)
    except subprocess.TimeoutExpired:
        outcome = 'timed_out'
        logging.error("%s: still running %.1f min past its budget; stopping it", city.city_id,
                      kill_grace_minutes)
        print("[queue] %s: TIMED OUT after %.1f min; stopping it" % (city.city_id, hard_timeout / 60.0))
        exit_code = stop_process(proc, city.city_id)
    except BaseException:
        # The QUEUE is being stopped - a cron timeout wrapper's SIGTERM, an operator's kill, Ctrl-C - rather
        # than this city misbehaving. Take the city with it. An orphaned DownloadRunner keeps scraping into
        # the store with nothing supervising it, and the queue lock it was running under is released the
        # instant we die, so tomorrow's queue starts alongside it: the exact overlap the lock exists to
        # prevent, arrived at through the one door the lock cannot watch.
        logging.error("%s: the queue is stopping; stopping the city too", city.city_id)
        print("[queue] %s: queue stopping; stopping the city too" % (city.city_id,))
        stop_process(proc, city.city_id)
        raise

    elapsed = time.monotonic() - started
    if outcome != 'timed_out':
        outcome = 'ok' if exit_code == 0 else 'failed'
    level = logging.INFO if outcome == 'ok' else logging.ERROR
    logging.log(level, "%s: %s (exit %s) in %.1f min", city.city_id, outcome, exit_code, elapsed / 60.0)
    print("[queue] %s: %s (exit %s) in %.1f min" % (city.city_id, outcome, exit_code, elapsed / 60.0))
    return CityResult(city.city_id, outcome, exit_code, elapsed)


def run_queue(cities, store_root, python_exe, runner_path, runner_args, max_runtime_minutes=None,
              city_max_runtime=None, kill_grace_minutes=DEFAULT_KILL_GRACE_MINUTES, env=None,
              run_one=run_city):
    """Run every city in order, stopping only when the window closes. Returns one CityResult per city.

    The window gates STARTING a city, never interrupts one that is already running - the same rule as the
    image phase's budget (#51), and for the same reason: a partial pano or a torn ledger costs more than
    finishing five minutes late. Elapsed time is measured with time.monotonic() so an NTP step or a DST
    transition cannot stretch or shrink the night.
    """
    started = time.monotonic()
    results = []
    for index, city in enumerate(cities):
        remaining = None
        if max_runtime_minutes is not None:
            remaining = max_runtime_minutes - (time.monotonic() - started) / 60.0
            if remaining <= 0:
                skipped = [c.city_id for c in cities[index:]]
                logging.warning("window of %.1f min spent; %d cities not reached: %s",
                                max_runtime_minutes, len(skipped), ', '.join(skipped))
                print("[queue] WARNING: window of %.1f min spent after %d of %d cities; not reached: %s"
                      % (max_runtime_minutes, index, len(cities), ', '.join(skipped)))
                results += [CityResult(c, 'skipped_deadline', None, None) for c in cities[index:]]
                break
        results.append(run_one(city, store_root, python_exe, runner_path,
                               _city_budget(city_max_runtime, remaining), kill_grace_minutes, runner_args,
                               env=env))
    return results


def summarise(results, elapsed_minutes):
    """The run's one-screen report: a line per city that did not simply work, then the totals.

    Every city gets a line in the queue log, but stdout is what cron mails, so it leads with what went wrong.
    A clean night is four lines; a bad one names every city and why.
    """
    by_outcome = {}
    for r in results:
        by_outcome.setdefault(r.outcome, []).append(r)
    lines = ["", "[queue] ==== summary ===="]
    for outcome in ('failed', 'timed_out', 'skipped_deadline'):
        for r in by_outcome.get(outcome, []):
            when = '' if r.seconds is None else ' after %.1f min' % (r.seconds / 60.0)
            code = '' if r.exit_code is None else ' (exit %d)' % r.exit_code
            lines.append("[queue] %-24s %s%s%s" % (r.city_id, outcome.upper(), code, when))
    ok = len(by_outcome.get('ok', []))
    lines.append("[queue] %d/%d cities ok, %d failed, %d timed out, %d not reached; %.1f min total"
                 % (ok, len(results), len(by_outcome.get('failed', [])),
                    len(by_outcome.get('timed_out', [])), len(by_outcome.get('skipped_deadline', [])),
                    elapsed_minutes))
    return '\n'.join(lines)


def exit_code_for(results):
    """0 only when every city ran and succeeded.

    A city that was never reached counts as a failure on purpose. cron mails a nonzero exit, and a fleet
    quietly completing 40 of 53 cities every night - which is exactly what an un-monitored window produces -
    is the condition this whole change exists to make visible. If a night's truncation is expected and
    accepted, the window is the wrong size.
    """
    return 0 if all(r.outcome == 'ok' for r in results) else 1


def main(argv=None):
    """Parse argv, take the lock, run the queue, print the summary; return the process exit code.

    Returns rather than calling sys.exit so the whole flow can be driven in-process by a test, the shape
    analyze.py and CropRunner already use. Exit codes: 0 all cities ok, 1 something did not run or failed,
    2 usage (argparse), 3 another queue run holds the lock.
    """
    args = build_parser().parse_args(argv)

    runner_args = strip_separator(list(args.runner_args))

    python_exe = args.python or sys.executable
    runner_path = args.runner or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'DownloadRunner.py')

    try:
        cities = read_city_list(args.cities)
        ordered = plan_order(cities, only=args.only,
                             rotation_ordinal=None if args.no_rotate else datetime.now().toordinal())
    except (OSError, ValueError) as e:
        print("Could not read the city list: %s" % (e,), file=sys.stderr)
        return 2

    if args.dry_run:
        # The budget shown is the FIRST city's - its own cap clamped by the whole window. Later cities get
        # whatever the window has left by the time they start, which a plan printed before anything runs
        # cannot know. Said here rather than left for someone to discover from a mismatched log line.
        budget = _city_budget(args.city_max_runtime, args.max_runtime)
        print("Would run %d cities into %s (budgets shown are the first city's):"
              % (len(ordered), args.store_root))
        for i, city in enumerate(ordered, 1):
            print("  %2d. %s" % (i, ' '.join(build_command(city, args.store_root, python_exe, runner_path,
                                                           budget, runner_args))))
        return 0

    # Same warning discipline as DownloadRunner's --min-depth-runtime check: tell the operator when the
    # combination they typed cannot do what it looks like it does. A window with no per-city cap is not an
    # error, but it does mean the window is advisory - one hung city holds it open indefinitely.
    if args.max_runtime is not None and args.city_max_runtime is None:
        print("WARNING: --max-runtime without --city-max-runtime; one slow city can hold the queue open "
              "past the window, because the window only gates STARTING a city.")

    os.makedirs(args.store_root, exist_ok=True)
    configure_logging(os.path.join(args.store_root, 'scrape_queue.log'))
    # CPython dies from SIGTERM without running finally blocks. Translating it into SystemExit means the
    # stop unwinds properly: run_city stops the city it is supervising instead of orphaning it, and the lock
    # is released on the way out. Same reasoning, and the same 128+15 code, as DownloadRunner's own (#49).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    started = time.monotonic()
    lock_path = args.lock or default_lock_path()
    try:
        with exclusive_lock(lock_path):
            # Announced only once the lock is held, so a run that is about to be refused never prints a
            # start line that reads like a fleet beginning to scrape.
            logging.info("queue starting: %d cities, window %s min, per-city %s min",
                         len(ordered), args.max_runtime, args.city_max_runtime)
            print("[queue] %d cities, window %s min, per-city cap %s min"
                  % (len(ordered), args.max_runtime, args.city_max_runtime))
            results = run_queue(ordered, args.store_root, python_exe, runner_path, runner_args,
                                max_runtime_minutes=args.max_runtime,
                                city_max_runtime=args.city_max_runtime,
                                kill_grace_minutes=args.kill_grace)
    except QueueLocked as e:
        # Loud, and nonzero: the previous night's queue still running when tonight's starts is the exact
        # condition 53 unsynchronised crontab slots could not detect.
        logging.error("another queue run holds %s (%s); exiting without running anything", lock_path, e)
        print("ERROR: another scrape queue is already running (lock %s: %s). Nothing was run."
              % (lock_path, e), file=sys.stderr)
        return 3

    summary = summarise(results, (time.monotonic() - started) / 60.0)
    print(summary)
    for line in summary.splitlines():
        if line.strip():
            logging.info(line.replace('[queue] ', ''))
    return exit_code_for(results)


if __name__ == '__main__':
    sys.exit(main())
