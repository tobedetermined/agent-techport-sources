"""Search results, records and patent pages for NASA Technology Transfer.
Standard library only.

Every rule here comes from the probe of 2026-09-30; see "NASA Technology
Transfer connector" in docs/design.md.
"""

import html
import html.parser
import re
from collections import Counter

SITE = "https://technology.nasa.gov"
KINDS = ("patent", "software", "spinoff")
SPINOFF_CAP = 1000          # the API returns at most this many spinoffs for one word
MAX_WORDS = 6               # one request per word and kind
DESCRIPTION_CHARS = 300

# Common words the API under-matches: every one of the 17 measured (a, an, and,
# as, at, by, for, from, in, is, of, on, or, that, the, to, with) found far fewer
# patents than contain it ("and": 87 of at least 606). As one of several required
# words, each would silently drop most matches. The rest are *assumed* alike.
COMMON = {"a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "in", "into", "is", "it", "its",
          "not", "of", "on", "or", "than", "that", "the", "their", "this", "to", "was", "were", "which", "with"}

# Center codes as this plugin's other sources spell them. The API also uses
# LARC, DFRC (Dryden, renamed Armstrong in 2014) and HDQS.
CENTERS = ("ARC", "AFRC", "GRC", "GSFC", "HQ", "JPL", "JSC", "KSC", "LaRC", "MSFC", "SSC")
ALIASES = {"LARC": "LaRC", "DFRC": "AFRC", "HDQS": "HQ"}


class QueryError(ValueError):
    """A value the tools can't take. The message says what is accepted."""


# ---------------------------------------------------------------- requests


def kinds(value):
    v = (value or "all").strip().lower()
    if v == "all":
        return KINDS
    if v.endswith("s") and v[:-1] in KINDS:
        v = v[:-1]
    if v in KINDS:
        return (v,)
    raise QueryError(f"kind must be all, or one of: {', '.join(KINDS)}.")


def words(query):
    """(words to search, common words left out). Each word is searched on its
    own: the API can't take query parameters, ignores quotes, and matches
    several words unpredictably."""
    out, dropped, seen = [], [], set()
    for w in re.findall(r"[\w.\-]+", query or ""):
        w = w.strip(".-")
        if w and w.casefold() not in seen:
            seen.add(w.casefold())
            (dropped if w.casefold() in COMMON else out).append(w)
    if not out and dropped:
        raise QueryError(f"Only common words ({', '.join(dropped)}), which the API doesn't search reliably: "
                         "add a specific word.")
    if not out:
        raise QueryError("The API can't list a whole catalog: give at least one search word.")
    if len(out) > MAX_WORDS:
        raise QueryError(f"Up to {MAX_WORDS} search words.")
    return out, dropped


# ---------------------------------------------------------------- records


def clean(text):
    """Without markup (search highlights, <p>), entities decoded, spaces tidied."""
    if not text:
        return None
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(text))).split())
    text = re.sub(r" ([.,;:!?)])", r"\1", text)          # a space left where a tag ended
    return text or None


def center(code):
    """One spelling per center; codes outside the list are kept as given."""
    if not code or not str(code).strip():
        return None
    upper = str(code).strip().upper()
    if upper in ALIASES:
        return ALIASES[upper]
    return next((c for c in CENTERS if c.upper() == upper), str(code).strip())


def center_code(value):
    found = center(value)
    if found not in CENTERS:
        raise QueryError(f"Unknown center {value!r}. Use one of: {', '.join(CENTERS)}.")
    return found


def _plain(value):
    return (value or "").strip() or None


def record(kind, r):
    """A search result (13 values by position) with names. Positions 6-8 are
    used by software only, 10-11 by patents only."""
    r = list(r) + [""] * (13 - len(r))
    return {"kind": kind, "reference": _plain(r[1]), "title": clean(r[2]), "description": clean(r[3]),
            "category": clean(r[5]), "center": center(r[9]),
            "release": clean(r[6]) if kind == "software" else None,
            "how_to_get": clean(r[7]) if kind == "software" else None,
            "link": _plain(r[8]) if kind == "software" else None,
            "image": _plain(r[10]) if kind == "patent" else None,
            "tags": clean(r[11]) if kind == "patent" else None,
            "score": r[12] if isinstance(r[12], (int, float)) else None}


def combine(found, match):
    """found: {word: [records]}, one search per word. "all" keeps the records
    every word found; "any" keeps them all. Best total score first."""
    if match not in ("all", "any"):
        raise QueryError("match must be one of: all, any.")
    first, score, hits = {}, Counter(), Counter()
    for recs in found.values():
        for r in recs:
            key = (r["kind"], r["reference"])
            first.setdefault(key, r)
            score[key] += r["score"] or 0
            hits[key] += 1
    keys = [k for k in first if match == "any" or hits[k] == len(found)]
    keys.sort(key=lambda k: -score[k])                  # stable: ties keep the API's order
    return [dict(first[k], score=round(score[k], 4)) for k in keys]


