#!/usr/bin/env python3
"""Ask a live deployment which fields /adminapi/labels/cvMetadata serves, and compare them against what
CropRunner requires (#135).

`label_type_id` became `label_type` upstream (SidewalkWebpage#4103, released v11.11.0 on 2026-09-02) and the
cropper produced zero crops against every live deployment for 16 days while CI stayed green the whole time.
Nothing was wrong with the suite: it is network-free by design, so every test of the `-d` intake runs against
a fixture this repo wrote, and a fixture cannot notice that the server stopped agreeing with it. A contract
with another codebase can only be checked against that codebase.

So this lives outside tests/ and is the one thing here that talks to a deployment on purpose:

  python3 check_cvmetadata_schema.py --host sidewalk-sea.cs.washington.edu

It exits 0 when every field CropRunner needs is served, 1 naming the ones that are not, and 3 when the
deployment could not be read at all - a check that did not run has not passed. Under cron, nonzero is the
whole interface: mail-on-failure is the unattended path to a human, the same one log_analyzer uses.

Three properties are load-bearing, and each is the failure mode of the obvious implementation:

  - **The required list is read off CropRunner, never restated.** Two copies drift silently, which is the
    bug. REQUIRED_LABEL_COLUMNS and LABEL_TYPE_COLUMNS are imported and asked at call time.
  - **A field the server ADDS is not a failure.** Servers add fields; a check that cried wolf on that would
    be muted within a month, and a muted tripwire is worse than none. Only a field the cropper needs and no
    longer receives fails the run. New fields are still printed, because an addition is often the first
    visible half of a rename.
  - **Only the first record is read.** cvMetadata is the entire label list - 183,682 rows for seattle-wa,
    tens of MB - and a nightly check that pulled all of it would cost more than the thing it guards. The
    response is streamed and the read stops at the first complete record, whose keys ARE the field names.

See docs/api-fields.md, "Checking the contract against a live deployment".
"""

import argparse
import http.client
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from collections import namedtuple

import CropRunner

# The endpoint CropRunner's -d intake fetches. One path, written once.
ENDPOINT_PATH = '/adminapi/labels/cvMetadata'

# No default, deliberately: the deployment to ask is a deployment fact, and a default would quietly check a
# host nobody chose - resolve_sftp's PS_SFTP_HOST rule and scrape_queue's --cities rule.
HOST_ENV = 'PS_CVMETADATA_HOST'

DEFAULT_TIMEOUT_SECONDS = 60.0
READ_CHUNK_BYTES = 64 * 1024

# How much of the body may be read before giving up on finding a record in it. One record is a few hundred
# bytes; a megabyte is room for a server that pretty-prints or grows the schema several times over, and a
# bound on a body that never ends.
MAX_PREFIX_BYTES = 1024 * 1024

USER_AGENT = 'sidewalk-panorama-tools cvMetadata schema check'

EXIT_OK = 0
EXIT_MISSING_FIELDS = 1
# 2 is argparse's usage error; nothing here returns it.
EXIT_UNAVAILABLE = 3


class SchemaUnavailable(Exception):
    """The deployment did not serve something this can read field names out of - the check did not run.

    Deliberately distinct from a schema failure. A proxy login page, a timeout or a deployment with no
    labels tells us nothing about the field names, and reporting it as "no fields served" would name every
    required field as missing: an alarm about the cropper raised by a broken network.
    """


class _Incomplete(Exception):
    """Internal: the prefix read so far may still become a record. Never leaves this module."""


class Comparison(namedtuple('Comparison', ['served', 'missing', 'missing_groups', 'not_required'])):
    """One verdict about one deployment's field set.

    served          the field names the endpoint sent, in the order it sent them
    missing         required fields that are absent - each one fails the run
    missing_groups  either/or groups with no member present (the label type pair) - each fails the run
    not_required    fields served that CropRunner does not REQUIRE (it may still read some of
                    them - pano_width through _metadata_dims). Information only, NEVER a failure.
    """

    @property
    def ok(self):
        """True only when nothing REQUIRED is absent. Note what is not consulted: `not_required`. Whether the
        server added a field is not part of this answer, and writing it in here is the one edit that turns
        the tripwire into the boy who cried wolf."""
        return not self.missing and not self.missing_groups


def required_fields():
    """The fields the crop loop indexes on every row, asked of CropRunner rather than copied from it.

    Asked at call time, not at import: a test that moves the constant has to move the verdict with it, and
    a module-level snapshot would pass that test by accident the first time and never again.
    """
    return tuple(CropRunner.REQUIRED_LABEL_COLUMNS)


