# Contributing

## Layout

| Path | What it holds |
| --- | --- |
| `src/hevo_mcp_server/` | The server: `tools.py` (tools and dispatch), `auth.py` (key file, regions), `client.py` (calls to Hevo), `settings.py` (every setting), `approval.py`, `guards.py`, `validation.py`, `secrets.py`. |
| `src/hevo_mcp_server/manifest/` | The endpoint catalog, generated from `openapi/` and `endpoint_overrides.json` by `build_manifest.py`. Never edited by hand. |
| `skills/hevo-pipelines/` | The skill the plugins read and the package ships. |
| `.claude-plugin/`, `.codex-plugin/`, `.agents/` | Plugin manifests for Claude Code and Codex. |
| `config/hevo_mcp.toml` | Every setting with its default. |
| `tests/` | Tests against a mocked Hevo API; no key needed. |

## Working on it

```sh
uv sync                                   # environment
uv run pytest -q                          # tests
uvx ruff check .                          # lint
uv run python build_manifest.py --check   # the manifest matches the specs
claude --plugin-dir .                     # try the plugin from this folder in Claude Code
```

The plugin marketplaces install a release tag (`ref` in `.claude-plugin/marketplace.json` and
`.agents/plugins/marketplace.json`, set by `scripts/set_version.py`), not the branch you're on, so use
`--plugin-dir` to try local changes.

- To change an endpoint, edit `openapi/` or `endpoint_overrides.json` and run `build_manifest.py`; CI fails if
  the committed manifest is stale.
- Every endpoint needs a `records` entry in `endpoint_overrides.json`. Endpoints that return pipeline records
  are not served: the server returns configuration and metadata only.
- Read-only is the default, approval prompts can't be bypassed, and the server never prints or logs a key.
- A new setting goes in `settings.py` and `config/hevo_mcp.toml`; a test keeps the two equal.
- The skill names only tools and endpoints that exist; a test checks it.

## Pull requests

- Name branches `feature/<description>` or `fix/<description>`.
- Write PR titles as `type(scope): description`, for example `fix(secrets): trim whitespace around a file's
  value`. PRs are squash-merged, so the title becomes the commit message.
- Run `uv run pytest -q`, `uvx ruff check .` and `uv run python build_manifest.py --check` before opening
  a PR. CI runs the same, plus a secret scan, a dependency audit and an install of the built package.
- Don't put keys, hostnames other than Hevo's public regions, or personal details in code, tests, commit
  messages or PR descriptions.

## Releasing

`uv run python scripts/set_version.py <version>` sets the version everywhere it appears. Merge, tag the
merged commit `v<version>` and push the tag; `.github/workflows/release.yml` builds, attests and publishes
the release to GitHub and, after approval, to PyPI.
