---
description: Check that each of the plugin's data sources answers, after a fresh install. Add "sbir" to include SBIR (first use downloads about 395 MB).
argument-hint: "[sbir]"
---

Check that the agent-techport-sources plugin works on this machine: one small
request to each source, then a table of the results. Don't use any other tools
or the web, and don't retry a failed call with different arguments: the point
is to see what fails as installed.

Arguments: "$ARGUMENTS". Include the SBIR check only if they contain "sbir":
SBIR's first use downloads about 395 MB (the SBIR.gov award file, built into a
local database of about 0.7 GB), so it is left out unless asked for.

Run these calls, each exactly as written:

1. TechPort: `techport_programs` with `program_id` 72. Pass if the program's
   acronym is FO (Flight Opportunities).
2. NTRS: `ntrs_search` with `query` "regolith" and `limit` 1. Pass if `total`
   is more than 0.
3. USAspending: `usaspending_search_awards` with `award_ids` ["NNX09CB40C"]
   and `limit` 1. Pass if `total` is at least 1 and the recipient is ORBITAL
   TECHNOLOGIES CORPORATION.
4. NASA Technology Transfer: `techtransfer_search` with `query` "regolith"
   and `limit` 1. Pass if `total` is more than 0.
5. SEC EDGAR: `edgar_company` with `company` "SPCE" and `limit` 1. Pass if
   the CIK is 1706946 (Virgin Galactic).
6. SBIR, only if asked for: `sbir_aggregate` with `group_by` "year",
   `agency` "NASA", `year_from` 2020 and `year_to` 2020. Pass if it returns
   one group for 2020 with awards counted. Otherwise mark it "skipped
   (add sbir to include it)".

Then show one table: source, pass / fail / skipped, and for a failure the
error in a few words. Below it, only for failures, say what the error points
to, using these:

- A tool isn't available at all: the plugin's servers didn't start. Run
  `/mcp` and look at `plugin:agent-techport-sources:*`. A server that failed
  to start usually means uv is missing, or PyPI couldn't be reached to
  install the Python packages (the plugin's README, "Requirements").
- "certificate verify failed" or another certificate error: the machine
  doesn't trust the source's certificate. The plugin checks certificates
  against the operating system's trust store; on a managed computer, the IT
  department's network settings may be involved.
- A timeout or "not responding": the source's host can't be reached from
  this network, or is down. The hosts are listed in the README, "What leaves
  your machine".
- EDGAR says the name or email isn't set: set them with
  `/plugin configure agent-techport-sources@agent-techport-sources`, then
  restart Claude Code.

If everything passed, say so in one line, and that the plugin is ready.
