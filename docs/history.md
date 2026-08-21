# What we removed, and why

Deleted code is recoverable from git history — this page is here so nobody has to go looking for it to find
out whether something was dropped on purpose.

## The Docker image (Aug 2026)

`Dockerfile` and `DownloadRunnerDockerEntrypoint.sh` are gone; the supported way to run the downloader is a
virtualenv, per [Downloader → Install](downloader.md#install).

The image existed to pin Ubuntu 22.04 / Python 3.10 and to sshfs-mount the pano store from inside the
container — the reason the documented `docker run` needed `--cap-add SYS_ADMIN --device=/dev/fuse
--security-opt apparmor:unconfined`. Almost all of the entrypoint's complexity was spent undoing problems the
container created rather than problems the scraper has: forwarding `SIGTERM` past PID 1 so the `log.csv`
evidence row still got written, keeping the runner's exit status from being clobbered by the unmount, and
working around `/app` being a CWD that died with the container ([#49](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/49)).
Run from cron against a venv, none of that machinery is needed: the exit code is the runner's, `SIGTERM`
reaches it directly, and the store is an ordinary host mount.

The pinning argument didn't survive either — CI installs `requirements.txt` on plain Ubuntu 22.04 / Python
3.10 on every push, which is the same proof the image was providing, without the beast.

Nothing about the store's on-disk layout changed, and no flag changed. `tests/test_entrypoint.py`, which
pinned the entrypoint's flag forwarding, went with the script it tested. See
[Downloader → Migrating off the old Docker image](downloader.md#migrating-off-the-old-docker-image).

## The legacy depth pipeline (Aug 2026)

In [#39](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/39) we removed the XML metadata
downloader — the `cbk?output=xml` endpoint it relied on died in 2022 — and the `decode_depthmap` binary. Depth
maps now come from the `streetlevel` library instead; see [Depth maps](depth.md).

The XML phase's *columns* survive in `log.csv` as a fixed-value stub, so that positions 7–18 never shift under
the log analyzer. See [Ops → The `log.csv` columns](ops.md#the-logcsv-columns).

The *reader* outlived the writer by a release. `download_single_pano` still parsed any `<pano_id>.xml` left on
the store, and [#52](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/issues/52) removed it because
it was not merely dead: those files are frozen 2022 metadata, and the block sat after the "image already
exists" return, so it could only run for a pano with an `.xml` and no `.jpg` — 1 of the 1,025 `.xml` files
sampled across dc, columbus-oh, amsterdam and newberg-or. On that one it trusted the declared
`num_zoom_levels` over the live probe, and a black tile at that zoom returned a **permanent** failure verdict.
Stale 2022 metadata could therefore blacklist a pano Google still serves. The zoom probe is now the only
thing that picks a zoom.

## Tohme and depth-based cropping (Apr 2023)

In PR [#26](https://github.com/ProjectSidewalk/sidewalk-panorama-tools/pull/26) we removed some old code: some
related to our Tohme paper from 2014, some to do with using depth maps for cropping images. Nobody appeared to
be using the Tohme code — those on our team didn't even know how it worked — and Google had removed access to
their depth data API. Removing it simplified the repository, making it easier to make use of our newer work
and easier to maintain the code that is actually being used.

---

If any of this ever needs to be revived, it is in the git history, reachable from the PRs and issues linked
above.
