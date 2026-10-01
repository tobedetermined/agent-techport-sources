"""MCP server for NASA TechPort. Runs over stdio.

Live API first; a daily local copy only for what the API can't do (counts,
listings without a keyword, contact search, batch lookups). The only host
contacted is techport.nasa.gov. See "TechPort connector" in docs/design.md.
"""

import json
import re
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations

from ..common import attach, output
from ..common.http import SourceError
from . import api as api_mod, copy, queries
from .model import iso_date, merge, normalize, strip_html, tx_code, without_contact_details

LIVE_MAX = 2000                  # keyword matches fetched live; above this, the copy answers

INSTRUCTIONS = """\
NASA TechPort: about 21,000 space technology R&D projects, mostly funded through
the Space Technology Mission Directorate (SBIR/STTR, Flight Opportunities, NIAC,
Game Changing Development, center IRAD and others). It does not cover
operational missions, science programs or procurement contracts, and the public
API has no funding amounts: for dollars use the SBIR tools (and USAspending).

GROUNDING RULE: every claim about projects, programs, organizations or
technology areas must come from a tool result. Never state counts, rankings or
comparisons from memory or by tallying records yourself: use techport_aggregate.
If the tools can't answer, say so; an honest "the data can't answer that" is
better than a plausible guess.

Relay: when results carry via_relay, the user has set a TechPort relay, so
TechPort's public data reached this machine through that host rather than
straight from techport.nasa.gov. Say so once when citing TechPort.

Data freshness: searches with keywords, single projects, programs,
organizations, capabilities, opportunities and what's-new are live. Counts,
listings without keywords, contact search and batch lookups come from a local
copy refreshed daily; those results give its date.

Documents: techport_get_project attaches a project's library files (briefing
charts, final reports, images), most useful first, up to about 11 MB per result;
its documents list names the rest, which techport_get_document fetches in
further calls. To understand a project, read the briefing chart and final or
closeout report first, then others in order, and say which documents you read
and which you didn't.

Contacts: names and roles are shown by default; emails only when asked for.

Lists come as tables: column names once, then one row of values per item. A
long list stops at a size limit, with a note saying how to get the rest.

Context: TechPort is the inventory of what NASA is developing. Strategy and
policy context (initiatives, program rationale, inter-agency plans) mostly lives
outside it; pair it with web search for the narrative behind the numbers.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

server = MCPServer(name="techport", instructions=INSTRUCTIONS)
tp = api_mod.TechPort()
_copy_lock = threading.Lock()
_refreshing = threading.Event()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _via():
    """Where TechPort requests went, when not straight to techport.nasa.gov."""
    return {"via_relay": tp.relay_host} if tp.relay_host else {}


def _live(extra=None):
    return {"source": "TechPort live API (techport.nasa.gov)", **_via(), "retrieved_at": _now(), **(extra or {})}


def _copy_notice(meta):
    notice = {"source": "local copy of TechPort, checked for changes daily", **_via(), "as_of": copy.as_of(meta),
              "projects_in_copy": int(meta.get("projects", 0))}
    if _last_failure["reason"]:
        notice["refresh_problem"] = (f"The last refresh failed ({_last_failure['reason']}); "
                                     "this copy may be more than a day old.")
    return notice


def _fail(e):
    if isinstance(e, (ToolError,)):
        raise e
    raise ToolError(str(e)) from e


def _data_dir():
    return copy.default_data_dir()


RETRY_AFTER_SECONDS = 3600
_last_failure = {"at": 0.0, "reason": None}


def _build_copy(first=False):
    """Bring the copy up to date: fetch only what changed when there is a recent
    full pull, else pull and load everything. On first build, TechPort's id list
    (a small, separate endpoint) says how many projects to expect, so a truncated
    dump is rejected."""
    meta, _ = copy.status(_data_dir())
    if meta and not first and not copy.full_pull_due(meta) and not os.environ.get("TECHPORT_JSON_PATH"):
        try:
            # A shared relay limits each address to 60 requests a minute; pace well under it.
            updated = copy.update(_data_dir(), tp, pause=1.0 if tp.relay_host else 0.2)
            if updated is not None:
                missed = int(updated.get("last_update_failed") or 0)
                if missed:      # kept what it got; wait an hour before asking again
                    _last_failure.update(at=time.time(), reason=f"{missed} changed projects couldn't be fetched")
                else:
                    _last_failure.update(at=0.0, reason=None)
                return
        except Exception as e:
            _last_failure.update(at=time.time(), reason=str(e))
            raise
    expected = None
    if first and not os.environ.get("TECHPORT_JSON_PATH"):
        try:
            expected = len(tp.updated_since("2000-01-01"))
        except SourceError:
            expected = None
    try:
        copy.refresh(_data_dir(), tp.http, os.environ.get("TECHPORT_JSON_PATH"), expected=expected,
                     url=tp.search_url)
        _last_failure.update(at=0.0, reason=None)
    except Exception as e:
        _last_failure.update(at=time.time(), reason=str(e))
        raise


def _recently_failed():
    return time.time() - _last_failure["at"] < RETRY_AFTER_SECONDS


def _refresh_in_background():
    if _refreshing.is_set() or _recently_failed():
        return          # one refresh at a time; after a failure, wait an hour before the next

    def run():
        try:
            with _copy_lock:
                _build_copy()
        except Exception:
            pass        # the old copy stays in use; the failure shows in the data notice
        finally:
            _refreshing.clear()
    _refreshing.set()
    threading.Thread(target=run, name="techport-refresh", daemon=True).start()


def _open_copy():
    """(db, meta) for the local copy. Builds it on first use (about 15 s); brings
    it up to date in the background when it was last checked over a day ago."""
    meta, fresh = copy.status(_data_dir())
    if meta is None:
        with _copy_lock:
            meta, fresh = copy.status(_data_dir())
            if meta is None:
                if _recently_failed():
                    raise ToolError(f"No local TechPort copy yet; the last attempt to build one failed "
                                    f"({_last_failure['reason']}). It will be retried within the hour.")
                try:
                    _build_copy(first=True)
                except Exception as e:
                    raise ToolError(f"No local TechPort copy yet, and building one failed: {e}") from e
    elif not fresh:
        _refresh_in_background()
    try:
        return copy.open_copy(_data_dir())
    except Exception as e:
        raise ToolError(f"Could not open the TechPort copy: {e}") from e


_program_list = []
_program_lock = threading.Lock()


def _programs():
    """Every program, for the program filter on live results: from the copy when
    there is one, else from TechPort's program list (fetched once)."""
    meta, _ = copy.status(_data_dir())
    if meta:
        db, _ = copy.open_copy(_data_dir())
        try:
            return queries.catalogue(db)
        finally:
            db.close()
    with _program_lock:        # tools run in worker threads; fill the list once
        if not _program_list:
            _program_list.extend([{"programId": p.get("programId"), "acronym": p.get("acronym"),
                                   "title": p.get("title")} for p in tp.programs(False)])
    return _program_list


