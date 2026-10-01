# Agent TechPort Sources

An independent project, not affiliated with or endorsed by NASA, the US
government or Anthropic.

A Claude Code plugin that gives Claude grounded access to public data on NASA
space technology: what NASA funded, what came of it, and who did the work.
Claude answers from the data itself. Counts and totals come from the tools,
not from Claude's memory, and every answer says where its data came from and
how current it is.

It runs entirely on your machine, as local MCP servers that call the
government sources directly. There is no hosted relay, no third-party
service and no telemetry.

**Status: early development (version 0.1.0).** All six planned sources
work.

| Source | What it covers | Status |
|---|---|---|
| **SBIR** (SBIR.gov) | Every SBIR/STTR award from all agencies, 1983 on | Working |
| **TechPort** (techport.nasa.gov) | About 21,000 NASA technology projects, with documents | Working |
| **USAspending** (usaspending.gov) | Federal contracts, grants and the companies behind them, fiscal year 2008 on | Working |
| **NTRS** (ntrs.nasa.gov) | About 650,000 NASA technical reports, papers and articles, with documents | Working |
| **NASA Technology Transfer** (technology.nasa.gov) | NASA patents available for licensing, the software catalog, and Spinoff stories | Working |
| **SEC EDGAR** (sec.gov) | Companies' filings, full-text search of them from 2001, and reported financials | Working |

## Requirements

