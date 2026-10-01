"""MCP server for USAspending.gov: federal contracts, grants and the companies
behind them. Runs over stdio.

Live API only, no local copy. The only host contacted is api.usaspending.gov
(see api.py). "USAspending connector" in docs/design.md has the probe results
behind every rule here.
"""

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..common import output
from ..common.http import SourceError
from . import api as api_mod, model

INSTRUCTIONS = """\
USAspending.gov: federal contracts, grants and IDVs (indefinite-delivery
vehicles) from fiscal year 2008 on, and the companies and universities that
received them. Live from the official API, which updates daily.

- Agencies are abbreviations such as NASA, DOD or DOE, as in the SBIR tools.
- Time filters are fiscal years (October to September). A search keeps awards
  with any transaction in those years, and each award's amount is its lifetime
  total; signed_only keeps only awards signed in those years ("won since
  2015"). For money spent in a period, use usaspending_aggregate, which adds
  up obligations by transaction date.
- Link an SBIR award by its contract or grant number (award_ids), and a
  company by its UEI.
- sbir_phase_iii finds follow-on contracts whose description says SBIR or STTR
  Phase III; contracts described otherwise aren't found. Say so when you use it.
- Use usaspending_aggregate for any total or ranking. Don't add up search
  results. To total a set of awards (Phase III contracts, or a list of award
  numbers), give it sbir_phase_iii or award_ids: it adds up whole awards.
  Its keyword totals count only transactions whose description matches.
- Lists come as tables: column names once, then one row of values per item.
  A long list stops at a size limit, with a note saying how to get the rest.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

server = MCPServer(name="usaspending", instructions=INSTRUCTIONS)
usa = api_mod.USAspending()
_cache = {}                    # the agency list; the data's date, and when it was read
UPDATED_TTL = 3600
MAX_PHASE_III_PAGES = 10       # 1,000 keyword matches; NASA had 385 when probed
LIST_ROWS = 25                 # transactions and subawards shown with one award
TEXT_CHARS = 200               # descriptions in those lists

# group_by: the API's category, or None for totals by fiscal year.
GROUPS = {"recipient": "recipient", "awarding_agency": "awarding_agency", "funding_agency": "funding_agency",
          "naics": "naics", "psc": "psc", "cfda": "cfda", "state": "state_territory", "country": "country",
          "fiscal_year": None}
OBLIGATED = "Amounts are money obligated in the period, by transaction date, not awards' lifetime totals."
KEYWORDS = ("Keyword totals count only transactions whose own description matches, so later modifications "
            "described differently are left out and awards can be understated. For whole awards, use "
            "sbir_phase_iii or award_ids.")
WHOLE = ("Totals of whole awards: each award's total obligation over its life (USAspending's Award Amount), "
         "added up by group. Not spending in a period.")
WHOLE_GROUPS = ("recipient", "awarding_agency", "fiscal_year")


def _agencies():
    if "agencies" not in _cache:
        _cache["agencies"] = usa.toptier_agencies()
    return _cache["agencies"]


def _data(*caveats):
    """What every answer says about where its data came from."""
    if time.time() - _cache.get("updated_at", 0) > UPDATED_TTL:
        _cache.update(updated=usa.last_updated(), updated_at=time.time())
    return {"source": "USAspending.gov API (api.usaspending.gov)", "data_updated": _cache["updated"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "caveats": ["Searches cover fiscal year 2008 on.", *caveats]}


def _short(text):
    return text[:TEXT_CHARS] + "…" if text and len(text) > TEXT_CHARS else text


def _filters(agency=None, **kwargs):
    return model.filters(agency=agency, agencies=_agencies() if agency else (), **kwargs)


def _phase_iii(f, award_type, sort, extra_fields=()):
    """Every keyword match (the count says how many pages), kept when the
    description says SBIR or STTR Phase III. Returns (kept, checked, found)."""
    found = usa.count(f)[award_type]
    pages = min(math.ceil(found / 100), MAX_PHASE_III_PAGES)
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = [r for page in pool.map(
            lambda p: usa.search(model.search_body(f, award_type, sort=sort, include_description=True, page=p,
                                                   extra_fields=extra_fields))["results"],
            range(1, pages + 1)) for r in page]
    return model.phase_iii(fetched), len(fetched), found


# ---------------------------------------------------------------- awards


@server.tool(annotations=READ_ONLY, structured_output=False)
def usaspending_search_awards(
    award_type: str = "contracts",
    agency: str | None = None,
    company: str | None = None,
    keywords: str | None = None,
    award_ids: list[str] | None = None,
    fiscal_year_from: int | None = None,
    fiscal_year_to: int | None = None,
    signed_only: bool = False,
    naics: str | None = None,
    psc: str | None = None,
    cfda: str | None = None,
    state: str | None = None,
    sbir_phase_iii: bool = False,
    include_description: bool = False,
    sort: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Find federal awards. All filters are optional and combine with AND.

    award_type: contracts (the default), grants or idvs; one per call.
    agency: the awarding agency, as an abbreviation (NASA, DOD, DOE...) or full
    name. company: part of a company's name, or its UEI. keywords: words
    searched in award text (loosely: "SBIR PHASE II" also finds Phase III);
    OR or | between alternatives; no parentheses or -word here.
    award_ids: contract or grant numbers, up to 100; the link from an SBIR award.
    fiscal_year_from / fiscal_year_to: fiscal years, 2008 on; an award is kept
    if any of its transactions falls in them; with signed_only, only awards
    signed in them. naics, psc: industry and product
    codes (contracts). cfda: a grant program number such as 43.012. state: the
    two-letter state of performance.
    sbir_phase_iii: only contracts whose description says SBIR or STTR Phase
    III (follow-on work after SBIR); those described otherwise aren't found.
    include_description adds the first 300 characters of each description.
    sort: amount (the default), newest or oldest (by start date).
    Returns the total, and one page (limit up to 100; offset to page). amount
    is each award's lifetime total. For totals, use usaspending_aggregate.
    """
    try:
        t = model.award_type_codes(award_type)
        f = _filters(award_type=t, agency=agency, company=company, keywords=keywords, award_ids=award_ids,
                     fiscal_year_from=fiscal_year_from, fiscal_year_to=fiscal_year_to, signed_only=signed_only,
                     naics=naics, psc=psc, cfda=cfda, state=state)
        limit, offset, sort = max(1, min(int(limit), 100)), max(0, int(offset)), sort or "amount"
        note = None
        if sbir_phase_iii:
            f["keywords"] = model.PHASE_III_KEYWORDS
            matched, checked, found = _phase_iii(f, t, sort)
            rows, total = matched[offset:offset + limit], len(matched)
            note = (f"Phase III: of {found:,} keyword matches, {total:,} have a description that says SBIR or "
                    "STTR Phase III. Follow-on contracts described otherwise aren't found.")
            if checked < found:
                note += f" Only the first {checked:,} keyword matches were checked."
        else:
            # The API pages by 100; a window [offset, offset + limit) spans at most two of its pages.
            first, last = offset // 100 + 1, (offset + limit - 1) // 100 + 1
            with ThreadPoolExecutor(max_workers=3) as pool:
                counted = pool.submit(usa.count, f)
                pages = list(pool.map(lambda p: usa.search(model.search_body(
                    f, t, sort=sort, include_description=include_description, page=p))["results"], range(first, last + 1)))
            fetched = [r for page in pages for r in page]
            start = offset - (first - 1) * 100
            rows, total = fetched[start:start + limit], counted.result()[t]
            if award_ids and first == 1 and total <= len(fetched):     # every match is in hand
                missing = model.missing_award_ids(award_ids, fetched)
                if missing:
                    note = (f"No {t} carry these award numbers: {missing}. They may be another award_type, "
                            "or not in USAspending.")
        result = {"award_type": t, "total": total, "offset": offset, "returned": len(rows),
                  "awards": [model.row(r, t, include_description=include_description) for r in rows],
                  "data": _data("A search keeps awards with any transaction in the fiscal years asked; "
                                "amount is each award's lifetime total.")}
        if note:
            result["note"] = note
        return output.text(output.limit_list(result, "awards", offset=offset))
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


