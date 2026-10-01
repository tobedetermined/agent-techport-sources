"""TechPort server tests that need the MCP SDK. Run with the plugin's Python:

    plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .
"""

import json
import os
import re
import shutil
import tempfile
import unittest

from tests import FIXTURES, PLUGIN_ROOT

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.server.mcpserver import MCPServer  # noqa: F401  version 2 only
except ImportError:          # no SDK (version 2): these tests are skipped
    anyio = None
if anyio is not None:        # with the SDK, a broken server must fail the tests, not skip them
    from servers.techport import server
    from servers.common import output
    from servers.common.http import TooLarge
else:
    server = None

MB = 1024 * 1024


class FakeTechPort:
    """Serves files of given sizes, and the fixture project with any extra library
    items; records what was fetched."""

    def __init__(self, sizes, extra_items=()):
        self.sizes = sizes
        self.extra_items = list(extra_items)
        self.fetched = []

    def file(self, file_id, max_bytes):
        self.fetched.append(file_id)
        size = self.sizes[file_id]
        if size > max_bytes:
            raise TooLarge("too big")
        return b"%PDF" + b"x" * (size - 4), "application/pdf"

    def project(self, project_id):
        with open(os.path.join(FIXTURES, "techport_project.json"), encoding="utf-8") as fh:
            p = json.load(fh)["project"]
        p["libraryItems"] = p["libraryItems"] + self.extra_items
        return p

    def search(self, query, limit):
        return {"results": []}

    def programs(self, active_only):
        return [{"programId": 64, "acronym": "FO", "title": "Flight Opportunities"},
                {"programId": 92295, "acronym": "HFORT", "title": "Human Flight Opportunities Research Test"}]

    def opportunities(self):
        return [{"opportunityId": i, "name": f"Opportunity {i}", "description": "About it."} for i in (1, 2, 3)]


def f(file_id, title, kind="Document", size=None, ext="pdf"):
    return {"fileId": file_id, "title": title, "type": kind, "extension": ext, "bytes": size,
            "url": f"https://techport.nasa.gov/api/file/{file_id}"}


def item(file_id, title, ext, size, kind="Document"):
    """A library item in TechPort's own shape, as the live API returns it."""
    return {"title": title, "libraryItemType": kind,
            "files": [{"fileId": file_id, "fileExtension": ext, "fileSize": size}]}


