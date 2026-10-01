"""Download the SBIR.gov bulk award file and load it into a local SQLite database.

Standard library only. The file comes from SBIR.gov and nowhere else; it is
downloaded again only when its Last-Modified date changes. See the "SBIR
connector" section of docs/design.md for why this file and what the quirks are.
"""

import csv
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from ..common import dbfiles
from ..common.http import HttpClient

SOURCE_URL = "https://data.www.sbir.gov/mod_awarddatapublic/award_data.csv"
SCHEMA_VERSION = "2"      # 2: placeholder words load as NULL; an older database is rebuilt

# (CSV header, column name, type). The order matches the file, which is checked
# on load: a changed header stops the load rather than mis-mapping columns.
COLUMNS = [
    ("Company", "company", "TEXT"),
    ("Award Title", "award_title", "TEXT"),
    ("Agency", "agency", "TEXT"),
    ("Branch", "branch", "TEXT"),
    ("Phase", "phase", "INTEGER"),
    ("Program", "program", "TEXT"),
    ("Agency Tracking Number", "agency_tracking_number", "TEXT"),
    ("Contract", "contract", "TEXT"),
    ("Proposal Award Date", "proposal_award_date", "TEXT"),
    ("Contract End Date", "contract_end_date", "TEXT"),
    ("Solicitation Number", "solicitation_number", "TEXT"),
    ("Solicitation Year", "solicitation_year", "INTEGER"),
    ("Solicitation Close Date", "solicitation_close_date", "TEXT"),
    ("Proposal Receipt Date", "proposal_receipt_date", "TEXT"),
    ("Date of Notification", "date_of_notification", "TEXT"),
    ("Topic Code", "topic_code", "TEXT"),
    ("Award Year", "award_year", "INTEGER"),
    ("Award Amount", "award_amount", "REAL"),
    ("UEI", "uei", "TEXT"),
    ("Duns", "duns", "TEXT"),
    ("HUBZone Owned", "hubzone_owned", "TEXT"),
    ("Socially and Economically Disadvantaged", "socially_economically_disadvantaged", "TEXT"),
    ("Woman Owned", "woman_owned", "TEXT"),
    ("Number Employees", "number_employees", "INTEGER"),
    ("Company Website", "company_website", "TEXT"),
    ("Address1", "address1", "TEXT"),
    ("Address2", "address2", "TEXT"),
    ("City", "city", "TEXT"),
    ("State", "state", "TEXT"),
    ("Zip", "zip", "TEXT"),
    ("Abstract", "abstract", "TEXT"),
    ("Contact Name", "contact_name", "TEXT"),
    ("Contact Title", "contact_title", "TEXT"),
    ("Contact Phone", "contact_phone", "TEXT"),
    ("Contact Email", "contact_email", "TEXT"),
    ("PI Name", "pi_name", "TEXT"),
    ("PI Title", "pi_title", "TEXT"),
    ("PI Phone", "pi_phone", "TEXT"),
    ("PI Email", "pi_email", "TEXT"),
    ("RI Name", "ri_name", "TEXT"),
    ("RI POC Name", "ri_poc_name", "TEXT"),
    ("RI POC Phone", "ri_poc_phone", "TEXT"),
]
HEADER = [h for h, _, _ in COLUMNS]

# Columns that only appear in tool output when the caller asks for contacts.
CONTACT_COLUMNS = [
    "contact_name", "contact_title", "contact_phone", "contact_email",
    "pi_title", "pi_phone", "pi_email", "ri_poc_name", "ri_poc_phone",
]

# The file uses full agency names; the data dictionary uses codes. Both are kept.
AGENCY_CODES = {
    "Department of Agriculture": "USDA",
    "Department of Commerce": "DOC",
    "Department of Defense": "DOD",
    "Department of Education": "ED",
    "Department of Energy": "DOE",
    "Department of Health and Human Services": "HHS",
    "Department of Homeland Security": "DHS",
    "Department of the Interior": "DOI",
    "Department of Transportation": "DOT",
    "Environmental Protection Agency": "EPA",
    "National Aeronautics and Space Administration": "NASA",
    "National Science Foundation": "NSF",
    "Nuclear Regulatory Commission": "NRC",
}

