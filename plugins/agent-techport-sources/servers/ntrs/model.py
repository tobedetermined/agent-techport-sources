"""Request bodies, record shapes and text handling for NTRS. Standard library only.

Every rule here comes from the probe of 2026-09-30; see "NTRS connector" in
docs/design.md.
"""

import html.parser
import re

from ..common import text as _text

SITE = "https://ntrs.nasa.gov"

# Center codes as the API spells them (filters are exact and case-sensitive),
# with the names its records give. CDMS holds most older records: 59% of all.
CENTERS = {
    "ARC": "Ames Research Center", "AFRC": "Armstrong Flight Research Center", "GRC": "Glenn Research Center",
    "GSFC": "Goddard Space Flight Center", "HQ": "Headquarters", "JPL": "Jet Propulsion Laboratory",
    "JSC": "Johnson Space Center", "KSC": "Kennedy Space Center", "LaRC": "Langley Research Center",
    "MSFC": "Marshall Space Flight Center", "SSC": "Stennis Space Center", "WFF": "Wallops Flight Facility",
    "WSTF": "White Sands Test Facility", "2230": "2230 Support",
    "CDMS": "Legacy CDMS: older records without a NASA center",
}
# The report types the API describes, plus two its data uses (ABSTRACT, EXTENDED_ABSTRACT).
# An unknown type isn't refused by the API, it just finds nothing, so they're checked here.
REPORT_TYPES = (
    "CONFERENCE_PAPER", "REPRINT", "OTHER", "CONTRACTOR_REPORT", "PRESENTATION", "TECHNICAL_MEMORANDUM",
    "PREPRINT", "ABSTRACT", "ACCEPTED_MANUSCRIPT", "CONFERENCE_PROCEEDINGS", "POSTER", "TECHNICAL_PUBLICATION",
    "CONTRACTOR_OR_GRANTEE_REPORT", "VIDEO", "THESIS_DISSERTATION", "SPECIAL_PUBLICATION", "EXTENDED_ABSTRACT",
    "BOOK_CHAPTER", "BOOK", "WHITE_PAPER", "CONTRIBUTION_TO_LARGER_WORK", "TECHNICAL_TRANSLATION",
    "CONFERENCE_PUBLICATION", "STI_TYPE_NONE",
)
SORTS = {"relevance": None, "newest": {"field": "published", "order": "desc"},
         "oldest": {"field": "published", "order": "asc"}}
MAX_RESULTS = 10_000        # from + size over this is a 400 from the API
ABSTRACT_CHARS = 300
MAX_AUTHORS = 50            # collaboration papers list up to 1,186 authors
PART_CHARS = _text.PART_CHARS
PASSAGE_CHARS = _text.PASSAGE_CHARS
# In a sample of 500 records this matched having files exactly; the
# downloadsAvailable flag disagreed with the file list on 22 of them.
HAS_FILES = "DOCUMENT_AND_METADATA"

# group_by: the API's own count, returned with every search.
GROUPS = {"center": "center", "report_type": "stiType", "subject": "subjectCategory", "year": "published",
          "author": "author", "organization": "organization", "keyword": "keyword",
          "funding_number": "fundingNumber"}


class QueryError(ValueError):
    """A value the API can't take. The message says what is accepted."""


# ---------------------------------------------------------------- requests


def center_code(value):
    wanted = value.strip().lower()
    for code in CENTERS:
        if code.lower() == wanted:
            return code
    raise QueryError(f"Unknown center {value!r}. Use one of: {', '.join(CENTERS)}.")


def type_code(value):
    wanted = re.sub(r"[\s-]+", "_", value.strip()).upper()
    if wanted in REPORT_TYPES:
        return wanted
    raise QueryError(f"Unknown report type {value!r}. Use one of: {', '.join(REPORT_TYPES)}.")