@unittest.skipIf(server is None, "needs the MCP SDK")
class FetchFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (server.tp, os.environ.get("CLAUDE_PLUGIN_DATA"))
        os.environ["CLAUDE_PLUGIN_DATA"] = self.tmp

    def tearDown(self):
        server.tp = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self.saved[1]
        shutil.rmtree(self.tmp)

    def run_files(self, files, sizes):
        server.tp = FakeTechPort(sizes)
        blocks, report = server._fetch_files(files)
        return blocks, {r["fileId"]: r for r in report}

    def test_most_useful_first_within_the_budget(self):
        files = [f(1, "Some image", "Image", 4 * MB, "png"), f(2, "Technical paper", size=4 * MB),
                 f(3, "Final Report", size=4 * MB), f(4, "Briefing Chart", size=4 * MB)]
        blocks, report = self.run_files(files, {1: 4 * MB, 2: 4 * MB, 3: 4 * MB, 4: 4 * MB})
        self.assertEqual([r for r in report if report[r]["included"]], [3, 4])      # 8 MB; a third would pass 11
        self.assertEqual([b.resource.uri.rsplit("/", 1)[1] for b in blocks], ["3", "4"])
        self.assertIn("techport_get_document", report[2]["reason"])
        self.assertEqual(server.tp.fetched, [3, 4])      # files that can't fit aren't even downloaded

    def test_sizes_unknown_until_downloaded(self):
        files = [f(1, "Briefing Chart"), f(2, "Other")]
        _, report = self.run_files(files, {1: 10 * MB, 2: 2 * MB})
        self.assertTrue(report[1]["included"])
        self.assertFalse(report[2]["included"])

    def test_over_the_attach_limit_is_saved_locally(self):
        _, report = self.run_files([f(1, "Big final report", size=15 * MB)], {1: 15 * MB})
        self.assertFalse(report[1]["included"])
        self.assertTrue(os.path.exists(report[1]["saved_to"]))
        self.assertTrue(report[1]["saved_to"].startswith(self.tmp))                  # the plugin's own folder

    def test_over_the_cap_is_a_link_only(self):
        _, report = self.run_files([f(1, "Huge", size=25 * MB), f(2, "Huge, size unknown")],
                                   {1: 25 * MB, 2: 25 * MB})
        self.assertIn("20 MB cap", report[1]["reason"])
        self.assertIn("20 MB cap", report[2]["reason"])
        self.assertEqual(server.tp.fetched, [2])

    def test_not_a_document(self):
        class Html(FakeTechPort):
            def file(self, file_id, max_bytes):
                return b"<html>", "text/html"
        server.tp = Html({})
        _, report = server._fetch_files([f(1, "Odd", ext=None)])
        self.assertIn("not a PDF or image", report[0]["reason"])

    def test_known_non_documents_are_not_downloaded(self):
        _, report = self.run_files([f(1, "Launch video", "Video", 5 * MB, "mp4"), f(2, "Data", "Data", None, "csv")],
                                   {1: 5 * MB, 2: 1000})
        self.assertEqual(server.tp.fetched, [])
        self.assertEqual(report[1]["reason"], "not a PDF or image (.mp4)")

    def test_at_most_20_files_attached_per_result(self):
        files = [f(i, f"Chart {i}", size=1000) for i in range(1, 26)]
        blocks, report = self.run_files(files, {i: 1000 for i in range(1, 26)})
        self.assertEqual(len(blocks), 20)
        self.assertEqual(len(server.tp.fetched), 20)                   # the other five aren't downloaded
        self.assertEqual(sum(1 for r in report.values() if "techport_get_document" in (r["reason"] or "")), 5)

    def test_every_outcome_reports_the_same_fields(self):
        # Attached, over the cap, saved locally, and left for a later call: one table.
        files = [f(1, "Briefing Chart", size=1 * MB), f(2, "Huge", size=25 * MB),
                 f(3, "Big final report", size=15 * MB), f(4, "Paper", size=10.5 * MB)]
        server.tp = FakeTechPort({1: 1 * MB, 2: 25 * MB, 3: 15 * MB, 4: int(10.5 * MB)})
        _, report = server._fetch_files(files)
        rows = output.table(report)["rows"]
        self.assertEqual(len(rows), 4)


