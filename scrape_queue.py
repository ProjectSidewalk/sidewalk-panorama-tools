# !/usr/bin/python3
"""Run the nightly scrape as one serialised queue instead of 53 hardcoded crontab slots (#101).

The fleet used to be one crontab line per city, staggered every 15-30 minutes across the whole UTC day.
Nothing was broken, but the ring had wrapped: 32 of 53 cities ran between 07:00 and 19:00 Pacific, which is
the working day on the hosts the runs actually load - the pano store and the SidewalkWebpage app servers.
Because each slot was picked by hand at onboarding, the ring also developed gaps and could develop
collisions; and a fixed UTC crontab drifts an hour against Seattle twice a year.

This driver walks a city list in order and starts the next city as soon as the previous one exits - and then,
while a full slot of the window remains, runs the cities that stopped on their budget again (#43), which:

  - serialises by construction. The stagger existed to keep two cities off /adminapi/panos and the store at
    once; a queue enforces that without anyone maintaining 53 slot numbers.
  - has one start time to pin to a timezone (the box's, set with timedatectl - NOT CRON_TZ, which Ubuntu's
    cron ignores; see docs/downloader.md), so the whole fleet moves with DST instead of drifting against it.
  - packs the ring into the idle hours. The measured fleet total is ~16 minutes of real work per day, so
    the queue finishes long before the window closes on an ordinary night.
  - cannot overlap itself. There was no lock anywhere in this repo before: a slow run and the next slot
    could put two processes on one city's pano_id_log.csv, log.csv and scrape.log at the same time.
  - spends the whole window. Measured after the depth backfill's first three nights: 477 of 690 minutes
    used, 13 cities already complete and exiting in seconds, and every city with a backlog capped at its
    12-minute slot - so the five largest were 240-463 nights out while a third of every night went unused.
    Pass 1 still gives every city its guaranteed slot; the passes after it give the leftover to whoever ran
    out of budget, in equal shares no smaller than a slot.
  - notices a launched city nobody added (#130). The manifest is the deployment fact and stays explicit,
    but a city it does not name does not exist to the queue, which ran green for six nights while two new
    cities went unscraped. So once a night, after the fleet, it asks one manifest host for /v3/api/cities
    and names every city that has no row, private ones included (#143) - on stdout and in the exit code.

Deliberately NOT parallel. The politeness constraint the stagger encoded is real, and the whole point here
is that exactly one city is talking to the APIs and the store at any moment.

Usage:
  python3 scrape_queue.py --cities <manifest.csv> --store-root <dir> [options] -- [runner args...]

See docs/downloader.md, "Nightly deployment".
"""

import argparse
import csv
import http.client
import importlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit


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
# city that never started. budget_minutes is what the city was given (None when there was no budget),
# pass_number which pass of the night ran it (#43), and stop_reasons the run summary the runner wrote
# (None when it wrote none); all three default so a four-field construction still works.
CityResult = namedtuple('CityResult',
                        'city_id outcome exit_code seconds budget_minutes pass_number stop_reasons',
                        defaults=(None, 1, None))

# A city the fleet serves that the manifest does not name (#130, private cities too since #143). fqdn is the
# host its roster url names (None when the roster publishes no url - every private city, never guessed);
# misnamed_as is the manifest city_id of a row that points at that same host under another name, so the
# operator is told why the city they "already added" is missing; visibility is the roster's word for it.
Unlisted = namedtuple('Unlisted', 'city_id fqdn misnamed_as visibility', defaults=('public',))

# The outcome of one night's cross-check. roster_host is the manifest host whose roster was used, or None when
# none of the hosts tried answered with one; attempts is [(fqdn, reason)] for each host that did not.
ManifestCheck = namedtuple('ManifestCheck',
                           'roster_host public_count unlisted attempts hosts_total private_count', defaults=(0,))

# What a '#' row's second column has to look like to credit a city whose roster url gives no host (every private
# city, whose url is null) so there is no host to match: a bare hostname - at least one dot, nothing but label characters. A prose comment split on
# its commas (` bayonne-fr launched 2026-09-11`) fails it, and so does an empty column.
_HOSTNAME_SHAPE = re.compile(r'^[a-z0-9-]+(\.[a-z0-9-]+)+$')

# The one stop reason that means "more time would have helped". It is DownloadRunner's own vocabulary -
# downloaders.gsv.DEPTH_STOP_MAX_RUNTIME and the image phase's matching string - repeated here rather than
# imported, because importing downloaders.gsv would pull aiohttp and streetlevel into a driver that never
# touches either. A test pins the two together, so a rename in the runner fails CI instead of silently
# turning every city into "finished" and leaving the window unspent.
STOP_MAX_RUNTIME = 'max-runtime'

# Name of the per-run summary file the queue asks each city to write, inside a per-run temp directory.
_RUN_SUMMARY_NAME = 'run_summary.json'

# The fleet roster (#130): every deployment serves this list of every city - city_id, url, visibility - and it
# is the same list from every host. The manifest is compared against it once a night, after the fleet has run.
ROSTER_PATH = '/v3/api/cities'
# Hosts to try before giving up. Bounds the check at ROSTER_MAX_HOSTS x ROSTER_TIMEOUT_SECONDS when the whole
# network is down; the answer would be "not cross-checked" whatever the tenth host said.
ROSTER_MAX_HOSTS = 3
ROSTER_TIMEOUT_SECONDS = 30.0
# The roster measured 2026-09-17 is 19 KB for 59 cities; this is the most that will be read of anything a host
# sends back, so a redirect to something large cannot hold the queue open past its window.
ROSTER_MAX_BYTES = 4 * 1024 * 1024
ROSTER_USER_AGENT = ('sidewalk-panorama-tools scrape_queue '
                     '(+https://github.com/ProjectSidewalk/sidewalk-panorama-tools)')


