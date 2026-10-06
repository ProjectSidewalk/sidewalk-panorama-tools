#!/usr/bin/env python3
"""
Scraper log analyzer.

Downloads each city's log.csv from the pano store and analyzes it for potential issues:
  🔴 CRITICAL - scraper is likely broken (stale log, download failure)
  🟡 WARNING  - something unusual that warrants investigation
  🔵 INFO     - low-priority observations

and reports the depth backfill (#43): per city, how much of the GSV corpus has a depth verdict, the nightly
request rate and an ETA; fleet-wide, the same totals plus the cities with the longest road ahead.

Connection settings come from the environment (or the matching flags); nothing about the pano store is
hardcoded here. See docs/log-analyzer.md.

  PS_SFTP_HOST  required  host, or an ~/.ssh/config Host alias
  PS_SFTP_BASE  required  directory containing the per-city folders
  PS_SFTP_USER  optional  omit when the ssh config supplies it
  PS_SFTP_PORT  optional  omit for 22
  PS_SFTP_KEY   optional  omit to let ssh choose (ssh config / agent)

and cross-checks cities.csv against the fleet roster (#133), so a city nobody added here is loud rather
than simply unmonitored:

  PS_ROSTER_HOST  optional  deployment(s) serving /v3/api/cities, comma-separated, tried in order;
                            omit for sidewalk-sea, then sidewalk-chicago

Usage:
  python3 analyze.py                        # download + analyze all cities
  python3 analyze.py --no-download          # analyze already-downloaded logs
  python3 analyze.py --city seattle-wa      # single city only
  python3 analyze.py --stale-days 5         # custom staleness threshold
"""

import argparse
import csv
import importlib.util
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# The roster cross-check (#133), loaded by file location rather than by name. Running this as a script puts
# log_analyzer/ on sys.path so a plain `import roster` would work, but the tests load analyze.py with
# importlib.spec_from_file_location and no such entry; loading it the same way here works in both cases
# without mutating sys.path at import time (this module is deliberately side-effect-free to import).
_roster_spec = importlib.util.spec_from_file_location(
    "log_analyzer_roster", Path(__file__).resolve().parent / "roster.py")
roster = importlib.util.module_from_spec(_roster_spec)
_roster_spec.loader.exec_module(roster)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent
LOGS_DIR    = SCRIPT_DIR / "logs"
CITIES_FILE = SCRIPT_DIR / "cities.csv"

# Where the roster cross-check (#133) asks for /v3/api/cities, in order until one answers. Unlike the SFTP
# host and base, these can safely have a default: every deployment on the current release serves the same
# fleet-wide roster, so the choice of host decides only whether the check runs, never what it compares
# against - and a check that cannot run is CRITICAL in roster_check. Seattle first because it is the
# flagship and the first to get a release. The fallback is a second app stage, not a second machine: it
# covers one deployment being restarted or broken, not the app host being down. --roster-host /
# PS_ROSTER_HOST replace the whole list.
DEFAULT_ROSTER_HOSTS = ("sidewalk-sea.cs.washington.edu", "sidewalk-chicago.cs.washington.edu")

# ---------------------------------------------------------------------------
# log.csv format
# ---------------------------------------------------------------------------
# DownloadRunner appends 20 positional fields per run and never writes a header (see write_log_csv_row and
# the column table in docs/ops.md). Production files carry a header only because it is added by hand when a city is
# set up, so parsing must work either way - a forgotten header should not turn into a confusing parse error.
LOG_COLUMNS = [
    "start_time",
    "xml_success", "xml_fail", "xml_skip", "xml_total", "xml_minutes",
    "image_success", "image_fallback_success", "image_fail", "image_skip", "image_total", "image_minutes",
    "depth_success", "depth_fail", "depth_skip", "depth_total", "depth_minutes",
    "total_minutes",
    # The GSV corpus the depth phase was given (#43) - the denominator for everything depth_progress reports.
    # Appended last so no older position moved; blank on every row written before it existed.
    "depth_eligible",
    # This run's image attempts that RAISED (#182) - transient, unledgered, retried next run - the per-run figure
    # image_fail (cumulative, permanent + transient) cannot give. Rule 11 reads it. Blank on every row written
    # before it existed, and on a run whose image phase did not finish.
    "image_raised",
]

# Fields 2-18 are phase results: a completed run fills every one of them, so a blank there means the run
# ended early (#49). Field 19 is not a phase result and is blank on every pre-#43 row, so the ended-early
# rule must not read it - or the whole fleet reads as crashing for a week after the column arrives. Field 20
# (#182) is left out for the same reason: it is blank on every pre-#182 row.
PHASE_COLUMNS = LOG_COLUMNS[1:LOG_COLUMNS.index("depth_eligible")]

# The widths a log.csv row is allowed to have: 18 before the corpus column (#43) existed, 19 before the
# raised-attempts column (#182), 20 since. Any other width is a torn or corrupted write, and read_log counts
# them rather than reshaping them into a run.
LOG_ROW_WIDTHS = (LOG_COLUMNS.index("depth_eligible"), LOG_COLUMNS.index("image_raised"), len(LOG_COLUMNS))


def _widths_phrase() -> str:
    """The allowed row widths as the width messages say them: "not one of 18, 19 or 20"."""
    *head, last = LOG_ROW_WIDTHS
    return f"not one of {', '.join(str(w) for w in head)} or {last}"

# ---------------------------------------------------------------------------
# Thresholds (override via CLI flags where applicable)
# ---------------------------------------------------------------------------
STALE_DAYS_DEFAULT       = 3    # --stale-days
ZERO_PROGRESS_DAYS       = 30   # consecutive days with 0 new images before flagging
ZERO_PROGRESS_LOOKBACK   = 90   # days to look back when checking for prior progress
NEW_FAIL_DAILY_WARNING   = 20   # new image_fail entries/day (7-day avg) to flag
LONG_RUN_MULTIPLIER      = 3.0  # recent image-phase runtime > this × the median (floored) → warning
LONG_RUN_MIN_MEDIAN      = 5    # minutes; the median is floored here before the multiplier is applied
NEW_FAIL_NIGHTS          = 7    # nights (not rows) averaged by rule 2
RUNS_PER_NIGHT_INFO      = 8    # runs on one date above which rule 8 asks whether a stray schedule survives
RUNS_PER_NIGHT_LOOKBACK  = 14   # dates rule 8 looks back over
INCOMPLETE_RUN_WARNING   = 3    # incomplete runs in the last 7 before flagging
OVERLAP_TOLERANCE_MIN    = 1.0  # durations are whole minutes; a start this close to the previous end is rounding
DEPTH_STALLED_NIGHTS     = 3    # consecutive nights with no depth request, with work left, before flagging
DEPTH_RATE_NIGHTS        = 7    # nights the depth request rate (and so the ETA) is averaged over
DEPTH_BARREN_NIGHTS      = 3    # nights that made depth requests since the last SAVE, before rule 10 (not
                                # calendar nights: a night with no row or a stand-down row is no evidence).
                                # Same as DEPTH_STALLED_NIGHTS so "stalled" and "saving nothing" read alike; one
                                # night is already improbable by chance (below), so this is operator latency -
                                # a one-night network or store outage heals itself and must not page.
DEPTH_BARREN_MIN_REQUESTS = 10  # requests since the last save before 0 saves is evidence. Fleet 2026-09-18:
                                # 62,186 saved of 96,511 requests (64%; canary 301/500), so P(0 of 10) = 0.36^10
                                # ~ 4e-5 - a FLEET figure: per city the share ran 35% (washington-dc) to 100%
                                # on 2026-09-27, and at a 35% save share P(0 of 10) ~ 1.3%, which 3 requesting
                                # nights still make negligible. Measured outage shapes: 25/night (the
                                # breaker), 1,889/night (drift).
DEPTH_UNAVAILABLE_SHARE  = 0.5  # ledger growth / failures (excluding the newest row) at or above which the
                                # failures are being ledgered as `unavailable`. By construction 0.0 when they are
                                # transient (never ledgered, gsv.py) and 1.0 in the drift shape; midpoint = margin.
MALFORMED_RECENT_DAYS    = 7    # a torn row newer than this (or undatable) warns; older ones are one INFO line.
                                # 20 of 26 warnings on 2026-09-19 were rows dated 2022..2026-05 (#43 close-out).
                                # 7 as NEW_FAIL_NIGHTS and DEPTH_RATE_NIGHTS; 30 would re-alert a one-off tear
                                # for a month, and the INFO line keeps the total visible anyway.
