"""The daily copy of TechPort, for the questions the live API can't answer.

TechPort's API can search by keyword but can't filter, page or count. An empty
search returns every project (21,044 on 2026-09-30, 115 MB). This module pulls
that at most once a day, when a tool first needs it, and loads it into SQLite
for aggregates, listings without a keyword, contact search and batch lookups.
Every answer from it states its date. Standard library only.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from ..common import dbfiles
from ..common.jsonstream import iter_array
from .model import normalize

SEARCH_URL = "https://techport.nasa.gov/api/projects/search"
SCHEMA_VERSION = "1"
PREFIX = "techport"
MAX_AGE_SECONDS = 24 * 3600
# A load more than 10% short of the expected count is rejected and the old copy
# kept. TechPort's search has failed before by returning a fixed 50 results.
MIN_SHARE = 0.9


class CopyUnavailable(RuntimeError):
    """No copy yet, and TechPort couldn't be reached to make one."""


class ShortLoad(ValueError):
    """TechPort returned far fewer projects than expected; the load was rejected."""


def default_data_dir():
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "techport")


SCHEMA = """
CREATE TABLE projects (
  id INTEGER PRIMARY KEY, title TEXT, acronym TEXT, status TEXT,
  program TEXT, program_title TEXT, program_id INTEGER, mission_directorate TEXT, phase TEXT,
  start_date TEXT, end_date TEXT, start_year INTEGER, end_year INTEGER, last_updated TEXT,
  trl_begin INTEGER, trl_current INTEGER, trl_end INTEGER,
  tx TEXT, tx_title TEXT, tx1 TEXT, tx2 TEXT,
  lead_org_id INTEGER, lead_org TEXT, lead_org_type TEXT, lead_org_state TEXT,
  lead_org_country TEXT, lead_org_duns TEXT,
  view_count INTEGER, rank INTEGER, record TEXT
);
CREATE TABLE destinations (project_id INTEGER, destination TEXT);
CREATE TABLE outcomes (project_id INTEGER, path TEXT, date TEXT, related_project_id INTEGER);
CREATE TABLE org_categories (project_id INTEGER, kind TEXT, category TEXT);  -- lead org's MSI and set-aside
CREATE TABLE contacts (project_id INTEGER, kind TEXT, name TEXT, role TEXT, email TEXT, orcid TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE projects_fts USING fts5(title, acronym, description, benefits);
"""

INDEXES = """
CREATE INDEX p_program ON projects (program);
CREATE INDEX p_status ON projects (status);
CREATE INDEX p_tx ON projects (tx);
CREATE INDEX p_lead ON projects (lead_org COLLATE NOCASE);
CREATE INDEX d_project ON destinations (project_id);
CREATE INDEX o_project ON outcomes (project_id);
CREATE INDEX c_name ON contacts (name COLLATE NOCASE);
CREATE INDEX c_project ON contacts (project_id);
"""


def _year(date):
    return int(date[:4]) if date else None


def _tx_levels(code):
    if not code:
        return None, None
    parts = code.split(".")
    return parts[0], ".".join(parts[:2]) if len(parts) > 1 else None


