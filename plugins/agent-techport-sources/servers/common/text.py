"""Long text in parts, and the passages that mention given words. Used for
NTRS's extracted text and SEC filings. Standard library only."""

import bisect
import re

PART_CHARS = 12_000         # text per part, as for lists (common/output.py)
PASSAGE_CHARS = 250         # text kept either side of a match


class NoWords(ValueError):
    """A find with no words to look for."""


def split(text, size=PART_CHARS):
    """(start, end) of each part: at most size characters, ending at a line
    break when one falls in the part's last quarter."""
    spans, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size * 3 // 4, end)
            if cut != -1:
                end = cut + 1
        spans.append((start, end))
        start = end
    return spans


_PAGE = re.compile(r"^\[page (\d+)\]$", re.M)


def _pages(text):
    return [(m.start(), int(m.group(1))) for m in _PAGE.finditer(text)]


def _page(marks, pos):
    i = bisect.bisect_right([start for start, _ in marks], pos)
    return marks[i - 1][1] if i else None


def page_at(text, pos):
    """The page a position is on, when the text has page markers."""
    return _page(_pages(text), pos)


def _part(spans, pos):
    i = bisect.bisect_right([start for start, _ in spans], pos)
    return max(i, 1)


def _term(words):
    """A pattern for whole words, with any spacing or punctuation between them
    (as search engines match a phrase: "(SBIR) Phase III" is "SBIR Phase III");
    a final * makes the last word a prefix ("Figure 1" doesn't match Figure 10;
    cryogen* matches cryogenic)."""
    prefix = words[-1].endswith("*")
    words = words[:-1] + [words[-1].rstrip("*")] if prefix else words
    words = [w for w in words if w]
    if not words:
        return None
    return r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + ("" if prefix else r"(?!\w)")


def find_pattern(find):
    """The regular expression for a find (as passages takes it), or None when
    it has no words."""
    terms = [p for p in (_term(t.split()) for t in (find or "").split("|") if t.strip()) if p]
    return re.compile("|".join(terms), re.I) if terms else None


def passages(text, find, *, spans, width=PASSAGE_CHARS):
    """The text around each match of find: whole words or phrases, any case,
    with | between alternatives and * for a prefix. Matches close together
    share one passage. Each gives the part (of spans) and page where it starts."""
    pattern = find_pattern(find)
    if pattern is None:
        raise NoWords("find needs words to look for.")
    windows = []                        # [start, end, matches, first match]
    for m in pattern.finditer(text):
        start, end = max(0, m.start() - width), min(len(text), m.end() + width)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
            windows[-1][2] += 1
        else:
            windows.append([start, end, 1, m.start()])
    marks = _pages(text)
    return [{"part": _part(spans, first), "page": _page(marks, first), "matches": n,
             "text": ("…" if start else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")}
            for start, end, n, first in windows]