class QueueLocked(Exception):
    """Another queue run holds the lock. Raised rather than returned so no caller can ignore it."""


class RosterUnavailable(Exception):
    """A host did not serve a city roster: unreachable, refused, or a body that is not one. Never fatal on
    its own - the next manifest host is tried - but named, so the report can say what each host did."""


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
    parser.add_argument('--single-pass', action='store_true',
                        help='Run every city once and stop, leaving the rest of the window unused. By '
                             'default, once every city has had its slot, the cities that ran out of budget '
                             'are run again while a full slot of window remains, each with the larger of a '
                             'slot and an equal share of what is left. Needs both --max-runtime and '
                             '--city-max-runtime; --only implies this flag.')
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


def read_city_list(path, disabled=None):
    """Parse the manifest into City records, preserving file order.

    Fails loudly on a missing column rather than reading every row's city_id as blank: this is the one input
    that decides which 50-odd cities get scraped tonight, and the failure mode of a quiet misparse is a fleet
    that silently stops. Same reasoning as fetch_pano_ids_csv's header guard (#72).

    utf-8-sig because a manifest edited in Excel carries a BOM, which would otherwise glue itself to the
    first fieldname and fire the guard on a perfectly good file.

    `disabled`, when given, is a dict the '#'-prefixed rows are recorded into as city_id -> fqdn (the fqdn
    lowercased; '#' and whitespace stripped from the id, which is otherwise kept exactly - it is a directory
    name), an out-parameter like run_queue's `results`. The cross-check
    (#130) reads it so a city taken out for a night counts as decided rather than missing. It is a dict and
    not a set because csv splits a prose comment on its commas too: `# laurens-ia, bayonne-fr launched
    2026-09-11` reads as city_id '# laurens-ia', and crediting that id alone would silence the very city the
    check exists for - so a disabled row is credited only when its fqdn still names the city's host.
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
                disabled_id = city_id.lstrip('#').strip()
                if disabled is not None and disabled_id:
                    disabled[disabled_id] = fqdn.lower()
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


def build_command(city, store_root, python_exe, runner_path, city_budget_minutes, runner_args,
                  run_summary_path=None):
    """The exact argv for one city.

    The city's budget is passed as DownloadRunner's own --max-runtime rather than enforced only by killing
    it: a runner that stops itself writes its log.csv row and leaves a clean ledger, where a kill leaves the
    row to the SIGTERM handler and anything in flight unfinished. The kill is the backstop, not the mechanism.

    run_summary_path asks the runner to say why each phase stopped, which is how the extra passes tell a
    city with a backlog from one that merely started slowly (#43). It goes BEFORE runner_args so an operator
    passing their own --run-summary-file through `--` still wins on argparse's last-one-wins.
    """
    cmd = [python_exe, runner_path, city.fqdn, os.path.join(store_root, city.city_id)]
    if city_budget_minutes is not None:
        cmd += ['--max-runtime', '%g' % city_budget_minutes]
    if run_summary_path is not None:
        cmd += ['--run-summary-file', run_summary_path]
    return cmd + list(runner_args)


def read_run_summary(path):
    """The stop reasons a city reported, or None if it reported nothing usable.

    None means "fall back to the elapsed-time rule", so every ambiguous case resolves to it rather than to a
    confident wrong answer: no file, unreadable, not JSON, not an object, or an object naming neither
    phase. The last of those matters - an empty object is a summary that says nothing, not a summary that
    says "nothing stopped me".

    "No file" is narrower than it looks. A DownloadRunner from before the flag existed does not silently
    write nothing - argparse refuses the unrecognised argument and the city exits 2, so it is booked as
    failed and never re-run; and a runner killed before its finally ran exits nonzero for the same result.
    What actually arrives here with an 'ok' run and no summary is a summary the runner could not WRITE (it
    warns on stdout when that happens), or an operator's own --run-summary-file after `--`, which wins.

    Both keys are always present in what comes back, so callers never have to distinguish a missing key from
    a null one.
    """
    try:
        with open(path) as f:
            reported = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(reported, dict) or not any(k in reported for k in ('image_stop', 'depth_stop')):
        return None
    return {key: reported.get(key) for key in ('image_stop', 'depth_stop')}


def _city_budget(city_max_runtime, remaining_minutes):
    """What to give one city in pass 1: its own cap, further clamped by what is left of the queue window.

    Composing the two is the point. Passing the city cap alone lets the last city of the night run an hour
    past the window; passing the remaining window alone lets the FIRST city eat the whole night, which is the
    head-of-line problem a serialised queue would otherwise introduce.
    """
    budgets = [b for b in (city_max_runtime, remaining_minutes) if b is not None]
    return min(budgets) if budgets else None


def _extra_pass_budget(city_max_runtime, remaining_minutes, cities_left):
    """What to give one city in a pass after the first: an equal share of what is left, never below a slot.

    The floor is load-bearing (#43). 213 minutes left for 39 working cities is 5.5 minutes each, which is
    below the production line's --min-depth-runtime and so zeroes every image phase and mails a WARNING per
    city; and a fraction of a minute can kill a city inside its pano-list fetch, which runs before the
    runner's own budget clock starts. So the share is floored at the slot, and run_queue starts nobody once
    a slot no longer fits. Five cities left with 625 minutes get 125 each, in one process each.
    """
    return max(city_max_runtime, remaining_minutes / cities_left)


# The runner flag whose value is a RESERVATION out of --max-runtime, so it only means the same thing after
# the budget is rewritten if it is rewritten too (#43).
_RESERVATION_FLAG = '--min-depth-runtime'


def scale_depth_reservation(runner_args, budget_minutes, slot_minutes):
    """Rewrite a pass-through --min-depth-runtime in proportion to an enlarged budget.

    --min-depth-runtime is a share of --max-runtime, not an absolute quantity of depth work: the image phase
    stops at `max_runtime - min_depth_runtime` so the tail belongs to depth. The queue rewrites --max-runtime
    for every extra pass, so passing the reservation through unchanged silently changes what it means. The
    production line is `--city-max-runtime 12 -- --min-depth-runtime 6`, an even split; an endgame pass
    handing one city 120 minutes would leave images 114 of them and depth the same 6 it had in pass 1 - the
    exact opposite of what the passes exist for, since the backfill they were built for is the depth one.

    Only ever scales UP, and only when the budget exceeds the slot. Pass 1's last city can be clamped BELOW
    the slot by what is left of the window, and enlarging a reservation towards that smaller budget is how
    you get `--min-depth-runtime >= --max-runtime` and a run that downloads no images at all.

    A value the runner would reject is passed through untouched: argument validation is the runner's job,
    and a queue that raised here would take the whole fleet down over one city's typo.
    """
    if budget_minutes is None or slot_minutes is None or not (budget_minutes > slot_minutes > 0):
        return list(runner_args)
    factor = budget_minutes / slot_minutes
    scaled = list(runner_args)
    for i, arg in enumerate(scaled):
        if arg == _RESERVATION_FLAG and i + 1 < len(scaled):
            value, target = scaled[i + 1], i + 1
        elif arg.startswith(_RESERVATION_FLAG + '='):
            value, target = arg.split('=', 1)[1], i
        else:
            continue
        try:
            minutes = float(value)
        except ValueError:
            continue
        rewritten = '%g' % (minutes * factor)
        scaled[target] = rewritten if target != i else '%s=%s' % (_RESERVATION_FLAG, rewritten)
    return scaled


def stopped_on_budget(result):
    """Whether a city's run ended because a budget ran out - which is to say, it still has work.

    Read from what the RUNNER said, not from how long the queue watched it for. DownloadRunner writes a
    run summary naming what stopped each phase (--run-summary-file); a phase that stopped on 'max-runtime'
    is one that would have kept going, in either phase, and that is the whole question.

    Timing the subprocess instead was wrong in both directions, because the queue measures from Popen to
    exit while the runner's budget clock starts only after the pano-list fetch:

      - false positive: a COMPLETE city whose /adminapi/panos prologue alone outlasts its 12-minute slot
        exits having downloaded nothing, yet measured >= its budget - so it was re-run in every pass of
        every night, forever, burning a full slot each time and inflating the share divisor for everyone.
      - false negative: with the production `--min-depth-runtime 6`, a city whose image phase stopped on
        its reserved 6-minute share and whose depth phase then exhausted its own list exits at minute ~6.4
        of 12 - so the one city that most needed the leftover window was the one denied it.

    Only 'max-runtime' counts. The other depth stops are reasons NOT to re-run: 'blocked' means the host is
    standing down for six hours and a re-run would spend the slot rediscovering that, 'consecutive-failures'
    is a tripped breaker that would trip again, and 'max-requests' is a per-process cap the operator asked
    for, which re-running would silently multiply. Either phase's budget stop is enough on its own, so a
    stood-down depth phase cannot veto a real image backlog.

    Only an 'ok' run qualifies, whatever it reported: a crash says nothing about work left and re-running it
    is a crash loop, a timed-out city was killed past its budget and would be killed again, and a city the
    window never reached never started.

    When there is no summary at all, this falls back to the elapsed-time rule. That rule is at least a
    NECESSARY condition (a run that stopped on budget always measures at least its budget), and leaving the
    window unspent is worse than the wasted slot its false positives cost. An EMPTY summary is not a missing
    one: a runner that reported "nothing stopped either phase" is authoritative and does not fall back.

    The fallback is narrower than "an older runner" - see read_run_summary: a DownloadRunner without the
    flag refuses to start (argparse, exit 2) and is booked as failed, and a runner killed mid-run exits
    nonzero, so neither ever gets here. What does is an 'ok' run whose summary could not be written, or one
    where an operator's own --run-summary-file after `--` displaced the queue's.
    """
    if result is None or result.outcome != 'ok':
        return False
    if result.stop_reasons is not None:
        return any(result.stop_reasons.get(key) == STOP_MAX_RUNTIME for key in ('image_stop', 'depth_stop'))
    return (result.budget_minutes is not None and result.seconds is not None
            and result.seconds >= result.budget_minutes * 60.0)


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
    # A temp directory per run, removed on the way out: the summary is a message between two processes,
    # not an artifact. Writing it into the store would leave a run_summary.json beside the city's panos and
    # ledgers, where the next reader would reasonably take it for one.
    summary_dir = tempfile.mkdtemp(prefix='scrape-queue-%s-' % city.city_id)
    summary_path = os.path.join(summary_dir, _RUN_SUMMARY_NAME)
    try:
        return _run_city_with_summary(city, store_root, python_exe, runner_path, city_budget_minutes,
                                      kill_grace_minutes, runner_args, env, summary_path)
    finally:
        shutil.rmtree(summary_dir, ignore_errors=True)


def _run_city_with_summary(city, store_root, python_exe, runner_path, city_budget_minutes,
                           kill_grace_minutes, runner_args, env, summary_path):
    """run_city's body, with the run-summary temp file already arranged and its cleanup owned by run_city."""
    cmd = build_command(city, store_root, python_exe, runner_path, city_budget_minutes, runner_args,
                        run_summary_path=summary_path)
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
    stop_reasons = read_run_summary(summary_path)
    if stop_reasons is None and outcome == 'ok':
        # Worth one log line and no more: the run was fine, and the elapsed-time fallback still decides.
        # NOT the signature of an old runner beside a new queue - that one refuses the flag and exits 2, so
        # it shows up as FAILED, not here. This is a runner that could not write the file (it warned on
        # stdout), or an operator's own --run-summary-file after `--` displacing the queue's.
        logging.info("%s: no run summary (could not be written, or displaced by a --run-summary-file after "
                     "--); falling back to elapsed time to decide whether it has work left", city.city_id)
    return CityResult(city.city_id, outcome, exit_code, elapsed, stop_reasons=stop_reasons)


def run_queue(cities, store_root, python_exe, runner_path, runner_args, max_runtime_minutes=None,
              city_max_runtime=None, kill_grace_minutes=DEFAULT_KILL_GRACE_MINUTES, env=None,
              run_one=None, extra_passes=True, results=None):
    """Run every city in order, then the ones that ran out of budget again while the window lasts.

    Returns the CityResults in the order they ran: one per city for pass 1, then one per re-run, each
    stamped with its pass_number and the budget it was given.

    Pass 1 is the guarantee: the window gates STARTING a city, never interrupts one that is already running
    - the same rule as the image phase's budget (#51), and for the same reason: a partial pano or a torn
    ledger costs more than finishing five minutes late. Elapsed time is measured with time.monotonic() so an
    NTP step or a DST transition cannot stretch or shrink the night.

    The passes after it are the window being spent (#43). Which cities still have work is read from the
    pass before - from what the RUNNER reported about why each phase stopped (stopped_on_budget), not from
    how long the queue watched it - so nothing crosses nights and nothing reads the store. They need both a window and a slot: without a window there is nothing to
    spend, and without a slot there is no unit to hand out and no floor under the shares. A later pass never
    reports a city as not reached - running out of window there is the design working, not a fleet failing
    to complete - but a crash in one still fails the night, because a crash is a crash.
    """
    # Resolved at CALL time, not bound as a default at definition time, so that replacing the module
    # attribute (which is how main() is driven in tests) actually takes effect.
    run_one = run_city if run_one is None else run_one
    started = time.monotonic()
    # An out-parameter as well as the return value, so a caller can still see what ran when this raises.
    # A stop (SIGTERM, Ctrl-C) unwinds through here, and discarding the night's record on the way out is
    # exactly what left a killed queue with nothing on stdout but its per-city lines.
    results = [] if results is None else results
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
                # c.city_id, not c: this field is printed into the cron-mailed alarm line and is
                # keyed on by the extra passes, and a City here leaks the fqdn into both.
                results += [CityResult(c.city_id, 'skipped_deadline', None, None)
                            for c in cities[index:]]
                break
        budget = _city_budget(city_max_runtime, remaining)
        result = run_one(city, store_root, python_exe, runner_path, budget, kill_grace_minutes, runner_args,
                         env=env)
        results.append(result._replace(budget_minutes=budget, pass_number=1))

    if not extra_passes or max_runtime_minutes is None or city_max_runtime is None:
        return results

    latest = {r.city_id: r for r in results}
    pass_number = 1
    while True:
        working = [c for c in cities if stopped_on_budget(latest.get(c.city_id))]
        remaining = max_runtime_minutes - (time.monotonic() - started) / 60.0
        if not working or remaining < city_max_runtime:
            break
        pass_number += 1
        logging.info("pass %d starting: %d cities still had work, %.1f min of window left",
                     pass_number, len(working), remaining)
        print("[queue] pass %d: %d cities still had work, %.1f min of window left"
              % (pass_number, len(working), remaining))
        for index, city in enumerate(working):
            remaining = max_runtime_minutes - (time.monotonic() - started) / 60.0
            if remaining < city_max_runtime:
                # Not an alarm: pass 1 reached every one of these cities tonight. Said, so tomorrow's reader
                # knows the window closed here rather than the queue stopping for a reason.
                logging.info("pass %d: a slot no longer fits (%.1f min left); %d cities wait for tomorrow",
                             pass_number, remaining, len(working) - index)
                break
            budget = _extra_pass_budget(city_max_runtime, remaining, len(working) - index)
            # The reservation is a SHARE of the budget, so it moves with it - otherwise an endgame pass's
            # enlarged slot goes almost entirely to the image phase (#43).
            pass_args = scale_depth_reservation(runner_args, budget, city_max_runtime)
            result = run_one(city, store_root, python_exe, runner_path, budget, kill_grace_minutes,
                             pass_args, env=env)
            result = result._replace(budget_minutes=budget, pass_number=pass_number)
            results.append(result)
            latest[city.city_id] = result
    return results


# --- The manifest is cross-checked against the fleet (#130) --------------------------------------------------
#
# laurens-ia and bayonne-fr launched on 2026-09-11 with no manifest row and the queue reported 53/53 ok for six
# nights, until the auto-labeler's first Laurens labels showed blank Gallery cards: the app cuts AI-label crops
# from what this scraper stores. The manifest stays the deployment fact - a default that scrapes the wrong fleet
# is worse - so the omission is made loud instead: every deployment serves the same roster of every city, and
# once a night the queue asks one manifest host for it and names every city, public or private (#143), that has no row.
#
# The key is city_id, and that is a measurement, not a preference. The app reads its scraped panos from
# <pano.images.directory>/<city-id>/<panoId[:2]>/<panoId>.jpg with its OWN id - the one the roster reports - so
# a row under any other name scrapes into a directory the app never looks at. Bayonne's row was
# `bayonne,sidewalk-bayonne...` for one afternoon; the app calls itself `bayonne-fr`. Keying on fqdn would have
# let that through, and the resulting night would have been indistinguishable from the Laurens one.


def _open_url(url, timeout):
    """GET one URL and return at most ROSTER_MAX_BYTES of its body. The one seam that touches a socket.

    An opener with an EMPTY ProxyHandler, because urllib's default honours HTTP(S)_PROXY from the environment.
    DownloadRunner's session sets trust_env=False for exactly this host's sake (its environment is sourced from
    BASH_ENV under cron): a proxy's login page is a 200 that is not a roster, on every night, and the check
    would report itself unable to run for ever. Same policy, same reason.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': ROSTER_USER_AGENT,
                                                   'Accept': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        return response.read(ROSTER_MAX_BYTES)


def parse_roster(body):
    """The roster's entries, or RosterUnavailable. A 200 is not a roster; only the measured shape is.

    Positive evidence, the #99 rule: a JSON object whose `cities` is a list of objects each carrying a
    non-empty string `city_id` and a `visibility` that is 'public' or 'private', with at least one public
    entry. Everything else - an error envelope, a proxy's HTML, an empty list, a renamed field - is "this host
    did not answer", so the next one is asked. The last two rules are the ones that matter. Visibility no
    longer decides membership (#143), so they are not guarding the unlisted set; they guard the report. A
    live fleet always lists at least one public city, so an empty list or an all-private roster is a broken
    answer, not a fleet - accepting one would print "checked against 0 public cities" every night, a check
    that never ran wearing the face of one that passed. A visibility this code does not know is refused rather than read as "not public":
    whatever a third value would mean for the check is a decision, and refusing makes it one that gets taken.
    """
    try:
        data = json.loads(body)
    except ValueError as e:
        raise RosterUnavailable('not JSON') from e
    entries = data.get('cities') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RosterUnavailable('not a city roster')
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('city_id'), str) or not entry['city_id']
                or entry.get('visibility') not in ('public', 'private')):
            raise RosterUnavailable('not a city roster')
    if not any(entry['visibility'] == 'public' for entry in entries):
        raise RosterUnavailable('roster lists no public city')
    return entries


def _describe_failure(error):
    """One short reason per failed host, for the report line. A timeout is the common case and is named."""
    if isinstance(error, urllib.error.HTTPError):
        return 'HTTP %d' % error.code
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.timeout):
        return 'timed out'
    if isinstance(reason, (http.client.HTTPException, UnicodeError)):
        return '%s: %s' % (type(reason).__name__, reason)
    return str(reason) or type(reason).__name__


def fetch_roster(fqdn, timeout=ROSTER_TIMEOUT_SECONDS):
    """The roster as served by one host, or RosterUnavailable naming what went wrong.

    Catches http.client's exceptions and ValueError as well as OSError: a BadStatusLine or IncompleteRead
    from a proxy is an HTTPException, not an OSError, and a decode error is a ValueError. Any of them
    escaping here would print a traceback AFTER the summary, on the one night the check had something to say.
    """
    url = 'https://%s%s' % (fqdn, ROSTER_PATH)
    try:
        body = _open_url(url, timeout)
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise RosterUnavailable(_describe_failure(e)) from e
    return parse_roster(body)


def load_roster(hosts, fetch=None, max_hosts=ROSTER_MAX_HOSTS):
    """Ask the hosts in order until one serves a roster: (roster, host, attempts), or (None, None, attempts).

    Best-effort by design - an app being down must not decide the night on its own - but bounded, because
    ten dead hosts at 30 s each is five minutes after the fleet has finished, for an answer the third one
    already gave. `fetch` resolves to the module attribute at call time, so a test can replace it.

    Logged as it goes, at INFO: up to three 30 s attempts sit between the last city's line and the summary,
    and a check hung in DNS (which the socket timeout does not bound) would otherwise look, in the log, like
    a queue that died before its summary. Log only - the summary line is what says how it ended.
    """
    fetch = fetch_roster if fetch is None else fetch
    hosts = list(hosts)
    asking = hosts[:max_hosts]
    logging.info("cross-checking the manifest: asking up to %d of %d hosts for %s",
                 len(asking), len(hosts), ROSTER_PATH)
    attempts = []
    for fqdn in asking:
        try:
            return fetch(fqdn), fqdn, attempts
        except RosterUnavailable as e:
            attempts.append((fqdn, str(e)))
            logging.info("%s served no roster (%s); %s", fqdn, e,
                         'trying the next host' if len(attempts) < len(asking) else 'no hosts left to ask')
    return None, None, attempts


def _roster_host(url):
    """The host a roster entry's url names, lowercased, or None when it publishes none - or one this cannot
    read, which is treated the same way rather than guessed at. Never guessed.

    urlsplit raises ValueError on a malformed bracketed host (`https://[abc`), and url is the one roster
    field parse_roster does not validate, because the check only quotes it. An escape here would land in
    main()'s BaseException handler and print a traceback after the summary, discarding the whole night's
    check over a field it never needed.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parts = urlsplit(url if '//' in url else '//' + url)
    except ValueError:
        return None
    return parts.hostname or None


def _disabled_row_credits(disabled_fqdn, host, published_hosts=frozenset()):
    """Whether a '#' row's second column is evidence that the row is this city and not a comment csv split
    on a comma. With a published host it has to BE that host. A private city publishes none (#143), so the
    column has to look like a hostname instead - weaker evidence, and the strongest there is: the prose that
    fails it is ` bayonne-fr launched 2026-09-11`, and an empty column (`#zurich,`) fails it too, which is
    what "keep both columns on it" means. The published-host rule is not relaxed where the host is known.

    A column that is ANOTHER roster city's published host is refused even though it has the shape: it is
    positive evidence the row is not this city - `#zurich,sidewalk-sea.cs.washington.edu`, copied from the
    Seattle row and never edited, would otherwise silence zurich for good."""
    if not disabled_fqdn:
        return False
    if host is not None:
        return disabled_fqdn == host
    return _HOSTNAME_SHAPE.match(disabled_fqdn) is not None and disabled_fqdn not in published_hosts


def unlisted_cities(roster, cities, disabled):
    """The roster cities that have no manifest row, in roster order - public and private alike.

    A row counts when its city_id matches exactly - it is a directory name, so `Seattle-WA` is a different
    directory - or when a disabled ('#') row carries both that id and the city's host, which is the evidence
    that the row is this city and not a comment csv split on a comma. Hosts are compared case-insensitively:
    that half of the comparison is DNS, not a path.

    Visibility does not decide membership (#143). The check keyed on `public` until 2026-09-19, and 20 of the
    59 deployments are private: a private city launched with no row was silent in exactly the way Laurens
    was. A private city publishes no url, so for it the '#' credit rests on the column's shape instead
    (`_disabled_row_credits`), and the study and scratch deployments that are never scraped each carry one
    such row - the manifest, not this code, records that decision.

    The misnamed hint is drawn from disabled rows too: `#bayonne,sidewalk-bayonne...` against a roster
    `bayonne-fr` is still the Bayonne mistake, and the operator who "already added it" still needs telling
    why it is missing. A roster naming one city twice is reported once - the live roster never has, but a
    count that can be inflated by the server is a count the totals line cannot be trusted on.
    """
    enabled = {c.city_id for c in cities}
    # Disabled rows first, so an enabled row naming the same host is the one the hint quotes.
    by_host = {fqdn: city_id for city_id, fqdn in disabled.items() if fqdn}
    by_host.update((c.fqdn.lower(), c.city_id) for c in cities)
    published = {h for h in (_roster_host(e.get('url')) for e in roster) if h}
    unlisted, seen = [], set()
    for entry in roster:
        city_id = entry['city_id']
        if city_id in enabled or city_id in seen:
            continue
        seen.add(city_id)
        host = _roster_host(entry.get('url'))
        if _disabled_row_credits(disabled.get(city_id), host, published):
            continue
        unlisted.append(Unlisted(city_id, host, by_host.get(host) if host else None, entry['visibility']))
    return unlisted


def roster_hosts(cities, results):
    """Which manifest hosts to ask, and in what order: the ones whose city ran ok tonight first, MOST RECENT
    first, then the rest in manifest order. The check runs after the fleet, so the host that just served
    /adminapi/panos is the one to spend a roster call on - and under a 690-minute window "just" is the last
    ok run, not the first: in run order the first host asked would be the one that answered eleven hours
    ago. A city re-run in a later pass counts by its latest OK run, since that is when its host was last seen
    up: a later run that failed or timed out is not evidence about the host - the runner's own crash and a
    hung depth request both book that way - so it neither demotes the city nor removes it from the ok group.
    """
    ok = list(dict.fromkeys(r.city_id for r in reversed(results) if r.outcome == 'ok'))
    by_id = {c.city_id: c.fqdn for c in cities}
    ran_ok = set(ok)
    return [by_id[c] for c in ok if c in by_id] + [c.fqdn for c in cities if c.city_id not in ran_ok]


def check_manifest(cities, disabled, hosts=None, fetch=None):
    """One night's cross-check: fetch the roster from the first host that serves one and compare.

    Hosts are asked once each, in first-appearance order: read_city_list refuses a duplicate city_id but
    not a duplicate fqdn, and three rows on one host would otherwise spend the whole ROSTER_MAX_HOSTS cap on
    that host and report "3 of 3 hosts tried" having asked one.
    """
    hosts = list(dict.fromkeys([c.fqdn for c in cities] if hosts is None else hosts))
    roster, host, attempts = load_roster(hosts, fetch=fetch)
    if roster is None:
        return ManifestCheck(None, 0, [], attempts, len(hosts))
    public_count = sum(1 for entry in roster if entry['visibility'] == 'public')
    return ManifestCheck(host, public_count, unlisted_cities(roster, cities, disabled), attempts, len(hosts),
                         private_count=len(roster) - public_count)


def _describe_unlisted(city):
    if city.fqdn is None:
        # The roster's word beside it, because a private city is the case with no url and the one whose row
        # the operator most often has to write as "known, never scraped here" rather than "add it".
        return '%s (%s; url not published)' % (city.city_id, city.visibility)
    if city.misnamed_as is None:
        return '%s (%s)' % (city.city_id, city.fqdn)
    return "%s (%s; the manifest calls it '%s', the app reads <store-root>/%s)" % (
        city.city_id, city.fqdn, city.misnamed_as, city.city_id)


def manifest_report(check, advisory=False):
    """The report's lines about the cross-check, as (gap, status): two lists of (line, level).

    The gap is what went wrong and belongs with the failures, above the totals; the status is what the check
    rested on and belongs after them. Split here rather than by the summary, so the summary never has to
    recognise a line by its wording.

    `advisory` is the dry run, where a roster nobody serves does not decide the exit code and the line says
    WARNING. On the night it does decide it, and the line says ERROR: cron mails any output regardless of the
    exit code, so the word on the line is the only signal the mail carries, and `grep ERROR scrape_queue.log`
    should agree with the exit code rather than find a WARNING beside an exit 1. Which side of the split that
    line lands on follows the same rule: on the night an unserved roster IS the failure, so it is a gap line
    and leads the totals like the other failures; on a dry run it is status, printed after the plan. It was
    status on both paths until the 2026-09-18 post-merge review, which put the night's ERROR line under a
    clean "54/54 cities ok" - the exact ordering the summary's docstring promises never happens.
    """
    gap, status = [], []
    if check.unlisted:
        gap.append(("[queue] cities missing from the manifest: %s"
                    % ', '.join(_describe_unlisted(c) for c in check.unlisted), logging.ERROR))
        gap.append(("[queue]   add one city_id,fqdn row per city - city_id must be the app's own id, "
                    "because that is the directory it reads; a '#city_id,fqdn' row records one that is "
                    "deliberately not scraped here", logging.ERROR))
    if check.roster_host is None:
        level = logging.WARNING if advisory else logging.ERROR
        (status if advisory else gap).append(
            ("[queue] %s: manifest not cross-checked - no roster from %s (%d of %d hosts tried)"
             % (logging.getLevelName(level),
                ', '.join('%s (%s)' % attempt for attempt in check.attempts),
                len(check.attempts), check.hosts_total), level))
    else:
        status.append(("[queue] manifest checked against %d public and %d private cities (roster from %s)"
                       % (check.public_count, check.private_count, check.roster_host), logging.INFO))
    return gap, status


# The order the non-ok lines are printed in. stdout is what cron mails, so the two or three real crashes
# have to appear before the twenty skipped_deadline lines rather than interleaved with them. Run order is
# kept WITHIN an outcome, so "which city crashed first" is still readable off the list.
_OUTCOME_ORDER = ('failed', 'timed_out', 'skipped_deadline')


def summarise(results, elapsed_minutes, manifest_check=None):
    """The run's one-screen report: a line per run that did not simply work, the night's totals, then one
    line per extra pass - and, when the manifest was cross-checked (#130), what that found.

    Every run gets a line in the queue log, but stdout is what cron mails, so it leads with what went wrong.
    A clean night is a few lines; a bad one names every city and why.

    The totals line has two different denominators and they are both deliberate. "N/M cities ok" counts
    pass 1 - one result per city - so a night that re-ran one city five times still reads as a fleet of M,
    not of M + 5. The failure counts beside it are over EVERY run, because a city hard-killed in pass 2 is a
    city that was hard-killed: counting only pass 1 there printed "0 failed, 0 timed out" on a night that
    exited 1, contradicting both the exit code and the per-run lines immediately above it.

    The cross-check follows the same two rules. Its gap lines - a missing city, or a roster nobody served -
    go ABOVE the totals with the other things that went wrong, and the totals line carries the count or says
    the check did not run, so "54/54 cities ok, 0 failed, 0 timed out, 0 not reached" is never printed above
    an exit 1 the runs did not earn. The line saying what the check rested on is evidence, not an alarm, and
    goes last.
    """
    gap, status = ([], []) if manifest_check is None else manifest_report(manifest_check)
    first = [r for r in results if r.pass_number == 1]
    by_outcome = {}
    for r in results:
        by_outcome.setdefault(r.outcome, []).append(r)
    lines = ["", "[queue] ==== summary ===="]
    for outcome in _OUTCOME_ORDER:
        for r in by_outcome.get(outcome, []):
            when = '' if r.seconds is None else ' after %.1f min' % (r.seconds / 60.0)
            code = '' if r.exit_code is None else ' (exit %d)' % r.exit_code
            which = '' if r.pass_number == 1 else ' in pass %d' % r.pass_number
            lines.append("[queue] %-24s %s%s%s%s" % (r.city_id, outcome.upper(), code, when, which))
    lines += [line for line, _ in gap]
    n_missing = 0 if manifest_check is None else len(manifest_check.unlisted)
    if manifest_check is not None and manifest_check.roster_host is None:
        missing = ', manifest not cross-checked'
    else:
        missing = ('' if not n_missing else ', %d %s missing from the manifest'
                   % (n_missing, 'city' if n_missing == 1 else 'cities'))
    lines.append("[queue] %d/%d cities ok, %d failed, %d timed out, %d not reached%s; %.1f min total"
                 % (sum(1 for r in first if r.outcome == 'ok'), len(first),
                    len(by_outcome.get('failed', [])),
                    len(by_outcome.get('timed_out', [])), len(by_outcome.get('skipped_deadline', [])),
                    missing, elapsed_minutes))
    passes = sorted({r.pass_number for r in results if r.pass_number > 1})
    for n in passes:
        runs = [r for r in results if r.pass_number == n]
        minutes = sum(r.seconds or 0.0 for r in runs) / 60.0
        lines.append("[queue] pass %d: %d cities re-run in %.1f min - %s"
                     % (n, len(runs), minutes,
                        ', '.join('%s %.1f' % (r.city_id, (r.seconds or 0.0) / 60.0) for r in runs)))
    lines += [line for line, _ in status]
    return '\n'.join(lines)


def exit_code_for(results, manifest_check=None):
    """0 only when every RUN succeeded - one per city in pass 1, plus one per re-run after it - and, when the
    manifest was cross-checked, the check found a roster and no gap.

    So a city that succeeded in pass 1 and crashed in pass 2 makes the night nonzero, deliberately: a crash
    is a crash whichever pass it happened in. What does NOT fail the night is a city an extra pass never
    reached, which produces no result at all - pass 1 reached it, and running out of window in pass 3 is the
    design working.

    A city that was never reached counts as a failure on purpose. cron mails a nonzero exit, and a fleet
    quietly completing 40 of 53 cities every night - which is exactly what an un-monitored window produces -
    is the condition this whole change exists to make visible. If a night's truncation is expected and
    accepted, the window is the wrong size.

    A city with no manifest row, private or public (#143), fails the night for the same reason (#130): the
    fleet ran green for six nights while two launched cities went unscraped, and the exit code is the one
    unattended alarm. So does a
    roster that no host would serve - decided 2026-09-17: the hosts asked have just served /adminapi/panos, so
    three of them failing this call is a broken check (an API rename, an env proxy, a moved endpoint) rather
    than weather, and a check silently skipped every night is the failure the check exists to prevent. None
    means the check did not run (a dry run reports it its own way; a stopped queue never gets to it).
    """
    runs_ok = all(r.outcome == 'ok' for r in results)
    check_ok = (manifest_check is None
                or (manifest_check.roster_host is not None and not manifest_check.unlisted))
    return 0 if runs_ok and check_ok else 1


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

    disabled = {}
    try:
        cities = read_city_list(args.cities, disabled=disabled)
        ordered = plan_order(cities, only=args.only,
                             rotation_ordinal=None if args.no_rotate else datetime.now().toordinal())
    except (OSError, ValueError) as e:
        print("Could not read the city list: %s" % (e,), file=sys.stderr)
        return 2

    # Both budgets are required for a pass to be possible at all (run_queue returns after pass 1
    # without them), so the banner and --dry-run must agree about that rather than only --dry-run
    # guarding on it. An operator who typed --max-runtime alone needs to be told the default-on
    # behaviour is off, not congratulated on it.
    extra_passes = (not args.single_pass and not args.only
                    and args.max_runtime is not None and args.city_max_runtime is not None)

    if args.dry_run:
        # The budget shown is the FIRST city's - its own cap clamped by the whole window. Later cities get
        # whatever the window has left by the time they start, which a plan printed before anything runs
        # cannot know. Said here rather than left for someone to discover from a mismatched log line.
        budget = _city_budget(args.city_max_runtime, args.max_runtime)
        # The one flag a real run adds that cannot be shown: the summary path is a per-run temp file.
        print("Would run %d cities into %s (budgets shown are the first city's; each real run also gets "
              "--run-summary-file <per-run temp file>):" % (len(ordered), args.store_root))
        for i, city in enumerate(ordered, 1):
            print("  %2d. %s" % (i, ' '.join(build_command(city, args.store_root, python_exe, runner_path,
                                                           budget, runner_args))))
        if extra_passes:
            print("Then extra passes over whichever cities ran out of budget, while a slot of the window "
                  "remains - which cities, and with what budgets, cannot be shown before pass 1 has run.")
        # The same cross-check the night runs (#130), so a hand-run before a launch answers "is everything
        # wired?" now rather than tomorrow morning. Printed only: no log is configured on a dry run, so
        # load_roster's INFO narration goes nowhere here (the first module-level logging call installs
        # Python's default WARNING-level stderr handler, measured, which drops it) and an offline operator
        # simply waits up to ROSTER_MAX_HOSTS x ROSTER_TIMEOUT_SECONDS for the WARNING line. A gap exits 1
        # exactly as the night would; a roster nobody served is advisory here - this is someone at a
        # keyboard, possibly offline, reading the plan - where the night treats it as a failed check.
        check = check_manifest(cities, disabled)
        gap, status = manifest_report(check, advisory=True)
        print('\n'.join(line for line, _ in gap + status))
        return 1 if check.unlisted else 0

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
    results = []
    check = None
    try:
        with exclusive_lock(lock_path):
            # Announced only once the lock is held, so a run that is about to be refused never prints a
            # start line that reads like a fleet beginning to scrape.
            logging.info("queue starting: %d cities, window %s min, per-city %s min, extra passes %s",
                         len(ordered), args.max_runtime, args.city_max_runtime,
                         'on' if extra_passes else 'off')
            print("[queue] %d cities, window %s min, per-city cap %s min, extra passes %s"
                  % (len(ordered), args.max_runtime, args.city_max_runtime, 'on' if extra_passes else 'off'))
            run_queue(ordered, args.store_root, python_exe, runner_path, runner_args,
                      max_runtime_minutes=args.max_runtime, city_max_runtime=args.city_max_runtime,
                      kill_grace_minutes=args.kill_grace, extra_passes=extra_passes,
                      results=results)
        # After the fleet and outside the lock (a GET needs none), against the whole manifest rather than
        # tonight's --only selection: Laurens and Bayonne launched together, and an operator re-running one
        # by hand is the moment to hear about the other. Inside the try, so a stop landing mid-fetch still
        # unwinds through the handler below with the night's record intact.
        check = check_manifest(cities, disabled, hosts=roster_hosts(cities, results))
    except QueueLocked as e:
        # Loud, and nonzero: the previous night's queue still running when tonight's starts is the exact
        # condition 53 unsynchronised crontab slots could not detect. Nothing ran, so there is nothing to
        # summarise - returned from inside the handler so the finally below has no report to print.
        logging.error("another queue run holds %s (%s); exiting without running anything", lock_path, e)
        print("ERROR: another scrape queue is already running (lock %s: %s). Nothing was run."
              % (lock_path, e), file=sys.stderr)
        return 3
    except BaseException:
        # The queue is being STOPPED - a cron timeout wrapper's SIGTERM (translated to SystemExit above), an
        # operator's kill, Ctrl-C. run_city has already stopped the city it was supervising; what is left is
        # the record of everything that did run tonight, which used to be discarded on the way out. That
        # mattered more the moment the passes made the queue occupy the whole 690-minute window rather than
        # the 477 minutes pass 1 alone used: the interval in which a stop can land is now the entire night,
        # and the evidence lost is several passes deep.
        _report(results, started)
        raise

    return _report(results, started, check)


def _report(results, started_monotonic, manifest_check=None):
    """Print and log the summary, and return the exit code it implies.

    Every line is logged at INFO except the cross-check's, which carry their own levels: a gap and an
    unserved roster are the night's failure and are logged as one, so `grep ERROR scrape_queue.log` finds
    them next week the way the mail finds them tonight.
    """
    summary = summarise(results, (time.monotonic() - started_monotonic) / 60.0, manifest_check)
    print(summary)
    levels = {} if manifest_check is None else dict(sum(manifest_report(manifest_check), []))
    for line in summary.splitlines():
        if line.strip():
            logging.log(levels.get(line, logging.INFO), line.replace('[queue] ', ''))
    return exit_code_for(results, manifest_check)


if __name__ == '__main__':
    sys.exit(main())