TRANSIENT_FAIL_NIGHTS    = 7    # consecutive logged nights rule 11 needs (as NEW_FAIL_NIGHTS). UNMEASURED: #178's
                                # week of nights is the input that sizes this and the floor below.
TRANSIENT_FAIL_MIN_PER_NIGHT = 10  # raised image attempts per night (field 20) rule 11 needs on every one of
                                # them - DownloadRunner's IMAGE_NO_SUCCESS_MIN_RAISED, restated, not imported
                                # (this module shares no code with the runners). UNMEASURED (#178): it exists to
                                # keep a mature city's one or two perennial raisers quiet.
ZERO_PROGRESS_MIN_NEW_WORK = 3  # new image-eligible panos (field 5 growth) or unattempted ones (5 - 11) rule 3
                                # needs. 7.9-8.4% of a GSV ledger is a permanent verdict (2026-09-06), so k new
                                # panos all retired - no success, no regression - is ~0.084^k: 8% at 1, 0.06% at 3.


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def resolve_sftp(args) -> dict:
    """Resolve pano-store connection settings from CLI flags, falling back to the environment.

    Host and base path are required and deliberately have no defaults: they are deployment details, and a
    wrong default would silently analyze the wrong store. User/port/key are optional so an ~/.ssh/config
    Host alias can supply them instead.
    """
    settings = {
        "host": args.host or os.environ.get("PS_SFTP_HOST"),
        "base": args.base or os.environ.get("PS_SFTP_BASE"),
        "user": args.user or os.environ.get("PS_SFTP_USER"),
        "port": args.port or os.environ.get("PS_SFTP_PORT"),
        "key":  args.key  or os.environ.get("PS_SFTP_KEY"),
    }
    missing = [name for name in ("host", "base") if not settings[name]]
    if missing:
        sys.exit(
            "Missing pano store settings: {}.\n"
            "Set PS_SFTP_HOST / PS_SFTP_BASE (or pass --host / --base). "
            "See docs/log-analyzer.md.".format(
                ", ".join("PS_SFTP_" + name.upper() for name in missing))
        )
    if settings["key"]:
        settings["key"] = os.path.expanduser(settings["key"])
    return settings


def load_cities(path: Path) -> list[dict]:
    """Return list of {city_id, display_name} dicts from cities.csv."""
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def download_log(city_id: str, dest: Path, sftp: dict) -> bool:
    """
    Use sftp batch mode to pull {base}/{city_id}/log.csv.
    Returns True on success.

    We use sftp rather than scp because the server runs a restricted SFTP subsystem that doesn't support the
    SCP wire protocol (newer scp clients default to SFTP-over-SSH and trigger "mtime.sec not present" errors).
    """
    remote_path = f"{sftp['base']}/{city_id}/log.csv"
    destination = f"{sftp['user']}@{sftp['host']}" if sftp["user"] else sftp["host"]

    cmd = ["sftp"]
    if sftp["port"]:
        cmd += ["-P", str(sftp["port"])]
    if sftp["key"]:
        cmd += ["-i", sftp["key"]]
    cmd += [
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",   # never prompt for a password
        "-b", "-",               # read commands from stdin
        destination,
    ]
    batch = f"get {remote_path} {dest}\n"
    result = subprocess.run(cmd, input=batch, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout).strip()
        print(f"    sftp error: {err}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_FULL_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")   # rule 9's datable torn-row stamp


