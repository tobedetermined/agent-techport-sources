import os
import shutil
import sqlite3
import tempfile
import unittest

from tests import FIXTURES
from servers.sbir import queries, store

SAMPLE = os.path.join(FIXTURES, "sbir_sample.csv")


class QueriesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        path = os.path.join(cls.tmp, "sbir.db")
        store.load_csv(SAMPLE, path)
        cls.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp)

    # search

    def test_search_by_agency_accepts_code_or_name(self):
        for agency in ("NASA", "nasa", "National Aeronautics and Space Administration"):
            with self.subTest(agency=agency):
                self.assertEqual(queries.search_awards(self.db, agency=agency)["total"], 3)

    def test_search_filters_combine(self):
        r = queries.search_awards(self.db, agency="NASA", program="STTR", phase=2, year_from=2024)
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["awards"][0]["award_year"], 2024)

    def test_phase_accepts_roman_and_words(self):
        for phase in (2, "2", "II", "Phase II"):
            with self.subTest(phase=phase):
                self.assertEqual(queries.search_awards(self.db, phase=phase)["total"], 2)

    def test_state_accepts_code_or_name(self):
        self.assertEqual(queries.search_awards(self.db, state="GU")["total"], 2)
        self.assertEqual(queries.search_awards(self.db, state="guam")["total"], 2)

    def test_company_is_case_insensitive_substring_and_literal(self):
        self.assertEqual(queries.search_awards(self.db, company="orbital")["total"], 2)
        self.assertEqual(queries.search_awards(self.db, company="%")["total"], 0)

    def test_keywords_with_punctuation_and_phrases(self):
        self.assertEqual(queries.search_awards(self.db, keywords="zero-boil-off")["total"], 1)
        self.assertEqual(queries.search_awards(self.db, keywords="excavator lunar")["total"], 2)
        self.assertEqual(queries.search_awards(self.db, keywords='"excavator lunar"')["total"], 0)
        self.assertEqual(queries.search_awards(self.db, keywords='"lunar regolith"')["total"], 2)
        self.assertEqual(queries.search_awards(self.db, keywords="regolith cryogenic")["total"], 0)
        self.assertEqual(queries.search_awards(self.db, keywords="regolith OR cryogenic")["total"], 3)

    def test_search_leaves_out_contacts_and_abstract_by_default(self):
        award = queries.search_awards(self.db, agency="NASA")["awards"][0]
        self.assertIn("pi_name", award)
        for hidden in ("pi_email", "pi_phone", "contact_email", "abstract"):
            self.assertNotIn(hidden, award)
        with_abstract = queries.search_awards(self.db, agency="NASA", include_abstract=True)["awards"][0]
        self.assertIn("abstract", with_abstract)

    def test_paging(self):
        page = queries.search_awards(self.db, limit=2, offset=1)
        self.assertEqual((page["total"], page["returned"], page["offset"]), (5, 2, 1))
        capped = queries.search_awards(self.db, limit=1000)
        self.assertEqual(capped["returned"], 5)
        self.assertIn("capped at 50", capped["note"])
        self.assertIn("capped at 10", queries.search_awards(self.db, limit=11, include_abstract=True)["note"])
        self.assertNotIn("note", queries.search_awards(self.db, limit=10, include_abstract=True))

    def test_sort(self):
        amounts = [a["award_amount"] for a in queries.search_awards(self.db, sort="amount")["awards"]]
        self.assertEqual(amounts, [750000.0, 250000.0, 150000.0, 100000.0, None])   # no amount last
        years = [a["award_year"] for a in queries.search_awards(self.db, sort="oldest")["awards"]]
        self.assertEqual(years, sorted(years))
        years = [a["award_year"] for a in queries.search_awards(self.db)["awards"]]
        self.assertEqual(years, sorted(years, reverse=True))                         # newest by default
        top = queries.search_awards(self.db, keywords="regolith", sort="amount")["awards"][0]
        self.assertEqual(top["award_amount"], 750000.0)
        with self.assertRaises(queries.QueryError):
            queries.search_awards(self.db, sort="relevance")                         # needs keywords
        with self.assertRaises(queries.QueryError):
            queries.search_awards(self.db, sort="biggest")

    def test_keywords_combine_with_filters(self):
        self.assertEqual(queries.search_awards(self.db, keywords="regolith", year_from=2024)["total"], 1)
        self.assertEqual(queries.search_awards(self.db, keywords="regolith", agency="DOD")["total"], 0)

    def test_bad_filter_values_explain_themselves(self):
        with self.assertRaisesRegex(queries.QueryError, "NASA"):
            queries.search_awards(self.db, agency="NASSA")
        with self.assertRaises(queries.QueryError):
            queries.search_awards(self.db, phase=3)

    # get

    def test_get_award_returns_every_matching_row(self):
        r = queries.get_award(self.db, agency_tracking_number="T4.02-2002", contract="TEST-NASA-0002")
        self.assertEqual((r["matches"], r["returned"]), (2, 2))
        self.assertIn("Phase I and a Phase II", r["note"])
        self.assertNotIn("more than one agency", r["note"])

    def test_get_award_caps_rows_and_filters_by_agency(self):
        original = queries.MAX_GET_ROWS
        queries.MAX_GET_ROWS = 1
        try:
            r = queries.get_award(self.db, contract="TEST-NASA-0002")
        finally:
            queries.MAX_GET_ROWS = original
        self.assertEqual((r["matches"], r["returned"]), (2, 1))
        self.assertIn("first 1 of 2", r["note"])
        self.assertEqual(queries.get_award(self.db, contract="TEST-NASA-0002", agency="DOD")["matches"], 0)

    def test_get_award_trims_input_and_hides_contacts(self):
        r = queries.get_award(self.db, contract=" TEST-NASA-0001 ")
        self.assertEqual(r["matches"], 1)
        award = r["awards"][0]
        self.assertIn("abstract", award)
        self.assertNotIn("pi_email", award)
        with_contacts = queries.get_award(self.db, id=award["id"], include_contacts=True)["awards"][0]
        self.assertEqual(with_contacts["pi_email"], "jane@example.com")

    def test_get_award_needs_a_key(self):
        with self.assertRaises(queries.QueryError):
            queries.get_award(self.db)

    # company

    def test_company_profile(self):
        r = queries.company(self.db, uei="exmplorb0002")
        self.assertTrue(r["found"])
        self.assertEqual(r["names"], ["Example Orbital LLC"])
        self.assertEqual((r["awards"], r["amount_total"]), (2, 1000000.0))
        self.assertEqual([y["value"] for y in r["by_year"]], [2023, 2024])
        self.assertEqual(r["profile"]["as_of_award_year"], 2024)
        self.assertNotIn("pi_email", r["profile"])

    def test_unknown_company(self):
        self.assertFalse(queries.company(self.db, uei="NOPE")["found"])

    # aggregate

    def test_aggregate_totals_and_missing_amounts(self):
        r = queries.aggregate(self.db, group_by="agency")
        by = {g["agency"]: g for g in r["groups"]}
        self.assertEqual(by["NASA"]["awards"], 3)
        self.assertEqual(by["NASA"]["amount_total"], 1150000.0)
        self.assertEqual(by["DOD"]["awards_without_amount"], 1)
        self.assertEqual(r["totals"]["awards"], 5)

    def test_company_ranking(self):
        r = queries.aggregate(self.db, group_by="company")
        self.assertEqual([g["uei"] for g in r["groups"]], ["EXMPLORB0002", "EXMPLCRY0001", "EXMPLDEF0003"])
        self.assertEqual(r["groups"][0]["company"], "Example Orbital LLC")
        self.assertEqual((r["groups"][0]["awards"], r["companies_total"]), (2, 3))
        self.assertEqual(r["awards_without_uei"], 1)                  # Example Bio has no UEI
        self.assertIn("no UEI", r["note"])
        self.assertEqual(r["totals"]["awards"], 5)                    # totals still include it

    def test_company_ranking_by_amount_filters_and_limit(self):
        r = queries.aggregate(self.db, group_by="company", sort="amount")
        self.assertEqual([g["amount_total"] for g in r["groups"]], [1000000.0, 150000.0, None])
        r = queries.aggregate(self.db, group_by="company", agency="NASA", limit=1)
        self.assertEqual([g["uei"] for g in r["groups"]], ["EXMPLORB0002"])
        self.assertEqual((r["companies_total"], r["awards_without_uei"]), (2, 0))
        self.assertNotIn("note", r)
        self.assertIn("capped at 100", queries.aggregate(self.db, group_by="company", limit=500)["note"])

    def test_other_groupings_can_be_sorted_and_limited(self):
        r = queries.aggregate(self.db, group_by="agency", sort="awards", limit=1)
        self.assertEqual([(g["agency"], g["awards"]) for g in r["groups"]], [("NASA", 3)])
        with self.assertRaises(queries.QueryError):
            queries.aggregate(self.db, group_by="agency", sort="relevance")

    def test_aggregate_rejects_unknown_grouping(self):
        with self.assertRaises(queries.QueryError):
            queries.aggregate(self.db, group_by="city")

    # notice

    def test_data_notice_flags_stale(self):
        n = queries.data_notice({"source": "x"}, "stale: URLError: offline")
        self.assertIn("Could not refresh", n["caveats"][0])


