"""MCP server for the NASA Technical Reports Server (NTRS). Runs over stdio.

Live API only, no local copy. The only host contacted is ntrs.nasa.gov (see
api.py). "NTRS connector" in docs/design.md has the probe results behind every
rule here.
"""

import os
import re
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations

from ..common import attach, output
from ..common.http import SourceError
from . import api as api_mod, model

INSTRUCTIONS = """\
NASA Technical Reports Server (NTRS): about 650,000 public records of NASA
research, from the 1910s on: technical reports and memoranda, contractor
reports, conference papers, presentations, journal articles (including about
39,000 from publishers, via CHORUS) and JPL records. Live from the official API.

GROUNDING RULE: every claim about what NASA published, by whom, where or when
must come from a tool result. For any count or ranking use ntrs_aggregate;
don't tally search results yourself. If the tools can't answer, say so.

- The query searches each record (title, abstract, keywords, authors), not the
  files' text. Words are all required; "quote a phrase"; | means or; -word
  excludes; a* matches a prefix; parentheses group.
- Filters match one exact value, in its exact case. Names come in many forms
  ("Jason Hartwig", "J. W. Hartwig", "Hartwig, Jason W."), and subjects differ
  in case. ntrs_aggregate grouped by author, organization or subject shows the
  forms in use; to search several at once, put them in the query:
  "Jason Hartwig" | "Hartwig, Jason W.".
- 59% of all records, mostly older ones, have no NASA center: their center is
  CDMS ("Legacy CDMS"). A center filter misses them.
- About 9% of records have no publication date, often conference papers,
  presentations and posters. Year filters leave them out (results say how many),
  and sorting by date puts them last.
- Documents: ntrs_get_record attaches a record's PDFs. ntrs_read_text gives the
  text NTRS extracted from a file, by part or as the passages that mention given
  words: the way into Word and PowerPoint files and PDFs too large to attach, and
  the quickest way to find a topic in a long report. Say which parts you read.
- Lists come as tables: column names once, then one row of values per item. A
  long list stops at a size limit, with a note saying how to get the rest.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

server = MCPServer(name="ntrs", instructions=INSTRUCTIONS)
ntrs = api_mod.NTRS()

MAX_LIMIT = 50
MAX_LIMIT_WITH_ABSTRACTS = 25           # rows with 300-character abstracts: about 25 fit the size limit
LATER = "not attached: this result already carries 11 MB of files; read its text with ntrs_read_text"
NO_PDF = "no PDF version; read its text with ntrs_read_text"
NO_PDF_OR_TEXT = "no PDF or text version; link only"
UNDATED = "often conference papers, presentations and posters"
# Measured in Claude Code 2.1.286: page images of an attached PDF cost about 1,490 tokens a page.
ATTACHED = ("The attached PDFs come with this result. Claude Code saves each one to a file and gives its path, to "
            "open a few pages at a time (about 1,500 tokens a page). To find a figure, table or section first, "
            "ntrs_read_text with find gives its page.")
SHARED = ("A record with several authors, organizations, subjects, keywords or funding numbers counts once "
          "for each, so rest counts values, not records.")


def _data():
    return {"source": "NASA Technical Reports Server (ntrs.nasa.gov), live",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _files_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "ntrs", "files")


def _record_id(value):
    rid = str(value).strip()
    if not re.fullmatch(r"\d{1,20}", rid):
        raise ToolError(f"record_id is the number NTRS gives each record, such as 20180002393 (from ntrs_search); "
                        f"not {value!r}.")
    return rid


def _undated_left_out(filters):
    """Records the same search finds without its years that have no publication date."""
    d = ntrs.search(model.search_body(**filters, limit=0))
    return model.counts(d.get("aggregations") or {}, "year", total=d["stats"]["total"])["undated"]


EXACT = ("subject", "author", "organization", "keyword")       # filters that have a grouping to check against


def _exact_case_hints(filters):
    """For a search that found nothing: the forms in use of its exact-value
    filters, found by searching without each one and reading its top 20
    values. One extra request per such filter, made only when nothing matched."""
    hints = []
    for name in EXACT:
        value = (filters.get(name) or "").strip()
        if not value:
            continue
        d = ntrs.search(model.search_body(**{**filters, name: None}, limit=0))
        groups = model.counts(d.get("aggregations") or {}, name, total=d["stats"]["total"])["groups"]
        used = [g for g in groups if g[name].casefold() == value.casefold() and g[name] != value]
        if used:
            forms = " or ".join(f"{g[name]!r} ({g['records']:,} records)" for g in used)
            hints.append(f"{name} {value!r} is written {forms} in NTRS, with the other filters")
        else:
            hints.append(f"{name} {value!r} isn't among the 20 commonest values with the other filters; "
                         f"ntrs_aggregate group_by={name} shows them")
    if not hints:
        return None
    return "Nothing matched. Exact-value filters take one value in its exact case: " + "; ".join(hints) + "."


def _filters(query, center, report_type, subject, author, organization, keyword, report_number, funding_number,
             has_files):
    return dict(query=query, center=center, report_type=report_type, subject=subject, author=author,
                organization=organization, keyword=keyword, report_number=report_number,
                funding_number=funding_number, has_files=has_files)


# ---------------------------------------------------------------- search


@server.tool(annotations=READ_ONLY, structured_output=False)
def ntrs_search(
    query: str | None = None,
    center: str | None = None,
    report_type: str | None = None,
    subject: str | None = None,
    author: str | None = None,
    organization: str | None = None,
    keyword: str | None = None,
    report_number: str | None = None,
    funding_number: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    has_files: bool = False,
    sort: str = "relevance",
    include_abstract: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find NTRS reports, papers and articles. All filters are optional and combine with AND.

    query: searches title, abstract, keywords and authors (not the files' text).
    Words are all required; "quote a phrase"; | for or ("boil-off" | boiloff);
    -word excludes; a* for a prefix; parentheses group.
    center: a NASA center code: ARC, AFRC, GRC, GSFC, HQ, JPL, JSC, KSC, LaRC,
    MSFC, SSC, WFF, WSTF, or CDMS for older records without a center.
    report_type: e.g. TECHNICAL_MEMORANDUM, TECHNICAL_PUBLICATION,
    CONTRACTOR_REPORT, CONFERENCE_PAPER, PRESENTATION, REPRINT,
    ACCEPTED_MANUSCRIPT, THESIS_DISSERTATION (any case).
    subject, author, organization, keyword, report_number, funding_number: one
    exact value each, in its exact case (ntrs_aggregate shows the forms in use).
    year_from/year_to: publication years, inclusive; records without a
    publication date are left out, and the result says how many.
    has_files: only records with files. sort: relevance (default), newest or
    oldest, by publication date (undated records last).
    Returns the total and one page as a table: id, title, type, published,
    center, first three authors, number of files. include_abstract adds the
    first 300 characters. limit up to 50 (25 with abstracts); offset to page.
    Only the first 10,000 results of a search can be reached.
    """
    try:
        cap = MAX_LIMIT_WITH_ABSTRACTS if include_abstract else MAX_LIMIT
        notes = []
        if int(limit) > cap:
            notes.append(f"limit is capped at {cap}" + (" with abstracts." if include_abstract else "."))
        limit = max(1, min(int(limit), cap))
        filters = _filters(query, center, report_type, subject, author, organization, keyword, report_number,
                           funding_number, has_files)
        d = ntrs.search(model.search_body(**filters, year_from=year_from, year_to=year_to, sort=sort,
                                          offset=offset, limit=limit))
        total = d["stats"]["total"]
        rows = [model.row(r, abstract=include_abstract) for r in d.get("results") or []]
        result = {"total": total, "offset": int(offset), "returned": len(rows), "records": rows}
        if total == 0:
            hint = _exact_case_hints({**filters, "year_from": year_from, "year_to": year_to})
            if hint:
                notes.append(hint)
        if year_from or year_to:
            left_out = _undated_left_out(filters)
            if left_out:
                notes.append(f"{left_out:,} more records match but have no publication date ({UNDATED}), so "
                             "the year filter left them out. Search without years to include them.")
        elif sort != "relevance":
            undated = model.counts(d.get("aggregations") or {}, "year", total=total)["undated"]
            if undated:
                notes.append(f"{undated:,} of these records have no publication date ({UNDATED}); "
                             "they come last in this order.")
        if total > model.MAX_RESULTS:
            notes.append(f"Only the first {model.MAX_RESULTS:,} results can be paged through; narrow the search "
                         "to reach the rest, or use ntrs_aggregate for counts.")
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return output.text(output.limit_list(result, "records", offset=int(offset)))
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------- one record


