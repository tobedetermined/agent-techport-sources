"""The queries behind the SBIR tools. Standard library only.

Each function takes an open sqlite3 connection and returns plain dicts, so it
can be tested without the MCP SDK. All counting and summing happens in SQL.
"""

import re
import sqlite3
import time
from datetime import date

from ..common import keywords
from ..common.output import MAX_CHARS, text
from .store import AGENCY_CODES, COLUMNS, CONTACT_COLUMNS, STATE_CODES

# Page caps. The output step (common/output.py) also stops every list at a
# size limit; real token counts are in docs/design.md, "Result sizes".
MAX_LIMIT = 50
MAX_LIMIT_WITH_ABSTRACT = 10
MAX_GET_ROWS = 20
DEFAULT_COMPANY_GROUPS = 20
MAX_COMPANY_GROUPS = 100

# Fields in search results. get_award returns every column.
SUMMARY_FIELDS = [
    "id", "company", "uei", "agency_code", "program", "phase", "award_title",
    "agency_tracking_number", "contract", "award_year", "proposal_award_date",
    "award_amount", "city", "state_code", "pi_name", "ri_name",
]

# Ties break on id, so paging is stable.
SORTS = {
    "relevance": "awards_fts.rank, a.award_year DESC, a.id",   # FTS5's bm25 score
    "newest": "a.award_year DESC, a.id",
    "oldest": "a.award_year, a.id",
    "amount": "a.award_amount IS NULL, a.award_amount DESC, a.id",   # largest first, no amount last
}

GROUPS = {
    "year": "award_year",
    "phase": "phase",
    "program": "program",
    "agency": "agency_code",
    "state": "state_code",
}

CODE_BY_NAME = {name.lower(): code for name, code in AGENCY_CODES.items()}
STATE_BY_NAME = {name.lower(): code for name, code in STATE_CODES.items()}


class QueryError(ValueError):
    """A filter value the data can't match. The message says what is accepted."""


# ---------------------------------------------------------------- filters


def _agency(value):
    v = value.strip()
    if v.upper() in AGENCY_CODES.values():
        return v.upper()
    if v.lower() in CODE_BY_NAME:
        return CODE_BY_NAME[v.lower()]
    raise QueryError(f"Unknown agency {value!r}. Use one of: {', '.join(sorted(AGENCY_CODES.values()))}.")


def _state(value):
    v = value.strip()
    if v.upper() in STATE_CODES.values():
        return v.upper()
    if v.lower() in STATE_BY_NAME:
        return STATE_BY_NAME[v.lower()]
    raise QueryError(f"Unknown state {value!r}. Use a two-letter code or full name.")


def _phase(value):
    v = str(value).strip().upper().removeprefix("PHASE").strip()
    if v in ("1", "I"):
        return 1
    if v in ("2", "II"):
        return 2
    raise QueryError(f"Unknown phase {value!r}. Use 1 or 2.")


def _program(value):
    v = value.strip().upper()
    if v not in ("SBIR", "STTR"):
        raise QueryError(f"Unknown program {value!r}. Use SBIR or STTR.")
    return v


def fts_query(text):
    """Free text to a safe FTS5 query, in the syntax shared with the other
    servers (common/keywords.py)."""
    try:
        return keywords.fts_query(text)
    except keywords.KeywordError as e:
        raise QueryError(str(e)) from e


def _where(agency=None, year_from=None, year_to=None, program=None, phase=None,
           company=None, uei=None, state=None, keywords=None):
    clauses, args = [], []
    if agency:
        clauses.append("a.agency_code = ?"); args.append(_agency(agency))
    if year_from is not None:
        clauses.append("a.award_year >= ?"); args.append(int(year_from))
    if year_to is not None:
        clauses.append("a.award_year <= ?"); args.append(int(year_to))
    if program:
        clauses.append("a.program = ?"); args.append(_program(program))
    if phase is not None and phase != "":
        clauses.append("a.phase = ?"); args.append(_phase(phase))
    if company:
        clauses.append("a.company LIKE ? ESCAPE '\\'")
        args.append("%" + re.sub(r"([%_\\])", r"\\\1", company.strip()) + "%")
    if uei:
        clauses.append("a.uei = ?"); args.append(uei.strip().upper())
    if state:
        clauses.append("a.state_code = ?"); args.append(_state(state))
    if keywords:
        clauses.append("a.id IN (SELECT rowid FROM awards_fts WHERE awards_fts MATCH ?)")
        args.append(fts_query(keywords))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


