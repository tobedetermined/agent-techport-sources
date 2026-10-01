"""Search results, records and patent pages for NASA Technology Transfer: the
rules found in the probe of 2026-09-30 ("NASA Technology Transfer connector" in
docs/design.md). No network."""

import json
import os
import unittest

from tests import FIXTURES
from servers.techtransfer import model

with open(os.path.join(FIXTURES, "techtransfer.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
with open(os.path.join(FIXTURES, "techtransfer_patent_page.html"), encoding="utf-8") as fh:
    PAGE = fh.read()


def recs(kind, word):
    return [model.record(kind, r) for r in FX[kind][word]]


class KindsAndWordsTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(model.kinds("all"), ("patent", "software", "spinoff"))
        self.assertEqual(model.kinds(" Patents "), ("patent",))
        self.assertEqual(model.kinds("spinoff"), ("spinoff",))
        with self.assertRaisesRegex(model.QueryError, "patent, software, spinoff"):
            model.kinds("licenses")

    def test_words_are_split_and_deduplicated(self):
        self.assertEqual(model.words(' cryogenic  "tank" LOX/LH2 Cryogenic KSC-TOPS-59 '),
                         (["cryogenic", "tank", "LOX", "LH2", "KSC-TOPS-59"], []))

    def test_common_words_are_left_out(self):
        # The API finds "and" in 87 patents where at least 606 have it: as one of
        # several required words, it would silently drop most matches.
        self.assertEqual(model.words("Thermal AND insulation for the tanks"),
                         (["Thermal", "insulation", "tanks"], ["AND", "for", "the"]))
        with self.assertRaisesRegex(model.QueryError, "common words"):
            model.words("the and of")

    def test_words_are_needed_and_limited(self):
        with self.assertRaisesRegex(model.QueryError, "can't list"):
            model.words(' "" ')
        with self.assertRaisesRegex(model.QueryError, "6"):
            model.words("one two three four five six seven")
        self.assertEqual(len(model.words("one two three four five six of the and")[0]), 6)


class RecordTest(unittest.TestCase):
    def test_a_patent(self):
        self.assertEqual(recs("patent", "cryogenic")[0], {
            "kind": "patent", "reference": "KSC-TOPS-901", "title": "Cryogenic Example Coating",
            "description": "Thermal coatings for cryogenic tanks reflect sunlight & keep propellant cold.",
            "category": "Materials and Coatings", "center": "KSC", "release": None, "how_to_get": None,
            "link": None, "image": "https://technology.nasa.gov/t2media/tops/img/KSC-TOPS-901/example.jpg",
            "tags": None, "score": 16.5})

    def test_software_and_spinoffs(self):
        sw = recs("software", "tank")
        self.assertEqual((sw[0]["release"], sw[0]["link"]), ("Open Source", "https://github.com/example/example-tank"))
        self.assertEqual(sw[0]["description"], "A MATLAB simulation of a cryogenic tank.")
        self.assertEqual(sw[1]["how_to_get"], "Request through the software catalog")
        self.assertIn("It's now sold", recs("spinoff", "cryogenic")[0]["description"])

    def test_center_codes_get_one_spelling(self):
        self.assertEqual(recs("patent", "cryogenic")[2]["center"], "LaRC")       # LARC
        self.assertEqual(recs("patent", "tank")[1]["center"], "AFRC")           # DFRC: Dryden, now Armstrong
        self.assertEqual(model.center("HDQS"), "HQ")
        self.assertEqual(model.center_code(" larc "), "LaRC")
        self.assertEqual(model.center_code("dfrc"), "AFRC")
        with self.assertRaisesRegex(model.QueryError, "GRC"):
            model.center_code("Glenn")


class CombineTest(unittest.TestCase):
    def found(self):
        return {"cryogenic": recs("patent", "cryogenic"), "tank": recs("patent", "tank")}

    def test_all_words_keeps_records_every_word_found(self):
        both = model.combine(self.found(), "all")
        self.assertEqual([(r["reference"], r["score"]) for r in both], [("LEW-TOPS-902", 21.0)])

    def test_any_word_is_the_union_by_total_score(self):
        either = model.combine(self.found(), "any")
        self.assertEqual([r["reference"] for r in either], ["LEW-TOPS-902", "KSC-TOPS-901", "TOP2-904", "LAR-TOPS-903"])

    def test_one_word_keeps_the_apis_order(self):
        self.assertEqual([r["reference"] for r in model.combine({"tank": recs("patent", "tank")}, "all")],
                         ["LEW-TOPS-902", "TOP2-904"])

    def test_unknown_match(self):
        with self.assertRaisesRegex(model.QueryError, "all, any"):
            model.combine(self.found(), "most")


class FilterAndCountTest(unittest.TestCase):
    def test_counts_by_kind_center_and_category(self):
        c = model.counts(recs("patent", "cryogenic") + recs("software", "cryogenic"))
        self.assertEqual(c["by_kind"], {"patent": 3, "software": 1})
        self.assertEqual(c["by_center"], {"KSC": 1, "GRC": 1, "LaRC": 1, "ARC": 1})
        # Categories that differ only in case are counted together, under the capitalised form.
        self.assertEqual(c["by_category"]["Materials and Coatings"], 2)
        self.assertNotIn("materials and coatings", c["by_category"])

    def test_categories_differing_in_punctuation_count_together(self):
        rs = [{"kind": "spinoff", "center": "KSC", "category": c} for c in (
            "Industrial Productivity/Manufacturing Technology", "industrial productivity manufacturing technology",
            "Industrial Productivity", "ip", None)]
        self.assertEqual(model.counts(rs)["by_category"],
                         {"Industrial Productivity/Manufacturing Technology": 2, "Industrial Productivity": 1,
                          "ip": 1, "(none)": 1})

    def test_filters(self):
        rs = recs("patent", "cryogenic")
        self.assertEqual([r["reference"] for r in model.keep(rs, center="larc")], ["LAR-TOPS-903"])
        self.assertEqual([r["reference"] for r in model.keep(rs, category="MATERIALS")],
                         ["KSC-TOPS-901", "LEW-TOPS-902"])
        self.assertEqual(model.keep(rs), rs)


class RowTest(unittest.TestCase):
    def test_rows(self):
        rec = recs("software", "tank")[0]
        self.assertEqual(model.row(rec), {"kind": "software", "reference": "ARC-90001-1",
                                          "title": "Simulation of Cryogenic Example Tank",
                                          "category": "design and integration tools", "center": "ARC"})
        self.assertEqual(model.row(rec, release=True)["release"], "Open Source")
        long = dict(rec, description="x" * 400)
        self.assertEqual(len(model.row(long, description=True)["description"]), 301)


class KindOfTest(unittest.TestCase):
    def test_the_kind_shows_in_the_number(self):
        for ref, kind in (("KSC-TOPS-59", "patent"), ("TOP2-279", "patent"), ("KSC-SO-111", "spinoff"),
                          ("LARC-SO-12", "spinoff"), ("ARC-17900-1", "software"), ("npo-12345-1", "software")):
            self.assertEqual(model.kind_of(ref), kind, ref)


class PageTest(unittest.TestCase):
    def test_a_patent_page(self):
        d = model.page_details(PAGE)
        self.assertEqual(d["subtitle"], "Keeping Propellant Cold with an Example Coating")
        self.assertEqual(d["technology"], "The coating scatters sunlight from the UV to the mid-IR. "
                                          "It's sprayed on or applied as tiles.")
        self.assertEqual(d["benefits"], ["Reflects nearly all sunlight", "Flexible and moisture resistant"])
        self.assertEqual(d["applications"], ["Propellant depots", "Lunar surface tanks"])
        self.assertEqual(d["case_numbers"], ["KSC-90001", "KSC-90002"])
        self.assertEqual([p["number"] for p in d["patents"]], ["99,000,001", "99,000,002"])
        self.assertTrue(d["patents"][0]["link"].startswith("https://ppubs.uspto.gov/"))
        self.assertEqual(d["papers"], ['"An Example Coating Paper", A. Example, B. Example, 2017.'])

    def test_similar_results_are_not_read(self):
        text = json.dumps(model.page_details(PAGE))
        self.assertNotIn("Decoy", text)
        self.assertNotIn("11,111,111", text)

    def test_empty_sections_and_pages_without_them(self):
        d = model.page_details(PAGE.replace("<li>Propellant depots</li><li>Lunar surface tanks</li>", ""))
        self.assertEqual(d["applications"], [])
        self.assertIsNone(model.page_details("<html><body><p>Page not found</p></body></html>"))


if __name__ == "__main__":
    unittest.main()