def read_log(log_path: Path) -> pd.DataFrame:
    """Read a log.csv into a sorted DataFrame with LOG_COLUMNS, header row optional.

    Rows are positional. They are read with the csv module and padded or truncated to len(LOG_COLUMNS) BEFORE
    pandas sees them - the intake discipline #72 gave the runners, for the same reason: nothing about the
    file's shape may be inferred. This was a read_csv(names=LOG_COLUMNS) call, and measured on pandas 3.0.5
    that cannot read the file the 19th column (#43) leaves behind on every production city - a hand-written
    18-name header, years of 18-field rows, then 19-field rows - under either engine: the C parser fixes the
    width from the first row and dies with "Expected 18 fields, saw 19", so every city would have read as
    CRITICAL "Could not parse log" the morning after the deploy, and stayed there. Padding by hand also
    changes the direction of the other failure in that class: read_csv answers a row one field WIDER than the
    names by taking the first column as the index and shifting every count one place left (the #46 shape),
    while padding shifts it right instead. Neither is a run, so rows whose width is not in LOG_ROW_WIDTHS are
    counted and reported (rule 9) and left OUT of the frame - not reshaped into one.

    Blank fields (a run that crashed or was stopped before that phase finished, see #49) stay NaN: missing
    data, never a fabricated 0. So do field 19 and field 20 on every row older than each (#43, #182) - the
    production files hold 18-, 19- and 20-field rows under one hand-written 18-name header.
    """
    width = len(LOG_COLUMNS)
    # encoding is explicit: read_csv defaulted to UTF-8, open() takes the locale's, and a cron tool should
    # not read a different file on a differently-configured host.
    with open(log_path, newline="", encoding="utf-8") as f:
        rows = [[cell.strip() for cell in row] for row in csv.reader(f)]
    # write_log_csv_row prefixes every row with "\n", so a fresh file opens with an empty line; and the header
    # is optional - never written by the runner, added by hand at city setup.
    rows = [row for row in rows if any(row)]
    if rows and rows[0][0] == "start_time":
        rows = rows[1:]
    # Counted BEFORE padding, because this is the only place the evidence exists. A row of some other width
    # is not a run: one stray comma reaching a written value shifts every count one place right, the 19th
    # field falls off the end, and the night reads as a healthy all-zero run - while whatever landed in
    # position 19 becomes the city's corpus via last_value and can report a 183,680-pano city complete. The
    # docstring above used to claim hand-padding "removes" the wider-row failure; it changes the shift's
    # direction. So the width is reported (rule 9) AND the row is dropped: the first version counted it and
    # then padded it into the frame anyway, and a stray comma after field 5 turned a 183,680-pano city into
    # `0 total | depth 0/12` with rule 8 silent, because 12 (its shifted total_minutes) is a believable
    # corpus. A torn write - the realistic case, a row cut short by the mount dropping - is dropped too; it
    # was reading as a crashed run, which is a fair description but not evidence the row can be trusted for.
    malformed = sum(1 for row in rows if len(row) not in LOG_ROW_WIDTHS)
    # Field 1 of a torn row is still trusted for one thing: dating it, so rule 9 can tell tonight's tear from
    # one years old (#163). The stamp is written first and a tear is at the tail, so the stamp is the part
    # that survives; a tear that did cut it parses as NaT, and rule 9 counts that as recent. ISO8601 accepts
    # a PREFIX, though - `2026` parses as 2026-01-01 and `2026-09-2` as 2026-09-02 - so a stamp cut inside
    # its first 19 bytes would be dated months back and filed as history. Only a stamp carrying the whole
    # `YYYY-MM-DD HH:MM:SS` is trusted; anything shorter is undatable (#169 review).
    torn_starts = [row[0] if _FULL_STAMP.match(row[0]) else None
                   for row in rows if len(row) not in LOG_ROW_WIDTHS]
    rows = [(row + [""] * width)[:width] for row in rows if len(row) in LOG_ROW_WIDTHS]

    # dtype=object, so pandas does not get to pick a string dtype whose missing-value semantics differ across
    # versions; every column below is converted explicitly.
    df = pd.DataFrame(rows, columns=LOG_COLUMNS, dtype=object)
    for column in LOG_COLUMNS[1:]:
        df[column] = pd.to_numeric(df[column].where(df[column] != "", None), errors="coerce")

    # Parsed here rather than with read_csv's parse_dates=, which gives up on a column it cannot read
    # *uniformly* and hands back the raw strings - no exception, no NaT. The notna() filter below then keeps
    # the junk, and analyze_city dies on `.dt` a few lines later with "Can only use .dt accessor with
    # datetimelike values". main() has no per-city try/except, so one bad row ends the report for every city
    # after it, and those cities stop being monitored with nothing to say so.
    #
    # format="ISO8601" rather than leaving pandas to infer, for two reasons that both end in silently
    # discarded runs. Historic rows were written as str(datetime.now()), which omits the ".ffffff" when the
    # microsecond lands on exactly 0, so a long-lived log holds both widths; and rows written since #101
    # carry a UTC offset the older ones do not, so a long-lived log holds both shapes as well. Inference
    # locks onto the first form it sees and coerces every row of the other to NaT. ISO8601 accepts all four
    # combinations, and coerces only genuine garbage.
    #
    # utc=True is what makes those two eras comparable. It converts an offset-carrying row to UTC and reads a
    # bare one as UTC, which is exactly right: every scraper host has run UTC (docs/downloader.md), so a
    # legacy naive row IS a UTC row. Without it a file holding both shapes comes back as object dtype and
    # every comparison below raises on the mixture. The column is tz-aware from here on, so `now` is too.
    df["start_time"] = pd.to_datetime(df["start_time"], errors="coerce", format="ISO8601", utc=True)

    # A run whose timestamp is unparseable can't be placed in time; nothing below can use it.
    df = df[df["start_time"].notna()]
    result = df.sort_values("start_time").reset_index(drop=True)
    result.attrs["malformed_rows"] = malformed
    result.attrs["malformed_starts"] = tuple(pd.to_datetime(
        pd.Series(torn_starts, dtype=object), errors="coerce", format="ISO8601", utc=True))
    return result


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_city(city_id: str, log_path: Path, stale_days: int) -> list[dict]:
    """
    Analyze one city's log file.  Returns a list of issue dicts:
        {"level": "CRITICAL"|"WARNING"|"INFO", "msg": str}
    """
    issues = []

    # --- Load ---
    if not log_path.exists():
        return [{"level": "CRITICAL", "msg": "Log file missing (download failed?)"}]

    try:
        df = read_log(log_path)
    except Exception as exc:
        return [{"level": "CRITICAL", "msg": f"Could not parse log: {exc}"}]

    if df.empty:
        # A file of nothing but torn rows is not "empty": the evidence that something wrote to it is the
        # malformed count, and it would be lost here since rule 9 runs on the frame the rows were dropped from.
        malformed = df.attrs.get("malformed_rows", 0)
        if malformed:
            return [{"level": "CRITICAL",
                     "msg": (f"Log file has no readable run: {malformed} row(s) have a field count that is "
                             f"{_widths_phrase()}")}]
        return [{"level": "CRITICAL", "msg": "Log file is empty"}]

    # Convenience: calendar date column, in UTC. Rules 2, 3 and 8 and depth_progress's rate all group by it,
    # so where the day boundary falls matters to them: it is safe under the Pacific night window the queue
    # runs in (#101), since 20:00-05:00 PT is 03:00-12:00 UTC (04:00-13:00 under PST) and a night therefore
    # sits entirely inside one UTC day. What is NO LONGER true is "one row per date" - the queue's extra
    # passes put several rows on a night by design, which is why every rule below counts dates rather than
    # rows.
    df["date"] = df["start_time"].dt.normalize()

    # --- 1. Staleness ---
    # Both sides are tz-aware UTC: read_log normalises the column, so this is an exact comparison rather
    # than one that was off by the scraper host's UTC offset and got away with it at a multi-day threshold
    # (#101). It stays exact if the fleet moves to a Pacific-pinned schedule, or to a host that isn't on UTC.
    now = datetime.now(timezone.utc)
    last_ts  = df["start_time"].iloc[-1]
    days_old = (now - last_ts).days

    if days_old > stale_days:
        issues.append({
            "level": "CRITICAL",
            "msg": (
                f"Last log entry is {days_old} days old "
                f"(last run: {last_ts.strftime('%Y-%m-%d')})"
            ),
        })

    # --- 2. Rapidly growing image failures ---
    # image_fail is a cumulative count of permanently-failed images.
    # We compute the per-row delta and look at the recent 7-day average.
    if len(df) > 1:
        df["fail_delta"] = df["image_fail"].diff().clip(lower=0)  # ignore drops (retries succeed)
        # Summed per DATE and averaged over dates. tail(7) was seven ROWS, and the queue's extra passes (#43)
        # put more than one row on a night, so seven rows is three and a half nights at two passes each and a
        # real 30/day failure rate read as 15 and never reached the threshold. Rule 6 was rewritten for
        # exactly this reason; this rule and rule 3 were left behind.
        recent_nights = df.groupby("date")["fail_delta"].sum().tail(NEW_FAIL_NIGHTS)
        avg_new_fails = recent_nights.mean()
        if pd.notna(avg_new_fails) and avg_new_fails >= NEW_FAIL_DAILY_WARNING:
            issues.append({
                "level": "WARNING",
                "msg": (
                    f"Image failures growing fast: "
                    f"~{avg_new_fails:.0f} new permanent failures/day (7-day avg). "
                    f"Total failures: {fmt_count(last_value(df, 'image_fail'))}"
                ),
            })

    # --- 3. Extended zero progress (regression check) ---
    # Flag only if the city *used to* download images but has stopped.
    # We check: last ZERO_PROGRESS_DAYS all-zero AND the preceding
    # ZERO_PROGRESS_LOOKBACK days had at least some success.
    df["daily_success"] = df["image_success"] + df["image_fallback_success"]
    # Per DATE, for rule 2's reason: these slices are named in days and were counted in rows, so "no new
    # images in 30 days" fired after about fifteen of them once a city started running twice a night.
    by_night = df.groupby("date")["daily_success"].sum()
    n = len(by_night)

    if n > ZERO_PROGRESS_DAYS + ZERO_PROGRESS_LOOKBACK:
        tail_n   = by_night.tail(ZERO_PROGRESS_DAYS)
        prior_n  = by_night.iloc[-(ZERO_PROGRESS_DAYS + ZERO_PROGRESS_LOOKBACK) : -ZERO_PROGRESS_DAYS]

        tail_all_zero  = (tail_n == 0).all()
        prior_had_some = (prior_n > 0).any()

        if tail_all_zero and prior_had_some:
            # ...and there was something to fetch (#163). A mature city with no new panos downloads nothing
            # and is healthy; three of 2026-09-19's warnings were exactly that. The evidence is field 5
            # (xml_total = len(image_pano_infos), the image-eligible corpus), NOT field 11: image_total is
            # prior + tonight's ATTEMPTS, so panos a starved image phase never reaches do not grow it, and
            # "field 11 did not grow" is silent on precisely the regression this rule exists for. Two arms:
            # field 5 grew from the last logged night before the window to the newest night in it, or
            # field 5 - field 11 on the newest row carrying both (a lower bound on eligible panos never
            # attempted) is at least the minimum. The bound is loose by the ledger rows no longer in the list:
            # 5 - 11 was -1 in 50 of 56 cities on 2026-09-27, so the arm needs 4 real unattempted panos
            # there, and -1,460 in chicago-il, where only the growth arm can fire. A corpus nobody wrote
            # (field 5 blank) is unknown, not zero, and keeps the old behaviour: fire.
            #
            # A written 0 is unknown too. Field 5 is len(image_pano_infos) with no empty-list guard, so an empty
            # /adminapi/panos answer writes a plausible 0 - the value corpus_size refuses in field 19 for the
            # same reason. Taken at face value it read a 0 on the baseline night as `1,000 new panos`, and a
            # month of empty answers as a flat corpus with nothing to fetch; for a Mapillary- or Panoramax-only
            # city (field 19 legitimately 0) nothing else would report the second (#169 review).
            xml_total = df["xml_total"].where(df["xml_total"] > 0)
            xml_by_night = xml_total.groupby(df["date"]).last()   # last() skips NaN
            before = xml_by_night.iloc[:-ZERO_PROGRESS_DAYS].dropna()
            during = xml_by_night.tail(ZERO_PROGRESS_DAYS).dropna()
            growth = None if before.empty or during.empty else int(during.iloc[-1] - before.iloc[-1])
            both = df[xml_total.notna() & df["image_total"].notna()]
            backlog = int(both["xml_total"].iloc[-1] - both["image_total"].iloc[-1]) if not both.empty else None
            evidence = []
            if growth is None:
                evidence.append("the image-eligible corpus size is unknown (field 5 blank or 0)")
            elif growth >= ZERO_PROGRESS_MIN_NEW_WORK:
                evidence.append(f"{growth:,} new image-eligible panos (field 5, "
                                f"{int(before.iloc[-1]):,} → {int(during.iloc[-1]):,})")
            if backlog is not None and backlog >= ZERO_PROGRESS_MIN_NEW_WORK:
                evidence.append(f"{backlog:,} eligible panos never attempted on the newest run "
                                f"(field 5 − field 11)")
            last_success_mask = df["daily_success"] > 0
            if evidence and last_success_mask.any():
                last_success_date = df.loc[last_success_mask, "date"].iloc[-1]
                issues.append({
                    "level": "WARNING",
                    "msg": (
                        f"No new images downloaded in {ZERO_PROGRESS_DAYS} days "
                        f"(last success: {last_success_date.strftime('%Y-%m-%d')}) though there was work: "
                        f"{' and '.join(evidence)}. Check the image phase's budget (--min-depth-runtime at "
                        f"or above --max-runtime downloads nothing) and scrape.log."
                    ),
                })

    # --- 11. A steady set of transient image failures (#182) ---
    # A pano that raises is never ledgered, so it is retried - and raises - every night. Field 9 is cumulative
    # and mixes those raises with permanent verdicts, so a steady set adds the same number every night and no
    # other rule sees it a week in (rule 3's gate cannot tell it from a mature city with nothing new; #169 D13).
    # Field 20 is the per-run count. Per night it is the MAX of the night's rows, not the sum: the queue's extra
    # passes re-attempt the same unledgered set, so summing would count one set twice. A night whose field 20
    # is blank (a row before #182, or an image phase that did not finish) is no evidence, and breaks the run:
    # NaN is never read as 0, nor as over the floor. Independent of rule 3's entry gate - it needs no 30-day
    # quiet tail and no 90-day lookback, only TRANSIENT_FAIL_NIGHTS logged nights with nothing downloaded.
    raised_by_night = df.groupby("date")["image_raised"].max()
    raised_tail = raised_by_night.tail(TRANSIENT_FAIL_NIGHTS)
    if (len(raised_tail) == TRANSIENT_FAIL_NIGHTS and raised_tail.notna().all()
            and (raised_tail >= TRANSIENT_FAIL_MIN_PER_NIGHT).all()
            and by_night.reindex(raised_tail.index).eq(0).all()):
        lo, hi = int(raised_tail.min()), int(raised_tail.max())
        span = f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
        issues.append({
            "level": "WARNING",
            "msg": (
                f"{span} image attempts raised on each of the last {TRANSIENT_FAIL_NIGHTS} nights with no "
                f"download (field 20): a steady set of panos failing transiently - nothing is ledgered, so "
                f"they retry every night. Look at the IMAGEDOWNLOAD errors in scrape.log."
            ),
        })

    # --- 4. Abnormally long runtime, outside the depth phase ---
    # total_minutes used to be compared whole. The depth phase is budget-driven - it runs to whatever
    # --max-runtime it is handed, and under the queue's extra passes (#43) that varies by design from one slot
    # to ten - so a city in backfill would trip this every time its allocation grew. What CAN run unexpectedly
    # long is the image phase, which is work-driven; the depth minutes come out first. A row with either
    # duration blank (#49) is NaN here and compares false, so a crashed run neither trips nor moves the median.
    # Read from image_minutes (field 12) directly rather than total minus depth. That subtraction was the
    # difference of two INDEPENDENTLY ROUNDED integers, so a run of 12.4 total and 11.6 depth gave 0 where
    # the image phase really took a minute - and for a mature city, whose image phase is a ledger read and a
    # membership scan while depth spends the whole slot, it gave 0 on essentially every row.
    median_mins = df["image_minutes"].median()
    if pd.notna(median_mins):
        # The median is FLOORED before the multiplier, and that floor is what makes the rule work at all.
        # With a median of 0 the old `median_mins > 0` guard was false and the rule never evaluated: a hung
        # image phase was unreportable for exactly the class of city the fleet is moving into (measured: a
        # 192-minute run returned no issue at all). At the other end, a median of 1 made the threshold 3
        # minutes and an ordinary 4-minute image phase warned. One floor fixes both ends.
        threshold = max(median_mins, LONG_RUN_MIN_MEDIAN) * LONG_RUN_MULTIPLIER
        recent_7  = df.tail(7)
        long_runs = recent_7[recent_7["image_minutes"] > threshold]
        if not long_runs.empty:
            worst = long_runs["image_minutes"].max()
            issues.append({
                "level": "WARNING",
                "msg": (
                    f"Recent unusually long image phase: {worst:.0f} min "
                    f"(historical median: {median_mins:.0f} min, "
                    f"threshold: {threshold:.0f} min)"
                ),
            })

    # --- 5. Runs that ended early ---
    # A run that crashed or was stopped leaves every field from its first unfinished phase onward blank (#49).
    # None of the checks above can see those runs - NaN compares false everywhere - so a city that dies every
    # night would otherwise look healthy right up until its log goes stale. PHASE_COLUMNS, not every column:
    # field 19 is blank on every pre-#43 row, which is not a crash.
    recent_7    = df.tail(7)
    incomplete  = recent_7[recent_7[PHASE_COLUMNS].isna().any(axis=1)]
    if len(incomplete) >= INCOMPLETE_RUN_WARNING:
        issues.append({
            "level": "WARNING",
            "msg": (
                f"{len(incomplete)} of the last {len(recent_7)} runs ended early "
                f"(blank columns). Check scrape.log next to log.csv."
            ),
        })

    # --- 6. Overlapping runs (last 30 runs) ---
    # Was "more than one run on a calendar day", at INFO. The queue's extra passes (#43) run a city more than
    # once a night by design, so that rule would fire for most of the fleet nightly and mean nothing. What
    # two rows on one day were a proxy for is two PROCESSES on one store directory at once, racing on the
    # same ledgers - and that is readable directly: a run that starts before the previous one's recorded end.
    # Durations are whole minutes, rounded, so a start within OVERLAP_TOLERANCE_MIN of the previous end is
    # the rounding, not an overlap: a 11.6-minute pass logged as 12 followed straight away by its second pass
    # would otherwise read as a 24-second collision. A crashed row has no duration (NaN) and so no end.
    recent_30 = df.tail(30)
    starts    = recent_30["start_time"]
    ends      = starts + pd.to_timedelta(recent_30["total_minutes"], unit="m")
    tolerance = pd.Timedelta(minutes=OVERLAP_TOLERANCE_MIN)
    overlaps  = [start.strftime("%Y-%m-%d %H:%M")
                 for start, prev_end in zip(starts.iloc[1:], ends.iloc[:-1])
                 if pd.notna(prev_end) and start + tolerance < prev_end]
    if overlaps:
        issues.append({
            "level": "WARNING",
            "msg": (
                f"Overlapping runs (last 30): a run started before the previous one ended at "
                f"{', '.join(overlaps)} - two processes on one store directory race on the ledgers"
            ),
        })

    # --- 7. Depth backfill stalled ---
    # Nothing above can see the depth phase: five zeros in its columns is what --skip-depth writes, what the
    # block latch writes when it stands a run down, what an unwritable ledger writes - and what a city that is
    # simply finished writes too. Only the corpus size (field 19) tells those apart, which is why it exists.
    progress = depth_progress(df)
    if progress and progress["unresolved"] > 0 and progress["quiet_nights"] >= DEPTH_STALLED_NIGHTS:
        # Both arms name candidates rather than asserting one, because the row cannot tell them apart and
        # asserting was wrong on BOTH shapes this diagnosis exists to separate. An unwritable ledger does not
        # write five zeros - gsv.download_depth_maps returns (0, 0, skipped, skipped) when it cannot open the
        # ledger - so it landed in the first arm and sent the operator to tune a runtime flag while the store
        # was read-only. And a fresh city whose image phase ate the whole budget has nothing in the ledger to
        # skip, so it landed in the second and was told the phase had not run when it had.
        if progress["newest_accounted"]:
            cause = ("the phase accounted for panos but made no requests: it ran out of budget (check "
                     "--min-depth-runtime), or the ledger could not be written")
        else:
            cause = ("the phase accounted for nothing: --skip-depth, a block latch, streetlevel missing, the "
                     "image phase spending the whole budget on a city with an empty ledger, or a run that "
                     "crashed before the phase - deliberate if --skip-depth is set")
        issues.append({
            "level": "WARNING",
            "msg": (
                f"Depth backfill stalled: no depth requests on the last {progress['quiet_nights']} nights "
                f"with {progress['unresolved']:,} of {progress['eligible']:,} panos unresolved ({cause}). "
                f"Check scrape.log next to log.csv."
            ),
        })

    # --- 10. Depth phase requesting and saving nothing (#163) ---
    # Numbered 10 so no existing rule moves; it sits here because it reads the same `progress`. Rule 7 counts
    # requests, so a phase whose every request fails resets it nightly: measured, 10 nights of 25 transient
    # failures and 10 nights of 1,889 requests all written off as `unavailable` both returned no issue. The
    # two shapes are told apart by whether the failures come back as skips (depth_progress). The ledgering
    # arm is CRITICAL because it is permanent: an `unavailable` row is never re-requested, so every night it
    # runs costs those panos their depth until someone scrubs the ledger. The transient arm loses nothing -
    # those panos retry - so it is a WARNING, rule 7's tier.
    #
    # The count is of nights that ASKED (unsaved_request_nights), so the share is always a number here: three
    # requesting nights are at least two requesting rows before the newest (depth_progress). The first version
    # counted calendar nights and so could reach this with one requesting row, and carried a third message
    # for "cannot be classified yet"; that state is no longer reachable (#169 review).
    if progress and progress["barren"]:
        asked = progress["unsaved_request_nights"]
        since = progress["unsaved_nights"]
        # A city that never saved has no `last save` to count from: unsaved_nights is then the log's span + 1,
        # so name the span and say there was no save rather than citing one (#169 final review).
        if progress["ever_saved"]:
            tail = f", over {since} nights since the last save" if since != asked else ""
        else:
            tail = (f", over {since} nights" if since != asked else "") + ", with no save in the log"
        nights = f"on the last {asked} nights it made requests{tail}"
        share = progress["unavailable_share"]
        if share >= DEPTH_UNAVAILABLE_SHARE:
            issues.append({
                "level": "CRITICAL",
                "msg": (
                    f"Depth phase saved nothing {nights}, and is writing panos off: "
                    f"{progress['written_off']:,} of the {progress['failed_before_newest']:,} requests that "
                    f"failed before the newest run are now `unavailable` rows in depth_log.csv, never to be "
                    f"re-requested. The fleet saves ~60% of its requests on a healthy night, so this is "
                    f"upstream drift (streetlevel or the depth payload), not the panos. Put --skip-depth back "
                    f"after the `--` in the crontab and scrub the ledger before the next run - docs/ops.md, "
                    f"When the depth phase saves nothing."
                ),
            })
        else:
            tail = ("None of it was ledgered, so those panos retry - but the backfill is not moving."
                    if not progress["written_off"] else
                    f"Only {progress['written_off']:,} of the failures were ledgered as `unavailable`; the "
                    f"rest retry - but the backfill is not moving.")
            issues.append({
                "level": "WARNING",
                "msg": (
                    f"Depth phase saved nothing {nights}: {progress['unsaved_requests']:,} "
                    f"requests, every one failed, with {progress['unresolved']:,} of "
                    f"{progress['eligible']:,} panos unresolved. {tail} Candidates: the consecutive-failure "
                    f"breaker (network, or a full or unmounted store), Google refusing requests, or a depth "
                    f"payload streetlevel can no longer read - the DEPTHDOWNLOAD lines in scrape.log name "
                    f"which."
                ),
            })

    # --- 8. The corpus column contradicts the ledger ---
    # depth_eligible is len(gsv_panos), so an empty or source-less pano-list answer writes a plausible 0 and
    # erases the city's whole depth report. corpus_size refuses such a value; this is where that refusal is
    # said out loud, because a silently substituted corpus is the same class of quiet wrongness.
    if progress and progress["corpus_suspect"]:
        issues.append({
            "level": "WARNING",
            "msg": (
                f"Newest GSV corpus size is not believable (field 19); measuring against "
                f"{progress['eligible']:,} from an earlier run. A 0 here is what an empty or source-less "
                f"/adminapi/panos answer writes - check the pano-list fetch in scrape.log."
            ),
        })

    # --- 9. Rows that are not runs ---
    # Only a RECENT torn row is news (#163). Counted over the whole file, a tear from 2022 warned every
    # morning: 20 of the 26 warnings on 2026-09-19 were rows dated 2022..2026-05, which teaches the reader to
    # skip the WARNING tier. Dated by field 1 on rule 1's clock; an undatable row counts as recent, because a
    # tear can cut the stamp itself and that is exactly tonight's row. Older rows are one INFO line with the
    # total, so the count stays visible without alerting.
    malformed = df.attrs.get("malformed_rows", 0)
    if malformed:
        starts = df.attrs.get("malformed_starts", ())
        cutoff = now - timedelta(days=MALFORMED_RECENT_DAYS)
        recent = [t for t in starts if pd.isna(t) or t >= cutoff]
        undatable = sum(1 for t in recent if pd.isna(t))
        widths = _widths_phrase()
        if recent:
            undated = f" ({undatable} with no readable timestamp)" if undatable else ""
            issues.append({
                "level": "WARNING",
                "msg": (
                    f"{len(recent)} of {malformed} row(s) have a field count that is {widths} and are from "
                    f"the last {MALFORMED_RECENT_DAYS} days{undated} - a torn or corrupted write. Every count "
                    f"in such a row is shifted, so it is left out of every figure above rather than read as "
                    f"a run."
                ),
            })
        else:
            issues.append({
                "level": "INFO",
                "msg": (
                    f"{malformed} historical row(s) have a field count that is {widths}, the newest dated "
                    f"{max(starts):%Y-%m-%d} - old torn writes, already left out of every figure. Recorded, "
                    f"not alerted: only a row from the last {MALFORMED_RECENT_DAYS} days warns."
                ),
            })

    return issues


