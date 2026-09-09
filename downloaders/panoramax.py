# Panoramax panorama downloader (#110).
#
# Panoramax is the French open street-level imagery commons, run by IGN and OpenStreetMap France. It is a
# FEDERATION: api.panoramax.xyz is a meta-catalog over instances that each hold their own pictures, so a
# picture id resolves at the catalog while its pixels live on whichever instance holds it. It is keyless -
# no token, no Authorization header, and therefore none of mapillary.py's TokenRedactionFilter apparatus.
#
# Bayonne is the first Panoramax city (SidewalkWebpage#5185), opening 2026-09-18. Everything measured below
# was measured against the live catalog on 2026-09-08; the numbers are in reports/2026-09-08-panoramax-api.md.

import logging
import os
import stat

from . import common
from .common import (DownloadResult, atomic_output_path, jpeg_dimensions, retrying_session,
                     write_downscaled_sidecar_from_file)

# The meta-catalog, which is also what the shipped PanoramaxViewer resolves `setPano` against. Resolving here
# rather than at an instance is what makes one base URL enough for a federation: the catalog answers for a
# picture on any instance and hands back an asset href pointing at that instance (verified 2026-09-08 for a
# picture on panoramax.ign.fr and one on panoramax.openstreetmap.fr).
CATALOG_API_BASE = 'https://api.panoramax.xyz/api'

# Panoramax carries flat photographs and 360 panoramas in the same catalog, and the item says which via
# `pers:interior_orientation.field_of_view`. Measured over 1,000 pictures in the Bayonne bbox: 677 are 360
# (Bayonne's own survey, producer sig_bayonne, etalab-2.0) and 323 are flat 92-degree GoPro HERO8 pictures
# from unrelated contributors under CC-BY-SA-4.0. See declared_field_of_view for why that matters here.
PANORAMIC_FIELD_OF_VIEW_DEG = 360


class PanoramaxErrorResponse(RuntimeError):
    """A response from the catalog that is not the picture record that was asked for (#99, #110).

    A condition of the RUN, not of the pano: it raises, the image loop counts it among tonight's failures
    and ledgers nothing, and the pano is re-attempted next run (#41). Returning DownloadResult.failure
    instead would write one permanent downloaded=0 row per pano attempted for as long as the condition
    lasted - the 2026-09-01 Mapillary incident (161 false rows, hand-edited out of pano_id_log.csv on the
    store) by another route, and this source has no token to blame it on, so the cause would be less
    obvious rather than more.
    """


def _detail(envelope):
    """A capped rendering of an untrusted error envelope for scrape.log.

    Capped for the same reason mapillary._envelope_detail is: DownloadRunner logs str(e) per failed pano
    into a 10 MB x 3 rotation, so an uncapped blob times a city's corpus rotates away the night's own
    diagnosis. BOTH fields are capped, not just the one that reads like prose - they come from the same
    untrusted place, and a proxy stuffing an HTML fragment into `status` is likelier than the catalog
    sending a 100 KB `message`.

    Takes a dict, and is not defensive about it: require_picture_record has already established that the
    body is one, and it is the only caller. A non-dict arm here would be a branch no input can reach.
    """
    return 'status=%.40s message=%.200s' % (envelope.get('status'), envelope.get('message'))


