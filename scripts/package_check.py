"""Checks an installed hevo-mcp package the way a customer runs it: as a local stdio server.

    uv build && uv venv /tmp/pkg && VIRTUAL_ENV=/tmp/pkg uv pip install dist/*.whl
    /tmp/pkg/bin/python -I scripts/package_check.py /tmp/pkg/bin/hevo-mcp

Starts the installed command with a dummy key (nothing reaches Hevo), then checks the tools are listed,
the bundled manifest has every endpoint, get_schema works, the skill is served and a write is refused without an opt-in.
Exit code 0 when every check passes; 1 otherwise.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

# The installed package must serve every endpoint of the manifest in this checkout.
MANIFEST = Path(__file__).resolve().parent.parent / "src" / "hevo_mcp_server" / "manifest" / "endpoints.json"
META = json.loads(MANIFEST.read_text())["meta"]
EXPECTED_ENDPOINTS = META["endpoints"]
EXPECTED_TOOLS = META["tools"] + 3  # the group tools plus list_endpoints, get_schema and hevo_whoami


async def main(command: str) -> int:
    # A throwaway key file in a temporary keys folder: the server reads it like a user's ~/.hevo/hevo-key.json.
    keys = Path(tempfile.mkdtemp())
    (keys / "hevo-key.json").write_text(json.dumps({"api_key": "package-check", "secret_key": "not-a-real-secret"}))
    (keys / "hevo-key.json").chmod(0o600)
    env = {"HEVO_KEYS_DIR": str(keys)}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    async with Client(StdioTransport(command=command, args=[], env=env)) as c:
        tools = await c.list_tools()
        check("tools/list", len(tools) == EXPECTED_TOOLS, f"{len(tools)} tools, expected {EXPECTED_TOOLS}")
        total = (await c.call_tool("list_endpoints", {})).structured_content["total"]
        check("bundled manifest", total == EXPECTED_ENDPOINTS, f"{total} endpoints")
        schema = (await c.call_tool("get_schema", {"name": "list_pipelines"})).structured_content
        check("get_schema", schema.get("path") == "/api/v2/pipelines", schema.get("path"))
        skill = (await c.read_resource("skill://hevo-pipelines/SKILL.md"))[0].text
        check("bundled skill", skill.startswith("---\nname: hevo-pipelines\n"), f"{len(skill)} chars")
        out = (await c.call_tool("pipelines_write", {"name": "enable_pipeline", "region": "us",
                                                          "path_params": {"pipeline_id": 1}},
                                 raise_on_error=False)).structured_content or {}
        check("read-only by default", out.get("error") == "READ_ONLY", out.get("error"))
    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
