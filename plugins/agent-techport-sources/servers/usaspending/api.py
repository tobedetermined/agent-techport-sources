"""Calls to the USAspending API (api.usaspending.gov), and nothing else.

Every endpoint here was probed on 2026-09-30; see "USAspending connector" in
docs/design.md. No key is needed.
"""

import urllib.parse

from ..common.http import HttpClient

BASE = "https://api.usaspending.gov"


def client(**kwargs):
    # Grouped totals took up to 17 s when probed; allow well over that.
    return HttpClient("USAspending", {"api.usaspending.gov"}, timeout=90, **kwargs)


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


class USAspending:
    def __init__(self, http=None):
        self.http = http or client()

    # Awards
    def search(self, body):
        """One page of awards (up to 100). Don't trust page_metadata.hasNext on
        deep pages: it was wrong on pages 100-500. count() gives the total."""
        return self.http.post_json(f"{BASE}/api/v2/search/spending_by_award/", body)

    def count(self, filters):
        """Matching awards per type group: contracts, grants, idvs and others."""
        return self.http.post_json(f"{BASE}/api/v2/search/spending_by_award_count/", {"filters": filters})["results"]

    def award(self, award_id):
        """One award by its generated id (CONT_AWD_..., ASST_NON_...)."""
        return self.http.get_json(f"{BASE}/api/v2/awards/{_quote(award_id)}/")

    def transactions(self, award_id, limit):
        """The latest transactions first. The API takes up to 5,000 per page."""
        return self.http.post_json(f"{BASE}/api/v2/transactions/", {
            "award_id": award_id, "limit": limit, "page": 1, "sort": "action_date", "order": "desc"})

    def subawards(self, award_id, limit):
        """The largest subawards first."""
        return self.http.post_json(f"{BASE}/api/v2/subawards/", {
            "award_id": award_id, "limit": limit, "page": 1, "sort": "amount", "order": "desc"})

    # Recipients
    def recipients(self, keyword, limit=20):
        """Recipient records matching a name or UEI, at each level: P (a parent),
        C (a company with a parent), R (a company without one)."""
        return self.http.post_json(f"{BASE}/api/v2/recipient/", {"keyword": keyword, "award_type": "all", "limit": limit})

    def recipient(self, recipient_id):
        return self.http.get_json(f"{BASE}/api/v2/recipient/{_quote(recipient_id)}/", params={"year": "all"})

    # Totals: money obligated in the period, by transaction date
    def by_category(self, category, filters, limit):
        return self.http.post_json(f"{BASE}/api/v2/search/spending_by_category/{category}/",
                                   {"filters": filters, "limit": limit, "page": 1})

    def over_time(self, filters):
        return self.http.post_json(f"{BASE}/api/v2/search/spending_over_time/", {"group": "fiscal_year", "filters": filters})

    # Reference data
    def toptier_agencies(self):
        return self.http.get_json(f"{BASE}/api/v2/references/toptier_agencies/")["results"]

    def last_updated(self):
        """The date the data was last updated, as MM/DD/YYYY."""
        return self.http.get_json(f"{BASE}/api/v2/awards/last_updated/")["last_updated"]