def _search_record(project_id, title):
    """The search-shape record for one project (for phase and closeout documents):
    from the copy if there is one, else from a live search on its title."""
    meta, _ = copy.status(_data_dir())
    if meta:
        db, _ = copy.open_copy(_data_dir())
        try:
            row = db.execute("SELECT record FROM projects WHERE id = ?", (project_id,)).fetchone()
        finally:
            db.close()
        if row:
            return json.loads(row[0])
    try:
        found = tp.search(title, 200).get("results", [])
    except SourceError:
        return None
    for r in found:
        if r.get("projectId") == project_id:
            return normalize(r)
    return None


def _files_dir():
    return os.path.join(_data_dir(), "files")


PRIORITY = ("briefing", "final", "closeout", "summary", "report")


def _priority(f):
    """Most useful first: briefing charts and final/closeout reports, other
    documents, then images."""
    title = (f.get("title") or "").lower()
    is_doc = (f.get("type") or "").lower() in ("document", "closeout document") or (f.get("extension") or "") == "pdf"
    return (0 if any(w in title for w in PRIORITY) else 1 if is_doc else 2)


LATER = "not attached to keep this result under Claude Code's size limit; fetch with techport_get_document"


def _cannot_fetch(f):
    """Why a file can never be attached, judged from what TechPort says about it
    (extension, size), or None. Such files aren't downloaded."""
    return attach.cannot_attach(f.get("extension"), f.get("bytes"))


