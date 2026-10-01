"""MCP server for SEC EDGAR: companies' filings, full-text search of them, and
their reported financials. Runs over stdio.

Live only, no local copy. The only hosts contacted are SEC's (see api.py), and
only with the contact the user set, which SEC requires. "SEC EDGAR connector"
in docs/design.md has the probe results behind every rule here.
"""

import os
import re
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..common import output, text
from ..common.http import SourceError
from . import api as api_mod, model

INSTRUCTIONS = """\
SEC EDGAR: filings by companies registered with the SEC (annual and quarterly
reports, 8-Ks, prospectuses, Form D notices of private fundraising), full-text
search of them from 2001 on, and reported financials. Live from SEC.

GROUNDING RULE: every claim about a company's filings or figures must come from
a tool result; quote figures with their year and source. Use the counts that
come with a search; don't tally hits yourself. If the tools can't answer, say so.

- Every call needs the user's SEC EDGAR contact (name and email), a plugin
  option. If a tool says it is missing or has no email, tell the user how to
  set it, as the message says, before trying other EDGAR tools.
- Most SBIR companies are private and file little or nothing with the SEC.
  EDGAR helps most with public companies. EDGAR has no UEI, so link a company
  from the SBIR or USAspending tools by its name or ticker, and say when a
  match is by name only.
- Full-text search needs every word; "quote a phrase". Phrases match in any
  case, so a program name that is also a common phrase ("Flight
  Opportunities") finds unrelated filings too; add a word such as NASA, or a
  form filter, and use the passages to tell them apart. It covers each
  document of a filing, exhibits included. Hits carry no text: to judge what
  they say, ask edgar_search for passages rather than opening each filing.
- Filings are long (a 10-K is several hundred thousand characters):
  edgar_read_filing with find gives the passages and their pages; read parts
  only as needed, and say which parts you read.
- Financials come from the figures companies tag in their reports (XBRL), by
  calendar year, in US dollars; small companies' and older reports may have
  none.
- Lists come as tables: column names once, then one row of values per item. A
  long list stops at a size limit, with a note saying how to get the rest.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

server = MCPServer(name="edgar", instructions=INSTRUCTIONS)
edgar = None                    # made on first use, with the user's contact
MAX_LIMIT = 50
MAX_PASSAGE_HITS = 10           # documents fetched for one search's passages
CONTACT_VARS = ("SEC_CONTACT", "CLAUDE_PLUGIN_OPTION_SEC_CONTACT")
HOW_TO_SET = ("Set \"SEC EDGAR contact\" in Claude Code's /config, under this plugin's options, as your name "
              "and email (Jane Doe jane@example.com), then restart Claude Code. It is sent only to SEC.")
NO_CONTACT = "SEC requires a contact (your name and email) with every request to EDGAR. " + HOW_TO_SET
NO_EMAIL = "The SEC EDGAR contact that is set has no email address, and SEC requires one. " + HOW_TO_SET
UNUSABLE = ("The SEC EDGAR contact that is set can't be sent: it must be one line of plain Latin letters, "
            "with an email address. " + HOW_TO_SET)
FRAMES = ("Years are SEC's calendar-year frames: for a fiscal year that doesn't end in December, the closest 12 "
          "months. Amounts in US dollars; cash and total assets at year end.")


def _contact():
    """(contact, problem): the first usable contact, or None and the message
    that says what is wrong with the one that is set, if any."""
    problem = NO_CONTACT
    for name in CONTACT_VARS:
        value = os.environ.get(name)
        found = model.contact(value)
        if found:
            return found, None
        if value and value.strip() and "${" not in value:      # set, but not usable
            problem = NO_EMAIL if "@" not in value else UNUSABLE
    return None, problem


def _client():
    global edgar
    found, problem = _contact()
    if not found:
        raise ToolError(problem)
    if edgar is None:
        edgar = api_mod.Edgar(found)
    return edgar


def _data():
    return {"source": "SEC EDGAR (sec.gov), live", "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


NOT_BY_NAME = ("Private companies often don't file with the SEC at all, so a near match by name may be an "
               "unrelated filer (a fund, a shell or a series vehicle with a similar name).")


def _names_it(name, company):
    """Whether a filer's name starts with the name asked for, word for word:
    "Virgin Galactic Holdings, Inc" names Virgin Galactic; "American Ventures
    ... Fund LLC, Series V Blue Origin" doesn't name Blue Origin."""
    words, have = re.findall(r"\w+", company.casefold()), re.findall(r"\w+", name.casefold())
    return bool(words) and have[:len(words)] == words


def _resolve(e, company):
    """(cik, candidates): a company from its CIK, ticker or name. A name is
    looked up in SEC's ticker list, then in EDGAR's own index."""
    found = model.cik(company)
    if found:
        return found, []
    matches = model.from_tickers(e.tickers(), company)
    if len(matches) == 1:
        return matches[0]["cik"], []
    if not matches:
        matches = model.suggestions(e.suggest(company))
        exact = [m for m in matches if m["name"].casefold() == company.strip().casefold()]
        if len(exact) == 1 or len(matches) == 1:
            return (exact or matches)[0]["cik"], []
    if not matches:
        raise ToolError(f"No company matches {company!r} in SEC's lists. Most private companies, including "
                        "most SBIR firms, don't file with the SEC.")
    return None, matches[:20]


