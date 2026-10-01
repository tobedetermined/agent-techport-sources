import json
import os
import unittest

from tests import FIXTURES
from servers.techport import model


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class HelpersTest(unittest.TestCase):
    def test_iso_date_accepts_every_format_techport_uses(self):
        for raw, want in (("2021-05-19", "2021-05-19"), ("2021-05-19T00:00:00Z", "2021-05-19"),
                          ("2026-9-30", "2026-09-30"), ("08/07/26", "2026-08-07"), ("08/07/2026", "2026-08-07"),
                          (None, None), ("", None), ("soon", None)):
            with self.subTest(raw=raw):
                self.assertEqual(model.iso_date(raw), want)

    def test_tx_code(self):
        for raw in ("TX6.4.1", "6.4.1", "TX06.4.1", "tx06.4.1"):
            self.assertEqual(model.tx_code(raw), "TX06.4.1")
        self.assertEqual(model.tx_code("14"), "TX14")
        self.assertIsNone(model.tx_code(None))

    def test_strip_html(self):
        self.assertEqual(model.strip_html("<p>A <b>tank</b> &amp; more.</p><p>Two</p>"), "A tank & more.\nTwo")
        self.assertIsNone(model.strip_html("<p></p>"))


class NormalizeTest(unittest.TestCase):
    """Search-shape and single-project-shape records become the same dict."""

    @classmethod
    def setUpClass(cls):
        cls.search = {p["projectId"]: model.normalize(p) for p in fixture("techport_search.json")["results"]}
        cls.detail = model.normalize(fixture("techport_project.json")["project"])

    def test_both_shapes_agree_on_the_shared_fields(self):
        s = self.search[900001]
        for key in ("projectId", "title", "status", "program", "missionDirectorate", "startDate", "endDate",
                    "lastUpdated", "trlBegin", "trlCurrent", "trlEnd", "primaryTx", "additionalTx",
                    "destinations", "states", "description", "benefits", "url"):
            with self.subTest(key=key):
                self.assertEqual(s[key], self.detail[key])

    def test_values(self):
        p = self.search[900001]
        self.assertEqual(p["destinations"], ["Moon and Cislunar", "Mars"])      # detail spells Moon_and_Cislunar
        self.assertEqual(p["description"], "A cryogenic tank that keeps liquid hydrogen for months & more.")
        self.assertEqual(p["leadOrganization"]["setAside"],
                         ["Small Disadvantaged Business (SDB)", "Women-Owned Small Business (WOSB)"])
        self.assertEqual(self.detail["otherOrganizations"][0]["type"], "NASA Center")   # from NASA_Center
        self.assertEqual(p["contacts"], [{"name": "Jane Q Tester", "role": "Principal Investigator",
                                          "email": "jane@example.com", "orcid": None}])

    def test_trl_zero_means_not_set(self):
        self.assertIsNone(self.search[900002]["trlCurrent"])
        self.assertEqual(self.search[900002]["trlEnd"], 4)

    def test_files_links_and_closeout_documents(self):
        s = self.search[900001]
        self.assertEqual([f["fileId"] for f in s["files"]], [800001, 800002])   # library + closeout
        self.assertEqual(s["links"], [{"title": "News story", "type": "Link", "url": "https://www.example.com/story"}])
        self.assertEqual([(f["fileId"], f["bytes"]) for f in self.detail["files"]], [(800001, 1000), (800003, 500)])

    def test_merge_adds_what_only_search_has(self):
        merged = model.merge(self.detail, self.search[900001])
        self.assertEqual(merged["phase"], "2")
        self.assertEqual(sorted(f["fileId"] for f in merged["files"]), [800001, 800002, 800003])
        self.assertEqual(merged["files"][0]["bytes"], 1000)                     # detail's copy kept
        self.assertEqual(len(merged["links"]), 1)
        self.assertEqual(merged["leadOrganization"]["setAside"][0], "Small Disadvantaged Business (SDB)")
        self.assertIs(model.merge(self.detail, None), self.detail)

    def test_without_contact_details(self):
        slim = model.without_contact_details(self.search[900001])
        self.assertEqual(slim["contacts"], [{"name": "Jane Q Tester", "role": "Principal Investigator"}])
        self.assertEqual(slim["programContacts"], [{"name": "Pat M Manager", "role": "Program Director"}])

    def test_outcomes(self):
        o = self.search[900002]["outcomes"][0]
        self.assertEqual((o["path"], o["relatedProjectId"], o["date"]), ("Advanced To another project", 900001, "2021-01-14"))


if __name__ == "__main__":
    unittest.main()