def last_value(df: pd.DataFrame, column: str):
    """Most recent non-blank value in a column, or None when every run left it blank."""
    values = df[column].dropna()
    return None if values.empty else values.iloc[-1]


def corpus_size(df: pd.DataFrame):
    """The GSV corpus to measure the backfill against, and whether it had to be taken from an older row.

    last_value skips a BLANK field - a run that crashed before the phase - but 0 is not blank, and 0 is
    exactly what a degenerate pano-list fetch writes: depth_eligible is len(gsv_panos), so an empty or
    source-less answer from /adminapi/panos leaves a legitimate-looking `...,0` row. Taken at face value that
    erased the city's entire depth report on one bad night - the stats line asserted it had no GSV panos, it
    vanished from the fleet block and its `longest remaining` ranking, and nothing anywhere said why. The
    impossible state it produces (resolved > eligible) raised no complaint either, because unresolved is
    floored at 0.

    The test is 0 and nothing else, deliberately. A corpus SHRINKS legitimately as Google retires panos, and
    it can fall below what the ledger already holds - those panos were resolved when they were still in the
    corpus. So "smaller than resolved" is not evidence of anything, and a percentage-drop rule would be a
    threshold with no measurement behind it. 0 is the one value that cannot be a real corpus for a city that
    has ever reported one, and it is exactly what the observed failure writes.

    A city that genuinely has no GSV panos has never reported one, so it still reports 0 and is not flagged.
    """
    values = df["depth_eligible"].dropna()
    if values.empty:
        return None, False
    newest = int(values.iloc[-1])
    if newest > 0:
        return newest, False
    positive = [int(v) for v in values if v > 0]
    return (positive[-1], True) if positive else (0, False)


