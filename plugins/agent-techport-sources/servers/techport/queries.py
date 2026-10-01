"""Filters, sorting, aggregates and contact search for TechPort. Standard library only.

The same SQL runs against the daily copy and against an in-memory database of
live search results (copy.memory_db), so a filter means the same thing either
way. All counting happens in SQL, never in the model.
"""

import difflib
import json
import re

from ..common import keywords


# Page caps. The output step (common/output.py) also stops every list at a
# size limit; real token counts are in docs/design.md, "Result sizes".
MAX_LIMIT = 50
MAX_LIMIT_WITH_DESCRIPTION = 25
MAX_BATCH = 50
MAX_GROUPS = 100
STATUSES = ("Active", "Completed", "Canceled")

SUMMARY = ("p.id AS projectId, p.title, p.status, p.program, p.program_id AS programId, p.trl_current AS trlCurrent, "
           "p.tx AS primaryTx, p.tx_title AS primaryTxTitle, p.lead_org AS leadOrganization, "
           "p.lead_org_type AS leadOrganizationType, p.start_date AS startDate, p.end_date AS endDate, "
           "p.last_updated AS lastUpdated")

SORTS = {
    "relevance": "p.rank, p.id",                     # TechPort's order; live searches only
    "newest": "p.start_date IS NULL, p.start_date DESC, p.id",
    "oldest": "p.start_date IS NULL, p.start_date, p.id",
    "recently_updated": "p.last_updated IS NULL, p.last_updated DESC, p.id",
    "trl": "p.trl_current IS NULL, p.trl_current DESC, p.id",
    "views": "p.view_count DESC, p.id",
}

# group_by name -> (expression, join)
GROUPS = {
    "status": ("p.status", ""),
    # By id: some acronyms belong to two programs (PSRP, SBP) and some programs
    # have none, so grouping by acronym would merge different programs.
    "program": ("p.program_id", ""),
    "mission_directorate": ("p.mission_directorate", ""),
    "lead_organization": ("p.lead_org", ""),
    "organization_type": ("p.lead_org_type", ""),
    "state": ("p.lead_org_state", ""),
    "country": ("p.lead_org_country", ""),
    "trl": ("p.trl_current", ""),
    "technology_area": ("p.tx1", ""),                   # TX01..TX17
    "technology_subarea": ("p.tx2", ""),                # TX01.2
    "technology": ("p.tx", ""),                         # TX01.2.2
    "start_year": ("p.start_year", ""),
    "end_year": ("p.end_year", ""),
    "destination": ("d.destination", "JOIN destinations d ON d.project_id = p.id"),
    "outcome": ("o.path", "JOIN outcomes o ON o.project_id = p.id"),
    "msi_category": ("c.category", "JOIN org_categories c ON c.project_id = p.id AND c.kind = 'msi'"),
    "set_aside": ("c.category", "JOIN org_categories c ON c.project_id = p.id AND c.kind = 'set_aside'"),
}


class QueryError(ValueError):
    """A filter value that can't be used. The message says what is accepted."""


def _like(value):
    return "%" + re.sub(r"([%_\\])", r"\\\1", value.strip()) + "%"


def fts_query(text):
    """Free text to a safe FTS5 query, in the syntax shared with the other
    servers (common/keywords.py)."""
    try:
        return keywords.fts_query(text, field="query")
    except keywords.KeywordError as e:
        raise QueryError(str(e)) from e


def catalogue(db):
    """Every program in the copy, as {"programId", "acronym", "title"}."""
    return [{"programId": r[0], "acronym": r[1], "title": r[2]} for r in db.execute(
        "SELECT program_id, max(program), max(program_title) FROM projects "
        "WHERE program_id IS NOT NULL GROUP BY program_id ORDER BY program_id")]


def resolve_program(value, programs):
    """The programs a program filter means, from the catalogue programs. The first
    rule that matches wins: a programId; an exact acronym; one part of an acronym
    ("SBIR" in "SBIR/STTR"); an exact title; part of a title. So "FO" never
    reaches titles that contain "for", and "Flight Opportunities" isn't HFORT."""
    v = str(value).strip()
    key = v.casefold()
    if not key:
        return []
    if v.isdigit():
        found = [p for p in programs if p["programId"] == int(v)]
        if not found:
            raise QueryError(f"No program has programId {v}. techport_programs lists them.")
        return found
    acronym = lambda p: (p["acronym"] or "").casefold()
    title = lambda p: (p["title"] or "").casefold()
    for rule in (lambda p: acronym(p) == key,
                 lambda p: key in [s.strip() for s in acronym(p).split("/")],
                 lambda p: title(p) == key,
                 lambda p: key in title(p)):
        found = [p for p in programs if rule(p)]
        if found:
            return found
    names = {n: p for p in programs for n in (p["acronym"], p["title"]) if n}
    close = difflib.get_close_matches(v, list(names), n=5, cutoff=0.5) or \
        [n for n in names if n.casefold().startswith(key[:2])][:5]
    hint = ("Closest: " + "; ".join(f"{names[n]['acronym']} ({names[n]['title']}, programId "
                                    f"{names[n]['programId']})" for n in close) + ". ") if close else ""
    raise QueryError(f"No program matches {v!r}. {hint}techport_programs lists them all.")


