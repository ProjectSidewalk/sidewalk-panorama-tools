"""GSV user photospheres - `CAoS...` pano ids - fetched from the photo host, not cbk (#223).

cbk has no tiles for a photosphere: its probe answered 400 for every one of 27 ids (2026-10-21), which since
#166 is a transient raise, so they were re-attempted every night and never reached the store, labels and all.
A CAoS id is base64url of a small protobuf (08 0a 12 <len> <photo id>), and the photo host serves the full-size
equirectangular original for the photo id inside. What these tests hold:

* **The decode** - the three measured forms (AF1Qip..., the older CIHM... and CIAB...), `.` as `=` padding,
  omitted padding, and None for every shape that is not exactly that protobuf, which sends the pano down the
  cbk path unchanged; and the form read off the photo id's leading letters.
* **The contract, with its request count** - ONE photo-host request, and never photometa, the probe or a tile:
  a complete JPEG at the app's frame is saved byte for byte; a frame the app disagrees with is the tile path's
  FrameDisagreementError; no app frame means the served one is the frame; an interstitial landing raises the
  HTTPError _get_response would (push-back to the #162 breaker), a retried-out 429 is the RetryError that breaker
  already reads; a 400 for the CIHM or CIAB form is the ONE permanent verdict that rests on the photo host
  (DownloadResult.failure, 628 of 628 measured in reports/2026-11-05-photosphere-forms.md) and any other non-200
  (an AF1Qip 400, a 403) is PhotosphereHostError, never push-back; a 200 that is not a complete JPEG raises,
  while one followed by a measured camera trailer (store mode's 64 KB rule) is saved, trailer and all.
* **The composition through DownloadRunner's image loop** into pano_id_log.csv and the run summary.

Network-free: the photo host is stubbed at gsv._photosphere_session, photometa at gsv._fetch_image_levels,
the probe at gsv._get_response, tiles at gsv._download_tiles.
"""

import base64
import csv
import logging
import os
from io import BytesIO

import pytest
import requests
from PIL import Image, ImageFile

from downloaders import common, gsv
from downloaders.common import DownloadResult, jpeg_dimensions
from test_gsv_stitcher import deny_probe, stub_photometa, stub_tiles

# Real ids from #223, one of each form, and the photo ids they decode to (measured by the issue's decode).
AF1Q_PANO = 'CAoSLEFGMVFpcE05ZHg3dFhIa1htX193c1JDUW10UmRKd1J4dDY1Ujk5cDFGYWlu'
AF1Q_PHOTO = 'AF1QipM9dx7tXHkXm__wsRCQmtRdJwRxt65R99p1Fain'
CIHM_PANO = 'CAoSF0NJSE0wb2dLRUlDQWdJQ0U0ZXpLOXdF'
CIHM_PHOTO = 'CIHM0ogKEICAgICE4ezK9wE'
CIAB_PANO = 'CAoSHENJQUJJaEFEeWNLejZoTFNPbWY2MWNzQUJ6RDA.'
CIAB_PHOTO = 'CIABIhADycKz6hLSOmf61csABzD0'
OTHER_PHOTO = 'ZZZZsomeUnseenForm'          # no such form has been seen; built below so its 400 can be tested

FRAME = (64, 32)


def caos(photo_id_bytes, length=None, pad='.'):
    """A CAoS id for `photo_id_bytes`, built the way Google's are: base64url of 08 0a 12 <len> <id>, with `=`
    padding written as `pad` ('' omits it)."""
    n = len(photo_id_bytes) if length is None else length
    encoded = base64.urlsafe_b64encode(b'\x08\x0a\x12' + bytes([n]) + photo_id_bytes).decode()
    return encoded.replace('=', pad)


OTHER_PANO = caos(OTHER_PHOTO.encode())


def photosphere_jpeg(size=FRAME, color=(40, 120, 200)):
    buf = BytesIO()
    Image.new('RGB', size, color).save(buf, 'jpeg', quality=90)
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status_code=200, content=b'', content_type='image/jpeg', url=None, history=()):
        self.status_code = status_code
        self.content = content
        self.headers = requests.structures.CaseInsensitiveDict(
            {} if content_type is None else {'Content-Type': content_type})
        self.url = url
        self.history = list(history)
        self.closed = False

    def close(self):
        self.closed = True