def required_field_groups():
    """Either/or requirements: a row needs at least one member of each group.

    One today - the label type arrives as `label_type_id` or `label_type` (#123) - and it is the group the
    16 days were about. It cannot be folded into required_fields(): naming `label_type_id` as missing would
    send an operator looking for a column no current deployment sends.
    """
    return (tuple(CropRunner.LABEL_TYPE_COLUMNS),)


def compare_served_fields(served):
    """Compare what a deployment serves against what CropRunner requires.

    :param served: field names, in served order.
    :return: Comparison. `ok` is false only when something REQUIRED is absent; an unfamiliar field is
             reported under `not_required` and changes nothing, because a server adding a field is the normal
             case and a check that fails on it stops being read.
    """
    served = list(served)
    present = set(served)
    groups = required_field_groups()
    missing = [field for field in required_fields() if field not in present]
    missing_groups = [group for group in groups if not any(field in present for field in group)]
    reads = set(required_fields()).union(*(set(group) for group in groups)) if groups else set(required_fields())
    not_required = [field for field in served if field not in reads]
    return Comparison(served=served, missing=missing, missing_groups=missing_groups, not_required=not_required)


def record_fields(text):
    """The field names of the first record in a cvMetadata payload prefix.

    Positive evidence, the #99 rule: only a JSON array whose first element is an object is a cvMetadata
    response. An error envelope, a proxy's HTML and an empty list are each SchemaUnavailable rather than
    "zero fields", because the difference between "the endpoint moved" and "we never reached the endpoint"
    is the difference between two different people's problem.

    :param text: the decoded prefix of the body, which may stop anywhere.
    :raises _Incomplete: the prefix is still consistent with a record that has not arrived yet.
    :raises SchemaUnavailable: it is not a cvMetadata payload, whatever else arrives.
    """
    head = text.lstrip()
    if not head:
        raise _Incomplete()
    if not head.startswith('['):
        raise SchemaUnavailable('the body is not a cvMetadata label list (it starts %r)' % (head[:40],))
    body = head[1:].lstrip()
    if body.startswith(']'):
        raise SchemaUnavailable('the deployment served no labels, so it says nothing about the field names')
    if not body:
        raise _Incomplete()
    try:
        record, _ = json.JSONDecoder().raw_decode(body)
    except ValueError:
        raise _Incomplete()
    if not isinstance(record, dict):
        raise SchemaUnavailable('the first element of the list is not a label record')
    return list(record.keys())


def served_fields(read, max_bytes=MAX_PREFIX_BYTES, chunk_bytes=READ_CHUNK_BYTES):
    """Read just enough of a response body to learn the field names.

    :param read: a callable taking a byte count, like an HTTP response's read - the whole body is never
                 asked for, because the whole body is the city's entire label list.
    :param max_bytes: give up after this much. A server that streams without ever completing a record is
                      the case this bounds; the check has to end tonight either way.
    :return: field names, in served order.
    :raises SchemaUnavailable: nothing readable arrived (truncated, empty, or not a label list).

    The prefix is decoded with errors='replace' because a read can stop in the middle of a multi-byte
    character. Only the keys are wanted and those are ASCII; a mangled replacement inside a *value* is still
    a valid JSON string, so it cannot change the answer.
    """
    prefix = b''
    while True:
        chunk = read(chunk_bytes)
        if chunk:
            prefix += chunk
        try:
            return record_fields(prefix.decode('utf-8', 'replace'))
        except _Incomplete:
            if not chunk:
                raise SchemaUnavailable('the body ended before a complete label record (%d bytes)'
                                        % (len(prefix),))
            if len(prefix) >= max_bytes:
                raise SchemaUnavailable('no complete label record in the first %d bytes' % (len(prefix),))


