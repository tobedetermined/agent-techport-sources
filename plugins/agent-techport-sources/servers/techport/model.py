"""One shape for a TechPort project, whichever endpoint it came from.

TechPort returns projects in two shapes. The search endpoint uses snake_case
inside nested objects (organization_name, full_name) and fields such as
destinationTypes and modifiedDate; the single-project endpoint uses camelCase
(organizationName, fullName), destinationType and lastUpdated, and carries
file sizes. normalize() turns either into the same dict, so every tool and the
daily copy see one schema. Standard library only.
"""

import html
import json
import re

PROJECT_URL = "https://techport.nasa.gov/projects/{}"
FILE_URL = "https://techport.nasa.gov/api/file/{}"

# The single-project endpoint spells these with underscores; search uses words.
ORG_TYPES = {"NASA_Center": "NASA Center", "NASA_Facility": "NASA Facility", "NASA_Other": "NASA Other",
             "NASA_Program": "NASA Program", "FFRDC_UARC": "FFRDC/UARC",
             "Non_Profit_Institution": "Non-Profit Institution", "Other_US_Government": "Other US Government",
             "Industry": "Industry", "Academia": "Academia"}


def strip_html(text):
    if not text:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def iso_date(value):
    """Dates arrive as '2021-05-19', '2021-05-19T00:00:00Z', '2026-9-30' or '08/07/26'."""
    if not value:
        return None
    v = str(value).strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", v)
    if m:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)   # two-digit years are all 20xx here
        return f"{year:04d}-{int(m[1]):02d}-{int(m[2]):02d}"
    return None


def tx_code(code):
    """'TX6.4.1', '6.4.1' and 'TX06.4.1' all become 'TX06.4.1'."""
    if not code:
        return None
    raw = str(code).upper().removeprefix("TX")
    area, _, rest = raw.partition(".")
    if not area.isdigit():
        return f"TX{raw}"
    return f"TX{int(area):02d}" + (f".{rest}" if rest else "")


def _trl(value):
    # About 400 projects have TRL 0; NASA's scale starts at 1, so 0 means "not set".
    return value if isinstance(value, int) and value > 0 else None


def _g(d, *names):
    """First present value among the names (for snake_case vs camelCase)."""
    for n in names:
        if d and d.get(n) is not None:
            return d[n]
    return None


def _org(o, role=None):
    if not o:
        return None
    st = o.get("stateTerritory") or {}
    country = o.get("country") or st.get("country") or {}
    kind = _g(o, "organization_type", "organizationType")
    return {
        "organizationId": _g(o, "organization_id", "organizationId"),
        "name": _g(o, "organization_name", "organizationName"),
        "acronym": o.get("acronym"),
        "type": ORG_TYPES.get(kind, kind),
        "role": role or _g(o, "organization_role", "organizationRolePretty"),
        "city": o.get("city"),
        "state": _g(o, "state_abbreviation") or st.get("abbreviation"),
        "country": _g(o, "country_name") or country.get("name"),
        "duns": _g(o, "duns_number", "dunsNumber"),
        "msiCategory": _as_list(_g(o, "msi_category", "msiCategory")),
        "setAside": _as_list(_g(o, "set_aside_type", "setAsideType")),
    }


def _as_list(value):
    # Category fields come as a list, a JSON-encoded list in a string
    # ('["Small Disadvantaged Business (SDB)", ...]'), a plain string, or null.
    if value is None or isinstance(value, list):
        return value or []
    if isinstance(value, str) and value.startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            return [value]
    return [value]


def _contact(c, program=False):
    role = (_g(c, "program_contact_role", "programContactRolePretty") if program
            else _g(c, "project_contact_role", "projectContactRolePretty"))
    return {"name": _g(c, "full_name", "fullName"), "role": role or None,
            "email": c.get("email"), "orcid": c.get("orcid")}


def _outcome(o):
    related = o.get("related_project_id") or o.get("relatedProjectId")
    if not related and isinstance(o.get("relatedProject"), dict):
        related = o["relatedProject"].get("projectId")
    # The two shapes word paths differently ("Advanced from another project" in
    # search, "Advanced_From" plus an infoText sentence per project); filters
    # use the search wording, which is what the daily copy holds.
    return {
        "path": _g(o, "technology_outcome_path", "infoText", "technologyOutcomePathPretty"),
        "date": iso_date(_g(o, "technology_outcome_date", "technologyOutcomeDate")),
        "relatedProjectId": related,
        "partner": _g(o, "organization_name", "technology_outcome_partner", "technologyOutcomePartnerPretty"),
        "details": strip_html(o.get("details") or o.get("infoTextExtra")),
    }