def _fetch(f, max_bytes):
    return ntrs.file(f["url"], max_bytes)


def _file_rows(rid, files, include):
    """(blocks, rows): the PDFs attached under the shared rules
    (servers/common/attach.py), and one row per file saying what happened."""
    pdfs = [{**f, "url": f["pdf"], "extension": "pdf", "bytes": None, "save_as": f"{rid}-{f['file']}"}
            for f in files if f["pdf"]]
    blocks, report = [], []
    if include and pdfs:
        blocks, report = attach.attach(pdfs, _fetch, fields=("file",), later=LATER, folder=_files_dir())
    outcome = {r["file"]: r for r in report}
    rows = []
    for f in files:
        r = outcome.get(f["file"])
        if r:
            note = r["reason"]
            if note and not r["saved_to"] and f["text"] and "ntrs_read_text" not in note:
                note += "; read its text with ntrs_read_text"
        elif f["pdf"]:
            note = "not fetched (include_documents is false)"
        else:
            note = NO_PDF if f["text"] else NO_PDF_OR_TEXT
        rows.append({"file": f["file"], "name": f["name"], "type": f["type"], "format": f["format"],
                     "url": f["pdf"] or f["original"], "text": bool(f["text"]), "bytes": r["bytes"] if r else None,
                     "attached": bool(r and r["included"]), "saved_to": r["saved_to"] if r else None, "note": note})
    return blocks, rows


