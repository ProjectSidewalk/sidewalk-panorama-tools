# What the Panoramax catalog actually serves, ten days before Bayonne opens

**2026-09-08** · Pre-integration measurement for the third imagery source
([#110](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/110))

> **Reproduce:** `pytest tests/test_panoramax_api_census.py` offline; the live measurement is
> ```bash
> python reports/scripts/panoramax_api_census.py \
>     --write reports/data/2026-09-08-panoramax-api.json
> ```
> Keyless and read-only — no token, no account, ~1,000 metadata requests and six image downloads.

## Why measure before writing the downloader

Bayonne is the first Panoramax city and opens **2026-09-18**. On that day `/adminapi/panos` starts
serving `source: 'panoramax'`, and until #110 lands `filter_supported_sources` drops every one of those
panos as an unsupported source — the quiet failure [#101](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/101)
exists to stop: a run that completes with nothing to do, one WARNING line in cron mail, and
`missing_pano` for every Bayonne label in the cropper.

Three things about the API decide how the downloader has to be written, and none of them could be settled
from the documentation. All three turned out to matter, and one of them contradicts what
[#110](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/110) assumed.

## 1. Absence is a 404 at one endpoint and a 200 at the other

A `downloaded=0` row in `pano_id_log.csv` is permanent and never retried
([#41](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/41)), so
[#99](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/99)'s rule is that a verdict must
rest on the API *affirming* the picture is gone, never on a field being absent from whatever came back.
Two endpoints can resolve a picture id, and they answer absence in opposite ways:

| Asked about `00000000-0000-4000-8000-000000000000` | Status | Body |
|---|---|---|
| `GET /api/pictures/<id>` | **404** | `{"status": 404, "message": "Feature not found"}` |
| `GET /api/search?ids=<id>` | **200** | `{"features": []}` — **0** features |

The search endpoint's answer is absence dressed as success: exactly the body shape that must never found a
permanent verdict. So the downloader resolves by `/api/pictures/<id>`, and a test pins the empty-search
body so that a future "just use search, it is one request either way" change fails there rather than in
the ledger.

The same endpoint resolves ids across the federation. Asked about a real picture it returned **200**,
named the picture, and handed back an `hd` href on `panoramax.ign.fr` — a *different host* from the
catalog. Contributor pictures in the same bbox resolve at the same catalog to hrefs on
`panoramax.openstreetmap.fr`. The href is therefore followed, never built.

## 2. A third of the pictures in a city's bbox are not panoramas

Of **1,000** pictures the catalog lists in the Bayonne bbox:

| | Count | Field of view | Licence | Producer | Asset host |
|---|---|---|---|---|---|
| 360° panoramas | **677** | 360 | `etalab-2.0` | `sig_bayonne` | `panoramax.ign.fr` |
| Everything else | **323** | 92 | `CC-BY-SA-4.0` | `Patchanka` | `panoramax.openstreetmap.fr` |

**67.7%** of the bbox is what Project Sidewalk is here for. The rest is a contributor's GoPro HERO8
photographs — flat, rectilinear, 4000 px wide — riding the same streets, under a different licence, on a
different host. That is not a defect in the catalog; it is what a commons looks like. Panoramax is
federated and anyone can contribute, so a *place* is not a *fleet*, and no property of the city
distinguishes the two arms. Only the per-picture `pers:interior_orientation.field_of_view` does.

Nothing downstream of the downloader inspects projection. A flat 4000×2020 JPEG saved as
`<pano_id>.jpg` is a readable image of roughly the right shape at entirely the wrong projection:
`CropRunner` would cut from it with the equirectangular seam modulo, wrap `pano_x` at a seam that does not
exist, and emit a crop that looks completely plausible. So the downloader refuses a picture whose item
*affirms* a field of view other than 360, and ledgers the refusal — the same permanent-verdict shape as
Mapillary's "knows the image, publishes no original-resolution rendition".

Whether the app will ever hand us such a picture is a separate question — the viewer filters its own
search to 360° — but "the layer above currently filters correctly" is not a property this scraper can
verify, and the cost of the guard is one dictionary lookup.

### The guard was written wrong the first time, and only a test caught it

A STAC item nests everything but `id`, `assets`, `links` and `geometry` under `properties`. The first
version of `declared_field_of_view` read `field_of_view` off the item's top level — where `assets` lives —
so it returned `None` for every picture ever served and the guard never fired. Every surrounding test still
passed, because they were testing the download path, not the refusal. It surfaced only because the fixture
pinned here is a **real** flat item rather than a hand-written one, and the test asserted the refusal
rather than the parse. A guard that silently never fires is worse than no guard: the comment above it still
promises the protection.

## 3. The item does carry the frame's dimensions — and they are not 12288 px

[#110](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/110) recorded "No dimensions in
the item". There are, in two independent places, and they agree:

- `properties.pers:interior_orientation.sensor_array_dimensions` — what `PanoramaxViewer.#panoDataParams`
  writes into `pano_data.width`/`height`, and therefore what the cropper's `dims_mismatch` preflight will
  compare the stored file against;
- `properties.tiles:tile_matrix_sets.geovisio.tileMatrix[0]` — `matrixWidth × tileWidth`, which has to
  describe the full-resolution frame for the viewer's tiles adapter to address it.

Over the 677 panoramas: **677 agree**, **0 disagree**, **0 not stated**. Six `hd` assets were then
downloaded and their JPEG SOF headers read directly — **6 of 6** matched the tile matrix, sampled across
both frame sizes. That answers #110's first open upstream question: the dimensions `pano_data` gets are
the dimensions of the file this downloader stores, so the cropper will not skip every Bayonne label.

The frames themselves:

| Frame | Count |
|---|---|
| 5760×2880 | **547** |
| 5376×2688 | **130** |

Both are GoPro Max, 2:1, ~1.4 MB on the wire, ~50 MB decoded. **Not** the 12288×6144 that #110 and
SidewalkWebpage#5185 both assumed — that figure describes professional-rig imagery elsewhere in the
federation, not Bayonne's own survey. Two consequences worth recording:

- Nothing here strains the decode limits. `_MAX_PANO_PIXELS` is 16384×8192; a 5760-wide pano is a fifth of
  that, and the [#115](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/115) display-copy
  sidecar is a no-op below its 8192 px cap. The downloader still calls it, because the 12288 imagery is
  real and a later French city may be shot with it.
- The frame is **smaller than any GSV city's**, and crop sizing rule v2 normalises into a 6656 px frame
  (`reports/2026-08-19-crop-sizing-v2.md`). Bayonne crops will land at the small end of that rule. That is
  not a downloader problem and is not addressed here; it belongs with
  [#111](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/111)'s end-to-end check and
  [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54).

## What this does not answer

- **Which frame the stored `pano_x`/`pano_y` are in.** The evidence says `hd`: the viewer's tile grid
  addresses the hd frame and the dimensions it writes to `pano_data` are hd's. But no Panoramax label
  exists yet, so this is inference from the viewer's code and the catalog's own numbers, not a measurement
  against a real label. It becomes checkable the day Bayonne opens, and the check is one number:
  `dims_mismatch` in a `CropRunner` run over Bayonne must be zero.
- **Tilt.** Panoramax serves `hd.jpg` as uploaded and the rigs are not gravity-aligned — the Mapillary
  situation, not the GSV one. Owned by [#54](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/54)
  and SidewalkWebpage#5184.
- **Whether the bbox composition generalises.** One city, one bbox, 1,000 pictures. The 360/flat split is
  a fact about Bayonne on 2026-09-08, and the *guard* is what generalises, not the ratio.
- **Anything about rate limits or courtesy pacing.** ~1,000 metadata requests went out at whatever rate
  urllib managed and nothing pushed back, but that is not evidence about a nightly fleet run. The
  downloader inherits `retrying_session`'s policy and no more; if Panoramax ever pushes back, that is a
  measurement nobody has yet.

## Artifact

`reports/data/2026-09-08-panoramax-api.json` carries every count above, the per-check `hd` header
comparisons, and the verbatim 404 body. `tests/test_panoramax_api_census.py` asserts that each number in
this write-up appears in it.
