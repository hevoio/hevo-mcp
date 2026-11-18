"""Read-only smoke check against a running Hevo MCP server (local or deployed).

    HEVO_MCP_TOKEN=$(printf '%s:%s' "$KEY" "$SECRET" | base64) \\
    python scripts/smoke_check.py https://mcp.example.com/mcp --region us

The server knows only Hevo's public regions, so `--region` is one of those and the key is a production key.

`--all-reads` also calls every read endpoint that needs no path id once (one row where paginated).

Checks, in order: /health-check, MCP initialize and tools/list, hevo_whoami against both APIs, one list
call per product, two shaped errors (unknown endpoint, invalid arguments) and a 401 without a token. Nothing is written.
Exit code 0 when every check passes; 1 otherwise. The token is never printed.
"""

import argparse
import asyncio
import os
import sys
import time

import httpx2 as httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

EXPECTED_TOOLS = {"list_endpoints", "get_schema", "hevo_whoami"}
# Errors that say the call needs input the check can't supply, or a role the test key lacks: not a failure.
SKIPPABLE = {"INVALID_ARGUMENTS", "UPSTREAM_FORBIDDEN"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="MCP endpoint, e.g. http://127.0.0.1:8000/mcp")
    parser.add_argument("--region", default="", help="X-Hevo-Region value")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--all-reads", action="store_true",
                        help="also call every read endpoint that takes no path id (GET only, nothing written)")
    args = parser.parse_args()
    token = os.environ.get("HEVO_MCP_TOKEN", "")
    if not token:
        print("set HEVO_MCP_TOKEN to base64(key:secret)", file=sys.stderr)
        return 2
    base = args.url.rsplit("/", 1)[0]
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    async with httpx.AsyncClient(timeout=args.timeout) as h:
        try:
            r = await h.get(f"{base}/health-check")
            body = r.json()
            check("health-check", r.status_code == 200 and body.get("status") == "ok",
                  f"version {body.get('version')} endpoints {body.get('endpoints')} catalog {body.get('catalog')}")
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            check("health-check", False, repr(exc))
        try:
            r = await h.post(args.url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                             headers={"accept": "application/json, text/event-stream"})
            check("no token -> 401", r.status_code == 401, f"got {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            check("no token -> 401", False, repr(exc))

    headers = {"X-Hevo-Region": args.region} if args.region else {}
    transport = StreamableHttpTransport(args.url, auth=token, headers=headers)
    try:
        async with Client(transport, timeout=args.timeout) as c:
            start = time.perf_counter()
            tools = {t.name for t in await c.list_tools()}
            check("tools/list", EXPECTED_TOOLS <= tools and any(t.endswith("_read") for t in tools),
                  f"{len(tools)} tools in {(time.perf_counter() - start) * 1000:.0f} ms")

            who = (await c.call_tool("hevo_whoami", {})).structured_content
            checks = who.get("api_checks", {})
            check("hevo_whoami", all(v == "ok" for v in checks.values()),
                  f"region {who.get('region')} key {who.get('api_key')} scope {who.get('scope')} checks {checks}")

            for tool, name in (("pipelines_read", "list_pipelines"),
                               ("destinations_read", "list_destinations")):
                if tool not in tools:
                    check(name, False, "tool missing")
                    continue
                start = time.perf_counter()
                out = (await c.call_tool(tool, {"name": name, "query": {"limit": 1}},
                                         raise_on_error=False)).structured_content or {}
                check(name, "error" not in out,
                      out.get("error", f"{(time.perf_counter() - start) * 1000:.0f} ms"))

            if args.all_reads:
                await all_reads(c, check)
            r = await c.call_tool("pipelines_read", {"name": "no_such_endpoint"}, raise_on_error=False)
            out = r.structured_content or {}
            check("shaped error", r.is_error and out.get("error") == "UNKNOWN_ENDPOINT"
                  and len(out.get("request_id", "")) == 32, str(out.get("error")))
            r = await c.call_tool("pipelines_read", {"name": "list_pipelines", "querry": {}},
                                  raise_on_error=False)
            out = r.structured_content or {}
            check("invalid arguments", r.is_error and out.get("error") == "INVALID_ARGUMENTS", str(out.get("error")))
    except Exception as exc:  # noqa: BLE001
        check("mcp session", False, repr(exc))

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


async def all_reads(c: Client, check) -> None:
    rows = (await c.call_tool("list_endpoints", {"search": "/"})).structured_content["endpoints"]
    reads = [e for e in rows if e["action"] == "read" and "{" not in e["path"]]
    skipped = []
    for ep in reads:
        args = {"name": ep["name"], **({"query": {"limit": 1}} if ep["paginated"] else {})}
        out = (await c.call_tool(ep["tool"], args, raise_on_error=False)).structured_content or {}
        if out.get("error") in SKIPPABLE:
            skipped.append(f"{ep['name']} ({out['error']})")
            continue
        check(f"read {ep['name']}", "error" not in out, out.get("error", ""))
    print(f"     {len(reads)} read endpoints without a path id; skipped {len(skipped)}: {', '.join(skipped) or 'none'}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
