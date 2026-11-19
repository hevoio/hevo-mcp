# Hevo MCP server

Connect AI agents such as Claude Code, Codex, Claude Desktop and Cursor to [Hevo](https://hevodata.com).
Through Hevo's public APIs, the agent can inspect and manage your Edge and Standard pipelines,
Destinations, Models, Workflows, team users and OAuth accounts. Ask it things like "which of my pipelines
need attention?", "why is pipeline 12 failing?" or "pause these 10 objects".

- **Your own key, your own permissions.** The server uses your Hevo API key, so the agent can never do
  more than your Hevo role allows.
- **Read-only by default.** Changes need an explicit setting, and every risky change asks for your
  approval first.
- **Configuration and metadata only.** The server never returns pipeline records, so your data stays in
  your Hevo region.

This repository holds the MCP server (`src/hevo_mcp_server/`), the `hevo-pipelines` skill that teaches
the agent how to use it (`skills/hevo-pipelines/`), and plugins that install both for Claude Code and
Codex.

**Contents:** [Prerequisites](#prerequisites) · [Setup](#setup) · [Allowing changes](#allowing-changes) ·
[Approval prompts](#approval-prompts) · [Passwords for new Sources and Destinations](#passwords-for-new-sources-and-destinations) ·
[Privacy and security](#privacy-and-security) · [Updating](#updating) · [Troubleshooting](#troubleshooting) ·
[Development](#development) · [License](#license)

## Prerequisites

- **A Hevo account** and permission to create an API key. If you can't see **Account → API Keys** in
  Hevo, ask a Team Administrator.
- **Your region:** the subdomain you sign in at. `us.hevodata.com` is `us`. The regions are `us`, `us2`,
  `us-gcp` (Standard Pipelines only), `eu`, `eu2` (Edge Pipelines only), `in`, `asia` and `au`.
- **[uv](https://docs.astral.sh/uv/)**, which installs and runs the server. It downloads Python 3.12
  for the server, so you don't need to install Python yourself.

## Setup

### 1. Install uv

macOS and Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a new terminal afterwards and check that `uvx --version` works.

### 2. Save your Hevo API key

In Hevo, open **Account → API Keys** (`https://<region>.hevodata.com/account/api-keys`), click
**+ New API Key**, then **Download**. The secret is shown only once. Save the downloaded file as
`~/.hevo/hevo-key.json`, readable only by you. The server finds it there by name.

macOS and Linux (the server refuses a key file that other users can read):

```sh
mkdir -p ~/.hevo && chmod 700 ~/.hevo
mv ~/Downloads/hevo-key.json ~/.hevo/hevo-key.json && chmod 600 ~/.hevo/hevo-key.json
```

Windows (PowerShell). The key file is `C:\Users\<you>\.hevo\hevo-key.json`, and these commands leave
only your account with access:

```powershell
New-Item -ItemType Directory -Force "$HOME\.hevo" | Out-Null
icacls "$HOME\.hevo" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
Move-Item "$HOME\Downloads\hevo-key.json" "$HOME\.hevo\hevo-key.json"
icacls "$HOME\.hevo\hevo-key.json" /inheritance:r /grant:r "${env:USERNAME}:F"
```

The key acts with the permissions of the Hevo user who created it. Never paste the key or secret into a
chat with your agent. To keep the key in another folder, set `HEVO_KEYS_DIR` to that folder.

### 3. Add the server to your agent

**Claude Code** (plugin, which also adds the `hevo-pipelines` skill):

```
/plugin marketplace add hevoio/hevo-mcp
/plugin install hevo-data@hevo-data
```

**Codex** (plugin, which also adds the skill):

```sh
codex plugin marketplace add hevoio/hevo-mcp
```

Then run `/plugins` in the Codex CLI, or open Plugins in the ChatGPT desktop app, and install
`hevo-data`.

**Plugins in release 1.0.0:** run `uv python pin --global 3.12` once before installing (any OS). These
plugins don't ask uv for Python 3.12, so uv may pick the newest Python instead, and installing the server can then need a C compiler (on Windows, the Microsoft C++ Build
Tools). The pin makes uv default to Python 3.12 wherever a project doesn't pin its own version.

**Claude Desktop, Cursor, Windsurf and other MCP clients.** Add this server to the client's MCP settings
file (`claude_desktop_config.json`, `~/.cursor/mcp.json`, …):

```json
{
  "mcpServers": {
    "hevo": {
      "command": "uvx",
      "args": ["--from", "hevo-mcp==1.0.0",
               "-c", "https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt",
               "--python", "3.12", "hevo-mcp"]
    }
  }
}
```

- **Claude Desktop** doesn't use your shell's `PATH`: replace `uvx` with its full path, from `which uvx`
  on macOS or `where.exe uvx` on Windows. In the JSON file, write each backslash of a Windows path twice
  (`C:\\Users\\<you>\\.local\\bin\\uvx.exe`).
- **VS Code:** put the same entry under `"servers"` instead of `"mcpServers"`, and add `"type": "stdio"`.
- **Codex without the plugin** (`~/.codex/config.toml`):

  ```toml
  [mcp_servers.hevo]
  command = "uvx"
  args = ["--from", "hevo-mcp==1.0.0",
          "-c", "https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt",
          "--python", "3.12", "hevo-mcp"]
  ```

- **Claude Code without the plugin:**

  ```sh
  claude mcp add -s user hevo \
    -- uvx --from hevo-mcp==1.0.0 -c https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt --python 3.12 hevo-mcp
  ```

`-c` installs exactly the dependency versions this release was tested with. Keep `--python 3.12`: without
it, uv picks the newest Python it can find, and some dependencies may then need a C compiler to install.
To use the skill in a client without plugins, point the agent at
[skills/hevo-pipelines/SKILL.md](skills/hevo-pipelines/SKILL.md), for example as a Cursor rule.

### 4. Check the connection

Start a new session (or restart the client), and ask the agent:

> run hevo_whoami for region us

Use your own region. It reports the key file in use and whether Hevo accepts it. From then on, the agent
asks for your region if it doesn't know it, and names it on every call.

## Allowing changes

The server is **read-only by default**: a call that would change something returns `READ_ONLY`, and
nothing reaches Hevo. To allow changes, set `HEVO_MCP_DEFAULT_SCOPE` where the server starts:

| Scope | The agent can |
| --- | --- |
| `read` (default) | List and inspect |
| `write` | Also create, update, pause, resume, run and resync |
| `delete` | Also delete pipelines and Destinations |

- **Plugins:** set the variable in the environment your agent starts from, then start it again:
  `export HEVO_MCP_DEFAULT_SCOPE=write` on macOS and Linux, or `setx HEVO_MCP_DEFAULT_SCOPE write` and a
  new terminal on Windows.
- **Other clients:** add it to the server entry in the client's settings:
  `"env": { "HEVO_MCP_DEFAULT_SCOPE": "write" }`.

Your Hevo role still applies: a scope only limits what the agent can do, it never grants more.

## Approval prompts

Within the scope, risky calls also need **your approval every time**. The server shows the exact request
in your MCP client and sends nothing to Hevo until you approve. The agent can't answer the prompt for
you, and no setting turns it off.

```
Delete Edge Pipeline 352 in the us region?

Deletes this Pipeline. The tables in the Destination are not deleted. This action cannot be undone.

Approve only if you asked your agent for this change. Your agent cannot approve it for you.

Technical details: DELETE /api/v2/pipelines/352 (delete_pipeline)
```

| Needs approval | Doesn't |
| --- | --- |
| Every delete | Reads |
| Configuration changes (Destinations, Sources, pipeline settings, objects, mappings, transformations, Models) | Creating a pipeline, Destination or Model, unless it uses a secret file |
| Cancel, disable, pause, skip, resync, restart, reset | |
| Runs that load data and count toward your Events quota (run now, sync now, load now) | |
| Enabling an Edge pipeline, resuming an object, and changes to team users and OAuth accounts | |

A bulk action, such as "pause these 40 objects", gets one prompt that lists every target.

Your client must support MCP elicitation to show the prompt (Claude Code does). In a client that can't, these calls return `APPROVAL_UNAVAILABLE` and nothing is sent. Claude Code in
print mode (`claude -p`), which includes its VS Code extension, declines prompts without showing them:
make changes from an interactive `claude` session in a terminal.

## Passwords for new Sources and Destinations

Creating a Source or Destination needs its real password, token or key, because Hevo tests the
connection before saving. Don't type it into the chat. Save it as a file in `~/.hevo/secrets/` and give
the agent the file's name. The agent writes `"password": "$secret_file:<name>"` in the request, and the
server puts the file's contents in just before calling Hevo. The chat, the approval prompt and the logs
only ever see the file name.

macOS and Linux:

```sh
mkdir -p ~/.hevo/secrets && chmod 700 ~/.hevo/secrets
read -rs PW && printf '%s' "$PW" > ~/.hevo/secrets/mysql-prod && chmod 600 ~/.hevo/secrets/mysql-prod
```

Windows (PowerShell), after restricting `.hevo` as in [step 2](#2-save-your-hevo-api-key):

```powershell
New-Item -ItemType Directory -Force "$HOME\.hevo\secrets" | Out-Null
$pw = Read-Host -AsSecureString "Value"
[IO.File]::WriteAllText("$HOME\.hevo\secrets\mysql-prod", [Net.NetworkCredential]::new('', $pw).Password)
Remove-Variable pw
```

The file holds only the value: no quotes, no `password=`, no JSON around it. A service-account JSON or a
PEM key is saved as it is. In Windows PowerShell, use `WriteAllText` as above: `>` and `Set-Content` add
characters that would become part of the value.

A secret file can only fill a field that holds a secret (`password`, `token`, `key`, `client_secret`,
`*_token`, …), and every request that uses one asks for your approval, so you see which host the secret
goes to.

## Privacy and security

**What the server reads:** your key file, the one secret file a request names (only after you approved
that request), and the settings file if you pass one. It writes nothing to disk. Logs go to standard
error, where your MCP client shows them, with keys and secrets removed.

**Where it sends data:**

- **Hevo's API**, over HTTPS, in the region you name. Hevo's answers go to your agent, and so to its
  model provider: pipeline and object names, configuration with secrets redacted, and your team members'
  names and email addresses from the account tools.
- **PyPI**, once a day at most, to check for a newer release. The request is anonymous and never carries
  your key.

There is no telemetry or analytics. The server never calls an endpoint that returns pipeline records,
because Hevo promises that pipeline data stays in the region the customer chose.

**What the agent sees:** secrets in results come back as `[redacted]`: password, key and token fields,
every HTTP header value a REST API Source stores (such as `Authorization`), and credentials in URLs (such as
`?api_key=`). Hidden characters and terminal escape codes are removed from everything Hevo returns, so a
pipeline name or error message can't hide instructions.

**What this protects against:** the read-only default, the approval prompts and the redaction stop
mistakes by the model, and calls steered by text it read (an error message that says "delete
everything"). They can't stop an agent that is hostile and can read files on your machine: it could read
the key file and call Hevo itself. So create the key from a Hevo user with the least role that does the
job, and keep the agent away from `~/.hevo`. In Claude Code, add this to `~/.claude/settings.json`:

```json
{ "permissions": { "deny": ["Read(~/.hevo/**)", "Bash(cat ~/.hevo/*)"] } }
```

## Updating

- **Claude Code plugin:** run `claude plugin marketplace update hevo-data`, then
  `claude plugin update hevo-data@hevo-data`, and start a new session.
- **Codex plugin:** run `codex plugin marketplace upgrade hevo-data` and start a new session.
- **Other clients:** change the version in both places in the settings (`hevo-mcp==<version>` and the
  `v<version>` in the `constraints.txt` URL), then restart the client.

`hevo_whoami` tells you when a newer release is available.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| The client shows no Hevo tools, or says the connection closed | Check that `uvx --version` works. In Claude Desktop, use the full path to `uvx`. Restart the client. |
| The server fails to start with a build error (on Windows: "Microsoft Visual C++ 14.0 or greater is required") | uv picked a Python version the dependencies have no prebuilt packages for. Check the command has `--python 3.12`. For a plugin, run `uv python pin --global 3.12`, then restart the agent. |
| `No Hevo API key file at ...` | Save the key as `~/.hevo/hevo-key.json` ([step 2](#2-save-your-hevo-api-key)). The message names the path it looked at. |
| `No region given` | Tell the agent your region: the subdomain you sign in at. |
| `UPSTREAM_UNAUTHORIZED` | Wrong or deleted key. Download a new one. |
| `UPSTREAM_FORBIDDEN` | The Hevo role of the user who created the key doesn't allow this action. |
| `REGION_UNSUPPORTED` | `eu2` has Edge Pipelines only and `us-gcp` has Standard Pipelines only. Use the region that holds them. |
| `READ_ONLY` | Allow changes ([Allowing changes](#allowing-changes)). |
| `APPROVAL_UNAVAILABLE` | Your client can't show approval prompts. Use one that can ([Approval prompts](#approval-prompts)). |
| `SECRET_UNAVAILABLE` | The secret file is missing, empty, readable by other users, or named with a path. The message says which. |

Errors include a `request_id` that also appears in the server's log, which helps support find the call.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the project layout, how the tools are generated from Hevo's
API specs, pull requests and releases.

```sh
uv sync                  # install with dev dependencies
uv run pytest -q         # tests (a mocked Hevo API; no key needed)
uvx ruff check .         # lint
```

All settings, with their defaults, are listed in [config/hevo_mcp.toml](config/hevo_mcp.toml). A plugin
or package install needs none of them.

## License

MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The license covers this code; it doesn't grant
use of the Hevo name or logo beyond describing where the software comes from.