def _one_company(e, company):
    found, candidates = _resolve(e, company)
    if found is None:
        raise ToolError(f"Several companies match {company!r}: "
                        + "; ".join(f"{c['name']} (CIK {c['cik']}" + (f", {c['ticker']})" if c["ticker"] else ")")
                                    for c in candidates[:8]) + ". Ask again with a CIK or ticker.")
    return found


@server.tool(annotations=READ_ONLY, structured_output=False)
def edgar_company(company: str, forms: list[str] | None = None, since: str | None = None, limit: int = 20,
                  offset: int = 0) -> str:
    """A company registered with the SEC, by name, ticker or CIK: its profile
    (industry code, location, tickers, former names) and its recent filings,
    newest first.

    forms: e.g. ["10-K", "10-Q", "8-K", "S-1", "D"]; a form brings its
    amendments too (10-K/A). since: YYYY-MM-DD. Filings come as a table:
    form, filed, period, description, 8-K items, accession, document. Pass the
    accession and CIK to edgar_read_filing. limit up to 50; offset to page.
    A name that fits several companies returns the candidates.
    """
    try:
        e = _client()
        found, candidates = _resolve(e, company)
        if found is None:
            note = f"Several companies match {company!r}; ask again with a CIK or ticker."
            if not any(_names_it(c["name"], company) for c in candidates):
                note += " " + NOT_BY_NAME
            return output.text({"candidates": output.table(candidates), "note": note, "data": _data()})
        sub = e.submissions(found)
        rows = model.filings(sub, forms=forms, since=since)
        limit, offset = max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))
        page = rows[offset:offset + limit]
        result = {"company": model.profile(sub), "filings_total": len(rows), "offset": offset, "returned": len(page),
                  "filings": page}
        notes = []
        prof = result["company"]
        names = [prof.get("name") or ""] + [f["name"] or "" for f in prof["former_names"]]
        if not model.cik(company) and not any(_names_it(n, company) for n in names) \
                and company.strip().upper() not in [t.upper() for t in prof.get("tickers") or []]:
            notes.append(f"The name found isn't {company!r}: EDGAR's name index gave its closest match. "
                         "Check it is the company meant. " + NOT_BY_NAME)
        if (sub.get("filings") or {}).get("files"):
            notes.append("These are the recent filings SEC lists here (about 1,000); older ones exist.")
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return output.text(output.limit_list(result, "filings", offset=offset))
    except (model.QueryError, SourceError) as err:
        raise ToolError(str(err)) from err


def _passages(e, rows, query, wanted):
    """Adds passage and found to every row, filled for the first wanted rows.
    Returns the hits whose document couldn't be read, with why."""
    failed = []
    for i, row in enumerate(rows):
        row["passage"], row["found"] = None, None
        if i >= wanted:
            continue
        if row["document"] and not model.readable(row["document"]):
            url = model.archive_url(row["cik"], row["accession"], row["document"])
            failed.append(f"{row['accession']} ({model.unreadable_reason(row['document'], url)})")
            continue
        try:
            body = model.filing_text(e.document(row["cik"], row["accession"], model.document_name(row["document"])))
            best = model.best_passage(body, query)
            row["passage"], row["found"] = (best["passage"], best["found"]) if best else (None, 0)
        except (SourceError, model.QueryError) as err:
            failed.append(f"{row['accession']} ({err})")
    return failed


@server.tool(annotations=READ_ONLY, structured_output=False)
def edgar_search(query: str, forms: list[str] | None = None, year_from: int | None = None,
                 year_to: int | None = None, company: str | None = None, limit: int = 20, offset: int = 0,
                 passages: int = 0) -> str:
    """Full-text search of SEC filings, 2001 on, every document of a filing
    (exhibits included).

    query: words that must all appear; "quote a phrase", e.g.
    "SBIR Phase III" NASA. forms: e.g. ["10-K", "10-Q", "8-K"]. year_from /
    year_to: filing years. company: a name, ticker or CIK.
    Returns the total, counts by company, form and state for all matches, and
    one page of hits as a table: company, CIK, form, document type, filed,
    period, description, accession, document. limit up to 50; offset to page.
    passages: for the first N hits (up to 10), the passage of the matched
    document that best shows the search, and how often its words appear
    there. Use it to judge what hits say without opening each filing; it
    takes a few seconds.
    """
    try:
        e = _client()
        found = _one_company(e, company) if company else None
        offset = max(0, int(offset))
        d = e.search(model.search_params(query, forms=forms, year_from=year_from, year_to=year_to, cik=found,
                                         offset=offset))
        total = (d.get("hits") or {}).get("total") or {}
        rows = model.hits(d)[:max(1, min(int(limit), MAX_LIMIT))]
        notes = []
        if passages:
            if int(passages) > MAX_PASSAGE_HITS:
                notes.append(f"passages is capped at {MAX_PASSAGE_HITS}.")
            failed = _passages(e, rows, query, min(int(passages), MAX_PASSAGE_HITS))
            if failed:
                notes.append("Passages not read for: " + "; ".join(failed) + ".")
        result = {"total": total.get("value", 0), "offset": offset, "returned": len(rows), **model.search_counts(d),
                  "hits": rows}
        if total.get("relation") == "gte":
            notes.insert(0, f"At least {total.get('value', 0):,} matches: SEC counts no further. "
                            "Narrow the search for an exact count.")
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return output.text(output.limit_list(result, "hits", offset=offset))
    except (model.QueryError, SourceError) as err:
        raise ToolError(str(err)) from err


