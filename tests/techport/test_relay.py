"""The optional TechPort relay: off by default; when turned on, every TechPort
request goes to it and nowhere else. Standard library only."""

import json
import os
import shutil
import tempfile
import unittest

from tests import FIXTURES
from tests.test_http import FakeResponse, opener
from servers.common import http
from servers.techport import api, copy


class RelaySettingTest(unittest.TestCase):
    def test_off_unless_turned_on(self):
        for value in (None, "", "false", "False", "0", "${user_config.use_techport_relay}"):
            with self.subTest(value=value):
                self.assertEqual(api.TechPort(relay=value or "").base, "https://techport.nasa.gov")
        for value in ("true", "True", " 1 ", "yes"):
            with self.subTest(value=value):
                tp = api.TechPort(relay=value)
                self.assertEqual((tp.base, tp.relay_host), (api.RELAY, "nasatechport-mcp.fly.dev"))

    def test_the_environment_variable_is_read_when_no_value_is_given(self):
        saved = os.environ.get(api.RELAY_VAR)
        os.environ[api.RELAY_VAR] = "true"
        try:
            self.assertEqual(api.TechPort().relay_host, "nasatechport-mcp.fly.dev")
        finally:
            if saved is None:
                os.environ.pop(api.RELAY_VAR, None)
            else:
                os.environ[api.RELAY_VAR] = saved


class RequestsGoOnlyToTheRelayTest(unittest.TestCase):
    def client(self, base, *responses):
        open_, calls = opener(*responses)
        return api.client(base, backoff=0, opener=open_), calls

    def test_paths_are_unchanged_under_the_relay(self):
        c, calls = self.client(api.RELAY, FakeResponse(b'{"program": {"acronym": "FO"}}'))
        tp = api.TechPort(http=c, relay="true")
        self.assertEqual(tp.program(72)["acronym"], "FO")
        self.assertEqual(calls, ["https://nasatechport-mcp.fly.dev/api/programs/72"])

    def test_techport_itself_is_not_contacted_when_a_relay_is_set(self):
        c, calls = self.client(api.RELAY)
        with self.assertRaises(http.HostNotAllowed):
            c.get_json("https://techport.nasa.gov/api/programs/72")
        self.assertEqual(calls, [])

    def test_the_daily_copy_downloads_through_the_relay(self):
        class FakeHTTP:
            def download(self, url, dest):
                self.url = url
                shutil.copyfile(os.path.join(FIXTURES, "techport_search.json"), dest)
                return os.path.getsize(dest)
        fake, tmp = FakeHTTP(), tempfile.mkdtemp()
        try:
            tp = api.TechPort(http=fake, relay="true")
            meta = copy.refresh(tmp, fake, url=tp.search_url)
            self.assertEqual((fake.url, meta["source"]), ("https://nasatechport-mcp.fly.dev/api/projects/search",) * 2)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
