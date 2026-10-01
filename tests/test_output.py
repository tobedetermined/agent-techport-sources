import unittest
from datetime import date
from unittest import mock

from servers.common import output

PAD = "a" * 90      # a row [n,"aaa..."] is 96 characters of compact JSON while n < 10


def rows(*ns):
    return [{"n": n, "pad": PAD} for n in ns]


class TextTest(unittest.TestCase):
    def test_text_is_compact_json(self):
        self.assertEqual(output.text({"a": [1, "é"], "b": None}), '{"a":[1,"é"],"b":null}')

    def test_dates_are_written_as_text(self):
        self.assertEqual(output.text({"d": date(2026, 9, 30)}), '{"d":"2026-09-30"}')


class TableTest(unittest.TestCase):
    def test_columns_are_named_once(self):
        self.assertEqual(output.table([{"id": 1, "name": "x"}, {"id": 2, "name": "y"}]),
                         {"columns": ["id", "name"], "rows": [[1, "x"], [2, "y"]]})

    def test_values_follow_the_columns_whatever_the_key_order(self):
        self.assertEqual(output.table([{"a": 1, "b": 2}, {"b": 4, "a": 3}])["rows"], [[1, 2], [3, 4]])

    def test_records_with_different_fields_are_refused(self):
        for records in ([{"a": 1}, {"a": 2, "b": 3}], [{"a": 1}, {"b": 2}], [{"a": 1}, None]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                output.table(records)

    def test_an_empty_list_is_an_empty_table(self):
        self.assertEqual(output.table([]), {"columns": [], "rows": []})

    def test_uniform_means_records_that_can_share_a_table(self):
        self.assertTrue(output.uniform([{"a": 1}, {"a": 2}]))
        for value in ([], None, ["x"], [{"a": 1}, {"b": 2}], [{"a": 1}, None]):
            with self.subTest(value=value):
                self.assertFalse(output.uniform(value))


class LimitListTest(unittest.TestCase):
    def test_an_empty_list_stays_an_empty_list(self):
        self.assertEqual(output.limit_list({"returned": 0, "items": []}, "items"), {"returned": 0, "items": []})

    def test_a_list_that_fits_is_only_turned_into_a_table(self):
        result = output.limit_list({"returned": 2, "items": [{"a": 1}, {"a": 2}]}, "items")
        self.assertEqual(result, {"returned": 2, "items": {"columns": ["a"], "rows": [[1], [2]]}})

    def test_stops_at_the_size_limit_and_says_where_to_continue(self):
        # 1 + 97 per row: two rows are 195 characters, three are 292.
        with mock.patch.object(output, "MAX_CHARS", 250):
            result = output.limit_list({"offset": 10, "returned": 4, "items": rows(1, 2, 3, 4)}, "items", offset=10)
        self.assertEqual([r[0] for r in result["items"]["rows"]], [1, 2])
        self.assertEqual(result["returned"], 2)
        self.assertIn("offset=12", result["note"])

    def test_always_keeps_at_least_one(self):
        with mock.patch.object(output, "MAX_CHARS", 50):
            result = output.limit_list({"items": rows(1, 2)}, "items")
        self.assertEqual(len(result["items"]["rows"]), 1)
        self.assertIn("note", result)

    def test_records_are_measured_as_records(self):
        # {"n":1,"pad":"aaa..."} is 106 characters: two fit in 250 (215), three don't (322).
        with mock.patch.object(output, "MAX_CHARS", 250):
            result = output.limit_list({"items": rows(1, 2, 3)}, "items", as_table=False)
        self.assertEqual([r["n"] for r in result["items"]], [1, 2])

    def test_the_way_to_continue_can_name_what_was_left_out(self):
        with mock.patch.object(output, "MAX_CHARS", 250):
            result = output.limit_list({"items": rows(1, 2, 3, 4)}, "items",
                                       continue_with=lambda left: f"ask again for {[r['n'] for r in left]}")
        self.assertIn("ask again for [3, 4]", result["note"])

    def test_an_existing_note_is_kept(self):
        with mock.patch.object(output, "MAX_CHARS", 250):
            result = output.limit_list({"note": "limit is capped at 50.", "items": rows(1, 2, 3)}, "items")
        self.assertTrue(result["note"].startswith("limit is capped at 50. "))
        self.assertIn("size limit", result["note"])


if __name__ == "__main__":
    unittest.main()
