"""USAspending server tests that need the MCP SDK. The API is replaced by a fake
serving made-up records (tests/fixtures/usaspending.json), so there is no
network. Run with the plugin's Python:

    plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .
"""

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
    from servers.usaspending import model, server
else:
    server = None

with open(os.path.join(FIXTURES, "usaspending.json"), encoding="utf-8") as fh:
    FX = json.load(fh)


def contract_row(i, description=None, uei=None, amount=None, signed="2024-01-01"):
    return {"internal_id": i, "generated_internal_id": f"CONT_AWD_C{i:04d}", "Award ID": f"C{i:04d}",
            "Recipient Name": f"EXAMPLE CO {i}", "Recipient UEI": uei or f"EXMPL{i:07d}",
            "Award Amount": amount if amount is not None else 1000.0 * (500 - i),
            "Start Date": "2024-01-01", "End Date": "2025-01-01",
            "Awarding Agency": "National Aeronautics and Space Administration",
            "PSC": {"code": "AR12", "description": "SPACE R&D"}, "Description": description,
            "Base Obligation Date": signed}


class FakeUSAspending:
    """Serves the fixture records, and pages through `rows` as the API does
    (100 per page); records every call."""

    def __init__(self, rows=(), rows_by_type=None):
        self.rows_by_type = rows_by_type or {"contracts": list(rows)}
        self.calls = []

    def _type(self, codes):
        return next((t for t, c in model.AWARD_TYPES.items() if c == codes), "contracts")

    def toptier_agencies(self):
        return FX["toptier_agencies"]["results"]

    def last_updated(self):
        return FX["last_updated"]["last_updated"]

    def search(self, body):
        self.calls.append(("search", body))
        rows = self.rows_by_type.get(self._type(body["filters"].get("award_type_codes")), [])
        start = (body["page"] - 1) * body["limit"]
        return {"results": rows[start:start + body["limit"]], "page_metadata": {"page": body["page"], "hasNext": False}}

    def count(self, filters):
        self.calls.append(("count", filters))
        t = self._type(filters.get("award_type_codes"))
        return {g: (len(self.rows_by_type.get(g, [])) if g == t else 0)
                for g in ("contracts", "grants", "idvs", "direct_payments", "loans", "other")}

    def award(self, award_id):
        self.calls.append(("award", award_id))
        return FX["award_contract"] if award_id.startswith("CONT") else FX["award_grant"]

    def transactions(self, award_id, limit):
        self.calls.append(("transactions", award_id, limit))
        return FX["transactions"]

    def subawards(self, award_id, limit):
        self.calls.append(("subawards", award_id, limit))
        return FX["subawards"]

    def recipients(self, keyword, limit=20):
        self.calls.append(("recipients", keyword))
        return FX["recipients"]

    def recipient(self, recipient_id):
        self.calls.append(("recipient", recipient_id))
        return FX["recipient_profile"]

    def by_category(self, category, filters, limit):
        self.calls.append(("category", category, filters, limit))
        return FX["by_agency"]

    def over_time(self, filters):
        self.calls.append(("over_time", filters))
        return FX["over_time"]


