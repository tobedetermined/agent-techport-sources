"""Request bodies and record shapes for USAspending: the rules found in the probe
of 2026-09-30 ("USAspending connector" in docs/design.md). No network."""

import json
import os
import unittest
from datetime import date

from tests import FIXTURES
from servers.usaspending import model

with open(os.path.join(FIXTURES, "usaspending.json"), encoding="utf-8") as fh:
    FX = json.load(fh)
AGENCIES = FX["toptier_agencies"]["results"]
TODAY = date(2026, 9, 30)


class FiltersTest(unittest.TestCase):
    def test_every_argument_maps_to_the_api_filter(self):
        f = model.filters(award_type="grants", agency="nasa", agencies=AGENCIES, company="Example Cryo",
                          keywords="lunar regolith", award_ids=[" TESTGRANT0001 "], fiscal_year_from=2020,
                          fiscal_year_to=2021, naics="541715", psc="ar12", cfda="43.012", state="oh", today=TODAY)
        self.assertEqual(f, {
            "time_period": [{"start_date": "2019-10-01", "end_date": "2021-09-30"}],
            "award_type_codes": ["02", "03", "04", "05", "F001", "F002"],
            "agencies": [{"type": "awarding", "tier": "toptier", "name": "National Aeronautics and Space Administration"}],
            "recipient_search_text": ["Example Cryo"],
            "keywords": ["lunar regolith"],
            "award_ids": ["TESTGRANT0001"],
            "naics_codes": ["541715"],
            "psc_codes": ["AR12"],
            "program_numbers": ["43.012"],
            "place_of_performance_locations": [{"country": "USA", "state": "OH"}],
        })

    def test_time_runs_from_fiscal_2008_to_today_by_default(self):
        self.assertEqual(model.filters(today=TODAY)["time_period"],
                         [{"start_date": "2007-10-01", "end_date": "2026-09-30"}])

    def test_signed_only_selects_awards_by_signing_date(self):
        f = model.filters(fiscal_year_from=2015, signed_only=True, today=TODAY)
        self.assertEqual(f["time_period"], [{"start_date": "2014-10-01", "end_date": "2026-09-30", "date_type": "date_signed"}])

    def test_search_cannot_reach_before_fiscal_2008(self):
        with self.assertRaisesRegex(model.QueryError, "2008"):
            model.filters(fiscal_year_from=2007, today=TODAY)

    def test_years_must_run_forward(self):
        with self.assertRaises(model.QueryError):
            model.filters(fiscal_year_from=2022, fiscal_year_to=2020, today=TODAY)

    def test_agency_by_full_name_any_case(self):
        f = model.filters(agency="department of defense", agencies=AGENCIES, today=TODAY)
        self.assertEqual(f["agencies"][0]["name"], "Department of Defense")

    def test_unknown_values_are_refused_with_what_is_accepted(self):
        with self.assertRaisesRegex(model.QueryError, "NASA"):
            model.filters(agency="NASSA", agencies=AGENCIES, today=TODAY)
        with self.assertRaisesRegex(model.QueryError, "contracts"):
            model.filters(award_type="loans", today=TODAY)
        with self.assertRaises(model.QueryError):
            model.filters(keywords="ab", today=TODAY)                     # the API needs 3 characters
        with self.assertRaises(model.QueryError):
            model.filters(award_ids=[str(i) for i in range(101)], today=TODAY)


