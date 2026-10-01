# CLAUDE.md: agent-techport-sources

Development instructions for Agent TechPort Sources. This repo
is public. Write every file as if NASA colleagues and outside users
will read it.

**Start here:** `docs/design.md` has the decisions, the evidence behind them,
and the open questions. A local working log (`log.md`, not published), if
you have one, says where the last session left off and what comes next.

## Boundary

- Sources are public, or were provided by NASA. Bring in no private material,
  no personal context from other folders or sessions, and nothing specific to
  one person's machine (usernames, hostnames, local paths).
- The plugin must not route anything through third-party servers or send
  telemetry. The only network calls go to the source hosts, plus PyPI at
  install time, and Astral (`releases.astral.sh`) only if uv has to download
  Python. Document every host in the README.
- The public repository started from one squashed commit; the full history
  stays local and is never pushed. Commit messages, like files, are public.

## How to work

- **Discuss method before executing.** Talk steps through first. Don't start
  bulk pulls, scaffolding or large changes on your own initiative.
- **Verify, don't assume.** Probe the endpoint, load the file, count the rows.
  In docs, label anything unmeasured as *assumed*.
- Keep the dependency footprint minimal. Every package is something a NASA
  reviewer has to audit.
- `claude plugin validate .` must pass before any commit that touches the
  manifests.
