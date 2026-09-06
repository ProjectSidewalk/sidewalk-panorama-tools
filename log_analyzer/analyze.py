#!/usr/bin/env python3
"""
Scraper log analyzer.

Downloads each city's log.csv from the pano store and analyzes it for potential issues:
  🔴 CRITICAL - scraper is likely broken (stale log, download failure)
  🟡 WARNING  - something unusual that warrants investigation
  🔵 INFO     - low-priority observations

Connection settings come from the environment (or the matching flags); nothing about the pano store is
hardcoded here. See docs/log-analyzer.md.

  PS_SFTP_HOST  required  host, or an ~/.ssh/config Host alias
  PS_SFTP_BASE  required  directory containing the per-city folders
  PS_SFTP_USER  optional  omit when the ssh config supplies it
  PS_SFTP_PORT  optional  omit for 22
  PS_SFTP_KEY   optional  omit to let ssh choose (ssh config / agent)

Usage:
  python3 analyze.py                        # download + analyze all cities
  python3 analyze.py --no-download          # analyze already-downloaded logs
  python3 analyze.py --city seattle-wa      # single city only
  python3 analyze.py --stale-days 5         # custom staleness threshold
"""

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR  = Path(__file__).parent
LOGS_DIR    = SCRIPT_DIR / "logs"
CITIES_FILE = SCRIPT_DIR / "cities.csv"

# ---------------------------------------------------------------------------
# log.csv format
# ---------------------------------------------------------------------------
# DownloadRunner appends 18 positional fields per run and never writes a header (see write_log_csv_row and
# the column table in docs/ops.md). Production files carry a header only because it is added by hand when a city is
# set up, so parsing must work either way - a forgotten header should not turn into a confusing parse error.
LOG_COLUMNS = [
    "start_time",
    "xml_success", "xml_fail", "xml_skip", "xml_total", "xml_minutes",
    "image_success", "image_fallback_success", "image_fail", "image_skip", "image_total", "image_minutes",
    "depth_success", "depth_fail", "depth_skip", "depth_total", "depth_minutes",
    "total_minutes",
]

# ---------------------------------------------------------------------------
# Thresholds (override via CLI flags where applicable)
# ---------------------------------------------------------------------------
STALE_DAYS_DEFAULT       = 3    # --stale-days
ZERO_PROGRESS_DAYS       = 30   # consecutive days with 0 new images before flagging
ZERO_PROGRESS_LOOKBACK   = 90   # days to look back when checking for prior progress
NEW_FAIL_DAILY_WARNING   = 20   # new image_fail entries/day (7-day avg) to flag
LONG_RUN_MULTIPLIER      = 3.0  # recent runtime > this × median → warning
INCOMPLETE_RUN_WARNING   = 3    # incomplete runs in the last 7 before flagging


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

