import gzip
import importlib.util
import io
import json
import os
import ssl
import unittest
import urllib.error
import urllib.request

from servers.common import http

HAS_TRUSTSTORE = importlib.util.find_spec("truststore") is not None


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type="application/json", length=True):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}
        if length:
            self.headers["Content-Length"] = str(len(body))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener(*responses):
    """Returns each response (or raises each exception) in turn, recording URLs."""
    calls = []

    def open_(req, timeout=None):
        calls.append(req.full_url)
        r = responses[len(calls) - 1]
        if isinstance(r, Exception):
            raise r
        return r
    return open_, calls


class HttpClientTest(unittest.TestCase):
    def client(self, *responses):
        open_, calls = opener(*responses)
        return http.HttpClient("Test", {"api.example.gov"}, backoff=0, opener=open_), calls

    def test_refuses_hosts_not_on_the_list(self):
        c, calls = self.client()
        for url in ("https://evil.example.com/x", "https://api.example.gov.evil.com/x", "http://localhost/x"):
            with self.subTest(url=url), self.assertRaises(http.HostNotAllowed):
                c.get_json(url)
        self.assertEqual(calls, [])                        # nothing was sent

    def test_redirects_are_checked_too(self):
        c = http.HttpClient("Test", {"api.example.gov"})
        handler = next(h for h in c._open.__self__.handlers if isinstance(h, http._CheckedRedirects))
        req = urllib.request.Request("https://api.example.gov/a")
        with self.assertRaises(http.HostNotAllowed):
            handler.redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example.com/b")
        ok = handler.redirect_request(req, None, 302, "Found", {}, "https://api.example.gov/b")
        self.assertEqual(ok.full_url, "https://api.example.gov/b")

    def test_head_returns_lower_case_headers(self):
        resp = FakeResponse(b"")
        resp.headers = {"Last-Modified": "Tue, 01 Sep 2026 05:42:41 GMT", "ETag": '"x"'}
        c, _ = self.client(resp)
        self.assertEqual(c.head("https://api.example.gov/f")["last-modified"], "Tue, 01 Sep 2026 05:42:41 GMT")

    def test_user_agent_carries_the_manifests_version(self):
        from tests import PLUGIN_ROOT
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            version = json.load(f)["version"]
        self.assertEqual(http.USER_AGENT, f"agent-techport-sources/{version} (Claude Code plugin)")

    def test_params_and_user_agent(self):
        c, calls = self.client(FakeResponse(b'{"ok": true}'))
        self.assertEqual(c.get_json("https://api.example.gov/a", params={"q": "x y", "skip": None}), {"ok": True})
        self.assertEqual(calls, ["https://api.example.gov/a?q=x+y"])
        self.assertIn("agent-techport-sources", c.headers["User-Agent"])

    def test_retries_server_errors_then_succeeds(self):
        err = urllib.error.HTTPError("u", 503, "busy", {}, None)
        c, calls = self.client(err, urllib.error.URLError("reset"), FakeResponse(b"[1]"))
        self.assertEqual(c.get_json("https://api.example.gov/a"), [1])
        self.assertEqual(len(calls), 3)

    def test_retry_after_is_waited_for_when_short(self):
        slept = []
        real, http.time.sleep = http.time.sleep, slept.append
        try:
            busy = urllib.error.HTTPError("u", 429, "busy", {"Retry-After": "5"}, None)
            c, calls = self.client(busy, FakeResponse(b"[1]"))
            self.assertEqual(c.get_json("https://api.example.gov/a"), [1])
            self.assertEqual((slept, len(calls)), ([5], 2))                  # 5 s as asked, no extra backoff
            long = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": "60"}, None)
            c, calls = self.client(long)
            with self.assertRaisesRegex(http.SourceError, r"busy \(HTTP 503\).*in 60 seconds"):
                c.get_json("https://api.example.gov/a")
            self.assertEqual(len(calls), 1)                                  # not retried at all
        finally:
            http.time.sleep = real

    def test_gives_up_with_a_clear_message(self):
        c, _ = self.client(*[urllib.error.URLError("down")] * 3)
        with self.assertRaisesRegex(http.SourceError, "Test is not responding"):
            c.get_json("https://api.example.gov/a")

    def test_404_is_not_retried(self):
        c, calls = self.client(urllib.error.HTTPError("u", 404, "nf", {}, None))
        with self.assertRaisesRegex(http.SourceError, "not found"):
            c.get_json("https://api.example.gov/a")
        self.assertEqual(len(calls), 1)

    def test_a_sources_own_meaning_for_a_status(self):
        # TechPort, from inside NASA's network: every request is answered 401.
        open_, calls = opener(*[urllib.error.HTTPError("u", 401, "unauthorized", {}, None)] * 3)
        c = http.HttpClient("Test", {"api.example.gov"}, backoff=0, opener=open_,
                            status_messages={401: "it asked for a login."})
        with self.assertRaisesRegex(http.SourceError, r"^Test returned HTTP 401: it asked for a login\.$"):
            c.get_json("https://api.example.gov/a")
        with self.assertRaisesRegex(http.SourceError, "HTTP 401: it asked for a login"):
            c.download("https://api.example.gov/big", "/dev/null")
        with self.assertRaisesRegex(http.SourceError, "HTTP 401: it asked for a login"):
            c.head("https://api.example.gov/f")
        self.assertEqual(len(calls), 3)                    # not retried
        c, _ = self.client(urllib.error.HTTPError("u", 401, "unauthorized", {}, None))
        with self.assertRaisesRegex(http.SourceError, r"^Test returned HTTP 401\.$"):     # no meaning given
            c.get_json("https://api.example.gov/a")

    def test_non_json_is_reported_not_crashed_on(self):
        c, _ = self.client(FakeResponse(b"<html>maintenance</html>", "text/html"))
        with self.assertRaisesRegex(http.SourceError, "other than JSON"):
            c.get_json("https://api.example.gov/a")

    def test_size_limit(self):
        c, _ = self.client(FakeResponse(b"x" * 100))
        with self.assertRaises(http.TooLarge):
            c.request("https://api.example.gov/f", max_bytes=10)
        c, _ = self.client(FakeResponse(b"x" * 100, length=False))       # no Content-Length header
        with self.assertRaises(http.TooLarge):
            c.request("https://api.example.gov/f", max_bytes=10)
        c, _ = self.client(FakeResponse(b"x" * 10))
        self.assertEqual(c.request("https://api.example.gov/f", max_bytes=10)[0], b"x" * 10)


