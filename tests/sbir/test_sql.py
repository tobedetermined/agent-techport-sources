"""The SQL behind sbir_query: what it reads, what it refuses, and its limits.

Each test opens its own read-only connection, as each tool call does, because
run_sql sets up the connection it is given for one query.
"""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import FIXTURES
from servers.common import dbfiles
from servers.sbir import queries, store

SAMPLE = os.path.join(FIXTURES, "sbir_sample.csv")

# x from 1 to n, and optionally a text column of pad a's.
NUMBERS = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {n}) SELECT x FROM c"
PADDED = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < {n}) "
          "SELECT x, printf('%.*c', {pad}, 'a') FROM c")
RUNAWAY = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"


class SqlTestCase(unittest.TestCase):
    csv = SAMPLE

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.path = os.path.join(cls.tmp, "sbir.db")
        store.load_csv(cls.csv, cls.path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_sql(self, sql):
        db = dbfiles.connect_ro(self.path)
        try:
            return queries.run_sql(db, sql=sql)
        finally:
            db.close()


class RunSqlTest(SqlTestCase):

    # what comes back

    def test_rows_come_back_as_lists_under_their_column_names(self):
        r = self.run_sql("SELECT agency_code, count(*) AS n, sum(award_amount) AS total FROM awards "
                         "WHERE agency_code IS NOT NULL GROUP BY agency_code ORDER BY agency_code")
        self.assertEqual(r, {"columns": ["agency_code", "n", "total"],
                             "rows": [["DOD", 1, None], ["NASA", 3, 1150000.0]],
                             "returned": 2})

    def test_awards_has_every_column_but_the_contacts(self):
        r = self.run_sql("SELECT * FROM awards WHERE id = 1")
        self.assertEqual(len(r["columns"]), 36)      # 42 in the file, less 9 contact fields, plus id and 2 codes
        row = dict(zip(r["columns"], r["rows"][0]))
        self.assertEqual((row["pi_name"], row["agency_code"], row["state_code"]), ("Jane Doe", "NASA", "CA"))
        for hidden in ("pi_email", "pi_phone", "pi_title", "contact_name", "contact_email", "ri_poc_name"):
            self.assertNotIn(hidden, r["columns"])

    def test_keyword_index_joins_to_awards(self):
        r = self.run_sql("SELECT a.id FROM awards_fts JOIN awards a ON a.id = awards_fts.rowid "
                         "WHERE awards_fts MATCH 'regolith' ORDER BY a.id")
        self.assertEqual(r["rows"], [[2], [3]])

    def test_keyword_ranking_and_highlighting(self):
        # Rows 2 and 3 both mention regolith once; row 2 is the shorter, so it ranks first.
        r = self.run_sql("SELECT rowid FROM awards_fts WHERE awards_fts MATCH 'regolith' ORDER BY rank")
        self.assertEqual(r["rows"], [[2], [3]])
        r = self.run_sql("SELECT highlight(awards_fts, 0, '[', ']') FROM awards_fts "
                         "WHERE awards_fts MATCH 'excavator' AND rowid = 2")
        self.assertEqual(r["rows"], [["Lunar Regolith [Excavator]"]])

    def test_counts_over_its_own_ctes(self):
        # A count reads no columns, so SQLite checks a CTE that it keeps as a table
        # (materialized or recursive) by its name alone. Simple ones are folded into awards.
        r = self.run_sql("WITH nasa AS MATERIALIZED (SELECT id FROM awards WHERE agency_code = 'NASA') "
                         "SELECT count(*) FROM nasa")
        self.assertEqual(r["rows"], [[3]])
        r = self.run_sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 4) "
                         "SELECT count(*) FROM c")
        self.assertEqual(r["rows"], [[4]])

    def test_blobs_are_described_not_returned(self):
        self.assertEqual(self.run_sql("SELECT randomblob(4)")["rows"], [["<4-byte blob>"]])

    # what it refuses

    def test_one_statement_per_call(self):
        self.assertEqual(self.run_sql("SELECT 1;")["rows"], [[1]])      # a trailing semicolon is fine
        with self.assertRaises(queries.QueryError):
            self.run_sql("SELECT 1; SELECT 2")

    def test_empty_sql_is_refused(self):
        for sql in ("", "   ", "-- only a comment"):
            with self.subTest(sql=sql), self.assertRaises(queries.QueryError):
                self.run_sql(sql)

    def test_contact_columns_are_refused_however_asked(self):
        for sql in ("SELECT pi_email FROM awards",
                    "SELECT pi_email FROM main.awards",
                    "SELECT * FROM main.awards",
                    "SELECT (SELECT contact_phone FROM main.awards LIMIT 1)",
                    "SELECT count(*) FROM main.awards WHERE pi_email LIKE '%@example.com'"):
            with self.subTest(sql=sql), self.assertRaisesRegex(queries.QueryError, "include_contacts"):
                self.run_sql(sql)

    def test_only_the_awards_tables_can_be_read(self):
        # Built-in virtual tables differ between builds: dbstat is in some and not others,
        # json_each in all. Neither may be read.
        for sql in ("SELECT * FROM load_issues", "SELECT name FROM sqlite_schema", "SELECT * FROM dbstat",
                    "SELECT value FROM json_each('[1]')",
                    "SELECT count(*) FROM load_issues WHERE issue = 'malformed_row'"):
            with self.subTest(sql=sql), self.assertRaises(queries.QueryError):
                self.run_sql(sql)

    def test_nothing_but_select_runs(self):
        copy = os.path.join(self.tmp, "copy.db")
        for sql in ("INSERT INTO main.meta VALUES ('k', 'v')",
                    "UPDATE main.awards SET company = 'x'",
                    "UPDATE sqlite_master SET type = 'x'",
                    "DELETE FROM main.awards",
                    "CREATE TEMP TABLE t AS SELECT 1",
                    "DROP VIEW awards",
                    "ATTACH DATABASE ':memory:' AS other",
                    "PRAGMA query_only = OFF",
                    "PRAGMA table_info(awards)",
                    "BEGIN",
                    "ANALYZE",
                    f"VACUUM INTO '{copy}'"):
            with self.subTest(sql=sql), self.assertRaisesRegex(queries.QueryError, "nothing can be changed"):
                self.run_sql(sql)
        self.assertFalse(os.path.exists(copy))

    def test_writes_behind_a_with_still_meet_the_authorizer(self):
        # These pass the first-word check, so SQLite's own refusal is what stops them.
        for sql in ("WITH x AS (SELECT 1) DELETE FROM main.awards",
                    "WITH x AS (SELECT 1) UPDATE main.awards SET company = 'x'",
                    "WITH x AS (SELECT 1) INSERT INTO main.meta VALUES ('k', 'v')"):
            with self.subTest(sql=sql), self.assertRaises(queries.QueryError):
                self.run_sql(sql)
        self.assertEqual(self.run_sql("SELECT count(*) AS n FROM awards")["rows"][0][0] > 0, True)

    def test_leading_comments_are_skipped(self):
        for sql in ("-- a note\nSELECT 1 AS x", "/* note */ SELECT 1 AS x", "  values (1)"):
            with self.subTest(sql=sql):
                self.run_sql(sql)

    def test_the_checks_older_sqlite_makes_for_fts5_are_allowed(self):
        # SQLite 3.40.1 (Debian 12) prepares, but never runs, an UPDATE of sqlite_master
        # while FTS5 sets up its index, and fails the query if the checks are refused;
        # 3.53.1 makes none of them. Measured in a Linux container: these six.
        a = queries._authorize
        for column in ("type", "name", "tbl_name", "rootpage", "sql"):
            with self.subTest(column=column):
                self.assertEqual(a(sqlite3.SQLITE_UPDATE, "sqlite_master", column, "main", None), sqlite3.SQLITE_OK)
        self.assertEqual(a(sqlite3.SQLITE_READ, "sqlite_master", "ROWID", "main", None), sqlite3.SQLITE_OK)
        # Nothing more: the schema's text stays unreadable, other tables and the
        # temp schema can't be updated (and the connection can't write at all).
        for action, table, column, db in ((sqlite3.SQLITE_READ, "sqlite_master", "sql", "main"),
                                          (sqlite3.SQLITE_READ, "sqlite_master", "name", "main"),
                                          (sqlite3.SQLITE_UPDATE, "awards", "company", "main"),
                                          (sqlite3.SQLITE_UPDATE, "sqlite_master", "sql", "temp")):
            with self.subTest(action=action, table=table, column=column, db=db):
                self.assertEqual(a(action, table, column, db, None), sqlite3.SQLITE_DENY)

    def test_unsafe_functions_are_refused(self):
        for sql in ("SELECT fts3_tokenizer('simple')", "SELECT FTS3_TOKENIZER('simple')"):
            with self.subTest(sql=sql), self.assertRaises(queries.QueryError):
                self.run_sql(sql)

    # limits

    def test_a_runaway_query_is_stopped_at_the_time_limit(self):
        db = dbfiles.connect_ro(self.path)
        backstop = threading.Timer(5, db.interrupt)     # a broken limit fails the test instead of hanging it
        backstop.start()
        started = time.monotonic()
        try:
            with mock.patch.object(queries, "QUERY_SECONDS", 0.2), \
                    self.assertRaisesRegex(queries.QueryError, "limit"):
                queries.run_sql(db, sql=RUNAWAY)
        finally:
            backstop.cancel()
            db.close()
        self.assertLess(time.monotonic() - started, 2)

    def test_a_value_cannot_grow_past_the_value_limit(self):
        # group_concat of 2,000 strings of 1,000 characters. Only the length would come
        # back, so this fails only if SQLite was allowed to build the 2 MB string.
        with self.assertRaisesRegex(queries.QueryError, "MB"):
            self.run_sql(PADDED.format(n=2000, pad=1000).replace(
                "SELECT x, printf('%.*c', 1000, 'a') FROM c",
                "SELECT length(group_concat(printf('%.*c', 1000, 'a'), '')) FROM c"))

    def test_rows_stop_at_the_row_limit_and_say_so(self):
        with mock.patch.object(queries, "QUERY_MAX_ROWS", 3):
            cut = self.run_sql(NUMBERS.format(n=5))
            whole = self.run_sql(NUMBERS.format(n=3))
        self.assertEqual((cut["rows"], cut["returned"]), ([[1], [2], [3]], 3))
        self.assertIn("note", cut)
        self.assertEqual(whole["rows"], [[1], [2], [3]])
        self.assertNotIn("note", whole)                 # exactly at the limit isn't cut short

    def test_rows_stop_at_the_size_limit_and_say_so(self):
        # Each row is [x,"aaa..."] with 990 a's: 996 characters of compact JSON while x < 10,
        # so 5 rows fit in 5,000 characters and 6 don't.
        with mock.patch.object(queries, "QUERY_MAX_CHARS", 5000):
            r = self.run_sql(PADDED.format(n=9, pad=990))
        self.assertEqual(r["returned"], 5)
        self.assertIn("note", r)

    def test_a_first_row_over_the_size_limit_is_an_error(self):
        with mock.patch.object(queries, "QUERY_MAX_CHARS", 500), self.assertRaises(queries.QueryError):
            self.run_sql(PADDED.format(n=1, pad=990))


