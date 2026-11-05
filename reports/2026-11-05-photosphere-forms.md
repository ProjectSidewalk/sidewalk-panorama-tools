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

* **The pano list.** `/adminapi/panos` of sidewalk-chicago, fetched on 2026-11-04 at 20:20 PST, between that
  night's two chicago-il passes: **288,959** records, **64,087** labelled, **913** with the `CAoS` prefix. Its
  record count equals field 5 of the 2026-11-04 19:55 run, so it is that pass's list; the 23:21 pass saw a
  slightly longer one (table in D). The list is too large to commit and changes nightly, so what is committed
  is its `CAoS` slice (`pack-list`: pano id, form, decoded photo id, `has_labels`, width, height) plus the two
  totals the slice cannot carry.
* **Two sweeps** (`measure`). The first, early on 2026-11-05: every non-AF1Qip id in the slice, **628** of
  them, asked once at `https://lh3.ggpht.com/p/<photo id>=s0`, through the downloader's own
  `_photosphere_session()` and `PHOTOSPHERE_URL`, so the request is the one the nightly run makes. Headers only
  (`stream=True`, the body never read, the response closed), paced 0.5-1.0 s apart, from a residential Seattle
  address, not the production box. Status, `Content-Type`, `Content-Length` and the final URL recorded per id; a
  raise would have been a row, not a stop. The second, later the same morning after review: **every** id in the
  slice, **913**, the AF1Qip form included (`measure --all-forms`), into its own file, so the served form's rate
  is measured rather than carried over from #223's 24, and so the refused forms have two readings.
* **The host probe** (`probe`). One id of each form, plus an AF1Qip control, at four URL shapes: the production
  host; `lh3.googleusercontent.com/p/` and `lh5.googleusercontent.com/p/`, which serve the same `/p/` paths for
  Maps photos; and the production host **without** `/p/`, as a control for "any shape the host does not know
  answers 400 too". 12 rows.