def search_body(*, query=None, center=None, report_type=None, subject=None, author=None, organization=None,
                keyword=None, report_number=None, funding_number=None, year_from=None, year_to=None,
                has_files=False, sort="relevance", offset=0, limit=20):
    """The POST body for /api/citations/search. limit=0 gives counts only."""
    body = {}
    if query and query.strip():
        body["q"] = query.strip()
    if center:
        body["center"] = [center_code(center)]
    if report_type:
        body["stiType"] = type_code(report_type)
    for field, value in (("subjectCategory", subject), ("author", author), ("organization", organization),
                         ("keyword", keyword), ("reportNumber", report_number), ("fundingNumber", funding_number)):
        if value and value.strip():
            body[field] = [value.strip()]       # one value: a list of several means all of them, not any
    if year_from or year_to:
        if year_from and year_to and int(year_from) > int(year_to):
            raise QueryError("year_from is after year_to.")
        body["published"] = {}
        if year_from:
            body["published"]["gte"] = f"{int(year_from):04d}-01-01"
        if year_to:
            body["published"]["lt"] = f"{int(year_to) + 1:04d}-01-01"
    if has_files:
        body["disseminated"] = HAS_FILES
    if sort not in SORTS:
        raise QueryError(f"sort must be one of: {', '.join(SORTS)}.")
    if SORTS[sort]:
        body["sort"] = SORTS[sort]
    offset, limit = int(offset), int(limit)
    if offset < 0:
        raise QueryError("offset can't be negative.")
    if offset + limit > MAX_RESULTS:
        raise QueryError(f"NTRS returns only the first {MAX_RESULTS:,} results of a search. Narrow it "
                         "(center, report type, years, subject) to reach the rest.")
    body["page"] = {"size": limit, "from": offset}
    return body


# ---------------------------------------------------------------- records


def _authors(d):
    out = []
    for a in sorted(d.get("authorAffiliations") or [], key=lambda a: a.get("sequence") or 0):
        meta = a.get("meta") or {}
        out.append({"name": ((meta.get("author") or {}).get("name") or "").strip() or None,
                    "organization": ((meta.get("organization") or {}).get("name") or "").strip() or None})
    return out


def published(d):
    """The publication date, or None: conference papers, presentations and
    posters often have none."""
    for p in d.get("publications") or []:
        if p.get("publicationDate"):
            return p["publicationDate"][:10]
    return None


def _short(text, chars):
    return text[:chars] + "…" if text and len(text) > chars else text


def row(d, *, abstract=False):
    """A search result. Files are counted from the file list."""
    names = [a["name"] for a in _authors(d) if a["name"]]
    authors = "; ".join(names[:3]) + (f" (+{len(names) - 3} more)" if len(names) > 3 else "")
    out = {"id": str(d.get("id")), "title": (d.get("title") or "").strip() or None, "type": d.get("stiType"),
           "published": published(d), "center": (d.get("center") or {}).get("code"),
           "authors": authors or None, "files": len(d.get("downloads") or [])}
    if abstract:
        out["abstract"] = _short(d.get("abstract"), ABSTRACT_CHARS)
    return out


def _dropping_none(d):
    return {k: v for k, v in d.items() if v is not None}


def _day(value):
    return (value or "")[:10] or None


def _report_numbers(d):
    """Each number once: records list most twice, once as "Report Number: ..."."""
    numbers = []
    for n in d.get("otherReportNumbers") or []:
        n = re.sub(r"^Report Number:\s*", "", str(n)).strip()
        if n and n not in numbers:
            numbers.append(n)
    return numbers


def _related(d):
    out, seen = [], set()
    for r in d.get("related") or []:
        if r.get("id") is None or r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({"id": str(r["id"]), "title": r.get("title"), "relation": r.get("type")})
    return out


