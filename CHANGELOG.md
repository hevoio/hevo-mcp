# Changelog

All notable changes to the Hevo MCP server. The version is the one in `pyproject.toml` and in every
plugin manifest; a `v<version>` tag publishes that version to GitHub and PyPI.

## 1.0.0

First release.

**What it is.** An MCP server that gives AI agents safe access to Hevo through Hevo's public APIs, with
the user's own API key: Edge pipelines and Destinations (Edge API v2), Standard pipelines, objects, event
types, schema mappings, transformations, Destinations, Models and Workflows (Public API v2.0), and the
team's users and OAuth accounts. 86 endpoints in 22 tools, plus `list_endpoints`, `get_schema` and
`hevo_whoami`. Edge tools carry plain names (`pipelines_read`, `list_pipelines`); Standard tools carry a
`standard_` prefix (`standard_pipelines_read`, `standard_list_pipelines`).

**How it runs.** Locally over stdio, started by the agent: the Claude Code and Codex plugins (`hevo-data`)
or the PyPI package (`uvx hevo-mcp`) for any other MCP client. The same package runs as a shared HTTP
server where each caller sends their own key.

**Setup.** One key file, `~/.hevo/hevo-key.json` (or `hevo-key.json` under `HEVO_KEYS_DIR`), readable by
its owner only. The agent asks for the Hevo region once and passes it on every call (eight regions, including `eu2` for Edge Pipelines only and `us-gcp` for Standard Pipelines only). No key or region is read
from environment variables.

**Safety.** Read-only by default: changes need `HEVO_MCP_DEFAULT_SCOPE=write` or `delete` (the
`X-Hevo-Scope` header on a shared server). Every delete, configuration change, destructive action, data
load, enable, resume, role change and OAuth-account removal is shown to the user in their MCP client and
sent only after they approve; creates don't ask, and no setting skips the prompt. A bulk action is one call with `batch` and one approval prompt. A
Standard pipeline delete must say whether the Destination tables go too (`dropResource`). Request bodies and
query parameters are validated against the endpoint schema before anything else. Source and Destination passwords go in as
`$secret_file:<name>` references to files in `~/.hevo/secrets`, never through the chat. Results have
secrets redacted and hidden characters removed. No endpoint returns pipeline records, so pipeline data
stays in the customer's Hevo region.

**Skill.** The `hevo-pipelines` skill ships with the plugins and is served by the server as `skill://`
resources and as six workflow prompts: pipeline health report, why a pipeline is failing, inactive
pipelines and Destinations, which pipelines replicate a table, event volume over time, and a workspace
summary.

**Operations.** Shaped errors with a `request_id`, progress updates during long calls, retries on reads
only, a dependency-drift report and a newer-release notice in `hevo_whoami`, and JSON logs on standard
error. The server sends data only to Hevo's public region hosts and, once a day at most, one anonymous
version check to PyPI.
