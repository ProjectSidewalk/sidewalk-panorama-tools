#!/usr/bin/env python3
"""The fleet roster cross-check for the log analyzer (#133).

`cities.csv` has the same silent-gap shape #130 fixed in the queue: a city the file does not name is not
monitored, and nothing says so. It has gone wrong three times - `newport-ky` was scraped nightly for weeks
while sitting outside the analyzer, the 2026-09-17 sweep found `laurens-ia` with no row at all and Bayonne's
row reading `bayonne` where the app calls itself `bayonne-fr`, and on 2026-09-22 re-launched `washington-dc`
had no row. Found by hand every time, by diffing this
file against the crontab; `docs/log-analyzer.md` offered a `comm -23` one-liner "whenever the two are both
in front of you", which is to say never on a schedule.

The analyzer needs this more sharply than the queue does. It IS the monitoring layer, so a city it does not
watch has **no** alarm at all - the queue at least books the city's own exit status.

**This is a deliberate copy of `scrape_queue.py`'s roster machinery, not an import.** The two modules share
no code today and that is the point: `log_analyzer/analyze.py` is ops monitoring that has to keep working
when the runners are broken, and importing the fleet driver would couple the alarm to the thing it watches.
`tests/test_log_analyzer.py` pins this module's `parse_roster` and `scrape_queue.parse_roster` against the
same fixture body, so the copy cannot drift without a test saying so.

**The fetch is unauthenticated, deliberately.** The public roster lists every city, private ones included,
which is all this comparison needs. A keyed variant that would also return private cities' `url` was
considered and rejected (2026-09-23): the app withholds those urls only to keep them out of crawlable link
graphs (SidewalkWebpage#5259), and they are published in SidewalkWebpage's public `conf/cityparams.conf`,
so a credential here would guard nothing secret while adding one more to store and rotate.
"""

import http.client
import json
import re
import socket
import urllib.error
import urllib.request
from collections import namedtuple
from urllib.parse import urlsplit

# Every deployment serves this list of every city - city_id, url, visibility - and it is the same list from
# every host, which is why asking one host is enough.
ROSTER_PATH = '/v3/api/cities'
ROSTER_TIMEOUT_SECONDS = 30.0
# The roster measured 2026-09-22 is 19 KB for 59 cities; this is the most that will be read of anything a
# host sends back, so a redirect to something large cannot hold the report open.
ROSTER_MAX_BYTES = 4 * 1024 * 1024
ROSTER_USER_AGENT = ('sidewalk-panorama-tools log_analyzer '
                     '(+https://github.com/ProjectSidewalk/sidewalk-panorama-tools)')

# What a '#' row has to carry to be credited as "deliberately not monitored". See `disabled_rows`.
OPT_OUT_MARKER = 'not-monitored:'

# One unlisted city: its roster id, the host its url names (None for a private city, whose url is withheld),
# and its visibility, so the report can say which kind of gap it found.
Unlisted = namedtuple('Unlisted', 'city_id host visibility')


class RosterUnavailable(Exception):
    """No roster from this host: it did not answer, or what it answered is not a roster."""


