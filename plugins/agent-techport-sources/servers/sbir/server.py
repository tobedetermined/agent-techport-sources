"""MCP server for SBIR/STTR awards, from the SBIR.gov bulk file held locally.

Runs over stdio. The only network calls are the ones in store.py, to SBIR.gov.
"""

import os
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..common import output
from . import queries, store

INSTRUCTIONS = """\
SBIR and STTR awards from all agencies, from SBIR.gov's public award file,
held locally and refreshed when SBIR.gov publishes a new file (monthly).

- Agencies are codes: NASA, DOD, HHS, DOE, NSF, USDA, EPA, DOC, ED, DOT, DHS, NRC, DOI.
- Filter by award year, not award date; about half the rows have no award date.
- The file has no unique award id. Cite an award by agency tracking number plus
  contract number; sbir_get_award may return more than one row for a pair.
  The tools' own id is only for this copy of the file: it changes when the
  monthly file is reloaded, so never cite it or keep it for later.
- UEI links a company across agencies. Awards before 2015 often have no UEI.
- Use sbir_aggregate for any count or total. Don't add up search results.
- Contact details (emails, phones, titles) are left out unless asked for with
  include_contacts. Company and PI name are always shown.
- sbir_query runs one read-only SQL SELECT, for questions the other tools
  can't answer. Try them first, and show the SQL whenever an answer depends
  on it.
- Lists come as tables: column names once, then one row of values per item.
  A long list stops at a size limit, with a note saying how to get the rest.
"""

QUERY_DESCRIPTION = f"""\
Run one read-only SQL SELECT (SQLite) on the SBIR awards, for what the other
sbir tools can't answer: medians, shares, Phase I to Phase II transitions,
custom groupings. Try the other tools first, and show the SQL in your answer
whenever it depends on this tool.

Table awards, one row per row of SBIR.gov's file:
- Award: id (ours, changes with each monthly file; sbir_get_award takes it), award_title, abstract,
  agency_code (NASA, DOD, HHS, DOE, NSF, USDA, EPA, DOC, ED, DOT, DHS, NRC,
  DOI), agency (full name), branch (DoD components; NULL for NASA), program
  (SBIR or STTR), phase (1 or 2), agency_tracking_number, contract,
  award_year, award_amount (dollars; NULL when blank), topic_code,
  solicitation_number, solicitation_year.
- Dates, as YYYY-MM-DD text and often NULL: proposal_award_date,
  contract_end_date, solicitation_close_date, proposal_receipt_date,
  date_of_notification. Use award_year for time.
- Company: company, uei (links a company across agencies; often NULL before
  2015), duns, company_website, address1, address2, city, state (full name),
  state_code, zip, number_employees, hubzone_owned,
  socially_economically_disadvantaged, woman_owned (each Y, N or U).
- People: pi_name (principal investigator), ri_name (research institution,
  on STTR awards).
agency_tracking_number and contract together aren't unique: for NASA, a
Phase I and a Phase II often share them. SBIR.gov's placeholders 'N/A', 'NA',
'None' and 'null' are loaded as NULL (abstract is NULL on about 28,000 rows);
ri_name 'Stub', on about 15,000 rows, is kept as it is. Contact details aren't
available here; for one award's, use sbir_get_award with include_contacts.

Keyword search: awards_fts indexes award_title and abstract; its rowid is
awards.id.
  SELECT a.id, a.award_title FROM awards_fts JOIN awards a ON a.id = awards_fts.rowid
  WHERE awards_fts MATCH 'cryogenic AND tank' ORDER BY rank LIMIT 20
Broad prefix searches such as 'a*' are slow.

median() and percentile() work where SQLite includes them; an error says if
not. Limits: {queries.QUERY_SECONDS} seconds, values up to \
{queries.MAX_VALUE_BYTES // 1_000_000} MB, and up to {queries.QUERY_MAX_ROWS} rows or about \
{queries.QUERY_MAX_CHARS:,} characters per result, with a note when rows were left out.
Returns compact JSON: columns, rows, and the data's date and caveats.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

server = MCPServer(name="sbir", instructions=INSTRUCTIONS)

# The database is prepared in the background at start, so the server answers
# the client straight away even when the first download takes a while.
_ready = threading.Event()
_state: dict[str, Any] = {}


def _prepare():
    data_dir = store.default_data_dir()
    try:
        path, status = store.ensure_database(data_dir, csv_override=os.environ.get("SBIR_CSV_PATH") or None)
        _state.update(data_dir=data_dir, status=status, loaded_at=store.read_meta(path)["loaded_at"])
    except Exception as e:  # reported on the first tool call
        _state["error"] = e
    finally:
        _ready.set()


def _run(fn, **kwargs):
    if not _ready.wait(timeout=900):
        raise ToolError("The SBIR data is still downloading. Try again in a minute.")
    if "error" in _state:
        raise ToolError(str(_state["error"]))
    try:
        # Opened per call: another session may have refreshed the data since.
        db, meta = store.open_current(_state["data_dir"])
    except Exception as e:
        raise ToolError(f"Could not open the SBIR database: {e}") from e
    try:
        result = fn(db, **kwargs)
    except queries.QueryError as e:
        raise ToolError(str(e)) from e     # reaches the model as an error it can correct
    finally:
        db.close()
    # A "stale" status only applies while we're still on the database it was about.
    status = _state["status"] if meta.get("loaded_at") == _state["loaded_at"] else None
    result["data"] = queries.data_notice(meta, status)
    return result


@server.tool(annotations=READ_ONLY, structured_output=False)
def sbir_search_awards(
    agency: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    program: str | None = None,
    phase: int | None = None,
    company: str | None = None,
    uei: str | None = None,
    state: str | None = None,
    keywords: str | None = None,
    include_abstract: bool = False,
    sort: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find SBIR/STTR awards. All filters are optional and combine with AND.

    agency: code such as NASA or DOD. year_from/year_to: award year, inclusive.
    program: SBIR or STTR. phase: 1 or 2. company: part of the name, any case.
    uei: exact. state: two-letter code or full name. keywords: searched in the
    title and abstract, with the same syntax as NTRS: words must all appear;
    "a phrase"; OR or | between alternatives; (parentheses); -word to exclude;
    word* for a prefix.
    sort: relevance (best keyword match; the default with keywords), newest
    (the default otherwise), oldest, or amount (largest first). Returns the
    total number of matches and one page (limit up to 50, or 10 with
    include_abstract; offset to page).
    """
    result = _run(queries.search_awards, agency=agency, year_from=year_from, year_to=year_to,
                  program=program, phase=phase, company=company, uei=uei, state=state,
                  keywords=keywords, include_abstract=include_abstract, sort=sort,
                  limit=limit, offset=offset)
    return output.text(output.limit_list(result, "awards", offset=result["offset"]))