# ---------------------------------------------------------------- notices


def data_notice(meta, status=None):
    """What every response says about the data it came from."""
    year = date.today().year
    notice = {
        "source": meta.get("source"),
        "file_last_modified": meta.get("source_last_modified"),
        "loaded_at": meta.get("loaded_at"),
        "caveats": [
            f"Awards for {year} may be incomplete: SBIR.gov updates the file monthly "
            "and agencies report with a lag.",
            "Time filters use award year; about half the rows have no award date.",
        ],
    }
    if status and status.startswith("stale"):
        notice["caveats"].insert(0, f"Could not refresh from SBIR.gov ({status}); "
                                    "these results come from the last file downloaded.")
    return notice


def _rows(cursor):
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def _visible(row, include_contacts):
    if include_contacts:
        return row
    return {k: v for k, v in row.items() if k not in CONTACT_COLUMNS}


# ---------------------------------------------------------------- tools


def search_awards(db, *, limit=20, offset=0, include_abstract=False, sort=None, **filters):
    keywords = filters.pop("keywords", None)
    sort = (sort or ("relevance" if keywords else "newest")).strip().lower()
    if sort not in SORTS:
        raise QueryError(f"sort must be one of: {', '.join(SORTS)}.")
    if sort == "relevance" and not keywords:
        raise QueryError("sort=relevance needs keywords.")
    where, args = _where(**filters)
    cap = MAX_LIMIT_WITH_ABSTRACT if include_abstract else MAX_LIMIT
    requested = int(limit)
    limit = max(1, min(requested, cap))
    offset = max(0, int(offset))
    fields = [f"a.{f}" for f in SUMMARY_FIELDS] + (["a.abstract"] if include_abstract else [])
    if keywords:
        # Search the full-text index first and join the awards to it, best
        # matches first (FTS5's rank is its bm25 relevance score).
        source = "awards_fts JOIN awards a ON a.id = awards_fts.rowid"
        match = "awards_fts MATCH ?"
        where = f" WHERE {match}" + (" AND " + where[len(" WHERE "):] if where else "")
        args = [fts_query(keywords)] + args
    else:
        source = "awards a"
    order = SORTS[sort]
    total = db.execute(f"SELECT count(*) FROM {source}{where}", args).fetchone()[0]
    rows = _rows(db.execute(
        f"SELECT {', '.join(fields)} FROM {source}{where} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [limit, offset]))
    result = {"total": total, "offset": offset, "returned": len(rows), "awards": rows}
    if requested > cap:
        result["note"] = f"limit is capped at {cap}" + (" when abstracts are included." if include_abstract else ".")
    return result


def get_award(db, *, id=None, agency_tracking_number=None, contract=None, agency=None,
              include_contacts=False):
    clauses, args = [], []
    if id is not None:
        clauses.append("id = ?"); args.append(int(id))
    if agency_tracking_number:
        clauses.append("agency_tracking_number = ?"); args.append(agency_tracking_number.strip())
    if contract:
        clauses.append("contract = ?"); args.append(contract.strip())
    if not clauses:
        raise QueryError("Give an id, or an agency tracking number and/or contract number.")
    if agency:
        clauses.append("agency_code = ?"); args.append(_agency(agency))
    where = " AND ".join(clauses)
    total = db.execute(f"SELECT count(*) FROM awards WHERE {where}", args).fetchone()[0]
    rows = _rows(db.execute(f"SELECT * FROM awards WHERE {where} ORDER BY award_year, phase, id LIMIT ?",
                            args + [MAX_GET_ROWS]))
    result = {"matches": total, "returned": len(rows),
              "awards": [_visible(r, include_contacts) for r in rows]}
    if total > 1:
        agencies = {r[0] for r in db.execute(f"SELECT DISTINCT agency_code FROM awards WHERE {where}", args)}
        note = ("More than one row matches; the file has no unique award key. "
                "For NASA, every repeated tracking number and contract pair is a Phase I "
                "and a Phase II award under one number.")
        if len(agencies) > 1:
            note += " These rows span more than one agency; add agency to narrow them."
        if total > len(rows):
            note += (f" Showing the first {len(rows)} of {total}, oldest first. "
                     "Use sbir_search_awards to page through them.")
        result["note"] = note
    return result


def company(db, *, uei):
    uei = uei.strip().upper()
    names = [r[0] for r in db.execute(
        "SELECT company FROM awards WHERE uei = ? GROUP BY company ORDER BY max(award_year) DESC", (uei,))]
    if not names:
        return {"uei": uei, "found": False,
                "note": "No awards with this UEI. Awards before 2015 often have no UEI; "
                        "try sbir_search_awards with the company name."}
    latest = _rows(db.execute(
        "SELECT company, company_website, address1, address2, city, state, state_code, zip, "
        "number_employees, hubzone_owned, socially_economically_disadvantaged, woman_owned, "
        "award_year AS as_of_award_year FROM awards WHERE uei = ? ORDER BY award_year DESC, id DESC LIMIT 1",
        (uei,)))[0]
    totals = _rows(db.execute(
        "SELECT count(*) AS awards, sum(award_amount) AS amount_total, "
        "min(award_year) AS first_year, max(award_year) AS last_year FROM awards WHERE uei = ?", (uei,)))[0]
    breakdown = {}
    for key, column in (("by_agency", "agency_code"), ("by_phase", "phase"),
                        ("by_program", "program"), ("by_year", "award_year")):
        breakdown[key] = _rows(db.execute(
            f"SELECT {column} AS value, count(*) AS awards, sum(award_amount) AS amount_total "
            f"FROM awards WHERE uei = ? GROUP BY {column} ORDER BY {column}", (uei,)))
    return {
        "uei": uei, "found": True, "names": names, "profile": latest, **totals, **breakdown,
        "note": "Counts only awards that carry this UEI. Awards before 2015 often have no UEI, "
                "so older history may be undercounted.",
    }


def aggregate(db, *, group_by, sort=None, limit=None, **filters):
    """Counts and dollar totals per group, plus overall totals.

    group_by="company" groups by UEI, names each company by its most recent
    award, and returns the top companies (by awards unless sort="amount").
    Awards without a UEI can't be attributed to a company; their count is
    reported, not dropped silently.
    """
    if group_by not in GROUPS and group_by != "company":
        raise QueryError(f"group_by must be one of: {', '.join([*GROUPS, 'company'])}.")
    if sort not in (None, "", "awards", "amount"):
        raise QueryError("sort must be awards or amount.")
    where, args = _where(**filters)
    stats = ("count(*) AS awards, sum(a.award_amount) AS amount_total, "
             "sum(a.award_amount IS NULL) AS awards_without_amount")
    totals = _rows(db.execute(f"SELECT {stats} FROM awards a{where}", args))[0]
    by_size = {"awards": "awards DESC, amount_total DESC",
               "amount": "amount_total IS NULL, amount_total DESC, awards DESC"}
    result = {"group_by": group_by}

    if group_by == "company":
        cap = MAX_COMPANY_GROUPS
        requested = int(limit) if limit else DEFAULT_COMPANY_GROUPS
        limit = max(1, min(requested, cap))
        with_uei = where + (" AND " if where else " WHERE ") + "a.uei IS NOT NULL"
        order = by_size[sort or "awards"] + ", uei"
        result["groups"] = _rows(db.execute(
            f"SELECT a.uei AS uei, {stats} FROM awards a{with_uei} "
            f"GROUP BY a.uei ORDER BY {order} LIMIT ?", args + [limit]))
        for g in result["groups"]:     # only the top rows need a name
            g["company"] = db.execute(
                "SELECT company FROM awards WHERE uei = ? ORDER BY award_year DESC, id DESC LIMIT 1",
                (g["uei"],)).fetchone()[0]
        result["companies_total"] = db.execute(
            f"SELECT count(DISTINCT a.uei) FROM awards a{with_uei}", args).fetchone()[0]
        result["awards_without_uei"] = db.execute(
            f"SELECT count(*) FROM awards a{where + (' AND ' if where else ' WHERE ')}a.uei IS NULL",
            args).fetchone()[0]
        if result["awards_without_uei"]:
            result["note"] = (f"{result['awards_without_uei']} matching awards have no UEI and aren't "
                              "counted for any company; most are from before 2015.")
        if requested > cap:
            result["note"] = (result.get("note", "") + f" limit is capped at {cap}.").strip()
    else:
        column = GROUPS[group_by]
        order = by_size[sort] if sort else f"a.{column}"
        sql = (f"SELECT a.{column} AS {group_by}, {stats} FROM awards a{where} "
               f"GROUP BY a.{column} ORDER BY {order}")
        if limit:
            sql += f" LIMIT {max(1, int(limit))}"
        result["groups"] = _rows(db.execute(sql, args))
    result["totals"] = totals
    return result


# ---------------------------------------------------------------- sbir_query

# sbir_query runs one SELECT written by the model. SQLite enforces the rules
# itself: the connection is read-only, an authorizer lets a statement read the
# listed tables and nothing else (never the contact columns), and the limits
# stop a query that runs too long, builds a huge value, or returns too much.
QUERY_SECONDS = 10               # realistic questions on the full file took under 1 s
QUERY_MAX_ROWS = 200
# Rows as compact JSON, the same limit as every other list result. Measured in
# Claude Code: query results cost one token per 1.6-1.7 characters, so this
# keeps the widest result near 8,000 tokens, under the 10,000-token warning.
QUERY_MAX_CHARS = MAX_CHARS
MAX_VALUE_BYTES = 1_000_000      # the longest real value, an abstract, is 23 KB

# Every awards column except the contact fields, in file order.
PUBLIC_COLUMNS = (["id"] + [c for _, c, _ in COLUMNS if c not in CONTACT_COLUMNS]
                  + ["agency_code", "state_code"])

# awards (our view, and the table under it), the keyword index, and the
# index's own tables, which FTS5 reads for itself.
READABLE_TABLES = {"awards", "awards_fts", "awards_fts_data", "awards_fts_idx",
                   "awards_fts_config", "awards_fts_docsize"}
UNSAFE_FUNCTIONS = {"fts3_tokenizer", "load_extension"}


SCHEMA_COLUMNS = {"type", "name", "tbl_name", "rootpage", "sql"}      # sqlite_master's


def _authorize(action, arg1, arg2, db_name, source):
    """SQLite asks this about every table, column and function a statement uses."""
    if db_name == "main" and arg1 == "sqlite_master" and (
            (action == sqlite3.SQLITE_UPDATE and arg2 in SCHEMA_COLUMNS)
            or (action == sqlite3.SQLITE_READ and arg2 == "ROWID")):
        # SQLite 3.40 (Debian 12's) prepares, but never runs, an UPDATE of
        # sqlite_master while FTS5 sets up its index, and fails the query if these
        # checks are refused; 3.53 makes none of them. Nothing can be written: the
        # connection is read-only and query_only.
        return sqlite3.SQLITE_OK
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:              # arg1 is the table, arg2 the column
        if arg1 in READABLE_TABLES and not (arg1 == "awards" and arg2 in CONTACT_COLUMNS):
            return sqlite3.SQLITE_OK
        if arg2 == "":
            # A table none of whose columns are used, as in count(*). SQLite names it
            # without its database, so a CTE and a table look alike here. Allowing it
            # lets a query count any table's rows, but read no values.
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:          # arg2 is the function's name
        return sqlite3.SQLITE_DENY if arg2 in UNSAFE_FUNCTIONS else sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
        return sqlite3.SQLITE_OK                   # FTS5 reads it internally; it changes nothing
    return sqlite3.SQLITE_DENY


READ_ONLY_ONLY = ("sbir_query runs one read-only SELECT on awards and its keyword index awards_fts; "
                  "nothing can be changed or created.")


def _sql_error(e):
    """SQLite's complaint, reworded so the model knows what to do instead."""
    message = str(e)
    column = re.search(r"access to (?:\w+\.)*(\w+) is prohibited|no such column: (?:\w+\.)?(\w+)", message)
    if column and (column.group(1) or column.group(2)) in CONTACT_COLUMNS:
        return ("Contact details (emails, phones, titles, and the agency and research-institution "
                "contacts) aren't available to sbir_query. For one award's contacts, use "
                "sbir_get_award with include_contacts.")
    if message == "interrupted":
        return (f"The query ran past the {QUERY_SECONDS}-second limit and was stopped. Filter early "
                "(agency_code, award_year), avoid broad prefix searches such as 'a*', or use sbir_aggregate.")
    if message == "string or blob too big":
        return (f"A value grew past the {MAX_VALUE_BYTES // 1_000_000} MB limit (group_concat over many "
                "rows, for example). Return rows or counts instead.")
    if "one statement at a time" in message:
        return "Send one SELECT statement per call."
    if "prohibited" in message or "not authorized" in message or message == "authorization denied":
        return f"{READ_ONLY_ONLY[:-1]}; SQLite refused this ({message})."
    return f"SQLite: {message}. The tool description lists the columns of awards and awards_fts."


def run_sql(db, *, sql):
    """Run one read-only SELECT and return its rows, as many as the limits allow.

    db must be a connection opened read-only for this query alone, and closed by
    the caller afterwards: the view, authorizer and limits set here stay on it.
    """
    if not sql or not sql.strip():
        raise QueryError("sql is empty.")
    # Checked before SQLite sees it, so the refusal is the same whatever the
    # statement would have touched (SQLite's own message names a view).
    first = re.match(r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/|\()*(\w*)", sql, re.S).group(1).upper()
    if first not in ("SELECT", "WITH", "VALUES"):
        raise QueryError(READ_ONLY_ONLY)
    # Temporary objects are found before main ones, so "awards" means this view.
    db.execute(f"CREATE TEMP VIEW awards AS SELECT {', '.join(PUBLIC_COLUMNS)} FROM main.awards")
    db.execute("PRAGMA query_only = ON")
    db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
    db.set_authorizer(_authorize)
    deadline = time.monotonic() + QUERY_SECONDS
    db.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
    try:
        cursor = db.execute(sql)
        if cursor.description is None:
            raise QueryError("Send one SELECT statement.")
        columns = [c[0] for c in cursor.description]
        rows, chars, cut = [], 1, None             # 1: the brackets around the rows, less one comma
        for row in cursor:
            if len(rows) == QUERY_MAX_ROWS:
                cut = f"Stopped at {QUERY_MAX_ROWS} rows; the query returns more."
                break
            row = [f"<{len(v)}-byte blob>" if isinstance(v, bytes) else v for v in row]
            chars += len(text(row)) + 1
            if chars > QUERY_MAX_CHARS:
                if not rows:
                    raise QueryError(f"The first row alone is over the {QUERY_MAX_CHARS:,}-character limit. "
                                     "Select fewer or shorter columns (substr(abstract, 1, 500), for example).")
                cut = f"Stopped after {len(rows)} rows at the {QUERY_MAX_CHARS:,}-character limit; the query returns more."
                break
            rows.append(row)
    except (sqlite3.Error, sqlite3.Warning) as e:
        raise QueryError(_sql_error(e)) from e
    result = {"columns": columns, "rows": rows, "returned": len(rows)}
    if cut:
        result["note"] = cut + " Aggregate in SQL, select fewer or shorter columns, or page with LIMIT and OFFSET."
    return result
