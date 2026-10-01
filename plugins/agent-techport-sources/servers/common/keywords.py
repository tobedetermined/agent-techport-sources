"""Keyword syntax shared by the SQLite full-text searches (SBIR and the TechPort
copy), and its OR alternatives for USAspending. Standard library only.

The syntax matches NTRS's, so a query reads the same on every server:
words must all appear; "a phrase" stays together; OR or | between terms;
(parentheses) group; -word excludes; word* matches a prefix. Anything else is
quoted, so hyphens and punctuation can't break SQLite's query syntax.
"""

import re

SYNTAX = ('words must all appear; "a phrase"; OR or | between alternatives; (parentheses) to group; '
          '-word to exclude; word* for a prefix')

_TOKEN = re.compile(r'"[^"]*"|[()|]|[^\s()|"]+')


class KeywordError(ValueError):
    """Keywords that can't be turned into a query. The message gives the syntax."""


def _quote(word):
    return '"' + word.replace('"', '""') + '"'


def _term(token):
    """One search term in FTS5 form, or None when nothing searchable is left."""
    if token.startswith('"'):
        inner = token.strip('"').strip()
        return _quote(inner) if inner else None
    prefix = token.endswith("*") and len(token.rstrip("*")) > 0
    inner = token.rstrip("*") if prefix else token
    if not re.search(r"\w", inner):
        return None
    return _quote(inner) + ("*" if prefix else "")


def fts_query(text, field="keywords"):
    """Free text to a safe FTS5 query in the shared syntax."""
    out, excluded, depth = [], [], 0
    for token in _TOKEN.findall(text or ""):
        if token in ("|", "OR"):
            if out and out[-1] not in ("OR", "("):
                out.append("OR")
        elif token == "AND":
            continue                                    # every word must appear anyway
        elif token == "(":
            depth += 1
            out.append("(")
        elif token == ")":
            if depth == 0:
                raise KeywordError(f"{field} has a ')' with no '(' before it. Syntax: {SYNTAX}.")
            while out and out[-1] == "OR":
                out.pop()
            if out and out[-1] == "(":                  # an empty group
                out.pop()
            else:
                out.append(")")
            depth -= 1
        elif token.startswith("-") and len(token) > 1 and not token.startswith("--"):
            if depth:
                raise KeywordError(f"{field}: -word can't be used inside parentheses. Syntax: {SYNTAX}.")
            term = _term(token[1:])
            if term:
                excluded.append(term)
        else:
            term = _term(token)
            if term:
                out.append(term)
    if depth:
        raise KeywordError(f"{field} has a '(' that is never closed. Syntax: {SYNTAX}.")
    while out and out[-1] == "OR":
        out.pop()
    if not out:
        raise KeywordError(f"{field} has nothing to search for" + (" but exclusions" if excluded else "")
                           + f". Syntax: {SYNTAX}.")
    # FTS5 won't take an implicit AND before a group ('"a" ("b" OR "c")'), so
    # every AND is written out.
    joined = []
    for item in out:
        if joined and joined[-1] not in ("OR", "(") and item not in ("OR", ")"):
            joined.append("AND")
        joined.append(item)
    query = " ".join(joined)
    if excluded:
        query = f"({query}) " + " ".join(f"NOT {t}" for t in excluded)
    return query


def alternatives(text, field="keywords", min_length=3):
    """The OR alternatives in text, for sources whose keyword filter takes a list
    of alternatives (USAspending): "a | b" and "a OR b" give ["a", "b"].
    Grouping and exclusion have no equivalent there, so they are refused rather
    than searched for as words."""
    if re.search(r"[()]", text or "") or re.search(r"(^|\s)-\w", text or ""):
        raise KeywordError(f"{field} here takes words, \"phrases\", and OR or | between alternatives; "
                           "parentheses and -word exclusions aren't supported by this source.")
    parts = [p.replace('"', " ").strip() for p in re.split(r"\||\bOR\b", text or "")]
    parts = [" ".join(p.split()) for p in parts if p.strip()]
    if not parts:
        raise KeywordError(f"{field} is empty.")
    short = [p for p in parts if len(p) < min_length]
    if short:
        raise KeywordError(f"each alternative in {field} needs at least {min_length} characters: {short}.")
    return parts
