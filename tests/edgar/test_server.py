"""SEC EDGAR server tests that need the MCP SDK. SEC is replaced by a fake
serving made-up records (tests/fixtures/edgar.json, edgar_filing.html), so
there is no network. Run with the plugin's Python:

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
    from servers.edgar import server
else:
    server = None

with open(os.path.join(FIXTURES, "edgar.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
with open(os.path.join(FIXTURES, "edgar_filing.html"), encoding="utf-8") as fh:
    FILING = fh.read()
CONTACT = "Test Person test@example.com"
CONTACT_VARS = ("SEC_CONTACT", "SEC_NAME", "SEC_EMAIL", "CLAUDE_PLUGIN_OPTION_SEC_NAME",
                "CLAUDE_PLUGIN_OPTION_SEC_EMAIL")


class FakeEdgar:
    """Serves the fixture; records every call."""

    def __init__(self, facts_missing=False, total=None):
        self.facts_missing, self.total, self.calls = facts_missing, total, []

    def tickers(self):
        self.calls.append(("tickers",))
        return list(FX["tickers"].values())

    def suggest(self, name):
        self.calls.append(("suggest", name))
        return copy.deepcopy(FX["suggest"]) if "cryo" in name.lower() else {"hits": {"total": {"value": 0}, "hits": []}}

    def submissions(self, cik):
        self.calls.append(("submissions", cik))
        sub = copy.deepcopy(FX["submissions"])
        if cik != 9000001:
            sub.update(cik=f"{cik:010d}", name="EXAMPLE CRYO SYSTEMS INC", tickers=[], exchanges=[])
        return sub

    def facts(self, cik):
        self.calls.append(("facts", cik))
        if self.facts_missing:
            raise SourceError("SEC EDGAR: not found (404). Check the id; the search tools list valid ones.")
        return copy.deepcopy(FX["facts"])

    def search(self, params):
        self.calls.append(("search", params))
        d = copy.deepcopy(FX["search"])
        if self.total:
            d["hits"]["total"] = {"value": self.total, "relation": "gte"}
        return d

    def document(self, cik, accession, document):
        self.calls.append(("document", cik, accession, document))
        return FILING


@unittest.skipIf(server is None, "needs the MCP SDK")
class ServerCase(unittest.TestCase):
    contact = CONTACT

    def setUp(self):
        self.saved = (server.edgar, {v: os.environ.get(v) for v in CONTACT_VARS})
        for v in CONTACT_VARS:
            os.environ.pop(v, None)
        if self.contact:
            os.environ["SEC_CONTACT"] = self.contact

    def tearDown(self):
        server.edgar = self.saved[0]
        for v, value in self.saved[1].items():
            if value is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = value

    def use(self, fake):
        server.edgar = fake
        return fake

    def call(self, tool, **kwargs):
        text = getattr(server, tool)(**kwargs)
        self.assertNotIn("\n", text)              # compact
        return json.loads(text)


class NoContactTest(ServerCase):
    contact = None

    def test_every_tool_refuses_and_says_how_to_set_it(self):
        fake = self.use(FakeEdgar())
        for name, email in ((None, None), ("${user_config.sec_name}", "${user_config.sec_email}"), ("", "")):
            for var, value in (("SEC_NAME", name), ("SEC_EMAIL", email)):
                if value is not None:
                    os.environ[var] = value
            for tool, kwargs in (("edgar_company", {"company": "EXRK"}), ("edgar_search", {"query": "NASA"}),
                                 ("edgar_financials", {"company": "EXRK"}),
                                 ("edgar_read_filing", {"cik": "9000001", "accession": "0009000001-25-000010"})):
                with self.assertRaisesRegex(ToolError, "^SEC requires a name and an email.*/plugin configure "
                                                       "agent-techport-sources@agent-techport-sources.*restart"):
                    getattr(server, tool)(**kwargs)
        self.assertEqual(fake.calls, [])

    def test_each_missing_or_wrong_option_is_named(self):
        self.use(FakeEdgar())
        os.environ["SEC_NAME"] = "Jane Doe"                                   # the email left empty
        with self.assertRaisesRegex(ToolError, '"Your email address" option isn\'t set'):
            server.edgar_company(company="EXRK")
        os.environ["SEC_EMAIL"] = "jane at example"
        with self.assertRaisesRegex(ToolError, "doesn't look like an email address"):
            server.edgar_company(company="EXRK")
        os.environ["SEC_NAME"] = "Jane Doe\r\nX-Other: 1"
        os.environ["SEC_EMAIL"] = "jane@example.com"
        with self.assertRaisesRegex(ToolError, "can't be sent"):
            server.edgar_company(company="EXRK")

    def test_name_and_email_make_the_contact(self):
        fake = self.use(FakeEdgar())
        os.environ["SEC_NAME"], os.environ["SEC_EMAIL"] = " Jane  Doe ", "jane@example.com"
        self.assertEqual(server._contact(), ("Jane Doe jane@example.com", None))
        os.environ["SEC_NAME"] = ""                                           # the email alone will do
        self.assertEqual(server._contact(), ("jane@example.com", None))
        self.assertEqual(self.call("edgar_company", company="EXRK")["company"]["cik"], 9000001)

    def test_the_plugin_option_variables_work_too(self):
        self.use(FakeEdgar())
        os.environ["CLAUDE_PLUGIN_OPTION_SEC_NAME"] = "Jane Doe"
        os.environ["CLAUDE_PLUGIN_OPTION_SEC_EMAIL"] = "jane@example.com"
        self.assertEqual(server._contact(), ("Jane Doe jane@example.com", None))


@unittest.skipIf(server is None, "needs the MCP SDK")
class NamesItTest(unittest.TestCase):
    def test_a_name_must_start_the_filers_name(self):
        self.assertTrue(server._names_it("Virgin Galactic Holdings, Inc", "virgin galactic"))
        self.assertFalse(server._names_it("American Ventures QP Opportunity Fund LLC, Series V Blue Origin",
                                          "Blue Origin"))
        self.assertFalse(server._names_it("Anything", "  "))


class CompanyTest(ServerCase):
    def test_by_ticker(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_company", company="exrk")
        self.assertEqual(body["company"]["name"], "Example Rocket Corp")
        self.assertEqual(body["filings"]["columns"], ["form", "filed", "period", "description", "items", "accession",
                                                     "document"])
        self.assertEqual((body["filings_total"], body["returned"]), (4, 4))
        self.assertEqual(fake.calls, [("tickers",), ("submissions", 9000001)])

    def test_forms_and_dates(self):
        self.use(FakeEdgar())
        body = self.call("edgar_company", company="9000001", forms=["10-K"])
        self.assertEqual([r[0] for r in body["filings"]["rows"]], ["10-K", "10-K/A"])
        body = self.call("edgar_company", company="9000001", since="2025-08-01")
        self.assertEqual(body["filings_total"], 2)

    def test_several_matches_are_listed(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_company", company="Example Rocket")
        self.assertEqual([r[0] for r in body["candidates"]["rows"]], [9000001, 9000003])
        self.assertIn("ticker", body["note"])
        self.assertNotIn("submissions", [c[0] for c in fake.calls])

    def test_a_company_without_a_ticker_from_edgars_index(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_company", company="Example Cryo Systems Inc")
        self.assertEqual(body["company"]["cik"], 9000004)
        self.assertIn(("suggest", "Example Cryo Systems Inc"), fake.calls)

    def test_no_match(self):
        self.use(FakeEdgar())
        with self.assertRaisesRegex(ToolError, "private companies"):
            server.edgar_company(company="Nonexistent Widgets")


class SearchTest(ServerCase):
    def test_hits_and_counts(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_search", query='"SBIR Phase III" NASA', forms=["10-K"], year_from=2020, company="EXRK")
        params = fake.calls[-1][1]
        self.assertEqual((params["ciks"], params["forms"], params["startdt"]), ("0009000001", "10-K", "2020-01-01"))
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["hits"]["columns"][:3], ["company", "cik", "form"])
        self.assertEqual(body["by_company"], {"Example Rocket Corp (EXRK)": 1, "Example Lunar Inc.": 1})

    def test_no_passages_unless_asked(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_search", query='"SBIR Phase III" NASA')
        self.assertNotIn("passage", body["hits"]["columns"])
        self.assertNotIn("document", [c[0] for c in fake.calls])

    def test_passages_for_the_top_hits(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_search", query='"SBIR Phase III" NASA', passages=1)
        rows = [dict(zip(body["hits"]["columns"], r)) for r in body["hits"]["rows"]]
        self.assertIn("SBIR Phase III contract", rows[0]["passage"])
        self.assertEqual(rows[0]["found"], 3)
        self.assertIsNone(rows[1]["passage"])
        self.assertEqual([c for c in fake.calls if c[0] == "document"],
                         [("document", 9000001, "0009000001-25-000010", "exrk-10k2024.htm")])

    def test_passages_are_capped_and_failures_named(self):
        class OneFails(FakeEdgar):
            def document(self, cik, accession, document):
                if cik == 9000002:
                    self.calls.append(("document", cik, accession, document))
                    raise SourceError("SEC EDGAR returned HTTP 503.")
                return super().document(cik, accession, document)
        fake = self.use(OneFails())
        body = self.call("edgar_search", query='"SBIR Phase III" NASA', passages=50)
        self.assertEqual(len([c for c in fake.calls if c[0] == "document"]), 2)      # only two hits here
        self.assertIn("capped at 10", body["note"])
        self.assertIn("0009000002-24-000003", body["note"])
        self.assertIn("503", body["note"])

    def test_a_pdf_hit_is_named_not_read(self):
        class WithPdf(FakeEdgar):
            def search(self, params):
                d = super().search(params)
                d["hits"]["hits"][1]["_id"] = "0009000002-24-000003:annual-report.pdf"
                return d
        fake = self.use(WithPdf())
        body = self.call("edgar_search", query='"SBIR Phase III" NASA', passages=2)
        self.assertIn("annual-report.pdf is a PDF", body["note"])
        self.assertEqual(len([c for c in fake.calls if c[0] == "document"]), 1)

    def test_paging_and_large_totals(self):
        fake = self.use(FakeEdgar(total=10000))
        body = self.call("edgar_search", query="NASA", limit=1, offset=40)
        self.assertEqual(fake.calls[-1][1]["from"], 40)
        self.assertEqual((body["returned"], body["offset"]), (1, 40))
        self.assertIn("at least 10,000", body["note"].lower())

    def test_errors(self):
        self.use(FakeEdgar())
        with self.assertRaisesRegex(ToolError, "2001"):
            server.edgar_search(query="NASA", year_from=1990)
        with self.assertRaisesRegex(ToolError, "Several companies"):
            server.edgar_search(query="NASA", company="Example Rocket")


class FinancialsTest(ServerCase):
    def test_years_and_concepts(self):
        self.use(FakeEdgar())
        body = self.call("edgar_financials", company="EXRK")
        self.assertEqual(body["years"]["columns"], ["year", "revenue", "rd_expense", "operating_income", "net_income",
                                                    "cash", "total_assets"])
        self.assertEqual([r[0] for r in body["years"]["rows"]], [2016, 2017, 2018, 2019])
        self.assertEqual(body["concepts"]["revenue"], {"SalesRevenueNet": [2016, 2017],
                                                       "RevenueFromContractWithCustomerExcludingAssessedTax": [2018, 2019]})
        self.assertIn("calendar", body["note"])
        self.assertIn("revenue: different years use different concepts", body["note"])
        self.assertIn("OperatingIncomeLoss", body["note"])

    def test_no_financial_data(self):
        self.use(FakeEdgar(facts_missing=True))
        with self.assertRaisesRegex(ToolError, "no XBRL financial data"):
            server.edgar_financials(company="EXRK")


class ReadFilingTest(ServerCase):
    def test_a_part_with_its_pages(self):
        fake = self.use(FakeEdgar())
        body = self.call("edgar_read_filing", cik="9000001", accession="0009000001-25-000010", document="exrk-10k2024.htm")
        self.assertEqual((body["part"], body["parts"], body["pages"]), (1, 1, "1-3"))
        self.assertTrue(body["text"].startswith("[page 1]"))
        self.assertEqual(body["url"], "https://www.sec.gov/Archives/edgar/data/9000001/000900000125000010/exrk-10k2024.htm")
        self.assertEqual(fake.calls, [("document", 9000001, "0009000001-25-000010", "exrk-10k2024.htm")])

    def test_passages(self):
        self.use(FakeEdgar())
        body = self.call("edgar_read_filing", cik=9000001, accession="000900000125000010", find="SBIR | NASA")
        self.assertEqual(body["passages"]["columns"], ["part", "page", "matches", "text"])
        self.assertEqual(body["matches"], 3)

    def test_the_main_document_is_found_for_recent_filings(self):
        fake = self.use(FakeEdgar())
        self.call("edgar_read_filing", cik="9000001", accession="0009000001-25-000010")
        self.assertEqual(fake.calls[-1], ("document", 9000001, "0009000001-25-000010", "exrk-10k2024.htm"))
        with self.assertRaisesRegex(ToolError, "document"):
            server.edgar_read_filing(cik="9000001", accession="0009000001-20-000001")

    def test_bad_values(self):
        fake = self.use(FakeEdgar())
        with self.assertRaisesRegex(ToolError, "accession"):
            server.edgar_read_filing(cik="9000001", accession="10-K")
        with self.assertRaisesRegex(ToolError, "cik"):
            server.edgar_read_filing(cik="Example", accession="0009000001-25-000010")
        with self.assertRaisesRegex(ToolError, "document"):
            server.edgar_read_filing(cik="9000001", accession="0009000001-25-000010", document="../x")
        with self.assertRaisesRegex(ToolError, "PDF.*https://www.sec.gov/Archives/edgar/data/9000001/"):
            server.edgar_read_filing(cik="9000001", accession="0009000001-25-000010", document="report.pdf")
        self.assertEqual(fake.calls, [])


@unittest.skipIf(server is None or shutil.which("uv") is None, "needs version 2 of the MCP SDK, and uv")
class EdgarOverStdioTest(unittest.TestCase):
    """The exact plugin.json command, with no contact set: every call fails
    before any request, so the test needs no network."""

    def test_tools_and_errors(self):
        anyio.run(self._session)

    async def _session(self):
        root = os.path.abspath(PLUGIN_ROOT)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)["mcpServers"]["edgar"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"]}
            async with stdio_client(StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    self.assertEqual(set(tools), {"edgar_company", "edgar_search", "edgar_financials",
                                                  "edgar_read_filing"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
                    r = await s.call_tool("edgar_company", {"company": "RKLB"})
                    self.assertTrue(r.is_error)
                    self.assertIn("SEC requires", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
