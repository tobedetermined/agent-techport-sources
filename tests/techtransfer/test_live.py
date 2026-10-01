"""Checks against the live Technology Transfer portal that its responses still
have the shapes the tools rely on (probed 2026-09-30): values by position, and
patent pages with the same class names. Network needed; run with LIVE_TESTS=1
and the plugin's Python."""

import os
import unittest

from servers.techtransfer import api, model


@unittest.skipUnless(os.environ.get("LIVE_TESTS"), "set LIVE_TESTS=1 to test against the real hosts")
class LiveTechTransferTest(unittest.TestCase):
    def test_search_still_returns_values_by_position(self):
        [r] = [r for r in api.TechTransfer().search("patent", "KSC-TOPS-59") if r[1] == "KSC-TOPS-59"]
        rec = model.record("patent", r)
        self.assertEqual(len(r), 13)
        self.assertEqual(rec["center"], "KSC")
        self.assertIn("cryogenic", rec["title"].lower())
        self.assertIsInstance(rec["score"], float)

    def test_a_patent_page_still_has_its_sections(self):
        d = model.page_details(api.TechTransfer().page("KSC-TOPS-59"))
        self.assertTrue(d["benefits"] and d["applications"] and d["case_numbers"])
        self.assertIn("10,273,024", [p["number"] for p in d["patents"]])


if __name__ == "__main__":
    unittest.main()
