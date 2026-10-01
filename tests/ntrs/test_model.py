"""Request bodies, record shapes and text handling for NTRS: the rules found in
the probe of 2026-09-30 ("NTRS connector" in docs/design.md). No network."""

import json
import os
import re
import unittest

from tests import FIXTURES
from servers.ntrs import model

with open(os.path.join(FIXTURES, "ntrs.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
TM, PAPER, ARTICLE, LEGACY = FX["search"]["results"]
AGGS = FX["search"]["aggregations"]


class SearchBodyTest(unittest.TestCase):
    def test_every_argument_maps_to_the_api_field(self):
        body = model.search_body(
            query=' "cryogenic fluid" | slosh ', center="grc", report_type="technical memorandum",
            subject="Propellants And Fuels", author=" Example, Ada B. ", organization="Glenn Research Center",
            keyword="cryogenic", report_number="E-00001", funding_number="EXAMPLE00C0001", year_from=2020,
            year_to=2024, has_files=True, sort="newest", offset=40, limit=20)
        self.assertEqual(body, {
            "q": '"cryogenic fluid" | slosh',
            "center": ["GRC"],
            "stiType": "TECHNICAL_MEMORANDUM",
            "subjectCategory": ["Propellants And Fuels"],
            "author": ["Example, Ada B."],
            "organization": ["Glenn Research Center"],
            "keyword": ["cryogenic"],
            "reportNumber": ["E-00001"],
            "fundingNumber": ["EXAMPLE00C0001"],
            "published": {"gte": "2020-01-01", "lt": "2025-01-01"},
            "disseminated": "DOCUMENT_AND_METADATA",
            "sort": {"field": "published", "order": "desc"},
            "page": {"size": 20, "from": 40},
        })

    def test_relevance_is_the_apis_default_order(self):
        self.assertEqual(model.search_body(query="  "), {"page": {"size": 20, "from": 0}})
        self.assertEqual(model.search_body(sort="oldest")["sort"], {"field": "published", "order": "asc"})
        with self.assertRaisesRegex(model.QueryError, "relevance, newest, oldest"):
            model.search_body(sort="amount")

    def test_center_codes_in_any_case_become_the_apis_own(self):
        self.assertEqual(model.search_body(center="larc")["center"], ["LaRC"])
        self.assertEqual(model.search_body(center=" cdms ")["center"], ["CDMS"])
        with self.assertRaisesRegex(model.QueryError, "GRC"):
            model.search_body(center="Glenn")

    def test_report_types_in_any_case_with_spaces_or_hyphens(self):
        self.assertEqual(model.search_body(report_type="conference paper")["stiType"], "CONFERENCE_PAPER")
        self.assertEqual(model.search_body(report_type="Technical-Memorandum")["stiType"], "TECHNICAL_MEMORANDUM")
        self.assertEqual(model.search_body(report_type="abstract")["stiType"], "ABSTRACT")    # in the data, not the spec
        with self.assertRaisesRegex(model.QueryError, "TECHNICAL_MEMORANDUM"):
            model.search_body(report_type="memo")

    def test_open_ended_years(self):
        self.assertEqual(model.search_body(year_from=2020)["published"], {"gte": "2020-01-01"})
        self.assertEqual(model.search_body(year_to=2024)["published"], {"lt": "2025-01-01"})
        with self.assertRaisesRegex(model.QueryError, "year_from"):
            model.search_body(year_from=2025, year_to=2020)

    def test_exact_values_keep_their_case(self):
        # The API matches these exactly and case-sensitively; changing case would find nothing.
        self.assertEqual(model.search_body(subject="propellants and fuels")["subjectCategory"],
                         ["propellants and fuels"])

    def test_paging_stops_at_10000(self):
        self.assertEqual(model.search_body(offset=9990, limit=10)["page"], {"size": 10, "from": 9990})
        with self.assertRaisesRegex(model.QueryError, "10,000"):
            model.search_body(offset=9995, limit=10)
        with self.assertRaisesRegex(model.QueryError, "offset"):
            model.search_body(offset=-1)

    def test_counts_only(self):
        self.assertEqual(model.search_body(query="cryogenic", limit=0)["page"], {"size": 0, "from": 0})


class RowTest(unittest.TestCase):
    def test_a_search_row(self):
        self.assertEqual(model.row(TM), {
            "id": "20260000001", "title": "Example Cryogenic Tank Insulation Test Results",
            "type": "TECHNICAL_MEMORANDUM", "published": "2026-01-01", "center": "GRC",
            "authors": "Ada Example; Ben Example; Cy Example (+1 more)", "files": 2})

    def test_files_are_counted_from_the_list_not_the_flag(self):
        self.assertTrue(LEGACY["downloadsAvailable"])
        self.assertEqual(model.row(LEGACY)["files"], 0)
        self.assertEqual(model.row(ARTICLE)["files"], 0)

    def test_undated_and_journal_records(self):
        self.assertIsNone(model.row(PAPER)["published"])
        self.assertEqual(model.row(ARTICLE)["id"], "31234567890123")       # a string in the API
        self.assertIsNone(model.row(ARTICLE)["center"])
        self.assertEqual(model.row(PAPER)["authors"], "Example, Ada B.")

    def test_the_abstract_option_keeps_the_first_300_characters(self):
        long = model.row(TM, abstract=True)["abstract"]
        self.assertEqual(len(long), 301)
        self.assertTrue(long.endswith("…"))
        self.assertEqual(model.row(PAPER, abstract=True)["abstract"], PAPER["abstract"])

    def test_rows_share_their_fields(self):
        for abstract in (False, True):
            fields = {tuple(model.row(r, abstract=abstract)) for r in (TM, PAPER, ARTICLE, LEGACY)}
            self.assertEqual(len(fields), 1)


class RecordTest(unittest.TestCase):
    def test_one_record_in_full(self):
        r = model.record(TM)
        self.assertEqual(r["id"], "20260000001")
        self.assertEqual(r["type"], "TECHNICAL_MEMORANDUM")
        self.assertEqual(r["type_detail"], "Technical Memorandum (TM)")
        self.assertEqual(r["published"], "2026-01-01")
        self.assertEqual(r["abstract"], TM["abstract"])
        self.assertEqual(r["authors"][0], {"name": "Ada Example", "organization": "Glenn Research Center"})
        self.assertEqual([a["name"] for a in r["authors"]], ["Ada Example", "Ben Example", "Cy Example", "Di Example"])
        self.assertNotIn("authors_total", r)
        self.assertEqual(r["center"], {"code": "GRC", "name": "Glenn Research Center"})
        self.assertEqual(r["subjects"], ["Propellants And Fuels"])
        self.assertEqual(r["keywords"], ["cryogenic", "insulation"])
        self.assertEqual(r["funding_numbers"], [{"number": "000000.01.02.03", "type": "WBS"},
                                                {"number": "EXAMPLE00C0001", "type": "CONTRACT_GRANT"}])
        self.assertEqual(r["report_numbers"], ["E-00001", "NASA/TM-20260000001"])
        self.assertEqual(r["publications"], [{"publisher": "National Aeronautics and Space Administration",
                                              "date": "2026-01-01", "doi": "10.0000/EXAMPLE0001"}])
        self.assertEqual(r["related"], [{"id": "20190000009", "title": "Example Earlier Insulation Study",
                                         "relation": "SEE_ALSO"}])
        self.assertEqual(r["page"], "https://ntrs.nasa.gov/citations/20260000001")

    def test_meetings_and_empty_lists(self):
        r = model.record(PAPER)
        self.assertEqual(r["meetings"], [{"name": "Example Space Conference 2025", "location": "Example City, AL",
                                          "start": "2025-07-22", "end": "2025-07-24"}])
        self.assertEqual((r["keywords"], r["funding_numbers"], r["report_numbers"], r["publications"]),
                         ([], [], [], []))

    def test_a_journal_article(self):
        r = model.record(ARTICLE)
        self.assertIsNone(r["center"])
        self.assertEqual(r["publications"], [{"name": "Journal of Example Fluids", "publisher": "Example Publishing",
                                              "volume": "29", "issue": "1", "date": "2017-11-01",
                                              "doi": "10.0000/example.0002"}])

    def test_long_author_lists_are_cut(self):
        many = dict(TM, authorAffiliations=[
            {"sequence": i, "meta": {"author": {"name": f"Author {i}"}, "organization": {"name": "Example Lab"}}}
            for i in range(1186)])
        r = model.record(many)
        self.assertEqual(len(r["authors"]), model.MAX_AUTHORS)
        self.assertEqual(r["authors_total"], 1186)
        self.assertEqual(r["authors"][0]["name"], "Author 0")


class FilesTest(unittest.TestCase):
    def test_the_report_comes_before_its_abstract(self):
        files = model.files(TM)
        self.assertEqual([(f["file"], f["name"], f["type"]) for f in files],
                         [(1, "20260000001.pdf", "STI"), (2, "Example abstract.docx", "ABSTRACT")])
        self.assertEqual(files[0]["pdf"], "https://ntrs.nasa.gov/api/citations/20260000001/downloads/20260000001.pdf")
        self.assertEqual(files[0]["text"], "https://ntrs.nasa.gov/api/citations/20260000001/downloads/20260000001.txt")
        self.assertEqual(files[1]["format"], "docx")

    def test_a_word_file_without_a_pdf(self):
        [f] = model.files(PAPER)
        self.assertIsNone(f["pdf"])
        self.assertTrue(f["text"].endswith("Example%20Paper.docx.txt"))
        self.assertEqual(model.files(ARTICLE), [])


class CountsTest(unittest.TestCase):
    def test_years_in_order_with_the_undated_count(self):
        c = model.counts(AGGS, "year", total=1314)
        self.assertEqual(c["groups"], [{"year": 1966, "records": 40}, {"year": 2015, "records": 72},
                                       {"year": 2024, "records": 30}])
        self.assertEqual(c["undated"], 1314 - 142)
        self.assertNotIn("rest", c)

    def test_top_groups_and_the_rest(self):
        c = model.counts(AGGS, "author", total=1314)
        self.assertEqual(c["groups"], [{"author": "Ada Example", "records": 31}, {"author": "Example, Ada B.", "records": 7}])
        self.assertEqual(c["rest"], 2000)
        self.assertNotIn("undated", c)

    def test_centers_with_their_names(self):
        c = model.counts(AGGS, "center", total=1314)
        self.assertEqual(c["groups"][1], {"center": "MSFC", "name": "Marshall Space Flight Center", "records": 340})
        self.assertIn("without a NASA center", c["groups"][0]["name"])
        self.assertNotIn("rest", c)                      # centers come back complete

    def test_unknown_group(self):
        with self.assertRaisesRegex(model.QueryError, "report_type"):
            model.counts(AGGS, "decade", total=1)

    def test_every_group_maps_to_an_aggregation(self):
        for group in model.GROUPS:
            self.assertTrue(model.counts(AGGS, group, total=1314)["groups"], group)


class TextTest(unittest.TestCase):
    def test_extracted_xhtml_becomes_text_with_page_markers(self):
        text = model.clean_text(FX["fulltext_xhtml"])
        self.assertNotIn("<", text.replace("<0.2", ""))
        self.assertIn("Results & discussion", text)
        self.assertIn("<0.2 as required", text)
        self.assertNotIn("Example head title", text)                   # the <head> is left out
        self.assertEqual(re.findall(r"\[page (\d+)\]", text), ["1", "2"])
        self.assertIn("- Heat leak below prediction", text)
        self.assertNotRegex(text, r"\n\n\n|[ \t]\n")

    def test_plain_text_is_tidied(self):
        text = model.clean_text(FX["fulltext_plain"])
        self.assertTrue(text.startswith("Example Report Cover\n"))
        self.assertNotRegex(text, r"\n\n\n|[ \t]\n")
        self.assertNotIn("[page", text)

    def test_parts_end_at_a_line_break_within_the_size(self):
        para = ("x" * 99 + "\n") * 50                 # 5,000 characters, lines of 100
        text = para * 3
        spans = model.split(text, size=12_000)
        self.assertEqual("".join(text[a:b] for a, b in spans), text)
        self.assertTrue(all(b - a <= 12_000 for a, b in spans))
        self.assertTrue(all(text[b - 1] == "\n" for a, b in spans[:-1]))
        self.assertEqual(len(spans), 2)

    def test_a_line_longer_than_a_part_is_cut(self):
        spans = model.split("y" * 30_000, size=12_000)
        self.assertEqual([b - a for a, b in spans], [12_000, 12_000, 6_000])

    def test_page_at_a_position(self):
        text = model.clean_text(FX["fulltext_xhtml"])
        self.assertEqual(model.page_at(text, text.index("Results &")), 2)
        self.assertEqual(model.page_at(text, 0), 1)
        self.assertIsNone(model.page_at(model.clean_text(FX["fulltext_plain"]), 5))

    def test_passages_match_any_case_across_line_breaks(self):
        text = model.clean_text(FX["fulltext_xhtml"])
        [found] = model.passages(text, "BOIL-OFF | heat leak", spans=model.split(text))   # close together: one passage
        self.assertEqual((found["part"], found["page"], found["matches"]), (1, 2, 2))
        self.assertIn("boil-off was 0.1 percent", found["text"])
        self.assertIn("Heat leak below", found["text"])
        self.assertNotIn("\n", found["text"])
        across = model.passages("the measured\nboil-off  rate", "measured boil-off", spans=[(0, 28)])
        self.assertEqual(len(across), 1)
        self.assertIsNone(across[0]["page"])

    def test_passages_near_each_other_merge(self):
        text = "alpha " * 10 + "target one " + "beta " * 5 + "target two " + "gamma " * 200
        found = model.passages(text, "target", spans=model.split(text))
        self.assertEqual(len(found), 1)
        self.assertIn("target one", found[0]["text"])
        self.assertIn("target two", found[0]["text"])
        self.assertTrue(found[0]["text"].endswith("…"))

    def test_passages_match_whole_words_or_a_prefix(self):
        text = "See Figure 1. Later, Figure 10 and Figure 12 show cryogenic data." + " filler" * 200
        found = model.passages(text, "Figure 1", spans=model.split(text), width=5)
        self.assertEqual(sum(p["matches"] for p in found), 1)               # not Figure 10 or 12
        found = model.passages(text, "cryogen", spans=model.split(text))
        self.assertEqual(found, [])
        found = model.passages(text, "cryogen*", spans=model.split(text))
        self.assertEqual(sum(p["matches"] for p in found), 1)

    def test_no_words_no_passages(self):
        with self.assertRaises(model.QueryError):
            model.passages("text", " | ", spans=[(0, 4)])
        with self.assertRaises(model.QueryError):
            model.passages("text", "*", spans=[(0, 4)])


if __name__ == "__main__":
    unittest.main()
