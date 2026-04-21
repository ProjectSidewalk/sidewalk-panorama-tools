from . import gsv, mapillary
from .common import DownloadResult


def download_pano(storage_path, pano_info):
    """Dispatch a single pano download to the source-appropriate module.

    Returns a DownloadResult. Raises ValueError for an unrecognized source so the caller's per-pano exception handler
    logs it as a failure (preventing infinite retry loops on malformed data).
    """
    source = pano_info.get('source', 'gsv')
    if source == 'gsv':
        return gsv.download_single_pano(storage_path, pano_info)
    if source == 'mapillary':
        return mapillary.download_single_pano(storage_path, pano_info)
    raise ValueError(f"Unknown pano source: {source!r}")


__all__ = ['DownloadResult', 'download_pano', 'gsv', 'mapillary']
