# Agent TechPort Sources: design note

Status: **in development, plugin version 0.1.1.** The SBIR, TechPort and
USAspending servers are built and tested; the other three sources are planned. Decisions and
evidence date from 2026-09-30. Every number below was measured unless it is
marked *assumed*.

## What it is

A Claude Code plugin that gives Claude grounded access to the public data
sources used in NASA space technology research: what NASA funded, what came of
it, where the money went, and how the companies are doing. It is meant for NASA
staff and for outside researchers alike. Studies that use it install it like
any other user; no study-specific code or data lives in this repo.

## Decisions

| Item | Decision |
|---|---|
| Name | Display name "Agent TechPort Sources"; plugin, marketplace and repo id `agent-techport-sources`. Renamed 2026-09-30 from "NASA Space Tech Research Plugin": as a personal project, a name with "NASA" in it could suggest NASA endorses it (51 U.S.C. § 20141; NASA's brand guidance says "NASA X (Brand Name) ... is also prohibited"). The READMEs say it is independent and not endorsed by NASA, the US government or Anthropic (Anthropic added 2026-09-30, since the credit line names Claude Opus 5.5). *Not checked:* whether "TechPort", NASA's system name, is a registered trademark |
| Distribution | One GitHub repo that is both the marketplace and the plugin (`.claude-plugin/marketplace.json` with plugin `source: "./plugins/agent-techport-sources"`). Published from one squashed commit; the full history stays local (open question 1) |
| Licence | Apache-2.0, decided 2026-09-30, for a personal open-source release. `LICENSE` is at the repo root and inside the plugin folder (only that folder reaches users). All 30 packages in `uv.lock` are permissive: MIT, BSD-3-Clause, Apache-2.0, MIT-0, PSF (checked from the installed packages' metadata; the two not installed on macOS, `pywin32` and `httpx2-jsfetch`, on PyPI) |
| Sources (v1) | TechPort, NTRS, USAspending, NASA Technology Transfer, SEC EDGAR, SBIR (public bulk CSV) |
| TechPort access | The plugin's own local stdio server calling `techport.nasa.gov` directly. Hosted TechPort MCP connectors are not used or depended on |
| Routing | Local stdio MCP servers that call the government endpoints directly. No third-party servers, no hosted relay, no telemetry. The only network calls go to the source hosts, plus PyPI at install time (and Astral's `releases.astral.sh` if uv has to download Python; see "Install-time hosts") |
| Language | Python 3.11+, run with `uv`. Declared dependencies: the official MCP SDK (`mcp`), and `truststore` for checking HTTPS certificates against the operating system's trust store (see "HTTPS certificates"). `truststore` was already installed as part of the SDK, so the set is unchanged: 30 packages, all from PyPI, pinned in the committed `uv.lock`. Calls to the source APIs use the standard library (`urllib`). Tests use the standard library's `unittest`, so there are no dev dependencies |
| Self-contained | Nothing needs to exist on the user's machine beforehand. The SBIR data is downloaded from SBIR.gov on first use |
| SBIR contact data | Kept. All 42 columns are loaded, including the public PI and contact fields. Tool output shows company and PI name by default; titles, phones, emails, the "Contact" fields and the research-institution contact only when the caller asks for them |
| Scope in time | The last decade (2015+) is the main working window. Older records load but aren't guaranteed to be complete |

### Why Python

The main pitch is that NASA reviewers can read the code and trust it, and
Python is the language that audience reads. It also suits the SBIR/SQLite data
work. Go was the fallback, for when no runtime is allowed. It isn't needed:
Python and uv installed and fetched packages from PyPI on a managed NASA Mac,
with no admin rights. That was one machine that already had a user-local
Homebrew. So the README should lead with uv's standalone user-level installer,
not Homebrew. Test it on a colleague's standard machine before calling it
NASA-ready.

All the official MCP SDKs for TypeScript, Python, Go, C#, Rust and Ruby are
Tier 1, so SDK maturity doesn't decide this. Claude Code doesn't supply any
runtime; its native installer is a standalone binary.

### Anthropic is still in the loop

The plugin can guarantee that the plumbing adds no party beyond the source
hosts. It can't take Anthropic out of the loop, because prompts and tool
results go to the model. Say so plainly in the README. Whether Claude Code is
approved for use at NASA is a separate question from whether this plugin is
clean.

## Sources: probe results (2026-09-30)

| Source | Endpoint probed | Result |
|---|---|---|
| TechPort | `techport.nasa.gov/api/projects/search` | 200, no key (details in "TechPort connector") |
| NTRS | `ntrs.nasa.gov/api/citations/search` | 200, no key (details in "NTRS connector") |
| USAspending | `api.usaspending.gov/api/v2/...` | 200, no key |
| NASA Tech Transfer | `technology.nasa.gov/api/api/patent/...` | 200, no key (details in "NASA Technology Transfer connector") |
| SEC EDGAR | `data.sec.gov/submissions/...` | 200, no key; requires a User-Agent header with contact info (details in "SEC EDGAR connector") |
| SBIR.gov API | `api.www.sbir.gov/public/api/{awards,company,solicitation}` | 403 on all three; the site says "currently undergoing maintenance" |

Considered for later, not in v1: NSF Awards, DOE OSTI (200, no key; OSTI first,
for space nuclear and power topics), Federal Register (200), Grants.gov (POST
API), and the sources that need a key: SAM.gov, NASA ADS, Congress.gov,
PatentsView.

## SBIR connector

### Which file

SBIR.gov links three bulk CSVs:

| URL | Linked from | Last-Modified | Size | UEI | Abstract |
|---|---|---|---|---|---|
| `https://data.www.sbir.gov/mod_awarddatapublic/award_data.csv` | `/awards` | 2026-09-01 | 395 MB | yes | yes |
| `https://data.www.sbir.gov/awarddatapublic/award_data.csv` | `/data-resources` | 2026-01-01 | 368 MB | **no** | yes |
| `https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv` | both | 2026-09-01 | 91 MB | yes | no |

**Use the `mod_awarddatapublic` file.** It's current, and it's the only one
with both UEI and abstracts. The `/data-resources` link points at a legacy path
that stopped updating in January. The site says the files are refreshed
monthly. The sizes the site quotes (290/65 MB) are out of date.

Behaviour: download on first use into `${CLAUDE_PLUGIN_DATA}`, load into
SQLite, record the file's Last-Modified date, and download again only when that
date changes. Allow a user-supplied file path as an override.

### Load test (measured)

- 219,590 rows and 42 columns, with **0 malformed rows**. The site says
  219,649 awards, so the file is 59 rows short.
- The load takes about 3 s with the Python standard library (`csv` + `sqlite3`),
  so this connector needs no extra dependencies. The database is about 490 MB.
  (Research script, contact columns dropped, no indexes. The plugin's loader
  measures differently; see "Loader" below.)
- NASA: **19,485 awards**, 19,479 of them with an abstract field. But 4,720 of
  those are placeholders such as `N/A` (4,131 before 2000, 10 since 2015), so
  14,759 have a real abstract (found 2026-09-30). Since schema version 2 the
  loader stores those placeholders as NULL (open question 11). Award years run
  1983–2025, and the total is about $5.0 B. By type: SBIR Phase I 12,567,
  SBIR Phase II 5,321, STTR Phase I 1,114, STTR Phase II 483.

### Data quirks the loader must handle (measured)

- **The data dictionary doesn't match the files.** It says Agency is a 2–4
  character code, Phase is "1"/"2" and State is a 2-letter code. The file has
  full agency names, "Phase I"/"Phase II" and full state names. Research Area
  Keywords and Award Link are in the dictionary but in no CSV. Normalise on
  import. The data says "Department of Defense" even though the site now shows
  "Department of War".
- **No unique key.** 2,239 NASA (tracking number, contract) pairs occur more
  than once, and no two rows are identical. Assign our own row id. Confirmed
  2026-09-30: every one of the 2,239 is a Phase I and a Phase II award under
  one number. Across agencies, 119 tracking numbers repeat under more than one
  agency, and one contract number (`NAS 96-1`) is on 348 rows.
- **Award date is blank on 106,552 rows** (8,709 of them NASA). Use Award Year
  for time filters.
- **UEI is missing for 4,961 NASA awards, all from 1983–2014.** From 2015 on,
  every NASA award has a UEI (5,617 of 5,617).
- **No NASA awards dated 2026 yet,** although other agencies have them. Treat
  the current year as possibly incomplete.
- **4 NASA contract numbers since 2015 have trailing whitespace.** Trim them.
- NASA rows have no Branch values, so SBIR awards can't be split by NASA
  center from this file.
- Individual award pages exist (`sbir.gov/awards/{id}`), but the CSV has no
  award id column. Cite awards by tracking number plus contract number instead.

### Loader (built and measured 2026-09-30)

`plugins/agent-techport-sources/servers/sbir/store.py`, standard library only.

- **Refresh:** a HEAD request to SBIR.gov compares Last-Modified with the date
  stored in the database. Same date: use the database as it is. New date:
  download and build a new database file with its own name, then switch a
  small pointer file (`current.txt`) to it, so a reader never sees half a
  database. The old file is deleted once nothing has it open; on Windows,
  where an open file can't be deleted or replaced, it is left for a later
  start to clean up. The CSV is deleted after loading.
  A session that ends mid-download never reaches that step, so each start
  also deletes partial downloads (`*.part`, SBIR's or TechPort's) more than
  a day old. A younger one may be another session's download in progress.
  Added 2026-09-30, after review.
  If there's no Last-Modified header, ETag is compared instead; with neither,
  the database is kept rather than downloading 395 MB on every start. Any
  failed refresh (SBIR.gov unreachable, a dropped connection, a download
  shorter than the Content-Length, or a maintenance page served in place of
  the file, which fails the header check) keeps the existing database and
  reports it as stale, with the reason. No database and a failed first load: a
  clear error.
- **User-supplied file:** `ensure_database(csv_override=...)` loads a local
  file instead, and reloads it when its size or modification time changes.
- **Header check:** the 42 column names must match the expected list exactly.
  If SBIR.gov changes the file layout, the load stops instead of putting values
  in the wrong columns.
- **Schema:** snake_case column names, our own `id`, and two added codes:
  `agency_code` (NASA, DOD, HHS, DOE, NSF, USDA, EPA, DOC, ED, DOT, DHS, NRC,
  DOI; the 13 agencies in the file) and `state_code` (states, DC and
  territories; the file has Guam, Marshall Islands, Puerto Rico and the Virgin
  Islands). Phase is stored as 1 or 2. Award year, solicitation year and
  employee count are integers; the amount is a number.
- **Cleaning:** every value is trimmed, and blank becomes NULL. Name fields
  have repeated spaces collapsed ("Jane  Doe", where the middle initial is
  blank). The formatting placeholders `() -` (phone), `-` (zip) and
  `Not Available` become NULL.
  - **Placeholder words (since schema version 2):** `N/A`, `NA`, `None` and
    `null`, in any case, become NULL when they are the whole value. On the
    2026-09-01 file that is 28,653 values, 28,210 of them abstracts.
    - The contact columns hold a few too: `pi_title` 48, `contact_title` 1,
      `pi_email` 1.
    - A value that merely contains those letters ("Nancy Null") is kept.
  - Values that look like placeholders but might be real are kept as they
    are: phone `9999999999` (1,428 rows) and RI name `Stub` (15,222 rows). 41 rows (DOE, NSF, Commerce) have a blank award amount, which
  is stored as NULL, not 0.
- **Nothing dropped silently:** rows with the wrong field count, agency,
  state or phase values the loader doesn't recognise, and non-blank numbers
  that don't parse (stored as NULL) are listed in a `load_issues` table. The row counts are in a `meta` table with the source
  URL, Last-Modified date, ETag and size.
- **Full-text search:** an FTS5 index over title and abstract.

Measured on the 2026-09-01 file:
- 219,590 rows loaded, 0 malformed rows, 0 unrecognised values.
- First use (download plus load) took 14 s; later starts, one HEAD request,
  took 0.3 s. Load alone took 10.5 s.
- The database with indexes and full-text search is **713 MB**. During a
  refresh the old database, the downloaded CSV (395 MB) and the new database
  exist together, briefly, so peak use is about 1.8 GB.
- The "Contact" columns are mostly agency-side, not company contacts. On NASA
  rows, 9,654 of the 10,418 contact emails are at reisystems.com (the
  contractor that runs SBIR.gov) and 764 at mail.nasa.gov. The PI fields are
  the company's. *Assumed:* the Contact fields name the agency or SBIR.gov
  administrative contact for the award.

Tests: `python3 -m unittest discover -s tests -t .` from the repo root. They
use a small hand-made CSV (`tests/fixtures/sbir_sample.csv`) covering each
quirk. Its companies, people and IDs are made up (`TEST-NASA-0001`,
`EXMPLCRY0001`, `example.com`) and checked against the real file so they don't
collide with real awards. Set `SBIR_REAL_CSV` to a downloaded copy of the file to
also check the full-file numbers above.

### Tools (v1, agreed 2026-09-30)

| Tool | Does | Notes |
|---|---|---|
| `sbir_search_awards` | Filter awards by agency, award year range, program (SBIR/STTR), phase, company, UEI, state, and keywords in title and abstract; sort by relevance, newest, oldest or amount | Time filters use Award Year, not Award Date. Paged; returns the total match count as well as the page |
| `sbir_get_award` | Full record for one award, by agency tracking number plus contract number | That pair isn't unique, so it returns every matching row, each with our own row id. Contact details only with `include_contacts` |
| `sbir_company` | One company by UEI: name, location, flags, and its awards summarised by agency, phase and year | Pre-2015 NASA awards often lack a UEI, so this can undercount older history; say so in the output |
| `sbir_aggregate` | Award counts and dollar totals grouped by year, phase, program, agency, state or company (by UEI, ranked), with the same filters as search | Totals are computed in SQL, never tallied by the model |

Rules across all four:
- Agency, phase and state values are normalised on import. Tools accept and
  return the normalised values.
- Company and PI name by default. Other contact fields only on request (see
  "SBIR contact data").
- Every response states the data file's Last-Modified date, and flags the
  current year as possibly incomplete.
- Keyword search uses SQLite's built-in full-text index (FTS5), so it needs no
  extra dependency. FTS5 is present both in Homebrew's Python 3.14 (SQLite
  3.54.0) and in the standalone Python 3.11 that `uv` installs (SQLite 3.53.1).
  Checked 2026-09-30.

### Server (built and tested 2026-09-30)

`plugins/agent-techport-sources/servers/sbir/`: `server.py` (MCP, uses
`MCPServer` from `mcp` 2.x; it was called `FastMCP` in 1.x) and `queries.py`
(the SQL behind the tools, standard library only, so it's tested without the
SDK). `plugin.json` starts it with:

```
uv run --quiet --frozen --directory ${CLAUDE_PLUGIN_ROOT} python -m servers.sbir
```

`--frozen` installs exactly what `uv.lock` pins and never re-resolves.

- The server answers the client straight away (0.3 s) and prepares the data in
  the background. A tool call made before the data is ready waits for it; on
  first use that was about 15 s.
- All the tools, `sbir_query` included, are marked read-only and closed-world.
- Each tool call opens whichever database the pointer names at that moment, so
  a long-running session keeps working after another session refreshes the
  data and removes the old file.
- Result sizes: search pages are capped at 50 rows, or 10 with abstracts;
  `sbir_get_award` returns at most 20 rows plus the total. Claude Code warns
  above 10,000 tokens of MCP output and saves anything over 25,000 to a file.
  The first figures for these caps (5,800 and 6,800 tokens) were estimates,
  probably characters ÷ 4. Measured in real tokens, a full search page was
  13,351. Since 2026-09-30 every result is compact JSON with lists as tables
  and a size limit, and the same page is 6,933 tokens. See "Result sizes
  (all tools)".
- Keyword search ranks inside the full-text index: 0.04 s for "sensor"
  (22,491 matches), 0.2 s for "the" (189,214).
- A bad filter value (an unknown agency, say) comes back as a tool error that
  lists the accepted values, so the model can correct itself.
- Every result includes a `data` block: source URL, file date, load time, and
  the caveats (current year possibly incomplete; stale data if a refresh
  failed).
- `SBIR_CSV_PATH` in the server's environment loads a local file instead of
  downloading.

Tested over stdio with the SDK's own client, using the exact command in
`plugin.json` and an empty data directory: download, load, all four tools, and
the error paths. The tools reproduce the measured NASA numbers above (phase and
program counts, about $5.0 B, 5,617 awards since 2015).
`tests/sbir/test_server_stdio.py` repeats this with the test file; run it with
the plugin's own Python (it needs the SDK):
`plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .`

Cold install, measured from a fresh clone with an empty uv cache: 1.3 s from
launch to a ready server, including fetching the 30 packages from PyPI (26 MB
cache, 32 MB environment). Claude Code allows 30 s for a server to start
(`MCP_TIMEOUT`). A slow connection or a Python download (below) could exceed
that on first launch; *not yet measured.*

Tested in Claude Code 2.1.286 on 2026-09-30 with `claude -p --plugin-dir
plugins/agent-techport-sources`, starting from no data. The server connected
as `plugin:agent-techport-sources:sbir`; data went to
`~/.claude/plugins/data/agent-techport-sources-inline/` (692 MB). Asked for
NASA Phase II awards in award year 2023 and the three largest, Claude answered
148 awards, $135,747,108 (SBIR 128, STTR 20), and the right top three, all
matching a direct query. The whole run took 32 s, download included.

Gap it showed: search couldn't sort by amount, so Claude paged through all
148 rows (three calls) to find the largest. After adding `sort` (open question
8), the same question took one aggregate call and one sorted search: same
answer, 16 s instead of 32, and under half the cost. Marketplace install, tested the same day from a fresh clone:
`claude plugin marketplace add <clone> --scope local`, then `claude plugin
install agent-techport-sources@agent-techport-sources --scope local`. The
install copied only the plugin directory into the cache (version 0.1.0 with
the commit SHA recorded), the server connected, and the tools answered.
`claude plugin uninstall` removed the plugin's data directory as documented.
One limit on what this proved: a *directory* marketplace runs the plugin in
place, not from the cache copy (the session reported the clone's path, and the
cache copy never got a `.venv`). The run-from-cache path only happens with a
git or GitHub source, so it can't be tested until the repo has a remote.

Gap it showed: asked for the company with the most NASA awards since 2015,
Claude had no way to rank companies. It tried five names one by one, said
plainly that it couldn't confirm the answer, and happened to be right (Creare,
77). It missed the close second, CFD Research (73). See open question 9.

### sbir_query: read-only SQL (built 2026-09-30)

The escape hatch agreed in open question 10: `queries.run_sql` and the
`sbir_query` tool. The model writes one `SELECT`, and SQLite enforces the
rules, on a connection opened for that one query:

- **Read-only, twice over:** the connection is opened read-only, and
  `PRAGMA query_only` is set.
- **No contact fields:** a temporary view named `awards` holds the 36 other
  columns. Temporary objects are found before main ones, so `SELECT *` just
  works. Reading a contact column through `main.awards` is refused anywhere
  in a statement, a `WHERE` clause included.
- **An authorizer** (SQLite's check on everything a statement touches)
  allows only:
  - reading values from `awards` and its keyword index `awards_fts`, plus
    the index's internal tables, which FTS5 reads itself;
  - recursive CTEs, and any function except `fts3_tokenizer` and
    `load_extension`;
  - one pragma, `data_version`, which FTS5 issues internally and which
    changes nothing.

  Everything else is refused: writes, `CREATE` (temporary tables included),
  `ATTACH`, other pragmas, transactions, and `VACUUM INTO`. Without the
  authorizer, `VACUUM INTO` wrote a copy of the database even on the
  read-only connection (seen in the tests).
  - Allow-lists, not deny-lists, because SQLite builds differ. uv's Python
    3.11 (SQLite 3.53.1) and Homebrew's 3.14 (3.53.3) include `dbstat`;
    Apple's system SQLite also has `bytecode` and the unsafe two-argument
    form of `fts3_tokenizer`.
  - One gap, accepted (agreed 2026-09-30): when a statement uses none of a table's columns
    (`count(*)`), SQLite checks the table by name alone, without its
    database, so a CTE and a table look alike. That check is allowed, so a
    query can count the rows of any table in the database but can't read a
    value from it.
- **One statement per call.**
- **Time limit, 10 s:** a progress handler checks the clock every 10,000
  steps. With a 2 s limit, it stopped runaway recursive CTEs and heavy FTS5
  searches at 2.00–2.08 s. Without a limit, one such search was still running
  after 10 minutes. Realistic questions on the full file take under 1 s.
- **Value limit, 1 MB** (`SQLITE_LIMIT_LENGTH`): without it, `group_concat`
  over every abstract built a 302 MB string in 0.2 s, and the process peaked
  at 1.9 GB. The longest real value is 23 KB.
- **Result limit:** up to 200 rows or 12,000 characters of rows, with a note
  when rows were left out. A first row over the limit is an error that
  suggests fewer or shorter columns. Blobs are described, not returned.
- **Output:** compact JSON text rather than a dict. The SDK sends dicts as JSON
  indented by 2, which for query rows is 1.3–3.3 times larger. Returning the
  text makes the size limit apply to exactly what the model reads.
- **Errors say what to do instead:** a contact column points to
  `sbir_get_award` with `include_contacts`; a timeout suggests filtering
  early or `sbir_aggregate`.
- **The documented schema is the tool description:** the 36 columns with
  their meanings and quirks, the keyword-search join, and the limits. A test
  fails if a readable column is missing from it. Loading the tool's
  definition costs about 1,230 tokens.

Token costs, measured in Claude Code 2.1.286:

| Result | Characters | Tokens |
|---|---|---|
| Every column, 16 rows (size limit) | 12,829 | 7,528 |
| 14 columns, 60 rows (size limit) | 12,572 | 7,862 |
| 4 columns, 200 rows (row limit) | 9,325 | 5,866 |

The first build allowed 24,000 characters, *assumed* safe at 4 characters per
token. Query results measured 1.6–1.7 characters per token: the widest was
25,066 characters and 14,755 tokens. So the limit was halved.

**How tokens are measured.** Run `claude -p` with `--output-format
stream-json --verbose` and read each model call's usage. A tool result's
tokens are the growth in input tokens (input + cache creation + cache read)
from the call before it to the call after, less the earlier call's output
tokens. That is the real cost in context. Add `--disallowedTools Bash` to
these runs: `--allowedTools` alone didn't stop a test session on the test
machine from running commands, and one wrote a script when the tools fell
short.

Claude Code's own limit uses a different count, an estimate. The
25,066-character result (14,755 real tokens) was saved to a file with
`MAX_MCP_OUTPUT_TOKENS=10000` but passed with 14,000. So Claude Code counted
it at 10,000–14,000, or 1.8–2.5 characters per token: neither real tokens nor
characters ÷ 4.

It isn't a fixed character ratio either. Two prose-heavy TechPort records of
25,619 and 23,691 characters came through at that same 10,000 limit, so
Claude Code counted them at 2.4 or more characters per token. *Assumed:* the
10,000-token warning uses the same count as that limit. `claude -p` doesn't
show the warning, so it wasn't observed.

**Tested in Claude Code 2.1.286.** Asked "Of the companies that won a NASA
SBIR or STTR Phase I award between 2015 and 2022, what share also won a NASA
Phase II award, in any year?", Claude made four `sbir_query` calls and showed
each statement. It answered 701 of 1,061 (66.1%) and cited the file's date.
- It checked the Phase II awards without a UEI by company name; there were
  no further matches.
- It gave the stricter reading too: 618 of 1,061 (58.2%) with the Phase II in
  or after the year of the company's first Phase I.
- Every number matched a direct query.
- It went straight to `sbir_query`: the four tools can't intersect two sets
  of companies.

The run took 46 s.

Tests: `tests/sbir/test_sql.py` covers each rule on the test file, plus four
real-file checks: 5,617 NASA awards since 2015, the top two companies at 77
and 73, 22,491 keyword matches for "sensor", and the transition question.
Each authorizer rule was broken on purpose once, and a test failed each time.
The stdio test calls the tool through the real server command.

## TechPort connector (built 2026-09-30)

### What the API does today

- **Keyword search works now:** `GET /api/projects/search?query=...` returns
  every match in one ranked response (1,465 for "cryogenic"). `limit` works.
  **`offset` and all filter parameters are ignored**, so filtering, sorting and
  paging have to happen in our code.
- **An empty search returns the whole portfolio:** 21,044 projects, 115 MB, 8 s.
  Parsing it in one go peaked at 900 MB of memory, so the loader must parse it
  incrementally.
- **`/api/projects/search/allData` returns 404**, so an empty search is the
  only way found to fetch every project in one call.
- **POST search needs a nonce** from TechPort's web app ("there may be
  hijacking going on"). The plugin does not use it.
- **Change detection:** `/api/projects?updatedSince=YYYY-MM-DD` lists changed
  project ids with dates (92 in the last week). With 2000-01-01 it lists all
  21,046 ids. No Last-Modified or ETag headers.
- **Per project:** `/api/projects/{id}` (about 15 KB, 0.2 s) uses different
  field names from the search results (for example `destinationType` vs
  `destinationTypes`, `lastUpdated` vs `modifiedDate`). The code normalises
  both (`model.py`).
- **Also working:** `/api/strategy` (capability areas and
  shortfalls), `/api/strategy/shortfalls/search`, `/api/file/{id}` (library
  documents, e.g. a 900 KB PDF), `/api/programs`, `/api/organizations`,
  `/api/opportunities`, `/api/enums`, `/api/taxonomies`, and the TREX
  classifier at `POST /trex/predict` (the spec's `/api/trex/predict` needs the
  nonce).
- **Link to SBIR:** `/api/organizations?organizationUei=` finds a TechPort
  organisation by UEI (Creare → organisation 4561). Projects carry the lead
  organisation's DUNS, and the SBIR file has a DUNS column, which is useful
  for awards before 2015.
- **No funding data** in the public API (`detailedFunding: false`). Dollar
  questions go to SBIR, and to USAspending once it exists.
- Contact names and emails are on 18,764 projects, so the SBIR contacts rule
  applies: names by default, emails on request.
- SBIR/STTR phase codes include `2E`, `2X` and `2S`. *Assumed* meanings, not
  confirmed.

### Decided (2026-09-30): live API, plus a daily copy where the API can't answer

- **Live on every call:** keyword search (all matches fetched, then filtered,
  sorted and paged in our code), single projects, programs, organisations,
  capability areas and shortfalls, opportunities, TREX classification, and
  what's new (`updatedSince`).
- **Daily copy** (the full 115 MB pull, at most once a day, when a question
  first needs it): aggregates, listings with no keyword (such as "all FO
  projects"), contact search, and batch lookups. Every answer from the copy
  states its date; the copy is at most a day old.

### Tools (agreed 2026-09-30)

| Tool | Data |
|---|---|
| `techport_find_projects` | live with keywords, daily copy without |
| `techport_get_project` | live; batch from the copy |
| `techport_aggregate` | daily copy; group, sort, top N, including organisation ranking |
| `techport_whats_new` | live `updatedSince` |
| `techport_programs` | live |
| `techport_organizations` | live; by name, acronym, UEI or CAGE |
| `techport_capabilities` | live `/api/strategy` |
| `techport_find_contacts` | daily copy; emails only on request |
| `techport_opportunities` | live |
| `techport_classify_technology` | live `/trex/predict` |
| `techport_get_document` | live `/api/file/{id}`, returned as an embedded PDF (option B below) |

### Server (built and tested 2026-09-30)

`plugins/agent-techport-sources/servers/techport/`, with shared code in
`servers/common/`:
- `http.py`: the only HTTP code in the plugin (SBIR's loader uses it too;
  nothing else under `servers/` imports `urllib`). Standard library; retries on
  429/5xx/timeouts; clear errors; and an allowed-hosts list per server. A
  request to any other host is refused before it is sent, and so is a redirect
  to one (tested).
- `jsonstream.py` parses the 115 MB dump one project at a time: 0.3 s, 42 MB
  peak memory, where `json.load` needed 900 MB.
- `dbfiles.py`: the "new file plus pointer" logic, now shared with SBIR.

The TechPort server itself:
- `model.py` normalises both record shapes.
- `copy.py` builds the daily copy: 21,044 projects, 4 s to load, 224 MB.
- `queries.py` holds the SQL. Live search results are loaded into an in-memory
  database with the same tables, so a filter means the same thing on either
  path.
- `server.py` has the 11 tools. Launched like SBIR (`uv run ... -m
  servers.techport`).

How requests are answered:
- **Keyword searches** go to TechPort live, up to 2,000 matches; our code then
  filters, sorts and pages them. Above 2,000 matches, the copy's own keyword
  index answers, with a note. That index matches fewer fields than TechPort's
  (1,190 vs 1,465 for "cryogenic").
- **No keyword, counts, contacts, batch:** the copy. It is built the first
  time one of these is needed (12 s on first use), then refreshed in the
  background when a day old; the old copy serves meanwhile.
- **Single projects:** live, merged with the search-shape record for the
  fields only that shape has (phase, set-aside categories, closeout
  documents).

**Measured limit that changed the document plan:** Claude Code closes the
connection when one tool result is over about 16 MiB. With a 0.9 MB PDF
repeated, 13 copies (15.6 MB encoded) worked and 14 (16.8 MB) failed
("Connection closed"). Base64 adds a third, so **one result carries at most
about 11 MB of files**. The plan that follows, agreed 2026-09-30:
- The 20 MB per-file cap stays as agreed.
- `techport_get_project` attaches files most useful first (briefing chart,
  final/closeout reports, other documents, images) up to 11 MB. It lists every
  file once, in that order, with its status: attached, or why not.
  `techport_get_document` takes a list of file ids and fetches them the same
  way, 11 MB per call.
- A single file of 11–20 MB can't be attached. It is saved in the plugin's own
  data folder (`files/`, deleted after 30 days) and its path returned; Claude
  Code asks the user before reading it in default mode.
- Over 20 MB: link only.

Safeguards added after review:
- **A short dump can't replace a good copy.** A load more than 10% short of
  the expected count is rejected and the old copy kept. Expected means the
  current copy's count, or, on first build, TechPort's id list (21,046 ids).
  Reason: TechPort's search has been reported to return the same 50 results
  for any query in the past (*reported, not observed here*).
- **A failed refresh waits an hour** before the next attempt, instead of
  starting a 115 MB download on every call during an outage. The data notice
  says the copy may be old.
- **Programs are grouped by programId, not acronym.** PSRP and SBP each belong
  to two programs, and three programs have no acronym.
- **Result sizes.** The first figures here were estimates by the same
  unrecorded method as SBIR's. Real measurements are under "Result sizes
  (all tools)". The row caps stay:
  - Batch lookups return summary rows, up to 50 per call; full records went
    over by far (100 came to 168,000 tokens, estimated).
  - `find` pages up to 50 rows, or 25 with descriptions.
  - `whats_new` returns up to 100 rows.
  - The opportunities list keeps the essentials, with descriptions cut to
    200 characters.

Tested in Claude Code 2.1.286:
- **Before the limit was known:** project 13075 (36 files, 61 MB) broke the
  connection. Claude then fetched the project without documents and one file
  on its own, and said which it had opened.
- **After:** 13 files attached, 23 listed, no errors, 21 s. Claude read the
  summary PDF, gave a figure from it that isn't in the description (64% peak
  efficiency at 12.5 kW), and listed what it had and hadn't opened.

Tests:
- `tests/techport/` and `tests/test_http.py` use made-up records in both
  shapes (no real contact data in the repo).
- The stdio test starts the real launch command, with the copy built from the
  test file (`TECHPORT_JSON_PATH`), so it needs no network.
- All 11 tools were also run once against the live API.

### Reading PDFs (library items, capability gap documents): option B, agreed

Rendering pages to images with PyMuPDF is ruled out: PyMuPDF is **AGPL-3.0**
(or a commercial licence), which is a problem for a NASA release, and it is a
large compiled dependency. Options:

- **A. Save the file locally and return its path.** Claude Code's own Read tool
  reads PDFs, text and visuals, page by page. No dependency. *Assumed until
  tested:* works in Claude Code; may prompt for permission to read outside the
  project; won't work in clients that can't read local files.
- **B. Return the PDF inside the tool result** as an embedded MCP resource
  (`application/pdf`). No dependency. *Unknown:* whether Claude Code passes it
  to the model.
- **C. Render pages to images with `pypdfium2`** (BSD/Apache, PDFium). Works
  in any client that shows images; one compiled dependency.
- **D. Text only with `pypdf`** (BSD, pure Python). Small, but misses charts
  and slides.

**Tested in Claude Code 2.1.286, 2026-09-30.** A throwaway server returned
TechPort file 359025, a one-page SBIR briefing chart. The check question asked
for the colours of three strips in its diagram (red, blue, yellow), which
appear only in the image, not in the text.

| Option | `auto` permission mode | `default` permission mode | Saw the image |
|---|---|---|---|
| A. Local path, then Read | worked | **Read denied** (plugin data folder is outside the project); interactive users would get a prompt per document | yes |
| B. Embedded PDF | worked | **worked, no prompt** | yes |

How B works in Claude Code: the embedded PDF is not put into the model's
context directly. Claude Code saves it to the session's `tool-results` folder
and tells the model the path, and the model opens it with Read, which renders
pages as images. Claude Code allows that read without a prompt.

**Decided 2026-09-30: B.** No dependency, no permission prompt, the model sees
charts and slides, and clients that do accept embedded PDFs get the file
directly. Build notes:
- **Agreed 2026-09-30:** `techport_get_project` for a single project returns
  all of its library files (PDFs and images) by default; `include_documents:
  false` skips them. Batch lookups never include documents. Links and stories
  return their URLs only: they point to hosts other than TechPort, and
  fetching them would break the host boundary.
- **Cap: 20 MB per file** (agreed). A file over the cap, or one that fails to
  download, is listed with its TechPort URL, never dropped silently.
- Measured on the largest library (project 13075, "Solar Electric
  Propulsion"): 19 PDFs and 14 images, 61 MB, 15 s to fetch; largest file
  3.9 MB, median 1.6 MB. Across TechPort, 10,285 of 21,044 projects have
  library items, median 2 per project, max 43. Items by type: 12,479 images,
  5,977 documents, 4,485 links, 1,882 stories, 165 data sets, 9 videos.
- **Reading has a limit that fetching doesn't.** A page image costs about
  1,500 tokens: measured on an NTRS report in Claude Code 2.1.286, 1,370–1,600
  a page over 19 page reads (17,909 for pages 1–12). So a large library can
  exceed one conversation. The server instructions tell the model to start
  with the briefing chart and final report, read further in order, and say
  which documents it has and hasn't read.
- Each returned file is saved by Claude Code in the session's `tool-results`
  folder, next to the page images Claude Code already makes whenever it reads
  a PDF. Claude Code deletes old sessions after `cleanupPeriodDays`. *Assumed,
  not verified:* that deletion includes `tool-results`. The plugin never
  deletes Claude Code's files. The README must say where the files go and
  which setting controls how long they're kept.
- *Relies on observed Claude Code behaviour* (saving binary tool results to a
  file). Recheck it when Claude Code updates, and say in the README that
  documents need a client that handles embedded PDFs.

## Result sizes (all tools, measured 2026-09-30)

Open question 12. Every tool now returns compact JSON through one output step,
`servers/common/output.py`:

- **Lists of records become tables:** column names once, then one row of
  values per record. That only happens when every record has the same fields:
  - for the tools' own results, anything else is refused;
  - lists inside a TechPort project record come from upstream, so an odd one
    is left as records.
- **Every list stops at 12,000 characters of rows,** at least one record. A
  note says how to get the rest: the next offset, the ids left out, or
  narrowing the search. `whats_new`, `find_contacts` and `opportunities`
  gained an offset for this.
- **Full SBIR award records stay records:** many fields, usually one or two
  rows.
- **`techport_get_project` lists each file once,** with its status.

**Why tables.** For the same 50-row SBIR page:
- indented JSON, which the SDK sends for a dict: 13,351 tokens;
- compact JSON: 10,744;
- compact with the list as a table: 6,909.

Claude read 16- and 14-column tables without error in 13 lookups, nested lists
included.

Worst cases in real tokens (method under "sbir_query"), before and after:

| Result | Before | After | Returned after |
|---|---|---|---|
| `sbir_search_awards`, 50 rows | 13,351 | 6,933 | all 50 |
| `sbir_get_award`, 20 full records with contacts | 12,504 | 5,810 | 11, the rest named by id |
| `sbir_get_award`, 20 full records | 10,218 | 5,471 | 13, the rest named by id |
| `sbir_aggregate`, 100 companies | 8,991 | 4,539 | all |
| `sbir_search_awards`, 10 with abstracts | 8,466 | 4,058 | 6, offset for the rest |
| `sbir_company`, the largest | 2,929 | 1,340 | – |
| `sbir_query`, the widest | – | 7,862 | 60 |
| `techport_get_project`, the largest library (13075) | 15,438 | 8,425 | 13 of 36 files attached |
| `techport_find_contacts`, 50 people | 14,591 | 4,944 | 25, offset |
| `techport_get_project`, batch of 50 | 13,345 | 6,346 | 45, the rest named by id |
| `techport_find_projects`, 50 rows | 13,201 | 6,146 | 45, offset |
| `techport_organizations`, 100 | 12,456 | 4,450 | all |
| `techport_whats_new`, 100 | 12,276 | 5,258 | 67, offset |
| `techport_opportunities` | 11,480 | 5,339 | 27 of 36, offset |
| `techport_find_projects`, 25 with descriptions | 11,479 | 4,760 | 14, offset |
| `techport_programs` | 7,254 | 3,069 | all 98 |
| `techport_aggregate`, 100 groups | 3,439 | 1,731 | all |
| `techport_capabilities` | 1,843 | 1,224 | all 19 |

- **The largest results are single TechPort projects with long text of their
  own.** Each is one record, so no list limit applies:

  | Project | Description + benefits | Files attached | Tokens |
  |---|---|---|---|
  | 93175 | 20,022 characters | 2 | 9,871 |
  | 116431 | 12,219 | 7, plus 2 saved locally | 9,702 |
  | 12080, the longest in TechPort | 23,281 | 1 | 9,219 |
  | 13075, the biggest library | 1,925 | 13 | 8,425 |

  - All four are under the 10,000-token warning and over the 8,000 target.
  - **Claude Code's own count agrees.** With `MAX_MCP_OUTPUT_TOKENS=10000`,
    12080 and 93175 both came through inline, so Claude Code counts them at
    10,000 or less.
  - Across TechPort, description plus benefits has a median of 2,110
    characters. Only 25 projects pass 10,000.
  - **Agreed 2026-09-30:** keep the project's own text whole rather than
    cut it.
  - Claude Code adds about 345 characters per attached file. For 13075's 13
    files that is about 2,000 tokens.
- **Not measured:** `techport_get_document` and
  `techport_classify_technology`. Their text is short; `get_document`'s files
  are attached, not text.
- **Projects that list hundreds of files** (open question 13, found after the
  first table). The file list sits inside one record, so the list limit
  didn't reach it. Almost none of these files can be attached: data files of
  hundreds of MB to GB, videos, CSVs. Some were downloaded just to learn their
  type. They are now counted by reason, not listed:

  | Project | Files | Characters before | Characters after | Counted instead |
  |---|---|---|---|---|
  | 116277 | 639 | 120,423 | 2,901 (1,264 tokens) | 639 `.cine` |
  | 116262 | 388 | 92,358 | 5,917 | 370 `.tar`, 16 `.zip`, 2 `.xlsx` |
  | 157798 | 394 | 61,491 | 3,042 | 390 `.mp4`, 3 `.zip`, 1 `.csv` |
  | 94114 | 219 | 41,387 | 6,995 | 78 `.zip`, 75 `.raw`, 62 `.csv` |

- **Many small files in one result** (open question 14). The 11 MB budget
  doesn't limit how many files are attached, but Claude Code's per-file notes
  add up. One result now attaches at most 20 files. That is about 7,000
  characters of notes (*estimated* from the measured note size), where 50 would
  have been about 17,000.
- **Tool definitions cost tokens too** when Claude Code's tool search loads
  them: about 1,230 for `sbir_query` alone, and 5,838 for nine TechPort tools.
- **Claude read every "Stopped after" note** and quoted the offset or the
  ids to continue.

## USAspending connector (probed 2026-09-30)

`api.usaspending.gov`, API v2: no key, JSON. Data last updated 2026-09-30;
it updates daily (`/api/v2/awards/last_updated/`). About 40 requests over
three rounds, read-only.

**What works:**

| Need | Endpoint | Measured |
|---|---|---|
| Find awards | `POST /api/v2/search/spending_by_award/` | 2–9 s; up to 100 per page (101 is refused, 422); keyword search; contract or grant numbers (`award_ids`, 50 at once found all 50, 15 s) |
| Count them | `POST /api/v2/search/spending_by_award_count/` | 4–9 s; NASA FY2025: 9,885 contracts, 8,834 grants, 946 IDVs |
| One award | `GET /api/v2/awards/<generated_internal_id>/` | 0.2–0.6 s; 6 KB (contract), 13 KB (grant) |
| Its transactions | `POST /api/v2/transactions/` | 0.3 s for 100; accepts up to 5,000 per page (1,651 rows, 637 KB for the Space Station contract) |
| Its subawards | `POST /api/v2/subawards/` | 0.2 s; paged |
| A company by UEI | `POST /api/v2/recipient/` with the UEI as keyword, then `GET /api/v2/recipient/<id>/` | 0.4 s, then 6 s; parent, child and recipient-only records |
| Totals by group | `POST /api/v2/search/spending_by_category/<group>/` | 3–17 s; recipient, awarding or funding agency, NAICS, PSC, CFDA, state, country |
| Totals by year | `POST /api/v2/search/spending_over_time/` | 5.5 s |

**What the tools must handle:**
- **The time filter selects awards with transactions in the period, not awards
  that started then.** FY2025 brings back the 1993 Space Station contract. In
  search results, "Award Amount" is the award's lifetime total. Money spent in
  a period comes from the totals endpoints, which sum obligations in the
  period.
- **Searches start no earlier than 2007-10-01.** The API says so in its
  messages.
- **`hasNext` is wrong on deep pages.** Pages 100, 101 and 500 each returned
  100 distinct, ordered results with `hasNext` false. Use the count endpoint.
- **The sort field must be among the requested fields,** or the API returns 400.
- **An unknown field name isn't refused;** it comes back null. Validate field
  names in code.
- **Keyword search is looser than a phrase.** "SBIR PHASE II" also matched 42
  Phase III descriptions in its top 100. Re-check phrases in code.
- **Size:** 100 results with 14 fields came to 144,813 bytes. Descriptions
  have a median of 234 characters and a maximum of 3,907, so rows need them
  left out or shortened by default.
- **Contracts and grants can't share a request:** one type group per call (422).
- **Executive pay** (`executive_details`: officers' names and amounts) is in
  award records. It was empty in the records seen; it is personal data, like
  contacts.
- Grouped totals are slow (up to 17 s). No throttling was seen in a burst of 8.
- Certificates: see "HTTPS certificates" (this host needs the OS trust store
  on macOS).

**Findings:**
- **SBIR Phase III follow-ons.** NASA contract descriptions say "SBIR PHASE
  III" on 359 contracts and 6 IDVs (FY2008 on), and "STTR PHASE III" on 20.
  For example: ICON Technology $33.4M, Katalyst $29.8M, Starfish Space $15.0M,
  Astrobotic $14.0M, Creare $13.4M. Phase III awards aren't in the SBIR file,
  so this is the plugin's view of what came of SBIR work. It only finds
  contracts whose description says so.
- **USAspending doesn't flag SBIR.** Award records have no SBIR or STTR field
  (`80NSSC24PB452` shows only a small-business set-aside), so the SBIR link
  stays the contract number, as in "Linking SBIR to USAspending". That link
  checked out again: one award, the same internal id, the same UEI.
- **One company across agencies:** Creare since FY2008 shows DoD $463.6M,
  NASA $118.1M, HHS $31.1M, DOE $18.0M.
- **NASA has no sub-agencies in USAspending.** Grouping by funding agency
  shows other agencies paying through NASA contracts; NOAA, for example, put
  $498M through them in FY2025.
- **NASA FY2025 contracts by place of performance:** California $4.28B,
  Maryland $1.89B, Alabama $1.87B.
- **NASA FY2025 grants by program:** Science (43.001) $1.17B, Space Technology
  (43.012) $58.1M.

### Tools (agreed 2026-09-30)

| Tool | Does | Notes |
|---|---|---|
| `usaspending_search_awards` | Find awards by agency, award type (contracts, grants or IDVs), company name or UEI, keywords, contract or grant numbers (up to 100), fiscal years, NAICS, PSC or CFDA code, and state; sorted by amount or date | Descriptions left out by default; an option adds the first 300 characters. The total comes from the count endpoint. A Phase III option finds contracts described as SBIR or STTR Phase III, re-checked in code. `signed_only` (added after the test): only awards signed in the fiscal years asked |
| `usaspending_get_award` | One award in full: amounts, dates, the company and its parent, awarding and funding office, codes, place of performance | Optionally its latest transactions and largest subawards, capped. Executive pay left out |
| `usaspending_recipient` | A company by UEI: profile (names, parent, location, business types) and its federal awards by agency | The "company across agencies" view |
| `usaspending_aggregate` | Money obligated in a period, grouped by company, awarding or funding agency, NAICS, PSC, CFDA program, state, country or fiscal year, with search's filters | Obligations in the period, not lifetime amounts; says so in every answer. Added after the test: whole-award totals for `sbir_phase_iii` (contracts only; NASA also has 6 IDVs described as Phase III) or `award_ids`, by company, agency or fiscal year signed |

Not in v1: bulk downloads, federal budget accounts, loans and direct
payments (NASA has none). Written from this probe alone.

### Built and tested in Claude Code (2026-09-30)

`servers/usaspending/`:
- `api.py`: the calls, allowed only `api.usaspending.gov`.
- `model.py`: request bodies and record shapes, standard library only.
- `server.py`: the four tools, with output through `servers/common/output.py`.

All 14 tool calls worked against the real API, with no result cut by the size
limit. Real tokens of the largest:

| Result | Characters | Tokens |
|---|---|---|
| Search, 50 contracts | 10,688 | 6,285 |
| Search, 25 with descriptions | 10,263 | 6,465 |
| Phase III search, 50 | 10,204 | 6,026 |
| Top 100 recipients | 6,343 | 4,437 |

**The test question:** "Which companies have won NASA SBIR or STTR Phase III
follow-on contracts since fiscal year 2015, and which received the most?"
Claude answered 145 companies, led by ICON Technology ($33.4M), Katalyst
($30.2M) and Creare ($17.6M). It gave careful caveats: contracts signed
before FY2015, task orders inflating one company's count, one company under
three UEIs. But it had to leave the tools to get there:
- **To total the 264 contracts by company,** it wrote a throwaway script that
  called the plugin's code. The test environment allowed Bash. The tools
  couldn't total a set of awards.
- **It found that keyword totals are wrong for whole awards.** The totals
  endpoint applies keywords to each *transaction's* description, so later
  modifications described differently drop out. Verified: Starfish Space's
  Phase III contract has $14,999,999 obligated, but only its first $5.0M
  transaction says "SBIR PHASE III". The keyword total for the company was
  $5,324,631; without the keyword, $17,210,348.

Verified afterwards:
- `time_period` takes `date_type: "date_signed"`. Phase III contracts signed
  since FY2015 number 242, Claude's count after removing the 22 that started
  earlier; the default (`action_date`) gives 264. The totals endpoint takes the
  option too, but its `date_signed` and `new_awards_only` variants gave
  different numbers whose exact meaning isn't clear, so they aren't used.
- On 12 Phase III contracts, the search field "Base Obligation Date" equals
  the award's `date_signed`, and "Award Amount" equals its `total_obligation`.

**Decided 2026-09-30:**
- **Whole-award totals.** `usaspending_aggregate` takes `sbir_phase_iii` and
  `award_ids`. It fetches those awards (up to 1,000) and adds up each award's
  total obligation by company (UEI), awarding agency or fiscal year signed,
  labelled as lifetime totals of those awards.
- **A warning on keyword totals.** An aggregate that uses keywords warns that
  only matching transactions are counted.
- **`signed_only`** in search and in the whole-award totals: only awards
  signed in the fiscal years asked. The default stays "any transaction in the
  years".

**Built and retested (2026-09-30).** The same question needed 2 tool calls:
whole-award totals, then a search for the contracts behind them. It took
32 s and no Bash or script; before, it took about 14 calls, 2 minutes and a
script.
- The numbers match the earlier script's exactly: 242 contracts signed
  FY2015–FY2026, 145 companies, $273.2M.
- ICON Technology $33.4M (1 contract), Katalyst $30.2M (3), Creare $17.6M (5).
- Claude stated the limits: a minimum count (found by description), signed
  in the years, lifetime obligations, and FY2026 not final.

## Linking SBIR to USAspending (measured)

### Company across agencies: UEI

UEI coverage for 2015 onwards: DoD 99.9%, DOE 98%, HHS 95%, NSF 91%, NASA 100%.
Each UEI maps to exactly one company name in the file.

Of the **1,364 companies with NASA SBIR awards since 2015, 990 (73%) also won
SBIR awards from another agency**: DoD 840, DOE 334, NSF 164, HHS 110,
Commerce 95, USDA 59.

### Award to USAspending: contract/grant number, normalised per agency

Search USAspending's `/api/v2/search/spending_by_award/` with `award_ids`,
covering both contract types (A–D) and grant types (02–05).

| Agency | Sample (2015–2025) | Matched | Normalisation | UEI agrees |
|---|---|---|---|---|
| NASA | 110 (10 per year) | 110 | trim whitespace; the number is USAspending's Award ID as is | 107 |
| DoD | 30 | 29 | remove dashes | 29 |
| DOE | 30 | 30 | remove dashes | 27 |
| NSF | 30 | 30 | as is | 26 |
| HHS/NIH | 30 | 29 | core grant id (`2R44AG050454-02` → `R44AG050454`) | 25 |

No lookup returned more than one award. For NASA the dollar amounts agree
(median USAspending/SBIR ratio 1.00, 10th–90th percentile 1.00–1.01).

Known issues:
- **NIH regex bug:** the core-id pattern in `xmatch.py` expects a
  letter-digit-digit activity code (`R44`) and misses codes like `SB1`
  (`1SB1AI183963-01`). Widen it to `[A-Z0-9]{3}`.
- **NIH granularity:** yearly segments (`-01`, `-02`) roll up into one
  USAspending award, so a single SBIR row maps to part of a multi-year award
  and the amounts won't match row for row. Report it as "part of award X".
- **DoD miss `D17PC00294`:** *assumed:* an Interior contract office buying on
  DoD's behalf, filed under a different agency or number format.
- **UEI disagreements:** *assumed:* acquisitions or re-registrations.
- Not tested yet: USDA, Commerce, DHS, DOT, EPA, Education (about 3,000 awards
  since 2015). Confirm each agency's rule before a study depends on it.

Rules for the connector: link companies by **UEI**; link awards by the
**normalised contract/grant number**, with UEI as a cross-check; store
USAspending's `generated_internal_id` (for example
`CONT_AWD_80NSSC24PB452_8000_-NONE-_-NONE-`) for follow-up calls; **always
report the match rate and list unmatched rows**, never drop them silently.

The scripts behind these numbers are in `research/2026-09-30-sbir-usaspending/`.

## NTRS connector (probed 2026-09-30)

`ntrs.nasa.gov`, NTRS API 1.0: no key, JSON. The API description is served
only inside the Swagger UI page (`/api/openapi/swagger-ui-init.js`). About
350 requests over eight rounds, read-only; 159 of them were HEAD requests
for file sizes.

**What's in it:** 647,795 public records: 607,920 NASA submissions, 39,221
journal articles by NASA-funded authors (from CHORUS, the publishers'
clearinghouse) and 654 from JPL. The API returns public records only: the
distribution counts show a single value, PUBLIC.

**What works:**

| Need | Endpoint | Measured |
|---|---|---|
| Find records | `POST /api/citations/search` (GET takes the same fields) | 78–384 ms on the server; any page size (500 records = 2.3 MB); a record is 4.6 KB (median of 100; largest 8.9 KB) |
| Counts by group | the same response | Every search also returns counts by center, report type, subject category, year published, author, organization, funding number, keyword and report number |
| One record | `GET /api/citations/{id}` | 6 KB for the one measured; the same fields as a search result |
| Its files | `GET /api/citations/{id}/downloads`, then each file's `links` | `original`; `pdf`, which some Word and PowerPoint files also have (2 of the 7 seen); and `fulltext`, the text NTRS extracted. HEAD gives the size |
| Author name forms | `GET /api/citations/autocomplete?field=author&q=…` | Up to 20 forms: "Hartwig" gives "J Hartwig", "J. W. Hartwig", "Hartwig, Jason W." and 17 more |

**Search fields (measured):**
- `q`: quoted phrases work ("cryogenic fluid management": 448 records
  unquoted, 334 quoted). It searches the record, author names included, but
  **not the files' text**: two phrases from the middle of one report's text
  (20180002393) found nothing.
- **`q` takes a small query syntax** (measured by the counts adding up):
  words are all required; `|` means or (`cryogenic | propellant` 15,344 =
  7,464 + 9,194 − 1,314 with both); `-` excludes (`cryogenic -propellant`
  6,150 = 7,464 − 1,314); `*` ends a prefix (`cryogen*` 8,055); parentheses
  group (`cryogenic ("Jason Hartwig" | "J. W. Hartwig")` 86 = 58 + 28). The
  words AND, OR and NOT are searched as words, and `field:` prefixes don't
  work. This is how to search several forms of a name at once.
- `title` and `abstract` ignore quotes and match far more loosely (the phrase
  above in `title`: 10,808 either way). Not for the tools.
- `center`, `stiType` (22 report types), `subjectCategory`, `author`,
  `organization`, `keyword`, `reportNumber` and `fundingNumber` are **exact
  and case-sensitive**: `GRC` 18,675, `grc` 0; `Bolshinskiy, L. G.` 8, in
  lower case 0.
- **A list of values means all of them, not any of them.** Two centers, two
  report types or two funding numbers each return 0. One value per field.
- `published` takes a date range (`gte`, `lt`); `sort` takes `published`,
  `created` or `modified`; `page` takes `size` and `from`.

**What the tools must handle:**
- **Names come in many forms.** Of the 20 author forms for "Hartwig", nine
  could be one person, from "J Hartwig" to "Hartwig, Jason W." (*assumed*).
  Each form is a separate exact value; the six counted had 1–7 records each.
  "NASA Glenn Research Center" (12,034) and "Glenn Research Center" (4,555)
  are separate organizations, and so are subjects that differ only in case
  ("Propellants And Fuels" 322 and "Propellants and Fuels" 95 in the test
  search below). An exact filter on one form misses the others. `q` with the
  name finds more (189 for "Hartwig"), other people of that name included.
  The counts by author, organization or subject show the forms in use.
- **Paging stops at 10,000.** `from` 9,990 with 10 per page works; `from`
  10,000 is a 400 error. Past that, the search has to be narrowed.
- **Not every record has a publication date.** In the test search
  ("cryogenic propellant", 1,314 records), 234 have none; the 15 among the
  first 100 are conference papers, presentations and posters. A `published`
  range leaves them out, and sorting by `published`, newest first, puts them
  last. Across the whole collection, 57,152 records (9%) have none.
  `created` is when a record entered NTRS: 2013 for the 1959–1961 reports
  checked, 2026-09-28 for the CHORUS articles checked, so sorting by it puts
  those first.
- Publication dates can be in the future (2026-10-07) or wrong (the year
  202 appears in the counts).
- **Older records have no NASA center.** The largest "center" in the test
  search is CDMS, "Legacy CDMS" (420 of 1,314). The 179 among the first 500
  date from the 1960s to the 1990s. A center filter misses them: of the 38
  records there with a Lewis Research Center author (Glenn's former name),
  35 are under CDMS and 2 under GRC.
- **Counts by year come back ordered by count, not by year,** so they need
  sorting. All years come back (117 for the whole collection). Other groups
  give the top 20 and a count of the rest; centers come back complete (15).
- **CHORUS ids are 14-digit strings** (`33223363495292`), though the API
  description says number. These records have no center and no files: the
  article is at the publisher, and the record gives its DOI.
- **Files.** Of the first 100 records in the test search, 83 had one file
  each and 17 had none. 77 had a `pdf` link (75 PDFs plus two converted Word
  or PowerPoint files). 5 Word or PowerPoint files had no PDF, and 1 file was
  a video. PDFs: median 1.4 MB; 7 of the 77 are over 20 MB (the largest
  84 MB). So under the TechPort rules (embedded PDFs, 20 MB cap), 70 of the
  83 records with files could attach one.
- **`fulltext`** exists for 82 of the 83 (all but the video): median 29 KB,
  90th percentile 130 KB, largest 2.2 MB; 18 are under 12,000 bytes. Some
  come as XHTML from the text extractor rather than plain text, so markup
  has to be stripped. File names vary (`20180002393.txt`, `….docx.txt`):
  use the `links` the API gives; don't build them.
- **Rate limit:** the headers say 500 requests per window. *Assumed:* each
  server counts separately. `Remaining` went up from 492 to 493 between two
  requests, and the reset time moved by 470 s within 4 s. The window length
  wasn't measured (resets were 110–776 s ahead). The plugin's use is far
  below it.
- `highlight` marks the matching words of the title and abstract (in each
  record's `_meta`). Not needed: the tools return the abstract.
- Not needed either: the export endpoints (JSON, XML, CSV) and the list of
  redistributed records. PubSpace (`/api/pubspace/search`, the peer-reviewed
  subset: 176 records for "cryogenic") needs no separate tool: the first five
  checked are ordinary NTRS records (reprints and accepted manuscripts).
- Certificates: no problem (see "HTTPS certificates").

**Findings:**
- **SBIR contracts rarely appear in NTRS.** From a random sample of 40 NASA
  SBIR contract numbers (2008–2023: 30 Phase II, 10 Phase I), 3 Phase II and
  no Phase I contracts are listed as a funding number. Quoted searches for
  the other 37 found nothing either. NTRS is no general route back to SBIR
  awards.
- Funding numbers are mixed: contract numbers (`80MSFC18C0011`: 700
  records), WBS codes (`448428.05.04.22`) and free text.

### Tools (agreed 2026-09-30)

| Tool | Does | Notes |
|---|---|---|
| `ntrs_search` | Find reports and papers: words or quoted phrases (title, abstract, keywords, authors; not the files' text), plus one exact value each for center, report type, subject, author, organization, keyword, report number and funding number; publication years; only records with files | Sorted by relevance, newest or oldest (by publication date, undated records last). With a year filter, the result says how many undated records it left out. Rows: id, title, type, date, center, first three authors, whether it has files; an option adds the first 300 characters of the abstract. Paging stops at 10,000, and the tool says so |
| `ntrs_get_record` | One record in full: abstract, authors and their organizations, keywords, subjects, funding and report numbers, journal or meeting with DOI, files with sizes | Its PDFs come attached by default (below) |
| `ntrs_read_text` | The text NTRS extracted from a file: 12,000 characters per part, or only the passages that mention given words | For Word and PowerPoint files, PDFs too large to attach, or finding one passage in a long report |
| `ntrs_aggregate` | Counts of records by center, report type, subject, year, author, organization, keyword or funding number, with search's filters | Years in order; other groups the top 20 plus a count of the rest. Shows which forms of a name are in use |

Decided with the user, one at a time:
- Written fresh, from this probe.
- Four tools, with reading text as its own tool: its inputs (a part number,
  or words to find) have nothing to do with looking up a record.
- **Documents attach by default, under the rules `techport_get_project`
  already follows:** up to 11 MB of files embedded per result; a file of
  11–20 MB saved in the plugin's data folder, with its path (reading it may
  ask for permission); over 20 MB, the link only. `include_documents: false`
  skips them. Files without a PDF, or too large, point to `ntrs_read_text`.
- **Name forms:** each filter takes one exact form, as the API does. For
  several forms, Claude writes them into the query with `|`. The tool
  description explains the syntax, and `ntrs_aggregate` shows the forms in
  use. The query finds a name anywhere in the record, not only among the
  authors; accepted as slightly looser.

### Built and tested in Claude Code (2026-09-30)

`servers/ntrs/`:
- `api.py`: the calls, allowed only `ntrs.nasa.gov`.
- `model.py`: request bodies, record shapes, counts and text handling,
  standard library only.
- `server.py`: the four tools, with output through `servers/common/output.py`.

TechPort's rules for attaching files moved to `servers/common/attach.py`, so
both connectors follow one set. Up to 11 MB of files go in one result, and at
most 20 files. A file of 11–20 MB is saved in the plugin's data folder
(`ntrs/files/` here; removed after 30 days); over 20 MB, the link only. A
saved file's name must be plain. TechPort's tests passed unchanged.

**Checked during the build** (a request or two each):
- **"Has files" uses `disseminated`, not the `downloadsAvailable` flag.** In
  500 records, `disseminated: DOCUMENT_AND_METADATA` matched having a file
  list exactly. The flag disagreed on 22: 14 said files and had none, and 8
  had a file (an abstract document) and said none. Rows count the files in
  the list.
- **The 15 centers:** CDMS 381,733 (59% of all records), GSFC 42,194, JPL
  41,536, LaRC 28,467, MSFC 23,084, ARC 22,636, JSC 21,095, GRC 18,675, HQ
  18,457, KSC 6,304, AFRC 2,035, SSC 992, "2230 Support" 615, WFF 76, WSTF 15.
- **Author lists reach 1,186 names** (an IceCube article, a 113 KB record; a
  Planck paper has 802 and 193 KB). `ntrs_get_record` shows the first 50 and
  says how many there are, with the record page's link.
- **The data has two report types the API description lacks,** and both work
  as filters: ABSTRACT (22,560) and EXTENDED_ABSTRACT (1,259). An unknown type
  finds nothing rather than failing, so types are checked in code.
- A page size of 0 returns the counts alone (13.9 KB). `ntrs_aggregate` uses
  it.
- All four tools against the real API: every call under a second.

**Real tokens of the largest results** (method under "sbir_query"):

| Result | Characters | Tokens |
|---|---|---|
| Read text, one part (12,000 characters of a report) | 12,637 | 5,203 |
| Search, 50 rows | 9,147 | 4,725 |
| Read text, passages for "propellant" (12 of 78 shown) | 12,087 | 4,516 |
| Search, 25 with abstracts | 9,821 | 4,226 |
| One record with 50 of its 802 authors | 5,190 | 2,306 |
| Counts by year, whole collection (117 years) | 1,675 | 956 |

Loading the four tool definitions cost 3,328 tokens.

**Tested in Claude Code 2.1.286**, with the NTRS tools and Read allowed and
Bash and the web tools not:
1. *"How has NASA's output of technical reports and papers on cryogenic fluid
   management changed over the decades, and which NASA centers produce most of
   it?"* 15 calls (14 counts, one search), 55 s.
   - Claude wrote its query in the search syntax (or, prefix, grouping), and
     dropped "CFM" after a search showed unrelated matches.
   - It answered with 1,022 records (334 with the exact phrase). By decade
     from the 1960s: 35, 58, 123, 139, 130, 240 and 65. Centers: CDMS 322,
     GRC 290, MSFC 223, KSC 96. 232 records undated.
   - It named the limits: undated records missing from the decades, legacy
     records without a center (101 of them from Lewis), and counts weighted
     to conference papers.
   - Checked afterwards against direct queries and the saved tool results:
     totals, decades, peak years, centers overall and by era, report types
     and legacy organizations all match.
   - **One derived figure was wrong.** Claude put Glenn, Marshall and Kennedy
     at "609 of 674, about 90%" of the records with a center. The center
     counts it was given add up to 697, so it is 87%. That was Claude's own
     arithmetic on count results, which the grounding rule warns against.
2. *"What has Jason Hartwig published in NTRS about liquid acquisition
   devices? List the records, then read the most recent one and tell me what
   it concluded."* 10 calls, 51 s.
   - Claude found the name forms with a count, then searched them in one
     query (21 records).
   - It saw that the newest record has no publication date and sorts last.
   - It used `find` to reach the conclusions, and read two parts of the text
     rather than the PDF.
   - The figures it quoted are all in the report's text: 95% expulsion
     efficiency (85% under the conservative assumption), under 4 cm² of
     wetted screen at 2 gpm, and 4 flights of about 120 parabolas.
3. *Attached PDF:* what the first figure of 20180002393 shows, as drawn.
   - The PDF came attached. Claude Code saved it, and Claude read the page
     images with Read and described Figure 1 correctly.
   - Claude said `attached: true` had led it to expect the document itself.
     It paged through 16 pages to find the figure (23,605 tokens).
   - **Fixed:** when PDFs are attached, the result says what Claude Code does
     with them, and points to `ntrs_read_text` with `find` for a figure's
     page.
   - Re-run: Claude searched for "Figure 1" first, and read 7 pages instead
     of 16 (9,932 tokens instead of 23,605).
   - The search had also matched Figures 10–19 (19 matches). **Fixed:**
     `find` matches whole words, with `*` for a prefix, as the search does.
     The same search now gives 3 matches.
   - This report's extracted text has no page marks, so `find` can't give
     the page. Only XHTML extractions have them.

**Agreed 2026-09-30, after the tests:** keep the attached-PDF note, whole-word
`find`, and a live check (`LIVE_TESTS=1`) that NTRS's responses still have
the shapes the tools rely on.

## NASA Technology Transfer connector (probed 2026-09-30)

`technology.nasa.gov`, the Technology Transfer portal's API: no key, JSON.
Its own page (`/api/`) documents three keyword searches and nothing else:
`/api/api/{patent|software|spinoff}/{keywords}`. About 250 requests over five
rounds, read-only.

**What a search returns:**
- **Every match at once,** best score first, as arrays of 13 values by
  position rather than named fields. "space" returned 340 patents, 441 KB, in
  0.9 s; most searches took under a second.
- **There is no paging.** The `page` and `perpage` in each response mean
  nothing, and a `?page=2` parameter is searched as a term: "cryogenic 2"
  returns the same 77 records. Requests must carry no query parameters.
- **Spinoff searches stop at 1,000 results:** "nasa" and "space" returned
  exactly 1,000. Patent and software searches stayed under it.
- An empty search is a 500 error, and `*` finds nothing. No rate-limit
  headers in about 250 requests.

**The 13 positions:**

| | Patents | Software | Spinoffs |
|---|---|---|---|
| 0 | internal id | internal id | internal id |
| 1 | reference number (`KSC-TOPS-59`) | case number (`ARC-17900-1`) | spinoff number (`KSC-SO-111`) |
| 2 | title, with highlight markup | same | same |
| 3 | description: median 970 characters, longest 1,497 | median 655, longest 2,828; some with `<p>` | the story: median 530, longest 2,415; HTML entities |
| 4 | the same as 1 | same | same |
| 5 | category | category, in lower case | category |
| 6 | empty | release type (Open Source, U.S. Release Only, ...) | empty |
| 7 | empty | how to get it (32 of 142 in one search) | empty |
| 8 | empty | a link or file name (64 of 142; often GitHub) | empty |
| 9 | center | center | center |
| 10 | image link | empty | empty |
| 11 | tags, rarely (6 of 77) | empty | empty |
| 12 | score | score | score |

**How searches match (measured):**
- **Several words give part of the union of each word's matches.**
  "cryogenic tank" returned exactly the union (37 = 30 + 13 − 6 with both).
  But "space technology" returned 229 patents, between the 185 with both
  words and the 460 with either. Spinoff "nasa technology" returned 716,
  fewer than "technology" alone (977). *Unexplained*; low-scoring matches may
  be dropped.
- **Search covers text it doesn't return.** Of the 30 patents "cryogenic"
  finds, only 19 have the word in the fields returned (number, title,
  description, category, tags); for the other 11 it must be elsewhere,
  perhaps the page's benefits and applications (*assumed*). Every one of the
  611 combined patents with the word was among the 30. So filtering returned
  fields can't reproduce the API's matching.
- Quotes are ignored: "cryogenic tank" in quotes gives the same 37.
- A reference or case number finds its record (`KSC-TOPS-59`: 1 result).
- Short words match oddly: "a" finds 129 patents, "the" 117.

**What's in the catalogs.** No endpoint lists them. Combining searches (common
words, every category, every center) found:
- 611 patents; the last 10 of 72 searches added none.
- 1,270 software entries; the last 10 of 59 added one.
- 2,466 spinoffs, still growing at the end of 63 searches, two of which hit
  the 1,000 cap.

These are lower bounds. The API's page says the patent portfolio has "over
1,400 technologies"; *unexplained*.

**What the tools must handle:**
- **Center codes vary:** `LARC` and `LaRC`, `DFRC` (Dryden, now Armstrong) and
  `HDQS` (Headquarters).
- **Categories differ only in case:** "Materials and Coatings" 36 and
  "materials and coatings" 21 in one search.
- **Markup to strip:** the highlight `<span>`s, HTML entities (`&apos;`) and
  `<p>` tags.
- **No dates:** the API gives no filing, issue or release date, and no
  spinoff year.

**Patent pages** (`/patent/{reference}`, HTML on the same host) carry what
search doesn't:
- benefits and applications
- every case number
- the US patent numbers, linked to the USPTO's `ppubs.uspto.gov`
- papers, and similar technologies

Six pages from six centers all mark these with the same class names
(`benefits`, `applications`, `case_number`, `patent_number`, `publications`).
Reading them means parsing HTML, which the standard library can do. The
plugin would run none of the page's scripts; the page loads analytics from
`dap.digitalgov.gov`, among others. Pages send software and spinoffs to other
hosts, `software.nasa.gov` and `spinoff.nasa.gov`, which weren't probed.

**Findings:**
- **395 spinoff stories mention SBIR:** all 395 results for "SBIR" do. They
  usually name the company and the center, e.g. "With SBIR awards from Kennedy
  Space Center, Sierra Lobo Inc. ... developed the Cryo-Tracker Mass Gauging
  System". That could link spinoffs to SBIR awards by company name (*not
  tested*).
- Certificates: no problem (see "HTTPS certificates").

### Tools (agreed 2026-09-30)

| Tool | Does | Notes |
|---|---|---|
| `techtransfer_search` | Patents, software and spinoffs, or one kind, by search words; filters for center and category | All words must match: one API search per word, intersected; `match: any` gives the union. Rows: kind, reference, title, category, center, and release type for software; an option adds the first 300 characters of the description. Every result also gives the total and counts by kind, center and category, exact within the search. Pages locally. Says when a spinoff word hit the API's 1,000 cap, and that the API can't list a whole catalog |
| `techtransfer_get` | One record by reference number (`KSC-TOPS-59`, `ARC-17900-1`, `KSC-SO-111`) | The full description. For a patent, its page adds benefits, applications, case numbers, US patent numbers (with USPTO links) and papers. For software: release type, how to get it, and its link. For a spinoff: the story |

Decided with the user, one at a time:
- Written fresh, from this probe.
- **Live only.** Each search returns all its matches at once, so counts within
  it are exact (spinoffs stop at 1,000). A copy built from combined searches
  couldn't be shown complete, nor match like the API.
- **Several words: all must match,** by intersecting one search per word; an
  option gives any word.
- **Patent pages are read** for one patent at a time: same host, standard
  library, no scripts run. If a page changes shape, the tool falls back to
  the search fields and the page link, and says so.
- **Two tools.** No separate counts tool: every search already holds all its
  matches.

### Built and tested in Claude Code (2026-09-30)

`servers/techtransfer/`:
- `api.py`: the calls, allowed only `technology.nasa.gov`, with no query
  parameters.
- `model.py`: positions to named fields, markup cleanup, one spelling per
  center, the per-word intersection, counts, and reading a patent page.
  Standard library only.
- `server.py`: the two tools, with output through `servers/common/output.py`.

**Checked during the build:**
- **Numbers show their catalog.** Patents are `XXX-TOPS-N` or `TOPn-N`,
  spinoffs `XXX-SO-N`, and software everything else (`NPO-N-N`,
  `ARC-N-N`...). A number searched in the wrong catalog finds nothing.
- **A number search isn't exact.** "KSC-TOPS-5" returns KSC-TOPS-58 and -59,
  so `techtransfer_get` keeps only the exact match, and names the close ones
  when there is none.
- **All 10 sampled patent pages have all five sections.** The page ends with
  "Similar Results", which uses the same class names (`title`, `description`,
  `benefits`) for other patents, so reading stops there. A page's "The
  Technology" text isn't in the API.
- All tools against the real API: every call under a second.

**Real tokens of the largest results** (method under "sbir_query"):

| Result | Characters | Tokens |
|---|---|---|
| Search, 25 with descriptions | 11,087 | 4,285 |
| Search, 50 rows (any word) | 6,219 | 3,048 |
| One patent with its page (the largest of the 12 with the longest descriptions) | 6,853 | 2,595 |

Loading the two tool definitions cost 1,240 tokens.

**Tested in Claude Code 2.1.286**, with the Tech Transfer tools (and, for the
second question, the SBIR tools) allowed, and Bash and the web tools not:
1. *"Which NASA technologies are available to license for keeping cryogenic
   propellants cold, and what do they cover? Include the US patent
   numbers."* 14 calls (8 searches, 6 patents), 42 s.
   - Claude combined all-word and any-word searches, then read six patents'
     pages.
   - It grouped them by fit to the question and left out instruments that
     only measure insulation.
   - It said three of the six pages list no US patent numbers, only NASA case
     numbers, and that the data can't say whether patents are pending.
   - Every patent number, case number and figure it quoted is on the pages.
2. *"Which NASA spinoff products came out of SBIR awards from Glenn Research
   Center? Name the companies, then for two of them show their NASA SBIR
   awards."* 6 calls, 38 s.
   - It found 54 spinoffs and named about 40 companies from the stories'
     first 300 characters. It flagged stories that credit another center's
     SBIR, and those that don't name Glenn.
   - It then listed Hyper Tech Research's 18 and Ridgetop Group's 21 NASA
     SBIR awards.
   - For each of the two companies it proposed the Phase I and II pair that
     fits its spinoff story, by title and topic, and said the link itself
     isn't in the data.
   - Every company name is in the spinoff results, and every award number,
     amount, year and total it quoted is in the SBIR results.
   - It also used general knowledge, that NNC contracts were Glenn's, and
     said that this isn't in the data. Like the NTRS test's arithmetic, that
     goes past the grounding rule, though it was labelled.

**Added after the tests:**
- **Common words are left out of searches.** Every one of 17 common words
  measured is under-matched by the API. "and" finds 87 patents where at
  least 606 of the 611 combined have it in their title or description; "is"
  86 of 440; "on" 192 of 282. As one of several required words, each would
  silently drop most matches. Thirty common words are now left out (the 13
  not measured are *assumed* alike) and named in the result; they don't
  count toward the 6-word limit. A search of only common words is refused.
  Content words go the other way ("using" 200 against 141), as the API also
  matches text it doesn't return.
- As for NTRS, a live check (`LIVE_TESTS=1`) that results still come by
  position and patent pages still have their sections.
- `techtransfer_get` upper-cases a number before searching (the data's
  numbers are upper case), and `plugin.json` gained the keyword "technology
  transfer".

**Agreed 2026-09-30, after the tests:** keep all four of these changes.

## SEC EDGAR connector (probed 2026-09-30)

SEC's EDGAR on three hosts: `www.sec.gov`, `data.sec.gov` and `efts.sec.gov`.
No key, JSON, gzip. About 30 requests over three rounds, read-only, at no more
than four a second.

**SEC's access rules** (its fair-access policy, not measured): every request
must carry a User-Agent with the requester's contact details, and no more than
10 requests a second. No rate-limit headers were seen. The probe sent a
contact the user chose; how plugin users supply theirs is for the tools
decision.

**What works:**

| Need | Endpoint | Measured |
|---|---|---|
| Ticker to company id (CIK) | `www.sec.gov/files/company_tickers.json` | 10,431 listed companies; 798 KB (220 KB gzipped) |
| A company by name | `efts.sec.gov/LATEST/search-index?keysTyped=...` | Suggestions from EDGAR's own company index, unlisted filers included |
| A company and its filings | `data.sec.gov/submissions/CIK##########.json` | Rocket Lab: profile (industry code, addresses, tickers, former names) and 639 recent filings, 103 KB |
| Financial facts | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | Rocket Lab: 399 US-GAAP concepts, 1.3 MB (96 KB gzipped) |
| One fact across companies | `data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/CY2024.json` | Revenues: 2,503 companies, 383 KB |
| Full-text search | `efts.sec.gov/LATEST/search-index?q=...` | 0.1–2.8 s; 100 hits a page, `from` for more; filters for form, date range and company; counts by company, industry code, state and form |
| A filing's files | `www.sec.gov/Archives/edgar/data/{cik}/{accession}/index.json` | Rocket Lab's latest 10-K: 133 files |
| A filing document | `www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}` | That 10-K: 2.4 MB of HTML, about 430,000 characters of text |

**Full-text search (measured):**
- Quoted phrases work. `"Small Business Innovation Research" NASA` finds
  108 annual reports (10-K) and 510 filings of any form;
  `"SBIR Phase III" NASA` finds 7 in 10-K, 10-Q and 8-K filings.
- It covers 2001 on: the earliest hits are from 2001.
- Broad words work too: "NASA" in 10-Ks gives 2,915.
- **Errors come and go:** three queries returned a 500 error and worked when
  repeated. Requests need retrying.

**What the tools must handle:**
- **Filings are long.** A 10-K runs to hundreds of thousands of characters
  ("NASA" appears 23 times in Rocket Lab's), so reading needs the passages
  approach used for NTRS's extracted text.
- **Concept names vary by company.** Rocket Lab reports revenue as
  `RevenueFromContractWithCustomerExcludingAssessedTax`, not `Revenues`
  (a 404), so financial facts need a list of alternatives per measure.
- **Most SBIR companies aren't SEC filers.** Creare has no entity at all;
  Astrobotic has one, with three paper Reg D notices (`REGDEX`). EDGAR helps
  most with public companies.
- **No shared identifier.** EDGAR has no UEI or DUNS number, so the link from
  SBIR or USAspending is the company name, or a ticker.

### Tools (agreed 2026-09-30)

| Tool | Does | Notes |
|---|---|---|
| `edgar_company` | A company by name, ticker or CIK: profile (industry code, state, tickers, former names, website) and its recent filings, filtered by form type | Filings as a table: form, filed, period, description, 8-K items, accession number. An ambiguous name lists the candidates |
| `edgar_search` | Full-text search of filings from 2001 on: words and quoted phrases, form types, years, one company | Hits as a table, with counts by company, form and state; paging |
| `edgar_financials` | Key annual figures from a company's XBRL facts: revenue, R&D, operating and net income, cash, total assets | By year, naming the concept used for each, since names vary by company |
| `edgar_read_filing` | A filing's main document as text, by accession number | Parts of 12,000 characters, or the passages that mention given words, as for NTRS |

Decided with the user, one at a time:
- Written fresh, from this probe.
- **Each user's contact is an optional plugin setting** (`userConfig`),
  asked for when the plugin is enabled and changeable in `/config`. Users
  who never need EDGAR can skip it; the EDGAR tools then refuse and say how
  to set it. It is stored in the user's Claude Code settings, sent only to
  SEC's hosts, and never written to the repo. Checked: Claude Code 2.1.286's
  validator accepts the field and rejects a reference to an undeclared one.
  *Not yet tested:* the prompt when the plugin is enabled, and the value
  reaching the server.
- **Four tools,** with financials on their own: they need a 1.3 MB download
  per company and answer a different kind of question.

### Built and tested in Claude Code (2026-09-30)

`servers/edgar/`:
- `api.py`: the calls, allowed only SEC's three hosts. Every request carries
  the user's contact, compressed responses are asked for, and requests are
  spaced to stay under 7 a second.
- `model.py`: the contact check, company lookup, filings, search hits and
  counts, financials, and filing HTML to text. Standard library only.
- `server.py`: the four tools, with output through `servers/common/output.py`.

Shared changes made for it:
- `servers/common/text.py`: NTRS's parts and passages, moved so both
  connectors use them. NTRS's tests passed unchanged.
- The HTTP client can ask for gzip, for EDGAR only: the 10-K is 2.4 MB as
  HTML, 209 KB compressed. A compressed body over the size limit is refused
  rather than cut short (a test caught that case before it shipped).

**Checked during the build:**
- **Filing text.** The 10-K's 2.4 MB of inline-XBRL HTML becomes 435,395
  characters of text in 0.1 s, with 110 page marks from its page breaks.
  Tables read as rows ("Total revenues | 601,799 | 436,214 | 244,592"), and
  nothing from the hidden XBRL header comes through.
- **The contact setting reaches the server.** Claude Code fills in
  `${user_config.sec_contact}` from `pluginConfigs["<plugin>@<marketplace>"].options`
  (`@inline` for a plugin loaded with `--plugin-dir`). It reads that from
  user settings, `--settings` or managed settings, not from a project's
  settings; an unset option arrives as an empty string. Verified for
  `@inline`, and for the installed plugin's key
  (`agent-techport-sources@agent-techport-sources`) in the field test of
  2026-10-01, where the prompt appeared at install and the value was saved
  under that key in user settings.
- **SEC accepted an email alone.** The test runs sent only an email, and
  every request was answered. The setting asks for a name and email, as SEC's
  policy does; the tools accept either. A documentation
  summary had given the shape without `options`, which cost three test runs;
  Claude Code's own messages showed the right one. *Not tested:* the prompt
  when an installed plugin is enabled.
- A company with no XBRL facts (Astrobotic) gets a clear error; one without
  a ticker is found through EDGAR's own index.

**Real tokens of the largest results** (method under "sbir_query"):

| Result | Characters | Tokens |
|---|---|---|
| Search, 50 hits | 8,408 | 5,192 |
| One part of the 10-K | 11,821 | 4,031 |
| Passages for "NASA" in the 10-K (23 matches) | 10,998 | 4,009 |
| A company with 50 filings | 5,389 | 3,029 |
| Financials | 1,220 | 625 |

Loading the four tool definitions cost 2,047 tokens.

**Tested in Claude Code 2.1.286**, the contact supplied with `--settings`:
1. *"Which public companies have said in SEC filings that they won NASA SBIR
   Phase III contracts, and what exactly did they say?"* 58 turns, 227 s.
   - The answer is careful and grounded: none of the hits is a public
     company describing a NASA SBIR Phase III. The nearest is Joby's Air
     Force Phase III agreement, filed as an exhibit, which NASA funds in
     part. The rest are other agencies' Phase IIIs, companies that file
     under Regulation A or crowdfunding, or coincidences.
   - All 30 factual claims checked (quotes, contract and accession numbers,
     amounts, dates, agencies) are in the tool results. Two needed a second
     look: a quote Claude shortened with an ellipsis, and a sentence a
     pattern missed at "U.S.".
   - It also cross-checked through the USAspending and SBIR tools (379 NASA
     Phase III contracts; the Nano Nuclear inventor's four DOE awards).
     `--allowedTools` doesn't restrict tool use on the test machine.
   - **It took so long because search hits carry no text.** To judge each
     hit, Claude had to open the filing and find the passage. See open
     question 15.
2. *"How have Rocket Lab's revenue and R&D spending changed in recent years,
   and what does its latest 10-K say about NASA?"* Capped at 15 turns; took
   7, 32 s.
   - Revenue and R&D by year came straight from `edgar_financials`; Claude
     labelled its growth percentages as its own arithmetic.
   - From the 10-K's NASA passages: two launch sites at Wallops, the
     CAPSTONE and ESCAPADE missions, NASA's launch certification, and US
     government work at 47% of revenue, NASA not broken out.
   - All 20 factual claims checked are in the tool results; the growth
     percentages are Claude's own arithmetic, and it said so.

## Proposed layout

```
agent-techport-sources/                  marketplace root (repo)
├── .claude-plugin/
│   └── marketplace.json                  one entry, source "./plugins/agent-techport-sources"
├── plugins/
│   └── agent-techport-sources/          everything users install; nothing else is copied
│       ├── .claude-plugin/plugin.json    name, version, mcpServers
│       ├── servers/                      sbir, techport, usaspending, ntrs, techtransfer, edgar;
│       │                                 common/ (HTTP, output, file attachments, local databases)
│       ├── skills/                       how to use each source; the linking rules above
│       ├── pyproject.toml, uv.lock       must sit here: servers run from the installed copy
│       └── README.md                     install, requirements, exactly what leaves the machine
├── tests/                                dev only, not installed
├── research/                             dev only, not installed
├── docs/design.md
└── README.md                             repo overview, points to the plugin README
```

Notes on the SDK's dependencies, for reviewers: `opentelemetry-api` is a
required dependency of `mcp`. On its own it records and sends nothing; data
only leaves the machine if an OpenTelemetry SDK and exporter are installed and
configured, and this plugin includes neither. Checked 2026-09-30 in the
installed environment: the tracer provider is OpenTelemetry's no-op
`ProxyTracerProvider`, and `opentelemetry-sdk` is not installed. The SDK's HTTP client (`httpx2`),
Starlette and Uvicorn come with it for its HTTP transports; the plugin's
servers use stdio.

`pyproject.toml` sets `[tool.uv] package = false`: uv runs the servers straight
from the plugin directory, so no build backend is fetched at install time.

Why a subdirectory (checked against the Claude Code plugin docs, 2026-09-30):
on install, Claude Code copies only the plugin's source directory into its
cache and loads that copy. Files outside it aren't copied, and a plugin can't
reference paths above its own root (`../` fails validation). So docs, tests and
research stay out of users' installs, and anything the servers need at run time
(code, `pyproject.toml`, `uv.lock`) has to live inside the plugin directory.
`./plugins/<name>` is the form the marketplace docs use, and it leaves room for
a second plugin later. `claude plugin validate .` run at the repo root checks
the marketplace and each plugin entry.

User install:

```
claude plugin marketplace add tobedetermined/agent-techport-sources
claude plugin install agent-techport-sources@agent-techport-sources
```

Platforms: macOS is tested. Windows is intended and the code allows for it
(no Unix-only calls, and the database refresh copes with files that other
sessions have open), but it is **not yet tested on a Windows machine**. Say so
in the README until someone has installed it on Windows, run the tests and
asked an SBIR question.

**Linux, tested 2026-09-30** in Docker containers of Debian 12 (Astral's
`uv:python3.12-bookworm-slim` image: Python 3.12.12, SQLite 3.40.1), on ARM64
and, emulated, x86-64. Packages installed from `uv.lock` as users get them
(28), then the full suite and the live HTTPS checks:
- **One bug found and fixed.** `sbir_query`'s keyword searches failed: SQLite
  3.40 prepares, but never runs, an `UPDATE` of `sqlite_master` while FTS5
  sets up its index, and the query sandbox refused its checks (a read of
  `sqlite_master.ROWID` and updates of its five columns). The sandbox now
  allows exactly those six in the main database; nothing can be written, as
  the connection is read-only and `query_only`. SQLite 3.53 on macOS makes
  none of these checks.
- After the fix: all 341 tests pass on both.
- **Every source host connects over HTTPS** with Linux's own certificate
  store (OpenSSL 3.0.18): USAspending, NTRS and Tech Transfer through the
  live tests on both architectures; SBIR.gov, TechPort and SEC's three hosts
  through each server's own client, once each, from ARM64.
- *Not tested:* Claude Code itself on Linux, and Linux distributions other
  than Debian 12.

**Install-time hosts beyond PyPI.** If the machine has no Python 3.11 or
newer, uv downloads one from `releases.astral.sh` (Astral, the maker of uv,
mirroring python-build-standalone). uv's own installer also comes from
Astral. So the boundary statement is: at run time, only the source hosts; at
install time, PyPI, plus Astral if uv has to fetch Python. Setting
`UV_PYTHON_DOWNLOADS=never` stops the Python download, at the cost of needing
Python installed first.

**Decided 2026-09-30: allow the download, and document it.** Requiring Python
up front makes installing harder for the users least able to manage it, and
the download is a one-time fetch of a Python build, not a relay for any data.
The README must say plainly: which hosts are contacted and when; that Astral
is contacted only if no Python 3.11+ is found; and how to prevent it (install
Python first, or set `UV_PYTHON_DOWNLOADS=never`).

### HTTPS certificates (found 2026-09-30)

USAspending failed from the plugin's Python with "certificate verify failed:
self-signed certificate in certificate chain".

**The cause:**
- The Python that uv installs on macOS checks certificates against
  `/etc/ssl/cert.pem`, a static file shipped with macOS.
- That file lacks "Sectigo Public Server Authentication Root R46". USAspending's
  certificate chains to that root through an Entrust intermediate; Entrust's
  public certificates moved to Sectigo.
- The macOS keychain and Homebrew's bundle have the root. That is why curl,
  Safari, Apple's own Python and Homebrew's Python all connected.
- The other sources chain to roots the file has: SBIR.gov, TechPort, NTRS,
  NASA Technology Transfer and SEC EDGAR were all checked.
- Linux is unaffected: all eight source hosts connect from a Debian 12
  container (see "Platforms"). Windows is *assumed* unaffected (not
  tested).

**Decided 2026-09-30:** check certificates against the operating system's own
trust store, using `truststore`.
- On a managed machine, that is the list the organisation manages: nothing new
  is trusted, and certificate checking is never switched off.
- `truststore` is MIT-licensed and has no dependencies. pip ships the same
  version. It was already installed with the MCP SDK (`httpx2`, `httpcore2`),
  so declaring it adds no package.
- It asks macOS for the standard website check and adds no explicit
  revocation check. Whatever revocation checking macOS does on its own is the
  same as for any other app.

**In code:**
- `servers/common/http.py` imports `truststore` on the first HTTPS request, so
  tests on a plain Python don't need it.
- A certificate failure is reported as such and not retried; before, it read
  as "not responding".
- `LIVE_TESTS=1` runs a live check that USAspending connects.

Requirements: Claude Code, `uv`, outbound HTTPS to the source hosts and PyPI,
and about 1.1 GB of disk for SBIR data (up to about 1.8 GB briefly during a
monthly refresh; see "Loader"). Put executables somewhere other than a
top-level `bin/`: plugins that have one can't be installed through claude.ai or
Cowork.

## Field test from another project (2026-10-01)

The plugin installed from the local marketplace (local scope) into a
separate research project, and a Claude session there ran test batches for
each server, reporting failures to a session in this repo. Installing worked:
the SEC contact prompt appeared, all six servers connected, and the first
SBIR question built the database. The older TechPort connector in claude.ai
was switched off for the test, so answers could only come from the plugin.

Found, and fixed in 0.1.1 (counts are live data, 2026-10-01):

| Finding | Cause | Fix |
|---|---|---|
| `program="FO"` gave 955 projects across 9 programs, not 430; the live keyword path gave 59 for "regolith", not 36, with nothing in the output to show it | The filter matched the exact acronym **or** any title containing the text ("for", "inFOrmation") | The first rule that matches wins: a programId; an exact acronym; one part of an acronym (`SBIR` in `SBIR/STTR`); an exact title; part of a title. Results show `program_filter` (the programs matched), with a note when there are several (PSRP, SBP). On live results it resolves against every program, not only those in the results. Rows carry `programId`. FO now gives 430, FO + "regolith" 35, FO + Moon 178, FO + Industry 134. |
| `program="SBIR"` gave 0; `program="72"` gave 0 | As above | As above; no match is an error naming the closest programs |
| SBIR `keywords="parabolic \| suborbital"` gave 1, not 82; USAspending gave 0 | `\|` was searched as a word | One syntax for SBIR and the TechPort copy, NTRS's: words, "phrases", OR or `\|`, (parentheses), -word, word* (`servers/common/keywords.py`, checked against SQLite FTS5). USAspending's keyword filter takes a list of alternatives (checked: `["parabolic","suborbital"]` gives 138 contracts, 63 + 76 less one in both), so `\|` and OR become that list, and parentheses or -word there are refused. `word*` is now a real prefix (SBIR `lunar*`: 1,142, was 1,137, the same as `lunar`). TechPort's own live search has the same problem (measured: `parabolic \| suborbital` 38, the same as `parabolic suborbital`; `regolith (parabolic OR suborbital)` 831, the count for `regolith` alone, against 18 and 22 for each pair), so a `find_projects` query with OR, `\|` or parentheses goes to the copy's index (27 for that query) with a note. Phrases and -word stay live; they work there (327; 635 of 831). |
| EDGAR said "no contact" when one was set | The value saved at install was a name with no email, which the server rejects, then reported as missing | Separate messages for a contact with no email and one that can't be sent; each says to restart after setting it. The server instructions say to tell the user before trying other EDGAR tools. |
| `usaspending_recipient(name="Virgin Galactic")` and `"Zero-G"` miss the companies | USAspending's recipient search covers current legal names only (checked: 0 results), while the award search matches former and trade names | The result and the tool description point to `usaspending_search_awards` with `company=`, whose rows give the UEI |
| Award numbers with no award were dropped silently | Not reported | `usaspending_aggregate` and `usaspending_search_awards` list them |
| `techport_capabilities(query="dust")` stopped at 9 of 36 | Full descriptions (up to 3,500 characters each) hit the size limit, with no offset | Descriptions cut to 200 characters in the search list (36 rows: 11,424 characters), `offset` added; `capability=` gives them in full |
| `sbir_query("DELETE ...")` said "cannot modify awards because it is a view" | SQLite's message | Anything not starting with SELECT, WITH or VALUES is refused first with one message; the authorizer behind it is still tested through `WITH ... DELETE` |
| NTRS exact-value filters in the wrong case gave a silent 0 | Documented, but easy to miss | When nothing matches, one extra request per such filter finds the forms in use ("Propellants And Fuels" and "Propellants and Fuels" both exist) and names them |
| Tech Transfer category counts split one category by punctuation | Upstream values | Counted together when they differ only in case or punctuation; abbreviations such as "ip" and "ps" are left as they are rather than guessed |
| TechPort has no UEI for some newer companies (Interlune) that USAspending has | Upstream data | `techport_organizations` notes that a null UEI means TechPort has none, and to link by name |

Re-test of 0.1.1, after a restart there: every fix above passed. Found then:
SBIR's own award ids change when the monthly file reloads (the file updated
2026-10-01 05:42; award 91961 became 92000, 54120 became 54037), because the
id is the row's place in the file. The descriptions now say the id is only for
the current copy and not to cite it; awards are cited by tracking number and
contract, which `sbir_get_award` takes. A stable id derived from the record
was considered and not built: no pair of fields is unique (see "sbir_query"),
so it would need most of the row and still not survive upstream corrections.

EDGAR (batch 6, once the contact had an email), fixed the same day:
- **Financials 1,000× too small** (Virgin Galactic net income 2021-2025:
  -352,899 for -352,899,000). SEC's companyfacts put the `CY2021`-`CY2025`
  frames on figures from the company's proxy (DEF 14A, the
  pay-versus-performance table, tagged in thousands), while three 10-Ks
  report the same periods in dollars. Of this company's framed figures,
  4,530 came from 10-Q, 10-K and 10-K/A and five from DEF 14A. Now a framed
  figure from anything but a periodic report (10-K, 10-Q, 20-F, 40-F and
  their amendments) is replaced by the latest report's figure for the same
  period, or left out. Rocket Lab's figures are unchanged.
- Full-text phrases match in any case, so "Flight Opportunities" also finds
  airline filings ("greater flight opportunities"): 87 hits, a few relevant.
  The instructions now say to add a word such as NASA or a form filter.
- A private company's name can match only unrelated filers (two funds'
  "Series ... Blue Origin" vehicles). The lookup now says when no filer's
  name starts with the name asked, and that private companies often don't
  file with the SEC.
- Search passages, open question 15: 10 passages for 87 hits came to about
  6-7 KB and a few seconds, judged fine by the test.

Not changed: PSC V126 is all launch services (SpaceX at the top), so it
isn't a way to isolate Flight Opportunities' purchases; that is research
advice, not a tool problem.

## Open questions

1. **Release.** **Decided 2026-09-30:**
   - A personal open-source project, renamed `agent-techport-sources` (see
     "Decisions": Name) and licensed Apache-2.0.
   - **History: squashed at publish time.** The public repo starts from one
     commit of the final tree; the full local history stays on a branch
     that is never pushed. Earlier commits hold text since removed, under
     the old name, and the interim author line `tobedetermined`.
   - **Author line:** "Alexander van Dijk" with his GitHub noreply address,
     which he gives when publishing, and a `Co-Authored-By: Claude Opus 5.5
     <noreply@anthropic.com>` trailer, so GitHub shows both, as every local
     commit already does.
   - **Credit (asked for by Alexander):** wherever his name is written,
     Claude Opus 5.5 gets at least as much credit. The READMEs say "Written by
     Claude Opus 5.5 (Anthropic) with Alexander van Dijk" and who did what;
     `plugin.json` names both as author. The copyright line and the
     marketplace owner stay his: someone who can hold copyright and answer
     for the marketplace has to be named there.
   - Linux: tested in containers (see "Platforms"). Still to do: Windows
     testing.
   - **`log.md` stays out of the public tree** (decided 2026-10-01): it is a
     working log with local paths and names an internal study. It stays in
     the full local history; the contributor notes that point to it are
     reworded at the squash.
   - **Code review before publishing** (2026-10-01): an ultrareview of the
     shipped code (servers, manifests, READMEs; 47 files, 6,714 lines; the
     8,000-line limit kept tests and docs out of scope) found two real bugs
     and one waste, all fixed with tests: the live program list could be
     filled twice by concurrent first calls (the SDK runs tools in threads),
     so every program looked ambiguous; an id asked twice in
     `techport_get_project` came back twice while the count said once; and
     once the attachment budget was spent, files of unknown size were still
     downloaded only to be left for later (now only while 1 MB is left). Of
     three style notes, two were applied; the third (two servers each keep a
     three-line row helper) was left, to keep the servers independent.
2. **NASA marketplace allowlist.** Managed Claude Code settings
   (`strictKnownMarketplaces`) can stop users adding GitHub marketplaces. Find
   out whether NASA's deployment sets this.
3. ~~Contact fields in tool output.~~ **Decided 2026-09-30:** company and PI
   name by default, full contact details on request. See "SBIR contact data".
4. ~~Plugin at the repo root or in a subdirectory.~~ **Decided 2026-09-30:**
   `plugins/agent-techport-sources/`. See "Proposed layout".
5. **Per-server tool surface.** SBIR is decided (see "Tools (v1)"). For the
   other servers the suggested starting point is the same shape: search, get
   one record, and one aggregate where the API supports it. Widen when a study
   needs more.
6. **Untested agencies** for SBIR→USAspending linking (see above).
7. ~~Python from Astral at install time.~~ **Decided 2026-09-30:** allowed
   and documented. See "Install-time hosts beyond PyPI".
8. ~~Sorting in `sbir_search_awards`.~~ **Decided and built 2026-09-30:**
   `sort` = relevance (default with keywords), newest (default otherwise),
   oldest, or amount (largest first; awards without an amount last).
9. ~~Ranking companies.~~ **Decided and built 2026-09-30:** `sbir_aggregate`
   takes `group_by: "company"` (grouped by UEI, named by the latest award; top
   20 by default, up to 100), plus `sort` (awards or amount) and `limit` for
   every grouping. Awards without a UEI are counted and reported, not dropped.
   Checked on the real file: NASA since 2015, Creare 77, CFD Research 73.
10. ~~Read-only SQL for SBIR (`sbir_query`).~~ **Built 2026-09-30.** See
    "sbir_query" under "SBIR connector". An escape hatch alongside the four
    tools, not instead of them, for questions they don't cover.
    - Why not full "code mode" (model-written code run in a sandbox)? A sandbox
      is a large dependency and a security review. Code written per question
      can't be reviewed in advance. And for the remote APIs, the model's code,
      not ours, would decide which hosts are contacted.
11. ~~Placeholder text in SBIR fields.~~ **Built 2026-09-30** (schema version
    2; see "Loader"). Measured 2026-09-30:
    - `abstract` is `N/A` or similar (`NA`, `None`, `null`) on 28,210 rows,
      4,720 of them NASA.
    - `ri_name` is `Stub` on 15,222.
    - `topic_code`, `solicitation_number`, `company_website` and others hold
      such text on a few hundred rows.

    **Decided 2026-09-30:**
    - The loader turns this family (`N/A`, `NA`, `None`, `null`, in any
      case) into NULL in every text field. The contact columns get scanned
      first.
    - `Stub` stays, as before.
    - A schema-version bump rebuilds each local database once.
12. ~~Result caps were sized with estimated token counts.~~ **Built
    2026-09-30.** See "Result sizes (all tools)". Every measured list result
    is now under 8,000 tokens. Single TechPort projects with long text reach
    9,871, under the warning. A full SBIR search
    page is 13,351 real tokens, and over 10,000 by Claude Code's own count
    too, so it is over the warning (*assumed:* the warning uses that count).
    **Decided 2026-09-30:** next, before USAspending.
    1. Measure each capped result's worst case, SBIR and TechPort, with the
       method under "sbir_query".
    2. Propose changes per tool.
    3. Bring each worst case under about 8,000 tokens, the same margin as
       `sbir_query`.

    **Measured 2026-09-30:** 13 of 18 results were over 8,000 tokens, most at
    11,000–15,500. For the same 50-row SBIR page:
    - indented JSON: 13,351 tokens;
    - compact JSON: 10,744;
    - compact, with the list as a table (column names once): 6,909. TechPort's
      page as a table: 7,307.

    Claude read 16- and 14-column tables without error: 13 lookups, nested
    lists included.

    **Agreed 2026-09-30:**
    - Every tool returns compact JSON. Lists of records become tables, chosen
      per tool, and only when every row has the same fields.
    - Every list result stops at 12,000 characters of rows, at least one row,
      with a note saying how to continue.
    - `techport_get_project` lists each file once, with its status. Until
      then, the project's file list and the attachment report both described
      the same files.

13. ~~TechPort projects that list hundreds of files.~~ **Built 2026-09-30**;
    see "Result sizes (all tools)". Found 2026-09-30: four
    projects list 219–639 files. `techport_get_project` sends 41,000–120,000
    characters for them (see "Result sizes (all tools)").

    **Decided 2026-09-30:**
    - One row per file Claude can fetch (PDFs and images up to 20 MB), most
      useful first, under the list size limit. Any left out are named by
      file id.
    - Files the plugin can never fetch are counted by reason, with the
      project page link.
    - No download just to learn a file's type when its extension already
      shows it isn't a PDF or image.
14. ~~How many files one result attaches.~~ **Built 2026-09-30.** Claude
    Code's note of about 345 characters per attached file isn't bounded by the
    11 MB budget. **Decided 2026-09-30:** at most 20 files per result, in both
    `techport_get_project` and `techport_get_document`. The rest are listed
    to fetch in a later call.
15. ~~EDGAR search hits carry no text.~~ **Built 2026-09-30, a partial
    fix.** SEC's full-text search returns no snippets, so judging a hit meant
    opening the filing: the Phase III test took 58 turns and nearly four
    minutes.
    - `edgar_search` takes `passages`: for the first N hits (up to 10), the
      passage that best shows the search (a phrase counts twice a single
      word) and how often its words appear. Off by default. Ten passages
      cost 2,964 tokens (6,494 characters) and a few seconds.
    - Found while testing it, and fixed: a phrase's words now match across
      punctuation, as SEC's search does (Joby's "(SBIR) Phase III" had been
      missed; this applies to NTRS's `find` too); and PDFs, which SEC's
      search indexes but the plugin can't read, are named with their link
      instead of being read as garbage, here and in `edgar_read_filing`.
    - **Re-run of the same question: 45 turns and 202 s,** against 58 and
      227 s, with the same conclusion; all 22 claims checked are in the tool
      results. Claude used passages in every search but still opened about
      27 filings: hits beyond the ten with passages, and checks of a detail
      such as the awarding agency.
    - Further steps, *not decided:* a higher cap, or a passage per search
      term.

## Next steps

1. All six planned sources are built, and open question 15 has a partial
   fix. Release: name, licence, history plan and author line are decided,
   Linux is tested; Windows testing is left (open question 1). The local
   working log (not published) has where work stands.
