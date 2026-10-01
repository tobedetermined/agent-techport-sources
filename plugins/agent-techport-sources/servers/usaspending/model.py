"""Request bodies and record shapes for USAspending. Standard library only.

Every rule here comes from the probe of 2026-09-30; see "USAspending
connector" in docs/design.md.
"""

import re
from datetime import date

from ..common import keywords as kw

# Searches start no earlier than this (fiscal year 2008); the API says so.
FLOOR = date(2007, 10, 1)

# The API's own award type groups. One group per request: mixing them is a 422.
AWARD_TYPES = {
    "contracts": ["A", "B", "C", "D"],
    "grants": ["02", "03", "04", "05", "F001", "F002"],
    "idvs": ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"],
}

# Search columns per award type: (the API's field, our name). Each type fills
# different fields: grants have no PSC, IDVs no end date (probed). An unknown
# field isn't refused by the API, just returned empty, so these were checked.
_COMMON = [("generated_internal_id", "id"), ("Award ID", "award_id"), ("Recipient Name", "recipient"),
           ("Recipient UEI", "uei"), ("Award Amount", "amount"), ("Start Date", "start_date")]
FIELDS = {
    "contracts": _COMMON + [("End Date", "end_date"), ("Awarding Agency", "awarding_agency"), ("PSC", "psc")],
    "grants": _COMMON + [("End Date", "end_date"), ("Awarding Agency", "awarding_agency"), ("CFDA Number", "cfda")],
    "idvs": _COMMON + [("Last Date to Order", "last_date_to_order"), ("Awarding Agency", "awarding_agency"),
                       ("PSC", "psc")],
}
DESCRIPTION = ("Description", "description")
DESCRIPTION_CHARS = 300

# The sort field must be among the requested fields, or the API answers 400.
SORTS = {"amount": ("Award Amount", "desc"), "newest": ("Start Date", "desc"), "oldest": ("Start Date", "asc")}

# Keyword search is looser than a phrase, so Phase III matches are re-checked here.
PHASE_III_KEYWORDS = ["SBIR PHASE III", "STTR PHASE III"]
_PHASE_III = re.compile(r"\b(SBIR|STTR)\b.*?\bPHASE[\s-]*III\b", re.IGNORECASE)


class QueryError(ValueError):
    """A value the API can't take. The message says what is accepted."""


# ---------------------------------------------------------------- requests


def award_type_codes(award_type):
    key = (award_type or "").strip().lower()
    if key not in AWARD_TYPES:
        raise QueryError(f"award_type must be one of: {', '.join(AWARD_TYPES)}.")
    return key


def fiscal_years(year_from=None, year_to=None, today=None, signed_only=False):
    """A time period of whole fiscal years (October to September). By default
    the API keeps awards with any transaction in it; with signed_only, only
    awards signed in it (its own date_type filter, probed)."""
    today = today or date.today()
    start = date(int(year_from) - 1, 10, 1) if year_from else FLOOR
    if start < FLOOR:
        raise QueryError("USAspending's search starts at fiscal year 2008 (October 2007).")
    end = date(int(year_to), 9, 30) if year_to else today
    if end < start:
        raise QueryError("fiscal_year_from is after fiscal_year_to.")
    period = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    if signed_only:
        period["date_type"] = "date_signed"
    return [period]


def agency_filter(agency, agencies):
    """agency: an abbreviation such as NASA, as the SBIR tools use, or a full name.
    agencies: the API's list of top-tier agencies."""
    wanted = agency.strip().lower()
    for a in agencies:
        if wanted in ((a.get("abbreviation") or "").lower(), (a.get("agency_name") or "").lower()):
            return {"type": "awarding", "tier": "toptier", "name": a["agency_name"]}
    raise QueryError(f"Unknown agency {agency!r}. Use an abbreviation such as NASA, DOD or DOE, or the full name.")


def filters(*, award_type=None, agency=None, agencies=(), company=None, keywords=None, award_ids=None,
            fiscal_year_from=None, fiscal_year_to=None, signed_only=False, naics=None, psc=None, cfda=None,
            state=None, today=None):
    f = {"time_period": fiscal_years(fiscal_year_from, fiscal_year_to, today, signed_only)}
    if award_type:
        f["award_type_codes"] = AWARD_TYPES[award_type_codes(award_type)]
    if agency:
        f["agencies"] = [agency_filter(agency, agencies)]
    if company:
        f["recipient_search_text"] = [company.strip()]      # a name, or a UEI
    if keywords:
        try:
            f["keywords"] = kw.alternatives(keywords)          # a list is OR to the API
        except kw.KeywordError as e:
            raise QueryError(str(e)) from e
    if award_ids:
        ids = [str(i).strip() for i in award_ids if str(i).strip()]
        if len(ids) > 100:
            raise QueryError("Up to 100 award numbers at once.")
        f["award_ids"] = ids
    if naics:
        f["naics_codes"] = [str(naics).strip()]
    if psc:
        f["psc_codes"] = [psc.strip().upper()]
    if cfda:
        f["program_numbers"] = [cfda.strip()]
    if state:
        f["place_of_performance_locations"] = [{"country": "USA", "state": state.strip().upper()}]
    return f


def search_body(filters, award_type, *, sort, include_description, page, limit=100, extra_fields=()):
    """extra_fields: more of the API's fields, for the server's own use."""
    if sort not in SORTS:
        raise QueryError(f"sort must be one of: {', '.join(SORTS)}.")
    fields = ([api for api, _ in FIELDS[award_type]] + ([DESCRIPTION[0]] if include_description else [])
              + list(extra_fields))
    field, order = SORTS[sort]
    return {"filters": filters, "fields": fields, "limit": limit, "page": page, "sort": field, "order": order}