def depth_progress(df: pd.DataFrame):
    """What the depth backfill looks like for one city, from log.csv alone (#43) - or None when no row carries
    field 19 yet, because nothing below can be computed without the corpus size.

    The definition of each figure is the part that matters:

      eligible     the GSV corpus (field 19), from the newest row that can be true - see corpus_size.
      resolved     LEDGER-DERIVED: skip + success (fields 15 and 13) of the newest row on which the phase
                   actually RAN. NOT depth_total, which adds depth_fail - and depth_fail carries TRANSIENT
                   failures, which are deliberately not ledgered and are re-requested next run (the ledger
                   semantics in docs/depth.md). Counting them let a city report "depth complete" with panos
                   that have no artifact and never will, and made the figure run BACKWARDS when a night of
                   heavy transient failure was followed by a quiet one. The cost is a one-night lag: this
                   run's `unavailable` verdicts are permanent and ARE ledgered, but the row cannot separate
                   them from transient ones inside depth_fail, so they only count once the next run reads
                   them back as skip. A lower bound that converges is the safe direction - it can delay
                   "complete", never assert it falsely. A row whose five depth fields are all 0 is not
                   "0 resolved" - that is the shape of a phase that never ran - and reading it as zero
                   progress would report a city un-backfilled the morning after one stand-down.
      unresolved   eligible - resolved, floored at 0: the corpus can shrink between runs.
      nightly      requests per CALENDAR night: success + failed summed per date, over the last
                   DEPTH_RATE_NIGHTS dates ending at the newest one in the log, nights with no row counted
                   as 0. Not per row (the queue runs a city more than once a night, #43) and not per LOGGED
                   date (a city only gets a slot on the nights the window reaches it, so averaging the dates
                   that happen to appear read three runs spread over a month as three consecutive nights -
                   measured 10x optimistic, and biased against exactly the starved cities the fleet block
                   exists to surface). The divisor is the window's SPAN - the nights the log actually
                   covers, capped at DEPTH_RATE_NIGHTS - the same divisor as nightly_resolved, so the two
                   rates can be read against each other: dividing requests by 7 and panos by the span
                   printed a two-night-old city as `+590 panos/night on +169 requests`.
      nightly_resolved
                   panos newly resolved per calendar night over the same window and span. The ETA's
                   denominator has to be this and not `nightly`: a night of heavy transient failure spends
                   requests and resolves nothing, and dividing panos by requests reads it as a productive
                   night.
      nights_left  unresolved / nightly_resolved, or None when either is 0. Undefined is not zero.
      quiet_nights CALENDAR nights since the newest date that saw a depth request, uncapped. Counting rows
                   made the alarm's latency scale with how often a city runs - three quarterly rows read as
                   "the last 3 nights" when it had been 90 days - and printed a row count as a night count.
      newest_accounted
                   whether the newest row's phase ACCOUNTED for any pano (depth_total > 0). Deliberately not
                   called "ran": an unwritable ledger returns (0, 0, skipped, skipped) (gsv.py), so it
                   accounts for panos without making a request, and a fresh city whose image phase ate the
                   budget accounts for nothing while having run. The row cannot separate those, so rule 7
                   names the candidates instead of asserting one.
      corpus_suspect
                   whether `eligible` had to be taken from an older row than the newest - see corpus_size.
      unsaved_nights
                   CALENDAR nights since the newest date with a depth SAVE (depth_success > 0), on
                   quiet_nights' convention: log span + 1 when the city never saved.
      unsaved_requests
                   requests (success + failed) on the rows after that date - all of them failures.
      unsaved_request_nights
                   the dates after that one on which the phase made at least one request: the nights that
                   asked and saved nothing. What rule 10 counts - a night with no row or a stand-down row is
                   no evidence the phase is failing. Also the stats line's count, so the two agree.
      ever_saved   whether the log holds any save at all; without one rule 10 names no `last save`.
      written_off  what the ledger (skip + success) gained between the first and the newest row of that span
                   that made requests, floored at 0 (a corpus can shrink). The `unavailable` verdicts, read
                   back one run late.
      failed_before_newest
                   depth_fail summed over the same requesting rows except the newest, whose verdicts cannot
                   have come back as skips yet.
      unavailable_share
                   written_off / failed_before_newest, or None when there is nothing to divide by - fewer than
                   two requesting rows, so one-night lag leaves the failures unclassifiable. Never None when
                   barren: DEPTH_BARREN_NIGHTS (3) requesting nights are at least two requesting rows before
                   the newest, and every request on them failed.
      barren       rule 10 (#163): the phase is asking and saving nothing. True when panos are unresolved,
                   unsaved_request_nights >= DEPTH_BARREN_NIGHTS, unsaved_requests >= DEPTH_BARREN_MIN_REQUESTS,
                   quiet_nights < DEPTH_STALLED_NIGHTS (once requests stop the night is rule 7's, so the two
                   never report one outage twice), and the newest requesting row did NOT walk its whole list
                   (depth_total < depth_eligible). That last guard is what keeps the ordinary end of a
                   backfill quiet: a handful of stragglers that fail every night are reached every night.
                   quiet_nights cannot see this shape at all - it counts requests, and a failed request is a
                   request - which is why a phase whose every attempt failed read as healthy before #163.
    """
    ran = df[df["depth_total"] > 0]  # NaN compares false: a crashed row neither ran nor resolved anything
    ledgered = ran["depth_skip"].fillna(0) + ran["depth_success"].fillna(0)
    resolved = int(ledgered.iloc[-1]) if not ran.empty else 0

    eligible, corpus_suspect = corpus_size(df)
    if eligible is None:
        return None
    unresolved = max(eligible - resolved, 0)

    requests  = df["depth_success"].fillna(0) + df["depth_fail"].fillna(0)
    per_night = requests.groupby(df["start_time"].dt.normalize()).sum()
    newest    = per_night.index.max()
    window    = pd.date_range(end=newest, periods=DEPTH_RATE_NIGHTS, freq="D")
    # Both rates divide by the window's own span - the calendar nights the log covers, capped at the window -
    # so a city three nights into its log is not reported at three-sevenths of its true rate, and requests
    # and panos are measured over the same nights. A night before the log's first row contributes nothing
    # to either sum, so `reindex(fill_value=0)` keeps every night the log DOES cover in the denominator.
    span      = min(DEPTH_RATE_NIGHTS, (newest - per_night.index.min()).days + 1)
    recent    = per_night.reindex(window, fill_value=0)
    nightly   = float(recent.sum()) / span

    # The rate the ETA divides by is panos-per-night, measured as what the ledger gained across the window.
    earlier  = ledgered[ran["start_time"] < window[0]]
    baseline = int(earlier.iloc[-1]) if not earlier.empty else 0
    nightly_resolved = max(resolved - baseline, 0) / span
    nights_left = unresolved / nightly_resolved if unresolved and nightly_resolved > 0 else None

    # Calendar nights since the last request, not a count of quiet rows.
    with_requests = per_night[per_night > 0]
    quiet_nights = (int((newest - with_requests.index.max()).days) if not with_requests.empty
                    else int((newest - per_night.index.min()).days) + 1)

    # Calendar nights since the last SAVE (rule 10), on quiet_nights' convention: a city that never saved
    # counts from its first night, +1, so a first night can never fire and a never-saved city fires on its
    # DEPTH_BARREN_NIGHTS-th. A request is not progress; only a save is.
    date = df["start_time"].dt.normalize()
    saves = df["depth_success"].fillna(0).groupby(date).sum()
    saved = saves[saves > 0]
    last_save = saved.index.max() if not saved.empty else None
    unsaved_nights = int((newest - last_save).days if last_save is not None
                         else (newest - per_night.index.min()).days + 1)
    in_span = (date > last_save) if last_save is not None else pd.Series(True, index=df.index)
    unsaved_requests = int(requests[in_span].sum())
    # The nights that ASKED and saved nothing - what rule 10 counts, not unsaved_nights. A night with no row,
    # or a five-zero stand-down row, is no evidence either way: counting calendar nights let a save, a few
    # nights of --skip-depth (docs/ops.md's own rollback) or a window that never reached the city, then ONE
    # failing night read as `saved nothing on the last 5 nights` (#169 review).
    unsaved_request_nights = int(date[in_span & (requests > 0)].nunique())
    # The two all-failed shapes differ only across rows: an `unavailable` verdict is ledgered at once and
    # comes back as the next run's skip, a transient failure never does. So what the ledger gained between
    # the first and the newest requesting row of the span, over the failures of every row but the newest
    # (whose verdicts cannot have come back yet), is 1.0 when failures are being written off and 0.0 when
    # they are transient - by construction, gsv.download_depth_maps. None until two rows have asked.
    asked = df[in_span & (requests > 0)]
    held = asked["depth_skip"].fillna(0) + asked["depth_success"].fillna(0)
    written_off = max(int(held.iloc[-1] - held.iloc[0]), 0) if len(asked) >= 2 else 0
    failed_before_newest = int(asked["depth_fail"].fillna(0).iloc[:-1].sum())
    unavailable_share = float(written_off / failed_before_newest) if failed_before_newest else None
    # A phase that reached every pano on its list (depth_total == depth_eligible) did not stop early: the
    # candidates are eligible - skip and every stop happens with work left. That is the ordinary end of a
    # backfill - a handful of stragglers that fail every night - and not an outage.
    newest_asked = df[requests > 0].iloc[-1] if (requests > 0).any() else None
    walked_list = bool(newest_asked is not None and pd.notna(newest_asked["depth_eligible"])
                       and newest_asked["depth_total"] >= newest_asked["depth_eligible"])
    barren = bool(unresolved > 0 and unsaved_request_nights >= DEPTH_BARREN_NIGHTS
                  and unsaved_requests >= DEPTH_BARREN_MIN_REQUESTS
                  and quiet_nights < DEPTH_STALLED_NIGHTS and not walked_list)

    return {
        "eligible": eligible,
        "resolved": resolved,
        "unresolved": unresolved,
        "nightly": nightly,
        "nightly_resolved": nightly_resolved,
        "nights_left": nights_left,
        "quiet_nights": quiet_nights,
        "newest_accounted": bool(df["depth_total"].iloc[-1] > 0),
        "corpus_suspect": corpus_suspect,
        "unsaved_nights": unsaved_nights,
        "unsaved_requests": unsaved_requests,
        "unsaved_request_nights": unsaved_request_nights,
        "ever_saved": last_save is not None,
        "written_off": written_off,
        "failed_before_newest": failed_before_newest,
        "unavailable_share": unavailable_share,
        "barren": barren,
    }


