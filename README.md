# Hevo MCP server

Connect AI agents to [Hevo](https://hevodata.com). Through Hevo's public APIs, an agent can list,
inspect and manage Edge and Standard pipelines and Destinations: "which of my pipelines need attention?",
"why is pipeline 12 failing?", "disable every pipeline loading into this Destination". It also manages
Standard Models, Workflows, event types, schema mappings and transformations, and the team's users and
OAuth accounts. Edge tools carry plain names (`pipelines_read`, `list_pipelines`); Standard tools carry a
`standard_` prefix (`standard_pipelines_read`, `standard_list_pipelines`), because the two products have
separate APIs and separate ids.

This repository holds:

| Part | Where | What it is |
| --- | --- | --- |
| MCP server | `src/hevo_mcp_server/` | The tools. Runs on your machine (stdio) or as a shared service (HTTP). Works with any MCP client. |
| Skill | `skills/hevo-pipelines/` | Hevo know-how for the agent: safety rules, how to find out why a pipeline fails, how to create one. It uses the MCP server's tools; it never calls the API itself. |
| Plugins | `.claude-plugin/`, `.codex-plugin/` + `.agents/plugins/` | The server and the skill in one install for Claude Code and Codex. |

The server stores no keys. It uses your own Hevo API key, so the agent can never do more than your Hevo
role allows, and it is **read-only unless you allow changes**.

**Contents:** [Before you start](#before-you-start) · [Choose how to run it](#choose-how-to-run-it) ·
[1. Plugin for your agent](#1-plugin-for-your-agent) · [2. Run it yourself (stdio)](#2-run-it-yourself-stdio) ·
[3. Host it for your organization (HTTP)](#3-host-it-for-your-organization-http) ·
[Allowing changes](#allowing-changes) · [What the server reads and sends](#what-the-server-reads-and-where-it-sends-data) ·
[Configuration reference](#configuration-reference) · [Tools](#tools) ·
[Troubleshooting](#troubleshooting) · [Development](#development) · [License](#license)

## Before you start

**1. Create a Hevo API key.** Sign in to Hevo, open **Account → API Keys**
(`https://<region>.hevodata.com/account/api-keys`), click **+ New API Key**, then **Download**. The
secret is shown only once. Save the downloaded file as **`~/.hevo/hevo-key.json`**
(`C:\Users\<you>\.hevo\hevo-key.json` on Windows), readable only by you ([Key file](#key-file) has the
commands for macOS, Linux and Windows): the server finds it there by name. The
key works in the region of your Hevo account, which your agent asks you for once and passes on every call.
If you can't see the API Keys page, ask a Team Administrator in your Hevo account.

The key acts with the permissions of the Hevo user who created it. Never paste the key or secret into a
chat with your agent.

**2. Know your region:** the subdomain you sign in at (`us.hevodata.com` is `us`). The agent asks for it
and names it on every call; nothing is configured. It can work in every other region too, with the same
key: one API key covers all of Hevo's production regions.

| Region | Hevo URL |
| --- | --- |
| `us` | `https://us.hevodata.com` |
| `us-gcp` | `https://us-gcp.hevodata.com` (Standard Pipelines only) |
| `us2` | `https://us2.hevodata.com` |
| `eu` | `https://eu.hevodata.com` |
| `eu2` | `https://eu2.hevodata.com` (Edge Pipelines only) |
| `in` | `https://in.hevodata.com` |
| `asia` | `https://asia.hevodata.com` |
| `au` | `https://au.hevodata.com` |

**3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)** for options 1 and 2. It runs
the server and downloads Python 3.12 for it if needed. The
plugins install the server from this repository; the other clients install the `hevo-mcp` package from PyPI.

## Choose how to run it

| Option | Best for | You get |
| --- | --- | --- |
| [1. Plugin for your agent](#1-plugin-for-your-agent) | Claude Code, Codex | MCP server + `hevo-pipelines` skill, one install, updates with the plugin |
| [2. Run it yourself (stdio)](#2-run-it-yourself-stdio) | Any MCP client, full control | MCP server on your machine with your key |
| [3. Host it for your organization (HTTP)](#3-host-it-for-your-organization-http) | Teams | One shared server; each person connects with their own key |

In options 1 and 2 the server runs on your own machine, and your key and data go only from your machine to
Hevo's API.

## 1. Plugin for your agent

Each plugin starts the MCP server for you and adds the `hevo-pipelines` skill. Setup is one file:

| What | Where | Meaning |
| --- | --- | --- |
| Key file | `~/.hevo/hevo-key.json` | The key file downloaded from Hevo, as it is ([Key file](#key-file)) |
| Region | Told to the agent | The agent asks which region your account is in (the subdomain you sign in at) and names it on every call; nothing to configure |
| `HEVO_MCP_DEFAULT_SCOPE` | Optional variable | `write` or `delete` to allow changes ([Allowing changes](#allowing-changes)) |
| `HEVO_KEYS_DIR` | Optional variable | Another folder than `~/.hevo` for the key file and the `secrets/` folder |

After installing, start a new session and ask: "run hevo_whoami for region us" (your region). It reports
the key file in use and whether Hevo accepts it.

### Claude Code

Save the key file, then:

```
/plugin marketplace add hevoio/hevo-mcp
/plugin install hevo-data@hevo-data
```

The skill is `/hevo-data:hevo-pipelines`; Claude also uses it on its own when you ask about Hevo.
`/plugin marketplace update hevo-data` picks up new releases.

### Codex

Set the same variables in the shell you start Codex from, then:

```sh
codex plugin marketplace add hevoio/hevo-mcp
```

Then install `hevo-data` from the `hevo-data` marketplace: run `/plugins` in the Codex CLI, or open Plugins
in the ChatGPT desktop app, and start a new session. Codex runs the server from the matching release on
PyPI and passes it only the variables listed in the plugin. Use the skill as `$hevo-pipelines`, or just
ask about Hevo. `codex plugin marketplace upgrade hevo-data` picks up new releases.

Other agents run the server from the package: see the next section.

## 2. Run it yourself (stdio)

The agent starts the server as a local process and talks to it over standard input and output. There is
no port and no URL. The key comes from the server's environment. Every client below runs the same
command, which installs the server from PyPI ([`hevo-mcp`](https://pypi.org/project/hevo-mcp/)):

```sh
uvx hevo-mcp==1.0.0                     # the release, with its dependencies resolved today
uvx --from hevo-mcp==1.0.0 -c https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt hevo-mcp
                                        # the release exactly as tested: every dependency pinned
```

The second form is what the plugins use and what the examples below show: `-c` points at the release's
`constraints.txt` (in this repository at the `v<version>` tag), so every dependency is the version CI tested.
The first form installs the same server with the dependencies resolved at install time; use it where
raw.githubusercontent.com isn't reachable (a proxy that blocks it). Either way, `hevo_whoami` reports
`dependencies: as_tested` or lists what differs, so you can tell which you have. Use another version for
another release (in both places: `hevo-mcp==<version>` and the `v<version>` tag). From a local clone or a
downloaded wheel, pin with the `constraints.txt` next to it:
`uvx --from /path/to/clone -c /path/to/clone/constraints.txt hevo-mcp`.

**Claude Code**

```sh
claude mcp add -s user hevo \
  -- uvx --from hevo-mcp==1.0.0 -c https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt hevo-mcp
```

`-s user` makes it available in every folder; without it, only in the current project.

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.hevo]
command = "uvx"
args = ["--from", "hevo-mcp==1.0.0",
        "-c", "https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt", "hevo-mcp"]
```

**Claude Desktop, Cursor, Windsurf and most other clients** (`claude_desktop_config.json`,
`~/.cursor/mcp.json`, `~/.gemini/settings.json`, …)

```json
{
  "mcpServers": {
    "hevo": {
      "command": "uvx",
      "args": ["--from", "hevo-mcp==1.0.0",
               "-c", "https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt", "hevo-mcp"]
    }
  }
}
```

**VS Code** (Command Palette → **MCP: Open User Configuration**, or `.vscode/mcp.json` for one project):
the same entry under `"servers"` instead of `"mcpServers"`, with `"type": "stdio"` added.

Claude Desktop doesn't use your shell's `PATH`: give the full path to `uvx` (`which uvx`). Restart the
client after editing its config. To use the skill in a client without plugin support, point the agent at
`skills/hevo-pipelines/SKILL.md` (for example as a Cursor rule or in your instructions file).

**Regions.** There is no default region. Every tool takes a `region` argument (`us`, `us-gcp`, `us2`, `eu`,
`eu2`, `in`, `asia`, `au`: the subdomain you sign in at), the agent asks you once if it doesn't know it, and
one server covers them all, so you can say "list my pipelines in eu". `hevo_whoami` without a region reports
the setup; with one (`region="eu"`) it checks the key there. Two regions carry one product: `eu2` has Edge
Pipelines only and `us-gcp` has Standard Pipelines only, so a Standard call in `eu2` or an Edge call in
`us-gcp` returns `REGION_UNSUPPORTED` instead of reaching Hevo. Team users and OAuth accounts answer in every
region.

### Key file

The key is one file, `~/.hevo/hevo-key.json` (or `hevo-key.json` in `HEVO_KEYS_DIR`): the JSON you download
from **Settings → API Keys** in Hevo, saved as it is. The key works in the region of your Hevo account,
which the agent passes on every call.
On macOS and Linux the server refuses a key file other users on the machine can read:

```sh
mkdir -p ~/.hevo && chmod 700 ~/.hevo
mv ~/Downloads/hevo-key.json ~/.hevo/hevo-key.json && chmod 600 ~/.hevo/hevo-key.json
```

**On Windows** `~` is your user profile folder, so the key file is `C:\Users\<you>\.hevo\hevo-key.json`
(`HEVO_KEYS_DIR` takes a Windows path too, e.g. `D:\keys\hevo`). Windows has no `chmod`, and the server
doesn't check who can read the file there, so restrict it yourself. In PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.hevo" | Out-Null
icacls "$HOME\.hevo" /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F"
Move-Item "$HOME\Downloads\hevo-key.json" "$HOME\.hevo\hevo-key.json"
icacls "$HOME\.hevo\hevo-key.json" /inheritance:r /grant:r "${env:USERNAME}:F"
```

The first `icacls` leaves only you on the folder, so files created in it later (such as `secrets\`) are
yours alone; the second does the same for the key file, which keeps the access list of the folder it was
moved from. Run `icacls "$HOME\.hevo\hevo-key.json"` to check: it should list only your account. File
Explorer won't name a folder `.hevo` directly; use PowerShell as above.

There is no way to pass the key as a variable: client config files are often synced or shared.

**Keep the agent itself away from the key.** The server never shows the key to the agent, but an agent with
its own file tools could still open the key file. In Claude Code, deny that in `~/.claude/settings.json`:

```json
{ "permissions": { "deny": ["Read(~/.hevo/**)", "Bash(cat ~/.hevo/*)"] } }
```

Other agents have similar settings (allowed folders, or approval before reading files).

## 3. Host it for your organization (HTTP)

One server for many people. Nobody's key is stored on it: each person sends their own key with every
request, so every call runs with exactly that person's Hevo permissions.

This mode is for **self-hosting inside your own network and for testing**. The bearer token is the person's
Hevo API key, forwarded to Hevo: it is not an OAuth-protected MCP server in the sense of the MCP
authorization spec (no RFC 9728 resource metadata, no token audience check), so don't expose it on the
internet, and don't connect agents to it from outside your network. The scope comes from a header the client
sends (`X-Hevo-Scope`), so it limits what a well-behaved client asks for, not what a hostile one can do with
the key it already holds. Hevo's own hosted MCP (coming separately) uses OAuth sign-in with consent-derived
scopes instead.

### Run the server

```sh
# from a clone of this repository
uv sync
HEVO_MCP_ALLOWED_REGIONS=us \
  uv run hevo-mcp --transport http --host 0.0.0.0 --port 8000

# or straight from PyPI
HEVO_MCP_ALLOWED_REGIONS=us uvx --from hevo-mcp==1.0.0 -c https://raw.githubusercontent.com/hevoio/hevo-mcp/v1.0.0/constraints.txt hevo-mcp --transport http --host 0.0.0.0 --port 8000
```

Run it under your usual process manager (systemd, a container platform, …) as a non-root user, behind
a load balancer that terminates TLS. Settings come from a config file, and the deployment-specific ones
from environment variables ([Configuration reference](#configuration-reference)).

The MCP endpoint is `<public URL>/mcp`. `GET /health-check` answers without a key and without calling
Hevo: `{"status": "ok", "version": …, "endpoints": …, "regions": …}`.

**Server options** (command line, or the environment variable):

| Option | Env var | Default | Meaning |
| --- | --- | --- | --- |
| `--transport` | `HEVO_MCP_TRANSPORT` | `stdio` | `http` for a shared server |
| `--host` | `HEVO_MCP_HOST` | `127.0.0.1` | Interface to listen on; `0.0.0.0` in a container |
| `--port` | `HEVO_MCP_PORT` | `8000` | Port |
| `--path` | | `/mcp` | MCP endpoint path |
| `--config` | `HEVO_MCP_CONFIG` | none | Settings file (TOML), see [config/hevo_mcp.toml](config/hevo_mcp.toml) |
| `--allowed-hosts` | `HEVO_MCP_ALLOWED_HOSTS` | none | Comma-separated `Host` values to accept (others get 421) |
| `--allowed-origins` | `HEVO_MCP_ALLOWED_ORIGINS` | none | Browser origins allowed to call (others get 403) |
| | `HEVO_MCP_PUBLIC_URL` | `http://<host>:<port>` | Public base URL as people reach it |
| | `HEVO_MCP_ALLOWED_REGIONS` | any | Regions this server serves, e.g. `eu,eu2`; others are refused |

An HTTP server never reads a key file: every caller sends their own key in the `Authorization` header.

### Connect a client

Each person needs the server URL and three headers:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <base64 of api_key:api_secret>` |
| `X-Hevo-Region` | Their region, e.g. `us` (a call's `region` argument can name another one the server serves) |
| `X-Hevo-Scope` | Optional. Omitted: read-only. `write` allows changes, `delete` also allows deletes |

Build the token once (it is the key, so treat it as a secret):

```sh
# macOS / Linux
export HEVO_MCP_TOKEN=$(printf '%s:%s' "$KEY" "$SECRET" | base64 | tr -d '\n')   # KEY and SECRET from the key file
```

```powershell
# Windows PowerShell
$env:HEVO_MCP_TOKEN = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$KEY`:$SECRET"))
```

**Claude Code**

```sh
claude mcp add --transport http hevo https://mcp.example.com/mcp \
  --header "Authorization: Bearer $HEVO_MCP_TOKEN" --header "X-Hevo-Region: us"
# add --header "X-Hevo-Scope: write" to allow changes
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "hevo": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${env:HEVO_MCP_TOKEN}", "X-Hevo-Region": "us" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`; prompts for the token once and stores it securely)

```json
{
  "inputs": [{ "id": "hevo-token", "type": "promptString", "description": "Hevo MCP token", "password": true }],
  "servers": {
    "hevo": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${input:hevo-token}", "X-Hevo-Region": "us" }
    }
  }
}
```

**Claude Desktop** can't send custom headers to a remote server; bridge with `mcp-remote`:

```json
{
  "mcpServers": {
    "hevo": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.example.com/mcp",
               "--header", "Authorization:Bearer ${HEVO_MCP_TOKEN}", "--header", "X-Hevo-Region:us"],
      "env": { "HEVO_MCP_TOKEN": "<token>" }
    }
  }
}
```

### Production checklist

- **TLS in front.** The bearer token is a Hevo key: terminate HTTPS at your load balancer or ingress.
- **Pin regions.** Set `HEVO_MCP_ALLOWED_REGIONS` to the regions this server serves,
  so a request for another region is refused instead of crossing regions. For data residency, run one
  server per region. The server warns `regions_not_pinned` at startup when it's unset.
- **Set `HEVO_MCP_ALLOWED_HOSTS`** to the public hostname (DNS-rebinding protection; include the host
  your load balancer's health check uses).
- **Take data-losing endpoints out of reach** if agents shouldn't resync or delete anything, even with
  approval: add them to `[access] disabled_endpoints` in the config file (an example is in
  [config/hevo_mcp.toml](config/hevo_mcp.toml)). The server warns `risky_endpoints_reachable` at startup
  while a default scope above `read` leaves any of them callable.
- **Keep it off the open internet.** Make the server reachable only from your network or VPN, or behind an
  IP allow-list at the load balancer. It refuses keys Hevo rejected and limits keys it hasn't seen accepted
  (`[rate_limits] unverified_per_minute`), but anyone who can reach it can make it call Hevo.
- **Several replicas** need no sticky sessions: the server is stateless. Give them one shared
  `HEVO_MCP_APPROVAL_KEY` (a random secret of 32 or more characters; comma-separate keys to rotate, the
  first seals), so the answer to an approval prompt can reach any replica. Each replica remembers on its
  own which approvals it has used, so an approved answer could be replayed once on each other replica
  within its 10 minutes, by a client that already holds it and only for that exact call. A shared record
  of used approvals is on the list for the hosted server.
- **Timeouts:** load balancer idle timeout at least 450 s (an Edge pipeline create can take minutes);
  give the orchestrator a stop grace period longer than `HEVO_MCP_SHUTDOWN_GRACE` (450 s).
- **Logs** are JSON lines on standard error, the audit records among them. Optionally ship them to
  Coralogix (`CORALOGIX_*` below). Logs never contain keys, request bodies or results.
- **After each deploy,** check it with a test key (read-only, nothing is written):

  ```sh
  HEVO_MCP_TOKEN=<base64 key:secret> uv run python scripts/smoke_check.py https://mcp.example.com/mcp --region us
  ```

## Allowing changes

The server is **read-only by default**. A call that would change something returns `READ_ONLY` with a
hint, and nothing reaches Hevo.

| Scope | The agent can | Set with (local, options 1 and 2) | Set with (HTTP, per connection) |
| --- | --- | --- | --- |
| `read` (default) | List and inspect | | |
| `write` | Also create, update, pause, resume, run, resync | `HEVO_MCP_DEFAULT_SCOPE=write` | `X-Hevo-Scope: write` |
| `delete` | Also delete pipelines and Destinations | `HEVO_MCP_DEFAULT_SCOPE=delete` | `X-Hevo-Scope: delete` |

Your Hevo role still applies on top: a scope only limits, it never grants.

### Approval

Within the scope, some calls also need **your approval, every time**. The server shows the change in your
MCP client and sends nothing to Hevo until you approve. The model can't answer the prompt: the answer comes
from your MCP client's own UI, so use a client you trust. No scope, setting or tool argument skips it.

Every prompt has the same shape. The first line names the change, the resource and the region (Claude Code
shows only that line until you open the prompt), then one line says what happens, then who may approve.
The exact request follows under "Technical details": method, path and endpoint, the query, the fields that
say where a Source or Destination connects (host, account, URL) on their own line in full, and the body,
never with a secret value (passwords are references or `[redacted]`). When the request uses a secret file,
the last line says which file goes into which field and to which host, so you can check the host is yours.

```
Delete Edge Pipeline 352 in the us region?

Deletes this Pipeline. The tables in the Destination are not deleted. This action cannot be undone.

Approve only if you asked your agent for this change. Your agent cannot approve it for you.

Technical details: DELETE /api/v2/pipelines/352 (delete_pipeline)
```

| Needs approval | Doesn't |
| --- | --- |
| Every delete | Creating a pipeline, Destination or Model, unless the body carries a secret reference; setting a Standard pipeline's or Model's status to `ACTIVE` |
| Configuration changes: Destination, Source, pipeline settings, objects, mappings, transformations, Models | Reads |
| Destructive actions: cancel a job, disable or pause, skip, resync, restart, reset, move a position | |
| Runs that ingest or load data, which count toward your Events quota: sync now, run now, load now | |
| Enabling an Edge pipeline or resuming an object; inviting users, changing roles, removing users or OAuth accounts | |
| Any request whose body carries a secret reference (`$secret_file:<name>`), whatever the endpoint | |

`list_endpoints` and `get_schema` show `needs_approval` for each endpoint, and the full list is in
[endpoint_overrides.json](endpoint_overrides.json) (writes need approval unless marked `never`).

**Bulk actions get one prompt.** "Pause these 40 objects" is one call with `batch` (a list of targets, one
`path_params` each; body and query shared, at most 100 per call, or the writes-per-minute limit on a shared
HTTP server, 20 by default): you see one prompt listing every target,
approve once, and the server makes one request per target and reports each result. The first line of the
prompt gives the count ("Disable Edge Pipeline (40 targets) in the us region?") and a "Targets" list names
every one. The approval covers exactly that set of targets and nothing else.

The prompt uses MCP elicitation, so the client must support it (Claude Code does). MCP 2026-07-28 clients
get it on every transport, including a shared stateless HTTP server. Older clients get it over stdio; over
HTTP they can't receive it, so those calls return `APPROVAL_UNAVAILABLE`. In the prompt, **Accept** makes the
change; **Decline**, dismissing it, or picking Reject and accepting stops it, and the call returns
`APPROVAL_DECLINED`; if nobody answers within 10 minutes, `APPROVAL_TIMEOUT`. In each case nothing reaches
Hevo. Claude Code in print mode, which includes its VS Code
extension, declines prompts without showing them: make changes from an interactive `claude` session. Every decision is logged with the call (`approval` in the `tool_call` and audit records).

### Passwords for new Sources and Destinations

Creating a Source or Destination needs its real password, token or key: Hevo tests the connection before
it saves anything. Don't type it into the chat. Save it as a file in **`~/.hevo/secrets/`** (readable
only by you) and tell the agent the file's name; the agent writes `"password": "$secret_file:<name>"` in
the request, and the server swaps in the file's contents just before calling Hevo. The conversation, the
approval prompt and the logs only ever see the reference, and nothing is stored.

The file holds the plain value, nothing else: no quotes, no `password=` prefix, no JSON wrapper. A
password is one line; a service-account JSON or a PEM private key is saved as is, over several lines.
Whitespace before and after the value (the final newline an editor adds, a pasted space) is ignored.

```sh
mkdir -p ~/.hevo/secrets && chmod 700 ~/.hevo/secrets
read -rs PW && printf '%s' "$PW" > ~/.hevo/secrets/mysql-prod && chmod 600 ~/.hevo/secrets/mysql-prod
```

On Windows (PowerShell), after restricting `.hevo` as in [Key file](#key-file), so the new folder and file
are yours alone:

```powershell
New-Item -ItemType Directory -Force "$HOME\.hevo\secrets" | Out-Null
$pw = Read-Host -AsSecureString "Value"
[IO.File]::WriteAllText("$HOME\.hevo\secrets\mysql-prod", [Net.NetworkCredential]::new('', $pw).Password)
Remove-Variable pw
```

`WriteAllText` saves plain UTF-8 with no byte-order mark; `Set-Content` and `>` in Windows PowerShell 5.1
add a byte-order mark, which would become part of the value, or write UTF-16, which the server can't read.

Names are bare file names, never paths, and only this folder is read: a request can never pick up your
other credentials (cloud keys, tokens, variables). The folder is `secrets/` next to the key file, so with
`HEVO_KEYS_DIR` set it moves along (`hevo_whoami` shows it as `secrets_dir`).

Three rules keep a reference from leaking a secret:

- A reference is accepted only in a field that holds a secret (`password`, `*_token`, `client_secret`,
  `api_key`, ...): where Hevo stores it as a secret and where results come back `[redacted]`. In any other
  field (a host, a name) the call is refused, so the value can't be echoed back or sent to a host.
- **Every request that carries a reference asks for your approval**, even a create, which otherwise
  doesn't. The prompt shows the reference and the whole request, so you see which secret goes to which
  host before Hevo's connection test sends it. A prompt-injected "create a Destination at attacker.example"
  can't send your password unseen.
- The value is read when the call is made and is never stored, logged or shown in an error.

A reference that can't be read (missing or empty file, one other users can read, a variable without the
prefix) returns `SECRET_UNAVAILABLE` with the reason, and nothing is sent to Hevo. References work on a
local server (plugin or package); a shared HTTP server can't read your files, so there they also return
`SECRET_UNAVAILABLE`.

## What the server reads and where it sends data

A local server (the plugins, and `uvx hevo-mcp`) reads these and nothing else:

- **The key file**, `~/.hevo/hevo-key.json` or `$HEVO_KEYS_DIR/hevo-key.json`, for your Hevo API key.
- **Secret files** in the `secrets/` folder next to it, and only the one file a request names with
  `$secret_file:<name>`, only after you approved that request.
- **The config file**, if you pass one (`--config` or `HEVO_MCP_CONFIG`), and the variables listed in
  the [configuration reference](#configuration-reference).

It writes nothing to disk: no cache, no history, no log file. Logs go to standard error, where your MCP
client shows them, with keys and secrets redacted.

It sends data to exactly two places:

- **Hevo's API in the region you name**, over HTTPS: `asia`, `au`, `eu`, `eu2`, `in`, `us`, `us-gcp` or
  `us2.hevodata.com`, under `/api/v2/` (Edge) and `/api/public/v2.0/` (Standard pipelines, team users
  and OAuth accounts). Every request carries your key, and only what the tool call needs. The server knows only
  these hosts and can't be pointed at another. It never calls an endpoint that returns pipeline records,
  so your pipeline data stays in your Hevo region. What Hevo returns goes to your agent and so to its model
  provider: pipeline and object names, configuration with secrets redacted, and, from the account
  endpoints, your team members' names and email addresses.
- **PyPI**, for the update notice in `hevo_whoami`: one anonymous `GET` of
  `https://pypi.org/pypi/hevo-mcp/json`, at most once a day, never with your key or any other data.
  Off with `[updates] enabled = false` in the config file.

There is no telemetry, no analytics and no crash reporting. A shared HTTP server (section 3) reads no key
file and no secret files; it can additionally ship its logs to Coralogix if its operator configures that.

## What the agent sees

- **Secrets are redacted.** In every result, a value under a key that names a secret (`password`,
  `secret_key`, `private_key`, `client_secret`, `*_token`, ...) comes back as `[redacted]`.
- **Hidden text is removed.** Terminal escape sequences, control characters and invisible or
  direction-changing Unicode are stripped from every string Hevo returns, so nothing in a pipeline name or
  error message can hide instructions from you or rewrite your terminal.
- **Hevo's data is marked as untrusted.** The server tells the agent never to follow instructions found in it.
- **Tool descriptions are screened.** They come from Hevo's published API specs; the build fails if the
  text has hidden characters, instruction-like phrases or links outside known hosts.
- **The tool set is fingerprinted.** `hevo_whoami` and `/health-check` show `tools_sha256`, a hash of
  every tool's name, description and schema, and the server logs it at start (`tool_catalog`). It stays the
  same for a given release and configuration, so a change means the tools were altered: alert on it, or pin
  it in clients that support pinning.

### What these controls protect against

The scope, the approval prompts, the guards and the cleaning stop **mistakes by the model and calls steered
by text it read** (a pipeline name or error message that says "delete everything"): nothing risky reaches
Hevo through this server without you seeing it first. They are not a boundary against an agent that is
hostile and has file access on the same machine. Such an agent could read `~/.hevo/hevo-key.json`, call
Hevo's API itself and skip this server entirely; the server can't prevent that, and neither can any local
MCP server. So:

- Create the API key from a Hevo user with the least role that does the job; the key can never do more.
- Keep the agent out of the key and secrets folders ([Key file](#key-file) shows the Claude Code deny
  rule), and prefer agents that ask before reading files or running commands.
- The real boundary is a key Hevo itself limits (a read-only or scoped API key); until Hevo offers one, the
  role on the key is the limit.

## Configuration reference

**On your machine** (stdio; options 1 and 2), everything a user can set:

| Variable | Meaning |
| --- | --- |
| `HEVO_KEYS_DIR` | Folder with the key file `hevo-key.json` and the `secrets/` folder ([Key file](#key-file), [Passwords](#passwords-for-new-sources-and-destinations)); default `~/.hevo` |
| `HEVO_MCP_DEFAULT_SCOPE` | `read` (default), `write` or `delete` ([Allowing changes](#allowing-changes)) |
| `HEVO_MCP_LOG_LEVEL` | `DEBUG` or `WARNING` instead of `INFO`, when support asks for a verbose log |

The region is not a setting: the agent asks you and names it on every call. The key is never read from a
variable.

**Settings.** Every other setting has a built-in default and is changed in a TOML file
(`--config` or `HEVO_MCP_CONFIG`); [config/hevo_mcp.toml](config/hevo_mcp.toml) lists all of them with their
defaults, and a typo or a bad value stops the server at startup with the key named. A plugin or package
install needs none of them. The few that say which deployment a hosted server is also have an environment
variable. Precedence: defaults < config file < environment variable < command-line option.

| File key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `[access] default_scope` | `HEVO_MCP_DEFAULT_SCOPE` | `read` | Scope for stdio, and for HTTP connections without `X-Hevo-Scope` |
| `[access] disabled_endpoints` | | none | Endpoint or tool-group names to hide and refuse |
| `[access] allowed_regions` | `HEVO_MCP_ALLOWED_REGIONS` | any | Regions an HTTP server serves; others are refused |
| `[rate_limits] unverified_per_minute` | | `60` | Calls per minute, per replica and per client address, for API keys Hevo hasn't accepted yet. A key Hevo rejects is then refused for a minute without asking Hevo again. Stops floods of made-up keys without letting one source lock everyone out; `0` disables |
| `[rate_limits] reads_per_minute` | | `60` | HTTP only: read calls per minute per caller, per replica; `0` disables. A local (stdio) server has one caller and no limit |
| `[rate_limits] writes_per_minute` | | `20` | HTTP only: write and delete calls per minute per caller, per replica |
| `[timeouts] connect_seconds` | | `15` | Connecting to Hevo |
| `[timeouts] read_seconds` | | `60` | One Hevo read request |
| `[timeouts] write_seconds` | | `420` | One Hevo write (a pipeline create tests both connections) |
| `[timeouts] read_deadline_seconds` | | `90` | A whole read tool call, retries and `fetch_all` pages included |
| `[progress] interval_seconds` | | `10` | Progress notifications while waiting on Hevo; `0` turns them off |
| `[limits] max_rows` | | `5000` | `fetch_all` stops here and returns `truncated` with `next_cursor` |
| `[limits] max_pages` | | `200` | Page cap for `fetch_all` |
| `[limits] max_result_bytes` | | `1000000` | Larger results come back as `RESULT_TOO_LARGE` |
| `[http] max_body_bytes` | `HEVO_MCP_MAX_BODY_BYTES` | `1000000` | Larger HTTP request bodies get 413 |
| `[http] allowed_hosts` | `HEVO_MCP_ALLOWED_HOSTS` | none | `Host` header values to accept |
| `[http] allowed_origins` | `HEVO_MCP_ALLOWED_ORIGINS` | none | Browser origins allowed to call |
| `[http] shutdown_grace_seconds` | `HEVO_MCP_SHUTDOWN_GRACE` | `450` | Wait for in-flight calls on SIGTERM |
| `[hevo_pool] max_connections` | | `100` | Outbound connections to Hevo per replica |
| `[hevo_pool] max_keepalive_connections` | | `20` | Idle connections kept open |
| `[updates] enabled` | | `true` | `hevo_whoami` says when a newer release exists. One anonymous request to PyPI, at most once a day, never with your key |
| `[updates] url` | | PyPI's `hevo-mcp` page (JSON) | Where releases are listed |

**Hosted server only** (environment):

| Variable | Meaning |
| --- | --- |
| `HEVO_MCP_TRANSPORT`, `HEVO_MCP_HOST`, `HEVO_MCP_PORT`, `HEVO_MCP_PUBLIC_URL`, `HEVO_MCP_CONFIG` | How and where the server runs ([Run the server](#run-the-server)) |
| `HEVO_MCP_APPROVAL_KEY` | Shared key that seals approval prompts, for several replicas of an HTTP server; each process makes its own when unset |
| `CORALOGIX_PRIVATE_KEY` | Ship logs to your Coralogix account. With `CORALOGIX_DOMAIN` (default `coralogix.com`) or `CORALOGIX_LOGS_URL`, `CORALOGIX_APPLICATION`, `CORALOGIX_SUBSYSTEM` |

No key or secret is stored in this repository. Everything sensitive comes from your environment, a file
path you choose, or (HTTP) each request's headers.

## Tools

- **One tool per product, resource and kind**, for example `pipelines_read`, `pipelines_write`,
  `pipelines_destructive`, `destinations_delete`, `metadata_read` (Edge), `standard_pipelines_read`,
  `standard_models_write` (Standard) and `accounts_write` (shared). Each takes `name` (the endpoint),
  `path_params`, `query`, `body`, and on paginated lists `fetch_all`. 86 endpoints in 22 tools: 28 from
  the Edge API v2, 49 Standard pipeline, object, event-type, mapping, transformation, Destination, Model and
  Workflow endpoints from the Public API v2.0, and its 9 account endpoints (team users and OAuth
  accounts, which both products share). Writes that lose or re-ingest data (resync, cancel a job, restart
  an object, move a position) are in the `*_destructive` tools, not the `*_write` ones, so a client-side
  "always allow" on a write tool never covers them and each tool's `destructiveHint` is accurate.
- **Bodies are checked before anything else sees them.** A request body is validated against the
  endpoint's schema (required keys, types, enums); a bad one returns `INVALID_BODY` with the problems, and
  you are never asked to approve a request Hevo would reject.
- `list_endpoints` and `get_schema` to discover endpoints, their parameters and their consequences (`get_schema` adds the response schema only with `include_response: true`).
- `hevo_whoami` to check the connection.
- **Left out on purpose:** any endpoint that returns pipeline records (the two Standard
  transformation-sample endpoints). The server returns only
  configuration and metadata, so pipeline data stays in the customer's region wherever the server runs.
  This is a data-protection decision (GDPR data residency): Hevo promises customers that their pipeline
  data never leaves the region they choose. Every endpoint in `endpoint_overrides.json` is classified
  (`records: true` means excluded) and the build fails for an unclassified one, so a spec sync can't ship
  one unreviewed. Don't add such an endpoint without a data-residency review.

**Resources:** `hevo://regions` (regions and their API hosts) and the `hevo-pipelines` skill,
`skill://hevo-pipelines/SKILL.md` with its manifest `skill://hevo-pipelines/_manifest`. The skill is the
same file the plugins install, shipped inside the package, so a client connected only by URL (a hosted
server) can load or attach it. Its key rules (approval before every change, how to diagnose a failing
pipeline) are also in the instructions the server sends every client.

Errors come back as `{"error": CODE, "message": ..., "hint": ...}` with a `request_id` that also appears
in the server's log.

## Troubleshooting

| Message | What to do |
| --- | --- |
| `No Hevo API key file at ...` | Save the downloaded key as `~/.hevo/hevo-key.json` ([Key file](#key-file)); the message names the exact path it looked at |
| `No region given` / `Send the X-Hevo-Region header` | Tell the agent your region (the subdomain you sign in at); over HTTP, send the header |
| `UPSTREAM_UNAUTHORIZED` | Wrong Access Key or Secret Key, a deleted key, or a key from another region |
| `UPSTREAM_FORBIDDEN` | The Hevo role of the user who created the key doesn't allow this action |
| `REGION_UNSUPPORTED` | That region doesn't carry this product: `eu2` has Edge Pipelines only, `us-gcp` Standard Pipelines only. Name the region that holds them |
| `RESULT_TOO_LARGE` | One answer is over 1 MB: page with `limit` and the cursor. `fetch_all` stops under the cap on its own and hands back the rows so far with the next cursor |
| `INVALID_ARGUMENTS` with a query or body problem | The query named a parameter the endpoint doesn't have (a misspelt filter would otherwise return everything), or a body was sent to an endpoint that takes none |
| `READ_ONLY` | Allow changes ([Allowing changes](#allowing-changes)) |
| `APPROVAL_UNAVAILABLE` | Your MCP client can't show approval prompts; use one that supports elicitation ([Approval](#approval)) |
| `This Hevo MCP endpoint serves region …` | You connected to a server for another region |
| HTTP 401 when connecting | Missing or malformed `Authorization: Bearer <base64 key:secret>` header |
| HTTP 421 | The `Host` header isn't in `HEVO_MCP_ALLOWED_HOSTS` |
| The client shows no Hevo tools | Check `uv` is installed and on the client's `PATH` (Claude Desktop: use the full path), then restart the client |

The server writes its logs to standard error, which MCP clients show in their MCP logs.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout, branch names and how a pull request is merged.

```sh
uv sync                                  # install with dev dependencies
uv run pytest                            # tests (a mocked Hevo API; no key needed)
uvx ruff check .                         # lint
uv build                                 # wheel and source package in dist/
```

- **The tools are generated** from Hevo's OpenAPI specs. `python build_manifest.py --fetch` pulls the
  current specs from Hevo's API docs into `openapi/`, `python build_manifest.py` rebuilds
  `src/hevo_mcp_server/manifest/`, and `endpoint_overrides.json` adds what a spec can't say (a
  `consequence`, `destructive`, a better name). CI fails if the committed manifest doesn't match the
  specs.
- **The skill lives here.** `skills/hevo-pipelines/SKILL.md` is the source of truth; it uses the MCP tools
  only, never Hevo's API directly. A test fails if it names an endpoint or tool that doesn't exist.
- **Live checks** (`scripts/smoke_check.py`, read-only) run a running server's tools against Hevo with a
  production key in `HEVO_MCP_TOKEN`, in one of Hevo's public regions. Run by hand after a deploy; CI
  never calls Hevo.
- **This repository's CI needs no secrets.** Hevo maintainers bring the catalog up to date with the
  published API specs and open pull requests here for review.
- `scripts/package_check.py` installs a built wheel and runs it over stdio as a customer would.
- **Plugins.** One server and one skill folder feed both plugin manifests (Claude Code, Codex).
  `tests/test_plugin_manifests.py` fails if their version, server command or settings
  drift apart; `claude plugin validate .` checks the Claude manifest.
- **Dependencies.** `constraints.txt` pins every dependency at its `uv.lock` version; the plugins and
  the documented `uvx` commands install with it, and a copy ships inside the package so `hevo_whoami` can
  report whether an install matches it. After changing dependencies (`uv lock`), run
  `uv run python scripts/set_version.py --constraints`; a test fails while it's stale.
- **Releasing.** `uv run python scripts/set_version.py <version>` sets the new version in `pyproject.toml`,
  every plugin manifest and every install-from-tag reference, and updates `uv.lock` and `constraints.txt`
  (`--check` only reports).
  Commit, merge, then tag the merged commit `v<version>` and push the tag: the plugins install from that tag,
  and Codex and the stdio instructions install that version from PyPI, and `hevo_whoami` tells older installs a newer version is
  available. Every release needs a new version: uv caches the server by version. Pushing the tag runs
  `.github/workflows/release.yml`: it builds the wheel and the source package, signs where they
  came from (GitHub artifact attestations), publishes them with `SHA256SUMS` as a GitHub release, and then,
  after an approval in the `pypi` environment, uploads the package to PyPI with trusted publishing (no token
  stored). A version on PyPI can never be replaced.

## License

MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE). The license covers this code; it
doesn't grant use of the Hevo name or logo beyond describing where the software comes from.
