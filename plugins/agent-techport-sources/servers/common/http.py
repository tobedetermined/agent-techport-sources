"""HTTP for the plugin's servers: the standard library, plus truststore for
checking certificates.

Each server makes an HttpClient with the hosts it may contact. A request to
any other host is refused before it is sent, so the "only the source hosts"
rule in the README is enforced in code, not just promised.
"""

import http.client
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = "agent-techport-sources/0.1.0 (Claude Code plugin)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A source API failed. The message says which source and what to try."""


class HostNotAllowed(SourceError):
    """The code tried to contact a host outside the server's list. A bug, never user error."""


class TooLarge(SourceError):
    """A response was bigger than the caller's limit."""


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only if it stays on an allowed host."""

    def __init__(self, client):
        super().__init__()
        self.client = client

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.client.check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def tls_context():
    """Certificates checked against the operating system's trust store: the Mac
    keychain, the Windows store, the Linux system bundle. On macOS the Python
    that uv installs otherwise reads /etc/ssl/cert.pem, which lacks some current
    roots; USAspending's chain failed against it (2026-09-30)."""
    import truststore           # on first use, so test runs on a plain Python don't need it
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class OsTrustHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS with tls_context(), made on the first request."""

    _os_context = None

    def https_open(self, req):
        if self._os_context is None:
            self._os_context = tls_context()
        return self.do_open(http.client.HTTPSConnection, req, context=self._os_context)


def _gunzip(payload, max_bytes=None):
    """Decode a gzip body, stopping just past max_bytes so a small download
    can't grow without limit."""
    out = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = out.decompress(payload, max_bytes + 1) if max_bytes else out.decompress(payload)
    return data if max_bytes and len(data) > max_bytes else data + out.flush()


class HttpClient:
    def __init__(self, source, allowed_hosts, *, timeout=60, retries=3, backoff=1.0,
                 extra_headers=None, opener=None, gzip=False):
        self.source = source                  # a name for error messages, e.g. "TechPort"
        self.gzip = gzip                      # ask for compressed responses, and decode them
        self.allowed_hosts = set(allowed_hosts)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.headers = {"User-Agent": USER_AGENT, **(extra_headers or {})}
        # urlopen alone would follow a redirect to any host; this opener checks each one.
        self._open = opener or urllib.request.build_opener(_CheckedRedirects(self), OsTrustHTTPSHandler()).open

    def check_host(self, url):
        host = urllib.parse.urlsplit(url).hostname
        if host not in self.allowed_hosts:
            raise HostNotAllowed(f"{self.source}: refusing to contact {host!r}; "
                                 f"allowed hosts are {sorted(self.allowed_hosts)}")

    def url(self, url, params=None):
        self.check_host(url)
        if params:
            params = {k: v for k, v in params.items() if v is not None}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def request(self, url, *, params=None, method="GET", body=None, max_bytes=None, timeout=None):
        """Return (bytes, content_type). Retries rate limits, server errors and timeouts."""
        full = self.url(url, params)
        headers = dict(self.headers)
        if self.gzip:
            headers["Accept-Encoding"] = "gzip"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            req = urllib.request.Request(full, data=data, method=method, headers=headers)
            try:
                with self._open(req, timeout=timeout or self.timeout) as r:
                    content_type = r.headers.get("Content-Type", "").split(";")[0].strip()
                    size = r.headers.get("Content-Length")
                    if max_bytes and size and size.isdigit() and int(size) > max_bytes:
                        raise TooLarge(f"{self.source}: {int(size):,} bytes, over the "
                                       f"{max_bytes:,}-byte limit")
                    payload = r.read(max_bytes + 1) if max_bytes else r.read()
                    self._check_size(payload, max_bytes)        # before decoding, so nothing is cut short
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        payload = _gunzip(payload, max_bytes)
                        self._check_size(payload, max_bytes)
                    return payload, content_type
            except HostNotAllowed:
                raise
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUS:
                    last = e
                    continue
                if e.code == 404:
                    raise SourceError(f"{self.source}: not found ({e.code}). Check the id; "
                                      "the search tools list valid ones.") from e
                raise SourceError(f"{self.source} returned HTTP {e.code}.") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                reason = getattr(e, "reason", None)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    raise SourceError(f"{self.source}: the site's certificate could not be verified "
                                      f"({getattr(reason, 'verify_message', None) or reason}), so the "
                                      "plugin won't connect to it.") from e
                last = e
                continue
        raise SourceError(f"{self.source} is not responding ({last}). It may be down or "
                          "unreachable from this network; try again later.") from last

    def _check_size(self, payload, max_bytes):
        if max_bytes and len(payload) > max_bytes:
            raise TooLarge(f"{self.source}: over the {max_bytes:,}-byte limit")

    def head(self, url, *, timeout=None):
        """Response headers of a HEAD request, with lower-case names."""
        full = self.url(url)
        last = None
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                req = urllib.request.Request(full, method="HEAD", headers=self.headers)
                with self._open(req, timeout=timeout or self.timeout) as r:
                    return {k.lower(): v for k, v in r.headers.items()}
            except HostNotAllowed:
                raise
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_STATUS:
                    raise SourceError(f"{self.source} returned HTTP {e.code}.") from e
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
        raise SourceError(f"{self.source} is not responding ({last}).") from last

    def get_json(self, url, *, params=None, timeout=None):
        payload, content_type = self.request(url, params=params, timeout=timeout)
        try:
            return json.loads(payload)
        except ValueError as e:
            raise SourceError(f"{self.source} sent something other than JSON "
                              f"({content_type or 'no content type'}); it may be under maintenance.") from e

    def post_json(self, url, body, *, timeout=None):
        payload, _ = self.request(url, method="POST", body=body, timeout=timeout)
        return json.loads(payload)

    def download(self, url, dest, *, params=None, timeout=600, chunk=1 << 20):
        """Stream a large response to a file. Returns the number of bytes written."""
        full = self.url(url, params)
        req = urllib.request.Request(full, headers=self.headers)
        written = 0
        try:
            with self._open(req, timeout=timeout) as r, open(dest, "wb") as f:
                while block := r.read(chunk):
                    f.write(block)
                    written += len(block)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise SourceError(f"{self.source}: download failed ({e}).") from e
        return written