def depth_status(progress) -> str:
    """The depth clause of a city's stats line, or '' before the column exists in that city's log."""
    if progress is None:
        return ""
    if progress["eligible"] == 0:
        return "no GSV panos, so no depth"
    if progress["unresolved"] == 0:
        return f"depth complete ({progress['eligible']:,})"
    if progress["resolved"] == 0 and progress["nightly"] == 0:
        return f"depth not started (0/{progress['eligible']:,})"
    pct = 100.0 * progress["resolved"] / progress["eligible"]
    eta = ("no rate, no ETA" if progress["nights_left"] is None
           else f"~{max(1, round(progress['nights_left'])):,} nights left")
    # Panos per night, not requests per night. Printing the request rate next to a pano-based ETA invited
    # exactly the arithmetic the ETA no longer does - and on a night of heavy transient failure the two
    # numbers differ by the whole failure count.
    # A barren phase says so here too: in the drift shape the ledger grows by every failure, so the rate and
    # the ETA beside it are counting `unavailable` verdicts and read as a healthy backfill (#163).
    # Counted in nights that made requests, the WARNING's own count - not unsaved_nights, the calendar span,
    # which on a gappy log is a different number for the same fact (#169 final review).
    barren = (f" · nothing saved in {progress['unsaved_request_nights']} requesting nights"
              if progress["barren"] else "")
    return (f"depth {progress['resolved']:,}/{progress['eligible']:,} ({pct:.1f}%) · "
            f"+{progress['nightly_resolved']:,.0f} panos/night · {eta}{barren}")