@unittest.skipIf(server is None, "needs the MCP SDK")
class ServerTest(unittest.TestCase):
    def setUp(self):
        self.saved = server.usa
        server._cache.clear()

    def tearDown(self):
        server.usa = self.saved
        server._cache.clear()

    def use(self, fake):
        server.usa = fake
        return fake

    def column(self, table, name):
        return [r[table["columns"].index(name)] for r in table["rows"]]

    # search

    def test_a_page_across_the_apis_pages_with_the_total_from_the_count(self):
        fake = self.use(FakeUSAspending([contract_row(i) for i in range(250)]))
        text = server.usaspending_search_awards(agency="NASA", limit=10, offset=95)
        self.assertNotIn("\n", text)
        body = json.loads(text)
        self.assertEqual((body["total"], body["offset"], body["returned"]), (250, 95, 10))
        self.assertEqual(self.column(body["awards"], "award_id"), [f"C{i:04d}" for i in range(95, 105)])
        self.assertEqual(sorted(c[1]["page"] for c in fake.calls if c[0] == "search"), [1, 2])
        self.assertEqual(body["awards"]["columns"][:3], ["id", "award_id", "recipient"])

    def test_phase_iii_keeps_only_descriptions_that_say_so(self):
        fake = self.use(FakeUSAspending([contract_row(1, "SBIR PHASE III - A"), contract_row(2, "SBIR PHASE II - B"),
                                         contract_row(3, "FY24 STTR PHASE III - C")]))
        body = json.loads(server.usaspending_search_awards(agency="NASA", sbir_phase_iii=True))
        self.assertEqual((body["total"], self.column(body["awards"], "award_id")), (2, ["C0001", "C0003"]))
        sent = next(c[1] for c in fake.calls if c[0] == "search")
        self.assertEqual(sent["filters"]["keywords"], model.PHASE_III_KEYWORDS)
        self.assertIn("Description", sent["fields"])
        self.assertIn("description", body["note"])

    def test_signed_only_asks_for_awards_signed_in_the_years(self):
        fake = self.use(FakeUSAspending([contract_row(1)]))
        server.usaspending_search_awards(fiscal_year_from=2015, signed_only=True)
        sent = next(c[1] for c in fake.calls if c[0] == "search")
        self.assertEqual(sent["filters"]["time_period"][0]["date_type"], "date_signed")

    def test_bad_values_come_back_as_tool_errors(self):
        self.use(FakeUSAspending())
        with self.assertRaisesRegex(ToolError, "Unknown agency"):
            server.usaspending_search_awards(agency="NASSA")
        with self.assertRaisesRegex(ToolError, "2008"):
            server.usaspending_search_awards(fiscal_year_from=2001)

    # one award

    def test_one_award_in_full_with_transactions_and_subawards(self):
        self.use(FakeUSAspending())
        text = server.usaspending_get_award(id="CONT_AWD_TESTNASA0001_8000_-NONE-_-NONE-",
                                            include_transactions=True, include_subawards=True)
        body = json.loads(text)
        self.assertEqual(body["award"]["recipient"]["name"], "EXAMPLE CRYO SYSTEMS INC")
        self.assertEqual(self.column(body["transactions"], "obligation"), [250000.0, 1000000.0])
        self.assertEqual(self.column(body["subawards"], "recipient"), ["EXAMPLE MACHINE SHOP", "EXAMPLE TEST LAB"])
        self.assertNotIn("TEST EXECUTIVE ONE", text)
        self.assertEqual(body["data"]["data_updated"], "09/30/2026")

    def test_an_award_number_is_found_whatever_its_type(self):
        fake = self.use(FakeUSAspending(rows_by_type={"grants": [{**contract_row(7), "generated_internal_id": "ASST_NON_G7"}]}))
        body = json.loads(server.usaspending_get_award(award_id="TESTGRANT0001"))
        self.assertEqual(body["award"]["award_id"], "TESTGRANT0001")
        self.assertEqual({c[1]["filters"]["award_ids"][0] for c in fake.calls if c[0] == "search"}, {"TESTGRANT0001"})

    def test_a_number_shared_by_two_awards_lists_both(self):
        self.use(FakeUSAspending(rows_by_type={"contracts": [contract_row(1)], "idvs": [contract_row(2)]}))
        body = json.loads(server.usaspending_get_award(award_id="C0001"))
        self.assertEqual(body["matches"], 2)
        self.assertNotIn("award", body)

    # a company

    def test_a_company_by_uei_across_agencies_and_years(self):
        fake = self.use(FakeUSAspending())
        body = json.loads(server.usaspending_recipient(uei="exmplcry0001"))
        self.assertEqual((body["profile"]["name"], body["profile"]["parent_uei"]), ("EXAMPLE CRYO SYSTEMS INC", "EXMPLHLD0001"))
        self.assertEqual(body["by_agency"]["rows"], [["DOD", "Department of Defense", 2000000.0],
                                                     ["NASA", "National Aeronautics and Space Administration", 1000000.0]])
        self.assertEqual(body["by_fiscal_year"]["rows"], [[2024, 1000000.0], [2025, 2000000.0]])
        self.assertIn(("recipient", "aaaa-C"), fake.calls)                     # the company's own record, not its parent's
        category = next(c for c in fake.calls if c[0] == "category")
        self.assertEqual(category[2]["recipient_search_text"], ["EXMPLCRY0001"])

    def test_a_company_by_name_lists_who_matches(self):
        fake = self.use(FakeUSAspending())
        body = json.loads(server.usaspending_recipient(name="Example Cryo"))
        self.assertIn("uei", body["recipients"]["columns"])
        self.assertIn("former or trade names", body["note"])
        self.assertFalse(any(c[0] == "recipient" for c in fake.calls))

    # totals

    def test_totals_by_group_and_by_year(self):
        fake = self.use(FakeUSAspending())
        body = json.loads(server.usaspending_aggregate(group_by="awarding_agency", company="EXMPLCRY0001", limit=5))
        self.assertEqual(self.column(body["groups"], "obligated"), [2000000.0, 1000000.0])
        self.assertEqual(next(c for c in fake.calls if c[0] == "category")[1:4:2], ("awarding_agency", 5))
        body = json.loads(server.usaspending_aggregate(group_by="fiscal_year", agency="NASA"))
        self.assertEqual(body["groups"]["rows"], [[2024, 1000000.0], [2025, 2000000.0]])
        self.assertTrue(any("obligated" in c for c in body["data"]["caveats"]))
        with self.assertRaisesRegex(ToolError, "recipient"):
            server.usaspending_aggregate(group_by="city")

    def test_keyword_totals_warn_that_only_matching_transactions_count(self):
        self.use(FakeUSAspending())
        warned = lambda body: any("keyword" in c.lower() for c in body["data"]["caveats"])
        self.assertTrue(warned(json.loads(server.usaspending_aggregate(group_by="recipient", keywords="cryogenic"))))
        self.assertFalse(warned(json.loads(server.usaspending_aggregate(group_by="recipient"))))

    def test_whole_award_totals_of_phase_iii_contracts_by_company(self):
        # Two Phase III contracts for one UEI (300 + 200), one for another (400); the Phase II
        # row matches the keywords loosely and is dropped.
        fake = self.use(FakeUSAspending([
            contract_row(1, "SBIR PHASE III - A", uei="EXMPLAAA0001", amount=300.0),
            contract_row(2, "SBIR PHASE II - B", uei="EXMPLBBB0002", amount=900.0),
            contract_row(3, "STTR PHASE III - C", uei="EXMPLCCC0003", amount=400.0),
            contract_row(4, "FY24 SBIR PHASE III - D", uei="EXMPLAAA0001", amount=200.0)]))
        body = json.loads(server.usaspending_aggregate(group_by="recipient", agency="NASA", sbir_phase_iii=True))
        self.assertEqual([(r[0], r[2], r[3]) for r in body["groups"]["rows"]],
                         [("EXMPLAAA0001", 2, 500.0), ("EXMPLCCC0003", 1, 400.0)])
        self.assertEqual(body["awards"], 3)
        self.assertFalse(any(c[0] == "category" for c in fake.calls))       # not the per-transaction totals
        self.assertIn("Base Obligation Date", next(c[1] for c in fake.calls if c[0] == "search")["fields"])
        self.assertTrue(any("whole award" in c for c in body["data"]["caveats"]))
        top = json.loads(server.usaspending_aggregate(group_by="recipient", agency="NASA", sbir_phase_iii=True, limit=1))
        self.assertEqual(([r[0] for r in top["groups"]["rows"]], top["groups_total"]), (["EXMPLAAA0001"], 2))
        self.assertIn("top 1 of 2", top["note"])

    def test_whole_award_totals_of_award_numbers_by_year_signed(self):
        self.use(FakeUSAspending(rows_by_type={
            "contracts": [contract_row(1, amount=100.0, signed="2019-10-01")],
            "grants": [{**contract_row(2, amount=50.0, signed="2019-09-30"), "generated_internal_id": "ASST_NON_G2"}]}))
        body = json.loads(server.usaspending_aggregate(group_by="fiscal_year", award_ids=["C0001", "C0002"]))
        self.assertEqual(body["groups"]["rows"], [[2019, 1, 50.0], [2020, 1, 100.0]])

    def test_award_numbers_with_no_award_are_listed(self):
        self.use(FakeUSAspending(rows_by_type={"contracts": [contract_row(1)], "grants": [contract_row(2)]}))
        body = json.loads(server.usaspending_aggregate(group_by="recipient", award_ids=["c0001", "C0002", "NOT-A-REAL-ID"]))
        self.assertEqual(body["awards"], 2)
        self.assertIn("['NOT-A-REAL-ID']", body["note"])
        body = json.loads(server.usaspending_search_awards(award_ids=["C0001", "NOT-A-REAL-ID"]))
        self.assertIn("No contracts carry these award numbers: ['NOT-A-REAL-ID']", body["note"])
        body = json.loads(server.usaspending_search_awards(award_ids=["C0001"]))
        self.assertNotIn("note", body)

    def test_whole_award_totals_only_by_company_agency_or_year(self):
        self.use(FakeUSAspending())
        with self.assertRaisesRegex(ToolError, "recipient, awarding_agency or fiscal_year"):
            server.usaspending_aggregate(group_by="naics", sbir_phase_iii=True)


@unittest.skipIf(server is None or shutil.which("uv") is None, "needs version 2 of the MCP SDK, and uv")
class UsaspendingOverStdioTest(unittest.TestCase):
    """The exact plugin.json command. Only calls that fail before any network
    request, so the test needs no network."""

    def test_tools_and_errors(self):
        anyio.run(self._session)

    async def _session(self):
        root = os.path.abspath(PLUGIN_ROOT)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)["mcpServers"]["usaspending"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"]}
            async with stdio_client(StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    self.assertEqual(set(tools), {"usaspending_search_awards", "usaspending_get_award",
                                                  "usaspending_recipient", "usaspending_aggregate"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
                    r = await s.call_tool("usaspending_search_awards", {"award_type": "loans"})
                    self.assertTrue(r.is_error)
                    self.assertIn("contracts", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