# ---------------------------------------------------------------- records


def _code(value):
    return value.get("code") if isinstance(value, dict) else value


def row(api_row, award_type, *, include_description):
    """A search result with our short names. Codes come from the API as
    {code, description}; rows keep the code."""
    out = {ours: _code(api_row.get(api)) for api, ours in FIELDS[award_type]}
    if include_description:
        text = api_row.get(DESCRIPTION[0]) or None
        if text and len(text) > DESCRIPTION_CHARS:
            text = text[:DESCRIPTION_CHARS] + "…"
        out["description"] = text
    return out


def phase_iii(api_rows):
    """The rows whose description says SBIR or STTR Phase III."""
    return [r for r in api_rows if _PHASE_III.search(r.get(DESCRIPTION[0]) or "")]


SIGNED = "Base Obligation Date"     # equals the award's date_signed (checked on 12 awards)


def _fiscal_year(day):
    """The fiscal year of a YYYY-MM-DD date: it starts on 1 October."""
    if not day:
        return None
    year, month = int(day[:4]), int(day[5:7])
    return year + 1 if month >= 10 else year


def missing_award_ids(requested, api_rows):
    """The award numbers asked for that no row carries, in the order asked."""
    found = {(r.get("Award ID") or "").strip().upper() for r in api_rows}
    return [i for i in dict.fromkeys(str(i).strip() for i in requested or []) if i and i.upper() not in found]


def whole_award_totals(api_rows, group_by):
    """Search rows added up by company (UEI), awarding agency or fiscal year
    signed. Each award counts with its "Award Amount", which equals its total
    obligation (checked on 12 awards). Companies come largest first, years in
    order; a row without a UEI is grouped by its name."""
    groups = {}
    for r in api_rows:
        if group_by == "recipient":
            uei = r.get("Recipient UEI")
            key = uei or ("name", r.get("Recipient Name"))
            group = groups.setdefault(key, {"uei": uei, "name": r.get("Recipient Name"), "awards": 0, "obligated": 0.0})
        elif group_by == "awarding_agency":
            group = groups.setdefault(r.get("Awarding Agency"),
                                      {"agency": r.get("Awarding Agency"), "awards": 0, "obligated": 0.0})
        else:
            year = _fiscal_year(r.get(SIGNED))
            group = groups.setdefault(year, {"fiscal_year": year, "awards": 0, "obligated": 0.0})
        group["awards"] += 1
        group["obligated"] += r.get("Award Amount") or 0.0
    if group_by == "fiscal_year":
        return sorted(groups.values(), key=lambda g: (g["fiscal_year"] is None, g["fiscal_year"] or 0))
    return sorted(groups.values(), key=lambda g: -g["obligated"])


def _agency(a):
    a = a or {}
    return {"agency": (a.get("toptier_agency") or {}).get("name"),
            "subtier": (a.get("subtier_agency") or {}).get("name"),
            "office": a.get("office_agency_name")}


def _base(hierarchy):
    base = (hierarchy or {}).get("base_code") or {}
    return {"code": base.get("code"), "description": base.get("description")} if base else None


def award(d):
    """One award as the tool returns it. Left out: executive pay (personal data,
    like contacts) and the spending-account breakdowns."""
    period = d.get("period_of_performance") or {}
    recipient = d.get("recipient") or {}
    place = d.get("place_of_performance") or {}
    contract = d.get("latest_transaction_contract_data") or {}
    amounts = {"obligated": d.get("total_obligation"), "base_and_all_options": d.get("base_and_all_options"),
               "base_exercised_options": d.get("base_exercised_options"), "outlays": d.get("total_outlay"),
               "subawards": d.get("total_subaward_amount")}
    if d.get("category") not in ("contract", "idv"):        # grants and other assistance
        amounts = {"obligated": d.get("total_obligation"), "non_federal_funding": d.get("non_federal_funding"),
                   "total_funding": d.get("total_funding"), "outlays": d.get("total_outlay"),
                   "subawards": d.get("total_subaward_amount")}
    out = {
        "id": d.get("generated_unique_award_id"),
        "award_id": d.get("piid") or d.get("fain") or d.get("uri"),
        "category": d.get("category"),
        "type": d.get("type_description"),
        "description": d.get("description"),
        "amounts": amounts,
        "dates": {"signed": d.get("date_signed"), "start": period.get("start_date"), "end": period.get("end_date"),
                  "potential_end": (period.get("potential_end_date") or "")[:10] or None,
                  "last_modified": period.get("last_modified_date")},
        "recipient": {"name": recipient.get("recipient_name"), "uei": recipient.get("recipient_uei"),
                      "parent_name": recipient.get("parent_recipient_name"),
                      "parent_uei": recipient.get("parent_recipient_uei"),
                      "business_categories": recipient.get("business_categories") or [],
                      "location": {k: (recipient.get("location") or {}).get(k)
                                   for k in ("city_name", "state_code", "country_name")}},
        "awarding": _agency(d.get("awarding_agency")),
        "funding": _agency(d.get("funding_agency")),
        "place_of_performance": {k: place.get(k) for k in ("city_name", "state_code", "country_name")},
        "subaward_count": d.get("subaward_count"),
        "parent_award": d.get("parent_award"),
    }
    if d.get("naics_hierarchy") or d.get("psc_hierarchy"):
        out["naics"] = _base(d.get("naics_hierarchy"))
        out["psc"] = _base(d.get("psc_hierarchy"))
    if contract:
        out["set_aside"] = contract.get("type_set_aside_description")
        out["extent_competed"] = contract.get("extent_competed")
    if d.get("cfda_info") is not None:
        out["cfda"] = [{"code": c.get("cfda_number"), "title": c.get("cfda_title")} for c in d.get("cfda_info") or []]
    return out