def fleet_depth_summary(progress_by_city: dict, total_cities: int = None) -> list[str]:
    """The fleet-wide backfill block for the end of the report: totals, the rate, and the longest road ahead.

    Empty when no city reports a corpus yet, so a fleet that has not deployed the column sees nothing rather
    than a block of zeros. The 'longest remaining' line is the operational number: the fleet finishes when
    its slowest city does, and a fleet average (the 47-night estimate that argued for the current slot size)
    hid a tail more than ten times longer.
    """
    reporting = {c: p for c, p in progress_by_city.items() if p is not None and p["eligible"] > 0}
    if not reporting:
        return []
    total_cities = len(progress_by_city) if total_cities is None else total_cities
    eligible  = sum(p["eligible"] for p in reporting.values())
    resolved  = sum(p["resolved"] for p in reporting.values())
    nightly   = sum(p["nightly"] for p in reporting.values())
    gaining   = sum(p["nightly_resolved"] for p in reporting.values())
    complete  = sum(1 for p in reporting.values() if p["unresolved"] == 0)
    stalled   = sum(1 for p in reporting.values()
                    if p["unresolved"] > 0 and p["quiet_nights"] >= DEPTH_STALLED_NIGHTS)
    barren    = sum(1 for p in reporting.values() if p["barren"])
    with_eta  = sorted((p["nights_left"], c) for c, p in reporting.items() if p["nights_left"] is not None)
    longest   = ", ".join(f"{c} ~{max(1, round(n)):,} nights ({reporting[c]['unresolved']:,} left)"
                          for n, c in reversed(with_eta[-3:]))
    lines = [
        # The denominator is every city the report covered, not every city that got as far as a corpus. A
        # city whose log could not be downloaded never reaches this dict, so counting the dict read
        # "46 of 46 cities report a corpus" at exactly the moment six of them were invisible.
        f"  DEPTH BACKFILL — {len(reporting)} of {total_cities} cities report a corpus",
        f"  resolved {resolved:,} of {eligible:,} GSV panos ({100.0 * resolved / eligible:.1f}%) · "
        f"+{gaining:,.0f} panos/night on +{nightly:,.0f} requests ({DEPTH_RATE_NIGHTS}-night avg) · "
        f"{complete} complete · {stalled} stalled · {barren} saving nothing",
    ]
    if longest:
        lines.append(f"  longest remaining: {longest}")
    return lines


def fmt_count(value) -> str:
    """Thousands-separated integer, or '?' when the value is missing.

    The last row's counts are blank whenever the newest run ended early - exactly the situation the report is
    most needed in - so this must never be an int() that raises.
    """
    return "?" if value is None or pd.isna(value) else f"{int(value):,}"