def _fetch_files(files):
    """Attach files, most useful first, under the shared rules
    (servers/common/attach.py). Returns (blocks, report); the report lists every
    file, each entry with the same fields, so it can be sent as a table."""
    ordered = [{**f, "save_as": str(int(f["fileId"]))} for f in sorted(files, key=_priority)]
    return attach.attach(ordered, lambda f, max_bytes: tp.file(f["fileId"], max_bytes),
                         fields=("fileId", "title", "type", "url"), later=LATER, folder=_files_dir())


def _text(obj):
    return TextContent(type="text", text=output.text(obj))


# Measured 2026-10-01: TechPort's own search reads "|" as AND ("parabolic | suborbital" 38, the same
# as "parabolic suborbital"), and ignores grouping ("regolith (parabolic OR suborbital)" 831, the
# count for "regolith" alone, against 18 and 22 for each pair). "a OR b" alone works (1,092), but
# mixed with other words it doesn't, so any OR goes to the copy. Phrases and -word work (327; 635 of 831).
GROUPED = re.compile(r"\||\bOR\b|[()]")


# Lists inside a project record that are sent as tables when their entries
# share fields, as model.py builds them; an odd entry leaves a list as it is.
PROJECT_TABLES = ("additionalTx", "otherOrganizations", "contacts", "programContacts", "outcomes", "files", "links")


def _with_tables(project):
    return {k: output.table(v) if k in PROJECT_TABLES and output.uniform(v) else v for k, v in project.items()}


