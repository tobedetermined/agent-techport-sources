"""Starts the SBIR server with the exact command in plugin.json and talks to it
over stdio, the way Claude Code does. Needs the MCP SDK, so run it with the
plugin's environment:

    plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .

Skipped under a plain Python that doesn't have version 2 of the SDK.
"""

import json
import os
import re
import shutil
import tempfile
import unittest

from tests import FIXTURES, PLUGIN_ROOT
from servers.sbir.queries import PUBLIC_COLUMNS

try:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    # Only in version 2. A 1.x client would import, but names fields differently.
    from mcp.server.mcpserver import MCPServer  # noqa: F401
except ImportError:
    anyio = None

ROOT = os.path.abspath(PLUGIN_ROOT)


@unittest.skipIf(anyio is None or shutil.which("uv") is None, "needs version 2 of the MCP SDK, and uv")
class ServerOverStdioTest(unittest.TestCase):
    def test_tools_over_stdio(self):
        anyio.run(self._session)

    async def _session(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            cfg = json.load(f)["mcpServers"]["sbir"]
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", ROOT) for a in cfg["args"]]
        with tempfile.TemporaryDirectory() as data:
            env = {"CLAUDE_PLUGIN_DATA": data, "PATH": os.environ["PATH"],
                   "SBIR_CSV_PATH": os.path.join(FIXTURES, "sbir_sample.csv")}
            params = StdioServerParameters(command=cfg["command"], args=args, env=env, cwd=data)
            async with stdio_client(params) as streams:
                async with ClientSession(*streams[:2]) as s:
                    await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    self.assertEqual(set(tools), {"sbir_search_awards", "sbir_get_award",
                                                  "sbir_company", "sbir_aggregate", "sbir_query"})
                    self.assertTrue(all(t.annotations.read_only_hint for t in tools.values()))
                    # The description is the model's only schema: it must name every column it can read.
                    undocumented = [c for c in PUBLIC_COLUMNS
                                    if not re.search(rf"\b{c}\b", tools["sbir_query"].description)]
                    self.assertEqual(undocumented, [])

                    r = await s.call_tool("sbir_query", {"sql": "SELECT agency_code, count(*) AS n FROM awards "
                                                                "WHERE agency_code IS NOT NULL GROUP BY 1 ORDER BY 1"})
                    self.assertFalse(r.is_error)
                    text = r.content[0].text
                    self.assertNotIn("\n", text)       # compact: the size limit applies to what the model reads
                    body = json.loads(text)
                    self.assertEqual((body["columns"], body["rows"]), (["agency_code", "n"], [["DOD", 1], ["NASA", 3]]))
                    self.assertIn("caveats", body["data"])

                    r = await s.call_tool("sbir_query", {"sql": "SELECT pi_email FROM main.awards"})
                    self.assertTrue(r.is_error)
                    self.assertIn("include_contacts", r.content[0].text)

                    r = await s.call_tool("sbir_aggregate", {"group_by": "agency", "agency": "NASA"})
                    self.assertFalse(r.is_error)
                    body = json.loads(r.content[0].text)
                    self.assertEqual(body["totals"]["awards"], 3)
                    self.assertIn("caveats", body["data"])
                    self.assertEqual(body["groups"]["rows"], [["NASA", 3, 1150000.0, 0]])

                    # Every tool answers in compact JSON, lists of records as tables.
                    r = await s.call_tool("sbir_search_awards", {"agency": "NASA"})
                    self.assertNotIn("\n", r.content[0].text)
                    awards = json.loads(r.content[0].text)["awards"]
                    self.assertIn("award_year", awards["columns"])
                    self.assertEqual(len(awards["rows"]), 3)
                    r = await s.call_tool("sbir_company", {"uei": "EXMPLORB0002"})
                    self.assertEqual(json.loads(r.content[0].text)["by_year"],
                                     {"columns": ["value", "awards", "amount_total"],
                                      "rows": [[2023, 1, 750000.0], [2024, 1, 250000.0]]})
                    # Full award records stay records: many fields, few rows.
                    r = await s.call_tool("sbir_get_award", {"contract": "TEST-NASA-0002"})
                    self.assertNotIn("\n", r.content[0].text)
                    self.assertEqual(len(json.loads(r.content[0].text)["awards"]), 2)

                    r = await s.call_tool("sbir_search_awards", {"agency": "NASSA"})
                    self.assertTrue(r.is_error)
                    self.assertIn("Use one of", r.content[0].text)


if __name__ == "__main__":
    unittest.main()
