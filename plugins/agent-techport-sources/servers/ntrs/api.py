"""Calls to the NTRS API (ntrs.nasa.gov), and nothing else.

Every endpoint here was probed on 2026-09-30; see "NTRS connector" in
docs/design.md. No key is needed.
"""

import urllib.parse

from ..common.http import HttpClient

BASE = "https://ntrs.nasa.gov"
MAX_TEXT_BYTES = 8 * 1024 * 1024        # extracted text: the largest seen was 2.2 MB


def client(**kwargs):
    return HttpClient("NTRS", {"ntrs.nasa.gov"}, timeout=60, **kwargs)


class NTRS:
    def __init__(self, http=None):
        self.http = http or client()

    def search(self, body):
        """Records, with counts by group. Paging stops at from + size = 10,000."""
        return self.http.post_json(f"{BASE}/api/citations/search", body)

    def record(self, record_id):
        """One record by id. Ids are digits; journal articles (CHORUS) have 14."""
        return self.http.get_json(f"{BASE}/api/citations/{urllib.parse.quote(str(record_id), safe='')}")

    def file(self, url, max_bytes):
        """(bytes, content type) of a file link the API gave."""
        return self.http.request(url, max_bytes=max_bytes)

    def text(self, url):
        """The text NTRS extracted from a file: plain text or XHTML."""
        data, _ = self.http.request(url, max_bytes=MAX_TEXT_BYTES)
        return data.decode("utf-8", "replace")