@server.tool(annotations=READ_ONLY, structured_output=False)
def edgar_financials(company: str) -> str:
    """A company's key annual figures from its XBRL-tagged reports: revenue,
    R&D expense, operating income, net income, cash and total assets, by
    calendar year, in US dollars.

    company: a name, ticker or CIK. Companies name the same figure differently,
    and change names over time; concepts says which concept each year used,
    and the note says when a measure's years don't all use the same one.
    """
    try:
        e = _client()
        found = _one_company(e, company)
        try:
            facts = e.facts(found)
        except SourceError as err:
            if "404" in str(err):
                raise ToolError(f"SEC has no XBRL financial data for CIK {found}: it may not file tagged reports "
                                "(small and older filers often don't).") from err
            raise
        f = model.financials(facts)
        notes = [FRAMES]
        for measure in f["mixed"]:
            notes.append(f"{measure}: different years use different concepts (see concepts), which may not be "
                         "defined alike; compare years that use the same one.")
        if f["not_usd"]:
            notes.append(f"Left out, reported in other currencies: {', '.join(f['not_usd'])}.")
        result = {"company": {"cik": found, "name": facts.get("entityName")}, "years": f["years"],
                  "concepts": f["concepts"], "note": " ".join(notes), "data": _data()}
        return output.text(output.limit_list(result, "years"))
    except (model.QueryError, SourceError) as err:
        raise ToolError(str(err)) from err


@server.tool(annotations=READ_ONLY, structured_output=False)
def edgar_read_filing(cik: str | int, accession: str, document: str | None = None, part: int = 1,
                      find: str | None = None) -> str:
    """A filing document as text, in parts of up to 12,000 characters, or only
    the passages that mention given words.

    cik and accession: from edgar_company or edgar_search. document: the file
    to read (from edgar_search, which may point to an exhibit); by default the
    filing's main document. find: whole words or phrases, any case, with |
    between alternatives and * for a prefix; returns each passage with its part
    and page, to read in full next. [page N] marks where page N of the
    document starts.
    """
    try:
        found = model.cik(cik)
        if not found:
            raise ToolError(f"cik is the company's SEC number, such as 1819994; not {cik!r}.")
        number = model.accession(accession)
        name = model.document_name(document) if document else None
        if name and not model.readable(name):
            raise ToolError(model.unreadable_reason(name, model.archive_url(found, number, name)))
        e = _client()
        if not name:
            name = model.primary_document(e.submissions(found), number)
            if not name:
                raise ToolError("This filing isn't among the company's recent ones, so give its document name "
                                "(edgar_search lists it).")
            if not model.readable(name):
                raise ToolError(model.unreadable_reason(name, model.archive_url(found, number, name)))
        body = model.filing_text(e.document(found, number, name))
        spans = text.split(body)
        result = {"cik": found, "accession": number, "document": name,
                  "url": model.archive_url(found, number, name), "characters": len(body), "parts": len(spans)}
        if find:
            try:
                found_passages = text.passages(body, find, spans=spans)
            except text.NoWords as err:
                raise ToolError(str(err)) from err
            result.update(find=find, matches=sum(p["matches"] for p in found_passages), passages=found_passages)
            if not found_passages:
                result["note"] = "No matches. The filing may use other words."
            result["data"] = _data()
            return output.text(output.limit_list(result, "passages", continue_with=lambda left: (
                f"{len(left)} more passages, from part {left[0]['part']} on: read those parts, or narrow find.")))
        if not 1 <= int(part) <= max(len(spans), 1):
            raise ToolError(f"part must be 1 to {len(spans)}.")
        start, end = spans[int(part) - 1] if spans else (0, 0)
        first, last = text.page_at(body, start), text.page_at(body, max(start, end - 1))
        result.update(part=int(part), pages=None if first is None else f"{first}-{last}", text=body[start:end])
        if int(part) < len(spans):
            result["note"] = f"Part {part} of {len(spans)}: ask for part={int(part) + 1} to go on, or use find."
        result["data"] = _data()
        return output.text(result)
    except (model.QueryError, SourceError) as err:
        raise ToolError(str(err)) from err


def main():
    server.run()
