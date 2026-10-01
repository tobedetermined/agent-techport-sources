"""Calls to the NASA Technology Transfer portal (technology.nasa.gov), and
nothing else.

Every endpoint here was probed on 2026-09-30; see "NASA Technology Transfer
connector" in docs/design.md. No key is needed.
"""

import urllib.parse

from ..common.http import HttpClient

BASE = "https://technology.nasa.gov"
MAX_PAGE_BYTES = 2 * 1024 * 1024        # a patent page is about 40 KB


def client(**kwargs):
    return HttpClient("NASA Technology Transfer", {"technology.nasa.gov"}, timeout=60, **kwargs)


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


class TechTransfer:
    def __init__(self, http=None):
        self.http = http or client()

    def search(self, kind, word):
        """Every match for one word, best first, as arrays of 13 values (spinoffs:
        at most 1,000). No query parameters: the API would search them as words."""
        return self.http.get_json(f"{BASE}/api/api/{kind}/{_quote(word)}").get("results") or []

    def page(self, reference):
        """A patent's web page (HTML), for what search doesn't return."""
        data, _ = self.http.request(f"{BASE}/patent/{_quote(reference)}", max_bytes=MAX_PAGE_BYTES)
        return data.decode("utf-8", "replace")