@server.tool(annotations=READ_ONLY)
def ntrs_get_record(record_id: str | int, include_documents: bool = True) -> list:
    """One NTRS record in full: title, type, publication date, the whole
    abstract, authors and their organizations (the first 50), center, subjects,
    keywords, funding and report numbers, journal or meeting (with DOI), related
    records, and its files.

    The record's PDFs come attached by default, up to 11 MB per result (Claude
    Code's limit). A PDF of 11-20 MB is saved locally and its path given; over
    20 MB, the link only. Word and PowerPoint files without a PDF version, and
    PDFs too large to attach, can be read with ntrs_read_text, as can any file
    with text (the files table says which). Set include_documents=false to skip
    the download, e.g. when only the abstract or authors are needed.
    record_id: the id from ntrs_search, e.g. 20180002393.
    """
    try:
        rid = _record_id(record_id)
        d = ntrs.record(rid)
        rec = model.record(d)
        files = model.files(d)
        blocks, rows = _file_rows(rid, files, include_documents)
        notes = [ATTACHED] if blocks else []
        if not files:
            dois = [p["doi"] for p in rec["publications"] if p.get("doi")]
            notes.append("NTRS has no file for this record." + (f" The publication's DOI: {', '.join(dois)}."
                                                                 if dois else ""))
        if "authors_total" in rec:      # last: it ends with a link
            notes.append(f"Showing the first {len(rec['authors'])} of {rec['authors_total']:,} authors; the "
                         f"record page lists them all: {rec['page']}")
        rec["authors"] = output.table(rec["authors"]) if rec["authors"] else []
        result = {"record": rec, "files": output.table(rows) if rows else []}
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return [TextContent(type="text", text=output.text(result)), *blocks]
    except SourceError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------- text