def _library(items):
    """Split library items into downloadable files and links to other sites."""
    files, links = [], []
    for li in items or []:
        kind = _g(li, "library_item_type", "libraryItemType")
        title = li.get("title")
        found = li.get("files") or ([li["file"]] if isinstance(li.get("file"), dict) else [])
        if found:
            for f in found:
                fid = f.get("fileId") or f.get("file_id")
                if fid:
                    files.append({"fileId": fid, "title": title, "type": kind,
                                  "extension": f.get("fileExtension"), "bytes": f.get("fileSize"),
                                  "url": FILE_URL.format(fid)})
        elif li.get("file_id"):
            files.append({"fileId": li["file_id"], "title": title, "type": kind, "extension": None,
                          "bytes": None, "url": FILE_URL.format(li["file_id"])})
        elif li.get("url"):
            links.append({"title": title, "type": kind, "url": li["url"]})
    return files, links


def _destinations(p):
    d = p.get("destinationTypes", p.get("destinationType"))
    if isinstance(d, str):
        d = [x.strip() for x in d.split(",")]
    # The single-project shape writes "Moon_and_Cislunar"; search writes "Moon and Cislunar".
    return [x.replace("_", " ") for x in (d or []) if x and x != "N/A"]


def normalize(p):
    """A TechPort project in either shape → the plugin's project dict."""
    program = p.get("program") or {}
    md = p.get("responsibleMd") or program.get("responsibleMd") or {}
    primary = p.get("primaryTx") or {}
    files, links = _library(p.get("libraryItems"))
    for c in p.get("closeoutDocumentation") or []:      # search shape only
        if isinstance(c, dict) and c.get("file_id"):
            files.append({"fileId": c["file_id"], "title": c.get("title"), "type": "Closeout document",
                          "extension": None, "bytes": None, "url": FILE_URL.format(c["file_id"])})
        elif isinstance(c, dict) and c.get("closeout_link_url"):
            links.append({"title": c.get("title"), "type": "Closeout link", "url": c["closeout_link_url"]})
    lead = _org(p.get("leadOrganization"), "Lead Organization")
    return {
        "projectId": p.get("projectId"),
        "title": p.get("title"),
        "acronym": p.get("acronym"),
        "status": p.get("status"),
        "program": {"acronym": program.get("acronym"), "title": program.get("title"),
                    "programId": _g(program, "program_id", "programId") or p.get("programId")},
        "missionDirectorate": _g(md, "acronym") or program.get("responsible_md_acronym"),
        "phase": p.get("phase"),
        "startDate": iso_date(p.get("startDate")),
        "endDate": iso_date(p.get("endDate")),
        "lastUpdated": iso_date(p.get("lastUpdated") or p.get("modifiedDate")),
        "trlBegin": _trl(p.get("trlBegin")),
        "trlCurrent": _trl(p.get("trlCurrent")),
        "trlEnd": _trl(p.get("trlEnd")),
        "primaryTx": {"code": tx_code(primary.get("code")), "title": primary.get("title")} if primary else None,
        "additionalTx": [{"code": tx_code(t.get("code")), "title": t.get("title")}
                         for t in p.get("additionalTxs") or [] if t.get("code")],
        "destinations": _destinations(p),
        "leadOrganization": lead,
        "otherOrganizations": [_org(o) for o in p.get("otherOrganizations") or []],
        "states": sorted({s.get("abbreviation") for s in p.get("states") or [] if s.get("abbreviation")}),
        "description": strip_html(p.get("description")),
        "benefits": strip_html(p.get("benefits")),
        "contacts": [_contact(c) for c in p.get("projectContacts") or []],
        "programContacts": [_contact(c, program=True) for c in p.get("programContacts") or []],
        "outcomes": [_outcome(o) for o in p.get("technologyOutcomes") or []],
        "files": files,
        "links": links,
        "viewCount": p.get("viewCount"),
        "url": PROJECT_URL.format(p.get("projectId")),
    }


def merge(detail, search):
    """The single-project record, plus what only the search shape carries:
    phase, set-aside categories, and closeout documents."""
    if not search:
        return detail
    out = dict(detail)
    out["phase"] = detail.get("phase") or search.get("phase")
    if detail.get("leadOrganization") and search.get("leadOrganization"):
        lead = dict(detail["leadOrganization"])
        for k, v in search["leadOrganization"].items():
            if lead.get(k) in (None, "", []):
                lead[k] = v
        out["leadOrganization"] = lead
    have = {f["fileId"] for f in detail.get("files") or []}
    out["files"] = list(detail.get("files") or []) + [f for f in search.get("files") or [] if f["fileId"] not in have]
    urls = {l["url"] for l in detail.get("links") or []}
    out["links"] = list(detail.get("links") or []) + [l for l in search.get("links") or [] if l["url"] not in urls]
    return out


def without_contact_details(project):
    """Names and roles stay; emails and ORCIDs go, unless the caller asked for them."""
    out = dict(project)
    for key in ("contacts", "programContacts"):
        out[key] = [{"name": c["name"], "role": c["role"]} for c in project.get(key) or []]
    return out