class SearchBodyTest(unittest.TestCase):
    def test_the_sort_field_is_always_among_the_fields(self):
        # The API answers 400 otherwise (probed).
        for award_type in model.AWARD_TYPES:
            for sort in model.SORTS:
                with self.subTest(award_type=award_type, sort=sort):
                    body = model.search_body({}, award_type, sort=sort, include_description=False, page=1)
                    self.assertIn(body["sort"], body["fields"])

    def test_sorts_and_pages(self):
        body = model.search_body({"x": 1}, "contracts", sort="newest", include_description=False, page=3)
        self.assertEqual((body["sort"], body["order"], body["limit"], body["page"], body["filters"]),
                         ("Start Date", "desc", 100, 3, {"x": 1}))
        with self.assertRaises(model.QueryError):
            model.search_body({}, "contracts", sort="biggest", include_description=False, page=1)

    def test_description_only_on_request(self):
        plain = model.search_body({}, "contracts", sort="amount", include_description=False, page=1)
        self.assertNotIn("Description", plain["fields"])
        self.assertIn("Description", model.search_body({}, "contracts", sort="amount", include_description=True, page=1)["fields"])

    def test_extra_fields_for_internal_use(self):
        body = model.search_body({}, "contracts", sort="amount", include_description=False, page=1,
                                 extra_fields=("Base Obligation Date",))
        self.assertEqual(body["fields"][-1], "Base Obligation Date")

    def test_each_type_asks_for_the_fields_it_fills(self):
        # Probed: grants have no PSC, IDVs no end date.
        self.assertIn("CFDA Number", model.search_body({}, "grants", sort="amount", include_description=False, page=1)["fields"])
        idv = model.search_body({}, "idvs", sort="amount", include_description=False, page=1)["fields"]
        self.assertNotIn("End Date", idv)
        self.assertIn("Last Date to Order", idv)


class RowTest(unittest.TestCase):
    def test_rows_use_short_names_and_flat_codes(self):
        api_row = {"internal_id": 1, "generated_internal_id": "CONT_AWD_X", "Award ID": "X1", "Recipient Name": "EXAMPLE CO",
                   "Recipient UEI": "EXMPL0000001", "Award Amount": 10.0, "Start Date": "2024-01-01", "End Date": "2025-01-01",
                   "Awarding Agency": "National Aeronautics and Space Administration",
                   "PSC": {"code": "AR12", "description": "SPACE R&D"}}
        self.assertEqual(model.row(api_row, "contracts", include_description=False), {
            "id": "CONT_AWD_X", "award_id": "X1", "recipient": "EXAMPLE CO", "uei": "EXMPL0000001", "amount": 10.0,
            "start_date": "2024-01-01", "end_date": "2025-01-01",
            "awarding_agency": "National Aeronautics and Space Administration", "psc": "AR12"})

    def test_descriptions_are_cut_to_300_characters_and_marked(self):
        api_row = {"Description": "x" * 400, "PSC": None}
        description = model.row(api_row, "contracts", include_description=True)["description"]
        self.assertEqual(len(description), 301)
        self.assertTrue(description.endswith("…"))
        self.assertEqual(model.row({"Description": "short", "PSC": None}, "contracts", include_description=True)["description"],
                         "short")


class PhaseThreeTest(unittest.TestCase):
    def test_only_descriptions_that_say_sbir_or_sttr_phase_iii(self):
        kept = model.phase_iii([{"Description": d} for d in (
            "FY22 SBIR PHASE III - DESIGN AND TEST", "STTR Phase-III follow-on", "SBIR PHASE II - EXAMPLE",
            "PHASE III OF THE STATION PROGRAM", None)])
        self.assertEqual([r["Description"] for r in kept], ["FY22 SBIR PHASE III - DESIGN AND TEST", "STTR Phase-III follow-on"])


