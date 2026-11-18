# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

The Hevo MCP server: it gives AI agents the tools to inspect and operate Hevo pipelines with the user's
own API key: Edge pipelines through the Edge API v2 (plain names: `pipelines_read`, `list_pipelines`),
Standard pipelines through the Public API v2.0 (`standard_` prefix: `standard_pipelines_read`,
`standard_list_pipelines`, because both products have pipelines with separate ids), and the team's users
and OAuth accounts, shared by both products, as their own `accounts_*` tools (`openapi/account.json` holds
only the `/accounts/` paths and `openapi/standard.json` excludes them, so each ships once). It runs locally
over stdio or as a shared HTTP server. The repository also holds a skill (`skills/hevo-pipelines/`) and plugin manifests for several agents.
`README.md` is the user-facing guide.

## Layout

| Path | Contents |
| --- | --- |
| `src/hevo_mcp_server/` | The server: `tools.py` (tools and dispatch), `auth.py` (keys, regions), `client.py` (calls to Hevo), `settings.py` (every setting), `observability.py`, `approval.py`, `guards.py`, `validation.py` (body vs endpoint schema), `secrets.py` (`$secret_file:` references) |
| `src/hevo_mcp_server/manifest/` | Generated endpoint catalog. Never edit by hand |
| `openapi/`, `endpoint_overrides.json`, `build_manifest.py` | Specs from Hevo's public API docs, hand-written overrides, and the generator |
| `skills/` | The `hevo-pipelines` skill: read by the plugins, shipped in the wheel as `hevo_mcp_server.skills`, served as `skill://` resources. Edited here; a test checks it names only endpoints that exist |
| `.claude-plugin/`, `.codex-plugin/`, `.agents/` | Plugin manifests for Claude Code and Codex; kept in step by `tests/test_plugin_manifests.py` |
| `config/hevo_mcp.toml` | Every setting with its default; a test keeps it equal to `settings.py` |

## Commands

```sh
uv sync                                   # environment
uv run pytest -q                          # tests (mocked Hevo; no key needed)
uvx ruff check .                          # lint
uv run python build_manifest.py --check   # manifest matches the specs
uv run python build_manifest.py --fetch   # pull current specs, then rebuild
claude --plugin-dir .                     # try local changes: the marketplace installs the release tag, not this folder
```

## Rules

- **Never print, log or commit keys.** The key file is `~/.hevo/hevo-key.json` (or `hevo-key.json` in
  `HEVO_KEYS_DIR`); read it only through the server's own code paths, and never `cat` it. The server reads
  no key from a variable.
- **This repository is public.** No private hostnames, environment names, people, ticket links or
  infrastructure details in tracked files, tests, commit messages or PR descriptions. The server knows only
  Hevo's public regions; there is no way to point it at another environment.
- **Read-only by default** must stay the default; writes need an explicit scope.
- **No endpoint that returns pipeline records.** GDPR data residency: Hevo promises that a customer's
  pipeline data never leaves the region they choose, so the server returns configuration and metadata only.
  Every endpoint needs a `records` entry in `endpoint_overrides.json` (`true` only with `exclude: true`);
  the build fails for an unclassified endpoint, so a spec sync can't ship one unreviewed. Don't add an
  endpoint that returns records without a data-residency review.
- **Secret references** (`$secret_file:`) resolve only under secret-named keys, only on
  a local server, and only after the user approved the request. Don't widen any of the three.
- **Approval can't be skipped:** endpoints with `approval` other than `never` (manifest) always ask the user
  through MCP elicitation (`approval.py`). Never add a setting, scope or argument that bypasses it; deletes
  always ask, and new write endpoints ask unless an override says `never`.
- **Keep versions in step:** `pyproject.toml`, every plugin manifest and the PyPI pins share one version
  (`scripts/set_version.py`); release tags are `v<version>`, and a tag publishes that version to PyPI.
- **Regenerate, don't edit, the manifest:** change `openapi/` or `endpoint_overrides.json`, then run
  `build_manifest.py`.
- **Branches and pull requests** follow `CONTRIBUTING.md`: branches `feature/<description>` or
  `fix/<description>`, PR titles `type(scope): description`, squash-merged (the title becomes the commit
  message). No ticket ids, hostnames or people in branch names, commit messages or PR descriptions.
- Tests must not depend on the developer's shell: `tests/conftest.py` clears `HEVO_*` variables and points
  the keys folder at an empty directory.