* **The ledgers** (`pack-ledger`, `pack-depth-ledger`). Every row whose id starts `CAoS` in every city's
  `pano_id_log.csv` on the store (makelab2, `/m-makeabilitylab/makeabilitylab/sidewalk_panos/Panoramas/<city>/`),
  read on 2026-11-05 before that evening's run: **1,752** rows in **25** cities. Every photosphere id carries
  the prefix, so a `grep` needs no id list. Row width is kept: a two-field row predates the `fetched_at` column,
  which reached the production store on 2026-09-17
  ([#129](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/129)). Also chicago-il's
  `depth_log.csv` rows for these ids.
* **chicago-il's last eight `log.csv` rows** and an `ls` of its `CA/` shard, read the same morning.
* `analyze` reduces the committed files and makes no requests. Every number below is in its output
  (`reports/data/2026-11-05-photosphere-forms.json`), and `tests/test_photosphere_forms_study.py` checks that the
  JSON is what the files reduce to and that every table line and every count in this prose appears in it.

The form of a photo id is its leading letters (`AF1Qip`, `CIHM`, `CIAB`). The first sweep's third form, `CIAB`,
had been lumped as "other" until the slice named it; nothing in the measurement depends on the name.

## A. The host's answer, by form

The first sweep, the refused forms only:

| Form | Measured | Labelled | 200 | 400 | Other status | Raised | Content types | Content lengths |
|---|---|---|---|---|---|---|---|---|
| CIAB | 30 | 7 | 0 | 30 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |
| CIHM | 598 | 46 | 0 | 598 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |

**628 of 628 answered 400**, with one `Content-Type` and one `Content-Length` between them: the same 1,555-byte
HTML error page for every id, no redirect (every final URL is the one asked), no raise, no 200. **53** of the
628 carry labels, 46 CIHM and 7 CIAB.

The second sweep, every id:

| Form | Measured | Labelled | 200 | 400 | Other status | Raised | Content types | Content lengths |
|---|---|---|---|---|---|---|---|---|
| AF1Qip | 285 | 52 | 285 | 0 | 0 | 0 | `image/jpeg` | 285 distinct, 760412-10833559 bytes |
| CIAB | 30 | 7 | 0 | 30 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |
| CIHM | 598 | 46 | 0 | 598 | 0 | 0 | `text/html; charset=UTF-8` | 1555 |

**Every AF1Qip id is served**: 285 of 285 answered 200 `image/jpeg`, each a different length (a JPEG per photo).
The refused forms answered exactly as in the first sweep; no id changed status between the two. Both sweeps are
from one address on one day, hours apart; the same measurement from the production box or on another day was
not made.

## B. The form or the host?

| Form | `https://lh3.ggpht.com/p/%s=s0` | `https://lh3.googleusercontent.com/p/%s=s0` | `https://lh5.googleusercontent.com/p/%s=s0` | `https://lh3.ggpht.com/%s=s0` |
|---|---|---|---|---|
| AF1Qip | 200 image/jpeg, 3364286 bytes | 200 image/jpeg, 3364286 bytes | 200 image/jpeg, 3364286 bytes | 400 text/html, 1555 bytes |
| CIAB | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes |
| CIHM | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes | 400 text/html, 1555 bytes |

The three `/p/` hosts answer identically for these three ids: the control is served by all three (the same
3,364,286-byte JPEG), the other two forms are refused by all three. So the refusal follows the id's form, not
the URL's host. The no-`/p/` column refuses even the control, which is what a shape the host does not know looks
like, and it is the same 1,555-byte page the refused forms get; it carries no evidence about form and is there
as a control. One id per form is a probe, not a sweep. Whether some other Google door serves a CIHM or CIAB
photo is not answered here; these are the hosts that serve the AF1Qip form.

## C. What the fleet's ledgers already say

| Form | Ledger rows | Cities | `downloaded=0` | `downloaded=1` |
|---|---|---|---|---|
| AF1Qip | 901 | 19 | 901 | 0 |
| CIAB | 58 | 6 | 58 | 0 |
| CIHM | 793 | 14 | 793 | 0 |

**Every one of the 1,752 rows is `downloaded=0`, and every one is two-field**, so every one was written before
the stamp column reached the store on 2026-09-17, under whatever rule then governed a failed `CAoS` fetch. For
chicago-il there is a tighter bound: on every complete pass in D, field 9 (image failures, seeded from the
ledger's `0` rows) minus field 20 (tonight's raises) is **22,985**, and 22,985 is the permanent-verdict count
`.claude/rules/downloader.md` records for chicago-il on 2026-09-06. So chicago-il has written no `0` row since
2026-09-06, and its 637 photosphere rows were already there. The ledger has no reason column and no `scrape.log`
from that era survives, so the rule and the dates are not recoverable; what is certain is that none of these rows
was written by the code that has run since, which raises on a `CAoS` id and ledgers nothing.

The consequence for #227: **the 901 AF1Qip photospheres, the form the host serves, will not be fetched by it.**
`pano_id_log.csv` gates the image phase, and a ledgered id is never attempted again. As merged, #227 downloads
only AF1Qip ids that are not yet in a ledger: new ones, as they enter a city's list. The other **851** rows are
CIHM and CIAB.

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

Only the ledgered rows were counted fleet-wide. The other **24** cities' pano lists were not pulled, so how many
unledgered `CAoS` ids they hold (the ones that raise nightly) is not known from this study.

## D. chicago-il: the list against its own ledger

| Form | Ids in the list | Labelled | Ledgered | Ledgered `0` | Ledgered `1` | Unledgered | Unledgered, labelled |
|---|---|---|---|---|---|---|---|
| AF1Qip | 285 | 52 | 285 | 285 | 0 | 0 | 0 |
| CIHM | 598 | 46 | 330 | 330 | 0 | 268 | 0 |
| CIAB | 30 | 7 | 22 | 22 | 0 | 8 | 2 |

**637** of the 913 are ledgered `0`, every AF1Qip id among them and **352** of the 628 refused-form ids; **276**
are unledgered, all CIHM or CIAB, 2 of them labelled. Every ledgered id is still in the list.

| Run start (`log.csv` field 1) | Image-eligible panos (field 5) | Image failures (field 9) | Raised (field 20) |
|---|---|---|---|
| 2026-10-31 19:00:24.048098-07:00 | 288015 | 23261 | 276 |
| 2026-11-01 19:00:26.466804-08:00 | 288129 | 23084 | 99 |
| 2026-11-01 19:22:04.764084-08:00 | 288129 | 23261 | 276 |
| 2026-11-02 19:00:30.593036-08:00 | 288510 | 23025 | 40 |
| 2026-11-02 19:44:41.298569-08:00 | 288510 | 23261 | 276 |
| 2026-11-03 19:00:14.430318-08:00 | 288513 | 23261 | 276 |
| 2026-11-04 19:55:37.287218-08:00 | 288959 | 23003 | 18 |
| 2026-11-04 23:21:28.404563-08:00 | 289073 | 23261 | 276 |

Field 20 is the image attempts the run raised ([#182](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/182)).
It reads **276 on 5 of the 8 rows**, and 276 is exactly the unledgered count in the committed list, which is the
list the 2026-11-04 19:55 run saw. The 23:21 run that also raised 276 saw a list 114 records longer, so the two
snapshots differ, but the inference does not depend on which: under the pre-#227 code on the box every unledgered
`CAoS` id raises, and nothing else in chicago-il does. On every pass that reaches the end of its list, the whole of
field 20 is these photospheres. The three lower readings are the other pass of the same night; which of a night's
two passes reaches the end of the list is a queue-log question not pursued here. The fleet-thresholds report
counted 5 raising `CAoS` ids in chicago-il on 2026-10-06; why the count is 276 now is **not established** (see
Open questions): across these eight rows it did not move while field 5 grew, which fits neither a steady inflow
nor nothing at all, and a code or flag change between the two reports has not been ruled out.

## E. Five photospheres already on the store

chicago-il's `CA/` shard holds **5** `CAoS` JPEGs, dated 2026-10-20 17:02 in the listing and owned by `jonf`:
the development measurement for #223 wrote them over the sshfs mount while proving the AF1Qip fetch. All five
decode to the AF1Qip form, all five are in the list, and all five are among the 285 ledgered `0`, so the image
phase never looks at them. Their dimensions were not checked against the app's. Once re-admitted (below) the
on-disk check registers each as `skipped`, the ledger's ordinary way of recording a file that was already there.
The listing is committed.

The depth side is settled: chicago-il's `depth_log.csv` holds all **913** as `unavailable` (photometa answers
"not found" for a photosphere, measured 2 of 2 in #227), so nothing in the depth phase changes.

## Recommendation

1. **Make the photo host's 400 a permanent `downloaded=0` for the CIHM and CIAB forms, keyed on the status and
   the decoded form together**, and leave an AF1Qip 400 transient. This is an **exception to #166's "a non-200
   is never a verdict"**, carved on this measurement and no wider: 628 of 628 at the production URL, twice, and 2
   of 2 at two more hosts that serve the control, all with one error page. The refusal is to the form. An AF1Qip
   400 has never been seen, so it is unmeasured and therefore transient, which is what #166 asks of anything
   unmeasured. The verdict says the host refuses the form, not that the picture is gone; the archive rule is not
   engaged, since no file is touched. The verdict is still a *response*: a new CIHM id is asked once, so if the
   host ever starts serving the form, new ids download and only the ledgered ones need a re-admit, for which the
   one-off pattern exists (`readmit_dimless_writeoffs.py`). The alternative, sitting the two forms out without a
   request, behaves the same for the 851 rows already written off but needs new runner plumbing and never asks a
   new id, so a host that changed its mind would go unnoticed. In chicago-il this takes field 20 from 276 to about
   0 on a quiet night, which is what lets the `images-no-success` floor mean something there.
2. **Re-admit the 901 two-field `downloaded=0` rows whose id decodes to an AF1Qip photo id**, in all 19 cities,
   so #227 fetches what it was built to fetch. The host served every one of chicago-il's 285 in the second sweep;
   the other 616 were not asked, so a few may be gone, and under recommendation 1 a gone AF1Qip photo is a
   perennial raiser (a 400 for that form stays transient). That is the right default while the form's 400 is
   unmeasured, and the number will be visible in field 20. The CIHM and CIAB rows stay: under recommendation 1
   they are already what the code would write. A one-off in the mould of `readmit_dimless_writeoffs.py` (dry run by
   default, the queue's lock, a backup and an audit copy, run on the box as the cron user), selecting on the id's
   decoded form rather than on a date, since a two-field row has none.
3. **Depth is unaffected** (E).

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
  them (19, 14 and 6). Every count in this report is now the script's, and the test checks the prose ones.
* **The first draft said "three more hosts" and "the three hosts are one service".** The fourth URL shape
  refuses the control too, so it says nothing about form: two more hosts. And three ids cannot show that three
  hosts are one service, only that they answered identically.
* **The first draft explained the jump from 5 to 276 raising ids as photospheres entering the list.** The eight
  committed rows cut against it (276 flat while the list grew), and the 2026-10-06 report's counting basis was
  not checked. It is an open question now.
* **The first draft quoted the depth ledger (913 `unavailable`) with no artifact behind it**, from a one-line
  `grep -c` in a terminal. The rows are committed now and counted by `analyze`.
* **The first sweep skipped the AF1Qip form** on the strength of #223's 24 of 24, and the re-admit
  recommendation rested on that. The review asked for the measurement; `measure --all-forms` made it.
* **The sweep did not name the CIAB form.** Its rows said "other"; the committed slice names the form and the
  measurement rows were relabelled from the slice by pano id, not re-measured.
* **An upload of the id list to makelab2 was refused** by the session's permission classifier as a remote
  write. The join was done locally instead, from a `grep` of the ledger, which is also why every photosphere
  row of every city came back rather than chicago-il's alone: the fleet finding is a by-product of the refusal.

## Data

* `reports/data/2026-11-05-photosphere-forms-chicago-list.json`: the list's totals (records, labelled, `CAoS`).
* `reports/data/2026-11-05-photosphere-forms-chicago-caos-ids.csv.gz`: the 913-id slice: pano id, form, decoded
  photo id, `has_labels`, width, height.
* `reports/data/2026-11-05-photosphere-forms-measurements.csv.gz`: the first sweep, one row per non-AF1Qip id:
  status, `Content-Type`, `Content-Length`, final URL, note.
* `reports/data/2026-11-05-photosphere-forms-measurements-all-forms.csv.gz`: the second sweep, every id.
* `reports/data/2026-11-05-photosphere-forms-host-probe.csv`: the 12 probe rows.
* `reports/data/2026-11-05-photosphere-forms-fleet-ledger.csv.gz`: every `CAoS` row of every city's
  `pano_id_log.csv`, with its width.
* `reports/data/2026-11-05-photosphere-forms-chicago-depth-ledger.csv.gz`: chicago-il's `depth_log.csv` rows for
  its `CAoS` ids.
* `reports/data/2026-11-05-photosphere-forms-chicago-log-tail.csv`: chicago-il's last eight `log.csv` rows.
* `reports/data/2026-11-05-photosphere-forms-chicago-caos-files.txt`: the `ls` of the five files in E.
* `reports/data/2026-11-05-photosphere-forms.json`: everything above, reduced by `analyze`.
* `reports/scripts/photosphere_forms.py`: `pack-list`, `measure`, `probe`, `pack-ledger`, `pack-depth-ledger`,
  `analyze`.

## Tests

`tests/test_photosphere_forms_study.py`: the classifier, the headers-only request, the sweeps and probe on a stub
host, the two ledger-dump readers, the gzip writer, and the whole reduction on a synthetic data directory (fields
5, 9 and 20 at distinct values, so a wrong index fails); the committed JSON re-derived from the committed files;
the findings pinned (every non-AF1Qip id measured and none served, the second sweep covering every id with no
status changed, the probe's form-versus-host split, every fleet row a two-field `0`, chicago-il's unledgered count
equal to its field 20 and field 9 net of field 20 equal to 22,985, the depth ledger all `unavailable`, the five
files AF1Qip and ledgered); and every table line and every count in this prose, matched as a whole number,
checked against the script's output.

## Open questions

* Is there any Google door that serves a CIHM or CIAB photo? Three hosts say no; nothing else was tried.
* Why does chicago-il raise 276 `CAoS` ids now when the 2026-10-06 report counted 5? Inflow since then, a
  different counting basis in that report, or the #156 or #184 code changes in between: not established.
* How many unledgered `CAoS` ids do the other 24 ledgered cities (and the cities with no `CAoS` row at all) hold?
  That needs their pano lists, and it is the fleet-wide version of chicago-il's 276.
* Does the same sweep from the production box, or on another day, answer the same? Both sweeps here are from one
  address on one morning.
* Which of a night's two chicago-il passes reaches the end of the list, and why the other stops short.
* When, and under which rule, the 1,752 two-field zeros were written. For chicago-il, by 2026-09-06.