def _program_filter(db, filters, programs):
    """Swap the program filter for the programIds it means. Returns the programs
    matched, to show in the result, or None when there is no program filter."""
    value = filters.pop("program", None)
    if value is None or not str(value).strip():
        return None
    found = resolve_program(value, programs if programs is not None else catalogue(db))
    filters["program_ids"] = [p["programId"] for p in found]
    return found


def _show_programs(result, found):
    if found is None:
        return
    result["program_filter"] = found
    if len(found) > 1:
        result["program_note"] = (f"The program filter matched {len(found)} programs, listed in "
                                  "program_filter; pass a programId to pick one.")


def where(*, query=None, program_ids=None, status=None, technology_area=None, destination=None,
          lead_organization=None, organization_type=None, state=None, mission_directorate=None,
          trl_min=None, trl_max=None, start_year_from=None, start_year_to=None,
          end_year_from=None, end_year_to=None, outcome=None, use_fts=False):
    """SQL conditions for the filters. use_fts applies query through the copy's
    full-text index; live results are already TechPort's matches for it.
    program_ids comes from resolve_program."""
    c, a = [], []
    if program_ids is not None:
        c.append(f"p.program_id IN ({','.join('?' * len(program_ids))})")
        a += list(program_ids)
    if status:
        s = status.strip().capitalize()
        if s == "Cancelled":
            s = "Canceled"
        if s not in STATUSES:
            raise QueryError(f"status must be one of {', '.join(STATUSES)}, or omitted for all.")
        c.append("p.status = ?"); a.append(s)
    if technology_area:
        code = technology_area.strip().upper()
        if not re.fullmatch(r"TX\d{2}(\.\d+){0,2}", code):
            raise QueryError("technology_area is a TX code or prefix, such as TX14, TX14.1 or TX14.1.1.")
        c.append("(p.tx = ? OR p.tx LIKE ?)"); a += [code, code + ".%"]
    if destination:
        c.append("EXISTS (SELECT 1 FROM destinations d WHERE d.project_id = p.id AND d.destination LIKE ? ESCAPE '\\')")
        a.append(_like(destination))
    if lead_organization:
        c.append("p.lead_org LIKE ? ESCAPE '\\'"); a.append(_like(lead_organization))
    if organization_type:
        c.append("p.lead_org_type LIKE ? ESCAPE '\\'"); a.append(_like(organization_type))
    if state:
        c.append("p.lead_org_state = ?"); a.append(state.strip().upper())
    if mission_directorate:
        c.append("p.mission_directorate = ? COLLATE NOCASE"); a.append(mission_directorate.strip())
    for col, lo, hi in (("p.trl_current", trl_min, trl_max), ("p.start_year", start_year_from, start_year_to),
                        ("p.end_year", end_year_from, end_year_to)):
        if lo is not None:
            c.append(f"{col} >= ?"); a.append(int(lo))
        if hi is not None:
            c.append(f"{col} <= ?"); a.append(int(hi))
    if outcome:
        c.append("EXISTS (SELECT 1 FROM outcomes o WHERE o.project_id = p.id AND o.path LIKE ? ESCAPE '\\')")
        a.append(_like(outcome))
    if query and use_fts:
        c.append("p.id IN (SELECT rowid FROM projects_fts WHERE projects_fts MATCH ?)")
        a.append(fts_query(query))
    return (" WHERE " + " AND ".join(c)) if c else "", a


def _rows(cursor):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def find(db, *, sort=None, limit=20, offset=0, include_description=False, live=False, programs=None,
         **filters):
    """programs: the catalogue for the program filter; by default the db's own,
    which for live results (copy.memory_db) holds only the programs found."""
    sort = sort or ("relevance" if live else "newest")
    if sort not in SORTS:
        raise QueryError(f"sort must be one of: {', '.join(SORTS)}.")
    if sort == "relevance" and not live:
        sort = "newest"                     # the copy has no TechPort ranking
    requested = int(limit)
    cap = MAX_LIMIT_WITH_DESCRIPTION if include_description else MAX_LIMIT
    limit = max(1, min(requested, cap))
    offset = max(0, int(offset))
    matched = _program_filter(db, filters, programs)
    w, a = where(use_fts=not live, **filters)
    total = db.execute(f"SELECT count(*) FROM projects p{w}", a).fetchone()[0]
    rows = _rows(db.execute(f"SELECT {SUMMARY}, p.record FROM projects p{w} ORDER BY {SORTS[sort]} "
                            f"LIMIT ? OFFSET ?", a + [limit, offset]))
    for r in rows:
        record = json.loads(r.pop("record"))
        r["destinations"] = record["destinations"]
        r["url"] = record["url"]
        if include_description:
            r["description"] = (record.get("description") or "")[:600] or None
    result = {"total": total, "offset": offset, "returned": len(rows), "sort": sort, "projects": rows}
    _show_programs(result, matched)
    if requested > cap:
        result["note"] = f"limit is capped at {cap}" + (" with descriptions" if include_description else "") + \
                         "; use offset to page."
    return result