class TrustTest(unittest.TestCase):
    """Certificates are checked against the operating system's trust store."""

    def test_clients_check_https_against_the_os_trust_store(self):
        c = http.HttpClient("Test", {"api.example.gov"})
        self.assertTrue(any(isinstance(h, http.OsTrustHTTPSHandler) for h in c._open.__self__.handlers))

    def test_a_certificate_failure_is_reported_as_one_and_not_retried(self):
        failed = urllib.error.URLError(ssl.SSLCertVerificationError(
            1, "certificate verify failed: self-signed certificate in certificate chain"))
        open_, calls = opener(failed, failed, failed)
        c = http.HttpClient("Test", {"api.example.gov"}, backoff=0, opener=open_)
        with self.assertRaisesRegex(http.SourceError, "certificate") as cm:
            c.get_json("https://api.example.gov/x")
        self.assertNotIn("not responding", str(cm.exception))    # not mistaken for an outage
        self.assertEqual(len(calls), 1)              # it won't verify on a second try either

    @unittest.skipUnless(HAS_TRUSTSTORE, "needs truststore (the plugin's environment)")
    def test_certificate_checks_are_never_switched_off(self):
        ctx = http.tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


class GzipTest(unittest.TestCase):
    """Optional compression, for SEC's large files (a 10-K: 2.4 MB, 209 KB gzipped)."""

    def client(self, response, **kwargs):
        sent = []

        def open_(req, timeout=None):
            sent.append(req)
            return response
        return http.HttpClient("Test", {"api.example.gov"}, backoff=0, opener=open_, **kwargs), sent

    def gzipped(self, body, length=True):
        r = FakeResponse(gzip.compress(body), length=length)
        r.headers["Content-Encoding"] = "gzip"
        return r

    def test_asked_for_and_decoded_when_switched_on(self):
        c, sent = self.client(self.gzipped(b'{"ok": true}'), gzip=True)
        self.assertEqual(c.get_json("https://api.example.gov/x"), {"ok": True})
        self.assertEqual(sent[0].get_header("Accept-encoding"), "gzip")

    def test_off_by_default(self):
        c, sent = self.client(FakeResponse(b'{"ok": true}'))
        c.get_json("https://api.example.gov/x")
        self.assertIsNone(sent[0].get_header("Accept-encoding"))

    def test_the_size_limit_applies_to_the_decoded_body(self):
        c, _ = self.client(self.gzipped(b"x" * 5000), gzip=True)
        with self.assertRaises(http.TooLarge):
            c.request("https://api.example.gov/x", max_bytes=1000)

    def test_a_compressed_body_over_the_limit_is_refused_not_cut(self):
        body = os.urandom(900)                       # doesn't compress: about 920 bytes gzipped
        c, _ = self.client(self.gzipped(body, length=False), gzip=True)    # no length given, as when chunked
        with self.assertRaises(http.TooLarge):
            c.request("https://api.example.gov/x", max_bytes=910)
        c, _ = self.client(self.gzipped(body), gzip=True)
        self.assertEqual(c.request("https://api.example.gov/x", max_bytes=1000)[0], body)


@unittest.skipUnless(os.environ.get("LIVE_TESTS"), "set LIVE_TESTS=1 to test against the real hosts")
class LiveTrustTest(unittest.TestCase):
    def test_usaspending_connects(self):
        # Its certificate chains to a root missing from macOS's /etc/ssl/cert.pem,
        # which the Python that uv installs on macOS reads (found 2026-09-30).
        c = http.HttpClient("USAspending", {"api.usaspending.gov"})
        self.assertIn("last_updated", c.get_json("https://api.usaspending.gov/api/v2/awards/last_updated/"))


if __name__ == "__main__":
    unittest.main()