class WholeAwardTotalsTest(unittest.TestCase):
    ROWS = [
        {"Recipient UEI": "EXMPLAAA0001", "Recipient Name": "EXAMPLE A", "Award Amount": 500.0,
         "Awarding Agency": "National Aeronautics and Space Administration", "Base Obligation Date": "2014-10-01"},
        {"Recipient UEI": "EXMPLBBB0002", "Recipient Name": "EXAMPLE B", "Award Amount": 700.0,
         "Awarding Agency": "National Aeronautics and Space Administration", "Base Obligation Date": "2015-03-01"},
        {"Recipient UEI": "EXMPLAAA0001", "Recipient Name": "EXAMPLE A INC", "Award Amount": 300.0,
         "Awarding Agency": "Department of Defense", "Base Obligation Date": "2014-09-30"},
        {"Recipient UEI": None, "Recipient Name": "NO UEI CO", "Award Amount": None,
         "Awarding Agency": "Department of Defense", "Base Obligation Date": None},
    ]

    def test_by_company_largest_first(self):
        # A: 500 + 300 = 800 over two awards; B: 700; the row without a UEI goes by its name.
        self.assertEqual(model.whole_award_totals(self.ROWS, "recipient"), [
            {"uei": "EXMPLAAA0001", "name": "EXAMPLE A", "awards": 2, "obligated": 800.0},
            {"uei": "EXMPLBBB0002", "name": "EXAMPLE B", "awards": 1, "obligated": 700.0},
            {"uei": None, "name": "NO UEI CO", "awards": 1, "obligated": 0.0}])

    def test_by_agency(self):
        self.assertEqual(model.whole_award_totals(self.ROWS, "awarding_agency"), [
            {"agency": "National Aeronautics and Space Administration", "awards": 2, "obligated": 1200.0},
            {"agency": "Department of Defense", "awards": 2, "obligated": 300.0}])

    def test_by_fiscal_year_signed(self):
        # A fiscal year starts on 1 October: 2014-10-01 is FY2015, 2014-09-30 is FY2014.
        self.assertEqual(model.whole_award_totals(self.ROWS, "fiscal_year"), [
            {"fiscal_year": 2014, "awards": 1, "obligated": 300.0},
            {"fiscal_year": 2015, "awards": 2, "obligated": 1200.0},
            {"fiscal_year": None, "awards": 1, "obligated": 0.0}])


class AwardTest(unittest.TestCase):
    def test_contract_in_full_without_executive_pay(self):
        a = model.award(FX["award_contract"])
        self.assertEqual((a["id"], a["award_id"], a["category"]),
                         ("CONT_AWD_TESTNASA0001_8000_-NONE-_-NONE-", "TESTNASA0001", "contract"))
        self.assertEqual(a["amounts"], {"obligated": 1250000.0, "base_and_all_options": 2000000.0,
                                        "base_exercised_options": 1250000.0, "outlays": 900000.0,
                                        "subawards": 300000.0})
        self.assertEqual(a["recipient"]["parent_uei"], "EXMPLHLD0001")
        self.assertEqual((a["awarding"]["office"], a["funding"]["office"]),
                         ("NASA SHARED SERVICES CENTER", "EXAMPLE SPACE FLIGHT CENTER"))
        self.assertEqual((a["naics"]["code"], a["psc"]["code"]), ("541715", "AR12"))
        self.assertEqual(a["set_aside"], "SMALL BUSINESS SET ASIDE - TOTAL")
        self.assertNotIn("TEST EXECUTIVE ONE", json.dumps(a))

    def test_grant_in_full(self):
        a = model.award(FX["award_grant"])
        self.assertEqual((a["award_id"], a["category"]), ("TESTGRANT0001", "grant"))
        self.assertEqual(a["amounts"]["non_federal_funding"], 50000.0)
        self.assertEqual(a["cfda"], [{"code": "43.012", "title": "Space Technology"}])



class KeywordAlternativesTest(unittest.TestCase):
    def test_bar_and_or_become_the_apis_list(self):
        self.assertEqual(model.filters(keywords="parabolic | suborbital", today=TODAY)["keywords"],
                         ["parabolic", "suborbital"])
        self.assertEqual(model.filters(keywords="lunar regolith", today=TODAY)["keywords"], ["lunar regolith"])
        for bad in ("ab", "regolith -Mars", "(a | b)"):
            with self.subTest(bad=bad), self.assertRaises(model.QueryError):
                model.filters(keywords=bad, today=TODAY)


if __name__ == "__main__":
    unittest.main()
