# Threads to use for asyncio - test but usually more threads the better as I/O task
thread_count = 8

# Proxy settings - if proxy not added, leave as is
proxies = {
    "http": "http://",
    "https": "http://",
}

# --- Depth request pacing (#43) -----------------------------------------------------------------------------
#
# The depth phase paces itself ADAPTIVELY between the floor and the ceiling below: it opens a run at
# `depth_start_interval`, doubles on any sign of push-back, and earns its way back down towards the floor only
# after a long clean streak. The gap actually slept is drawn uniformly from [interval, 2 x interval], so there
# is no fixed cadence for a rate limiter to key on.
#
# Read the floor as the one setting that decides how aggressive this host can ever get: nothing draws a gap
# shorter than it. 0.25 s is the pace the 2026-08-09 photometa census ran 1,360 requests at with no push-back
# at all, so it is the fastest rate this repo has live evidence for - which is exactly why it is the default
# rather than 0. The backfill is ~1.4 M requests and a measured 0.077 s median round trip, so at this floor it
# is on the order of a fortnight of nights, not the "multi-month" job this comment used to claim.
#
# NB the throttle is PER PROCESS. That is safe only because scrape_queue.py runs one city at a time; going back
# to concurrent per-city cron lines would multiply the rate Google sees by however many overlap.
depth_min_request_interval = 0.25

# Where a run opens, before it has sampled the endpoint's mood. Careful first, fast later.
depth_start_interval = 1.0

# The slowest the adaptive backoff will go. Past this the circuit breaker and the block latch are the right
# tools, not an ever-longer sleep inside a bounded --max-runtime window.
depth_max_request_interval = 30.0

# -------------------------------
# Windows Headers
# -------------------------------

# Edge
headers_list = [
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36 Edg/88.0.705.81',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Firefox 85 on Windows 10
    {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:85.0) Gecko/20100101 Firefox/85.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    },

    # Chrome 88 Windows 10
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.190 Safari/537.36',
        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Opera for Windows 10
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # -------------------------------
    # Mac Headers
    # -------------------------------

    # Edge 88 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36 Edg/88.0.705.81',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9,en-US;q=0.8',
        'Referer': 'http://maps.google.com',
    },

    # Opera 74 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Chrome 88 Mac
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.192 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Referer': 'http://maps.google.com',
        'Accept-Language': 'en-GB,en;q=0.9',
    },

    # Firefox 85 Mac
    {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.16; rv:86.0) Gecko/20100101 Firefox/86.0',
        'Accept': 'image/webp,*/*',
        'Accept-Language': 'en-US,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
    },

    # Safari 14 Mac
    {
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Upgrade-Insecure-Requests': '1',
        'Host': 'maps.google.com',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15',
        'Accept-Language': 'en-ie',
        'Connection': 'keep-alive',
    },

    # -------------------------------
    # Ubuntu Headers
    # -------------------------------

    # Firefox 86 Ubuntu
    {
        'User-Agent': 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:86.0) Gecko/20100101 Firefox/86.0',
        'Accept': 'image/webp,*/*',
        'Accept-Language': 'en-GB,en;q=0.5',
        'Referer': 'http://maps.google.com',
        'DNT': '1',
        'Connection': 'keep-alive',
    },

    # Chrome 88 Ubuntu
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    },

    # Opera 74 Ubuntu
    {
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36 OPR/74.0.3911.160',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'Accept-Language': 'en-GB,en;q=0.9',
        'Referer': 'http://maps.google.com',
    }
]