class PhotoHost:
    """A stand-in for the photo host's requests session: records every request, answers from `answer`
    (a FakeResponse, or a callable of the URL returning one)."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.responses = []
        self.exited = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False

    def get(self, url, **kwargs):
        self.requests.append(dict(kwargs, url=url))
        response = self.answer(url) if callable(self.answer) else self.answer
        if isinstance(response, BaseException):
            raise response            # what the session itself raises: a retry policy that gave up
        if response.url is None:
            response.url = url
        self.responses.append(response)
        return response


def stub_photo_host(monkeypatch, answer):
    host = PhotoHost(answer)
    monkeypatch.setattr(gsv, '_photosphere_session', lambda: host)
    return host


def serve(body=None, **kwargs):
    return FakeResponse(content=photosphere_jpeg() if body is None else body, **kwargs)


def info(pano_id=AF1Q_PANO, frame=FRAME):
    record = {'pano_id': pano_id}
    if frame is not None:
        record.update(width=frame[0], height=frame[1])
    return record


def stored(tmp_path, pano_id=AF1Q_PANO):
    return tmp_path / pano_id[:2] / (pano_id + '.jpg')


@pytest.fixture
def cbk_untouched(monkeypatch):
    """Photometa, the probe and the tile fan-out must not be asked about a photosphere."""
    def no_photometa(pano_id, session):
        pytest.fail('photometa must not be asked about a photosphere')

    monkeypatch.setattr(gsv, '_fetch_image_levels', no_photometa)
    deny_probe(monkeypatch)
    stub_tiles(monkeypatch, lambda tile: pytest.fail('a photosphere has no cbk tiles to fetch'))


# --- the decode ------------------------------------------------------------------------------------------

class TestPhotosphereIds:
    def test_the_prefix_is_the_test(self):
        assert gsv.is_photosphere_id(AF1Q_PANO)
        assert gsv.is_photosphere_id(CIHM_PANO)
        assert not gsv.is_photosphere_id('stitchPanoAAAAAAAAAAAA')
        assert not gsv.is_photosphere_id('caoSLEFGMVFp'), 'case matters: base64 is case-sensitive'

    @pytest.mark.parametrize('pano_id,photo_id', [(AF1Q_PANO, AF1Q_PHOTO), (CIHM_PANO, CIHM_PHOTO)],
                             ids=['AF1Qip-form', 'CIHM-form'])
    def test_both_measured_forms_decode_to_their_photo_id(self, pano_id, photo_id):
        assert gsv.photosphere_photo_id(pano_id) == photo_id

    @pytest.mark.parametrize('photo_id,dots', [(b'AF1QipAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB', 2),  # 4+45 bytes
                                               (b'AF1QipAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB_', 1)])  # 4+46 bytes
    def test_a_dot_stands_for_padding(self, photo_id, dots):
        pano_id = caos(photo_id)
        assert len(pano_id) - len(pano_id.rstrip('.')) == dots, pano_id
        assert gsv.photosphere_photo_id(pano_id) == photo_id.decode()

    def test_omitted_padding_decodes_too(self):
        photo_id = b'AF1QipAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB'
        pano_id = caos(photo_id, pad='')
        assert len(pano_id) % 4
        assert gsv.photosphere_photo_id(pano_id) == photo_id.decode()

    @pytest.mark.parametrize('pano_id', [
        caos(b'AF1QipABCDEFG', length=14),        # the length byte claims more than is there
        caos(b'AF1QipABCDEFG', length=12),        # ... or less
        caos(b'', length=0),                      # nothing inside
        caos(b'AF1Qip/ABCDEF'),                   # not URL-safe: a '/' would change the URL's path
        caos(b'AF1Qip\xe9ABCDEF'),                # not ASCII
        'CAoSa',                                  # a base64 error (five characters cannot be padded valid)
        'CAoSLEFGMVFp!E05ZHg3',                   # a character outside base64url
        'CAoSLEFG.MVFp',                          # a '.' that is not trailing padding
        'stitchPanoAAAAAAAAAAAA',                 # not a photosphere at all
    ], ids=['length-over', 'length-under', 'empty', 'slash', 'non-ascii', 'b64-error', 'bad-char',
            'inner-dot', 'not-caos'])
    def test_every_other_shape_is_none(self, pano_id):
        assert gsv.photosphere_photo_id(pano_id) is None


# --- the contract ----------------------------------------------------------------------------------------

class TestThePhotoHostContract:
    def test_the_photo_host_session_retries_and_times_out_like_the_probes(self):
        """_request_session's policy, not a bare Session: no default timeout means one hung connection stalls
        the night forever (#65 item 5)."""
        with gsv._photosphere_session() as session:
            adapter = session.get_adapter('https://lh3.ggpht.com/p/x=s0')
            assert isinstance(adapter, gsv._TimeoutHTTPAdapter)
            assert adapter._timeout == 30
            assert adapter.max_retries.total == 5

    def test_a_complete_jpeg_at_the_apps_frame_is_saved_byte_for_byte_in_one_request(self, tmp_path,
                                                                                    monkeypatch, cbk_untouched):
        body = photosphere_jpeg()
        host = stub_photo_host(monkeypatch, serve(body))

        assert gsv.download_single_pano(str(tmp_path), info()) == DownloadResult.success

        assert [r['url'] for r in host.requests] == ['https://lh3.ggpht.com/p/%s=s0' % AF1Q_PHOTO]
        assert host.exited == 1, 'the session is context-managed (#51)'
        assert host.requests[0]['headers'] in gsv.headers_list
        assert stored(tmp_path).read_bytes() == body, 'the host encode, verbatim - no PIL round trip'
        assert not (tmp_path / AF1Q_PANO[:2] / (AF1Q_PANO + '.jpg.part')).exists()

    def test_string_dims_from_a_csv_intake_compare_as_numbers(self, tmp_path, monkeypatch, cbk_untouched):
        stub_photo_host(monkeypatch, serve())
        record = {'pano_id': AF1Q_PANO, 'width': '64', 'height': '32'}
        assert gsv.download_single_pano(str(tmp_path), record) == DownloadResult.success

    def test_the_proxies_are_filtered_as_the_probe_filters_them(self, tmp_path, monkeypatch, cbk_untouched):
        """A normalized-away placeholder must reach requests as absent, not None (#51)."""
        monkeypatch.setattr(gsv, '_proxies', {'http': None, 'https': 'http://proxy.example:3128'})
        host = stub_photo_host(monkeypatch, serve())
        gsv.download_single_pano(str(tmp_path), info())
        assert host.requests[0]['proxies'] == {'https': 'http://proxy.example:3128'}

    def test_a_pano_already_on_disk_costs_no_request(self, tmp_path, monkeypatch, cbk_untouched):
        stored(tmp_path).parent.mkdir()
        stored(tmp_path).write_bytes(b'already here')
        host = stub_photo_host(monkeypatch, serve())

        assert gsv.download_single_pano(str(tmp_path), info()) == DownloadResult.skipped
        assert host.requests == []
        assert stored(tmp_path).read_bytes() == b'already here', 'the download path never overwrites'

    def test_a_frame_the_app_disagrees_with_is_a_frame_disagreement_and_nothing_is_written(
            self, tmp_path, monkeypatch, capsys, cbk_untouched):
        host = stub_photo_host(monkeypatch, serve(photosphere_jpeg((96, 48))))

        with pytest.raises(gsv.FrameDisagreementError) as refused:
            gsv.download_single_pano(str(tmp_path), info())

        message = str(refused.value)
        assert 'frame disagreement' in message and AF1Q_PANO in message and 'photosphere' in message
        assert '64x32' in message and '96x48' in message, 'both frames are named'
        out = capsys.readouterr().out
        assert 'WARNING' in out and 'frame disagreement' in out and AF1Q_PANO in out
        assert len(host.requests) == 1
        assert list(tmp_path.rglob('*.jpg*')) == []

    @pytest.mark.parametrize('record', [info(frame=None), dict(info(frame=None), width=None, height=None),
                                        dict(info(frame=None), width=64)],
                             ids=['keys-omitted', 'blank-values', 'height-missing'])
    def test_no_app_frame_means_the_served_frame_is_the_frame(self, tmp_path, monkeypatch, caplog, record,
                                                              cbk_untouched):
        """#184's shape. Not counted as a frame from photometa: photometa was never asked."""
        stub_photo_host(monkeypatch, serve(photosphere_jpeg((96, 48))))

        with caplog.at_level(logging.INFO):
            assert gsv.download_single_pano(str(tmp_path), record) == DownloadResult.success

        assert jpeg_dimensions(str(stored(tmp_path))) == (96, 48)
        assert gsv._photometa_run.frames_from_photometa == 0
        assert any('frame from the photo host: 96x48' in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize('pano_id, photo_id, status', [
        (AF1Q_PANO, AF1Q_PHOTO, 400), (AF1Q_PANO, AF1Q_PHOTO, 403), (AF1Q_PANO, AF1Q_PHOTO, 404),
        (AF1Q_PANO, AF1Q_PHOTO, 500), (CIHM_PANO, CIHM_PHOTO, 403), (CIHM_PANO, CIHM_PHOTO, 404),
        (CIHM_PANO, CIHM_PHOTO, 500), (CIAB_PANO, CIAB_PHOTO, 403), (CIAB_PANO, CIAB_PHOTO, 404),
        (CIAB_PANO, CIAB_PHOTO, 500), (OTHER_PANO, OTHER_PHOTO, 400)])
    def test_a_non_200_raises_unledgerable_and_is_not_push_back(self, tmp_path, monkeypatch, pano_id, photo_id,
                                                                 status, cbk_untouched):
        """Transient, as the cbk probe's non-200 is (#166) - every status for the AF1Qip form, whose 400 has never
        been seen, and every status but 400 for the refused forms. A 403 is in the list on purpose: it is one
        photo's answer, and must never read as the host refusing us (#162), or three dead photos would stand the
        fleet's depth phase down (#227 review)."""
        response = FakeResponse(status, b'<html>Error</html>', 'text/html')
        host = stub_photo_host(monkeypatch, response)

        with pytest.raises(gsv.PhotosphereHostError) as raised:
            gsv.download_single_pano(str(tmp_path), info(pano_id))

        assert raised.value.status_code == status
        assert not isinstance(raised.value, requests.HTTPError), 'pushback_reason reads an HTTPError 403 as push-back'
        assert response.closed
        assert gsv.pushback_reason(raised.value) is None
        assert [r['url'] for r in host.requests] == ['https://lh3.ggpht.com/p/%s=s0' % photo_id]
        assert list(tmp_path.rglob('*.jpg*')) == []

    @pytest.mark.parametrize('pano_id, photo_id, form', [(CIHM_PANO, CIHM_PHOTO, 'CIHM'), (CIAB_PANO, CIAB_PHOTO, 'CIAB')])
    def test_a_400_for_a_refused_form_is_the_permanent_verdict(self, tmp_path, monkeypatch, caplog, pano_id,
                                                              photo_id, form, cbk_untouched):
        """The one permanent verdict that rests on the photo host: 628 of 628 CIHM and CIAB ids in chicago-il
        answered 400 with one text/html error page, and one id per form the same at two more hosts
        (reports/2026-11-05-photosphere-forms.md). Keyed on the status, the form AND the error page: the AF1Qip
        400 above stays a raise, and so does a 400 of another content type (below)."""
        response = FakeResponse(400, b'<html>Error 400</html>', 'text/html')
        host = stub_photo_host(monkeypatch, response)

        with caplog.at_level(logging.INFO):
            result = gsv.download_single_pano(str(tmp_path), info(pano_id))

        assert result == DownloadResult.failure
        assert response.closed
        assert [r['url'] for r in host.requests] == ['https://lh3.ggpht.com/p/%s=s0' % photo_id]
        assert list(tmp_path.rglob('*.jpg*')) == []
        assert any('refuses the %s form' % form in r.getMessage() and pano_id in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.parametrize('content_type', ['image/jpeg', 'application/json', None])
    def test_a_400_for_a_refused_form_without_the_hosts_error_page_still_raises(self, tmp_path, monkeypatch,
                                                                             content_type, cbk_untouched):
        """The verdict asks for the host's own text/html error page, as the Panoramax verdict asks for the
        catalog's body: a 400 of another shape is something else and stays a transient."""
        stub_photo_host(monkeypatch, FakeResponse(400, b'', content_type))
        with pytest.raises(gsv.PhotosphereHostError):
            gsv.download_single_pano(str(tmp_path), info(CIHM_PANO))
        assert list(tmp_path.rglob('*.jpg*')) == []

    def test_a_refused_form_pano_already_on_disk_costs_no_request(self, tmp_path, monkeypatch, cbk_untouched):
        host = stub_photo_host(monkeypatch, lambda url: pytest.fail('a stored pano must not be asked for'))
        target = stored(tmp_path, CIHM_PANO)
        target.parent.mkdir(parents=True)
        target.write_bytes(b'stored')
        assert gsv.download_single_pano(str(tmp_path), info(CIHM_PANO)) == DownloadResult.skipped
        assert host.requests == []

    def test_an_interstitial_at_a_400_for_a_refused_form_is_still_push_back(self, tmp_path, monkeypatch,
                                                                            cbk_untouched):
        """The landing is checked before the status: a captcha page wearing a 400 is Google's refusal, not the
        form's, and must not be written off as the pano's verdict."""
        response = FakeResponse(400, b'', 'text/html', url='https://www.google.com/sorry/index?continue=x')
        stub_photo_host(monkeypatch, response)
        with pytest.raises(requests.HTTPError):
            gsv.download_single_pano(str(tmp_path), info(CIHM_PANO))
        assert list(tmp_path.rglob('*.jpg*')) == []

    @pytest.mark.parametrize('photo_id, form', [
        (AF1Q_PHOTO, 'AF1Qip'), (CIHM_PHOTO, 'CIHM'), (CIAB_PHOTO, 'CIAB'), ('ZZZZsomething', 'other')])
    def test_the_form_is_the_photo_ids_leading_letters(self, photo_id, form):
        assert gsv.photosphere_form(photo_id) == form
        assert set(gsv.PHOTOSPHERE_REFUSED_FORMS) == {'CIHM', 'CIAB'} and 'AF1Qip' not in gsv.PHOTOSPHERE_REFUSED_FORMS

    @pytest.mark.parametrize('status, reason', [(429, 'HTTP 429'), (503, None)])
    def test_a_retried_out_status_is_the_sessions_retry_error(self, tmp_path, monkeypatch, status, reason,
                                                               cbk_untouched):
        """The photo host rides _request_session, whose policy retries 429 and 5xx and gives up with a RetryError
        before _download_photosphere sees a status - so a 429 is push-back through the shape pushback_reason
        already reads, and a 5xx storm is weather, exactly as on the zoom probe (#162, #177)."""
        stub_photo_host(monkeypatch, requests.exceptions.RetryError('too many %d error responses' % status))
        with pytest.raises(requests.exceptions.RetryError) as raised:
            gsv.download_single_pano(str(tmp_path), info())
        assert gsv.pushback_reason(raised.value) == reason
        assert list(tmp_path.rglob('*.jpg*')) == []

    def test_an_interstitial_at_a_403_is_still_push_back(self, tmp_path, monkeypatch, cbk_untouched):
        """A captcha page wearing a 403 is Google's refusal, whatever the status: the landing is checked first."""
        response = FakeResponse(403, b'', 'text/html', url='https://www.google.com/sorry/index?continue=x')
        stub_photo_host(monkeypatch, response)
        with pytest.raises(requests.HTTPError) as raised:
            gsv.download_single_pano(str(tmp_path), info())
        # pushback_reason names the status before the landing, so 'HTTP 403' here; either way it counts.
        assert gsv.pushback_reason(raised.value) in ('HTTP 403', 'interstitial')
        assert response.closed

    def test_a_200_on_googles_interstitial_is_push_back_and_nothing_is_written(self, tmp_path, monkeypatch,
                                                                               cbk_untouched):
        response = serve(url='https://www.google.com/sorry/index?continue=x')
        stub_photo_host(monkeypatch, response)

        with pytest.raises(requests.HTTPError) as raised:
            gsv.download_single_pano(str(tmp_path), info())

        assert gsv.pushback_reason(raised.value) == 'interstitial'
        assert response.closed
        assert list(tmp_path.rglob('*.jpg*')) == []

    @pytest.mark.parametrize('response', [
        FakeResponse(200, b'<html>not a picture</html>', 'text/html; charset=utf-8'),
        FakeResponse(200, photosphere_jpeg(), None),                           # no Content-Type at all
        FakeResponse(200, photosphere_jpeg()[:-2]),                            # cut short: no EOI last
        FakeResponse(200, photosphere_jpeg()[:-200]),                          # cut mid-file
        FakeResponse(200, b'\x89PNG\r\n\x1a\n' + b'\x00' * 64 + b'\xff\xd9'),  # no SOI first
        FakeResponse(200, b'\xff\xd8' + b'\x00' * 64 + b'\xff\xd9'),           # SOI and EOI, no readable SOF
    ], ids=['html', 'no-content-type', 'no-eoi', 'truncated', 'no-soi', 'no-sof'])
    def test_a_200_that_is_not_a_complete_jpeg_raises_and_writes_nothing(self, tmp_path, monkeypatch,
                                                                        response, cbk_untouched):
        stub_photo_host(monkeypatch, response)

        with pytest.raises(gsv.PhotosphereBodyError) as raised:
            gsv.download_single_pano(str(tmp_path), info())

        assert not isinstance(raised.value, gsv.FrameDisagreementError), \
            'a bad body is not an answer about the frame, and #207 counts that type by isinstance'
        assert gsv.pushback_reason(raised.value) is None
        assert list(tmp_path.rglob('*.jpg*')) == []

    # The two trailer shapes measured 2026-11-09 on photospheres the EOI-last check refused every night
    # (reports/2026-11-09-photosphere-404s-and-trailers.md): Samsung's 111-byte `Image_UTC_Data` record, and
    # zero padding (130 and 3,257 bytes). The Samsung record's first 32 bytes are as served; its other 79 are
    # stand-in zeros here (only its length and the absence of FF D9 matter to the rule).
    SAMSUNG_TRAILER = b'\x00\x00\x01\n\x0e\x00\x00\x00Image_UTC_Data1552162688' + b'\x00' * 79

    @pytest.mark.parametrize('trailer', [SAMSUNG_TRAILER, b'\x00' * 130, b'\x00' * 3257],
                             ids=['samsung-111', 'zero-130', 'zero-3257'])
    def test_a_complete_jpeg_with_a_measured_trailer_is_saved_trailer_and_all(self, tmp_path, monkeypatch,
                                                                             trailer, cbk_untouched):
        body = photosphere_jpeg() + trailer
        assert len(self.SAMSUNG_TRAILER) == 111
        stub_photo_host(monkeypatch, serve(body))

        assert gsv.download_single_pano(str(tmp_path), info()) == DownloadResult.success
        assert stored(tmp_path).read_bytes() == body, 'verbatim means the trailer too - never trimmed'

    def test_a_trailer_longer_than_the_window_is_still_refused(self, tmp_path, monkeypatch, cbk_untouched):
        """The window bounds how much trailing data is believed, as in store mode: one byte past it refuses."""
        stub_photo_host(monkeypatch, serve(photosphere_jpeg() + b'\x00' * (common.TRAILING_BYTES_WINDOW - 1)))

        with pytest.raises(gsv.PhotosphereBodyError, match='not a complete JPEG'):
            gsv.download_single_pano(str(tmp_path), info())
        assert list(tmp_path.rglob('*.jpg*')) == []

    def test_a_cut_body_with_bytes_after_it_is_still_refused(self, tmp_path, monkeypatch, cbk_untouched):
        """What the old EOI-last check also caught, and the trailer rule must not wave through: a body cut mid-
        scan carries no FF D9 after its scan start, whatever follows it."""
        stub_photo_host(monkeypatch, serve(photosphere_jpeg()[:-200] + self.SAMSUNG_TRAILER))

        with pytest.raises(gsv.PhotosphereBodyError) as raised:
            gsv.download_single_pano(str(tmp_path), info())
        assert 'Content-Length' in str(raised.value) and 'last EOI at offset' in str(raised.value)
        assert list(tmp_path.rglob('*.jpg*')) == []

    @pytest.mark.parametrize('error, answer', [
        (gsv.PhotosphereHostError('x', 404), True),
        (gsv.PhotosphereHostError('x', 403), True),
        (gsv.PhotosphereHostError('x', 400), True),
        (gsv.PhotosphereHostError('x', 500), False),
        (gsv.PhotosphereHostError('x', 503), False),
        (gsv.PhotosphereBodyError('x'), True),
        (requests.exceptions.RetryError('too many 429 error responses'), False),
        (requests.HTTPError('interstitial'), False),
        (requests.exceptions.ConnectionError('reset'), False),
        (requests.exceptions.ReadTimeout('slow'), False),
        (gsv.FrameDisagreementError('frame disagreement'), False),
    ], ids=['404', '403', '400', '500', '503', 'body', 'retry-error', 'interstitial', 'connection', 'timeout',
            'frame'])
    def test_what_images_no_success_sets_aside_as_the_photo_host_answering(self, error, answer):
        """A 4xx or an unusable 200 is the host answering about one photo; anything that can be an outage of
        the host, the network or this code is not (reports/2026-11-09-photosphere-404s-and-trailers.md)."""
        assert gsv.is_photo_host_answer(error) is answer

    def test_a_body_that_does_not_decode_is_not_stored(self, tmp_path, monkeypatch, cbk_untouched):
        """PIL decodes a mid-scan cut that still ends in EOI without complaint, so the decode failure is
        injected; what is pinned is that a failed decode stops the save."""
        stub_photo_host(monkeypatch, serve())

        def broken_load(self):
            raise OSError('broken data stream')

        monkeypatch.setattr(ImageFile.ImageFile, 'load', broken_load)
        with pytest.raises(gsv.PhotosphereBodyError, match='does not decode'):
            gsv.download_single_pano(str(tmp_path), info())
        assert list(tmp_path.rglob('*.jpg*')) == []

    def test_the_tile_paths_after_save_steps_run_on_the_saved_image(self, tmp_path, monkeypatch, cbk_untouched):
        """#179's edge-band flag and #115's display copy, after the save; #121's tripwire on the served width."""
        stub_photo_host(monkeypatch, serve())
        seen = []
        monkeypatch.setattr(gsv, '_flag_deep_edge_band',
                            lambda image, pano_id: seen.append(('band', image.size, stored(tmp_path).is_file())))
        monkeypatch.setattr(gsv, '_write_display_copy',
                            lambda image, out, pano_id: seen.append(('copy', image.size, out)))
        monkeypatch.setattr(common, 'warn_if_wider_than_viewer_ceiling',
                            lambda pano_id, width, source: seen.append(('width', width, source)))

        gsv.download_single_pano(str(tmp_path), info())

        assert seen == [('width', 64, 'gsv'), ('band', FRAME, True), ('copy', FRAME, str(stored(tmp_path)))]

    def test_an_undecodable_caos_id_takes_the_cbk_path_unchanged(self, tmp_path, monkeypatch, caplog):
        """Today's behaviour for an id that is not the protobuf: photometa, then tiles; the photo host never."""
        pano_id = caos(b'AF1QipABCDEFG', length=14)
        host = stub_photo_host(monkeypatch, serve())
        asked = stub_photometa(monkeypatch, [(512, 256), (1024, 512)])
        deny_probe(monkeypatch)
        tiles = stub_tiles(monkeypatch, lambda tile: (tile[0], tile[1], photosphere_jpeg((512, 512))))

        with caplog.at_level(logging.INFO):
            result = gsv.download_single_pano(str(tmp_path), info(pano_id, (1024, 512)))

        assert result == DownloadResult.success
        assert host.requests == []
        assert asked == [pano_id] and len(tiles) == 2
        assert any('does not decode to a photo id' in r.getMessage() for r in caplog.records)


# --- through DownloadRunner's image loop -----------------------------------------------------------------

def ledger_rows(storage_path):
    path = os.path.join(str(storage_path), 'pano_id_log.csv')
    with open(path, newline='') as f:
        return [row for row in csv.reader(f) if row and row[0] != 'pano_id']


class TestThroughTheImageLoop:
    def test_success_400_and_disagreement_land_where_the_ledger_contract_says(self, tmp_path, monkeypatch,
                                                                             cbk_untouched):
        """A success is a `1` row with a fetch stamp; the CIHM 400 is a `0` row with one (the verdict, never
        retried); a frame disagreement leaves no row and is counted as a frame refusal under its condition (#207)."""
        import DownloadRunner

        wrong = caos(b'AF1QipWRONGFRAMEWRONGFRAME')
        wrong_photo = gsv.photosphere_photo_id(wrong)

        def answer(url):
            if CIHM_PHOTO in url:
                return FakeResponse(400, b'<html>Error 400</html>', 'text/html')
            if wrong_photo in url:
                return serve(photosphere_jpeg((96, 48)))
            return serve()

        host = stub_photo_host(monkeypatch, answer)
        stop_reasons, image_stats = {}, {}
        pano_infos = [dict(info(pano_id), source='gsv') for pano_id in (AF1Q_PANO, CIHM_PANO, wrong)]

        DownloadRunner.download_panorama_images(str(tmp_path), pano_infos, stop_reasons=stop_reasons,
                                                image_stats=image_stats)

        assert len(host.requests) == 3, 'one photo-host request each'
        rows = ledger_rows(tmp_path)       # the loop shuffles its candidates, so order is not asserted
        assert sorted(row[:2] for row in rows) == sorted([[AF1Q_PANO, '1'], [CIHM_PANO, '0']])
        assert all(row[2] for row in rows), 'a fetched pano and a written-off one both carry their stamp (#129)'
        assert stored(tmp_path).is_file() and not stored(tmp_path, CIHM_PANO).exists()
        assert image_stats['raised'] == 0, 'the verdict is an answer, not a raise (field 20)'
        assert stop_reasons['frame_refusals'] == 1
        codes = [c['code'] for c in stop_reasons['conditions']]
        assert codes == [DownloadRunner.CONDITION_FRAME_DISAGREEMENT]
        assert wrong in stop_reasons['conditions'][0]['detail']

    def test_a_run_of_refused_form_verdicts_does_not_trip_the_gsv_permanent_failure_breaker(self, tmp_path,
                                                                                            monkeypatch, capsys,
                                                                                            cbk_untouched):
        """chicago-il holds 276 unledgered CIHM/CIAB ids and the loop shuffles its candidates, so a quiet night
        is 276 permanent verdicts with no success between them - well past GSV_MAX_CONSECUTIVE_PERMANENT (#113).
        That breaker guards against cbk answering black 200s across the board; a photosphere's verdict comes from
        the photo host refusing its form and says nothing about cbk, so it neither counts nor resets."""
        import DownloadRunner

        n = DownloadRunner.GSV_MAX_CONSECUTIVE_PERMANENT + 10
        panos = [caos(b'CIHM0ogKE' + ('%04d' % i).encode()) for i in range(n)]
        assert all(gsv.photosphere_form(gsv.photosphere_photo_id(p)) == 'CIHM' for p in panos)
        host = stub_photo_host(monkeypatch, FakeResponse(400, b'<html>Error 400</html>', 'text/html'))
        stop_reasons, tripped = {}, set()
        pano_infos = [dict(info(p), source='gsv') for p in panos]

        DownloadRunner.download_panorama_images(str(tmp_path), pano_infos, tripped_sources=tripped,
                                                stop_reasons=stop_reasons)

        assert len(host.requests) == n, 'every id asked once'
        assert sorted(row[0] for row in ledger_rows(tmp_path)) == sorted(panos), 'every id ledgered'
        assert all(row[1] == '0' for row in ledger_rows(tmp_path))
        assert tripped == set()
        assert 'consecutive permanent failures' not in capsys.readouterr().out
