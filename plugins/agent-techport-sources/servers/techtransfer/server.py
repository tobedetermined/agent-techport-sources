"""MCP server for NASA Technology Transfer: patents available for licensing, the
software catalog and Spinoff stories. Runs over stdio.

Live API only, no local copy. The only host contacted is technology.nasa.gov
(see api.py). "NASA Technology Transfer connector" in docs/design.md has the
probe results behind every rule here.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..common import output
from ..common.http import SourceError
from . import api as api_mod, model

INSTRUCTIONS = """\
NASA Technology Transfer: NASA's patents available for licensing, its software
catalog, and Spinoff stories of NASA technology in commercial products. Live
from the Technology Transfer portal's API (technology.nasa.gov).

GROUNDING RULE: every claim about NASA's patents, software or spinoffs must come
from a tool result. Use the counts that come with each search; don't tally
records yourself. If the tools can't answer, say so.

- Search needs words: the API can't list a whole catalog, so every count is of
  what one search finds. All words must match unless match is "any". Common
  words (and, of, the...) are left out: the API under-matches them. The API
  also matches text it doesn't return, such as a patent page's technology
  description, so a result may not show a search word.
- Spinoff searches stop at 1,000 matches per word; results say when a word hit
  that limit.
- No dates: the API gives no patent, release or spinoff year.
- Centers get one spelling: LARC is LaRC, DFRC (Dryden) is AFRC, HDQS is HQ.
- techtransfer_get reads a patent's page for its benefits, applications, case
  numbers, US patent numbers and papers.
- Spinoff stories often name the company and the NASA center, and say when SBIR
  funding was involved; the SBIR tools find those awards by company name.
- Lists come as tables: column names once, then one row of values per item. A
  long list stops at a size limit, with a note saying how to get the rest.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

server = MCPServer(name="techtransfer", instructions=INSTRUCTIONS)
tt = api_mod.TechTransfer()

MAX_LIMIT = 50
MAX_LIMIT_WITH_DESCRIPTIONS = 25
CAVEAT = "Counts cover this search's matches: the API can't list a whole catalog, and gives no dates."
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{2,40}")


def _data():
    return {"source": "NASA Technology Transfer portal (technology.nasa.gov), live",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "caveat": CAVEAT}


def _searches(pairs):
    """{(kind, word): results}, fetched in parallel: one request per pair."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pair: pool.submit(tt.search, *pair) for pair in pairs}
    return {pair: job.result() for pair, job in jobs.items()}


@server.tool(annotations=READ_ONLY, structured_output=False)
def techtransfer_search(
    query: str,
    kind: str = "all",
    match: str = "all",
    center: str | None = None,
    category: str | None = None,
    include_description: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find NASA patents available for licensing, software in NASA's catalog,
    and Spinoff stories (NASA technology in commercial products).

    query: search words (up to 6). All must match unless match="any". Each
    word is searched on its own, so quotes and phrases don't apply, and
    common words (and, of, the...) are left out. A reference number such as
    KSC-TOPS-59 finds that record.
    kind: all (default), patent, software or spinoff.
    center: a NASA center code (ARC, AFRC, GRC, GSFC, HQ, JPL, JSC, KSC, LaRC,
    MSFC, SSC). category: text in the category's name, any case (e.g.
    "materials", "health", "propulsion").
    Returns the total, counts by kind, center and category for all matches,
    and one page as a table: kind, reference, title, category, center, and
    release type for software. include_description adds the first 300
    characters. limit up to 50 (25 with descriptions); offset to page.
    """
    try:
        kinds, (words, dropped) = model.kinds(kind), model.words(query)
        model.combine({}, match)                        # checks match before any request
        if center:
            model.center_code(center)
        found = _searches([(k, w) for k in kinds for w in words])
        matches, capped = [], []
        for k in kinds:
            matches += model.combine({w: [model.record(k, r) for r in found[(k, w)]] for w in words}, match)
            if k == "spinoff":
                capped += [w for w in words if len(found[(k, w)]) >= model.SPINOFF_CAP]
        matches.sort(key=lambda r: -(r["score"] or 0))
        matches = model.keep(matches, center=center, category=category)
        cap = MAX_LIMIT_WITH_DESCRIPTIONS if include_description else MAX_LIMIT
        notes = []
        if int(limit) > cap:
            notes.append(f"limit is capped at {cap}" + (" with descriptions." if include_description else "."))
        limit, offset = max(1, min(int(limit), cap)), max(0, int(offset))
        rows = [model.row(r, description=include_description, release="software" in kinds)
                for r in matches[offset:offset + limit]]
        if dropped:
            notes.append(f"Left out common words the API doesn't search reliably: "
                         f"{', '.join(repr(w) for w in dropped)}.")
        if capped:
            notes.append(f"The API returns at most {model.SPINOFF_CAP:,} spinoffs for a word, and "
                         f"{', '.join(repr(w) for w in capped)} reached that, so spinoffs beyond them are missing. "
                         "More specific words get past it.")
        result = {"total": len(matches), "offset": offset, "returned": len(rows), **model.counts(matches),
                  "records": rows}
        if notes:
            result["note"] = " ".join(notes)
        result["data"] = _data()
        return output.text(output.limit_list(result, "records", offset=offset))
    except (model.QueryError, SourceError) as e:
        raise ToolError(str(e)) from e