def require_picture_record(payload, pano_id):
    """Refuse any 200 body that is not the STAC item for `pano_id`; return None when it is.

    This is the positive-evidence gate (#99): every permanent verdict below rests on the catalog having
    AFFIRMED it knows this picture, never on a field being absent from whatever came back. Three shapes
    raise:

    - not a JSON object at all (a proxy's HTML error page that happened to parse, a bare list);
    - the catalog's error envelope, `{"status": ..., "message": ...}` - measured 2026-09-08 as
      `{"status":404,"message":"Feature not found"}`. It normally arrives WITH a 404 status and is handled
      there; carrying it on a 200 means something between us and the catalog rewrote the status, which is
      a condition of the run;
    - an object naming a different picture.

    str() on both sides of the id comparison for the reason mapillary's does it: the two sides come from
    different intakes and a bare != would raise for every pano forever with a message that looks like a
    match (#46). Panoramax ids are UUIDs, so nothing here is at risk of numeric coercion, but the intakes
    are the same three and the next source's ids may not be.
    """
    if not isinstance(payload, dict):
        raise PanoramaxErrorResponse("Panoramax metadata for %s was not a JSON object: %.80r"
                                     % (pano_id, payload))
    if payload.get('message') is not None and payload.get('id') is None:
        raise PanoramaxErrorResponse("Panoramax answered for %s with an error envelope (%s)"
                                     % (pano_id, _detail(payload)))
    if str(payload.get('id')) != str(pano_id):
        raise PanoramaxErrorResponse("Panoramax metadata for %s does not name that picture (id=%.80r)"
                                     % (pano_id, payload.get('id')))


def declared_field_of_view(payload):
    """The item's declared horizontal field of view in degrees, or None when it states none.

    Split out as its own seam because the caller's rule is asymmetric and easy to get backwards: a picture
    is refused only when the item AFFIRMS a field of view that is not 360. An item that states none is
    downloaded, because "no fov field" is exactly the absence-of-evidence a permanent verdict must not rest
    on (#99) - and because the field is a STAC extension the catalog is under no obligation to keep serving.

    Why the check exists at all: 32% of the pictures in the Bayonne bbox are flat 92-degree photographs
    (measured 2026-09-08), and nothing downstream would notice one. A flat rectilinear JPEG saved as
    <pano_id>.jpg is a readable image of the right shape at the wrong projection: CropRunner would cut from
    it with the equirectangular seam modulo, pano_x would wrap at a seam that is not there, and the crop
    would look plausible. That is the silent-corruption shape refetch_panos.py's frame_grew gate exists for,
    one source over.

    A STAC item nests everything but `id`, `assets`, `links` and `geometry` under `properties`, and this
    field is one of the nested ones - `assets` two functions down is not, which is exactly the asymmetry
    that made the first version of this read the top level and return None for every picture ever served.
    A guard that silently never fires is worse than no guard, because the tests around it still pass and
    the comment above still promises the protection.
    """
    properties = payload.get('properties')
    if not isinstance(properties, dict):
        return None
    interior = properties.get('pers:interior_orientation')
    if not isinstance(interior, dict):
        return None
    return interior.get('field_of_view')


def is_catalog_not_found(response):
    """Whether a 404 carries the catalog's own "this picture does not exist" body.

    Measured 2026-09-08: `{"status": 404, "message": "Feature not found"}`, pinned as
    OBSERVED_PANORAMAX_NOT_FOUND_2026_09_08 in tests/test_image_downloaders.py.

    Matched on the message, case-insensitively, and NOT on the whole body: the catalog is free to add
    fields, and a verdict that breaks when it does would turn every absent picture into a nightly retry -
    the harmless direction, but noisy forever. What must not match is an HTML error page, an empty body, or
    any JSON that does not say this - those mean something other than the catalog answered, and a permanent
    row would then be written off a stranger's reply.
    """
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    message = payload.get('message')
    return isinstance(message, str) and 'not found' in message.lower()


def is_panoramic_field_of_view(fov):
    """Whether an affirmed field_of_view means "full 360 panorama".

    Compared numerically, with a tolerance, and tolerant of the value arriving as a string. It was an exact
    `!= 360` against the int until the 2026-09-09 review, which is safe for 360.0 and for nothing else: a
    catalog that started serving `"360"`, a Decimal, or a stitched 359.9997 would route every genuine
    panorama in the city to a permanent downloaded=0 row. The module already applies this repo's #46
    doctrine - never let a comparison depend on what the values happen to look like - to the id check in
    require_picture_record, where the consequence is milder because it raises rather than ledgers. This is
    the comparison where getting it wrong is unrecoverable.

    A value that is not a number at all (a dict, a list, a word) is NOT panoramic and is refused, which is
    the same call the flat-photograph guard makes: the catalog affirmed something, and it is not 360.
    """
    try:
        return abs(float(fov) - PANORAMIC_FIELD_OF_VIEW_DEG) < 0.5
    except (TypeError, ValueError):
        return False


