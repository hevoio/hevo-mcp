"""Where runs may point: specs come only from Hevo's public API docs, and the smoke check calls every read
endpoint once without writing anything."""

import httpx2 as httpx
import pytest
from fastmcp import Client

import build_manifest as bm
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server
from scripts import smoke_check
from tests.helpers import FakeHevo, run

SETTINGS = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0)


def test_specs_come_only_from_public_api_docs(monkeypatch, tmp_path):
    assert all(cfg["docs"].endswith(".hevodata.com") for cfg in bm.PRODUCTS.values())
    monkeypatch.setitem(bm.PRODUCTS, "edge", {**bm.PRODUCTS["edge"], "docs": "https://test-env.example.com"})
    with pytest.raises(SystemExit, match="only from Hevo's public API docs"):
        bm.fetch_specs(tmp_path)


def test_all_reads_calls_each_read_endpoint_without_a_path_id(env):
    fake = FakeHevo()
    srv = create_server(remote=False, settings=SETTINGS,
                        client=HevoClient(httpx.MockTransport(fake), settings=SETTINGS))
    results: list[tuple[str, bool]] = []

    async def go():
        async with Client(srv) as c:
            await smoke_check.all_reads(c, lambda name, ok, detail="": results.append((name, ok)))

    run(go())
    assert results and all(ok for _, ok in results)
    assert all(r.method == "GET" for r in fake.calls)  # nothing is ever written
    assert not any("{" in r.url.path for r in fake.calls)