@server.tool(annotations=READ_ONLY, structured_output=False)
def sbir_get_award(
    id: int | None = None,
    agency_tracking_number: str | None = None,
    contract: str | None = None,
    agency: str | None = None,
    include_contacts: bool = False,
) -> str:
    """Full record for one award, including the abstract.

    Look up by the id from a search result (valid until the monthly file is
    reloaded, so don't cite it), or by agency tracking number and/or
    contract number; agency (a code such as NASA) narrows the match, since
    tracking numbers repeat across agencies. A number can match several rows
    (for NASA, a Phase I and Phase II pair); up to 20 are returned with the
    total. include_contacts adds titles, phones, emails and the agency and
    research-institution contacts.
    """
    result = _run(queries.get_award, id=id, agency_tracking_number=agency_tracking_number,
                  contract=contract, agency=agency, include_contacts=include_contacts)
    # Full records stay records: many fields, usually one or two rows.
    return output.text(output.limit_list(
        result, "awards", as_table=False,
        continue_with=lambda left: "Get the rest one at a time by id: "
                                   + ", ".join(str(a["id"]) for a in left) + "."))


@server.tool(annotations=READ_ONLY, structured_output=False)
def sbir_company(uei: str) -> str:
    """One company by UEI: name(s), latest address and ownership flags, and its
    awards summarised by agency, phase, program and year, with dollar totals."""
    result = _run(queries.company, uei=uei)
    for key in ("by_agency", "by_phase", "by_program", "by_year"):
        if key in result:
            result[key] = output.table(result[key])
    return output.text(result)


@server.tool(annotations=READ_ONLY, structured_output=False)
def sbir_aggregate(
    group_by: str,
    sort: str | None = None,
    limit: int | None = None,
    agency: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    program: str | None = None,
    phase: int | None = None,
    company: str | None = None,
    uei: str | None = None,
    state: str | None = None,
    keywords: str | None = None,
) -> str:
    """Count awards and total their dollars, grouped by year, phase, program,
    agency, state or company. Takes the same filters as sbir_search_awards.

    group_by="company" ranks companies (by UEI, named by their latest award):
    the top 20 by default, up to 100 with limit. sort: awards (the default for
    companies) or amount; other groupings are in natural order unless sorted.
    Also reports awards with no amount, and for companies, awards with no UEI,
    so totals aren't mistaken for complete.
    """
    result = _run(queries.aggregate, group_by=group_by, sort=sort, limit=limit, agency=agency,
                  year_from=year_from, year_to=year_to, program=program, phase=phase,
                  company=company, uei=uei, state=state, keywords=keywords)
    return output.text(output.limit_list(result, "groups"))


# Every tool returns compact JSON text rather than a dict, which the SDK would
# indent: size limits are then measured on exactly what the model reads.
@server.tool(annotations=READ_ONLY, structured_output=False, description=QUERY_DESCRIPTION)
def sbir_query(sql: str) -> str:
    return output.text(_run(queries.run_sql, sql=sql))


def main():
    threading.Thread(target=_prepare, name="sbir-prepare", daemon=True).start()
    server.run()
