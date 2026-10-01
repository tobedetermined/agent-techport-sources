"""Calls to SEC EDGAR (www.sec.gov, data.sec.gov, efts.sec.gov), and nothing
else.

SEC asks every client to send a contact in its User-Agent and to make no more
than 10 requests a second; this client sends the user's contact and keeps well
under that rate. Every endpoint here was probed on 2026-09-30; see "SEC EDGAR
connector" in docs/design.md.
"""

import threading
import time
import urllib.parse

from ..common.http import HttpClient
from . import model

HOSTS = {"www.sec.gov", "data.sec.gov", "efts.sec.gov"}
MIN_INTERVAL = 0.15                     # seconds between requests: under 7 a second
MAX_DOCUMENT_BYTES = 30 * 1024 * 1024   # a 10-K was 2.4 MB of HTML
TICKERS_TTL = 86400


def client(contact, **kwargs):
    return HttpClient("SEC EDGAR", HOSTS, timeout=60, gzip=True,
                      extra_headers={"User-Agent": model.user_agent(contact)}, **kwargs)


class Edgar:
    def __init__(self, contact, http=None):
        self.http = http or client(contact)
        self._lock = threading.Lock()
        self._last = 0.0
        self._tickers = (0.0, None)

    def _pace(self):
        with self._lock:
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _json(self, url):
        self._pace()
        return self.http.get_json(url)

    def tickers(self):
        """SEC's list of listed companies: [{cik_str, ticker, title}], kept a day."""
        at, rows = self._tickers
        if rows is None or time.time() - at > TICKERS_TTL:
            rows = list(self._json("https://www.sec.gov/files/company_tickers.json").values())
            self._tickers = (time.time(), rows)
        return rows

    def suggest(self, name):
        """EDGAR's own company index, which includes companies without a ticker."""
        return self._json("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode({"keysTyped": name}))

    def submissions(self, cik):
        return self._json(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")

    def facts(self, cik):
        return self._json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

    def search(self, params):
        """Full-text search, 2001 on: 100 hits a page. It sometimes answers 500
        and then works when asked again; the HTTP client retries."""
        return self._json("https://efts.sec.gov/LATEST/search-index?" + urllib.parse.urlencode(params))

    def document(self, cik, accession, document):
        self._pace()
        data, _ = self.http.request(model.archive_url(cik, accession, document), max_bytes=MAX_DOCUMENT_BYTES)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("cp1252", "replace")