def keep(records, *, center=None, category=None):
    """Records at one center (any spelling) and in categories containing the
    given text (any case)."""
    if center:
        wanted = center_code(center)
        records = [r for r in records if r["center"] == wanted]
    if category:
        wanted = category.casefold().strip()
        records = [r for r in records if wanted in (r["category"] or "").casefold()]
    return records


def _ranked(counter):
    return dict(counter.most_common())


def _category_key(cat):
    """Categories the same but for case and punctuation share a key:
    "Industrial Productivity/Manufacturing Technology" and
    "industrial productivity manufacturing technology"."""
    return " ".join(re.sub(r"[\W_]+", " ", cat.casefold()).split()) or cat


def counts(records):
    """Counts by kind, center and category. Categories that differ only in case
    or punctuation count together, under a capitalised form when there is one.
    Other upstream oddities (abbreviations such as "ip") are left as they are."""
    labels = {}
    for r in records:
        cat = r["category"] or "(none)"
        key = _category_key(cat)
        if key not in labels or (labels[key].islower() and not cat.islower()):
            labels[key] = cat
    return {"by_kind": _ranked(Counter(r["kind"] for r in records)),
            "by_center": _ranked(Counter(r["center"] or "(none)" for r in records)),
            "by_category": _ranked(Counter(labels[_category_key(r["category"] or "(none)")] for r in records))}


def row(rec, *, description=False, release=False):
    out = {"kind": rec["kind"], "reference": rec["reference"], "title": rec["title"], "category": rec["category"],
           "center": rec["center"]}
    if release:
        out["release"] = rec["release"]
    if description:
        text = rec["description"]
        out["description"] = text[:DESCRIPTION_CHARS] + "…" if text and len(text) > DESCRIPTION_CHARS else text
    return out


def kind_of(reference):
    """Which catalog a number belongs to, from its shape: patents KSC-TOPS-59 or
    TOP2-279, spinoffs KSC-SO-111, software everything else (ARC-17900-1)."""
    upper = reference.strip().upper()
    if "-SO-" in upper:
        return "spinoff"
    if "-TOPS-" in upper or re.match(r"TOP\d+-", upper):
        return "patent"
    return "software"


# ---------------------------------------------------------------- patent pages


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _PatentPage(html.parser.HTMLParser):
    """The parts of a technology.nasa.gov patent page that search doesn't
    return, found by their class names. Everything from "Similar Results" on
    belongs to other patents and is left out."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open = []              # (tag, classes) of elements not yet closed
        self.grabbing = []          # (depth, key, href, parts) of text being collected
        self.stopped = False
        self.items = {k: [] for k in ("subtitle", "technology", "benefits", "applications", "case_numbers",
                                      "patents", "papers")}

    def _inside(self, cls):
        return any(cls in classes for _, classes in self.open)

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if "similar_section" in classes:
            self.stopped = True
            return
        key = ("subtitle" if "subtitle" in classes else "technology" if "tech_desc" in classes
               else "case_numbers" if "case_number" in classes else "papers" if "publications" in classes
               else "benefits" if tag == "li" and self._inside("benefits")
               else "applications" if tag == "li" and self._inside("applications")
               else "patents" if tag == "a" and self._inside("patent_number") else None)
        if tag in VOID:
            return
        self.open.append((tag, classes))
        if key:
            self.grabbing.append((len(self.open), key, attrs.get("href"), []))

    def handle_endtag(self, tag):
        if self.stopped:
            return
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                while self.grabbing and self.grabbing[-1][0] > i:
                    _, key, href, parts = self.grabbing.pop()
                    text = " ".join("".join(parts).split())
                    if text:
                        self.items[key].append({"number": text, "link": href} if key == "patents" else text)
                del self.open[i:]
                return

    def handle_data(self, data):
        if not self.stopped:
            for _, _, _, parts in self.grabbing:
                parts.append(data)


def page_details(page):
    """Benefits, applications, case and patent numbers and papers from a patent
    page, or None when the page has none of them (it changed, or isn't one)."""
    parser = _PatentPage()
    parser.feed(page)
    parser.close()
    items = parser.items
    if not any(items.values()):
        return None
    return {"subtitle": (items["subtitle"] or [None])[0], "technology": (items["technology"] or [None])[0],
            "benefits": items["benefits"], "applications": items["applications"],
            "case_numbers": items["case_numbers"], "patents": items["patents"], "papers": items["papers"]}
