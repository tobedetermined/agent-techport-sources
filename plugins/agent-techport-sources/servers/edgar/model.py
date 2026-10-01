"""Requests, records and filing text for SEC EDGAR. Standard library only.

Every rule here comes from the probe of 2026-09-30; see "SEC EDGAR connector"
in docs/design.md.
"""

import html.parser
import re
from datetime import date

from ..common import text
from ..common.http import USER_AGENT

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
FIRST_YEAR = 2001           # full-text search starts here
PASSAGE_WIDTH = 180         # text kept either side of a match in a search hit's passage

# The financial measures, each with the US-GAAP concepts that can carry it, in
# order of preference: companies name the same figure differently, and change
# names over time (revenue moved to RevenueFromContractWithCustomer... in 2018).
MEASURES = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet",
                "SalesRevenueServicesNet"],
    "rd_expense": ["ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "total_assets": ["Assets"],
}
AT_YEAR_END = {"cash", "total_assets"}       # balances, not amounts over a year
# Periodic reports, whose figures are the financial statements. SEC sometimes puts a year's
# frame on a figure from another form: Virgin Galactic's CY2021-CY2025 net income comes from
# its proxy's pay-versus-performance table, in thousands (-352,899 for -352,899,000).
REPORTS = ("10-K", "10-Q", "20-F", "40-F", "10-KT", "10-QT")


class QueryError(ValueError):
    """A value the tools can't take. The message says what is accepted."""


# ---------------------------------------------------------------- the contact SEC requires


def contact(value):
    """The user's contact for SEC (a name and an email), tidied, or None. A
    setting Claude Code left unfilled arrives as "${user_config...}"."""
    if not value or "${" in value or any(ord(c) < 32 for c in value):
        return None
    value = " ".join(value.split())
    if not re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        return None
    try:
        value.encode("latin-1")             # what an HTTP header can carry
    except UnicodeEncodeError:
        return None
    return value


def user_agent(contact_value):
    return f"{USER_AGENT} {contact_value}"


# ---------------------------------------------------------------- companies


def cik(value):
    """A company's SEC id from digits, or None."""
    value = str(value).strip()
    return int(value) if value.isdigit() else None


def from_tickers(tickers, query):
    """Companies in SEC's ticker file that match: the ticker, then the exact
    name, then names containing every word of the query."""
    q = query.strip().casefold()
    rows = [{"cik": t["cik_str"], "name": t["title"], "ticker": t["ticker"]} for t in tickers]
    for key in ("ticker", "name"):
        exact = [r for r in rows if r[key].casefold() == q]
        if exact:
            return exact[:1]
    words = q.split()
    return [r for r in rows if words and all(w in r["name"].casefold() for w in words)]


def suggestions(d):
    """Companies from EDGAR's own index (listed or not), best match first."""
    out = []
    for h in (d.get("hits") or {}).get("hits") or []:
        src = h.get("_source") or {}
        ticker = (src.get("tickers") or "").strip() or None
        name = src.get("entity") or ""
        if ticker:
            name = re.sub(rf"\s*\({re.escape(ticker)}\)\s*$", "", name)
        out.append({"cik": int(h["_id"]), "name": name.strip(), "ticker": ticker})
    return out


def _day(value):
    return (value or "")[:10] or None


def profile(sub):
    business = (sub.get("addresses") or {}).get("business") or {}
    place = ", ".join(p for p in (business.get("city"), business.get("stateOrCountry")) if p)
    fye = sub.get("fiscalYearEnd") or ""
    return {"cik": int(sub["cik"]), "name": sub.get("name"), "tickers": sub.get("tickers") or [],
            "exchanges": sub.get("exchanges") or [],
            "industry": f"{sub['sic']} {sub.get('sicDescription') or ''}".strip() if sub.get("sic") else None,
            "category": sub.get("category") or None, "location": place or None,
            "incorporated": sub.get("stateOfIncorporation") or None,
            "fiscal_year_end": f"{fye[:2]}-{fye[2:]}" if len(fye) == 4 else None,
            "website": sub.get("website") or None,
            "former_names": [{"name": f.get("name"), "from": _day(f.get("from")), "to": _day(f.get("to"))}
                             for f in sub.get("formerNames") or []]}


def _form_matches(form, wanted):
    form = form.upper()
    return any(form == w or form.startswith(w + "/") for w in wanted)


def filings(sub, *, forms=None, since=None):
    """The company's recent filings (SEC keeps about 1,000 here), newest first.
    A form such as 10-K also brings its amendments (10-K/A)."""
    r = (sub.get("filings") or {}).get("recent") or {}
    wanted = [f.strip().upper() for f in forms or [] if f.strip()]
    rows = []
    for i, form in enumerate(r.get("form") or []):
        if wanted and not _form_matches(form, wanted):
            continue
        if since and (r["filingDate"][i] or "") < since:
            continue
        rows.append({"form": form, "filed": r["filingDate"][i], "period": r["reportDate"][i] or None,
                     "description": r["primaryDocDescription"][i] or None, "items": r["items"][i] or None,
                     "accession": r["accessionNumber"][i], "document": r["primaryDocument"][i]})
    return rows


