"""NTRS server tests that need the MCP SDK. The API is replaced by a fake serving
made-up records (tests/fixtures/ntrs.json), so there is no network. Run with
the plugin's Python:

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
    from servers.common import attach
    from servers.common.http import SourceError, TooLarge
    from servers.ntrs import model, server
else:
    server = None

with open(os.path.join(FIXTURES, "ntrs.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
MB = 1024 * 1024
TM_PDF = "https://ntrs.nasa.gov/api/citations/20260000001/downloads/20260000001.pdf"
TM_ABSTRACT_PDF = "https://ntrs.nasa.gov/api/citations/20260000001/downloads/Example%20abstract.docx.pdf"


class FakeNTRS:
    """Serves the fixture: one search response (cut to the page asked for), its
    records by id, files of given sizes and the two extracted texts. Records
    every call."""

    def __init__(self, total=None, sizes=None, texts=None, records=None):
        self.total, self.sizes, self.texts = total, sizes or {}, texts or {}
        self.records = {str(r["id"]): r for r in FX["search"]["results"]}
        self.records.update(records or {})
        self.calls = []

    def search(self, body):
        self.calls.append(("search", body))
        d = copy.deepcopy(FX["search"])
        page = body["page"]
        d["results"] = d["results"][page["from"]:page["from"] + page["size"]]
        if self.total is not None:
            d["stats"]["total"] = self.total
        return d

    def record(self, record_id):
        self.calls.append(("record", record_id))
        if record_id not in self.records:
            raise SourceError("NTRS: not found (404). Check the id; the search tools list valid ones.")
        return copy.deepcopy(self.records[record_id])

    def file(self, url, max_bytes):
        self.calls.append(("file", url))
        size = self.sizes.get(url, 1000)
        if size > max_bytes:
            raise TooLarge("too big")
        return b"%PDF" + b"x" * (size - 4), "application/pdf"

    def text(self, url):
        self.calls.append(("text", url))
        if url in self.texts:
            return self.texts[url]
        return FX["fulltext_xhtml"] if "20260000001" in url else FX["fulltext_plain"]


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (server.ntrs, os.environ.get("CLAUDE_PLUGIN_DATA"))
        os.environ["CLAUDE_PLUGIN_DATA"] = self.tmp

    def tearDown(self):
        server.ntrs = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self.saved[1]
        shutil.rmtree(self.tmp)

    def use(self, fake):
        server.ntrs = fake
        return fake

    def call(self, tool, **kwargs):
        text = getattr(server, tool)(**kwargs)
        self.assertNotIn("\n", text)              # compact
        return json.loads(text)

    def get(self, record_id, **kwargs):
        blocks = server.ntrs_get_record(record_id=record_id, **kwargs)
        self.assertNotIn("\n", blocks[0].text)
        body = json.loads(blocks[0].text)
        files = body["files"]
        rows = [dict(zip(files["columns"], r)) for r in files["rows"]] if files else []
        return body, rows, blocks[1:]


@unittest.skipIf(server is None, "needs the MCP SDK")
class SearchTest(ServerCase):
    def test_rows_come_as_a_table_with_the_total(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_search", query="cryogenic propellant")
        self.assertEqual(body["total"], 1314)
        self.assertEqual(body["records"]["columns"], ["id", "title", "type", "published", "center", "authors", "files"])
        self.assertEqual(len(body["records"]["rows"]), 4)
        self.assertIn("ntrs.nasa.gov", body["data"]["source"])
        self.assertEqual(len(fake.calls), 1)
        self.assertNotIn("note", body)

    def test_a_year_filter_says_how_many_undated_records_it_left_out(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_search", query="cryogenic propellant", year_from=2020)
        self.assertEqual(fake.calls[0][1]["published"], {"gte": "2020-01-01"})
        counts_only = fake.calls[1][1]
        self.assertNotIn("published", counts_only)                 # the same search without the years
        self.assertEqual((counts_only["q"], counts_only["page"]["size"]), ("cryogenic propellant", 0))
        self.assertIn("1,172", body["note"])                      # 1,314 records, 142 of them dated

    def test_newest_first_says_undated_records_come_last(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_search", query="cryogenic propellant", sort="newest")
        self.assertIn("1,172", body["note"])
        self.assertIn("last", body["note"])
        self.assertEqual(len(fake.calls), 1)                       # from the same response

    def test_the_limit_is_capped_lower_with_abstracts(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_search", query="cryogenic", include_abstract=True, limit=40)
        self.assertEqual(fake.calls[0][1]["page"]["size"], 25)
        self.assertIn("capped at 25", body["note"])
        self.assertEqual(body["records"]["columns"][-1], "abstract")

    def test_past_10000_results(self):
        self.use(FakeNTRS(total=20_000))
        body = self.call("ntrs_search", query="propulsion")
        self.assertIn("10,000", body["note"])
        with self.assertRaisesRegex(ToolError, "10,000"):
            server.ntrs_search(query="propulsion", offset=9995, limit=10)

    def test_bad_values_are_tool_errors(self):
        fake = self.use(FakeNTRS())
        with self.assertRaisesRegex(ToolError, "GRC"):
            server.ntrs_search(center="Glenn")
        with self.assertRaisesRegex(ToolError, "relevance"):
            server.ntrs_search(sort="best")
        self.assertEqual(fake.calls, [])


@unittest.skipIf(server is None, "needs the MCP SDK")
class ExactCaseTest(ServerCase):
    def test_a_wrong_case_filter_that_finds_nothing_names_the_forms_in_use(self):
        class Fake(FakeNTRS):
            def search(self, body):
                d = super().search(body)
                if "subjectCategory" in body:
                    d["stats"]["total"], d["results"] = 0, []
                return d
        fake = self.use(Fake())
        body = self.call("ntrs_search", query="regolith", subject="propellants AND fuels")
        self.assertEqual(body["total"], 0)
        self.assertIn("is written 'Propellants And Fuels' (", body["note"])
        self.assertIn("'Propellants and Fuels' (", body["note"])
        hint = fake.calls[-1][1]
        self.assertEqual((hint.get("q"), "subjectCategory" in hint), ("regolith", False))   # other filters kept

    def test_no_extra_request_when_something_matched(self):
        fake = self.use(FakeNTRS())
        self.call("ntrs_search", query="regolith", subject="Propellants And Fuels")
        self.assertEqual(len([c for c in fake.calls if c[0] == "search"]), 1)


@unittest.skipIf(server is None, "needs the MCP SDK")
class GetRecordTest(ServerCase):
    def test_a_record_with_its_pdfs_attached(self):
        fake = self.use(FakeNTRS())
        body, files, blocks = self.get("20260000001")
        self.assertEqual(body["record"]["title"], "Example Cryogenic Tank Insulation Test Results")
        self.assertEqual(body["record"]["authors"]["columns"], ["name", "organization"])
        self.assertEqual([(f["file"], f["attached"]) for f in files], [(1, True), (2, True)])
        self.assertEqual([b.resource.uri for b in blocks], [TM_PDF, TM_ABSTRACT_PDF])
        self.assertEqual([c for c in fake.calls if c[0] == "file"], [("file", TM_PDF), ("file", TM_ABSTRACT_PDF)])
        self.assertTrue(all(f["text"] for f in files))
        self.assertIn("saves each one to a file", body["note"])       # what Claude Code does with them
        self.assertIn("ntrs_read_text with find", body["note"])       # to find a figure's page first

    def test_every_file_row_has_the_same_fields(self):
        self.use(FakeNTRS(sizes={TM_PDF: 25 * MB}))
        _, files, _ = self.get("20260000001")
        self.assertEqual(list(files[0]), ["file", "name", "type", "format", "url", "text", "bytes", "attached",
                                          "saved_to", "note"])

    def test_a_word_file_without_a_pdf_points_to_its_text(self):
        fake = self.use(FakeNTRS())
        _, files, blocks = self.get("20250000002")
        self.assertEqual(blocks, [])
        self.assertFalse(files[0]["attached"])
        self.assertIn("ntrs_read_text", files[0]["note"])
        self.assertFalse([c for c in fake.calls if c[0] == "file"])

    def test_documents_can_be_left_out(self):
        fake = self.use(FakeNTRS())
        body, files, blocks = self.get("20260000001", include_documents=False)
        self.assertEqual(blocks, [])
        self.assertIn("include_documents", files[0]["note"])
        self.assertNotIn("note", body)
        self.assertEqual([c[0] for c in fake.calls], ["record"])

    def test_too_large_to_attach_is_saved_and_over_the_cap_is_a_link(self):
        self.use(FakeNTRS(sizes={TM_PDF: 15 * MB, TM_ABSTRACT_PDF: 25 * MB}))
        _, files, blocks = self.get("20260000001")
        self.assertEqual(blocks, [])
        self.assertEqual(files[0]["saved_to"], os.path.join(self.tmp, "ntrs", "files", "20260000001-1.pdf"))
        self.assertTrue(os.path.exists(files[0]["saved_to"]))
        self.assertIn(attach.OVER_CAP, files[1]["note"])
        self.assertIn("ntrs_read_text", files[1]["note"])

    def test_a_journal_article_without_files(self):
        self.use(FakeNTRS())
        body, files, blocks = self.get("31234567890123")
        self.assertEqual((files, blocks), ([], []))
        self.assertIn("10.0000/example.0002", body["note"])

    def test_long_author_lists_say_where_the_rest_are(self):
        many = dict(FX["search"]["results"][0], authorAffiliations=[
            {"sequence": i, "meta": {"author": {"name": f"Author {i}"}, "organization": {"name": "Example Lab"}}}
            for i in range(60)])
        self.use(FakeNTRS(records={"20260000001": many}))
        body, _, _ = self.get("20260000001", include_documents=False)
        self.assertEqual(len(body["record"]["authors"]["rows"]), 50)
        self.assertIn("50 of 60", body["note"])
        self.assertIn("https://ntrs.nasa.gov/citations/20260000001", body["note"])

    def test_ids_are_checked(self):
        fake = self.use(FakeNTRS())
        with self.assertRaisesRegex(ToolError, "20180002393"):
            server.ntrs_get_record(record_id="NASA/TM-2017-218239")
        self.assertEqual(fake.calls, [])
        with self.assertRaisesRegex(ToolError, "not found"):
            server.ntrs_get_record(record_id="999")
        body, _, _ = self.get(20250000002, include_documents=False)      # a number works too
        self.assertEqual(body["record"]["id"], "20250000002")


@unittest.skipIf(server is None, "needs the MCP SDK")
class ReadTextTest(ServerCase):
    def test_a_part_with_its_pages(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_read_text", record_id="20260000001")
        self.assertEqual((body["file"], body["part"], body["parts"], body["pages"]), (1, 1, 1, "1-2"))
        self.assertTrue(body["text"].startswith("[page 1]"))
        self.assertIn(("text", "https://ntrs.nasa.gov/api/citations/20260000001/downloads/20260000001.txt"), fake.calls)
        self.assertNotIn("note", body)

    def test_long_text_comes_in_parts(self):
        url = "https://ntrs.nasa.gov/api/citations/20250000002/downloads/Example%20Paper.docx.txt"
        self.use(FakeNTRS(texts={url: ("word " * 19 + "end\n") * 300}))     # 30,000 characters
        body = self.call("ntrs_read_text", record_id="20250000002", part=2)
        self.assertEqual((body["part"], body["parts"]), (2, 3))
        self.assertLessEqual(len(body["text"]), model.PART_CHARS)
        self.assertIsNone(body["pages"])
        self.assertIn("part=3", body["note"])
        with self.assertRaisesRegex(ToolError, "1 to 3"):
            server.ntrs_read_text(record_id="20250000002", part=4)

    def test_passages_that_mention_words(self):
        self.use(FakeNTRS())
        body = self.call("ntrs_read_text", record_id="20260000001", find="boil-off")
        self.assertEqual(body["passages"]["columns"], ["part", "page", "matches", "text"])
        self.assertEqual(body["passages"]["rows"][0][:3], [1, 2, 1])
        self.assertEqual(body["matches"], 1)

    def test_no_matches(self):
        self.use(FakeNTRS())
        body = self.call("ntrs_read_text", record_id="20260000001", find="regolith")
        self.assertEqual((body["matches"], body["passages"]), (0, []))
        self.assertIn("No matches", body["note"])

    def test_which_file(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_read_text", record_id="20260000001", file=2)
        self.assertEqual(body["name"], "Example abstract.docx")
        with self.assertRaisesRegex(ToolError, "1 to 2"):
            server.ntrs_read_text(record_id="20260000001", file=3)
        with self.assertRaisesRegex(ToolError, "no files"):
            server.ntrs_read_text(record_id="31234567890123")
        self.assertEqual(len([c for c in fake.calls if c[0] == "text"]), 1)


@unittest.skipIf(server is None, "needs the MCP SDK")
class AggregateTest(ServerCase):
    def test_years_in_order_with_the_undated(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_aggregate", group_by="year", query="cryogenic propellant")
        self.assertEqual(body["groups"], {"columns": ["year", "records"], "rows": [[1966, 40], [2015, 72], [2024, 30]]})
        self.assertEqual((body["total"], body["undated"]), (1314, 1172))
        self.assertEqual(fake.calls[0][1]["page"]["size"], 0)

    def test_top_authors_with_the_rest(self):
        self.use(FakeNTRS())
        body = self.call("ntrs_aggregate", group_by="author", query="cryogenic propellant")
        self.assertEqual(body["groups"]["rows"][0], ["Ada Example", 31])
        self.assertEqual(body["rest"], 2000)
        self.assertIn("once for each", body["note"])

    def test_centers_explain_cdms(self):
        self.use(FakeNTRS())
        body = self.call("ntrs_aggregate", group_by="center")
        self.assertEqual(body["groups"]["columns"], ["center", "name", "records"])
        self.assertIn("CDMS", body["note"])

    def test_a_year_filter_says_how_many_undated_records_it_left_out(self):
        fake = self.use(FakeNTRS())
        body = self.call("ntrs_aggregate", group_by="center", query="cryogenic", year_to=2010)
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("1,172", body["note"])

    def test_unknown_group(self):
        self.use(FakeNTRS())
        with self.assertRaisesRegex(ToolError, "report_type"):
            server.ntrs_aggregate(group_by="decade")


@unittest.skipIf(server is None or shutil.which("uv") is None, "needs version 2 of the MCP SDK, and uv")
class NtrsOverStdioTest(unittest.TestCase):
    """The exact plugin.json command. Only calls that fail before any network
    request, so the test needs no network."""

    def test_tools_and_errors(self):
        anyio.run(self._session)

    async def _session(self):
        root = os.path.abspath(PLUGIN_ROOT)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)["mcpServers"]["ntrs"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"]}
            async with stdio_client(StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    self.assertEqual(set(tools), {"ntrs_search", "ntrs_get_record", "ntrs_read_text", "ntrs_aggregate"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
                    r = await s.call_tool("ntrs_search", {"center": "Glenn"})
                    self.assertTrue(r.is_error)
                    self.assertIn("GRC", r.content[0].text)
                    # A number is accepted as an id (checked by the tool itself, before any request).
                    r = await s.call_tool("ntrs_get_record", {"record_id": -5})
                    self.assertTrue(r.is_error)
                    self.assertIn("the number NTRS gives each record", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
