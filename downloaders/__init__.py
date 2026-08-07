from . import gsv, mapillary
from .common import DownloadResult


def download_pano(storage_path, pano_info):
    """Dispatch a single pano download to the source-appropriate module.

    The contract with the image ledger (#41): return DownloadResult.failure ONLY for a permanent property of
    the pano itself (the source has no imagery for it, its dimensions are unknowable) - it is ledgered
    downloaded=0 and never re-attempted. Transient conditions - network, storage, rate limits, bugs - must
    RAISE instead; a raised pano is not ledgered and retries next run. (The ValueError below for an
    unrecognized source is nearly unreachable: filter_supported_sources drops those panos before the loop;
    if ever reached it costs one log line per run.)

    Both downloaders are held to this by tests/test_image_downloaders.py. The corollary they also honour:
    an image on disk IS the resume marker, so every write goes through common.atomic_output_path - a
    download killed mid-write must leave nothing rather than a truncated file the next run reports as done.
    """
    source = pano_info.get('source', 'gsv')
    if source == 'gsv':
        return gsv.download_single_pano(storage_path, pano_info)
    if source == 'mapillary':
        return mapillary.download_single_pano(storage_path, pano_info)
    raise ValueError(f"Unknown pano source: {source!r}")


__all__ = ['DownloadResult', 'download_pano', 'gsv', 'mapillary']
