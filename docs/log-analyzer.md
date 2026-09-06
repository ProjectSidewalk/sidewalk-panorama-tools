# Log analyzer — `log_analyzer/analyze.py`

Monitors the nightly scrape across every city. It pulls each city's `log.csv` off the pano store over SFTP and
flags the ones that look broken. This is an ops tool you run from a workstation or a cron box — the scraper
neither knows nor needs it, and it shares no code with the runners.

It needs only `pandas` plus the `sftp` client binary (`openssh-client`). `pandas` lives in
`requirements-dev.txt`, not `requirements.txt` — nothing the scraper or cropper runs imports it
([#72](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/72)) — so a box running only the
analyzer wants `pip3 install 'pandas>=2.0'`, not the whole dev file. The floor is not cosmetic:
`read_log` parses timestamps with `format="ISO8601"`, which 1.x does not accept, and the inference it
would otherwise fall back to discards every row whose timestamp width differs from the first row's.

## Connection settings

Read from the environment, or the matching flag. Host and base path are **required and have no defaults** — a
wrong default would silently analyze the wrong store.

| Variable | Flag | |
|---|---|---|
| `PS_SFTP_HOST` | `--host` | **required** — host, or an `~/.ssh/config` `Host` alias |
| `PS_SFTP_BASE` | `--base` | **required** — remote directory holding the per-city folders |
| `PS_SFTP_USER` | `--user` | optional — omit when the ssh config supplies it |
| `PS_SFTP_PORT` | `--port` | optional — omit for 22 |
| `PS_SFTP_KEY`  | `--key`  | optional — omit to let ssh choose (ssh config / agent) |

Setting up an `~/.ssh/config` `Host` alias is the tidiest option: with the user, port, and key declared there,
only `PS_SFTP_HOST` and `PS_SFTP_BASE` are needed.

```bash
export PS_SFTP_HOST=... PS_SFTP_BASE=... PS_SFTP_USER=... PS_SFTP_PORT=... PS_SFTP_KEY=~/.ssh/...

python3 log_analyzer/analyze.py                    # download all city logs, then analyze
python3 log_analyzer/analyze.py --no-download      # re-analyze the local cache
python3 log_analyzer/analyze.py --city seattle-wa  # one city
python3 log_analyzer/analyze.py --stale-days 5     # custom staleness threshold
```

Exit status is `1` when any city has a CRITICAL issue, so cron's mail-on-failure does the alerting. Downloaded
logs are cached in `log_analyzer/logs/` (gitignored).

`log_analyzer/cities.csv` maps `city_id` → display name; each `city_id` must match that city's folder name on
the pano store **exactly**. Add a row when a new city is deployed.

**A city missing from this file is not monitored, and nothing says so** — the analyzer reports on the cities it
is given and has no way to know the fleet is larger. `newport-ky` sat outside it while being scraped nightly,
and was found only by diffing this list against the production crontab in Sep 2026. Nothing in CI can catch
that drift, because the fleet is a deployment fact and this file is in the repo, so it is worth diffing by hand
whenever the two are both in front of you:

```bash
# on the scraper host: which scheduled cities is the analyzer not watching?
comm -23 <(cut -d, -f1 /etc/sidewalk/cities.csv | tail -n +2 | sort) \
         <(cut -d, -f1 log_analyzer/cities.csv | tail -n +2 | sort)
```

Once the [nightly queue](downloader.md#nightly-deployment) is deployed, its manifest is the authoritative list
to diff against; before then it is the crontab.

## Checks

| Level | Condition |
|-------|-----------|
| 🔴 CRITICAL | Log download failed, or the file is missing/empty/unparseable |
| 🔴 CRITICAL | Last log entry is more than `--stale-days` days old (default 3) |
| 🟡 WARNING | `image_fail` growing by ≥20/day (7-day average) — new panos failing |
| 🟡 WARNING | Zero new images for 30 consecutive days, after a period that had some (regression) |
| 🟡 WARNING | A recent run took >3× the historical median runtime |
| 🟡 WARNING | ≥3 of the last 7 runs ended early (blank columns) |
| 🔵 INFO | Multiple runs logged on the same calendar day |

Thresholds are module constants near the top of `analyze.py`.

A healthy mature city looks like: `image_success` small or zero most days, stable `image_fail`,
`image_skip ≈ image_total`.

## Two things about the parsing

**It reads the [18 positional columns](ops.md#the-logcsv-columns) by position**, tolerating a header row that
may or may not be there: `write_log_csv_row` never writes one, and production files get theirs by hand at city
setup. Blank fields stay `NaN` — a crashed run must never read as a quiet one — so every check guards against
NaN rather than coercing to `int`.

**It uses `sftp -b -`** (batch mode via stdin) rather than `scp`, because the store runs a restricted SFTP
subsystem that doesn't speak the SCP wire protocol; newer `scp` clients default to SFTP-over-SSH and fail with
`mtime.sec not present`.
