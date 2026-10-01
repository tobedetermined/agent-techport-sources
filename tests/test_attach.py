"""The shared rules for attaching files to tool results (servers/common/attach.py).

TechPort's tests check the size budget in detail through its own _fetch_files;
these check the contract every source relies on.
"""

import os
import shutil
import tempfile
import time
import unittest

from tests import PLUGIN_ROOT  # noqa: F401  puts the plugin on the import path

try:
    from mcp.server.mcpserver import MCPServer  # noqa: F401  version 2 only
except ImportError:          # no SDK (version 2): these tests are skipped
    MCPServer = None
if MCPServer is not None:    # with the SDK, a broken module must fail the tests, not skip them
    from servers.common import attach
    from servers.common.http import SourceError, TooLarge

MB = 1024 * 1024


def doc(name, size=None, ext="pdf"):
    return {"name": name, "url": f"https://example.invalid/{name}", "extension": ext, "bytes": size,
            "save_as": name}


@unittest.skipIf(MCPServer is None, "needs the MCP SDK")
class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fetched = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def fetch(self, sizes, kind="application/pdf"):
        def fetch(f, max_bytes):
            self.fetched.append(f["name"])
            size = sizes[f["name"]]
            if size is None:
                raise SourceError("example: the file could not be fetched")
            if size > max_bytes:
                raise TooLarge("too big")
            return b"%PDF" + b"x" * (size - 4), kind
        return fetch

    def run_attach(self, files, sizes, kind="application/pdf"):
        return attach.attach(files, self.fetch(sizes, kind), fields=("name", "url"),
                             later="left for another call", folder=self.tmp)

    def test_report_has_the_callers_fields_then_the_outcome(self):
        blocks, report = self.run_attach([doc("a", 1000)], {"a": 1000})
        self.assertEqual(list(report[0]), ["name", "url", "bytes", "included", "reason", "saved_to"])
        self.assertTrue(report[0]["included"])
        self.assertEqual(blocks[0].resource.uri, "https://example.invalid/a")
        self.assertEqual(blocks[0].resource.mime_type, "application/pdf")

    def test_files_are_attached_in_the_order_given(self):
        _, report = self.run_attach([doc("b", 6 * MB), doc("a", 6 * MB)], {"a": 6 * MB, "b": 6 * MB})
        self.assertEqual([r["name"] for r in report if r["included"]], ["b"])     # 12 MB would pass 11
        self.assertEqual(report[1]["reason"], "left for another call")
        self.assertEqual(self.fetched, ["b"])            # a file that can't fit isn't downloaded

    def test_a_spent_budget_downloads_no_more_files_of_unknown_size(self):
        names = ["a", "b", "c", "d"]
        sizes = {"a": int(10.5 * MB), "b": 2 * MB, "c": 3 * MB, "d": 4 * MB}
        _, report = self.run_attach([doc(n) for n in names], sizes)        # sizes unknown until downloaded
        self.assertEqual(self.fetched, ["a"])            # under 1 MB left: the rest wait, undownloaded
        self.assertEqual([r["reason"] for r in report[1:]], ["left for another call"] * 3)

    def test_a_file_too_big_to_attach_is_saved_under_its_name(self):
        _, report = self.run_attach([doc("big", 15 * MB)], {"big": 15 * MB})
        self.assertEqual(report[0]["saved_to"], os.path.join(self.tmp, "big.pdf"))
        self.assertTrue(os.path.exists(report[0]["saved_to"]))
        self.assertFalse(report[0]["included"])

    def test_saving_removes_this_folders_files_older_than_30_days(self):
        old, recent = os.path.join(self.tmp, "old.pdf"), os.path.join(self.tmp, "recent.pdf")
        for path in (old, recent):
            open(path, "wb").close()
        month_ago = time.time() - 31 * 86400
        os.utime(old, (month_ago, month_ago))
        self.run_attach([doc("big", 15 * MB)], {"big": 15 * MB})
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))

    def test_a_saved_name_must_be_plain(self):
        with self.assertRaises(ValueError):
            self.run_attach([{**doc("big", 15 * MB), "save_as": "../big"}], {"big": 15 * MB})
        self.assertEqual(os.listdir(self.tmp), [])

    def test_fetch_failures_are_reported_not_raised(self):
        _, report = self.run_attach([doc("gone"), doc("huge")], {"gone": None, "huge": 25 * MB})
        self.assertEqual(report[0]["reason"], "example: the file could not be fetched")
        self.assertEqual(report[1]["reason"], attach.OVER_CAP)

    def test_a_download_that_is_not_a_pdf_or_image(self):
        _, report = self.run_attach([doc("page", ext=None)], {"page": 1000}, kind="text/html")
        self.assertEqual(report[0]["reason"], "not a PDF or image (text/html)")

    def test_cannot_attach_judges_by_extension_and_known_size(self):
        self.assertEqual(attach.cannot_attach("mp4", None), "not a PDF or image (.mp4)")
        self.assertEqual(attach.cannot_attach("pdf", 25 * MB), attach.OVER_CAP)
        self.assertIsNone(attach.cannot_attach("PDF", 1000))
        self.assertIsNone(attach.cannot_attach(None, None))


if __name__ == "__main__":
    unittest.main()