def aggregate(db, *, group_by, limit=None, sort=None, programs=None, **filters):
    if group_by not in GROUPS:
        raise QueryError(f"group_by must be one of: {', '.join(GROUPS)}.")
    if sort not in (None, "", "count", "value"):
        raise QueryError("sort must be count or value.")
    expr, join = GROUPS[group_by]
    matched = _program_filter(db, filters, programs)
    w, a = where(use_fts=True, **filters)
    total = db.execute(f"SELECT count(*) FROM projects p{w}", a).fetchone()[0]
    ranked = group_by in ("lead_organization", "technology", "technology_subarea", "outcome",
                          "msi_category", "set_aside", "country", "state", "program")
    order = "projects DESC, value" if (sort == "count" or (not sort and ranked)) else "value IS NULL, value"
    n = max(1, min(int(limit or (25 if ranked else MAX_GROUPS)), MAX_GROUPS))
    groups = _rows(db.execute(
        f"SELECT {expr} AS value, count(DISTINCT p.id) AS projects FROM projects p {join}{w} "
        f"GROUP BY {expr} ORDER BY {order} LIMIT ?", a + [n]))
    distinct = db.execute(f"SELECT count(DISTINCT {expr}) FROM projects p {join}{w}", a).fetchone()[0]
    if group_by == "program":
        names = {r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT program_id, max(program), max(program_title) FROM projects GROUP BY program_id")}
        groups = [{"programId": g["value"], "acronym": names.get(g["value"], (None, None))[0],
                   "title": names.get(g["value"], (None, None))[1], "projects": g["projects"]} for g in groups]
    result = {"group_by": group_by, "total_projects": total, "groups_total": distinct,
              "returned": len(groups), "groups": groups}
    _show_programs(result, matched)
    if join:
        result["note"] = ("A project can have several values here, so group counts can add up "
                          "to more than total_projects.")
    return result


def batch(db, project_ids):
    """Summary rows for up to 50 projects. Full records are too big to return in
    bulk (100 came to 168,000 tokens), so they come one at a time."""
    ids = list(dict.fromkeys(int(i) for i in project_ids))       # an id asked twice is looked up once
    note = None
    if len(ids) > MAX_BATCH:
        note = f"Only the first {MAX_BATCH} ids are looked up; ask again for the rest."
        ids = ids[:MAX_BATCH]
    rows = _rows(db.execute(f"SELECT {SUMMARY}, p.record FROM projects p WHERE p.id IN ({','.join('?' * len(ids))})", ids))
    found = {}
    for r in rows:
        record = json.loads(r.pop("record"))
        r["destinations"] = record["destinations"]
        r["url"] = record["url"]
        found[r["projectId"]] = r
    result = {"requested": len(ids), "returned": len(found), "missing": [i for i in ids if i not in found],
              "projects": [found[i] for i in ids if i in found]}
    if note:
        result["note"] = note
    return result


def find_contacts(db, *, name, include_emails=False, limit=20, offset=0):
    if not name or len(name.strip()) < 2:
        raise QueryError("name needs at least two characters.")
    rows = db.execute(
        "SELECT c.name, c.role, c.kind, c.email, c.orcid, p.id, p.title, p.program, p.status, p.start_date "
        "FROM contacts c JOIN projects p ON p.id = c.project_id WHERE c.name LIKE ? ESCAPE '\\' "
        "ORDER BY c.name, p.start_date DESC", (_like(name),)).fetchall()
    people = {}
    for nm, role, kind, email, orcid, pid, title, program, status, start in rows:
        person = people.setdefault(nm, {"name": nm, "roles": set(), "projects": [], "emails": set(), "orcids": set()})
        if role:
            person["roles"].add(role)
        if email:
            person["emails"].add(email)
        if orcid:
            person["orcids"].add(orcid)
        if kind == "project":
            person["projects"].append({"projectId": pid, "title": title, "program": program,
                                       "status": status, "startDate": start, "role": role})
    out = []
    offset = max(0, int(offset))
    for person in list(people.values())[offset:offset + max(1, min(int(limit), MAX_LIMIT))]:
        entry = {"name": person["name"], "roles": sorted(person["roles"]),
                 "projectCount": len(person["projects"]), "projects": person["projects"][:25]}
        if include_emails:
            entry["emails"] = sorted(person["emails"])
            entry["orcids"] = sorted(person["orcids"])
        out.append(entry)
    return {"matches": len(people), "offset": offset, "returned": len(out), "people": out,
            "note": "Names are matched as text, so different people with similar names can appear, "
                    "and one person can appear under name variants."}
