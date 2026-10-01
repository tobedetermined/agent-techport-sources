"""Requests, records and filing text for SEC EDGAR: the rules found in the
probe of 2026-09-30 ("SEC EDGAR connector" in docs/design.md). No network."""

import json
import os
import re
import unittest

from tests import FIXTURES
from servers.edgar import model

with open(os.path.join(FIXTURES, "edgar.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
with open(os.path.join(FIXTURES, "edgar_filing.html"), encoding="utf-8") as fh:
    FILING = fh.read()
TICKERS = list(FX["tickers"].values())


class ContactTest(unittest.TestCase):
    def test_a_name_and_email(self):
        self.assertEqual(model.contact("  Jane Doe  jane@example.com "), "Jane Doe jane@example.com")
        self.assertEqual(model.user_agent("Jane Doe jane@example.com"),
                         f"{model.USER_AGENT} Jane Doe jane@example.com")

    def test_no_contact(self):
        # Unset, empty, no email, a setting Claude Code didn't fill in, or an attempt
        # to add a header line: none is a contact.
        for value in (None, "", "Jane Doe", "${user_config.sec_contact}", "jane@example.com\r\nX-Other: 1"):
            self.assertIsNone(model.contact(value), value)


class CompanyTest(unittest.TestCase):
    def test_tickers_and_ids(self):
        self.assertEqual(model.from_tickers(TICKERS, "exrk"), [{"cik": 9000001, "name": "Example Rocket Corp", "ticker": "EXRK"}])
        self.assertEqual([c["cik"] for c in model.from_tickers(TICKERS, "Example Rocket")], [9000001, 9000003])
        self.assertEqual([c["cik"] for c in model.from_tickers(TICKERS, "example rocket corp")], [9000001])   # an exact name
        self.assertEqual(model.cik("0009000001"), 9000001)
        self.assertIsNone(model.cik("EXRK"))

    def test_suggestions_from_edgars_company_index(self):
        self.assertEqual(model.suggestions(FX["suggest"]), [
            {"cik": 9000004, "name": "EXAMPLE CRYO SYSTEMS INC", "ticker": None},
            {"cik": 9000001, "name": "EXAMPLE ROCKET CORP", "ticker": "EXRK"}])

    def test_profile(self):
        p = model.profile(FX["submissions"])
        self.assertEqual(p, {
            "cik": 9000001, "name": "Example Rocket Corp", "tickers": ["EXRK"], "exchanges": ["Nasdaq"],
            "industry": "3760 Guided Missiles & Space Vehicles & Parts", "category": "Large accelerated filer",
            "location": "EXAMPLE CITY, CA", "incorporated": "DE", "fiscal_year_end": "12-31", "website": None,
            "former_names": [{"name": "Example Acquisition Corp", "from": "2020-08-07", "to": "2021-08-30"}]})

    def test_filings_by_form_including_amendments(self):
        rows = model.filings(FX["submissions"], forms=["10-k"])
        self.assertEqual([r["form"] for r in rows], ["10-K", "10-K/A"])
        self.assertEqual(rows[0], {"form": "10-K", "filed": "2025-02-27", "period": "2024-12-31",
                                   "description": "10-K", "items": None, "accession": "0009000001-25-000010",
                                   "document": "exrk-10k2024.htm"})
        self.assertEqual(len(model.filings(FX["submissions"])), 4)
        self.assertEqual(model.filings(FX["submissions"], forms=["8-K"])[0]["items"], "7.01,8.01,9.01")
        self.assertEqual([r["form"] for r in model.filings(FX["submissions"], since="2025-08-01")], ["8-K", "10-Q"])

    def test_primary_document_of_a_recent_filing(self):
        self.assertEqual(model.primary_document(FX["submissions"], "0009000001-25-000010"), "exrk-10k2024.htm")
        self.assertIsNone(model.primary_document(FX["submissions"], "0009000001-20-000001"))


class SearchTest(unittest.TestCase):
    def test_parameters(self):
        self.assertEqual(model.search_params(' "SBIR Phase III" NASA ', forms=["10-k", "8-K"], year_from=2020,
                                             year_to=2024, cik=9000001, offset=100),
                         {"q": '"SBIR Phase III" NASA', "forms": "10-K,8-K", "dateRange": "custom",
                          "startdt": "2020-01-01", "enddt": "2024-12-31", "ciks": "0009000001", "from": 100})
        self.assertEqual(model.search_params("NASA"), {"q": "NASA"})
        self.assertEqual(model.search_params("NASA", year_from=2020)["enddt"][:2], "20")       # up to today
        with self.assertRaisesRegex(model.QueryError, "year_from"):
            model.search_params("NASA", year_from=2024, year_to=2020)
        with self.assertRaisesRegex(model.QueryError, "2001"):
            model.search_params("NASA", year_from=1995)

    def test_hits_as_rows(self):
        rows = model.hits(FX["search"])
        self.assertEqual(rows[0], {"company": "Example Rocket Corp", "cik": 9000001, "form": "10-K",
                                   "document_type": "10-K", "filed": "2025-02-27", "period": "2024-12-31",
                                   "description": "ANNUAL REPORT", "accession": "0009000001-25-000010",
                                   "document": "exrk-10k2024.htm"})
        self.assertEqual((rows[1]["document_type"], rows[1]["document"]), ("EX-13", "ex13.htm"))

    def test_counts(self):
        c = model.search_counts(FX["search"])
        self.assertEqual(c["by_company"], {"Example Rocket Corp (EXRK)": 1, "Example Lunar Inc.": 1})
        self.assertEqual((c["by_form"], c["by_state"]), ({"10-K": 2}, {"CA": 1, "TX": 1}))


class PassageTest(unittest.TestCase):
    def test_a_query_splits_into_phrases_and_words(self):
        self.assertEqual(model.query_terms('"SBIR Phase III" NASA'), ["SBIR Phase III", "NASA"])
        self.assertEqual(model.query_terms("cryogenic  fluid"), ["cryogenic", "fluid"])
        self.assertEqual(model.query_terms('"a b" "c d"'), ["a b", "c d"])

    def test_the_best_passage_shows_the_most_of_the_search(self):
        body = ("NASA is mentioned here first. " + "filler " * 120
                + "Later, NASA awarded us an SBIR Phase III contract. " + "filler " * 120 + "NASA again.")
        best = model.best_passage(body, '"SBIR Phase III" NASA')
        self.assertIn("SBIR Phase III", best["passage"])
        self.assertEqual(best["found"], 4)                       # three NASAs and the phrase
        self.assertIsNone(model.best_passage(body, '"Phase IV"'))

    def test_a_phrase_counts_for_more_than_a_common_word(self):
        body = "NASA and NASA contracts. " + "filler " * 120 + "Our SBIR Phase III work. " + "filler " * 120
        self.assertIn("SBIR Phase III", model.best_passage(body, '"SBIR Phase III" NASA')["passage"])


class FinancialsTest(unittest.TestCase):
    def test_years_merge_the_concepts_a_company_used(self):
        f = model.financials(FX["facts"])
        years = {r["year"]: r for r in f["years"]}
        self.assertEqual([r["year"] for r in f["years"]], [2016, 2017, 2018, 2019])
        self.assertEqual([years[y]["revenue"] for y in (2016, 2017, 2018, 2019)], [10000000, 20000000, 30000000, 40000000])
        # Which concept each year used: the fallbacks aren't always the same figure
        # (cash with restricted cash, profit with noncontrolling interests).
        self.assertEqual(f["concepts"]["revenue"], {"SalesRevenueNet": [2016, 2017],
                                                    "RevenueFromContractWithCustomerExcludingAssessedTax": [2018, 2019]})
        self.assertEqual(f["concepts"]["net_income"], {"NetIncomeLoss": [2019]})
        self.assertEqual(f["mixed"], ["revenue"])
        self.assertEqual((years[2019]["rd_expense"], years[2019]["net_income"]), (5000000, -7000000))
        self.assertEqual(years[2019]["total_assets"], 100000000)                 # the year-end value, not September's
        self.assertIsNone(years[2019]["operating_income"])                       # reported in EUR only
        self.assertEqual(f["not_usd"], ["OperatingIncomeLoss"])

    def test_every_year_row_has_every_measure(self):
        rows = model.financials(FX["facts"])["years"]
        self.assertEqual({tuple(r) for r in rows}, {("year",) + tuple(model.MEASURES)})

    def test_no_facts(self):
        self.assertEqual(model.financials({"cik": 1, "entityName": "X", "facts": {}})["years"], [])

    def test_a_framed_figure_from_a_proxy_gives_way_to_the_reports(self):
        # Virgin Galactic, 2026-10-01: SEC framed CY2021 on the proxy's pay-versus-performance
        # figure, in thousands, while three 10-Ks report the same period in dollars.
        def fact(val, form, filed, frame=None, start="2021-01-01", end="2021-12-31"):
            return {"val": val, "form": form, "filed": filed, "start": start, "end": end,
                    **({"frame": frame} if frame else {})}
        facts = {"facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": [
            fact(-352899000, "10-K", "2022-02-28"), fact(-352899000, "10-K", "2024-02-27"),
            fact(-352899, "DEF 14A", "2026-04-21", "CY2021"),
            fact(-278907, "DEF 14A", "2026-04-21", "CY2025", "2025-01-01", "2025-12-31"),
            fact(-644887000, "10-K/A", "2023-02-28", "CY2020", "2020-01-01", "2020-12-31")]}}}}}
        years = {r["year"]: r["net_income"] for r in model.financials(facts)["years"]}
        self.assertEqual(years, {2020: -644887000, 2021: -352899000})          # 2025: no report figure, so none