def _open_stream(url, timeout):
    """GET one URL and return the open response, unread. The one seam in this repo's non-test code that is
    supposed to touch a live deployment, and the one thing the suite never calls.

    An opener with an EMPTY ProxyHandler, because urllib's default honours HTTP(S)_PROXY from the
    environment - scrape_queue's roster fetch and DownloadRunner's trust_env=False, same reason: a proxy's
    login page is a 200 that is not a label list, every night, and this would report itself unable to run
    for ever.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    return opener.open(request, timeout=timeout)


def _describe_failure(error):
    """One short reason for the report line. A timeout is the common case and is named as one."""
    if isinstance(error, urllib.error.HTTPError):
        return 'HTTP %d' % error.code
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.timeout):
        return 'timed out'
    if isinstance(reason, (http.client.HTTPException, UnicodeError)):
        return '%s: %s' % (type(reason).__name__, reason)
    return str(reason) or type(reason).__name__


def fetch_served_fields(fqdn, timeout=DEFAULT_TIMEOUT_SECONDS):
    """The field names one deployment's cvMetadata serves, or SchemaUnavailable naming what went wrong.

    Catches http.client's exceptions and ValueError alongside OSError for scrape_queue's reason: a
    BadStatusLine or IncompleteRead from a proxy is an HTTPException, not an OSError, and any of them
    escaping here prints a traceback instead of the one line saying why the check could not run.
    """
    url = 'https://%s%s' % (fqdn, ENDPOINT_PATH)
    logging.info('Asking %s for its cvMetadata field names', url)
    try:
        with _open_stream(url, timeout) as response:
            return served_fields(response.read)
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise SchemaUnavailable(_describe_failure(e)) from e


def report(fqdn, comparison):
    """Print the verdict and return the exit code.

    Printed, not logged: under cron this is the text that gets mailed, and the run's whole output is four
    lines an operator reads once a night at most. The failing line names every missing field, because a
    report that names one of three sends the reader round the same loop twice.
    """
    print('cvMetadata schema check: https://%s%s' % (fqdn, ENDPOINT_PATH))
    print('  %d fields served: %s' % (len(comparison.served), ', '.join(comparison.served)))
    if comparison.not_required:
        print('  %d beyond what CropRunner requires (fine - servers add fields, and some of these '
              'the cropper reads only when they are there): %s'
              % (len(comparison.not_required), ', '.join(comparison.not_required)))
    if comparison.ok:
        print('OK: every field CropRunner requires is served.')
        return EXIT_OK
    for field in comparison.missing:
        print('MISSING: CropRunner requires %r and %s no longer serves it '
              '(CropRunner.REQUIRED_LABEL_COLUMNS)' % (field, fqdn))
    for group in comparison.missing_groups:
        print('MISSING: CropRunner needs one of %s and %s serves none of them '
              '(CropRunner.LABEL_TYPE_COLUMNS)' % (', '.join(repr(f) for f in group), fqdn))
    print('FAIL: the cropper cannot read this deployment. See docs/api-fields.md.')
    return EXIT_MISSING_FIELDS


def build_parser():
    parser = argparse.ArgumentParser(
        description='Check that a live deployment still serves every cvMetadata field CropRunner requires.')
    parser.add_argument('--host', default=None, metavar='FQDN',
                        help='deployment to ask, e.g. sidewalk-sea.cs.washington.edu. No default: '
                             'falls back to $%s, and the run fails if neither is set.' % HOST_ENV)
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT_SECONDS, metavar='SECONDS',
                        help='per-request timeout (default: %(default)s)')
    parser.add_argument('--log-file', default=None, metavar='PATH',
                        help='also write this run\'s detail here; by default it goes to stderr only')
    return parser


def configure_logging(log_path=None):
    """Request-level detail to stderr, and to a file when one is asked for.

    The two channels are the repo's usual split: the verdict is printed, because that is what cron mails,
    while this carries the URL and the reason a failed fetch failed. A check with no directory of its own
    has nowhere to put a log by default, so there is no default path - and nothing here is worth failing a
    run over if the path is bad, so a file that cannot be opened is a warning.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]
    fallback_error = None
    if log_path:
        try:
            handlers.append(logging.FileHandler(log_path))
        except OSError as e:
            fallback_error = e
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        root.addHandler(handler)
    if fallback_error is not None:
        logging.warning('Could not open %s (%s); logging to stderr for this run', log_path, fallback_error)


def resolve_host(args):
    """The deployment to ask: the flag, else the environment, else exit.

    No default, and the fqdn is checked for a scheme or a path rather than pasted into a URL: `--host
    https://sidewalk-sea.../` would build `https://https://...`, whose failure names a DNS error and not
    the typo that caused it.
    """
    host = args.host or os.environ.get(HOST_ENV)
    if not host:
        sys.exit('No deployment to check. Pass --host <fqdn> or set %s. There is deliberately no default: '
                 'see docs/api-fields.md.' % HOST_ENV)
    host = host.strip()
    if '/' in host or ':' in host:
        sys.exit('--host takes a bare FQDN, not a URL: got %r' % (host,))
    return host


def main(argv=None):
    """Everything a `python3 check_cvmetadata_schema.py ...` invocation does.

    :return: 0 when the contract holds, 1 when a required field is gone, 3 when the deployment could not be
             read. Nonzero in both failing cases, because under cron the exit code is the only alarm - and a
             check that could not run has not passed.
    """
    args = build_parser().parse_args(argv)
    configure_logging(args.log_file)
    host = resolve_host(args)
    try:
        served = fetch_served_fields(host, timeout=args.timeout)
    except SchemaUnavailable as e:
        print('cvMetadata schema check: https://%s%s' % (host, ENDPOINT_PATH))
        print('UNAVAILABLE: could not read the field names (%s). The check did not run.' % (e,))
        return EXIT_UNAVAILABLE
    return report(host, compare_served_fields(served))


if __name__ == '__main__':
    raise SystemExit(main())