def record(d):
    """One record as ntrs_get_record returns it, without its files."""
    authors = _authors(d)
    out = {"id": str(d.get("id")), "title": (d.get("title") or "").strip() or None, "type": d.get("stiType"),
           "type_detail": d.get("stiTypeDetails"), "published": published(d), "abstract": d.get("abstract"),
           "authors": authors[:MAX_AUTHORS]}
    if len(authors) > MAX_AUTHORS:
        out["authors_total"] = len(authors)
    center = d.get("center")
    out["center"] = {"code": center.get("code"), "name": center.get("name")} if center else None
    out["subjects"] = d.get("subjectCategories") or []
    out["keywords"] = d.get("keywords") or []
    out["funding_numbers"] = [{"number": f.get("number"), "type": f.get("type")} for f in d.get("fundingNumbers") or []]
    out["report_numbers"] = _report_numbers(d)
    out["publications"] = [_dropping_none({
        "name": p.get("publicationName"), "publisher": p.get("publisher"), "volume": p.get("volume"),
        "issue": p.get("issue"), "date": _day(p.get("publicationDate")), "doi": p.get("doi")})
        for p in d.get("publications") or []]
    out["meetings"] = [_dropping_none({
        "name": (m.get("name") or "").strip() or None, "location": m.get("location"),
        "start": _day(m.get("startDate")), "end": _day(m.get("endDate"))})
        for m in d.get("meetings") or []]
    out["related"] = _related(d)
    out["page"] = f"{SITE}/citations/{d.get('id')}"
    return out


def _url(link):
    """A file link from the API, as a full URL on the NTRS site, or None."""
    if not link:
        return None
    if link.startswith("/") and not link.startswith("//"):
        return SITE + link
    return link if link.startswith(SITE + "/") else None


def files(d):
    """The record's files, the report before any abstract, numbered from 1 as
    ntrs_read_text takes them. pdf is None when NTRS has no PDF version (Word
    and PowerPoint files often don't); text is the text NTRS extracted."""
    out = []
    for f in sorted(d.get("downloads") or [], key=lambda f: f.get("type") != "STI"):
        links = f.get("links") or {}
        name = f.get("name") or ""
        out.append({"file": len(out) + 1, "name": name, "type": f.get("type"),
                    "format": name.rsplit(".", 1)[-1].lower() if "." in name else f.get("mimetype"),
                    "pdf": _url(links.get("pdf")), "text": _url(links.get("fulltext")),
                    "original": _url(links.get("original"))})
    return out


# ---------------------------------------------------------------- counts


def counts(aggregations, group_by, *, total):
    """Counts by group from a search's aggregations. Years come in order and
    complete, with the number of undated records. Other groups are the API's
    top 20 (centers: all), with "rest" counting matches outside them."""
    if group_by not in GROUPS:
        raise QueryError(f"group_by must be one of: {', '.join(GROUPS)}.")
    agg = aggregations.get(GROUPS[group_by]) or {}
    buckets = agg.get("buckets") or []
    if group_by == "year":
        groups = sorted(({"year": int(b["key_as_string"]), "records": b["doc_count"]} for b in buckets),
                        key=lambda g: g["year"])
        return {"groups": groups, "undated": total - sum(g["records"] for g in groups)}
    if group_by == "center":
        groups = [{"center": b["key"], "name": CENTERS.get(b["key"]), "records": b["doc_count"]} for b in buckets]
    else:
        groups = [{group_by: b["key"], "records": b["doc_count"]} for b in buckets]
    out = {"groups": groups}
    if agg.get("sum_other_doc_count"):
        out["rest"] = agg["sum_other_doc_count"]
    return out


# ---------------------------------------------------------------- text


class _Extracted(html.parser.HTMLParser):
    """The text in the XHTML that NTRS's extractor produces for some files: the
    body only, a [page N] line where each page starts, one line per paragraph
    or list item."""

    BLOCKS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.page, self.skip = [], 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in ("head", "script", "style"):
            self.skip += 1
        elif tag == "div" and ("class", "page") in attrs:
            self.page += 1
            self.out.append(f"\n\n[page {self.page}]\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("head", "script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def clean_text(raw):
    """A file's extracted text, tidied. Some come as XHTML, some as plain text."""
    if re.match(r"\s*<(html|\?xml|!doctype)", raw, re.I):
        parser = _Extracted()
        parser.feed(raw)
        parser.close()
        raw = "".join(parser.out)
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Parts and passages: shared with the SEC filing reader (servers/common/text.py).
split, page_at = _text.split, _text.page_at


def passages(text, find, *, spans, width=PASSAGE_CHARS):
    """The passages that mention find (see servers/common/text.py)."""
    try:
        return _text.passages(text, find, spans=spans, width=width)
    except _text.NoWords as e:
        raise QueryError(str(e)) from e