# ---------------------------------------------------------------- projects


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_find_projects(
    query: str | None = None,
    program: str | int | None = None,
    status: str | None = None,
    technology_area: str | None = None,
    destination: str | None = None,
    lead_organization: str | None = None,
    organization_type: str | None = None,
    state: str | None = None,
    mission_directorate: str | None = None,
    trl_min: int | None = None,
    trl_max: int | None = None,
    start_year_from: int | None = None,
    start_year_to: int | None = None,
    end_year_from: int | None = None,
    end_year_to: int | None = None,
    outcome: str | None = None,
    sort: str | None = None,
    include_description: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find TechPort projects by keyword and/or filters. Returns projectId, title,
    status, program, current TRL, primary technology area (TX code), lead
    organization, dates, destinations, lastUpdated and the TechPort page URL.
    Always cite projectId and lastUpdated.

    query: keywords, searched live by TechPort itself: words, "phrases",
    -word. With OR, | or (parentheses), the daily copy's index answers instead
    (NTRS's syntax). Without query, filters run on the daily copy (so you can
    list, e.g., every Flight Opportunities project).
    program: a programId (72), an acronym (FO, SBIR, GCD, NIAC) or a title; an
    acronym matches only that acronym, and part of a title is tried last. The
    result's program_filter shows the programs matched; see techport_programs.
    status: Active, Completed or Canceled (default: all).
    technology_area: TX code or prefix (TX14, TX14.1, TX14.1.1);
    techport_classify_technology suggests codes. destination: part of a
    destination name (Moon, Mars, Earth, Low Earth Orbit). lead_organization:
    part of the name. organization_type: NASA Center, Industry, Academia,
    FFRDC/UARC... state: two-letter code of the lead organization. trl_min /
    trl_max: current TRL (1-9; TRL not set is excluded when either is given).
    include_description adds the first 600 characters (pages of up to 25 then).
    outcome: part of an outcome path (Infused, Transitioned To Industry,
    Closed Out, Advanced).
    sort: relevance (keyword searches; the default then), newest (start date;
    the default otherwise), oldest, recently_updated, trl, views.
    Pages of up to 50; use offset for more. For counts and rankings use
    techport_aggregate, not this.
    """
    filters = dict(program=program, status=status, technology_area=technology_area, destination=destination,
                   lead_organization=lead_organization, organization_type=organization_type, state=state,
                   mission_directorate=mission_directorate, trl_min=trl_min, trl_max=trl_max,
                   start_year_from=start_year_from, start_year_to=start_year_to,
                   end_year_from=end_year_from, end_year_to=end_year_to, outcome=outcome)
    try:
        grouped = bool(query and GROUPED.search(re.sub(r'"[^"]*"', " ", query)))
        if query and query.strip() and not grouped:
            found = tp.search(query.strip(), LIVE_MAX)
            matches = found.get("total") or 0
            if matches <= LIVE_MAX:
                db = copy.memory_db([normalize(r) for r in found.get("results", [])])
                try:
                    # The program filter needs every program, not just those in these results.
                    programs = _programs() if program and str(program).strip() else None
                    result = queries.find(db, live=True, programs=programs, sort=sort, limit=limit, offset=offset,
                                          include_description=include_description, **filters)
                finally:
                    db.close()
                result["keyword_matches"] = matches
                result["data"] = _live({"search": "TechPort's own keyword search"})
            else:
                db, meta = _open_copy()
                try:
                    result = queries.find(db, query=query, sort=sort, limit=limit, offset=offset,
                                          include_description=include_description, **filters)
                finally:
                    db.close()
                result["note"] = (f"TechPort found {matches:,} matches for this query, more than the "
                                  f"{LIVE_MAX:,} fetched live, so the daily copy's own keyword index answered. "
                                  "It matches fewer fields than TechPort's search; narrower keywords give live "
                                  "results.")
                result["data"] = _copy_notice(meta)
        else:
            db, meta = _open_copy()
            try:
                result = queries.find(db, query=query if grouped else None, sort=sort, limit=limit,
                                      offset=offset, include_description=include_description, **filters)
            finally:
                db.close()
            if grouped:
                result["note"] = ("OR, | or parentheses: answered by the daily copy's keyword index, because "
                                  "TechPort's own search doesn't combine them reliably. The index matches "
                                  "fewer fields than TechPort's search.")
            result["data"] = _copy_notice(meta)
        return output.text(output.limit_list(result, "projects", offset=result["offset"]))
    except queries.QueryError as e:
        raise ToolError(str(e)) from e
    except (SourceError, copy.CopyUnavailable) as e:
        _fail(e)


@server.tool(annotations=READ_ONLY)
def techport_get_project(
    project_id: int | None = None,
    project_ids: list[int] | None = None,
    include_documents: bool = True,
    include_contacts: bool = False,
) -> list:
    """Everything about one project, live: description, benefits, TRL begin/current/end,
    program, lead and supporting organizations, technology areas, destinations,
    dates, outcomes (lineage: Advanced From/To, Transitioned To, Infused, with
    relatedProjectId to follow), contacts, and its documents.

    With project_id, the project's library files (briefing charts, final and
    closeout reports, images) come attached, most useful first, up to about
    11 MB per result (Claude Code's limit); the documents list names the rest,
    to fetch with techport_get_document. Links to other websites are listed,
    not fetched. Set include_documents=false to skip them,
    for example when only following lineage.
    With project_ids (up to 50), returns summary rows from the daily copy (title,
    status, program, TRL, technology area, lead organization, dates); call again
    with project_id for a full record.
    Contacts: names and roles by default; include_contacts adds emails and ORCIDs.
    """
    try:
        if project_ids:
            db, meta = _open_copy()
            try:
                result = queries.batch(db, project_ids)
            finally:
                db.close()
            result["data"] = _copy_notice(meta)
            return [_text(output.limit_list(result, "projects", continue_with=lambda left:
                                            "Ask again for the rest: project_ids "
                                            + str([p["projectId"] for p in left]) + "."))]
        if project_id is None:
            raise ToolError("Give project_id, or project_ids for several.")
        project = normalize(tp.project(project_id))
        project = merge(project, _search_record(project["projectId"], project["title"] or ""))
        if not include_contacts:
            project = without_contact_details(project)
        # Files the plugin can never fetch (over the cap, or not a PDF or image)
        # are counted, not listed: a few projects have hundreds of data files.
        reasons = [(f, _cannot_fetch(f)) for f in project["files"]]
        never = Counter(why for _, why in reasons if why)
        usable = [f for f, why in reasons if not why]
        blocks, report = ([], None)
        if include_documents and usable:
            blocks, report = _fetch_files(usable)
        # One entry per file, most useful first, with its status when fetched.
        listed = output.limit_list({"files": report if report is not None else usable}, "files",
                                   continue_with=lambda left: "Not listed for size; fetch by id with "
                                   "techport_get_document: " + str([f["fileId"] for f in left]) + ".")
        project["files"] = listed["files"]
        documents, notes = {}, [listed["note"]] if "note" in listed else []
        if report is not None:
            attached = sum(1 for r in report if r["included"])
            documents.update(attached=attached, not_attached=len(report) - attached)
        if never:
            documents["not_listed"] = dict(never)
            notes.append("Files the plugin can't fetch are counted, not listed; "
                         f"the project page links them all: {project['url']}")
        if notes:
            documents["note"] = " ".join(notes)
        result = {"project": _with_tables(project), "data": _live()}
        if documents:
            result["documents"] = documents
        return [_text(result), *blocks]
    except (SourceError, copy.CopyUnavailable) as e:
        _fail(e)


@server.tool(annotations=READ_ONLY)
def techport_get_document(file_ids: list[int]) -> list:
    """TechPort files by fileId (from a project's documents list, or a capability
    area's gap document in techport_capabilities), attached as PDFs or images.
    One call carries up to about 11 MB of files (Claude Code's limit on one tool
    result); files that don't fit are listed, to fetch in a further call. A file
    over 11 MB is saved locally and its path returned; over 20 MB, link only."""
    ids = [int(i) for i in file_ids][:50]
    if not ids:
        raise ToolError("Give one or more file_ids.")
    files = [{"fileId": i, "title": None, "type": None, "extension": None,
              "url": api_mod.BASE + f"/api/file/{i}"} for i in ids]
    blocks, report = _fetch_files(files)
    return [_text({"attached": len(blocks), "files": output.table(report), "data": _live()}), *blocks]


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_aggregate(
    group_by: str,
    query: str | None = None,
    program: str | int | None = None,
    status: str | None = None,
    technology_area: str | None = None,
    destination: str | None = None,
    lead_organization: str | None = None,
    organization_type: str | None = None,
    state: str | None = None,
    mission_directorate: str | None = None,
    trl_min: int | None = None,
    trl_max: int | None = None,
    start_year_from: int | None = None,
    start_year_to: int | None = None,
    end_year_from: int | None = None,
    end_year_to: int | None = None,
    outcome: str | None = None,
    sort: str | None = None,
    limit: int | None = None,
) -> str:
    """Count projects grouped by a field, with the same filters as
    techport_find_projects. Use this for ALL counting and ranking: "how many",
    "top organizations", "TRL distribution", "by year".

    group_by: status, program, mission_directorate, lead_organization,
    organization_type, state, country, trl, technology_area (TX01-TX17),
    technology_subarea (TX01.2), technology (TX01.2.2), start_year, end_year,
    destination, outcome, msi_category, set_aside.
    Programs are grouped by programId (some acronyms are shared by two
    programs) and returned with acronym and title.
    Rankings (organizations, technologies, programs...) come largest first, top
    25 by default; others in value order. sort: count or value. limit: up to 100.
    query here uses the copy's own keyword index, which matches fewer fields than
    TechPort's live search; its syntax is NTRS's: words must all appear; "a
    phrase"; OR or | between alternatives; (parentheses); -word; word*. Runs on the daily copy; the result gives its date.
    A null value means the field is not set (for TRL, TechPort's 0 is shown as null).
    """
    try:
        db, meta = _open_copy()
        try:
            result = queries.aggregate(db, group_by=group_by, sort=sort, limit=limit, query=query, program=program,
                                       status=status, technology_area=technology_area, destination=destination,
                                       lead_organization=lead_organization, organization_type=organization_type,
                                       state=state, mission_directorate=mission_directorate, trl_min=trl_min,
                                       trl_max=trl_max, start_year_from=start_year_from, start_year_to=start_year_to,
                                       end_year_from=end_year_from, end_year_to=end_year_to, outcome=outcome)
        finally:
            db.close()
        result["data"] = _copy_notice(meta)
        return output.text(output.limit_list(result, "groups"))
    except queries.QueryError as e:
        raise ToolError(str(e)) from e


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_whats_new(since: str | None = None, limit: int = 50, offset: int = 0) -> str:
    """Projects added or changed in TechPort since a date (YYYY-MM-DD; default: 7
    days ago), newest first, live from TechPort's own change list. Returns
    projectId, title, status, program and lastUpdated; up to 100 per call,
    offset to page."""
    since = since or (date.today() - timedelta(days=7)).isoformat()
    try:
        date.fromisoformat(since)
    except ValueError as e:
        raise ToolError("since must be a date like 2026-09-01.") from e
    try:
        changed = tp.updated_since(since)
    except SourceError as e:
        _fail(e)
    changed.sort(key=lambda c: iso_date(c.get("lastUpdated")) or "", reverse=True)
    offset = max(0, int(offset))
    top = changed[offset:offset + max(1, min(int(limit), 100))]

    def summary(c):
        try:
            p = normalize(tp.project(c["projectId"]))
            return {"projectId": p["projectId"], "title": p["title"], "status": p["status"],
                    "program": p["program"]["acronym"], "lastUpdated": p["lastUpdated"], "url": p["url"]}, True
        except SourceError:
            return {"projectId": c["projectId"], "title": None, "status": None, "program": None,
                    "lastUpdated": iso_date(c.get("lastUpdated")), "url": None}, False

    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(summary, top))
    result = {"since": since, "changed": len(changed), "offset": offset, "returned": len(fetched),
              "projects": [row for row, _ in fetched],
              "data": _live({"note": "TechPort lists changed projects; it doesn't say whether each is new or updated."})}
    failed = [row["projectId"] for row, ok in fetched if not ok]
    if failed:
        result["note"] = f"Details could not be fetched for projects {failed}; only their dates are shown."
    return output.text(output.limit_list(result, "projects", offset=offset))


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_find_contacts(name: str, include_emails: bool = False, limit: int = 20, offset: int = 0) -> str:
    """Find a person by name across TechPort project and program contacts: their
    roles and the projects they're on (pass projectIds to techport_get_project).
    Emails and ORCIDs only with include_emails. Up to 50 people per call, offset
    to page. Runs on the daily copy."""
    try:
        db, meta = _open_copy()
        try:
            result = queries.find_contacts(db, name=name, include_emails=include_emails, limit=limit, offset=offset)
        finally:
            db.close()
        result["data"] = _copy_notice(meta)
        return output.text(output.limit_list(result, "people", offset=result["offset"]))
    except queries.QueryError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------- reference data


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_programs(program_id: int | None = None, active_only: bool = True) -> str:
    """NASA programs in TechPort. Without program_id: all programs (programId,
    acronym, title, mission directorate). With program_id: the description and
    program contacts (names and roles). Use the acronym as the program filter
    elsewhere, or the programId when an acronym is shared by two programs (PSRP, SBP)."""
    try:
        if program_id is not None:
            p = tp.program(program_id)
            contacts = [{"name": c.get("fullName"), "role": c.get("programContactRolePretty")}
                        for c in p.get("programContacts") or []]
            return output.text({"program": {
                "programId": p.get("programId"), "acronym": p.get("acronym"), "title": p.get("title"),
                "missionDirectorate": p.get("responsibleMdAcronym"), "isActive": p.get("isActive"),
                "description": strip_html(p.get("description")),
                "contacts": output.table(contacts) if contacts else []}, "data": _live()})
        progs = tp.programs(active_only)
        result = {"programs": [{"programId": p.get("programId"), "acronym": p.get("acronym"), "title": p.get("title"),
                                "missionDirectorate": p.get("responsibleMdAcronym"), "isActive": p.get("isActive")}
                               for p in progs], "count": len(progs), "data": _live()}
        return output.text(output.limit_list(result, "programs"))
    except SourceError as e:
        _fail(e)


# Seen 2026-10-01: Interlune Corporation has no UEI in TechPort, but has one in USAspending.
NO_UEI = ("A null uei means TechPort has none for that organization, not that it has none: newer companies "
          "often lack one here. Link it to SBIR or USAspending by name (sbir_search_awards company=, "
          "usaspending_search_awards company=) and say the match is by name.")


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_organizations(
    name: str | None = None,
    acronym: str | None = None,
    uei: str | None = None,
    cage_code: str | None = None,
    organization_id: int | None = None,
    limit: int = 20,
) -> str:
    """Look up organizations in TechPort by name, acronym, UEI, CAGE code or
    organizationId: type, location, DUNS, UEI and CAGE. UEI links a company to
    the SBIR tools. For how many projects an organization leads, use
    techport_aggregate with lead_organization."""
    def shape(o):
        st = o.get("stateTerritory") or {}
        return {"organizationId": o.get("organizationId"), "name": o.get("organizationName"),
                "type": o.get("organizationTypePretty") or o.get("organizationType"), "city": o.get("city"),
                "state": st.get("abbreviation"), "country": (o.get("country") or {}).get("name"),
                "zip": o.get("zipCode"), "uei": o.get("uei"), "duns": o.get("dunsNumber"),
                "cageCode": o.get("cageCode"), "congressionalDistrict": o.get("congressionalDistrict")}
    try:
        if organization_id is not None:
            org = shape(tp.organization(organization_id))
            result = {"organization": org, "data": _live()}
            if not org["uei"] and org["type"] != "NASA Center":
                result["note"] = NO_UEI
            return output.text(result)
        if not any((name, acronym, uei, cage_code)):
            raise ToolError("Give a name, acronym, uei, cage_code or organization_id.")
        orgs = tp.organizations(name=name, acronym=acronym, uei=uei, cage=cage_code,
                                limit=max(1, min(int(limit), 100)))
        result = {"organizations": [shape(o) for o in orgs], "returned": len(orgs), "data": _live()}
        if any(not o["uei"] for o in result["organizations"]):
            result["note"] = NO_UEI
        return output.text(output.limit_list(result, "organizations"))
    except SourceError as e:
        _fail(e)


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_capabilities(capability: str | None = None, query: str | None = None, offset: int = 0) -> str:
    """NASA's technology capability areas and shortfalls: the "future state", what
    NASA says it still needs. No arguments: the 19 capability areas. capability
    (part of a name): that area's shortfalls with priorities and full
    descriptions, and the fileId of its gap document (read it with
    techport_get_document). query: shortfalls matching a phrase (a phrase match,
    so single words find more), descriptions cut to 200 characters; offset to
    page."""
    try:
        if query:
            found = tp.shortfalls(query)
            offset = max(0, int(offset))
            rows = [{**f, "description": (f.get("description") or "")[:200] or None} for f in found[offset:]]
            result = {"total": len(found), "offset": offset, "returned": len(rows), "shortfalls": rows,
                      "data": _live({"note": "Descriptions are cut to 200 characters; capability=<its capability> "
                                             "gives them in full."})}
            return output.text(output.limit_list(result, "shortfalls", as_table=output.uniform(rows), offset=offset))
        s = tp.strategy()
        caps = s.get("capabilities") or []
        if not capability:
            result = {"capabilities": [{"capabilityId": c.get("capabilityId"), "title": c.get("title"),
                                        "description": c.get("description"), "fileId": c.get("fileId")} for c in caps],
                      "shortfalls_total": len(s.get("shortfalls") or []), "data": _live()}
            return output.text(output.limit_list(result, "capabilities"))
        want = capability.strip().lower()
        chosen = [c for c in caps if want in (c.get("title") or "").lower()]
        # Upstream: shortfalls name their capability differently from the list
        # ("Small Spacecraft" vs "Small Spacecraft Technologies"), so match loosely.
        shortfalls = [f for f in s.get("shortfalls") or []
                      if want in (f.get("capability") or "").lower()
                      or any((f.get("capability") or "").lower() in (c.get("title") or "").lower() for c in chosen)]
        if not chosen and not shortfalls:
            raise ToolError(f"No capability area matches {capability!r}. Call with no arguments for the list.")
        result = {"capabilities": chosen, "shortfalls": shortfalls, "returned": len(shortfalls), "data": _live()}
        return output.text(output.limit_list(result, "shortfalls", as_table=output.uniform(shortfalls)))
    except SourceError as e:
        _fail(e)


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_opportunities(opportunity_id: int | None = None, offset: int = 0) -> str:
    """NASA funding opportunities listed in TechPort (about 36): name, description,
    typical amount and duration, frequency, next solicitation, eligible roles.
    Descriptions are shortened in the list; opportunity_id gives the full record.
    The list may take two calls: offset to page. Many lack TRL and role fields
    upstream."""
    try:
        if opportunity_id is not None:
            return output.text({"opportunity": tp.opportunity(opportunity_id), "data": _live()})
        opps = tp.opportunities()
        offset = max(0, int(offset))
        # The raw list is much larger; the list gives the essentials.
        keep = ("opportunityId", "name", "amount", "duration", "frequency", "nextSolicitation",
                "opportunityRole", "trlValues", "url")
        rows = [{**{k: o.get(k) for k in keep},
                 "directorate": (o.get("directorate") or {}).get("acronym"),     # a full org record upstream
                 "description": (o.get("description") or "")[:200] or None}
                for o in opps[offset:]]
        result = {"total": len(opps), "offset": offset, "returned": len(rows), "opportunities": rows,
                  "data": _live({"note": "Descriptions are cut to 200 characters; opportunity_id gives the full record."})}
        return output.text(output.limit_list(result, "opportunities", offset=offset))
    except SourceError as e:
        _fail(e)


@server.tool(annotations=READ_ONLY, structured_output=False)
def techport_classify_technology(title: str, description: str) -> str:
    """Suggest a NASA Technology Taxonomy code (TX) for a technology, using
    TechPort's own TREX model. The code works as technology_area in the other
    tools. TREX returns one code with no confidence score, and always returns
    something even for nonsense, so treat it as a suggestion."""
    try:
        raw = tp.classify(title, description)
        titles = tp.tx_titles()
    except SourceError as e:
        _fail(e)
    preds = [{"code": tx_code(k), "title": titles.get(tx_code(k)), "rank": v}
             for k, v in sorted(raw.items(), key=lambda kv: kv[1])] if isinstance(raw, dict) else []
    return output.text({"predictions": output.table(preds) if preds else [], "data": _live({"model": "TechPort TREX"})})


def main():
    server.run()
