"""Tells hevo_whoami whether a newer release of this server exists, so users learn to update.

Releases are the versions published on PyPI (`hevo-mcp`); a GitHub tags list (`v<version>` names) works too.
The check is one anonymous GET (no key, no user data), made at most once a day per process and only when
hevo_whoami runs. Pre-releases are ignored. Any failure (offline,
the repository not reachable, an odd answer) just means no notice: it never slows or breaks the tool.
Off with [updates] enabled = false in the config file.
"""

import re
import time

import httpx2 as httpx

from hevo_mcp_server.observability import VERSION

TAG = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
CACHE_SECONDS = 24 * 3600
RETRY_SECONDS = 3600  # after a failed check
TIMEOUT_SECONDS = 2.0
_cache: dict[str, tuple[float, str | None]] = {}

HOW_TO_UPDATE = ("Claude Code plugin: run claude plugin marketplace update hevo-data, then claude plugin update "
                 "hevo-data@hevo-data, and start a new session. Codex: run codex plugin marketplace upgrade hevo-data, "
                 "and start a new session. PyPI install: change the version in your MCP settings (or use hevo-mcp@latest). "
                 "Shared server: ask its operator.")


def _version(text: str) -> tuple[int, ...] | None:
    match = TAG.fullmatch(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


async def latest_version(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """The highest release at `url` (PyPI's JSON for the project, or a GitHub tags list), cached for a day;
    None if unknown."""
    hit = _cache.get(url)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    latest = None
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS,
                                     headers={"User-Agent": f"hevo-mcp/{VERSION}",
                                              "Accept": "application/json"}) as http:
            resp = await http.get(url)
        body = resp.json() if resp.status_code == 200 else None
        if isinstance(body, dict):  # PyPI: {"info": {"version": ...}, "releases": {"1.0.0": [...], ...}}
            names = [n for n, files in (body.get("releases") or {}).items() if files] or \
                    [str((body.get("info") or {}).get("version", ""))]
        elif isinstance(body, list):  # GitHub tags: [{"name": "v1.0.0"}, ...]
            names = [t["name"] for t in body if isinstance(t, dict) and isinstance(t.get("name"), str)]
        else:
            names = []
        found = [(v, n.lstrip("v")) for n in names if (v := _version(n))]
        latest = max(found)[1] if found else None
    except (httpx.HTTPError, ValueError):
        latest = None
    _cache[url] = (time.monotonic() + (CACHE_SECONDS if latest else RETRY_SECONDS), latest)
    return latest


async def update_notice(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """"A newer version ... is available" when the latest release is above this one, else None."""
    if not url:
        return None
    latest = await latest_version(url, transport=transport)
    current, newest = _version(VERSION), _version(latest or "")
    if not current or not newest or newest <= current:
        return None
    return f"A newer version of the Hevo MCP server is available: {latest} (this is {VERSION}). {HOW_TO_UPDATE}"