@server.tool(annotations=READ_ONLY, structured_output=False)
def usaspending_get_award(
    id: str | None = None,
    award_id: str | None = None,
    agency: str | None = None,
    include_transactions: bool = False,
    include_subawards: bool = False,
) -> str:
    """One award in full: amounts (obligated, all options, outlays), dates, the
    company and its parent, the awarding and funding agency and office, industry
    and product codes or grant program, place of performance, set-aside.

    Look up by id (from a search result) or award_id (a contract or grant
    number; agency narrows it). include_transactions adds the latest 25
    transactions; include_subawards the 25 largest subawards. Executive pay,
    which award records can carry, is left out.
    """
    try:
        if not id:
            if not award_id:
                raise model.QueryError("Give id (from a search result) or award_id (a contract or grant number).")
            with ThreadPoolExecutor(max_workers=3) as pool:
                found = list(pool.map(lambda t: [(t, r) for r in usa.search(model.search_body(
                    _filters(award_type=t, agency=agency, award_ids=[award_id]), t, sort="amount",
                    include_description=False, page=1))["results"]], model.AWARD_TYPES))
            matches = [m for group in found for m in group]
            if not matches:
                raise model.QueryError(f"No award numbered {award_id!r} in USAspending (fiscal year 2008 on).")
            if len(matches) > 1:
                return output.text({
                    "matches": len(matches),
                    "awards": [{"award_type": t, **model.row(r, t, include_description=False)} for t, r in matches],
                    "note": "More than one award has this number. Call again with the id of the one you want.",
                    "data": _data()})
            id = matches[0][1]["generated_internal_id"]
        result = {"award": model.award(usa.award(id)), "data": _data()}
        if include_transactions:
            tx = usa.transactions(id, LIST_ROWS)
            result["transactions"] = [{"date": x.get("action_date"), "modification": x.get("modification_number"),
                                       "action": x.get("action_type_description"),
                                       "obligation": x.get("federal_action_obligation"),
                                       "description": _short(x.get("description"))} for x in tx.get("results", [])]
            if (tx.get("page_metadata") or {}).get("hasNext"):
                result["transactions_note"] = f"The latest {LIST_ROWS} transactions; there are more."
            output.limit_list(result, "transactions")
        if include_subawards:
            sa = usa.subawards(id, LIST_ROWS)
            result["subawards"] = [{"number": s.get("subaward_number"), "recipient": s.get("recipient_name"),
                                    "amount": s.get("amount"), "date": s.get("action_date"),
                                    "description": _short(s.get("description"))} for s in sa.get("results", [])]
            if (sa.get("page_metadata") or {}).get("hasNext"):
                result["subawards_note"] = f"The {LIST_ROWS} largest subawards; there are more."
            output.limit_list(result, "subawards")
        return output.text(result)
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------- companies and totals


