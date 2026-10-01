"""Calls to TechPort's public API (techport.nasa.gov), and nothing else; or,
when the user sets a relay, the same calls to the relay instead.

Every endpoint here was probed on 2026-09-30; see "TechPort connector" in
docs/design.md. The POST search and /api/trex/predict endpoints need a nonce
from TechPort's web app and are not used; the classifier is called at
/trex/predict, which doesn't.
"""

import os
import urllib.parse

from ..common.http import HttpClient

BASE = "https://techport.nasa.gov"
TAXONOMY_ROOT = 8817          # "2024 NASA Technology Taxonomy", the only released root


# Measured 2026-10-01: from inside NASA's network (the NASA VPN), techport.nasa.gov resolves to
# a different server, which answers every request 401 and sends /api/authenticate to NASA's
# Launchpad sign-on. Off the VPN, the same request is answered without a login.
LOGIN_REQUIRED = ("it asked for a NASA login. From inside NASA's network (on the NASA VPN, or on "
                  "site), TechPort sends every request to NASA's sign-on page; this plugin reads "
                  "only TechPort's public API and doesn't log in. Off the VPN it works. The "
                  "plugin's other sources aren't affected.")


RELAY_VAR = "TECHPORT_RELAY"
# The public TechPort relay, run by Alexander van Dijk: TechPort's public API passed through
# unchanged, for networks where techport.nasa.gov asks for a login (the NASA VPN). Used only
# when the user turns on the plugin's "use_techport_relay" setting. See the README.
RELAY = "https://nasatechport-mcp.fly.dev"


def relay_on(value):
    """Whether the setting turns the relay on. Claude Code passes a yes/no
    setting as text; unset arrives empty or as the unfilled "${user_config...}"."""
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def client(base=BASE, **kwargs):
    return HttpClient("TechPort", {urllib.parse.urlsplit(base).hostname},
                      status_messages={401: LOGIN_REQUIRED}, **kwargs)


class TechPort:
    def __init__(self, http=None, relay=None):
        """relay: whether to use the relay; by default from TECHPORT_RELAY."""
        self.relay = RELAY if relay_on(os.environ.get(RELAY_VAR) if relay is None else relay) else None
        self.base = self.relay or BASE
        self.http = http or client(self.base)
        self._tx_titles = None

    @property
    def relay_host(self):
        return urllib.parse.urlsplit(self.relay).netloc if self.relay else None

    @property
    def search_url(self):
        return f"{self.base}/api/projects/search"

    # Projects
    def search(self, query, limit):
        """TechPort's own keyword search: every match, ranked, up to limit.
        It ignores offset and all filter parameters."""
        return self.http.get_json(f"{self.base}/api/projects/search", params={"query": query, "limit": limit})

    def project(self, project_id):
        return self.http.get_json(f"{self.base}/api/projects/{int(project_id)}")["project"]

    def updated_since(self, date):
        """[{projectId, lastUpdated}] for projects changed on or after date (YYYY-MM-DD)."""
        return self.http.get_json(f"{self.base}/api/projects", params={"updatedSince": date})["projects"]

    def file(self, file_id, max_bytes):
        """(bytes, content type) of a library or strategy document."""
        return self.http.request(f"{self.base}/api/file/{int(file_id)}", max_bytes=max_bytes, timeout=120)

    # Reference data
    def programs(self, active_only):
        return self.http.get_json(f"{self.base}/api/programs", params={"activeOnly": str(bool(active_only)).lower()})["programs"]

    def program(self, program_id):
        return self.http.get_json(f"{self.base}/api/programs/{int(program_id)}")["program"]

    def organizations(self, name=None, acronym=None, uei=None, cage=None, limit=50):
        return self.http.get_json(f"{self.base}/api/organizations", params={
            "organizationName": name, "organizationAcronym": acronym,
            "organizationUei": uei, "organizationCageCode": cage, "limit": limit})["organizations"]

    def organization(self, organization_id):
        return self.http.get_json(f"{self.base}/api/organizations/{int(organization_id)}")["organization"]

    def strategy(self):
        """{"capabilities": [...19], "shortfalls": [...187]}"""
        return self.http.get_json(f"{self.base}/api/strategy")

    def shortfalls(self, query):
        return self.http.get_json(f"{self.base}/api/strategy/shortfalls/search", params={"query": query})["shortfalls"]

    def opportunities(self):
        return self.http.get_json(f"{self.base}/api/opportunities")["opportunities"]

    def opportunity(self, opportunity_id):
        return self.http.get_json(f"{self.base}/api/opportunities/{int(opportunity_id)}")["opportunity"]

    def classify(self, title, description):
        """TechPort's TREX model. Returns {"01.2.2": 1}: one code, unpadded, ranked."""
        return self.http.post_json(f"{self.base}/trex/predict", {"title": title, "description": description})

    def tx_titles(self):
        """{"TX01.2.2": "Electrostatic Propulsion", ...} for all 495 taxonomy nodes. Fetched once."""
        if self._tx_titles is None:
            nodes = self.http.get_json(f"{self.base}/api/taxonomies/nodes", params={"rootId": TAXONOMY_ROOT})
            found = {}

            def walk(n):
                if isinstance(n, dict):
                    if n.get("code"):
                        found[n["code"]] = n.get("title")
                    for v in n.values():
                        walk(v)
                elif isinstance(n, list):
                    for v in n:
                        walk(v)

            walk(nodes)
            self._tx_titles = found
        return self._tx_titles