# The file uses full state and territory names.
STATE_CODES = {
    "Alabama": "AL", "Alaska": "AK", "American Samoa": "AS", "Arizona": "AZ",
    "Arkansas": "AR", "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Guam": "GU", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Marshall Islands": "MH",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Northern Mariana Islands": "MP", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Puerto Rico": "PR",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virgin Islands": "VI", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

PHASES = {"Phase I": 1, "Phase II": 2}

# Values the file uses to mean "no value". Only unambiguous placeholders are
# listed; anything that might be real data is kept.
PLACEHOLDERS = {"() -", "-", "Not Available"}
# Whole values, in any case: N/A alone is on 28,210 abstracts (2026-09-01 file).
PLACEHOLDER_WORDS = {"n/a", "na", "none", "null"}

# Name fields where a blank middle initial leaves a double space ("Jane  Doe").
NAME_COLUMNS = {"company", "contact_name", "pi_name", "ri_poc_name"}


class SbirDataUnavailable(RuntimeError):
    """No local database, and SBIR.gov could not be reached."""


def default_data_dir():
    """Where the database lives: the plugin's data directory, or a local fallback."""
    base = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.getcwd(), "data")
    return os.path.join(base, "sbir")


# ---------------------------------------------------------------- cleaning


def _clean(column, value):
    value = value.strip()
    if column in NAME_COLUMNS:
        value = " ".join(value.split())
    if value == "" or value in PLACEHOLDERS or value.lower() in PLACEHOLDER_WORDS:
        return None
    return value


def _to_int(value):
    return int(value) if value.isdigit() else None


def _to_float(value):
    try:
        return float(value)
    except ValueError:
        return None


def clean_row(raw, bad_numbers=None):
    """Turn one CSV row (42 strings) into database values, plus the two codes.

    A non-blank number that doesn't parse is stored as NULL and its column name
    appended to bad_numbers, so the caller can report it.
    """
    out = {}
    for (_, column, kind), value in zip(COLUMNS, raw):
        value = _clean(column, value)
        if column == "phase":
            value = PHASES.get(value)
        elif kind in ("INTEGER", "REAL") and value is not None:
            parsed = _to_int(value) if kind == "INTEGER" else _to_float(value)
            if parsed is None and bad_numbers is not None:
                bad_numbers.append(f"{column}={value!r}")
            value = parsed
        out[column] = value
    out["agency_code"] = AGENCY_CODES.get(out["agency"])
    out["state_code"] = STATE_CODES.get(out["state"])
    return out


# ---------------------------------------------------------------- loading


def _create_schema(db):
    cols = ",\n  ".join(f"{c} {t}" for _, c, t in COLUMNS)
    db.executescript(f"""
        CREATE TABLE awards (
          id INTEGER PRIMARY KEY,
          {cols},
          agency_code TEXT,
          state_code TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE load_issues (line INTEGER, issue TEXT, detail TEXT);
    """)


def _create_indexes(db):
    db.executescript("""
        CREATE INDEX awards_agency_year ON awards (agency_code, award_year);
        CREATE INDEX awards_year ON awards (award_year);
        CREATE INDEX awards_uei ON awards (uei);
        CREATE INDEX awards_company ON awards (company COLLATE NOCASE);
        CREATE INDEX awards_tracking_contract ON awards (agency_tracking_number, contract);
        CREATE INDEX awards_contract ON awards (contract);
        CREATE VIRTUAL TABLE awards_fts USING fts5(
          award_title, abstract, content='awards', content_rowid='id'
        );
        INSERT INTO awards_fts (awards_fts) VALUES ('rebuild');
    """)


def load_csv(csv_path, db_path, source=None):
    """Build a fresh database at db_path from the CSV. Returns the load summary.

    Rows with the wrong number of fields are skipped and listed in the
    load_issues table, never dropped silently. Unknown agency, state or phase
    values are loaded as they are and also listed there, as are numbers that
    don't parse (stored as NULL).
    """
    started = time.time()
    csv.field_size_limit(2**31 - 1)   # sys.maxsize overflows on Windows
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    try:
        _create_schema(db)
        names = [c for _, c, _ in COLUMNS] + ["agency_code", "state_code"]
        insert = "INSERT INTO awards ({}) VALUES ({})".format(
            ",".join(names), ",".join("?" * len(names)))
        issues = []
        rows = 0
        batch = []
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header = next(reader)
            if header != HEADER:
                raise ValueError(
                    "SBIR file header has changed; the loader needs updating. "
                    f"Expected {len(HEADER)} columns, got {header!r}")
            for raw in reader:
                if len(raw) != len(HEADER):
                    issues.append((reader.line_num, "malformed_row",
                                   f"{len(raw)} fields"))
                    continue
                bad_numbers = []
                row = clean_row(raw, bad_numbers)
                issues.extend((reader.line_num, "bad_number", b) for b in bad_numbers)
                for column, table in (("agency", AGENCY_CODES), ("state", STATE_CODES)):
                    if row[column] is not None and row[column] not in table:
                        issues.append((reader.line_num, f"unknown_{column}", row[column]))
                phase = raw[HEADER.index("Phase")].strip()
                if phase and phase not in PHASES:
                    issues.append((reader.line_num, "unknown_phase", phase))
                batch.append([row[n] for n in names])
                rows += 1
                if len(batch) >= 20000:
                    db.executemany(insert, batch)
                    batch = []
        db.executemany(insert, batch)
        db.executemany("INSERT INTO load_issues VALUES (?,?,?)", issues)
        _create_indexes(db)
        malformed = sum(1 for _, kind, _ in issues if kind == "malformed_row")
        meta = {
            "schema_version": SCHEMA_VERSION,
            "loaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows_loaded": str(rows),
            "malformed_rows": str(malformed),
            "other_issues": str(len(issues) - malformed),
            "load_seconds": f"{time.time() - started:.1f}",
        }
        meta.update(source or {})
        db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        db.commit()
    finally:
        db.close()
    return meta


