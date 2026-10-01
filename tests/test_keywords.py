import sqlite3
import unittest

from servers.common import keywords


class FtsQueryTest(unittest.TestCase):
    """Each query runs against a real FTS5 table, so the syntax is checked by SQLite."""

    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(":memory:")
        cls.db.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
        cls.docs = {1: "parabolic flight of regolith", 2: "suborbital regolith test on Mars",
                    3: "suborbital rocket", 4: "lunar dust in-situ", 5: "lunarization of parabolic dust"}
        cls.db.executemany("INSERT INTO t (rowid, body) VALUES (?, ?)", cls.docs.items())

    def hits(self, text):
        q = keywords.fts_query(text)
        return sorted(r[0] for r in self.db.execute("SELECT rowid FROM t WHERE t MATCH ?", (q,)))

    def test_bar_and_or_are_alternatives(self):
        self.assertEqual(self.hits("parabolic | suborbital"), [1, 2, 3, 5])
        self.assertEqual(self.hits("parabolic OR suborbital"), [1, 2, 3, 5])
        self.assertEqual(self.hits("parabolic|suborbital"), [1, 2, 3, 5])        # no spaces
        self.assertEqual(self.hits("parabolic suborbital"), [])                  # both must appear

    def test_groups_exclusion_and_prefix(self):
        self.assertEqual(self.hits("regolith (parabolic | suborbital)"), [1, 2])
        self.assertEqual(self.hits("regolith (parabolic | suborbital) -Mars"), [1])
        self.assertEqual(self.hits("lunar*"), [4, 5])
        self.assertEqual(self.hits("lunar"), [4])
        self.assertEqual(self.hits('"lunar dust"'), [4])
        self.assertEqual(self.hits("in-situ"), [4])                              # a hyphen isn't an exclusion

    def test_stray_operators_are_dropped(self):
        for text in ("OR parabolic", "parabolic OR", "parabolic | | suborbital", "(OR parabolic)",
                     "parabolic AND flight", "parabolic ()"):
            with self.subTest(text=text):
                self.assertTrue(self.hits(text))

    def test_what_cannot_be_searched_is_refused_with_the_syntax(self):
        for text in ("", "  ", "|", "-Mars", "(parabolic", "parabolic)", "(parabolic -Mars)"):
            with self.subTest(text=text), self.assertRaisesRegex(keywords.KeywordError, "Syntax|empty"):
                keywords.fts_query(text)


class AlternativesTest(unittest.TestCase):
    def test_split_on_bar_and_or(self):
        self.assertEqual(keywords.alternatives("parabolic | suborbital"), ["parabolic", "suborbital"])
        self.assertEqual(keywords.alternatives('"Flight Opportunities" OR suborbital'),
                         ["Flight Opportunities", "suborbital"])
        self.assertEqual(keywords.alternatives("zero gravity"), ["zero gravity"])
        self.assertEqual(keywords.alternatives("ORBITAL"), ["ORBITAL"])          # OR inside a word stays

    def test_unsupported_syntax_is_refused(self):
        for text in ("regolith (a | b)", "regolith -Mars", "ab | suborbital", "|"):
            with self.subTest(text=text), self.assertRaises(keywords.KeywordError):
                keywords.alternatives(text)


if __name__ == "__main__":
    unittest.main()