def _find(reference):
    """(record, close matches): the record whose number is reference, from
    the catalog its shape suggests first, then the others."""
    first = model.kind_of(reference)
    close = []
    for kind in [first] + [k for k in model.KINDS if k != first]:
        recs = [model.record(kind, r) for r in tt.search(kind, reference)]
        exact = [r for r in recs if (r["reference"] or "").upper() == reference.upper()]
        if exact:
            return exact[0], close
        close += [r["reference"] for r in recs if r["reference"]]
    return None, close


@server.tool(annotations=READ_ONLY, structured_output=False)
def techtransfer_get(reference: str) -> str:
    """One patent, software entry or spinoff by its number (from
    techtransfer_search): KSC-TOPS-59 or TOP2-279 (patents), ARC-17900-1
    (software), KSC-SO-111 (spinoffs).

    Gives the full description. For a patent, its page on technology.nasa.gov
    adds a subtitle, the technology in more detail, benefits, applications,
    NASA case numbers, US patent numbers (with links to the USPTO's records)
    and papers. For software: release type, how to get it, and its link. For
    a spinoff: the whole story.
    """
    try:
        ref = (reference or "").strip().upper()         # numbers are upper case in the data
        if not REFERENCE.fullmatch(ref):
            raise ToolError("reference is a number such as KSC-TOPS-59 (patent), ARC-17900-1 (software) or "
                            f"KSC-SO-111 (spinoff), from techtransfer_search; not {reference!r}.")
        rec, close = _find(ref)
        if rec is None:
            raise ToolError(f"No patent, software or spinoff numbered {ref!r}."
                            + (f" Close: {', '.join(close[:8])}." if close else ""))
        out = {k: v for k, v in rec.items() if v is not None and k != "score"}
        result = {"record": out}
        if rec["kind"] == "patent":
            out["page"] = f"{model.SITE}/patent/{rec['reference']}"
            try:
                details, why = model.page_details(tt.page(rec["reference"])), "it has none of the usual sections"
            except SourceError as e:
                details, why = None, str(e)
            if details:
                out.update({k: v for k, v in details.items() if v})
                if details["patents"]:
                    out["patents"] = output.table(details["patents"])
            else:
                result["note"] = (f"The patent's page couldn't be read ({why}), so benefits, applications and "
                                  f"patent numbers are missing. Its page: {out['page']}")
        result["data"] = _data()
        return output.text(result)
    except SourceError as e:
        raise ToolError(str(e)) from e


def main():
    server.run()