class FilingTextTest(unittest.TestCase):
    def test_inline_xbrl_becomes_text_with_page_marks(self):
        text = model.filing_text(FILING)
        self.assertEqual(text, "[page 1]\nEXAMPLE ROCKET CORP\nANNUAL REPORT 2024\n[page 2]\nItem 1. Business\n"
                               "We build launch vehicles. NASA awarded us an SBIR Phase III contract in 2023.\n"
                               "Revenues | $ | 601,799\nNet loss | (198,209 | )\n[page 3]\nItem 1A. Risk Factors\n"
                               "We depend on NASA & other government customers.")

    def test_hidden_parts_are_left_out(self):
        text = model.filing_text(FILING)
        self.assertNotIn("HIDDEN", text)
        self.assertNotIn("0009000001", text)
        self.assertNotIn("notText", text)

    def test_links_and_numbers(self):
        self.assertEqual(model.archive_url(9000001, "0009000001-25-000010", "exrk-10k2024.htm"),
                         "https://www.sec.gov/Archives/edgar/data/9000001/000900000125000010/exrk-10k2024.htm")
        self.assertEqual(model.accession(" 0009000001-25-000010 "), "0009000001-25-000010")
        self.assertEqual(model.accession("000900000125000010"), "0009000001-25-000010")
        for bad in ("10-K", "0009000001-25-0000101", "../x"):
            with self.assertRaisesRegex(model.QueryError, "accession"):
                model.accession(bad)
        with self.assertRaisesRegex(model.QueryError, "document"):
            model.document_name("../../etc/passwd")
        # HTML and text can be read; PDFs and images can't, without a dependency.
        self.assertEqual([model.readable(d) for d in ("a.htm", "B.HTML", "c.txt", "d.xml", "e.pdf", "f.jpg")],
                         [True, True, True, True, False, False])
        self.assertTrue(re.fullmatch(r"[\w.-]+", model.document_name("exrk-10k2024.htm")))


if __name__ == "__main__":
    unittest.main()
