import json
import os
import shutil
import tempfile
import unittest

from tests import FIXTURES
from servers.common import dbfiles
from servers.common.jsonstream import iter_array
from servers.techport import copy, model, queries

SEARCH = os.path.join(FIXTURES, "techport_search.json")


class JsonStreamTest(unittest.TestCase):
    def test_reads_items_across_chunk_boundaries(self):
        with open(SEARCH, encoding="utf-8") as f:
            want = [p["projectId"] for p in json.load(f)["results"]]
        for size in (7, 64, 1 << 20):
            with self.subTest(chunk_size=size):
                self.assertEqual([p["projectId"] for p in iter_array(SEARCH, "results", chunk_size=size)], want)

    def test_empty_array_and_missing_key(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "x.json")
            with open(path, "w") as f:
                f.write('{"results" :\n [ ] , "total": 0}')
            self.assertEqual(list(iter_array(path, "results", chunk_size=3)), [])
            with self.assertRaises(ValueError):
                list(iter_array(path, "projects"))
        finally:
            shutil.rmtree(tmp)


class CopyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.meta = copy.refresh(cls.tmp, http=None, json_override=SEARCH)
        cls.db, _ = copy.open_copy(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp)

    def test_meta_and_freshness(self):
        self.assertEqual(self.meta["projects"], "3")
        meta, fresh = copy.status(self.tmp)
        self.assertTrue(fresh)
        self.assertEqual(meta["projects"], "3")
        self.assertEqual(copy.status(os.path.join(self.tmp, "none")), (None, False))

    def test_old_copy_is_not_fresh(self):
        self.assertGreater(copy._age_seconds({"loaded_at": "2020-01-01T00:00:00+00:00"}), copy.MAX_AGE_SECONDS)

    def test_refresh_through_http_uses_download(self):
        class FakeHTTP:
            def download(self, url, dest):
                self.url = url
                shutil.copyfile(SEARCH, dest)
                return os.path.getsize(dest)
        http = FakeHTTP()
        tmp = tempfile.mkdtemp()
        try:
            meta = copy.refresh(tmp, http)
            self.assertEqual((http.url, meta["source"], meta["projects"]), (copy.SEARCH_URL, copy.SEARCH_URL, "3"))
            self.assertEqual([n for n in os.listdir(tmp) if n.endswith(".part")], [])
        finally:
            shutil.rmtree(tmp)

    def test_short_load_keeps_the_old_copy(self):
        before = dbfiles.current_path(self.tmp)
        with self.assertRaisesRegex(copy.ShortLoad, "previous copy is kept"):
            copy.refresh(self.tmp, http=None, json_override=SEARCH, expected=100)
        self.assertEqual(dbfiles.current_path(self.tmp), before)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".building")], [])

    def test_expected_defaults_to_the_current_copy(self):
        tmp = tempfile.mkdtemp()
        try:
            copy.refresh(tmp, http=None, json_override=SEARCH)
            with open(SEARCH, encoding="utf-8") as f:
                one = json.load(f)
            one["results"] = one["results"][:1]
            small = os.path.join(tmp, "small.json")
            with open(small, "w", encoding="utf-8") as f:
                json.dump(one, f)
            with self.assertRaises(copy.ShortLoad):          # 1 of 3 expected
                copy.refresh(tmp, http=None, json_override=small)
        finally:
            shutil.rmtree(tmp)

    # find

    def test_find_filters(self):
        self.assertEqual(queries.find(self.db)["total"], 3)
        self.assertEqual(queries.find(self.db, program="fo")["total"], 1)
        self.assertEqual(queries.find(self.db, program="Flight Opp")["total"], 1)       # part of the title
        self.assertEqual(queries.find(self.db, program="SBIR")["total"], 1)             # part of SBIR/STTR
        self.assertEqual(queries.find(self.db, program="64")["total"], 1)               # a programId
        self.assertEqual(queries.find(self.db, program=64)["program_filter"],
                         [{"programId": 64, "acronym": "FO", "title": "Flight Opportunities"}])
        self.assertEqual(queries.find(self.db, status="cancelled")["total"], 1)          # British spelling accepted
        self.assertEqual(queries.find(self.db, technology_area="TX14")["total"], 1)
        self.assertEqual(queries.find(self.db, technology_area="TX14.1.1")["total"], 1)
        self.assertEqual(queries.find(self.db, destination="moon")["total"], 2)
        self.assertEqual(queries.find(self.db, organization_type="NASA Center")["total"], 1)
        self.assertEqual(queries.find(self.db, trl_min=4)["total"], 2)                   # TRL not set excluded
        self.assertEqual(queries.find(self.db, start_year_from=2020, start_year_to=2023)["total"], 1)
        self.assertEqual(queries.find(self.db, outcome="advanced")["total"], 1)
        self.assertEqual(queries.find(self.db, query="cryogenic")["total"], 1)           # copy's own index

    def test_find_sort_and_page(self):
        r = queries.find(self.db, sort="oldest", limit=2)
        self.assertEqual(([p["projectId"] for p in r["projects"]], r["total"], r["returned"]), ([900002, 900003], 3, 2))
        self.assertEqual(queries.find(self.db)["projects"][0]["projectId"], 900001)     # newest by default
        self.assertEqual(queries.find(self.db, sort="relevance")["sort"], "newest")      # no ranking in the copy
        self.assertIn("capped at 50", queries.find(self.db, limit=500)["note"])
        self.assertIn("capped at 25", queries.find(self.db, limit=30, include_description=True)["note"])
        self.assertIn("description", queries.find(self.db, include_description=True)["projects"][0])

    def test_program_filter_rules(self):
        programs = [{"programId": 72, "acronym": "FO", "title": "Flight Opportunities"},
                    {"programId": 92295, "acronym": "HFORT", "title": "Human Flight Opportunities Research Test"},
                    {"programId": 34347, "acronym": "AIST", "title": "Advanced Information Systems Technology"},
                    {"programId": 32945, "acronym": "H-TIDeS", "title": "Technology for Lunar Surface"},
                    {"programId": 1, "acronym": "SBIR/STTR", "title": "Small Business Innovation Research"},
                    {"programId": 92284, "acronym": "PSRP", "title": "Payload Program A"},
                    {"programId": 92298, "acronym": "PSRP", "title": "Payload Program B"}]
        ids = lambda v: [p["programId"] for p in queries.resolve_program(v, programs)]
        self.assertEqual(ids("FO"), [72])                     # not "inFOrmation", not "for", not HFORT
        self.assertEqual(ids("fo"), [72])
        self.assertEqual(ids("Flight Opportunities"), [72])   # an exact title beats part of HFORT's
        self.assertEqual(ids("SBIR"), [1])
        self.assertEqual(ids("72"), [72])
        self.assertEqual(ids("PSRP"), [92284, 92298])         # a shared acronym: both, with a note
        self.assertEqual(ids("Lunar Surface"), [32945])       # part of a title, tried last
        with self.assertRaisesRegex(queries.QueryError, "No program has programId 5"):
            ids("5")
        with self.assertRaisesRegex(queries.QueryError, r"No program matches 'Flight Oportunities'.*FO"):
            ids("Flight Oportunities")

    def test_program_note_when_several_match(self):
        programs = [{"programId": 64, "acronym": "FO", "title": "Flight Opportunities"},
                    {"programId": 65, "acronym": "FO", "title": "Another FO"}]
        r = queries.aggregate(self.db, group_by="status", program="FO", programs=programs)
        self.assertEqual((r["total_projects"], len(r["program_filter"])), (1, 2))
        self.assertIn("matched 2 programs", r["program_note"])
        self.assertNotIn("program_note", queries.find(self.db, program="FO"))

    def test_bad_filters_explain_themselves(self):
        for kwargs in ({"status": "Pending"}, {"technology_area": "propulsion"}, {"sort": "biggest"}):
            with self.subTest(**kwargs), self.assertRaises(queries.QueryError):
                queries.find(self.db, **kwargs)

    def test_live_results_use_the_same_sql_in_techports_order(self):
        with open(SEARCH, encoding="utf-8") as f:
            results = [model.normalize(p) for p in json.load(f)["results"]]
        mem = copy.memory_db(list(reversed(results)))
        try:
            r = queries.find(mem, live=True)
            self.assertEqual(r["sort"], "relevance")
            self.assertEqual([p["projectId"] for p in r["projects"]], [900003, 900002, 900001])
            self.assertEqual(queries.find(mem, live=True, destination="mars")["total"], 1)
        finally:
            mem.close()

    # aggregate

    def test_aggregate(self):
        r = queries.aggregate(self.db, group_by="program")
        self.assertIn({"programId": 64, "acronym": "FO", "title": "Flight Opportunities", "projects": 1}, r["groups"])
        r = queries.aggregate(self.db, group_by="status")
        self.assertEqual({g["value"]: g["projects"] for g in r["groups"]}, {"Active": 1, "Canceled": 1, "Completed": 1})
        r = queries.aggregate(self.db, group_by="destination")
        self.assertEqual({g["value"]: g["projects"] for g in r["groups"]}, {"Mars": 1, "Moon and Cislunar": 2})
        self.assertIn("more than total_projects", r["note"])
        r = queries.aggregate(self.db, group_by="lead_organization")
        self.assertEqual((r["groups"][0]["projects"], r["groups_total"]), (1, 3))
        r = queries.aggregate(self.db, group_by="technology_area", status="Active")
        self.assertEqual(r["groups"], [{"value": "TX14", "projects": 1}])
        r = queries.aggregate(self.db, group_by="trl")
        self.assertIn({"value": None, "projects": 1}, r["groups"])                       # TRL 0 shown as not set
        r = queries.aggregate(self.db, group_by="msi_category")
        self.assertEqual(r["groups"], [{"value": "Hispanic Serving Institutions (HSI)", "projects": 1}])
        with self.assertRaises(queries.QueryError):
            queries.aggregate(self.db, group_by="funding")

    # contacts and batch

    def test_find_contacts_hides_emails_by_default(self):
        r = queries.find_contacts(self.db, name="tester")
        person = r["people"][0]
        self.assertEqual((person["name"], person["projectCount"]), ("Jane Q Tester", 2))
        self.assertEqual(person["roles"], ["Co-Investigator", "Principal Investigator"])
        self.assertNotIn("emails", person)
        with_emails = queries.find_contacts(self.db, name="tester", include_emails=True)["people"][0]
        self.assertEqual((with_emails["emails"], with_emails["orcids"]), (["jane@example.com"], ["0000-0000-0000-0001"]))

    def test_find_contacts_pages_with_offset(self):
        everyone = queries.find_contacts(self.db, name="er")
        self.assertEqual([p["name"] for p in everyone["people"]], ["Jane Q Tester", "Pat M Manager"])
        r = queries.find_contacts(self.db, name="er", offset=1)
        self.assertEqual(([p["name"] for p in r["people"]], r["matches"], r["offset"]), (["Pat M Manager"], 2, 1))

    def test_batch_returns_summary_rows(self):
        r = queries.batch(self.db, [900002, 1, 900001])
        self.assertEqual(([p["projectId"] for p in r["projects"]], r["missing"]), ([900002, 900001], [1]))
        self.assertNotIn("contacts", r["projects"][0])
        self.assertIn("url", r["projects"][0])
        self.assertIn("first 50", queries.batch(self.db, list(range(60)))["note"])
        r = queries.batch(self.db, [900002, 900002, 900001])                 # an id asked twice
        self.assertEqual((r["requested"], r["returned"], len(r["projects"])), (2, 2, 2))

    def test_copy_follows_the_pointer(self):
        first = dbfiles.current_path(self.tmp)
        copy.refresh(self.tmp, http=None, json_override=SEARCH)
        self.assertNotEqual(dbfiles.current_path(self.tmp), first)


if __name__ == "__main__":
    unittest.main()