def read_log(log_path: Path) -> pd.DataFrame:
    """Read a log.csv into a sorted DataFrame with LOG_COLUMNS, header row optional.

    Rows are always 18 positional fields, so columns are supplied by position and a leading header line, when
    present, is skipped. Blank fields (a run that crashed or was stopped before that phase finished, see #49)
    stay NaN: missing data, never a fabricated 0.
    """
    with open(log_path, newline="") as f:
        first = f.readline()
    has_header = first.split(",")[0].strip() == "start_time"

    df = pd.read_csv(
        log_path,
        header=0 if has_header else None,
        names=LOG_COLUMNS,
    )

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
    return df.sort_values("start_time").reset_index(drop=True)


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
        return [{"level": "CRITICAL", "msg": "Log file is empty"}]

    # Convenience: calendar date column, in UTC. Rule 6 below is the one that depends on where the day
    # boundary falls, and it is safe under the Pacific night window the queue runs in (#101): 20:00-05:00 PT
    # is 03:00-12:00 UTC (04:00-13:00 under PST), so a night sits entirely inside one UTC day and a city
    # still gets exactly one row per date.
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
        recent_7  = df.tail(7)
        avg_new_fails = recent_7["fail_delta"].mean()
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
    n = len(df)

    if n > ZERO_PROGRESS_DAYS + ZERO_PROGRESS_LOOKBACK:
        tail_n   = df.tail(ZERO_PROGRESS_DAYS)
        prior_n  = df.iloc[-(ZERO_PROGRESS_DAYS + ZERO_PROGRESS_LOOKBACK) : -ZERO_PROGRESS_DAYS]

        tail_all_zero  = (tail_n["daily_success"] == 0).all()
        prior_had_some = (prior_n["daily_success"] > 0).any()

        if tail_all_zero and prior_had_some:
            last_success_mask = df["daily_success"] > 0
            if last_success_mask.any():
                last_success_date = df.loc[last_success_mask, "date"].iloc[-1]
                issues.append({
                    "level": "WARNING",
                    "msg": (
                        f"No new images downloaded in {ZERO_PROGRESS_DAYS} days "
                        f"(last success: {last_success_date.strftime('%Y-%m-%d')})"
                    ),
                })

    # --- 4. Abnormally long runtime ---
    median_mins = df["total_minutes"].median()
    if pd.notna(median_mins) and median_mins > 0:
        threshold = median_mins * LONG_RUN_MULTIPLIER
        recent_7  = df.tail(7)
        long_runs = recent_7[recent_7["total_minutes"] > threshold]
        if not long_runs.empty:
            worst = long_runs["total_minutes"].max()
            issues.append({
                "level": "WARNING",
                "msg": (
                    f"Recent unusually long run: {worst:.0f} min "
                    f"(historical median: {median_mins:.0f} min, "
                    f"threshold: {threshold:.0f} min)"
                ),
            })

    # --- 5. Runs that ended early ---
    # A run that crashed or was stopped leaves every field from its first unfinished phase onward blank (#49).
    # None of the checks above can see those runs - NaN compares false everywhere - so a city that dies every
    # night would otherwise look healthy right up until its log goes stale.
    recent_7    = df.tail(7)
    incomplete  = recent_7[recent_7[LOG_COLUMNS[1:]].isna().any(axis=1)]
    if len(incomplete) >= INCOMPLETE_RUN_WARNING:
        issues.append({
            "level": "WARNING",
            "msg": (
                f"{len(incomplete)} of the last {len(recent_7)} runs ended early "
                f"(blank columns). Check scrape.log next to log.csv."
            ),
        })

    # --- 6. Duplicate runs on the same calendar day (last 30 days) ---
    recent_30  = df.tail(30)
    dup_dates  = recent_30[recent_30.duplicated("date", keep=False)]["date"].unique()
    if len(dup_dates):
        date_strs = ", ".join(d.strftime("%Y-%m-%d") for d in sorted(dup_dates))
        issues.append({
            "level": "INFO",
            "msg": f"Multiple runs on same day (last 30 days): {date_strs}",
        })

    return issues


def last_value(df: pd.DataFrame, column: str):
    """Most recent non-blank value in a column, or None when every run left it blank."""
    values = df[column].dropna()
    return None if values.empty else values.iloc[-1]


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
    return (
        f"last run {last_ts.strftime('%Y-%m-%d')} | "
        f"+{total_new} images (7d) | "
        f"{fmt_count(last_value(df, 'image_total'))} total | "
        f"{fmt_count(last_value(df, 'image_fail'))} permanent failures"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    args = parser.parse_args(argv)

    sftp = resolve_sftp(args) if args.download else None

    LOGS_DIR.mkdir(exist_ok=True)

    cities = load_cities(CITIES_FILE)
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

        # Load df for stats line (best-effort)
        stats_line = ""
        try:
            df = read_log(log_path)
            if not df.empty:
                stats_line = city_stats(df)
        except Exception:
            pass

        # Print city block
        icon = "✅" if not issues else (
            "🔴" if any(i["level"] == "CRITICAL" for i in issues) else "🟡"
        )
        print(f"\n  {icon}  {display_name}")
        if stats_line:
            print(f"      {stats_line}")
        for issue in issues:
            level_icon = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}.get(issue["level"], "·")
            print(f"      {level_icon} [{issue['level']}] {issue['msg']}")

    # Summary
    critical = [cid for cid, iss in results.items() if any(i["level"] == "CRITICAL" for i in iss)]
    warnings = [cid for cid, iss in results.items() if any(i["level"] == "WARNING"  for i in iss)]
    ok_count = len(results) - len(set(critical + warnings))

    print(f"\n{'━'*70}")
    print(f"  SUMMARY — {len(results)} cities checked")
    print(f"  🔴 Critical : {len(critical):>3}  {', '.join(critical) if critical else ''}")
    print(f"  🟡 Warning  : {len(warnings):>3}  {', '.join(warnings) if warnings else ''}")
    print(f"  ✅ OK       : {ok_count:>3}")
    print(f"{'━'*70}\n")

    # Non-zero status if there are critical issues - this is what cron's mail-on-failure keys on, and for
    # most of the fleet it is the only thing that ever reports a city going dark.
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