@unittest.skipUnless(os.environ.get("SBIR_REAL_CSV"), "set SBIR_REAL_CSV to the downloaded award_data.csv")
class RealFileSqlTest(SqlTestCase):
    """SQL on the full file reproduces the numbers measured for docs/design.md."""

    csv = os.environ.get("SBIR_REAL_CSV")

    def test_counts_agree_with_the_measured_numbers(self):
        r = self.run_sql("SELECT count(*) FROM awards WHERE agency_code = 'NASA' AND award_year >= 2015")
        self.assertEqual(r["rows"], [[5617]])
        r = self.run_sql("SELECT uei, count(*) AS n FROM awards WHERE agency_code = 'NASA' AND award_year >= 2015 "
                         "GROUP BY uei ORDER BY n DESC LIMIT 2")
        self.assertEqual([n for _, n in r["rows"]], [77, 73])          # Creare, CFD Research

    def test_keyword_index_on_the_full_file(self):
        r = self.run_sql("SELECT count(*) FROM awards_fts WHERE awards_fts MATCH 'sensor'")
        self.assertEqual(r["rows"], [[22491]])

    def test_a_phase_transition_question_runs_within_the_limit(self):
        # NASA Phase I companies of 2015-2022, and how many of them hold a NASA Phase II.
        r = self.run_sql("""
            WITH p1 AS (SELECT DISTINCT uei FROM awards WHERE agency_code = 'NASA' AND phase = 1
                        AND award_year BETWEEN 2015 AND 2022 AND uei IS NOT NULL),
                 p2 AS (SELECT DISTINCT uei FROM awards WHERE agency_code = 'NASA' AND phase = 2)
            SELECT count(*), sum(uei IN (SELECT uei FROM p2)) FROM p1""")
        self.assertEqual(r["rows"], [[1061, 701]])

    def test_wide_rows_are_cut_to_the_size_limit(self):
        r = self.run_sql("SELECT * FROM awards WHERE agency_code = 'NASA'")
        self.assertIn("note", r)
        rows_text = json.dumps(r["rows"], separators=(",", ":"), ensure_ascii=False)
        self.assertLessEqual(len(rows_text), queries.QUERY_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
