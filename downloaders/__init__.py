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
    """
    source = pano_info.get('source', 'gsv')
    if source == 'gsv':
        return gsv.download_single_pano(storage_path, pano_info)
    if source == 'mapillary':
        return mapillary.download_single_pano(storage_path, pano_info)
    raise ValueError(f"Unknown pano source: {source!r}")


__all__ = ['DownloadResult', 'download_pano', 'gsv', 'mapillary']
