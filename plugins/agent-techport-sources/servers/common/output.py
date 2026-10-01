"""What every tool sends: compact JSON, lists of records as tables, and a size limit.

Measured in Claude Code (docs/design.md, open question 12): the same 50 search
results cost 13,351 tokens as indented JSON and 6,909 as a compact table.
Standard library only.
"""

import json

# Characters of rows in one list result. Rows run at 1.6-1.9 characters per
# token, so this keeps a result near 7,000 tokens, under Claude Code's
# 10,000-token warning.
MAX_CHARS = 12_000


def text(result):
    """Compact JSON: no spaces or line breaks, text left as it is, dates as text."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


def uniform(records):
    """True for a non-empty list of records that all have the same fields."""
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return False
    fields = set(records[0])
    return all(isinstance(r, dict) and set(r) == fields for r in records)


def table(records):
    """Records with the same fields, as {"columns": [...], "rows": [[...], ...]}.

    Records whose fields differ are refused, so a value can't land under the
    wrong column.
    """
    columns = list(records[0]) if records else []
    for r in records:
        if not isinstance(r, dict) or set(r) != set(columns):
            raise ValueError(f"records with different fields can't share a table: {columns} and {r!r:.200}")
    return {"columns": columns, "rows": [[r[c] for c in columns] for r in records]}


def limit_list(result, key, *, as_table=True, offset=None, continue_with=None):
    """Cut result[key], a list of records, to the size limit, and unless as_table
    is false, turn it into a table. At least one record is kept.

    When records are left out, result["returned"] is corrected and a note says
    how to get the rest: continue_with(left_out) if given, else the next offset,
    else to narrow the search. Returns result.
    """
    records = result[key]
    as_table = as_table and bool(records)   # an empty list reads better as []
    shaped = table(records) if as_table else None
    items = shaped["rows"] if as_table else records
    kept, chars = 0, 1                      # 1: the brackets around the list, less one comma
    for item in items:
        chars += len(text(item)) + 1
        if kept and chars > MAX_CHARS:
            break
        kept += 1
    if as_table:
        shaped["rows"] = items[:kept]
        result[key] = shaped
    else:
        result[key] = records[:kept]
    if kept < len(records):
        if "returned" in result:
            result["returned"] = kept
        if continue_with:
            how = continue_with(records[kept:])
        elif offset is not None:
            how = f"Call again with offset={offset + kept} for the rest."
        else:
            how = "Narrow the search for the rest."
        note = f"Stopped after {kept} of these {len(records)} at the size limit ({MAX_CHARS:,} characters). {how}"
        result["note"] = f"{result['note']} {note}" if result.get("note") else note
    return result
