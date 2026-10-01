# agent-techport-sources

**Agent TechPort Sources** is a Claude Code plugin: the public data sources
for Agent TechPort. It gives grounded access to public data on NASA space
technology, through local MCP servers that call the government sources
directly.

An independent project, not affiliated with or endorsed by NASA, the US
government or Anthropic.

Written by Claude Opus 5.5 (Anthropic) with Alexander van Dijk: Claude wrote
the code, tests and documentation in Claude Code; Alexander set the direction
and made the decisions.

**Users:** see [`plugins/agent-techport-sources/README.md`](plugins/agent-techport-sources/README.md)
for what it does, how to install it, and exactly what leaves your machine.

## What's in this repository

| Path | What it is | Installed for users |
|---|---|---|
| `.claude-plugin/marketplace.json` | The repo is a Claude Code marketplace with one plugin | (catalogue only) |
| `plugins/agent-techport-sources/` | The plugin: manifest, MCP servers, the `/agent-techport-sources:selfcheck` command, `pyproject.toml`, `uv.lock` | **yes, this folder only** |
| `docs/design.md` | Decisions, the evidence behind them, measurements, open questions | no |
| `tests/` | Tests (standard-library `unittest`) | no |
| `research/` | One-off scripts behind the design note's numbers | no |

Claude Code copies only the plugin folder when installing, so docs, tests and
research never reach users. The licence (Apache-2.0) is in `LICENSE`, and a
copy is inside the plugin folder so installed copies carry it.

## Development

Start with `docs/design.md`. Then:

```
# Tests that need only Python 3.11+ (tests that need the MCP SDK are skipped)
python3 -m unittest discover -s tests -t .

# All tests, including the MCP servers over stdio (needs uv)
cd plugins/agent-techport-sources && uv sync --frozen && cd -
plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .

# Also check the SBIR loader and tools against the real SBIR file
curl -o /tmp/award_data.csv https://data.www.sbir.gov/mod_awarddatapublic/award_data.csv
SBIR_REAL_CSV=/tmp/award_data.csv plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .

# Also check live connections to the real hosts (network needed)
LIVE_TESTS=1 plugins/agent-techport-sources/.venv/bin/python -m unittest discover -s tests -t .

# Must pass before any commit that touches the manifests
claude plugin validate .

# Try the plugin in Claude Code without installing it
claude --plugin-dir plugins/agent-techport-sources
```

Environment variables for testing, read by the servers:

- `SBIR_CSV_PATH`: load this local SBIR CSV instead of downloading.
- `TECHPORT_JSON_PATH`: build the TechPort copy from this saved search
  response instead of downloading.

Rules for contributors are in `.claude/CLAUDE.md`. The main ones:
- Public or NASA-provided sources only.
- No third-party routing and no telemetry.
- Keep dependencies minimal.
- Label anything unmeasured as *assumed*.