@server.tool(annotations=READ_ONLY, structured_output=False)
def usaspending_recipient(uei: str | None = None, name: str | None = None) -> str:
    """A company or institution by UEI: its names, parent company, location and
    business types, and the money it was obligated, by awarding agency and by
    fiscal year (2008 on). This is the view of one company across agencies.
    With only a name: the recipients that match, to pick a UEI from. That
    search knows only current legal names; for a trade or former name
    ("Virgin Galactic"), search awards by company and take the UEI from there.
    """
    try:
        if uei:
            uei = uei.strip().upper()
            records = [r for r in usa.recipients(uei)["results"] if (r.get("uei") or "").upper() == uei]
            if not records:
                return output.text({"uei": uei, "found": False, "data": _data(),
                                    "note": "No USAspending recipient has this UEI. Try name."})
            # The company's own record: C (it has a parent) or R (it has none), before P (as a parent).
            record = next(r for level in ("C", "R", "P") for r in records if r.get("recipient_level") == level)
            f = model.filters(company=uei)
            with ThreadPoolExecutor(max_workers=3) as pool:
                profile = pool.submit(usa.recipient, record["id"])
                by_agency = pool.submit(usa.by_category, "awarding_agency", f, 100)
                by_year = pool.submit(usa.over_time, f)
            p = profile.result()
            agencies = [{"code": r.get("code"), "agency": r.get("name"), "obligated": r.get("amount")}
                        for r in by_agency.result().get("results", [])]
            years = [{"fiscal_year": int(r["time_period"]["fiscal_year"]), "obligated": r.get("aggregated_amount")}
                     for r in by_year.result().get("results", [])]
            return output.text({
                "uei": uei, "found": True,
                "profile": {"name": p.get("name"), "alternate_names": p.get("alternate_names") or [],
                            "uei": p.get("uei"), "duns": p.get("duns"), "parent_name": p.get("parent_name"),
                            "parent_uei": p.get("parent_uei"), "business_types": p.get("business_types") or [],
                            "location": {k: (p.get("location") or {}).get(k)
                                         for k in ("city_name", "state_code", "country_name")}},
                "by_agency": output.table(agencies) if agencies else [],
                "by_fiscal_year": output.table(years) if years else [],
                "data": _data("Amounts are money obligated to this UEI by transaction date; other companies "
                              "under the same parent aren't included.")})
        if name:
            rows = [{"name": r.get("name"), "uei": r.get("uei"), "level": r.get("recipient_level")}
                    for r in usa.recipients(name.strip())["results"]]
            return output.text({"query": name, "recipients": output.table(rows) if rows else [], "data": _data(),
                                "note": "Call again with uei for one company. Level: P a parent company, "
                                        "C a company with a parent, R a company without one. "
                                        + RECIPIENT_NAMES})
        raise model.QueryError("Give a uei, or a name to find one.")
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