@unittest.skipUnless(os.environ.get("SBIR_REAL_CSV"), "set SBIR_REAL_CSV to the downloaded award_data.csv")
class RealFileQueriesTest(unittest.TestCase):
    """The tools reproduce the numbers measured for docs/design.md."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        path = os.path.join(cls.tmp, "sbir.db")
        store.load_csv(os.environ["SBIR_REAL_CSV"], path)
        cls.db = sqlite3.connect(path)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp)

    def test_nasa_by_program_and_phase(self):
        r = queries.aggregate(self.db, group_by="phase", agency="NASA", program="SBIR")
        self.assertEqual({g["phase"]: g["awards"] for g in r["groups"]}, {1: 12567, 2: 5321})
        r = queries.aggregate(self.db, group_by="phase", agency="NASA", program="STTR")
        self.assertEqual({g["phase"]: g["awards"] for g in r["groups"]}, {1: 1114, 2: 483})

    def test_nasa_total_about_5_billion(self):
        total = queries.aggregate(self.db, group_by="agency", agency="NASA")["totals"]["amount_total"]
        self.assertAlmostEqual(total / 1e9, 5.0, delta=0.05)

    def test_largest_nasa_phase_2_awards_2023(self):
        r = queries.search_awards(self.db, agency="NASA", phase=2, year_from=2023, year_to=2023,
                                  sort="amount", limit=3)
        self.assertEqual(r["total"], 148)
        self.assertEqual([a["contract"] for a in r["awards"]],
                         ["80NSSC23CA213", "80NSSC23CA211", "80NSSC23CA214"])

    def test_top_nasa_companies_since_2015(self):
        r = queries.aggregate(self.db, group_by="company", agency="NASA", year_from=2015, limit=2)
        self.assertEqual([(g["company"], g["awards"]) for g in r["groups"]],
                         [("CREARE LLC", 77), ("CFD Research Corporation", 73)])
        self.assertEqual(r["awards_without_uei"], 0)                  # every NASA award since 2015 has a UEI

    def test_keyword_search_runs_on_full_data(self):
        r = queries.search_awards(self.db, agency="NASA", keywords="cryogenic propellant", limit=5)
        self.assertGreater(r["total"], 0)
        self.assertEqual(r["returned"], 5)


if __name__ == "__main__":
    unittest.main()
