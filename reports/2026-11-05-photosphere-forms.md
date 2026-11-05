# What the photo host answers for each form of `CAoS` photosphere id, and what the fleet's ledgers already say about them

2026-11-05. For [#223](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/223) (user
photospheres), measured the day after [#227](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/227)
merged and before it deployed. Also bears on
[#166](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/166) (which answers are permanent
verdicts) and settles recommendation 2 of
[the fleet-thresholds report](2026-10-06-fleet-thresholds-and-frame-refusals.md), which asked whether a `CAoS`
id's 400 should become a verdict.

## The question

A `CAoS` pano id wraps a photo-host photo id, and #227 fetches the photosphere from that host in one request.
Two forms of photo id had been seen: `AF1Qip...`, which the host served 24 of 24, and the older `CIHM...`, which
answered 400 on 9 of 9. Nine is not a measurement to hang a permanent `downloaded=0` on, so #227 shipped the 400
as a transient raise: no row, retried every night, one request each. chicago-il alone holds 598 CIHM-form ids,
so the question was whether to spend that request forever or to write the verdict.

Two things had to be measured before deciding: **does every id of the form answer 400**, and **is it the form
being refused or the host** (a wrong host would mean the pictures exist behind another URL). A third came out
of checking the first against the store: **what had the fleet's image ledgers already written about these
ids** before #227 existed.

## Method

Read-only against Google and the store; nothing was written anywhere but this repo.

* **The pano list.** `/adminapi/panos` of sidewalk-chicago, saved on 2026-11-05: **288,959** records,
  **64,087** labelled, **913** with the `CAoS` prefix. The list is 62 MB and changes nightly, so what is committed
  is its `CAoS` slice (`pack-list`: pano id, form, decoded photo id, `has_labels`, width, height) plus the two
  totals the slice cannot carry.
* **The sweep** (`measure`). Every non-AF1Qip id in the slice, **628** of them, asked once at
  `https://lh3.ggpht.com/p/<photo id>=s0`, through the downloader's own `_photosphere_session()` and
  `PHOTOSPHERE_URL`, so the request is the one the nightly run makes. Headers only (`stream=True`, the body never
  read, the response closed), paced 0.5-1.0 s apart, 628 requests in about 12 minutes, from a residential
  Seattle address, not the production box. Status, `Content-Type`, `Content-Length` and the final URL recorded
  per id; a raise would have been a row, not a stop. The AF1Qip form was not swept: it is the form #223
  measured served (24 of 24), and asking 285 more times would have measured the same thing.
* **The host probe** (`probe`). One id of each form, plus an AF1Qip control, at four URL shapes: the production
  host; `lh3.googleusercontent.com/p/` and `lh5.googleusercontent.com/p/`, which serve the same `/p/` paths for
  Maps photos; and the production host **without** `/p/`, as a control for "any shape the host does not know
  answers 400 too".
* **The ledgers** (`ledger`). Every row whose id starts `CAoS` in every city's `pano_id_log.csv` on the store
  (makelab2, `/m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas/<city>/`), read on 2026-11-05 before
  that evening's run: **1,752** rows in **25** cities. Every photosphere id carries the prefix, so a `grep`
  needs no id list. Row width is kept: a two-field row predates the `fetched_at` column, which reached the
  production store on 2026-09-17 ([#129](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/129)).
* **chicago-il's last eight `log.csv` rows** and an `ls` of its `CA/` shard, read the same morning.
* `analyze` reduces the committed files and makes no requests. Every number below is in its output
  (`reports/data/2026-11-05-photosphere-forms.json`), and `tests/test_photosphere_forms_study.py` checks that the
  JSON is what the files reduce to and that every table line and thousands-separated count here appears in it.

The form of a photo id is its leading letters (`AF1Qip`, `CIHM`, `CIAB`). The sweep's third form, `CIAB`, had
been lumped as "other" until the slice named it; nothing in the measurement depends on the name.

## A. The host's answer, by form

| Form | Measured | Labelled | 200 | 400 | Other status | Raised | Content types | Content lengths |
|---|---|---|---|---|---|---|---|---|
| CIAB | 30 | 7 | 0 | 30 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |
| CIHM | 598 | 46 | 0 | 598 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |

**628 of 628 answered 400**, with one `Content-Type` and one `Content-Length` between them: the same 1,555-byte
HTML error page for every id, no redirect (every final URL is the one asked), no raise, no 200. 53 of the 628
carry labels, 46 CIHM and 7 CIAB.

## B. The form or the host?

| Form | `https://lh3.ggpht.com/p/%s=s0` | `https://lh3.googleusercontent.com/p/%s=s0` | `https://lh5.googleusercontent.com/p/%s=s0` | `https://lh3.ggpht.com/%s=s0` |
|---|---|---|---|---|
| AF1Qip | 200 image/jpeg | 200 image/jpeg | 200 image/jpeg | 400 text/html |
| CIAB | 400 text/html | 400 text/html | 400 text/html | 400 text/html |
| CIHM | 400 text/html | 400 text/html | 400 text/html | 400 text/html |

The three `/p/` hosts answer identically: the control is served by all three (same 3,364,286-byte JPEG), the
other two forms are refused by all three. So the three hosts are one service, and what it refuses is the id's
form, not the URL's host. The no-`/p/` column refuses even the control, which is what a shape the host does not
know looks like, and it is the same 1,555-byte page. Whether some other Google door serves a CIHM or CIAB photo is
not answered here; these three are the ones that serve the AF1Qip form.

## C. What the fleet's ledgers already say

| Form | Ledger rows | Cities | `downloaded=0` | `downloaded=1` |
|---|---|---|---|---|
| AF1Qip | 901 | 19 | 901 | 0 |
| CIAB | 58 | 6 | 58 | 0 |
| CIHM | 793 | 14 | 793 | 0 |

**Every one of the 1,752 rows is `downloaded=0`, and every one is two-field**, so every one was written before
the stamp column reached the store on 2026-09-17, under whatever rule then governed a failed `CAoS` fetch. The
ledger has no reason column and no `scrape.log` from that era survives, so the rule and the date are not
recoverable; what is certain is that none of these rows was written by the code that has run since, which
raises on a `CAoS` id and ledgers nothing.

The consequence for #227: **the 901 AF1Qip photospheres, the form it can download, will not be fetched by it.**
`pano_id_log.csv` gates the image phase, and a ledgered id is never attempted again. As merged, #227 downloads
only AF1Qip ids that are not yet in a ledger: new ones, as they enter a city's list.

| City | AF1Qip | CIHM | CIAB | Total |
|---|---|---|---|---|
| chicago-il | 285 | 330 | 22 | 637 |
| kaohsiung-tw | 15 | 204 | 27 | 246 |
| gainesville-fl | 0 | 173 | 1 | 174 |
| knox-oh | 124 | 16 | 0 | 140 |
| zurich | 105 | 18 | 0 | 123 |
| spgg | 112 | 0 | 0 | 112 |
| seattle-wa | 61 | 9 | 0 | 70 |
| taipei | 59 | 2 | 0 | 61 |
| burnaby | 47 | 0 | 0 | 47 |
| cuenca | 25 | 0 | 0 | 25 |
| st-louis-mo | 21 | 2 | 0 | 23 |
| pittsburgh-pa | 19 | 1 | 0 | 20 |
| amsterdam | 8 | 11 | 0 | 19 |
| tucson-az | 0 | 14 | 0 | 14 |
| newport-ky | 0 | 9 | 0 | 9 |
| columbus-oh | 6 | 0 | 0 | 6 |
| columbia-sc | 2 | 0 | 3 | 5 |
| taichung-tw | 5 | 0 | 0 | 5 |
| waltham-ma | 0 | 0 | 4 | 4 |
| mendota-il | 3 | 0 | 0 | 3 |
| paterson-nj | 0 | 3 | 0 | 3 |
| new-taipei-tw | 2 | 0 | 0 | 2 |
| walla-walla-wa | 1 | 1 | 0 | 2 |
| houston-tx | 0 | 0 | 1 | 1 |
| keelung-tw | 1 | 0 | 0 | 1 |

Only the ledgered rows were counted fleet-wide. The other 24 cities' pano lists were not pulled, so how many
unledgered `CAoS` ids they hold (the ones that raise nightly) is not known from this study.

## D. chicago-il: the list against its own ledger

| Form | Ids in the list | Labelled | Ledgered | Ledgered `0` | Ledgered `1` | Unledgered | Unledgered, labelled |
|---|---|---|---|---|---|---|---|
| AF1Qip | 285 | 52 | 285 | 285 | 0 | 0 | 0 |
| CIHM | 598 | 46 | 330 | 330 | 0 | 268 | 0 |
| CIAB | 30 | 7 | 22 | 22 | 0 | 8 | 2 |

**637** of the 913 are ledgered `0`, every AF1Qip id among them; **276** are unledgered, all CIHM or CIAB, 2 of
them labelled. Every ledgered id is still in the list.

| Run start (`log.csv` field 1) | Image-eligible panos (field 5) | Raised (field 20) |
|---|---|---|
| 2026-10-31 19:00:24.048098-07:00 | 288015 | 276 |
| 2026-11-01 19:00:26.466804-08:00 | 288129 | 99 |
| 2026-11-01 19:22:04.764084-08:00 | 288129 | 276 |
| 2026-11-02 19:00:30.593036-08:00 | 288510 | 40 |
| 2026-11-02 19:44:41.298569-08:00 | 288510 | 276 |
| 2026-11-03 19:00:14.430318-08:00 | 288513 | 276 |
| 2026-11-04 19:55:37.287218-08:00 | 288959 | 18 |
| 2026-11-04 23:21:28.404563-08:00 | 289073 | 276 |

Field 20 is the image attempts the run raised ([#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182)).
It reads **276 on five of the eight rows**, and 276 is exactly the unledgered count: on every pass that
reaches the end of chicago-il's list, the whole of field 20 is these photospheres, and it has been so since
before #227. The three lower readings are the other pass of the same night; which of a night's two passes
reaches the end of the list is a queue-log question not pursued here. The fleet-thresholds report found 5 raising
`CAoS` ids in chicago-il on 2026-10-06; the 276 are the ids that entered the list since the ledgered rows were
written, which says the population grows as people visit photospheres under `--all-panos`.

## E. Five photospheres already on the store

chicago-il's `CA/` shard holds **5** `CAoS` JPEGs, all AF1Qip, all dated 2026-10-20 17:02 PDT and owned by
`jonf`: the development measurement for #223 wrote them over the sshfs mount while proving the AF1Qip fetch.
They are real panoramas under their correct names. All five ids are among the 285 ledgered `0`, so the image
phase never looks at them; once re-admitted (below) the on-disk check registers each as `skipped`, which is
the ledger's ordinary way of recording a file that was already there. The listing is committed.

## Recommendation

1. **Make the photo host's 400 a permanent `downloaded=0` for the CIHM and CIAB forms**, keyed on the decoded
   photo id's form, and leave an AF1Qip 400 transient. The measurement is 628 of 628 at the production URL and 2
   of 2 at three more, with one error page; the refusal is to the form, and an AF1Qip 400 has never been seen,
   so it stays unmeasured and therefore transient, as the #166 contract requires. The verdict is still a
   *response*: a new CIHM id is asked once, so if the host ever starts serving the form, new ids download and
   only the ledgered ones need a re-admit, for which the one-off pattern exists
   (`readmit_dimless_writeoffs.py`). The alternative, sitting the two forms out without a request, needs new
   runner plumbing and leaves the 851 CIHM/CIAB rows the fleet has already written off inconsistent with the
   276 that would sit out. In chicago-il this takes field 20 from 276 to about 0 on a quiet night, which is what
   lets the `images-no-success` floor mean something there.
2. **Re-admit the 901 two-field `downloaded=0` rows whose id decodes to an AF1Qip photo id**, in all 19 cities,
   so #227 fetches what it was built to fetch. The CIHM and CIAB rows stay: under recommendation 1 they are
   already what the code would write. A one-off in the mould of `readmit_dimless_writeoffs.py` (dry run by
   default, the queue's lock, a backup and an audit copy, run on the box as the cron user), selecting on the id's
   decoded form rather than on a date, since a two-field row has none.
3. **Depth is unaffected.** The depth ledger already holds every one of chicago-il's 913 as `unavailable`
   (photometa answers "not found" for a photosphere, measured in #227), so nothing there changes.

## What changed in the code

Nothing in this PR. The two recommendations are a follow-up PR against the downloader
(`.claude/rules/downloader.md` carries the verdict contract) and a one-off script; this report is the
measurement they cite. The form classifier lives in this script until that PR, which should move it into
`downloaders/gsv.py` and import it back here, so there is one definition (the desk-study rule).

## Wrong turns

* **The sweep was reported as "914 ids" and "630 requests a night".** 914 counted the CSV's header line; the
  slice holds 913. And the nightly cost was estimated from the 628 non-AF1Qip ids before the ledger join showed
  that 352 of them were already written off: the real nightly figure is 276, which field 20 confirms.
* **The ledger join first read as five rows.** A PowerShell `Select-Object -First 4 / -Last 12` over the
  pulled rows showed five, and I reported five `CAoS` rows in chicago-il's ledger before reading the whole file:
  637. The 901-row AF1Qip finding, the most consequential one here, was hiding behind that view.
* **Per-form city counts were eyeballed from the per-city listing** (21, 16 and 7) before `analyze` computed
  them (19, 14 and 6). Every count in this report is now the script's.
* **The sweep did not name the CIAB form.** Its rows said "other"; the committed slice names the form and the
  measurement rows were relabelled from the slice by pano id, not re-measured.
* **An upload of the id list to makelab2 was refused** by the session's permission classifier as a remote
  write. The join was done locally instead, from a `grep` of the ledger, which is also why every photosphere
  row of every city came back rather than chicago-il's alone: the fleet finding is a by-product of the refusal.

## Data

* `reports/data/2026-11-05-photosphere-forms-chicago-list.json`: the list's totals (records, labelled, `CAoS`).
* `reports/data/2026-11-05-photosphere-forms-chicago-caos-ids.csv.gz`: the 913-id slice: pano id, form, decoded
  photo id, `has_labels`, width, height.
* `reports/data/2026-11-05-photosphere-forms-measurements.csv.gz`: one row per swept id: status, `Content-Type`,
  `Content-Length`, final URL, note.
* `reports/data/2026-11-05-photosphere-forms-host-probe.csv`: the 12 probe rows.
* `reports/data/2026-11-05-photosphere-forms-fleet-ledger.csv.gz`: every `CAoS` row of every city's
  `pano_id_log.csv`, with its width.
* `reports/data/2026-11-05-photosphere-forms-chicago-log-tail.csv`: chicago-il's last eight `log.csv` rows.
* `reports/data/2026-11-05-photosphere-forms-chicago-caos-files.txt`: the `ls` of the five files in E.
* `reports/data/2026-11-05-photosphere-forms.json`: everything above, reduced by `analyze`.
* `reports/scripts/photosphere_forms.py`: `pack-list`, `measure`, `probe`, `pack-ledger`, `analyze`.

## Tests

`tests/test_photosphere_forms_study.py`: the classifier, the headers-only request, the sweep and probe on a stub
host, the ledger-dump reader and the whole reduction on a synthetic data directory; the committed JSON re-derived
from the committed files; the findings pinned (every non-AF1Qip id measured and none served, the probe's
form-versus-host split, every fleet row a two-field `0`, chicago-il's unledgered count equal to its field 20);
and every table line and thousands-separated count here checked against the script's output.

## Open questions

* Is there any Google door that serves a CIHM or CIAB photo? Three hosts say no; nothing else was tried.
* How many unledgered `CAoS` ids do the other 24 ledgered cities (and the cities with no `CAoS` row at all) hold?
  That needs their pano lists, and it is the fleet-wide version of chicago-il's 276.
* Which of a night's two chicago-il passes reaches the end of the list, and why the other stops short.
* When, and under which rule, the 1,752 two-field zeros were written.