# Measured 2026-10-01: the recipient search finds neither "Virgin Galactic" (GALACTIC ENTERPRISES,
# LLC) nor "Zero-G" (ZERO GRAVITY CORPORATION), while the award search finds both.
RECIPIENT_NAMES = ("This search covers each recipient's current legal name only, not former or trade names: "
                   "if the company isn't listed, usaspending_search_awards with company=<the name> also "
                   "matches the names on its awards, and its rows give the UEI.")


def _whole_awards(group_by, *, sbir_phase_iii, award_ids, award_type, keywords, limit, **common):
    """Whole-award totals: fetch the awards, then add up each one's total
    obligation by group. The API's own totals can't do this: with keywords
    they count only the transactions whose description matches."""
    if group_by not in WHOLE_GROUPS:
        raise model.QueryError("Whole-award totals (sbir_phase_iii or award_ids) group by "
                               "recipient, awarding_agency or fiscal_year.")
    note = None
    if sbir_phase_iii:
        f = _filters(award_type="contracts", award_ids=award_ids, **common)
        f["keywords"] = model.PHASE_III_KEYWORDS
        rows, checked, found = _phase_iii(f, "contracts", "amount", extra_fields=(model.SIGNED,))
        note = (f"Phase III: of {found:,} keyword matches, {len(rows):,} have a description that says SBIR or "
                "STTR Phase III. Follow-on contracts described otherwise aren't counted, nor are IDVs.")
        if checked < found:
            note += f" Only the first {checked:,} keyword matches were checked."
    else:
        types = [model.award_type_codes(award_type)] if award_type else list(model.AWARD_TYPES)
        with ThreadPoolExecutor(max_workers=3) as pool:
            rows = [r for found in pool.map(lambda t: usa.search(model.search_body(
                _filters(award_type=t, keywords=keywords, award_ids=award_ids, **common), t, sort="amount",
                include_description=False, page=1, extra_fields=(model.SIGNED,)))["results"], types) for r in found]
    if award_ids:
        missing = model.missing_award_ids(award_ids, rows)
        if missing:
            note = f"{note + ' ' if note else ''}No award carries these award numbers: {missing}."
    groups = model.whole_award_totals(rows, group_by)
    shown = groups if group_by == "fiscal_year" else groups[:limit]      # years stay whole, in order
    if len(shown) < len(groups):
        note = f"{note + ' ' if note else ''}The top {len(shown)} of {len(groups)} groups; limit goes up to 100."
    result = {"group_by": group_by, "totals_of": "whole awards", "awards": len(rows), "groups_total": len(groups),
              "returned": len(shown), "groups": shown, "data": _data(WHOLE)}
    if note:
        result["note"] = note
    return result