@unittest.skipIf(server is None, "needs the MCP SDK")
class GetProjectTest(unittest.TestCase):
    """techport_get_project for one project, from the fixture, with no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (server.tp, os.environ.get("CLAUDE_PLUGIN_DATA"))
        os.environ["CLAUDE_PLUGIN_DATA"] = self.tmp            # no daily copy here
        server.tp = FakeTechPort({800001: 1000, 800003: 500})

    def tearDown(self):
        server.tp = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self.saved[1]
        shutil.rmtree(self.tmp)

    def body(self, **kwargs):
        blocks = server.techport_get_project(project_id=900001, **kwargs)
        self.assertNotIn("\n", blocks[0].text)                 # compact
        return json.loads(blocks[0].text), blocks[1:]

    def test_each_file_is_listed_once_with_its_status(self):
        body, attached = self.body()
        files = body["project"]["files"]
        self.assertEqual([r[files["columns"].index("fileId")] for r in files["rows"]], [800001, 800003])
        self.assertEqual([r[files["columns"].index("included")] for r in files["rows"]], [True, True])
        self.assertEqual(body["documents"], {"attached": 2, "not_attached": 0})
        self.assertEqual(len(attached), 2)

    def test_without_documents_the_files_are_still_listed(self):
        body, attached = self.body(include_documents=False)
        self.assertEqual(len(body["project"]["files"]["rows"]), 2)
        self.assertNotIn("documents", body)
        self.assertEqual(attached, [])

    def test_lists_inside_the_record_are_tables(self):
        body, _ = self.body()
        self.assertEqual(body["project"]["contacts"]["columns"], ["name", "role"])    # no contact details
        self.assertIn("name", body["project"]["otherOrganizations"]["columns"])

    def test_files_it_can_never_fetch_are_counted_not_listed(self):
        server.tp = FakeTechPort({800001: 1000, 800003: 500}, extra_items=[
            item(1, "Flight video", "mp4", 50 * MB, "Video"), item(2, "Other video", "mp4", 5 * MB, "Video"),
            item(3, "Raw data", "zip", 8 * 1024 * MB, "Data"), item(4, "Big report", "pdf", 30 * MB)])
        body, _ = self.body()
        files = body["project"]["files"]
        self.assertEqual([r[files["columns"].index("fileId")] for r in files["rows"]], [800001, 800003])
        self.assertEqual(body["documents"]["not_listed"], {"not a PDF or image (.mp4)": 2,
                                                           "not a PDF or image (.zip)": 1,
                                                           "over the 20 MB cap; link only": 1})
        self.assertIn(body["project"]["url"], body["documents"]["note"])
        self.assertEqual(server.tp.fetched, [800001, 800003])          # nothing downloaded to learn its type

    def test_without_documents_unfetchable_files_are_counted_too(self):
        server.tp = FakeTechPort({}, extra_items=[item(1, "Flight video", "mp4", 50 * MB, "Video")])
        body, _ = self.body(include_documents=False)
        self.assertEqual(len(body["project"]["files"]["rows"]), 2)
        self.assertEqual(body["documents"]["not_listed"], {"not a PDF or image (.mp4)": 1})

    def test_a_long_list_of_usable_files_stops_at_the_size_limit(self):
        server.tp = FakeTechPort({}, extra_items=[item(i, f"Chart {i}", "pdf", 1000) for i in range(1, 301)])
        body, _ = self.body(include_documents=False)
        self.assertLess(len(body["project"]["files"]["rows"]), 302)
        self.assertIn("techport_get_document", body["documents"]["note"])
        self.assertIn("300]", body["documents"]["note"])               # the last file left out is named

    def test_live_search_resolves_programs_against_all_programs(self):
        with open(os.path.join(FIXTURES, "techport_search.json"), encoding="utf-8") as fh:
            found = json.load(fh)
        server.tp.search = lambda query, limit: found
        server._program_list.clear()
        try:
            body = json.loads(server.techport_find_projects(query="anything", program="FO"))
            self.assertEqual((body["total"], body["program_filter"][0]["programId"]), (1, 64))
            self.assertIn("programId", body["projects"]["columns"])
            # HFORT has no project in these results, but it is a program: zero, not an error
            body = json.loads(server.techport_find_projects(query="anything", program="HFORT"))
            self.assertEqual(body["total"], 0)
        finally:
            server._program_list.clear()

    def test_the_program_list_is_fetched_once_by_concurrent_calls(self):
        import threading, time
        calls = []

        def programs(active_only):
            calls.append(1)
            time.sleep(0.05)                              # an HTTP call, during which others run
            return [{"programId": 64, "acronym": "FO", "title": "Flight Opportunities"}]
        server.tp.programs = programs
        server._program_list.clear()
        try:
            threads = [threading.Thread(target=server._programs) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual((len(calls), len(server._program_list)), (1, 1))
        finally:
            server._program_list.clear()

    def test_shortfall_search_is_short_and_pages(self):
        long = "x" * 3000
        server.tp.shortfalls = lambda query: [{"shortfallId": i, "title": f"Shortfall {i}", "capability": "Dust",
                                               "description": long, "priority": i} for i in range(1, 41)]
        body = json.loads(server.techport_capabilities(query="dust"))
        self.assertEqual((body["total"], body["returned"]), (40, 40))              # all fit once shortened
        cols = body["shortfalls"]["columns"]
        self.assertEqual(len(body["shortfalls"]["rows"][0][cols.index("description")]), 200)
        body = json.loads(server.techport_capabilities(query="dust", offset=38))
        self.assertEqual([r[cols.index("shortfallId")] for r in body["shortfalls"]["rows"]], [39, 40])

    def test_or_and_groups_go_to_the_copy_not_techports_search(self):
        def refuse(query, limit):
            raise AssertionError("TechPort's own search was asked")
        server.tp.search = refuse
        server.tp.http = None                     # the copy is built from the fixture file
        os.environ["TECHPORT_JSON_PATH"] = os.path.join(FIXTURES, "techport_search.json")
        try:
            body = json.loads(server.techport_find_projects(query="cryogenic | nonexistentword"))
            self.assertEqual(body["total"], 1)
            self.assertIn("daily copy's keyword index", body["note"])
        finally:
            os.environ.pop("TECHPORT_JSON_PATH", None)

    def test_which_queries_count_as_grouped(self):
        grouped = lambda q: bool(server.GROUPED.search(re.sub(r'"[^"]*"', " ", q)))
        self.assertTrue(all(grouped(q) for q in ("a | b", "a OR b", "a (b c)", "a|b")))
        self.assertFalse(any(grouped(q) for q in ("ORBITAL test", '"mission OR test"', "regolith -Mars", "a or b")))

    def test_an_organization_without_a_uei_says_how_to_link_it(self):
        server.tp.organizations = lambda **kw: [
            {"organizationId": 1, "organizationName": "Example Lunar Co", "organizationType": "Industry", "uei": None},
            {"organizationId": 2, "organizationName": "Example Rover Co", "organizationType": "Industry",
             "uei": "EXMPL0000002"}]
        body = json.loads(server.techport_organizations(name="Example"))
        self.assertIn("link it to SBIR or USAspending by name", body["note"].replace("Link", "link"))
        server.tp.organizations = lambda **kw: [{"organizationId": 2, "organizationName": "Example Rover Co",
                                                 "organizationType": "Industry", "uei": "EXMPL0000002"}]
        self.assertNotIn("note", json.loads(server.techport_organizations(name="Example")))

    def test_opportunities_page_with_offset(self):
        body = json.loads(server.techport_opportunities(offset=1))
        ids = [r[body["opportunities"]["columns"].index("opportunityId")] for r in body["opportunities"]["rows"]]
        self.assertEqual((ids, body["offset"], body["returned"]), ([2, 3], 1, 2))


@unittest.skipIf(server is None, "needs the MCP SDK")
class RefreshBackoffTest(unittest.TestCase):
    def setUp(self):
        self.saved = dict(server._last_failure)

    def tearDown(self):
        server._last_failure.update(self.saved)
        server._refreshing.clear()

    def test_no_new_refresh_within_an_hour_of_a_failure(self):
        import time
        server._last_failure.update(at=time.time(), reason="TechPort is not responding")
        started = []
        real = server.threading.Thread
        server.threading.Thread = lambda *a, **k: started.append(1)
        try:
            server._refresh_in_background()
        finally:
            server.threading.Thread = real
        self.assertEqual(started, [])
        self.assertIn("refresh failed", server._copy_notice({"loaded_at": "x", "projects": "1"})["refresh_problem"])


@unittest.skipIf(server is None or shutil.which("uv") is None, "needs the MCP SDK and uv")
class TechPortOverStdioTest(unittest.TestCase):
    """The exact plugin.json command, with the daily copy built from the test file
    (TECHPORT_JSON_PATH), so only copy-backed tools are exercised: no network."""

    def test_copy_backed_tools(self):
        anyio.run(self._session)

    async def _session(self):
        root = os.path.abspath(PLUGIN_ROOT)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)["mcpServers"]["techport"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", root) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"],
                   "TECHPORT_JSON_PATH": os.path.join(FIXTURES, "techport_search.json")}
            params = StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)
            async with stdio_client(params) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name for t in (await s.list_tools()).tools}
                    self.assertEqual(len(tools), 11)
                    r = await s.call_tool("techport_aggregate", {"group_by": "program"})
                    self.assertFalse(r.is_error, r.content[0].text)
                    self.assertNotIn("\n", r.content[0].text)
                    body = json.loads(r.content[0].text)
                    self.assertEqual(body["total_projects"], 3)
                    self.assertEqual(body["data"]["projects_in_copy"], 3)
                    self.assertEqual(body["groups"]["columns"], ["programId", "acronym", "title", "projects"])
                    r = await s.call_tool("techport_find_projects", {"program": "FO"})
                    body = json.loads(r.content[0].text)
                    self.assertEqual(body["total"], 1)
                    self.assertEqual(len(body["projects"]["rows"]), 1)
                    r = await s.call_tool("techport_find_projects", {"status": "Pending"})
                    self.assertTrue(r.is_error)
                    self.assertIn("Active, Completed, Canceled", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