def _open_url(url, timeout):
    """GET one URL and return at most ROSTER_MAX_BYTES of its body. The one seam that touches a socket.

    An opener with an EMPTY ProxyHandler, because urllib's default honours HTTP(S)_PROXY from the
    environment. `DownloadRunner`'s session sets `trust_env=False` for the same reason and on the same host
    (its environment is sourced from BASH_ENV under cron): a proxy's login page is a 200 that is not a
    roster, on every night, and the check would report itself unable to run for ever.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {'User-Agent': ROSTER_USER_AGENT, 'Accept': 'application/json'}
    request = urllib.request.Request(url, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return response.read(ROSTER_MAX_BYTES)


def parse_roster(body):
    """The roster's entries, or RosterUnavailable. A 200 is not a roster; only the measured shape is.

    Positive evidence, the #99 rule: a JSON object whose `cities` is a list of objects each carrying a
    non-empty string `city_id` and a `visibility` that is 'public' or 'private', with at least one public
    entry. Everything else - an error envelope, a proxy's HTML, an empty list, a renamed field - is "this
    host did not answer".

    The last two rules are the ones that matter. An upstream rename of the visibility values would otherwise
    read as "every city is private", the unlisted set would be empty, and the report would say it checked
    against nothing every night - a check that never runs wearing the face of one that passed. A live fleet
    always lists at least one public city, so an empty list fails that rule too. A visibility this code does
    not know is refused rather than read as "not public": what a third value means for the check is a
    decision, and refusing makes it one that gets taken.

    Kept byte-for-byte equivalent to `scrape_queue.parse_roster`; a test pins both against one fixture.
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
    """One short reason for the report line. A timeout is the common case and is named."""
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
    escaping here would print a traceback after the summary, on the one night the check had something to say.
    """
    url = 'https://%s%s' % (fqdn, ROSTER_PATH)
    try:
        body = _open_url(url, timeout)
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise RosterUnavailable(_describe_failure(e)) from e
    return parse_roster(body)


def roster_host(url):
    """The host a roster entry's url names, lowercased, or None when it publishes none.

    Never guessed. `urlsplit` raises ValueError on a malformed bracketed host (`https://[abc`), and url is
    the one roster field `parse_roster` does not validate, because the check only quotes it. An escape here
    would land in main()'s handler and discard the whole night's check over a field it never needed.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip() if '//' in url else '//' + url.strip())
    except ValueError:
        return None
    return parts.hostname or None


def disabled_rows(rows):
    """The city_ids a '#' row records as deliberately not monitored.

    **The analyzer's evidence is weaker than the queue's, so the marker is explicit rather than inferred.**
    `scrape_queue` credits a '#' row when its second column IS the city's host - an independent fact it can
    check. `cities.csv`'s second column is a display name, so there is no such fact here, and the shape the
    queue guards against (`# laurens-ia, bayonne-fr launched 2026-09-11`, which `csv` splits into a row for
    `laurens-ia`) would be credited by any rule that only matched the id.

    So an opt-out row has to SAY it is one: `#<city_id>,"not-monitored: <why>"`. Prose does not produce that
    by accident, it greps, and it carries the reason next to the decision. A '#' row without the marker is
    a comment and is ignored - which means the city is still named by the check, which is the safe way round.
    """
    disabled = set()
    for row in rows:
        city_id = (row.get('city_id') or '').strip()
        if not city_id.startswith('#'):
            continue
        note = (row.get('display_name') or '').strip()
        if note.lower().startswith(OPT_OUT_MARKER):
            disabled.add(city_id.lstrip('#').strip())
    return disabled


def unlisted_cities(roster, city_ids, disabled=()):
    """The roster cities with no `cities.csv` row, in roster order - public and private alike.

    A row counts when its city_id matches exactly. It is a directory name on the store
    (`<base>/<city_id>/log.csv`), so `Seattle-WA` is a different directory, and the Bayonne finding is
    exactly a row whose name was one character off the id the app reads its own panos under.

    Visibility does not decide membership (#143). Twenty of the fifty-nine deployments are private, and
    `washington-dc` - re-launched, live, serving `/adminapi/panos` - had no row here on 2026-09-22, silent
    in exactly the way Laurens was. A city that is genuinely never monitored carries an opt-out row instead;
    the file, not this code, records that decision.

    A roster naming one city twice is reported once: the live roster never has, but a count the server can
    inflate is a count the totals line cannot be trusted on.
    """
    have = set(city_ids)
    disabled = set(disabled)
    unlisted, seen = [], set()
    for entry in roster:
        city_id = entry['city_id']
        if city_id in have or city_id in disabled or city_id in seen:
            continue
        seen.add(city_id)
        unlisted.append(Unlisted(city_id, roster_host(entry.get('url')), entry['visibility']))
    return unlisted
