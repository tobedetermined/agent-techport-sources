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

## Releasing

Day-to-day work happens on a local `main` with its full history. The public
repository (`github.com/tobedetermined/agent-techport-sources`) gets one
commit per release, on the local branch `public`, which is the only thing
ever pushed. No remote is configured, so nothing is pushed by accident.

1. On `main`: bump the version in `plugin.json` and `pyproject.toml`
   (`uv lock --offline` in the plugin folder), and the status line of
   `docs/design.md`. Run the tests with the plugin's Python, with
   `LIVE_TESTS=1`, and with a Python 3.11+ that has no MCP SDK; run
   `claude plugin validate .`. Commit.
2. Build the release commit on `public`: `main`'s tree without `log.md`,
   with the current `public` as its parent, authored
   `Alexander van Dijk <3953821+tobedetermined@users.noreply.github.com>`
   (author and committer), and ending with
   `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. The message
   says what changed since the last release.
   ```
   export GIT_INDEX_FILE=$(mktemp -u)
   git read-tree main && git rm -q --cached log.md
   TREE=$(git write-tree); unset GIT_INDEX_FILE
   git branch -f public $(git commit-tree $TREE -p public -F message.txt)   # with GIT_AUTHOR_*/GIT_COMMITTER_* set
   ```
3. Check it from a fresh clone of `public`: no local paths, private
   addresses or internal project names (`git grep`), `claude plugin validate .`,
   and the tests after `uv sync --frozen`.
4. Push only when Alexander says to:
   `git push https://github.com/tobedetermined/agent-techport-sources.git public:main`.
   Then tag `main` locally as `release-<version>` (tags aren't pushed).