def hd_asset_url(payload):
    """The href of the item's full-resolution `hd` asset, or None when it publishes none.

    None is returned for exactly one shape: an assets block that is present, well-formed, and does not offer
    `hd`. That is the catalog listing this picture's renditions and hd not being among them - a permanent
    property of the picture, the same verdict as Mapillary's "knows the image and publishes no
    original-resolution rendition".

    Everything else about the assets block RAISES, so the pano retries unledgered. A missing block, a
    non-object block, a non-object `hd`, or an `hd` whose href is null or empty are all the catalog changing
    shape, and require_picture_record cannot have ruled that out - it establishes only that the body is a
    dict whose id matches. Ledgering those was a 2026-09-09 review finding: one `?fields=` slimming and a
    whole city gets a permanent downloaded=0 in a single night.

    `hd`, not `sd` or the tile pyramid, because `hd` is the frame the stored pano_x/pano_y describe.
    PanoramaxViewer.#panoDataParams writes `pers:interior_orientation.sensor_array_dimensions` into
    pano_data, and those dimensions were equal to the hd JPEG's own SOF header on every picture measured
    (677/677 against the tile matrix, 6/6 against the JPEG itself, 2026-09-08). Fetching `sd` instead would
    put a smaller frame on the store under dimensions describing a larger one, and CropRunner's
    dims_mismatch preflight would skip every label in the city - loudly, which is the good failure, but for
    a reason nobody would guess from the message.

    The href is FOLLOWED, never built: it points at whichever federated instance holds the picture
    (panoramax.ign.fr for Bayonne's own survey, panoramax.openstreetmap.fr for contributor pictures in the
    same bbox), and the catalog is the only thing that knows which. Raises rather than returns for an href
    that is not an absolute https URL - relative hrefs do occur in the same document (the `rel: related`
    links are `/api/...` paths), so a relative one here means the catalog changed shape, and an http:// or
    file:// one means something is answering for it that should not be. Neither is a verdict on the pano.
    """
    assets = payload.get('assets')
    if assets is None:
        # The catalog affirmed the picture but sent no assets block AT ALL. That is a changed response
        # shape, not a statement about this picture's pixels - require_picture_record established only that
        # the body is a dict whose id matches, which `{"id": "<uuid>"}` alone satisfies. Same reasoning as
        # declared_field_of_view's missing-properties arm one function up, and the same failure if it were
        # ledgered instead: a `?fields=` slimming, or assets moving under properties the way
        # pers:interior_orientation already sits there, would write off a whole city in one night with
        # nothing loud about it (2026-09-09 review).
        raise PanoramaxErrorResponse("Panoramax picture %.80r came back with no assets block" %
                                     (payload.get('id'),))
    if not isinstance(assets, dict):
        raise PanoramaxErrorResponse("Panoramax picture %.80r has a non-object assets block: %.80r" %
                                     (payload.get('id'), type(assets).__name__))
    if 'hd' not in assets:
        # An assets block that is present and simply has no `hd` entry IS the verdict: the catalog is
        # telling us which renditions exist for this picture and hd is not among them.
        return None
    if not isinstance(assets['hd'], dict):
        raise PanoramaxErrorResponse("Panoramax picture %.80r has a non-object hd asset: %.80r" %
                                     (payload.get('id'), type(assets['hd']).__name__))
    if 'href' not in assets['hd']:
        return None
    href = assets['hd'].get('href')
    if not isinstance(href, str) or not href:
        # An hd asset that exists with a null or empty href is malformed, not a verdict - the same call the
        # relative-href branch below already makes, and it was the one shape that ledgered instead.
        raise PanoramaxErrorResponse("Panoramax picture %.80r has an hd asset with no usable href: %.80r" %
                                     (payload.get('id'), href))
    if not href.startswith('https://'):
        raise PanoramaxErrorResponse("Panoramax gave picture %.80r an hd href that is not an absolute https "
                                     "URL: %.200r" % (payload.get('id'), href))
    return href


