import os
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error

from tests import FIXTURES
from servers.sbir import store

SAMPLE = os.path.join(FIXTURES, "sbir_sample.csv")


class LoadSampleTest(unittest.TestCase):
    """The loader against a small hand-made file that has each known quirk."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = os.path.join(cls.tmp, "sbir.db")
        cls.meta = store.load_csv(SAMPLE, cls.db_path, {"source": "test"})
        cls.db = sqlite3.connect(cls.db_path)
        cls.db.row_factory = sqlite3.Row

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp)

    def one(self, sql, *args):
        return self.db.execute(sql, args).fetchone()

    def test_counts_and_meta(self):
        self.assertEqual(self.meta["rows_loaded"], "5")
        self.assertEqual(self.meta["malformed_rows"], "1")
        self.assertEqual(self.meta["source"], "test")

    def test_malformed_row_is_listed_not_dropped_silently(self):
        issues = self.db.execute("SELECT line, issue FROM load_issues ORDER BY line").fetchall()
        kinds = [tuple(i) for i in issues]
        self.assertIn((8, "malformed_row"), kinds)

    def test_unknown_values_are_loaded_and_listed(self):
        row = self.one("SELECT agency, agency_code, state, state_code, phase FROM awards WHERE award_title='Assay'")
        self.assertEqual((row["agency"], row["agency_code"]), ("Department of Magic", None))
        self.assertEqual((row["state"], row["state_code"]), ("Atlantis", None))
        self.assertIsNone(row["phase"])
        kinds = {r[0] for r in self.db.execute("SELECT issue FROM load_issues")}
        self.assertEqual(kinds, {"malformed_row", "unknown_agency", "unknown_state", "unknown_phase"})

    def test_all_42_columns_plus_codes_and_id(self):
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(awards)")]
        self.assertEqual(len(cols), 42 + 3)
        self.assertIn("pi_email", cols)

    def test_normalisation(self):
        row = self.one("SELECT * FROM awards WHERE agency_tracking_number='T1.01-1001'")
        self.assertEqual(row["contract"], "TEST-NASA-0001")          # trailing space trimmed
        self.assertEqual(row["agency_code"], "NASA")
        self.assertEqual(row["phase"], 1)
        self.assertEqual(row["state_code"], "CA")
        self.assertEqual(row["pi_name"], "Jane Doe")                 # double space collapsed
        self.assertIsNone(row["pi_phone"])                           # "() -" placeholder
        self.assertIsNone(row["proposal_award_date"])                # blank date
        self.assertEqual(row["award_year"], 2024)
        self.assertEqual(row["award_amount"], 150000.0)
        self.assertIn('"zero-boil-off" cryogenic\npropellant', row["abstract"])

    def test_company_name_whitespace(self):
        self.assertIsNotNone(self.one("SELECT 1 FROM awards WHERE company='Example Bio'"))

    def test_blank_amount_is_null_not_zero(self):
        self.assertIsNone(self.one("SELECT award_amount FROM awards WHERE company='Example Defense Co'")[0])

    def test_territory_and_placeholder_zip(self):
        row = self.one("SELECT state_code, zip FROM awards WHERE award_year=2023")
        self.assertEqual(row["state_code"], "GU")
        self.assertIsNone(row["zip"])

    def test_duplicate_tracking_contract_pairs_get_own_ids(self):
        ids = self.db.execute(
            "SELECT id FROM awards WHERE agency_tracking_number='T4.02-2002' AND contract='TEST-NASA-0002'").fetchall()
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0][0], ids[1][0])

    def test_full_text_search(self):
        hits = self.db.execute(
            "SELECT a.company FROM awards_fts f JOIN awards a ON a.id=f.rowid WHERE awards_fts MATCH 'regolith'"
        ).fetchall()
        self.assertEqual(len(hits), 2)
        self.assertEqual(self.one("SELECT count(*) FROM awards_fts WHERE awards_fts MATCH 'cryogenic'")[0], 1)

    def test_bad_numbers_are_null_and_reported(self):
        raw = ["x"] * len(store.HEADER)
        raw[store.HEADER.index("Award Amount")] = "$150,000"
        raw[store.HEADER.index("Award Year")] = "2025.0"
        raw[store.HEADER.index("Number Employees")] = ""
        raw[store.HEADER.index("Solicitation Year")] = "2024"
        bad = []
        row = store.clean_row(raw, bad)
        self.assertIsNone(row["award_amount"])
        self.assertIsNone(row["award_year"])
        self.assertEqual(bad, ["award_year='2025.0'", "award_amount='$150,000'"])

    def test_changed_header_stops_the_load(self):
        bad = os.path.join(self.tmp, "bad.csv")
        with open(SAMPLE, encoding="utf-8") as f, open(bad, "w", encoding="utf-8") as out:
            out.write(f.read().replace("Award Title", "Title", 1))
        with self.assertRaises(ValueError):
            store.load_csv(bad, os.path.join(self.tmp, "bad.db"))


class PlaceholderTest(unittest.TestCase):
    """SBIR.gov writes N/A, NA, None or null, in any case, where there is no value."""

    def clean(self, **values):
        raw = [""] * len(store.HEADER)
        for header, value in values.items():
            raw[store.HEADER.index(header)] = value
        return store.clean_row(raw)

    def test_placeholder_words_load_as_empty(self):
        row = self.clean(**{"Abstract": "N/A", "Topic Code": " na ", "PI Title": "null",
                            "Company Website": "None", "Award Title": "NULL"})
        self.assertEqual([row[c] for c in ("abstract", "topic_code", "pi_title", "company_website", "award_title")],
                         [None] * 5)

    def test_real_values_with_those_letters_stay(self):
        row = self.clean(**{"PI Name": "Nancy Null", "Award Title": "None Shall Pass",
                            "Company": "NA Technologies", "Abstract": "n/a-type junctions"})
        self.assertEqual((row["pi_name"], row["award_title"], row["company"], row["abstract"]),
                         ("Nancy Null", "None Shall Pass", "NA Technologies", "n/a-type junctions"))


class FakeHTTP:
    """Stands in for SBIR.gov: serves the sample file with a settable date."""

    def __init__(self, last_modified="Tue, 01 Sep 2026 05:42:41 GMT"):
        self.last_modified = last_modified
        self.etag = '"abc"'
        self.offline = False
        self.serve = "sample"          # or "html", "fail", "truncated"
        self.downloads = 0

    def head(self, url):
        if self.offline:
            raise urllib.error.URLError("offline")
        return {"last_modified": self.last_modified, "etag": self.etag,
                "bytes": str(os.path.getsize(SAMPLE))}

    def download(self, url, dest):
        self.downloads += 1
        if self.serve == "fail":
            raise ConnectionResetError("connection dropped")
        with open(SAMPLE, "rb") as f:
            data = f.read()
        if self.serve == "html":
            data = b"<html>SBIR.gov is currently undergoing maintenance</html>".ljust(len(data))
        if self.serve == "truncated":
            data = data[:100]
        with open(dest, "wb") as f:
            f.write(data)


class EnsureDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.http = FakeHTTP()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def ensure(self, **kw):
        return store.ensure_database(self.tmp, http=self.http, **kw)

    def files(self):
        return sorted(os.listdir(self.tmp))

    def test_refresh_switches_file_and_removes_the_old_one(self):
        old, _ = self.ensure()
        self.http.last_modified = "Thu, 01 Oct 2026 05:00:00 GMT"
        new, status = self.ensure()
        self.assertEqual(status, "refreshed")
        self.assertNotEqual(old, new)
        self.assertEqual(store.current_db_path(self.tmp), new)
        self.assertEqual(self.files(), ["current.txt", os.path.basename(new)])

    def test_stale_partial_downloads_are_cleaned_up(self):
        # A session that ends mid-download never reaches its own cleanup. TechPort's daily
        # copy downloads the same way, so the rule is shared.
        stale = [os.path.join(self.tmp, n) for n in ("award_data.99999.csv.part", "search.99999.json.part")]
        fresh = os.path.join(self.tmp, "award_data.99998.csv.part")
        for path in stale + [fresh]:
            open(path, "wb").close()
        for path in stale:
            os.utime(path, (0, 0))                           # a day or more old: abandoned
        self.ensure()
        self.assertEqual([os.path.exists(p) for p in stale], [False, False])
        self.assertTrue(os.path.exists(fresh))               # perhaps another session's download in progress

    def test_old_database_still_open_elsewhere_is_left_then_cleaned(self):
        """Windows refuses to delete a file another process has open. Simulate that."""
        old, _ = self.ensure()
        self.http.last_modified = "Thu, 01 Oct 2026 05:00:00 GMT"
        real_remove = os.remove

        def locked(path):
            if path == old:
                raise PermissionError(32, "The process cannot access the file", path)
            real_remove(path)

        os.remove = locked
        try:
            new, status = self.ensure()
        finally:
            os.remove = real_remove
        self.assertEqual(status, "refreshed")
        self.assertEqual(store.current_db_path(self.tmp), new)
        self.assertIn(os.path.basename(old), self.files())       # left behind, not an error
        self.assertEqual(self.ensure()[1], "current")               # next start cleans it up
        self.assertEqual(self.files(), ["current.txt", os.path.basename(new)])

    def test_reader_follows_a_refresh_by_another_session(self):
        """A long-running server must keep working after another session
        refreshes the data and removes the file it had been using."""
        self.ensure()
        db, meta = store.open_current(self.tmp)
        db.close()
        self.assertEqual(meta["source_last_modified"], self.http.last_modified)
        self.http.last_modified = "Thu, 01 Oct 2026 05:00:00 GMT"
        self.ensure()                                              # "another session" refreshes
        db, meta = store.open_current(self.tmp)
        self.assertEqual(db.execute("SELECT count(*) FROM awards").fetchone()[0], 5)
        db.close()
        self.assertEqual(meta["source_last_modified"], "Thu, 01 Oct 2026 05:00:00 GMT")

    def test_open_current_without_database(self):
        with self.assertRaises(store.SbirDataUnavailable):
            store.open_current(self.tmp)
        self.assertEqual(self.files(), [])                         # read-only: creates nothing

    def test_load_in_progress_is_not_cleaned_up(self):
        path, _ = self.ensure()
        building = os.path.join(self.tmp, "sbir-20990101T000000-1.db.building")
        open(building, "w").close()
        self.ensure()
        self.assertTrue(os.path.exists(building))
        os.utime(building, (0, 0))                                 # now looks abandoned
        self.ensure()
        self.assertFalse(os.path.exists(building))

    def test_first_use_downloads_then_reuses(self):
        path, status = self.ensure()
        self.assertEqual(status, "loaded")
        self.assertEqual(store.read_meta(path)["source_last_modified"], self.http.last_modified)
        self.assertEqual(self.ensure()[1], "current")
        self.assertEqual(self.http.downloads, 1)
        self.assertEqual(self.files(), ["current.txt", os.path.basename(path)])   # no leftover CSV or temp files

    def test_new_last_modified_triggers_refresh(self):
        self.ensure()
        self.http.last_modified = "Thu, 01 Oct 2026 05:00:00 GMT"
        self.assertEqual(self.ensure()[1], "refreshed")
        self.assertEqual(self.http.downloads, 2)

    def test_offline_with_database_is_stale(self):
        self.ensure()
        self.http.offline = True
        self.assertTrue(self.ensure()[1].startswith("stale"))

    def test_failed_refresh_keeps_the_existing_database(self):
        path, _ = self.ensure()
        before = store.read_meta(path)
        self.http.last_modified = "Thu, 01 Oct 2026 05:00:00 GMT"
        for serve in ("fail", "html", "truncated"):
            with self.subTest(serve=serve):
                self.http.serve = serve
                status = self.ensure()[1]
                self.assertTrue(status.startswith("stale"), status)
                self.assertEqual(store.read_meta(path), before)
                self.assertEqual(self.files(), ["current.txt", os.path.basename(path)])

    def test_failed_first_load_raises(self):
        self.http.serve = "html"
        with self.assertRaises(store.SbirDataUnavailable):
            self.ensure()

    def test_etag_used_when_no_last_modified(self):
        self.http.last_modified = None
        self.ensure()
        self.assertEqual(self.ensure()[1], "current")
        self.http.etag = '"def"'
        self.assertEqual(self.ensure()[1], "refreshed")

    def test_no_version_headers_does_not_redownload(self):
        self.http.last_modified = self.http.etag = None
        self.ensure()
        self.assertEqual(self.ensure()[1], "current")
        self.assertEqual(self.http.downloads, 1)

    def test_offline_without_database_raises(self):
        self.http.offline = True
        with self.assertRaises(store.SbirDataUnavailable):
            self.ensure()

    def test_user_supplied_file(self):
        path, status = self.ensure(csv_override=SAMPLE)
        self.assertEqual(status, "loaded")
        self.assertEqual(store.read_meta(path)["source"], os.path.abspath(SAMPLE))
        self.assertEqual(self.ensure(csv_override=SAMPLE)[1], "current")
        self.assertEqual(self.http.downloads, 0)


@unittest.skipUnless(os.environ.get("SBIR_REAL_CSV"), "set SBIR_REAL_CSV to the downloaded award_data.csv")
class RealFileTest(unittest.TestCase):
    """Checks the loader against the numbers measured for docs/design.md (file of 2026-09-01)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.meta = store.load_csv(os.environ["SBIR_REAL_CSV"], os.path.join(cls.tmp, "sbir.db"))
        cls.db = sqlite3.connect(os.path.join(cls.tmp, "sbir.db"))

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        shutil.rmtree(cls.tmp)

    def q(self, sql):
        return self.db.execute(sql).fetchone()[0]

    def test_measured_numbers(self):
        self.assertEqual(self.meta["rows_loaded"], "219590")
        self.assertEqual(self.meta["malformed_rows"], "0")
        self.assertEqual(self.q("SELECT count(*) FROM load_issues"), 0)
        self.assertEqual(self.q("SELECT count(*) FROM awards WHERE agency_code='NASA'"), 19485)
        self.assertEqual(self.q("SELECT count(*) FROM awards WHERE agency_code IS NULL"), 0)
        self.assertEqual(self.q("SELECT count(*) FROM awards WHERE phase IS NULL"), 0)
        self.assertEqual(self.q(
            "SELECT count(*) FROM awards WHERE agency_code='NASA' AND award_year>=2015 AND uei IS NULL"), 0)
        self.assertEqual(self.q(
            "SELECT count(*) FROM awards WHERE agency_code='NASA' AND proposal_award_date IS NULL"), 8709)
        # 19,479 NASA abstract fields, less 4,720 placeholders such as N/A (open question 11)
        self.assertEqual(self.q("SELECT count(abstract) FROM awards WHERE agency_code='NASA'"), 14759)


if __name__ == "__main__":
    unittest.main()