def primary_document(sub, accession_number):
    r = (sub.get("filings") or {}).get("recent") or {}
    numbers = r.get("accessionNumber") or []
    return r["primaryDocument"][numbers.index(accession_number)] if accession_number in numbers else None


# ---------------------------------------------------------------- full-text search


def search_params(query, *, forms=None, year_from=None, year_to=None, cik=None, offset=0, today=None):
    q = (query or "").strip()
    if not q:
        raise QueryError("Give words to search for; \"quote a phrase\".")
    params = {"q": q}
    if forms:
        params["forms"] = ",".join(f.strip().upper() for f in forms if f.strip())
    if year_from or year_to:
        if year_from and int(year_from) < FIRST_YEAR:
            raise QueryError(f"Full-text search covers {FIRST_YEAR} on.")
        if year_from and year_to and int(year_from) > int(year_to):
            raise QueryError("year_from is after year_to.")
        params.update(dateRange="custom", startdt=f"{int(year_from or FIRST_YEAR)}-01-01",
                      enddt=f"{int(year_to)}-12-31" if year_to else (today or date.today()).isoformat())
    if cik:
        params["ciks"] = f"{int(cik):010d}"
    if offset:
        params["from"] = int(offset)
    return params


def _company(display_name, *, keep_ticker):
    name = re.sub(r"\s*\(CIK \d+\)\s*$", "", display_name or "")
    if not keep_ticker:
        name = re.sub(r"\s*\([A-Z0-9.\-, ]+\)\s*$", "", name)
    return " ".join(name.split()) or None


def hits(d):
    """Search hits as rows. Each hit is one document of a filing, sometimes an
    exhibit (document_type EX-13, ...)."""
    rows = []
    for h in (d.get("hits") or {}).get("hits") or []:
        src = h.get("_source") or {}
        rows.append({"company": _company((src.get("display_names") or [""])[0], keep_ticker=False),
                     "cik": int((src.get("ciks") or ["0"])[0]), "form": src.get("form"),
                     "document_type": src.get("file_type"), "filed": src.get("file_date"),
                     "period": src.get("period_ending"), "description": src.get("file_description"),
                     "accession": src.get("adsh"), "document": h.get("_id", "").partition(":")[2] or None})
    return rows


def _buckets(agg, label=lambda k: k):
    out = {label(b["key"]): b["doc_count"] for b in (agg or {}).get("buckets") or []}
    if (agg or {}).get("sum_other_doc_count"):
        out["(others)"] = agg["sum_other_doc_count"]
    return out


def search_counts(d):
    a = d.get("aggregations") or {}
    return {"by_company": _buckets(a.get("entity_filter"), lambda k: _company(k, keep_ticker=True)),
            "by_form": _buckets(a.get("form_filter")), "by_state": _buckets(a.get("biz_states_filter"))}


def query_terms(query):
    """The quoted phrases of a full-text search, then its other words."""
    phrases = [" ".join(p.split()) for p in re.findall(r'"([^"]+)"', query or "") if p.strip()]
    return phrases + re.sub(r'"[^"]*"', " ", query or "").split()


def best_passage(body, query, width=PASSAGE_WIDTH):
    """The passage of a document that shows the most of a search (a phrase
    counts twice as much as a single word), and how many matches the
    document has; or None when nothing matches."""
    terms = [t.replace("|", " ") for t in query_terms(query)]
    try:
        found = text.passages(body, " | ".join(terms), spans=text.split(body), width=width)
    except text.NoWords:
        return None
    if not found:
        return None
    weights = [(text.find_pattern(t), 2 if " " in t else 1) for t in terms]
    best = max(found, key=lambda p: sum(w for pattern, w in weights if pattern and pattern.search(p["text"])))
    return {"passage": best["text"], "found": sum(p["matches"] for p in found)}


# ---------------------------------------------------------------- financials


def _report(form):
    return (form or "").split("/")[0] in REPORTS


def _from_a_report(fact, facts):
    """The framed fact if a periodic report gave it, else the latest report's
    figure for the same period, else None."""
    if _report(fact.get("form")):
        return fact
    same = [f for f in facts if _report(f.get("form")) and f.get("end") == fact.get("end")
            and f.get("start") == fact.get("start")]
    return max(same, key=lambda f: f.get("filed") or "") if same else None