- **Claude Code.**
- **[uv](https://docs.astral.sh/uv/)**, the Python package manager. Use its
  standalone installer, which needs no admin rights:
  `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS and Linux), or see
  the uv docs for Windows. uv finds or installs Python 3.11+ itself.
- **Outbound HTTPS** to the hosts listed under "What leaves your machine".
- **For the SEC EDGAR tools only, your name and email.** SEC requires a
  contact with every request; see "SEC EDGAR" below.
- **Disk space:**
  - SBIR: about 0.7 GB, rising briefly to about 1.8 GB during its monthly
    refresh.
  - TechPort: about 0.25 GB, rising briefly to about 0.6 GB during its daily
    refresh.
  - USAspending, NTRS, NASA Technology Transfer and SEC EDGAR keep no local
    copy.

Platforms: tested on macOS. On Linux, in Debian 12 containers, the full test
suite passes (ARM64 and x86-64) and every host the plugin contacts connects
(checked from ARM64); Claude Code itself hasn't been run on Linux with the
plugin. Windows is intended and the code allows for it,
but it has **not yet been tested on a Windows machine**.

## Install

From GitHub:

```
claude plugin marketplace add tobedetermined/agent-techport-sources
claude plugin install agent-techport-sources@agent-techport-sources
```

Or from a local copy of the repository (Claude Code then runs the plugin from
that folder, so changes there take effect on the next start):

```
claude plugin marketplace add /path/to/agent-techport-sources
claude plugin install agent-techport-sources@agent-techport-sources
```

The first time a server starts, uv downloads its Python packages (the official
MCP SDK, `truststore`, and what they depend on, 30 packages pinned in
`uv.lock`). This took
about 1.3 s on a fast connection.

## What leaves your machine

| Host | When | What is sent |
|---|---|---|
| `data.www.sbir.gov` | SBIR server start (a date check), and a download when SBIR.gov publishes a new file, about monthly | Plain HTTP requests for the public award file |
| `techport.nasa.gov` | Most TechPort questions; a full project list at most once a day | Your search words and filters, project and file ids, and text you ask TechPort's classifier to label |
| `api.usaspending.gov` | Every USAspending question | Your search words and filters, award numbers and company UEIs |
| `ntrs.nasa.gov` | Every NTRS question | Your search words and filters, and record ids; the files you ask for are downloaded from it |
| `technology.nasa.gov` | Every NASA Technology Transfer question | Your search words, each sent on its own, and reference numbers; for one patent, its web page is downloaded |
| `www.sec.gov`, `data.sec.gov`, `efts.sec.gov` | Every SEC EDGAR question | Your search words, company names, tickers and ids, and filing numbers; and, in every request, the contact you set for SEC |
| `pypi.org`, `files.pythonhosted.org` | Install and update only | Package downloads by uv |
| `releases.astral.sh` | Install only, and only if no Python 3.11+ is found | uv downloading Python. To prevent it, install Python 3.11+ yourself first, or set `UV_PYTHON_DOWNLOADS=never` |

Certificates are checked against your operating system's trust store, as your
browser does (on a managed Mac, the list your IT department manages), using
the `truststore` package. Certificate checking is never switched off.

Every request identifies itself as `agent-techport-sources/0.1.0 (Claude Code
plugin)`. The plugin's HTTP code refuses any host not on its server's list,
redirects included.

**Anthropic is still in the loop.** The plugin adds no party beyond the hosts
above. But Claude Code sends your questions and the tools' results to
Anthropic's model, as it does for any tool. Whether Claude Code is approved
for your work is a separate question from whether this plugin is clean.

**No telemetry.** The MCP SDK depends on `opentelemetry-api`. On its own that
package records and sends nothing; data would only leave through an
OpenTelemetry SDK and exporter, and this plugin includes neither.

## What it can do

### SBIR: `sbir_*` tools

- **Data:** the SBIR.gov public award file (all 42 columns, about 220,000
  awards) is downloaded on first use and held locally in SQLite.
- **Refresh:** a new download happens when SBIR.gov publishes a new file,
  about monthly, and once after a plugin update that changes how the file is
  loaded. If SBIR.gov is unreachable, the last copy keeps working and answers
  say they may be out of date. The exception is that first start after such
  an update, when the SBIR tools wait until SBIR.gov can be reached.
- **Tools:**
  - `sbir_search_awards`: filter by agency, year, program, phase, company,
    UEI, state or keywords; sort by relevance, date or amount.
  - `sbir_get_award`: one award in full.
  - `sbir_company`: a company by UEI, with its awards across agencies.
  - `sbir_aggregate`: counts and dollar totals by year, phase, program,
    agency, state or company (ranked).
  - `sbir_query`: one read-only SQL query, for what the other four can't
    answer, such as medians, shares, or how many Phase I winners went on to
    Phase II. Claude shows the SQL it ran. Contact details are never
    available through it, and a query is stopped after 10 seconds.
- **Caveats in every answer:** the current year may be incomplete, and time
  filters use award year, because about half the awards have no award date.

### TechPort: `techport_*` tools

- **Live on every call:** keyword search, single projects, programs,
  organisations, capability areas and shortfalls, funding opportunities,
  TechPort's own TREX technology classifier, and what's new.
- **From a local copy refreshed daily:** counts and rankings, listings without
  a keyword, contact search and batch lookups. These answers state the copy's
  date. The copy is built the first time such a question is asked, which
  takes about 15 s.
- **Documents:** getting one project also brings its library files (briefing
  charts, final reports, images), up to about 11 MB per answer. See "Limits".
- **No funding amounts:** TechPort's public API has none. For dollars, Claude
  is pointed to the SBIR tools.

### USAspending: `usaspending_*` tools

- **Data:** live from the official USAspending.gov API, which updates daily.
  It covers fiscal year 2008 on. There's no local copy.
- **Tools:**
  - `usaspending_search_awards`: contracts, grants or IDVs, filtered by
    agency, company or UEI, keywords, award numbers (the link from an SBIR
    award), fiscal years, industry and product codes, grant program or
    state. One option finds SBIR/STTR Phase III follow-on contracts.
  - `usaspending_get_award`: one award in full, with its latest transactions
    and largest subawards if asked.
  - `usaspending_recipient`: a company by UEI, with the money it received by
    agency and by year.
  - `usaspending_aggregate`: totals and rankings. By default, money obligated
    in a period, grouped by company, agency, code, state or year. Or the
    totals of whole awards, such as all Phase III contracts by company.
- **What answers say:**
  - A search keeps awards with any payment in the years asked, unless you
    ask for awards signed in them.
  - An award's amount is its lifetime total; totals for a period count only
    that period's obligations.
  - Phase III contracts are found only when their description says so.

### NTRS: `ntrs_*` tools

- **Data:** live from the NASA Technical Reports Server's API: about 650,000
  public records from the 1910s on. They include technical reports and
  memoranda, conference papers, presentations, and journal articles that
  publishers share through CHORUS. There's no local copy.
- **Tools:**
  - `ntrs_search`: words and phrases (with or, not and prefixes), filtered by
    center, report type, subject, author, organization, keyword, report or
    funding number, and publication years; sorted by relevance or date.
  - `ntrs_get_record`: one record in full, with its PDFs attached.
  - `ntrs_read_text`: the text NTRS extracted from a file, part by part, or
    only the passages that mention given words. It reads Word and PowerPoint
    files and PDFs too large to attach, and finds a figure or section in a
    long report.
  - `ntrs_aggregate`: counts by center, report type, subject, year, author,
    organization, keyword or funding number.
- **What answers say:**
  - Search covers each record's title, abstract, keywords and authors, not
    the text of its files.
  - Names appear in several forms ("Jason Hartwig", "Hartwig, Jason W."),
    and a filter matches one form exactly. Claude looks up the forms in use
    and searches them together.
  - 59% of all records, mostly older ones, have no NASA center, and about 9%
    have no publication date. Answers say when a filter leaves these out.

### NASA Technology Transfer: `techtransfer_*` tools

- **Data:** live from the Technology Transfer portal's API: NASA's patents
  available for licensing, its software catalog, and Spinoff stories of NASA
  technology in commercial products. There's no local copy.
- **Tools:**
  - `techtransfer_search`: patents, software and spinoffs by search words,
    with center and category filters. Every result counts its matches by
    kind, center and category.
  - `techtransfer_get`: one record by its number. For a patent, it also
    reads the patent's page for benefits, applications, NASA case numbers,
    US patent numbers and papers. The page's scripts are never run.
- **What answers say:**
  - The API can't list a whole catalog, so counts cover what one search
    finds. Each word is searched on its own and all must match, unless you
    ask for any. Common words such as "and" are left out, because the API
    under-matches them.
  - Spinoff searches stop at 1,000 matches per word.
  - The API gives no dates: no patent, release or spinoff year.

### SEC EDGAR: `edgar_*` tools

- **Data:** live from SEC's EDGAR. There's no local copy.
- **Your contact:** SEC requires every client to identify itself with a name
  and email. The plugin asks for this as an optional setting, "SEC EDGAR
  contact", when you enable it. You can change it later in `/config`, under
  the plugin's options. It is stored in your Claude Code settings and sent
  only to SEC, in each request's User-Agent header. Leave it empty if you
  won't use EDGAR: the EDGAR tools then say how to set it, and send
  nothing.
- **Tools:**
  - `edgar_company`: a company by name, ticker or SEC id: its profile and
    recent filings by form.
  - `edgar_search`: full-text search of filings from 2001 on, with counts by
    company, form and state. It can add, for the top hits, the passage that
    best shows the search words.
  - `edgar_financials`: reported revenue, R&D, operating and net income,
    cash and total assets, by year.
  - `edgar_read_filing`: a filing's text, by part or as the passages that
    mention given words. PDFs can't be read as text; their link is given.
- **What answers say:**
  - Most SBIR companies are private and file little or nothing with the SEC.
  - EDGAR has no UEI, so companies are linked to SBIR and USAspending by
    name or ticker.
  - Financial figures are SEC's calendar years, in US dollars, and exist
    only for companies that tag their reports.
- **Rate:** SEC allows 10 requests a second; the plugin stays under 7.

### Contact details

Names and roles of people (principal investigators, project managers) are
shown by default. Emails, phone numbers, titles and ORCIDs appear only when
you ask for them.

## Where data is stored

- **Downloaded data:** Claude Code's plugin data folder,
  `~/.claude/plugins/data/<plugin id>/`, with `sbir/` and `techport/` inside.
  Claude Code deletes it when you uninstall the plugin.
- **Your SEC contact,** if you set one: in your Claude Code user settings
  (under `pluginConfigs`), not in the plugin's folder.
- **Oversized documents:** a TechPort or NTRS file too large to attach
  (11–20 MB) is saved under `techport/files/` or `ntrs/files/` there, and the
  plugin deletes these after 30 days.
- **Documents Claude reads** are kept by Claude Code with the conversation,
  in the session's `tool-results` folder under `~/.claude/projects/`. The
  page images Claude Code makes whenever it reads any PDF go there too.
  - Claude Code deletes old sessions after `cleanupPeriodDays` days (a Claude
    Code setting); lower it to keep less.
  - Deleting the `tool-results` folders of finished sessions by hand is also
    safe.
  - The plugin never touches Claude Code's own folders.

## Limits

- **Document size:** Claude Code drops a tool result larger than about 16 MiB.
  That was measured, not documented; one result holds at most about 11 MB of
  attached files. So:
  - A TechPort project's files are attached most useful first. The rest are
    listed, and Claude fetches them in further calls. An NTRS record's PDFs
    are attached the same way.
  - A single file of 11–20 MB is saved locally instead. Claude Code will ask
    your permission before reading it.
  - Files over 20 MB are given as a link only. For NTRS, Claude can still
    read the text NTRS extracted from them.
  - Documents rely on Claude Code saving attached PDFs to a file for the model
    to read, which is how version 2.1.286 behaves.
- **Reading has limits too.** Each PDF page Claude looks at costs about
  1,500 tokens (measured), so a project with dozens of long documents can
  hold more than one conversation can read. Claude is told to start with the
  briefing chart and final report, to find a figure or section in an NTRS
  report through its text before opening pages, and to say which documents
  it has and hasn't opened.
- **Result size:** results come as compact tables. Every list stops at a
  size limit, with a note saying how to get the rest, and Claude pages on its
  own. Every worst case measured in Claude Code is under Claude Code's
  10,000-token warning. Single TechPort projects with very long descriptions
  come closest, at about 9,900 tokens.
  - A TechPort project lists the files Claude can fetch (PDFs and images up
    to 20 MB). Other files, such as videos and large data sets, are counted
    with a link to the project page. One result attaches at most 20 files.
- **Data quirks:** the tools handle the known quirks of each source and flag
  them in answers. They are documented in the repository's `docs/design.md`.

## Licence

Written by Claude Opus 5.5 (Anthropic) with Alexander van Dijk: Claude wrote
the code, tests and documentation in Claude Code; Alexander set the direction
and made the decisions.

Copyright 2026 Alexander van Dijk. Licensed under the Apache License, Version
2.0; see `LICENSE` in this folder. The 30 Python packages it installs are all
under permissive licences (MIT, BSD-3-Clause, Apache-2.0, MIT-0 and the PSF
licence).
