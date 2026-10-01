"""Long text in parts, and the passages that mention given words
(servers/common/text.py). Used for NTRS's extracted text and SEC filings.
Standard library only, so these run without the MCP SDK."""

import unittest

from tests import PLUGIN_ROOT  # noqa: F401  puts the plugin on the import path
from servers.common import text


class SplitTest(unittest.TestCase):
    def test_parts_end_at_a_line_break_within_the_size(self):
        body = ("x" * 99 + "\n") * 150                    # 15,000 characters, lines of 100
        spans = text.split(body, size=12_000)
        self.assertEqual("".join(body[a:b] for a, b in spans), body)
        self.assertTrue(all(b - a <= 12_000 for a, b in spans))
        self.assertTrue(all(body[b - 1] == "\n" for a, b in spans[:-1]))

    def test_a_line_longer_than_a_part_is_cut(self):
        self.assertEqual([b - a for a, b in text.split("y" * 30_000, size=12_000)], [12_000, 12_000, 6_000])

    def test_empty_text_has_no_parts(self):
        self.assertEqual(text.split(""), [])


class PageTest(unittest.TestCase):
    def test_page_marks(self):
        body = "[page 1]\nintro\n[page 2]\nresults here"
        self.assertEqual(text.page_at(body, body.index("results")), 2)
        self.assertEqual(text.page_at(body, 0), 1)
        self.assertIsNone(text.page_at("no marks here", 3))


class PassagesTest(unittest.TestCase):
    def test_whole_words_any_case_across_line_breaks(self):
        body = "The measured\nBOIL-OFF rate. Later, Figure 10 and Figure 1 appear." + " filler" * 100
        [p] = text.passages(body, "measured boil-off", spans=text.split(body))
        self.assertEqual((p["part"], p["page"], p["matches"]), (1, None, 1))
        self.assertNotIn("\n", p["text"])
        found = text.passages(body, "Figure 1", spans=text.split(body), width=5)
        self.assertEqual(sum(p["matches"] for p in found), 1)         # not Figure 10

    def test_words_of_a_phrase_match_across_punctuation(self):
        # As SEC's full-text search matches them: "(SBIR) Phase III" is "SBIR Phase III".
        body = "This Agreement is a Small Business Innovation Research (SBIR) Phase III award." + " filler" * 100
        self.assertEqual(len(text.passages(body, "SBIR Phase III", spans=text.split(body))), 1)

    def test_prefixes_and_alternatives(self):
        body = "cryogenic tanks; a cryocooler" + " filler" * 100
        self.assertEqual(text.passages(body, "cryogen", spans=text.split(body)), [])
        self.assertEqual(sum(p["matches"] for p in text.passages(body, "cryogen* | cryocooler",
                                                                  spans=text.split(body))), 2)

    def test_close_matches_share_a_passage(self):
        body = "alpha " * 10 + "target one " + "beta " * 5 + "target two " + "gamma " * 200
        [p] = text.passages(body, "target", spans=text.split(body))
        self.assertEqual(p["matches"], 2)
        self.assertTrue(p["text"].endswith("…"))
        self.assertFalse(p["text"].startswith("…"))

    def test_a_find_without_words(self):
        for find in (" | ", "*", ""):
            with self.assertRaises(text.NoWords):
                text.passages("text", find, spans=[(0, 4)])


if __name__ == "__main__":
    unittest.main()