def financials(facts):
    """Annual figures from a company's XBRL facts, in US dollars. Years are
    SEC's calendar-year frames (CY2024; balances at CY2024Q4I), which take the
    closest 12 months when a fiscal year doesn't end in December. For each
    year, the first concept in MEASURES that has a value is used, and concepts
    says which, year by year: a fallback isn't always the same figure (cash
    with restricted cash, profit with noncontrolling interests). mixed lists
    the measures whose years don't all use one concept."""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    values, used, not_usd = {}, {}, []
    for measure, concepts in MEASURES.items():
        frame = re.compile(r"CY(\d{4})Q4I" if measure in AT_YEAR_END else r"CY(\d{4})")
        for concept in concepts:
            units = (gaap.get(concept) or {}).get("units") or {}
            if units and "USD" not in units:
                not_usd.append(concept)
            usd = units.get("USD") or []
            for fact in usd:
                m = frame.fullmatch(fact.get("frame") or "")
                if m and (int(m[1]), measure) not in values:
                    fact = _from_a_report(fact, usd)
                    if fact:
                        values[(int(m[1]), measure)] = (fact["val"], concept)
    years = sorted({y for y, _ in values})
    rows = []
    for y in years:
        rows.append({"year": y, **{m: (values.get((y, m)) or (None,))[0] for m in MEASURES}})
        for m in MEASURES:
            if (y, m) in values:
                used.setdefault(m, {}).setdefault(values[(y, m)][1], []).append(y)
    return {"years": rows, "concepts": used, "mixed": [m for m in MEASURES if len(used.get(m, {})) > 1],
            "not_usd": not_usd}


# ---------------------------------------------------------------- filing documents


def accession(value):
    """An accession number in SEC's form, 0001234567-25-000123."""
    v = str(value or "").strip()
    if re.fullmatch(r"\d{18}", v):
        v = f"{v[:10]}-{v[10:12]}-{v[12:]}"
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", v):
        raise QueryError(f"Not an accession number: {value!r}. They look like 0001819994-26-000013 "
                         "(from edgar_company or edgar_search).")
    return v


def document_name(value):
    v = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][\w.\-]{0,120}", v):
        raise QueryError(f"Not a document name: {value!r}. They look like rklb-20251231.htm.")
    return v


READABLE = (".htm", ".html", ".txt", ".xml")


def readable(document):
    """Whether the plugin can read a document as text: HTML and text, not PDFs
    or images (SEC's search indexes those too)."""
    return str(document or "").lower().endswith(READABLE)


def unreadable_reason(document, url):
    kind = "a PDF" if str(document).lower().endswith(".pdf") else "not HTML or text"
    return f"{document} is {kind}, which the plugin can't read as text. Its link: {url}"


def archive_url(cik_value, accession_number, document):
    return f"{ARCHIVES}/{int(cik_value)}/{accession_number.replace('-', '')}/{document}"


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}
SKIP = {"head", "script", "style", "ix:header"}
PAGE = "\x00page\x00"


class _Filing(html.parser.HTMLParser):
    """Text from a filing's HTML (often inline XBRL). Hidden parts (the XBRL
    header, display:none) are left out; page breaks become page marks; table
    cells are separated by " | "."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.open = [], []          # open: (tag, hidden, break after)

    def _hidden(self):
        return any(h for _, h, _ in self.open)

    def handle_starttag(self, tag, attrs):
        style = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if "page-break-before:always" in style and not self._hidden():
            self.out.append(PAGE)
        after = "page-break-after:always" in style
        if tag in VOID:
            if not self._hidden():
                self.out.append(PAGE if after else "\n" if tag in BLOCKS else "")
            return
        self.open.append((tag, tag in SKIP or "display:none" in style, after))

    def handle_endtag(self, tag):
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                _, hidden, after = self.open[i]
                del self.open[i:]
                if not hidden and not self._hidden():
                    if tag in BLOCKS:
                        self.out.append("\n")
                    elif tag in ("td", "th"):
                        self.out.append(" | ")
                    if after:
                        self.out.append(PAGE)
                return

    def handle_data(self, data):
        if not self._hidden():
            self.out.append(data)


def filing_text(page):
    """A filing document as text, with [page N] marks when it has page breaks."""
    parser = _Filing()
    parser.feed(page)
    parser.close()
    lines = []
    for line in "".join(parser.out).replace("\xa0", " ").splitlines():
        line = re.sub(r"(?:\s*\|\s*)+", " | ", " ".join(line.split())).strip(" |")
        if line:
            lines.append(line)
    text = "\n".join(lines)
    if PAGE not in text:
        return text
    pages = text.split(PAGE)
    return "\n".join(f"[page {n}]\n{p.strip()}" if p.strip() else f"[page {n}]"
                     for n, p in enumerate(pages, 1)).strip()
