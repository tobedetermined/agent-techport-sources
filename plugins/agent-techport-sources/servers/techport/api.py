"""Calls to TechPort's public API (techport.nasa.gov), and nothing else.

Every endpoint here was probed on 2026-09-30; see "TechPort connector" in
docs/design.md. The POST search and /api/trex/predict endpoints need a nonce
from TechPort's web app and are not used; the classifier is called at
/trex/predict, which doesn't.
"""

from ..common.http import HttpClient

BASE = "https://techport.nasa.gov"
TAXONOMY_ROOT = 8817          # "2024 NASA Technology Taxonomy", the only released root


def client(**kwargs):
    return HttpClient("TechPort", {"techport.nasa.gov"}, **kwargs)


class TechPort:
    def __init__(self, http=None):
        self.http = http or client()
        self._tx_titles = None

    # Projects
    def search(self, query, limit):
        """TechPort's own keyword search: every match, ranked, up to limit.
        It ignores offset and all filter parameters."""
        return self.http.get_json(f"{BASE}/api/projects/search", params={"query": query, "limit": limit})

    def project(self, project_id):
        return self.http.get_json(f"{BASE}/api/projects/{int(project_id)}")["project"]

    def updated_since(self, date):
        """[{projectId, lastUpdated}] for projects changed on or after date (YYYY-MM-DD)."""
        return self.http.get_json(f"{BASE}/api/projects", params={"updatedSince": date})["projects"]

    def file(self, file_id, max_bytes):
        """(bytes, content type) of a library or strategy document."""
        return self.http.request(f"{BASE}/api/file/{int(file_id)}", max_bytes=max_bytes, timeout=120)

    # Reference data
    def programs(self, active_only):
        return self.http.get_json(f"{BASE}/api/programs", params={"activeOnly": str(bool(active_only)).lower()})["programs"]

    def program(self, program_id):
        return self.http.get_json(f"{BASE}/api/programs/{int(program_id)}")["program"]

    def organizations(self, name=None, acronym=None, uei=None, cage=None, limit=50):
        return self.http.get_json(f"{BASE}/api/organizations", params={
            "organizationName": name, "organizationAcronym": acronym,
            "organizationUei": uei, "organizationCageCode": cage, "limit": limit})["organizations"]

    def organization(self, organization_id):
        return self.http.get_json(f"{BASE}/api/organizations/{int(organization_id)}")["organization"]

    def strategy(self):
        """{"capabilities": [...19], "shortfalls": [...187]}"""
        return self.http.get_json(f"{BASE}/api/strategy")

    def shortfalls(self, query):
        return self.http.get_json(f"{BASE}/api/strategy/shortfalls/search", params={"query": query})["shortfalls"]

    def opportunities(self):
        return self.http.get_json(f"{BASE}/api/opportunities")["opportunities"]

    def opportunity(self, opportunity_id):
        return self.http.get_json(f"{BASE}/api/opportunities/{int(opportunity_id)}")["opportunity"]

    def classify(self, title, description):
        """TechPort's TREX model. Returns {"01.2.2": 1}: one code, unpadded, ranked."""
        return self.http.post_json(f"{BASE}/trex/predict", {"title": title, "description": description})

    def tx_titles(self):
        """{"TX01.2.2": "Electrostatic Propulsion", ...} for all 495 taxonomy nodes. Fetched once."""
        if self._tx_titles is None:
            nodes = self.http.get_json(f"{BASE}/api/taxonomies/nodes", params={"rootId": TAXONOMY_ROOT})
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