def city_stats(df: pd.DataFrame) -> str:
    """One-line summary of recent activity for display alongside the city name."""
    last_ts      = df["start_time"].iloc[-1]
    recent_7     = df.tail(7)
    total_new    = int((recent_7["image_success"] + recent_7["image_fallback_success"]).sum())
    depth        = depth_status(depth_progress(df))
    return (
        f"last run {last_ts.strftime('%Y-%m-%d')} | "
        f"+{total_new} images (7d) | "
        f"{fmt_count(last_value(df, 'image_total'))} total | "
        f"{fmt_count(last_value(df, 'image_fail'))} permanent failures"
        + (f" | {depth}" if depth else "")
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_roster_hosts(flag, env) -> tuple[str, ...]:
    """The hosts to ask for the roster: the flag, else PS_ROSTER_HOST, else DEFAULT_ROSTER_HOSTS.

    Each is a comma-separated list. Blank entries are dropped, so an empty or whitespace-only setting counts
    as unset rather than becoming a host of " " that reports itself unable to serve a roster.

        >>> resolve_roster_hosts(None, " a.example , b.example ")
        ('a.example', 'b.example')
    """
    for setting in (flag, env):
        hosts = tuple(h.strip() for h in (setting or "").split(",") if h.strip())
        if hosts:
            return hosts
    return DEFAULT_ROSTER_HOSTS


def roster_check(city_rows, hosts, fetch=None) -> tuple[list[str], bool]:
    """Cross-check cities.csv against the fleet roster: (report lines, whether it is CRITICAL).

    A comparison, not auto-discovery (#133). `cities.csv` stays the source of what to monitor and the roster
    is only the cross-check; generating the file from the roster would silently pick up cities nobody has
    decided to watch, which is a different failure from the one this closes.

    `hosts` are asked in order until one serves a roster, at most roster.ROSTER_MAX_HOSTS of them. Two ways
    it goes CRITICAL, and the second is the one that is easy to "simplify" away:

    * **an unlisted city** - the gap itself;
    * **no host served a roster** - best effort on the fetch, but a check that fails open is the #130 shape
      again. An empty `hosts` lands here too, through the loop's `else`, so it cannot read as a clean check.

    Returns lines rather than printing them so the whole thing is drivable in a test.
    """
    fetch = roster.fetch_roster if fetch is None else fetch
    failures = []
    for host in hosts[:roster.ROSTER_MAX_HOSTS]:
        try:
            entries = fetch(host)
            break
        except roster.RosterUnavailable as e:
            failures.append(f"{host}: {e}")
    else:
        skipped = len(hosts) - len(failures)
        asked = f"; {len(failures)} of {len(hosts)} hosts asked" if skipped > 0 and failures else ""
        return ([f"  🔴 [CRITICAL] no host served a roster ({'; '.join(failures) or 'no hosts given'}{asked}) — "
                 f"cities.csv was not cross-checked"], True)

    have = {cid for cid in ((r.get("city_id") or "").strip() for r in city_rows) if cid and not cid.startswith("#")}
    unlisted = roster.unlisted_cities(entries, have, roster.disabled_rows(city_rows))
    public = sum(1 for e in entries if e["visibility"] == "public")
    private = len(entries) - public
    checked = f"checked {len(have)} rows against {public} public + {private} private cities on {host}"

    # A host that failed before this one answered is still reported: Seattle gets releases first, so a
    # Seattle that fails every night would otherwise go unseen until the fallback fails too.
    if failures:
        checked += f" (after {'; '.join(failures)})"

    if not unlisted:
        return ([f"  ✅  Roster cross-check — {checked}"], False)

    lines = [f"  🔴  Roster cross-check — {checked}"]
    for city in unlisted:
        where = city.host or ("url withheld (private)" if city.visibility == "private" else "no url published")
        lines.append(f"      🔴 [CRITICAL] not in cities.csv: {city.city_id} ({where})")
    lines.append(f"      → add a row, or record the decision with "
                 f'`#{unlisted[0].city_id},"{roster.OPT_OUT_MARKER} <why>"`')
    return (lines, True)


def main(argv=None) -> int:
    """Download (unless --no-download), analyze every city, print the report; return the process exit code.

    Takes argv and returns a status rather than calling sys.exit, so the whole report can be driven in a
    test - the same shape CropRunner and migrate_depth_artifacts already use. The usage errors below stay as
    sys.exit(str): that prints the message to stderr and exits 1, which a return value cannot do.
    """
    parser = argparse.ArgumentParser(
        description="Download and analyze scraper logs for all cities.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--download", action=argparse.BooleanOptionalAction, default=True,
        help="Download logs before analyzing (default: on). Use --no-download to skip.",
    )
    parser.add_argument(
        "--city", metavar="CITY_ID",
        help="Analyze only this city (e.g. seattle-wa).",
    )
    parser.add_argument(
        "--stale-days", type=int, default=STALE_DAYS_DEFAULT,
        help=f"Days without a new log entry before flagging CRITICAL (default: {STALE_DAYS_DEFAULT}).",
    )
    conn = parser.add_argument_group("pano store connection (each falls back to the matching PS_SFTP_* variable)")
    conn.add_argument("--host", help="Host or ~/.ssh/config alias. [PS_SFTP_HOST]")
    conn.add_argument("--base", help="Remote directory holding the per-city folders. [PS_SFTP_BASE]")
    conn.add_argument("--user", help="SSH user; omit if the ssh config supplies it. [PS_SFTP_USER]")
    conn.add_argument("--port", help="SSH port; omit for 22. [PS_SFTP_PORT]")
    conn.add_argument("--key", help="Identity file; omit to let ssh choose. [PS_SFTP_KEY]")
    ros = parser.add_argument_group("fleet roster cross-check (#133)")
    ros.add_argument(
        "--roster-host",
        help=f"Deployment(s) to ask for {roster.ROSTER_PATH}, comma-separated, tried in order. Every "
             f"deployment serves the same roster, so override only when the defaults are down. "
             f"[PS_ROSTER_HOST, default {','.join(DEFAULT_ROSTER_HOSTS)}]",
    )
    args = parser.parse_args(argv)

    sftp = resolve_sftp(args) if args.download else None

    LOGS_DIR.mkdir(exist_ok=True)

    # Two views of one file. The roster check reads ALL rows, `#` opt-outs included, since they are its
    # record of what was decided. The per-city report reads only real cities: a `#zurich-infra3d` row analysed
    # as a city fails its download and books CRITICAL, so following the check's own advice would still exit
    # 1. And `--city` narrows only the report - handing the check one row would name the rest of the fleet as
    # missing from a file that has them.
    all_rows = load_cities(CITIES_FILE)
    cities = [c for c in all_rows if not (c.get("city_id") or "").strip().startswith("#")]
    if args.city:
        cities = [c for c in cities if c["city_id"] == args.city]
        if not cities:
            sys.exit(f"City '{args.city}' not found in {CITIES_FILE}")

    # %Z so the report header says which clock it is on - the same omission, one level up, that let a
    # 13:30-Pacific slot read as "runs at 20:30, seems fine" for months (#101).
    now_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    print(f"\n{'━'*70}")
    print(f"  Scraper Log Analysis — {now_str}")
    print(f"  Cities: {len(cities)}  |  Stale threshold: {args.stale_days} days")
    print(f"{'━'*70}\n")

    results: dict[str, list[dict]] = {}
    progress: dict[str, dict] = {}

    for city in cities:
        city_id      = city["city_id"]
        display_name = city.get("display_name") or city_id
        log_path     = LOGS_DIR / f"log-{city_id}.csv"

        # Download
        if args.download:
            sys.stdout.write(f"  {display_name}  — downloading… ")
            sys.stdout.flush()
            ok = download_log(city_id, log_path, sftp)
            if not ok:
                print("FAILED")
                results[city_id] = [{"level": "CRITICAL", "msg": "Download failed"}]
                continue
            print("done")

        # Analyze
        issues = analyze_city(city_id, log_path, stale_days=args.stale_days)
        results[city_id] = issues

        # Load df for the stats line and the fleet depth block (best-effort)
        stats_line = ""
        try:
            df = read_log(log_path)
            if not df.empty:
                stats_line = city_stats(df)
                progress[city_id] = depth_progress(df)
        except Exception:
            pass

        # Print city block
        # Blue when every finding is INFO (#163): rule 9's history line is recorded, not alerted, and a yellow
        # icon for it would be the every-morning warning again in another form. Such a city counts as OK.
        levels = {i["level"] for i in issues}
        icon = ("✅" if not issues else "🔴" if "CRITICAL" in levels
                else "🟡" if "WARNING" in levels else "🔵")
        print(f"\n  {icon}  {display_name}")
        if stats_line:
            print(f"      {stats_line}")
        for issue in issues:
            level_icon = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(issue["level"], "·")
            print(f"      {level_icon} [{issue['level']}] {issue['msg']}")

    # The fleet's depth backfill, before the summary (#43)
    fleet = fleet_depth_summary(progress, total_cities=len(cities))
    if fleet:
        print(f"\n{'━'*70}")
        for line in fleet:
            print(line)

    # The fleet roster cross-check (#133). After the per-city report, because it is a fleet-level fact and
    # not a city's: it deliberately does NOT enter `results`, whose length is the "N cities checked"
    # denominator. scrape_queue's summarise learned the same lesson - a fleet fact counted as a city makes
    # the denominator lie. --no-download is offline mode, so it skips the fetch like every other network step.
    roster_critical = False
    if args.download:
        roster_lines, roster_critical = roster_check(
            all_rows,
            resolve_roster_hosts(args.roster_host, os.environ.get("PS_ROSTER_HOST")),
        )
        print(f"\n{'━'*70}")
        for line in roster_lines:
            print(line)

    # Summary
    critical = [cid for cid, iss in results.items() if any(i["level"] == "CRITICAL" for i in iss)]
    warnings = [cid for cid, iss in results.items() if any(i["level"] == "WARNING"  for i in iss)]
    ok_count = len(results) - len(set(critical + warnings))

    print(f"\n{'━'*70}")
    print(f"  SUMMARY — {len(results)} cities checked")
    print(f"  🔴 Critical : {len(critical):>3}  {', '.join(critical) if critical else ''}")
    print(f"  🟡 Warning  : {len(warnings):>3}  {', '.join(warnings) if warnings else ''}")
    print(f"  ✅ OK       : {ok_count:>3}")
    if roster_critical:
        print(f"  🔴 Roster   : cities.csv did not pass the cross-check")
    print(f"{'━'*70}\n")

    # Non-zero status if there are critical issues - this is what cron's mail-on-failure keys on, and for
    # most of the fleet it is the only thing that ever reports a city going dark. The roster gap counts:
    # a city with no row here has NO alarm at all, which is the whole reason #133 exists.
    return 1 if (critical or roster_critical) else 0


if __name__ == "__main__":
    raise SystemExit(main())
