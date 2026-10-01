"""NASA Technology Transfer server tests that need the MCP SDK. The API is
replaced by a fake serving made-up records (tests/fixtures/techtransfer.json)
and a made-up patent page, so there is no network. Run with the plugin's Python:

    plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .
"""

import copy
import json
import os
import shutil
import tempfile
import unittest

from tests import FIXTURES, PLUGIN_ROOT

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:          # no SDK (version 2): these tests are skipped
    anyio = None
if anyio is not None:        # with the SDK, a broken server must fail the tests, not skip them
    from servers.common.http import SourceError
    from servers.techtransfer import model, server
else:
    server = None

with open(os.path.join(FIXTURES, "techtransfer.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
with open(os.path.join(FIXTURES, "techtransfer_patent_page.html"), encoding="utf-8") as fh:
    PAGE = fh.read()


def spinoff(i, score=1.0):
    return [f"dddddddddddddddddddd{i:04d}", f"JSC-SO-{i}", f"Spinoff {i}", "A story.", f"JSC-SO-{i}",
            "Consumer/Home/Recreation", "", "", "", "JSC", "", "", score]


class FakeTechTransfer:
    """Serves the fixture by kind and word, plus any extra results, and the
    made-up page for KSC-TOPS-901. Records every call."""

    def __init__(self, extra=None, page=PAGE):
        self.extra, self.page_html, self.calls = extra or {}, page, []

    def search(self, kind, word):
        self.calls.append(("search", kind, word))
        if (kind, word) in self.extra:
            return copy.deepcopy(self.extra[(kind, word)])
        return copy.deepcopy(FX[kind].get(word, []))

    def page(self, reference):
        self.calls.append(("page", reference))
        if isinstance(self.page_html, Exception):
            raise self.page_html
        return self.page_html


@unittest.skipIf(server is None, "needs the MCP SDK")
class ServerCase(unittest.TestCase):
    def setUp(self):
        self.saved = server.tt

    def tearDown(self):
        server.tt = self.saved

    def use(self, fake):
        server.tt = fake
        return fake

    def call(self, tool, **kwargs):
        text = getattr(server, tool)(**kwargs)
        self.assertNotIn("\n", text)              # compact
        return json.loads(text)


class SearchTest(ServerCase):
    def test_all_words_must_match_in_every_kind(self):
        fake = self.use(FakeTechTransfer())
        body = self.call("techtransfer_search", query="cryogenic tank")
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["by_kind"], {"patent": 1, "software": 1, "spinoff": 1})
        refs = [r[1] for r in body["records"]["rows"]]
        self.assertEqual(sorted(refs), ["ARC-90001-1", "KSC-SO-901", "LEW-TOPS-902"])
        self.assertEqual(body["records"]["columns"], ["kind", "reference", "title", "category", "center", "release"])
        self.assertEqual(len(fake.calls), 6)                     # one search per word and kind
        self.assertIn("this search", body["data"]["caveat"])

    def test_common_words_are_left_out_and_named(self):
        fake = self.use(FakeTechTransfer())
        body = self.call("techtransfer_search", query="cryogenic and tank", kind="patent")
        self.assertEqual(body["total"], 1)
        self.assertEqual([c[2] for c in fake.calls], ["cryogenic", "tank"])
        self.assertIn("'and'", body["note"])

    def test_any_word(self):
        self.use(FakeTechTransfer())
        body = self.call("techtransfer_search", query="cryogenic tank", kind="patent", match="any")
        self.assertEqual(body["total"], 4)
        self.assertEqual(body["records"]["columns"], ["kind", "reference", "title", "category", "center"])

    def test_filters_come_before_the_counts(self):
        self.use(FakeTechTransfer())
        body = self.call("techtransfer_search", query="cryogenic", kind="patent", center="larc")
        self.assertEqual((body["total"], body["by_center"]), (1, {"LaRC": 1}))
        body = self.call("techtransfer_search", query="cryogenic", kind="patent", category="materials")
        self.assertEqual(body["by_category"], {"Materials and Coatings": 2})

    def test_a_word_at_the_spinoff_limit_is_named(self):
        self.use(FakeTechTransfer(extra={("spinoff", "nasa"): [spinoff(i) for i in range(1000)]}))
        body = self.call("techtransfer_search", query="nasa", kind="spinoff")
        self.assertEqual(body["total"], 1000)
        self.assertIn("nasa", body["note"])
        self.assertIn("1,000", body["note"])

    def test_pages_are_cut_locally(self):
        self.use(FakeTechTransfer(extra={("spinoff", "home"): [spinoff(i, 100 - i) for i in range(60)]}))
        body = self.call("techtransfer_search", query="home", kind="spinoff", limit=10, offset=20)
        self.assertEqual((body["total"], body["offset"], body["returned"]), (60, 20, 10))
        self.assertEqual(body["records"]["rows"][0][1], "JSC-SO-20")
        body = self.call("techtransfer_search", query="home", kind="spinoff", include_description=True, limit=40)
        self.assertEqual(body["returned"], 25)
        self.assertIn("capped at 25", body["note"])

    def test_bad_values_fail_before_any_request(self):
        fake = self.use(FakeTechTransfer())
        for kwargs, message in (({"query": "x", "kind": "licenses"}, "patent, software, spinoff"),
                                ({"query": " "}, "can't list"),
                                ({"query": "x", "center": "Glenn"}, "GRC"),
                                ({"query": "x", "match": "most"}, "all, any")):
            with self.assertRaisesRegex(ToolError, message):
                server.techtransfer_search(**kwargs)
        self.assertEqual(fake.calls, [])

    def test_source_errors_are_tool_errors(self):
        class Down(FakeTechTransfer):
            def search(self, kind, word):
                raise SourceError("NASA Technology Transfer is not responding.")
        self.use(Down())
        with self.assertRaisesRegex(ToolError, "not responding"):
            server.techtransfer_search(query="cryogenic")


class GetTest(ServerCase):
    def test_a_patent_with_its_page(self):
        fake = self.use(FakeTechTransfer())
        rec = self.call("techtransfer_get", reference="ksc-tops-901")["record"]
        self.assertEqual(rec["reference"], "KSC-TOPS-901")             # the exact match, not KSC-TOPS-9011
        self.assertEqual(rec["benefits"], ["Reflects nearly all sunlight", "Flexible and moisture resistant"])
        self.assertEqual(rec["patents"]["columns"], ["number", "link"])
        self.assertEqual([p[0] for p in rec["patents"]["rows"]], ["99,000,001", "99,000,002"])
        self.assertEqual(rec["case_numbers"], ["KSC-90001", "KSC-90002"])
        self.assertEqual(rec["page"], "https://technology.nasa.gov/patent/KSC-TOPS-901")
        self.assertNotIn("score", rec)
        self.assertEqual(fake.calls, [("search", "patent", "KSC-TOPS-901"), ("page", "KSC-TOPS-901")])

    def test_an_unreadable_page_falls_back_to_the_search_fields(self):
        for page in (SourceError("NASA Technology Transfer returned HTTP 500."), "<html><body>Changed</body></html>"):
            self.use(FakeTechTransfer(page=page))
            body = self.call("techtransfer_get", reference="KSC-TOPS-901")
            self.assertIn("Thermal coatings", body["record"]["description"])
            self.assertNotIn("benefits", body["record"])
            self.assertIn("https://technology.nasa.gov/patent/KSC-TOPS-901", body["note"])

    def test_software_and_spinoffs(self):
        fake = self.use(FakeTechTransfer())
        rec = self.call("techtransfer_get", reference="ARC-90001-1")["record"]
        self.assertEqual((rec["kind"], rec["release"], rec["link"]),
                         ("software", "Open Source", "https://github.com/example/example-tank"))
        rec = self.call("techtransfer_get", reference="KSC-SO-901")["record"]
        self.assertIn("SBIR awards", rec["description"])
        self.assertNotIn(("page", "KSC-SO-901"), fake.calls)

    def test_not_found_names_close_matches(self):
        fake = self.use(FakeTechTransfer(extra={("patent", "KSC-TOPS-90"): FX["patent"]["KSC-TOPS-901"]}))
        with self.assertRaisesRegex(ToolError, "KSC-TOPS-901"):
            server.techtransfer_get(reference="KSC-TOPS-90")
        self.assertEqual([c[1] for c in fake.calls], ["patent", "software", "spinoff"])

    def test_a_reference_is_checked(self):
        fake = self.use(FakeTechTransfer())
        with self.assertRaisesRegex(ToolError, "KSC-TOPS-59"):
            server.techtransfer_get(reference="../etc/passwd")
        self.assertEqual(fake.calls, [])


@unittest.skipIf(server is None or shutil.which("uv") is None, "needs version 2 of the MCP SDK, and uv")
class TechTransferOverStdioTest(unittest.TestCase):
    """The exact plugin.json command. Only calls that fail before any network
    request, so the test needs no network."""

    def test_tools_and_errors(self):
        anyio.run(self._session)

    async def _session(self):
        root = os.path.abspath(PLUGIN_ROOT)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)["mcpServers"]["techtransfer"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"]}
            async with stdio_client(StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    self.assertEqual(set(tools), {"techtransfer_search", "techtransfer_get"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
                    r = await s.call_tool("techtransfer_search", {"query": "x", "kind": "licenses"})
                    self.assertTrue(r.is_error)
                    self.assertIn("patent, software, spinoff", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