# ---------------------------------------------------------------- fetching


class SbirHTTP:
    """The two calls the loader makes, both to SBIR.gov, through the plugin's
    shared client (which refuses any other host, redirects included)."""

    def __init__(self):
        self.http = HttpClient("SBIR.gov", {"data.www.sbir.gov"})

    def head(self, url):
        h = self.http.head(url)
        return {"last_modified": h.get("last-modified"), "etag": h.get("etag"), "bytes": h.get("content-length")}

    def download(self, url, dest):
        self.http.download(url, dest)


def read_meta(db_path):
    return dbfiles.read_meta(db_path)


def open_current(data_dir):
    """Open whatever database the pointer names now. Returns (connection, meta).

    Readers call this for every query, not once at start: another Claude
    session may have refreshed the data and deleted the file this one was using.
    """
    return dbfiles.open_current(data_dir, SbirDataUnavailable(
        "No SBIR database found. It is loaded when the SBIR server starts."))


def current_db_path(data_dir):
    """The database the pointer file names, or None before the first load."""
    return dbfiles.current_path(data_dir)


def ensure_database(data_dir=None, csv_override=None, http=None, url=SOURCE_URL):
    """Return (db_path, status), building or refreshing the database if needed.

    status is one of "current", "loaded", "refreshed", or "stale: <reason>"
    (SBIR.gov was unreachable or the refresh failed, so the existing database
    is used as it is). With no database to fall back on, a failure raises
    SbirDataUnavailable.

    With csv_override, that local file is loaded instead of downloading, and
    reloaded whenever its size or modification time changes.
    """
    data_dir = data_dir or default_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    _remove_old_databases(data_dir)
    db_path = current_db_path(data_dir)
    meta = read_meta(db_path) if db_path else None
    if meta and meta.get("schema_version") != SCHEMA_VERSION:
        meta = None

    if csv_override:
        st = os.stat(csv_override)
        source = {"source": os.path.abspath(csv_override),
                  "source_last_modified": str(int(st.st_mtime)),
                  "source_bytes": str(st.st_size)}
        if meta and all(meta.get(k) == v for k, v in source.items()):
            return db_path, "current"
        return _build(csv_override, data_dir, source), "refreshed" if meta else "loaded"

    http = http or SbirHTTP()
    part = os.path.join(data_dir, f"award_data.{os.getpid()}.csv.part")
    try:
        remote = http.head(url)
        version = {"source_last_modified": remote.get("last_modified") or "",
                   "source_etag": remote.get("etag") or ""}
        if meta and meta.get("source") == url and _same_version(meta, version):
            return db_path, "current"
        http.download(url, part)
        got = os.path.getsize(part)
        if remote.get("bytes") and str(got) != str(remote["bytes"]):
            raise ValueError(f"download incomplete: {got} of {remote['bytes']} bytes")
        new_path = _build(part, data_dir, {"source": url, **version, "source_bytes": str(got)})
    except Exception as e:
        # Anything from a dropped connection to a maintenance page served in
        # place of the file. Keep the database we have; it is still valid.
        if meta:
            return db_path, f"stale: {type(e).__name__}: {e}"
        raise SbirDataUnavailable(
            f"No local SBIR database yet, and loading {url} failed: {e}") from e
    finally:
        if os.path.exists(part):
            os.remove(part)
    return new_path, "refreshed" if meta else "loaded"


def _same_version(meta, version):
    """Compare by Last-Modified, else ETag. With neither, assume unchanged
    rather than download 400 MB on every start."""
    for key in ("source_last_modified", "source_etag"):
        if version[key]:
            return meta.get(key) == version[key]
    return True


def _build(csv_path, data_dir, source):
    """Load into a new, uniquely named file, then point to it (see common/dbfiles.py)."""
    return dbfiles.build(data_dir, "sbir", lambda path: load_csv(csv_path, path, source))


def _remove_old_databases(data_dir):
    dbfiles.remove_old(data_dir, "sbir")
