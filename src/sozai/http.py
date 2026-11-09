"""A small polite HTTP client: one User-Agent naming sozai, retries, and a pause between calls.

Every request goes through open_url, which only talks https to the hosts sozai has a reason to
reach, and checks every redirect the same way. URLs come from library APIs and the published
data, so a bad record must not be able to point sozai at another host or at plain http.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from sozai import __version__

USER_AGENT = f"sozai-search-mcp/{__version__} (+https://github.com/kanaworksai-hue/sozai-search-mcp)"
ALLOWED_HOSTS = (  # a host matches itself and its subdomains
    "polyhaven.com", "polyhaven.org",                 # API, files, thumbnails
    "ambientcg.com", "struffelproductions.com",       # API, downloads, thumbnails (acg-media.)
    "fab.com",                                        # Megascans listings and preview images
    "github.com", "githubusercontent.com",            # the published data release
    "huggingface.co", "hf.co",                        # the query model, the Megascans listing table
    "arweave.net",                                    # Open Source Avatars files (permanent storage)
    "science.nasa.gov",                               # NASA 3D Resources pages and previews (assets.)
)


class UnsafeURL(ValueError):
    pass


def check_url(url: str) -> str:
    """Return url when sozai may fetch it, else raise UnsafeURL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https":
        raise UnsafeURL(f"refusing a non-https URL: {url}")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS):
        raise UnsafeURL(f"refusing a URL outside the libraries sozai knows: {url}")
    return url


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def open_url(url: str, timeout: float, headers: dict | None = None):
    """urlopen for an allowed https URL, sending sozai's User-Agent."""
    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return _opener.open(request, timeout=timeout)


class Http:
    def __init__(self, pause: float = 0.25, timeout: float = 30.0, retries: int = 3):
        self.pause = pause
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0

    def get_json(self, url: str):
        return json.loads(self.get(url))

    def get(self, url: str, headers: dict | None = None) -> bytes:
        for attempt in range(self.retries):
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with open_url(url, self.timeout, {"Accept": "application/json", **(headers or {})}) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code < 500 and error.code != 429:
                    raise
                if attempt == self.retries - 1:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.retries - 1:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")
