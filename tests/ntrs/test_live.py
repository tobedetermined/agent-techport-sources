"""Checks against the live NTRS API that its responses still have the shapes the
tools rely on (probed 2026-09-30). Network needed; run with LIVE_TESTS=1 and
the plugin's Python."""

import os
import unittest

from servers.ntrs import api, model


@unittest.skipUnless(os.environ.get("LIVE_TESTS"), "set LIVE_TESTS=1 to test against the real hosts")
class LiveNtrsTest(unittest.TestCase):
    def test_search_still_returns_the_counts_the_tools_use(self):
        d = api.NTRS().search(model.search_body(query='"cryogenic fluid management"', limit=1))
        self.assertGreater(d["stats"]["total"], 300)                  # 334 when probed
        for name in model.GROUPS.values():
            self.assertIn(name, d["aggregations"])
        self.assertTrue(model.row(d["results"][0])["id"])

    def test_a_record_still_links_its_pdf_and_text(self):
        [f] = model.files(api.NTRS().record("20180002393"))
        self.assertTrue(f["pdf"].endswith(".pdf"))
        self.assertTrue(f["text"].startswith("https://ntrs.nasa.gov/api/citations/20180002393/downloads/"))


if __name__ == "__main__":
    unittest.main()
