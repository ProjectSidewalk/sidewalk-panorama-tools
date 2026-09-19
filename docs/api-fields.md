# API field glossary

The two Project Sidewalk endpoints this repo reads, field by field. Both are served by every deployed city at
`https://<city-fqdn>/...`.

## `/adminapi/panos` — the downloader's pano list

| Attribute | Definition |
|---|---|
| `pano_id` | A unique ID, provided by the imagery source, for the panoramic image |
| `width` | The width of the pano image in pixels |
| `height` | The height of the pano image in pixels |
| `lat` | The latitude of the camera when the image was taken |
| `lng` | The longitude of the camera when the image was taken |
| `camera_heading` | The heading (in degrees) of the center of the image with respect to true north |
| `camera_pitch` | The pitch (in degrees) of the camera with respect to horizontal |
| `source` | The source of the imagery (`gsv`, `mapillary`, `panoramax`, …) |

The downloader drops empty ids and the literal id `tutorial`, then keeps `gsv` and `panoramax`, plus
`mapillary` when `MAPILLARY_ACCESS_TOKEN` is set. Panoramax `pano_id`s are UUIDs, so nothing may assume an
id alphabet. See [Downloader → Imagery sources](downloader.md#imagery-sources).

## `/adminapi/labels/cvMetadata` — the cropper's label list

You won't need most of this in your work, but it's all here for reference. Everything through
`unsure_count` might be useful; then there are a few duplicates from the endpoint above; then everything
from `canvas_width` on probably doesn't matter for you.

The table below is the response measured against `sidewalk-sea` on 2026-09-18, and
`samples/cvmetadata-seattle.csv` is that response — the field names are transcribed from a capture rather
than from memory, because the four this table used to carry were each wrong in a different way (#123).

| Attribute | Definition |
|---|---|
| `label_id` | A unique ID for each label **within a given city**, provided by Project Sidewalk |
| `pano_id` | A unique ID, provided by the imagery source, for the panoramic image [same as `/adminapi/panos`] |
| `label_type` | The **name** of the label type placed (`CurbRamp`, `SurfaceProblem`, …) — see the table below |
| `pano_x` | The x-pixel location of the label on the pano, where top-left is (0,0) |
| `pano_y` | The y-pixel location of the label on the pano, where top-left is (0,0) |
| `agree_count` | The number of "agree" validations provided by Project Sidewalk users |
| `disagree_count` | The number of "disagree" validations provided by Project Sidewalk users |
| `unsure_count` | The number of "unsure" validations provided by Project Sidewalk users |
| `pano_width` | The width of the pano image in pixels [same as `/adminapi/panos`] |
| `pano_height` | The height of the pano image in pixels [same as `/adminapi/panos`] |
| `camera_heading` | The heading (in degrees) of the center of the image with respect to true north [same as `/adminapi/panos`] |
| `camera_pitch` | The pitch (in degrees) of the camera with respect to horizontal [same as `/adminapi/panos`] |
| `camera_roll` | The roll (in degrees) of the camera, when the source provides one. **Measured** blank for GSV (every row of the capture this table was transcribed from is a GSV row). Whether Mapillary and Panoramax populate it is *inferred* from those sources carrying a roll at all — unmeasured, because no capture from a non-GSV city has been taken |
| `canvas_width` | The width of the canvas where the user placed a label in Project Sidewalk |
| `canvas_height` | The height of the canvas where the user placed a label in Project Sidewalk |
| `canvas_x` | The x-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| `canvas_y` | The y-pixel location where the user clicked on the canvas to place the label, where top-left is (0,0) |
| `heading` | The heading (in degrees) of the center of the canvas with respect to true north when the label was placed |
| `pitch` | The pitch (in degrees) of the center of the canvas with respect to *the camera's pitch* when the label was placed |
| `zoom` | The zoom level in the GSV interface when the user placed the label |

**This endpoint does not send `source`.** `/adminapi/panos` does, and some older CSV exports have a
column by that name, but `LabelCVMetadata` has no such field. Nothing in the cropper ever read it.

**`label_type` replaced `label_type_id`, and the cropper still needs the id.** Until
[SidewalkWebpage#4103](https://github.com/ProjectSidewalk/SidewalkWebpage/issues/4103) (released
v11.11.0, 2026-09-02) this endpoint sent `label_type_id`, an integer. The enum migration made it the
name. Crops are still filed under the numeric id, so `CropRunner.resolve_label_type_id` maps the name
back through `LABEL_TYPE_IDS_BY_NAME` and accepts **either** field, since every archived export carries
the id. An unrecognised name is one counted error naming the value, never a guessed id.

Three things to know before you join on any of this:

* **`label_id` is unique per city, not globally.** Project Sidewalk runs one database schema per city, each
  with its own serial. Key on `(city, label_id)`; a cross-city merge on `label_id` alone silently
  cross-joins. `pano_id` is the imagery source's and *is* safe as a cross-city key.
* **`pano_width`/`pano_height` are a per-pano join against current metadata**, not a click-time snapshot —
  which is exactly why the cropper's dims preflight guards the store rather than the label's frame. See
  [Cropper → The two preflights](cropper.md#the-two-preflights).
* **`pano_x`/`pano_y` are heading-centred**, unlike the legacy `sv_image_x`, which is north-referenced. Mixing
  the conventions displaces a label by up to half a panorama — and by nothing at all on a pano facing south,
  so a one-example check can pass on the wrong one. See [Depth maps → Sampling depth under a
  label](depth.md#sampling-depth-under-a-label).

## Checking the contract against a live deployment

This table, and `CropRunner`'s idea of the fields it will be handed, are a contract with another codebase.
The suite here cannot check it: it is [network-free by design](testing.md), so every test of the `-d` intake
runs against a fixture this repo wrote, and a fixture cannot notice that the server stopped agreeing with
it. It did not notice — `label_type_id` became `label_type` on 2026-09-02, the cropper produced **zero crops
against every live deployment for 16 days**, and CI was green for all of them
([#135](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/135)).

So one thing here talks to a deployment on purpose, and it is deliberately not a test:

```bash
python3 check_cvmetadata_schema.py --host sidewalk-sea.cs.washington.edu
# or: PS_CVMETADATA_HOST=sidewalk-sea.cs.washington.edu python3 check_cvmetadata_schema.py
```

There is **no default host** — a wrong default silently checks a deployment nobody chose, the same reasoning
as the log analyzer's `PS_SFTP_HOST` and the queue's `--cities`. Any deployment will do: the schema is the
app's, not the city's, so one host answers for the fleet.

It exits `0` when every field the cropper requires is served, `1` naming the ones that are not, `2` on a
usage error (no host, or a URL where a bare FQDN belongs), and `3` when the deployment could not be read at
all — a check that did not run has not passed. **`2` is deliberately not `1`**: a crontab line that was
never valid must not report the same code as the upstream rename this exists to catch.

Nonzero is the whole unattended interface, so a nightly crontab line gets the failure by mail:

```cron
30 6 * * *  /srv/sidewalk-panorama-tools/.venv/bin/python \
              /srv/sidewalk-panorama-tools/check_cvmetadata_schema.py \
              --host sidewalk-sea.cs.washington.edu
```

The production checkout is `/srv/sidewalk-panorama-tools` ([ops](ops.md#operating-the-production-host)), and the
interpreter is given by absolute path for the reason `docs/downloader.md` already states: cron's `PATH` is
minimal. A `cd … && …` form is worse than it looks here — if the `cd` fails the `&&` short-circuits and cron
reports exit `1`, which is the missing-field code.

> **The mail only arrives if the host can send mail.** As of 2026-09-17 the production box had no MTA, and
> `syslog` recorded `No MTA installed, discarding output` after every nightly run — see
> [Hearing about a bad night](ops.md#hearing-about-a-bad-night). Until that is fixed, this check's unattended
> path ends at the box. That is a property of the host, not of the check, and it applies equally to the
> downloader's own cron lines.

Three things about it are load-bearing:

* **It reads the required list off `CropRunner`** — `REQUIRED_LABEL_COLUMNS` and `LABEL_TYPE_COLUMNS`, asked
  at call time — rather than restating it. Two copies of a field list drift silently, which is the bug being
  fixed, not a shape to reproduce one level out.
* **A field the server *adds* is not a failure.** Servers add fields, and a check that cried wolf on that
  would be muted within a month; a muted tripwire is worse than none. New fields are printed, because an
  addition is often the visible half of a rename.
* **It reads only the first record.** cvMetadata is the city's entire label list — 183,682 rows for
  seattle-wa — so the response is streamed and the read stops at the first complete record, whose keys *are*
  the field names. A check that pulled the whole body nightly would cost more than the thing it guards.

What it does **not** check, deliberately:

* **The fields with a documented fallback.** `pano_width`/`pano_height` are read through `_metadata_dims`,
  which falls back to the older `width`/`height` and then to the image on disk, so their absence degrades
  the dims preflight rather than stopping the cropper. Adding them would mean writing a second field list
  that `CropRunner` has no constant for — the drift this check exists to prevent.
* **Values — only names.** A deployment serving `pano_x: null` on every row passes this check while the
  cropper errors on every label. The check answers "are the fields still called what the cropper calls
  them", which is the failure that actually happened; it is not a guarantee that the cropper will succeed.
* **Any record but the first.** The keys of the first complete record are taken as the field names, which
  assumes every record carries the same keys — true of this endpoint today, and the assumption the
  cheapness rests on. A deployment that ever omitted null fields per-record would produce a false MISSING.

One more property worth knowing before reading a red result: **a usage error exits `2`, not `1`** — so no
host, or a URL where a bare FQDN belongs, no longer wears the missing-field code.

Be precise about what that does *not* buy, because the obvious reading is wrong. Exit `2` does not make
every broken cron line identifiable: a failed `cd` exits **1** (the shell's code, not this script's), a
missing interpreter exits **127**, and CPython's own "can't open file" is itself **2**. What keeps the
`cd` case from arising is the absolute-interpreter form above, not the exit code. Read the codes as:
`1` the schema moved *or* the shell failed before the script ran, `2` the script was invoked wrongly,
`3` the deployment could not be read, `127` the interpreter path is wrong.

## Label type IDs

`/adminapi/panos` has no label types; cvMetadata now sends the **name**, and this is the id it maps to —
which is also the crop output subdirectory name. Yes, 8 is skipped — in the *data*, not in the table
below, which carries all ten enum rows. 🤷

Skipped in the *data*, that is: id 8 (`Problem`) exists in the upstream enum, so
`CropRunner.LABEL_TYPE_IDS_BY_NAME` carries it even though no label in the wild uses it.

The middle column is the name the endpoint actually sends and the key of
`CropRunner.LABEL_TYPE_IDS_BY_NAME`; the right-hand column is the display wording the web app shows a
user, which is **not** what arrives over the wire. Both are here because the pairing of id to *enum*
name is the thing that has to stay true, and a table carrying only display names cannot check it —
`tests/test_crop_runner.py` parses this table and asserts it equals the map exactly.

| `label_type_id` | `label_type` (enum name, as served) | Display name |
|---|---|---|
| 1 | `CurbRamp` | Curb Ramp |
| 2 | `NoCurbRamp` | Missing Curb Ramp |
| 3 | `Obstacle` | Obstacle in a Path |
| 4 | `SurfaceProblem` | Surface Problem |
| 5 | `Other` | Other |
| 6 | `Occlusion` | Can't see the sidewalk |
| 7 | `NoSidewalk` | No Sidewalk |
| 8 | `Problem` | (not used in the data) |
| 9 | `Crosswalk` | Crosswalk |
| 10 | `Signal` | Pedestrian Signal |