@server.tool(annotations=READ_ONLY, structured_output=False)
def usaspending_aggregate(
    group_by: str,
    award_type: str | None = None,
    agency: str | None = None,
    company: str | None = None,
    keywords: str | None = None,
    award_ids: list[str] | None = None,
    sbir_phase_iii: bool = False,
    fiscal_year_from: int | None = None,
    fiscal_year_to: int | None = None,
    signed_only: bool = False,
    naics: str | None = None,
    psc: str | None = None,
    cfda: str | None = None,
    state: str | None = None,
    limit: int = 20,
) -> str:
    """Totals and rankings. Use this for any total: "where did NASA's contract
    money go", "who got the most", "by year". Don't add up search results.

    By default: money obligated in a period, grouped by recipient,
    awarding_agency, funding_agency, naics, psc, cfda (grant programs), state,
    country or fiscal_year, with search's filters (award_type optional, all
    types by default). Amounts are obligations in the fiscal years asked, by
    transaction date, not awards' lifetime totals. With keywords, only
    transactions whose own description matches are counted, so awards can be
    understated. Groups come largest first; limit up to 100. Recipients come
    with their UEI.

    Whole-award totals, for a set of awards: with sbir_phase_iii (contracts
    whose description says SBIR or STTR Phase III; IDVs aren't included) or award_ids (contract or
    grant numbers, up to 100), the awards are fetched (up to 1,000) and each
    one's total obligation is added up, grouped by recipient, awarding_agency
    or fiscal_year (the year each award was signed). signed_only keeps only
    awards signed in the fiscal years asked.
    """
    try:
        if group_by not in GROUPS:
            raise model.QueryError(f"group_by must be one of: {', '.join(GROUPS)}.")
        common = dict(agency=agency, company=company, fiscal_year_from=fiscal_year_from,
                      fiscal_year_to=fiscal_year_to, naics=naics, psc=psc, cfda=cfda, state=state)
        if sbir_phase_iii or award_ids:
            return output.text(output.limit_list(_whole_awards(
                group_by, sbir_phase_iii=sbir_phase_iii, award_ids=award_ids, award_type=award_type,
                keywords=keywords, signed_only=signed_only, limit=max(1, min(int(limit), 100)), **common), "groups"))
        if signed_only:
            raise model.QueryError("signed_only works with whole-award totals (sbir_phase_iii or award_ids), "
                                   "or in usaspending_search_awards.")
        f = _filters(award_type=award_type, keywords=keywords, **common)
        result = {"group_by": group_by}
        if GROUPS[group_by] is None:
            groups = [{"fiscal_year": int(r["time_period"]["fiscal_year"]), "obligated": r.get("aggregated_amount")}
                      for r in usa.over_time(f).get("results", [])]
        else:
            d = usa.by_category(GROUPS[group_by], f, max(1, min(int(limit), 100)))
            if group_by == "recipient":      # the API's code is the old DUNS number; the UEI links to SBIR
                groups = [{"uei": r.get("uei"), "name": r.get("name"), "obligated": r.get("amount")}
                          for r in d.get("results", [])]
            else:
                groups = [{"code": r.get("code"), "name": r.get("name"), "obligated": r.get("amount")}
                          for r in d.get("results", [])]
            if (d.get("page_metadata") or {}).get("hasNext"):
                result["note"] = f"The top {len(groups)}; more groups exist (limit goes up to 100)."
        caveats = [OBLIGATED] + ([KEYWORDS] if keywords else [])
        result.update(returned=len(groups), groups=groups, data=_data(*caveats))
        return output.text(output.limit_list(result, "groups"))
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


def main():
    server.run()