def _write_display_copy(out_image_name, pano_id):
    """The viewer's copy of a pano wider than it can texture (#115), beside the native file.

    OFF by default since 2026-09-09 - common.WRITE_DISPLAY_COPIES carries the measurements. Same shape and
    same guard as gsv._write_display_copy and mapillary._write_display_copy, deliberately: three downloaders
    reading one switch three different ways is how one of them ends up still writing.

    A no-op for Bayonne even with the switch on, whose frames are 5760 and 5376 wide (measured 2026-09-08)
    against an 8192 cap, and not a no-op for the 12288-wide professional-rig imagery elsewhere in the
    federation.

    From the file rather than a raster, because this path never decodes the image. Never fatal when it is
    on, for the reason the other two give: the native file is already the resume marker, and
    downscale_panos.py heals a missing sidecar.
    """
    if not common.WRITE_DISPLAY_COPIES:
        return
    try:
        write_downscaled_sidecar_from_file(out_image_name)
    except Exception as e:
        logging.error("Panoramax pano %s: display copy not written: %r", pano_id, e)


def download_single_pano(storage_path, pano_info):
    pano_id = pano_info['pano_id']

    destination_dir = os.path.join(storage_path, pano_id[:2])
    if not os.path.isdir(destination_dir):
        # exist_ok: concurrent runs race on shard dirs.
        os.makedirs(destination_dir, exist_ok=True)
        try:
            os.chmod(destination_dir, 0o775 | stat.S_ISGID)
        except PermissionError:
            pass  # lost the race to another user's process; their dir, their modes — must not fail the pano

    out_image_name = os.path.join(destination_dir, pano_id + ".jpg")
    if os.path.isfile(out_image_name):
        return DownloadResult.skipped

    # Context-managed so the per-pano connection pool is released deterministically, not at GC (#51).
    with retrying_session() as session:
        meta_resp = session.get('%s/pictures/%s' % (CATALOG_API_BASE, pano_id), timeout=30)

        if meta_resp.status_code == 404:
            # The catalog does not have this id: a permanent property of the pano, so it ledgers (#41) - but
            # ONLY if the catalog itself said so. Measured 2026-09-08, the body is
            # `{"status":404,"message":"Feature not found"}`, and that body is what makes this a verdict.
            #
            # The status alone is not enough, and this used to read no further than it (2026-09-09 review).
            # The stated reason was that a keyless API has no "we are not allowed to see it" state to
            # confuse with "it is not there" - true, and it answers the wrong question. It does not answer
            # "did the CATALOG answer at all". api.panoramax.xyz is a keyless community API and this is one
            # path segment of it: a renamed endpoint, a CDN or WAF answering 404 for an unrecognised UA, or
            # DNS landing on a parked host all produce a 404 for every pano in the city, and every one of
            # them would ledger. That is the 2026-09-01 Mapillary incident - 161 false rows hand-edited off
            # the shared store - at whole-city scale, and unlike Mapillary this source has no #113 breaker
            # entry to stop it. The affirmation is free in the same response body, so read it.
            if not is_catalog_not_found(meta_resp):
                raise PanoramaxErrorResponse(
                    "Panoramax answered 404 for picture %s without the catalog's not-found body, so "
                    "something other than the catalog may have answered: %.200r"
                    % (pano_id, meta_resp.text))
            logging.error("Panoramax has no picture %s (404, catalog confirms not found)", pano_id)
            return DownloadResult.failure
        # Everything else non-200 is a condition of the RUN - 429/5xx have already exhausted the retry
        # adapter, and a 4xx that is not 404 from a keyless API is the catalog or something in front of it
        # misbehaving. Raising retries the pano next run; returning failure would ledger it permanently, so
        # one bad night would blacklist every Panoramax pano in the city (#41).
        meta_resp.raise_for_status()

        try:
            payload = meta_resp.json()
        except ValueError:
            # A proxy error page or a body truncated mid-flight - transient, so let it propagate (#41).
            logging.error("Panoramax metadata response for %s was not valid JSON", pano_id)
            raise

        # Raises unless the catalog affirmed this exact picture. Everything below is allowed to draw a
        # permanent conclusion only because this line already ran (#99).
        require_picture_record(payload, pano_id)

        fov = declared_field_of_view(payload)
        if fov is not None and not is_panoramic_field_of_view(fov):
            # A permanent property of the picture: it is a flat photograph, and this scraper stores
            # equirectangular panoramas. Ledgering is what stops the whole flat third of the catalog from
            # being re-fetched, decoded and discarded every night.
            logging.error("Panoramax picture %s is not a 360 panorama (field_of_view=%.40r); not storing it",
                          pano_id, fov)
            return DownloadResult.failure

        image_url = hd_asset_url(payload)
        if image_url is None:
            logging.error("Panoramax knows picture %s but publishes no hd asset", pano_id)
            return DownloadResult.failure

        # allow_redirects=False, deliberately (2026-09-09 review). This is the one URL in the repo that is
        # taken from a remote response rather than built, on a public commons anyone can contribute to, and
        # requests follows up to 30 redirects to ANY scheme and host by default - so the https check above
        # would cover the first hop only, and a 302 to http://169.254.169.254/ or a private address would be
        # followed and its body streamed into the store. A federated instance serving its own pictures has
        # no reason to redirect; if one starts, that is the catalog changing shape and belongs in a review,
        # not in a silent fetch.
        image_resp = session.get(image_url, stream=True, timeout=120, allow_redirects=False)
        if image_resp.is_redirect or image_resp.is_permanent_redirect:
            raise PanoramaxErrorResponse(
                "Panoramax hd asset for picture %s redirected (%s); not following a redirect off the href "
                "the catalog published" % (pano_id, image_resp.status_code))
        # A non-200 here is the instance being unreachable or mid-deploy, never a property of the pano - the
        # metadata request above already proved the imagery exists (#41). Unlike Mapillary's, this URL is a
        # plain public path with no signature in it, so nothing secret reaches scrape.log via the HTTPError.
        image_resp.raise_for_status()

        # .part + rename: iter_content can die mid-stream (reset connection, full store), and a truncated
        # .jpg left at the final path is reported as a completed download by every later run.
        with atomic_output_path(out_image_name) as tmp_path:
            with open(tmp_path, 'wb') as f:
                for chunk in image_resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            # A 200 from an instance is no more proof the body is the image than a 200 from the catalog is
            # proof the body is the record - an instance mid-deploy can answer 200 with an HTML holding
            # page. Saved as .jpg that page IS the resume marker: permanent, with no ledger row to edit and
            # an error per label in the cropper every night. Checked before the rename, so the .part is
            # discarded and the pano retries (#41). Same header-only test, and the same gap, as the
            # Mapillary path: it refuses a body that is not a JPEG at all, not one truncated after its
            # header, which a short read out of iter_content has already raised on.
            if jpeg_dimensions(tmp_path) is None:
                raise PanoramaxErrorResponse("Panoramax image response for %s was not a JPEG" % pano_id)
    _write_display_copy(out_image_name, pano_id)
    return DownloadResult.success