def insert_project(db, p, rank=None):
    """Write one normalised project into the tables above. rank is TechPort's
    search order, set for live search results and NULL in the daily copy."""
    lead = p["leadOrganization"] or {}
    tx = (p["primaryTx"] or {}).get("code")
    tx1, tx2 = _tx_levels(tx)
    db.execute("INSERT INTO projects VALUES (" + ",".join("?" * 30) + ")", (
        p["projectId"], p["title"], p["acronym"], p["status"],
        p["program"]["acronym"], p["program"]["title"], p["program"]["programId"],
        p["missionDirectorate"], p["phase"],
        p["startDate"], p["endDate"], _year(p["startDate"]), _year(p["endDate"]), p["lastUpdated"],
        p["trlBegin"], p["trlCurrent"], p["trlEnd"],
        tx, (p["primaryTx"] or {}).get("title"), tx1, tx2,
        lead.get("organizationId"), lead.get("name"), lead.get("type"), lead.get("state"),
        lead.get("country"), lead.get("duns"),
        p["viewCount"], rank, json.dumps(p)))
    db.executemany("INSERT INTO destinations VALUES (?,?)", [(p["projectId"], d) for d in p["destinations"]])
    db.executemany("INSERT INTO outcomes VALUES (?,?,?,?)",
                   [(p["projectId"], o["path"], o["date"], o["relatedProjectId"]) for o in p["outcomes"]])
    db.executemany("INSERT INTO org_categories VALUES (?,?,?)",
                   [(p["projectId"], kind, c) for kind, key in (("msi", "msiCategory"), ("set_aside", "setAside"))
                    for c in lead.get(key) or []])
    db.executemany("INSERT INTO contacts VALUES (?,?,?,?,?,?)",
                   [(p["projectId"], kind, c["name"], c["role"], c["email"], c["orcid"])
                    for kind, key in (("project", "contacts"), ("program", "programContacts"))
                    for c in p[key] if c["name"]])
    db.execute("INSERT INTO projects_fts (rowid, title, acronym, description, benefits) VALUES (?,?,?,?,?)",
               (p["projectId"], p["title"], p["acronym"], p["description"], p["benefits"]))


def memory_db(projects):
    """An in-memory database with the copy's tables, holding live search results
    in TechPort's order, so live and copy results go through the same SQL."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(SCHEMA)
    seen = set()
    for rank, p in enumerate(projects):
        if p["projectId"] not in seen:        # guard: a repeated id would break the primary key
            seen.add(p["projectId"])
            insert_project(db, p, rank)
    db.executescript(INDEXES)
    return db


def load(json_path, db_path, source=None, expected=None):
    """Build the copy from TechPort's full search response saved at json_path.
    With expected (a project count), a load short by more than 10% is rejected."""
    started = time.time()
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        n = 0
        seen = set()
        for raw in iter_array(json_path, "results"):
            p = normalize(raw)
            if p["projectId"] in seen:
                continue
            seen.add(p["projectId"])
            insert_project(db, p)
            n += 1
        if n == 0:
            raise ShortLoad("TechPort returned no projects")
        if expected and n < MIN_SHARE * expected:
            raise ShortLoad(f"TechPort returned {n:,} projects where about {expected:,} were expected; "
                            "the previous copy is kept")
        db.executescript(INDEXES)
        meta = {"schema_version": SCHEMA_VERSION, "projects": str(n),
                "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "load_seconds": f"{time.time() - started:.1f}", **(source or {})}
        db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        db.commit()
    finally:
        db.close()
    return meta


def _age_seconds(meta):
    try:
        loaded = datetime.fromisoformat(meta["loaded_at"])
    except (KeyError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - loaded).total_seconds()


def status(data_dir):
    """(meta or None, is_fresh) for the current copy."""
    path = dbfiles.current_path(data_dir)
    meta = dbfiles.read_meta(path) if path else None
    if meta and meta.get("schema_version") != SCHEMA_VERSION:
        meta = None
    return meta, bool(meta) and _age_seconds(meta) < MAX_AGE_SECONDS


def refresh(data_dir, http, json_override=None, expected=None):
    """Pull the full project list and build a new copy. Returns its meta.

    expected: roughly how many projects there should be. Defaults to the
    current copy's count; the caller passes TechPort's id count on first build.
    """
    os.makedirs(data_dir, exist_ok=True)
    meta, _ = status(data_dir)
    expected = expected or (int(meta["projects"]) if meta else None)
    if json_override:
        source = {"source": os.path.abspath(json_override)}
        return dbfiles.read_meta(dbfiles.build(data_dir, PREFIX, lambda p: load(json_override, p, source, expected)))
    part = os.path.join(data_dir, f"search.{os.getpid()}.json.part")
    try:
        size = http.download(SEARCH_URL, part)
        source = {"source": SEARCH_URL, "source_bytes": str(size)}
        return dbfiles.read_meta(dbfiles.build(data_dir, PREFIX, lambda p: load(part, p, source, expected)))
    finally:
        if os.path.exists(part):
            os.remove(part)


def open_copy(data_dir):
    return dbfiles.open_current(data_dir, CopyUnavailable(
        "No local TechPort copy yet; it is made the first time a question needs it."))