@server.tool(annotations=READ_ONLY, structured_output=False)
def ntrs_read_text(record_id: str | int, file: int = 1, part: int = 1, find: str | None = None) -> str:
    """The text NTRS extracted from one of a record's files, in parts of up to
    12,000 characters, or only the passages that mention given words.

    file: the file's number in ntrs_get_record's files table (default 1, the
    report itself). part: which part to read (the result says how many there
    are). find: whole words or phrases to look for, any case, with | between
    alternatives and * for a prefix (cryogen*); returns each passage with its
    part and page, to read in full next. When the text has page marks, [page N] shows where page N of the PDF
    starts. Figures, and some tables and equations, are missing from extracted
    text: for those, read the PDF pages themselves.
    """
    try:
        rid = _record_id(record_id)
        files = model.files(ntrs.record(rid))
        if not files:
            raise ToolError("This record has no files at NTRS.")
        if not 1 <= int(file) <= len(files):
            raise ToolError(f"file must be 1 to {len(files)}; ntrs_get_record lists them.")
        f = files[int(file) - 1]
        if not f["text"]:
            raise ToolError(f"File {f['file']} ({f['name']}) has no text version; its link: {f['original']}")
        text = model.clean_text(ntrs.text(f["text"]))
        spans = model.split(text)
        result = {"record": rid, "file": f["file"], "name": f["name"], "characters": len(text), "parts": len(spans)}
        if find:
            found = model.passages(text, find, spans=spans)
            result.update(find=find, matches=sum(p["matches"] for p in found), passages=found)
            if not found:
                result["note"] = ("No matches. The text may use other words, or the words may be in figures "
                                  "or tables that weren't extracted.")
            result["data"] = _data()
            return output.text(output.limit_list(result, "passages", continue_with=lambda left: (
                f"{len(left)} more passages, from part {left[0]['part']} on: read those parts, or narrow find.")))
        if not 1 <= int(part) <= max(len(spans), 1):
            raise ToolError(f"part must be 1 to {len(spans)}.")
        start, end = spans[int(part) - 1] if spans else (0, 0)
        first, last = model.page_at(text, start), model.page_at(text, max(start, end - 1))
        result.update(part=int(part), pages=None if first is None else f"{first}-{last}", text=text[start:end])
        if int(part) < len(spans):
            result["note"] = f"Part {part} of {len(spans)}: ask for part={int(part) + 1} to go on, or use find."
        result["data"] = _data()
        return output.text(result)
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------- counts


@server.tool(annotations=READ_ONLY, structured_output=False)
def ntrs_aggregate(
    group_by: str,
    query: str | None = None,
    center: str | None = None,
    report_type: str | None = None,
    subject: str | None = None,
    author: str | None = None,
    organization: str | None = None,
    keyword: str | None = None,
    report_number: str | None = None,
    funding_number: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    has_files: bool = False,
) -> str:
    """Counts of NTRS records by group, for any search (the same filters as
    ntrs_search). One request, any number of records.

    group_by: center, report_type, subject, year (publication year), author,
    organization, keyword or funding_number. Years come in order, all of them,
    with the number of undated records. Other groups are the top 20 (centers:
    all), with rest counting the matches outside them. Grouping by author,
    organization or subject shows the exact forms in use, to pass to filters.
    """
    try:
        filters = _filters(query, center, report_type, subject, author, organization, keyword, report_number,
                           funding_number, has_files)
        d = ntrs.search(model.search_body(**filters, year_from=year_from, year_to=year_to, limit=0))
        total = d["stats"]["total"]
        counts = model.counts(d.get("aggregations") or {}, group_by, total=total)
        result = {"group_by": group_by, "total": total, "groups": counts["groups"]}
        notes = []
        if "undated" in counts:
            result["undated"] = counts["undated"]
            notes.append(f"undated: records without a publication date ({UNDATED}).")
        if "rest" in counts:
            result["rest"] = counts["rest"]
            notes.append("The top 20 groups; rest counts the matches outside them."
                         + (f" {SHARED}" if group_by not in ("report_type",) else ""))
        if group_by == "center":
            notes.append("CDMS (\"Legacy CDMS\") holds older records that have no NASA center.")
        if year_from or year_to:
            left_out = _undated_left_out(filters)
            if left_out:
                notes.append(f"The year filter left out {left_out:,} matching records that have no "
                             "publication date.")
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return output.text(output.limit_list(result, "groups"))
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


def main():
    server.run()
