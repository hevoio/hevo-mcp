"""The operational controls: every endpoint routes, rate limits, scopes, denylist, size caps, body cap."""

import asyncio
import json
import socket
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.middleware import Middleware

from hevo_mcp_server import tools, validation
from hevo_mcp_server.auth import HevoKeyVerifier, encode_basic
from hevo_mcp_server.catalog import BY_NAME, ENDPOINTS, TOOLS, load_schema
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.http_guards import MaxBodySize
from hevo_mcp_server.main import http_run_options
from hevo_mcp_server.ratelimit import RateLimiter
from hevo_mcp_server.settings import FIELDS, Settings, scope_allows
from hevo_mcp_server.tools import HevoServer, create_server
from tests.helpers import FakeHevo, call, minimal_body, run

TOKEN = encode_basic("key-alice-1234", "secret-alice")
UNLIMITED = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete")


def server(fake, settings=UNLIMITED, **kwargs):
    client = HevoClient(httpx.MockTransport(fake), settings=settings)
    return create_server(remote=False, client=client, settings=settings, **kwargs)


# --- Contract: every endpoint in the catalog is callable through its tool ------------------------------

def test_every_endpoint_routes_to_its_method_and_path(env):
    """A spec change that breaks naming, grouping or path placeholders fails here, not in production."""
    fake = FakeHevo({
        ("GET", "/api/v2/metadata/sources/connectors"): {"data": [{"connector_id": "stripe", "type": "STRIPE"}]},
        ("GET", "/api/v2/metadata/sources/connectors/stripe"): {"data": {"schema": {"properties": {"config": {}}}}},
        ("GET", "/api/v2/destinations/1"): {"data": {"id": 1, "connector_id": "snowflake"}},
        ("GET", "/api/v2/metadata/destinations/connectors"): {"data": [{"connector_id": "snowflake"}]},
    })

    async def go():
        sent = {}
        async with Client(server(fake)) as c:
            for ep in ENDPOINTS:
                if ep.deprecated:
                    continue
                params = {p: f"v-{p}" for p in _placeholders(ep.path)}
                args = {"path_params": params}
                if ep.name == "standard_delete_pipeline":
                    args["query"] = {"dropResource": False}
                if ep.name == "create_pipeline":
                    args["body"] = minimal_body(load_schema(ep.name), {
                        "source": {"name": "s", "connector_id": "stripe", "type": "STRIPE", "config": {}}, "destination_id": 1})
                elif ep.is_write and validation.takes_body(ep.name):
                    args["body"] = minimal_body(load_schema(ep.name))  # unknown keys are refused now
                before = len(fake.calls)
                result = await call(c, ep.tool, ep.name, **args)
                assert "error" not in result, (ep.name, result)
                last = fake.calls[-1]
                assert len(fake.calls) > before, ep.name
                sent[ep.name] = (last.method, last.url.path)
        return sent

    sent = run(go())
    for ep in ENDPOINTS:
        if ep.deprecated:
            continue
        expected = ep.path
        for p in _placeholders(ep.path):
            expected = expected.replace("{" + p + "}", f"v-{p}")
        assert sent[ep.name] == (ep.method, expected), ep.name
    assert len(sent) == len([e for e in ENDPOINTS if not e.deprecated])


def _placeholders(path):
    return [p.strip("{}") for p in path.split("/") if p.startswith("{")]


# --- Rate limiting -----------------------------------------------------------------------------------

def test_rate_limiter_is_a_token_bucket_per_caller_and_kind():
    now = [0.0]
    limiter = RateLimiter(reads_per_minute=2, writes_per_minute=1, clock=lambda: now[0])
    assert limiter.check("a", False) is None and limiter.check("a", False) is None
    assert limiter.check("a", False) == 30  # 2 per minute: next token in 30 s
    assert limiter.check("b", False) is None  # another caller has its own bucket
    assert limiter.check("a", True) is None and limiter.check("a", True) == 60
    now[0] = 31.0
    assert limiter.check("a", False) is None
    assert RateLimiter(0, 0).check("a", True) is None  # 0 disables


def test_rate_limited_calls_never_reach_hevo_on_a_shared_server(env, caplog):
    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo()
    port = _free_port()
    limited = Settings(rate_reads_per_minute=2, rate_writes_per_minute=0)
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=limited)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "delete"})
            async with Client(t) as c:
                ok1 = await call(c, "pipelines_read", "list_pipelines", region=None)
                ok2 = await call(c, "pipelines_read", "list_pipelines", region=None)
                refused = await call(c, "pipelines_read", "list_pipelines", region=None)
                write = await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1},
                                   region=None)
                return ok1, ok2, refused, write
        finally:
            task.cancel()

    ok1, ok2, refused, write = run(go())
    assert "error" not in ok1 and "error" not in ok2
    assert refused["error"] == "RATE_LIMITED" and refused["retry_after"] == 30
    assert "error" not in write  # writes have their own (unlimited here) bucket
    assert len(fake.calls) == 3
    rec = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("error") == "RATE_LIMITED"]
    assert rec and rec[0]["outcome"] == "refused" and rec[0]["hevo_calls"] == 0


def test_a_local_server_has_no_rate_limit(env):
    """One caller runs a stdio server, and the skill's workflows make one call per pipeline: the shared-server
    brake must not stall them. Hevo's own limits still apply."""
    fake = FakeHevo()
    limited = Settings(rate_reads_per_minute=2, rate_writes_per_minute=1, default_scope="delete")

    async def go():
        async with Client(server(fake, limited)) as c:
            return [await call(c, "pipelines_read", "list_pipelines") for _ in range(5)]

    results = run(go())
    assert all("error" not in r for r in results) and len(fake.calls) == 5


# --- Scope -------------------------------------------------------------------------------------------

def test_scope_levels_nest():
    assert scope_allows("delete", "delete") and scope_allows("delete", "read")
    assert scope_allows("write", "write") and not scope_allows("write", "delete")
    assert scope_allows("read", "read") and not scope_allows("read", "write")


def test_scope_header_caps_what_a_connection_can_do(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=UNLIMITED)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            out = {}
            for scope in ("read", "write", "delete"):
                t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": scope})
                async with Client(t) as c:
                    out[scope] = (
                        await call(c, "pipelines_read", "list_pipelines"),
                        await call(c, "pipelines_write", "enable_pipeline", path_params={"pipeline_id": 1}),
                        await call(c, "pipelines_delete", "delete_pipeline", path_params={"pipeline_id": 1}),
                        await call(c, "hevo_whoami"),
                    )
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "admin"})
            async with Client(t) as c:
                bad = await c.call_tool("hevo_whoami", {}, raise_on_error=False)
            return out, bad
        finally:
            task.cancel()

    out, bad = run(go())
    assert out["read"][0].get("error") is None and out["read"][1]["error"] == "READ_ONLY"
    assert out["read"][2]["error"] == "READ_ONLY"
    assert out["write"][1].get("error") is None and out["write"][2]["error"] == "SCOPE_EXCEEDED"
    assert out["write"][2]["required"] == "delete" and out["write"][2]["scope"] == "write"
    # Over HTTP the fix is the header.
    assert "X-Hevo-Scope header set to write" in out["read"][1]["message"]
    assert "X-Hevo-Scope header set to delete" in out["write"][2]["message"]
    assert all(r.get("error") is None for r in out["delete"][:3])
    assert [out[s][3]["scope"] for s in ("read", "write", "delete")] == ["read", "write", "delete"]
    assert bad.is_error and "X-Hevo-Scope" in bad.content[0].text
    # Only the permitted calls reached Hevo: 3 reads, 2 enables, 1 delete (plus whoami probes).
    assert [c.method for c in fake.writes()] == ["POST", "POST", "DELETE"]


def test_default_scope_setting_applies_without_a_header(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=Settings(default_scope="read", rate_reads_per_minute=0, rate_writes_per_minute=0))

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            async with Client(StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us"})) as c:
                return await call(c, "pipelines_write", "enable_pipeline", path_params={"pipeline_id": 1})
        finally:
            task.cancel()

    assert run(go())["error"] == "READ_ONLY" and fake.writes() == []


# --- Denylist ----------------------------------------------------------------------------------------

def test_disabled_endpoints_vanish_from_discovery_tools_and_dispatch(env):
    fake = FakeHevo()
    settings = Settings(disabled_endpoints=frozenset({"resync_pipeline", "destinations_delete"}),
                        default_scope="delete",
                        rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(server(fake, settings)) as c:
            tools = {t.name: t for t in await c.list_tools()}
            return (tools, await call(c, "list_endpoints", search="e"),
                    await call(c, "list_endpoints"),
                    await call(c, "get_schema", "resync_pipeline"),
                    await call(c, "pipelines_destructive", "resync_pipeline", path_params={"pipeline_id": 1}),
                    await call(c, "pipelines_destructive", "cancel_pipeline_job",
                               path_params={"pipeline_id": 1, "job_id": "j"}))

    tools, search, summary, schema, refused, allowed = run(go())
    assert "destinations_delete" not in tools  # whole group disabled: no tool
    assert tools["pipelines_destructive"].input_schema["properties"]["name"]["enum"] == [
        "cancel_pipeline_job", "resync_pipeline_objects"]
    names = {r["name"] for r in search["endpoints"]}
    assert "resync_pipeline" not in names and "delete_destination" not in names
    assert "delete_pipeline" in names and "resync_pipeline_objects" in names
    assert summary["total"] == len(ENDPOINTS) - 2
    assert schema["error"] == "ENDPOINT_DISABLED" and refused["error"] == "ENDPOINT_DISABLED"
    assert "error" not in allowed and [c.url.path for c in fake.writes()] == ["/api/v2/pipelines/1/jobs/j/actions/cancel"]


def test_unknown_disabled_name_fails_at_startup(env):
    with pytest.raises(SystemExit, match="not_an_endpoint"):
        server(FakeHevo(), Settings(disabled_endpoints=frozenset({"not_an_endpoint"})))


# --- Size caps ---------------------------------------------------------------------------------------

def test_fetch_all_stops_at_the_row_cap_and_hands_back_the_cursor(env):
    def edge(req):
        cursor = req.url.params.get("cursor", "0")
        n = int(cursor)
        return httpx.Response(200, json={"data": [{"id": n * 10 + i} for i in range(10)], "has_more": True,
                                         "next_cursor": str(n + 1)})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): edge})
    settings = Settings(max_rows=25, rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(server(fake, settings)) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert out["truncated"] is True and out["count"] == 30 and out["pages"] == 3  # whole pages, so >= cap
    assert out["next_cursor"] == "3" and out["cursor_param"] == "cursor"


def test_oversized_results_come_back_as_an_error_not_a_flood(env, caplog):
    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo({("GET", "/api/v2/pipelines"): {"data": [{"name": "x" * 100} for _ in range(50)]}})
    settings = Settings(max_result_bytes=2000, rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(server(fake, settings)) as c:
            return await call(c, "pipelines_read", "list_pipelines")

    out = run(go())
    assert out["error"] == "RESULT_TOO_LARGE" and out["bytes"] > 2000 and out["limit"] == 2000
    [rec] = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "tool_call"]
    assert rec["result_bytes"] < 2000 and rec["error"] == "RESULT_TOO_LARGE" and rec["status"] == 200


def test_oversized_request_bodies_get_413_before_the_mcp_layer(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=UNLIMITED)

    async def go():
        task = asyncio.create_task(srv.run_http_async(
            host="127.0.0.1", port=port, show_banner=False, stateless_http=True,
            middleware=[Middleware(MaxBodySize, limit=500)]))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            async with httpx.AsyncClient() as h:
                big = await h.post(url, content=b"{" + b" " * 600 + b"}", headers={
                    "content-type": "application/json", "authorization": f"Bearer {TOKEN}",
                    "accept": "application/json, text/event-stream"})
                health = await h.get(f"http://127.0.0.1:{port}/health-check")
            async with Client(StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us"})) as c:
                small = await call(c, "pipelines_read", "list_pipelines")
            return big, health.json(), small
        finally:
            task.cancel()

    big, health, small = run(go())
    assert big.status_code == 413 and big.json()["error"] == "PAYLOAD_TOO_LARGE"
    assert "error" not in small
    assert set(health["catalog"]) == {"edge", "standard", "account"} and health["endpoints"] == len(ENDPOINTS)



# --- Origin check and graceful shutdown (the production HTTP options) ----------------------------------

def _start(srv, port, settings=UNLIMITED, origins=()):
    return asyncio.create_task(srv.run_http_async(
        host="127.0.0.1", port=port, show_banner=False, **http_run_options(replace(settings, allowed_origins=tuple(origins)))))


def test_browser_origins_are_refused_unless_listed(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=UNLIMITED)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    base = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
            "x-hevo-region": "us"}

    async def go():
        task = _start(srv, port, origins=["https://inspector.example"])
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            async with httpx.AsyncClient() as h:
                foreign = await h.post(url, json=body, headers={**base, "origin": "https://evil.example"})
                listed = await h.post(url, json=body, headers={**base, "origin": "https://inspector.example"})
                none = await h.post(url, json=body, headers=base)
            return foreign, listed, none
        finally:
            task.cancel()

    foreign, listed, none = run(go())
    assert foreign.status_code == 403
    assert listed.status_code == 200 and none.status_code == 200  # agent backends send no Origin


def test_shutdown_waits_for_in_flight_calls(env, monkeypatch):
    """A deploy (SIGTERM) must let a slow Hevo call finish; FastMCP's own default would cut it off at 2 s."""
    import uvicorn

    servers = []
    init = uvicorn.Server.__init__

    def capture(self, config):
        init(self, config)
        servers.append(self)

    monkeypatch.setattr(uvicorn.Server, "__init__", capture)

    async def slow(request):
        await asyncio.sleep(4)
        return httpx.Response(200, json={"data": [{"id": 1}], "has_more": False})

    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, shutdown_grace_seconds=30)
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), settings=settings,
                        client=HevoClient(httpx.MockTransport(slow), settings=settings))

    async def go():
        task = _start(srv, port, settings)
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "pipelines_read", "arguments": {"name": "list_pipelines"}}}
        headers = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
                   "x-hevo-region": "us"}
        async with httpx.AsyncClient(timeout=30) as h:
            pending = asyncio.create_task(h.post(url, json=body, headers=headers))
            await asyncio.sleep(0.5)
            servers[-1].should_exit = True  # what uvicorn's SIGTERM handler does
            resp = await pending
        await asyncio.wait_for(task, 10)
        return resp

    resp = run(go())
    assert resp.status_code == 200
    [data] = [line[5:] for line in resp.text.splitlines() if line.startswith("data:") and line[5:].strip()]
    out = json.loads(data)["result"]["structuredContent"]
    assert "error" not in out and out["data"] == [{"id": 1}]

# --- Settings ----------------------------------------------------------------------------------------

def test_settings_come_from_env_and_reject_nonsense(monkeypatch):
    """Only the settings a user or a hosted deployment sets have a variable; tunables come from the file."""
    monkeypatch.setenv("HEVO_MCP_RATE_READS", "120")  # a tunable: no variable, so this is ignored
    monkeypatch.setenv("HEVO_MCP_DISABLED_ENDPOINTS", "resync_pipeline")  # same
    monkeypatch.setenv("HEVO_MCP_DEFAULT_SCOPE", "write")
    monkeypatch.setenv("HEVO_MCP_ALLOWED_REGIONS", "EU, eu2")
    s = Settings.from_env()
    assert s.rate_reads_per_minute == 60 and s.disabled_endpoints == frozenset()
    assert s.default_scope == "write" and s.allowed_regions == ("eu", "eu2")
    assert s.as_log_fields()["allowed_regions"] == ["eu", "eu2"]
    assert {f.env for f in FIELDS if f.env} == {"HEVO_MCP_DEFAULT_SCOPE", "HEVO_MCP_ALLOWED_REGIONS",
                                                 "HEVO_MCP_MAX_BODY_BYTES", "HEVO_MCP_ALLOWED_HOSTS",
                                                 "HEVO_MCP_ALLOWED_ORIGINS", "HEVO_MCP_SHUTDOWN_GRACE"}
    monkeypatch.setenv("HEVO_MCP_DEFAULT_SCOPE", "root")
    with pytest.raises(SystemExit, match="HEVO_MCP_DEFAULT_SCOPE"):
        Settings.from_env()
    monkeypatch.setenv("HEVO_MCP_DEFAULT_SCOPE", "read")
    monkeypatch.setenv("HEVO_MCP_MAX_BODY_BYTES", "lots")
    with pytest.raises(SystemExit, match="HEVO_MCP_MAX_BODY_BYTES"):
        Settings.from_env()


def test_every_tool_group_is_covered_by_the_contract_test():
    assert {e.tool for e in ENDPOINTS} == {t.name for t in TOOLS}
    assert BY_NAME["delete_pipeline"].tool == "pipelines_delete"


# --- helpers -----------------------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait(url):
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as h:
                await h.get(url)
            return
        except httpx.TransportError:
            await asyncio.sleep(0.1)


def _json(resp):
    return json.loads(resp.content)


# --- Read-only by default ---------------------------------------------------------------------------------

def test_the_default_scope_is_read_so_writes_need_an_opt_in(env):
    fake = FakeHevo()
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0)
    assert settings.default_scope == "read"

    async def go():
        async with Client(server(fake, settings)) as c:
            read = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1})
            write = await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})
            return read, write

    read, write = run(go())
    assert "error" not in read and write["error"] == "READ_ONLY"
    assert fake.writes() == []


# --- Risky endpoints ----------------------------------------------------------------------------------------

def test_risky_endpoints_lists_destructive_endpoints_a_wide_default_scope_exposes():
    from hevo_mcp_server.tools import risky_endpoints

    assert risky_endpoints(Settings()) == {}  # read default: no destructive call without opting in
    wide = risky_endpoints(Settings(default_scope="delete"))
    assert "delete_pipeline" in wide["destructive_by_default"] and "resync_pipeline" in wide["destructive_by_default"]
    assert all(BY_NAME[n].destructive for n in wide["destructive_by_default"])
    locked = risky_endpoints(Settings(default_scope="delete", disabled_endpoints=frozenset(wide["destructive_by_default"])))
    assert locked == {}


# --- No shared key in a hosted server -----------------------------------------------------------------------

# --- Multi region: each deployment serves its own region ----------------------------------------------------

def test_a_regional_deployment_refuses_other_regions(env):
    fake = FakeHevo()
    port = _free_port()
    settings = replace(UNLIMITED, allowed_regions=("eu", "eu2"))
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=settings)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            out = {}
            for region in ("eu", "EU2", "us"):
                t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": region})
                async with Client(t) as c:
                    out[region] = await c.call_tool("pipelines_read", {"name": "list_pipelines"},
                                                    raise_on_error=False)
            async with httpx.AsyncClient() as h:
                health = (await h.get(f"http://127.0.0.1:{port}/health-check")).json()
            return out, health
        finally:
            task.cancel()

    out, health = run(go())
    assert not out["eu"].is_error and not out["EU2"].is_error
    assert out["us"].is_error and "serves region eu, eu2" in out["us"].content[0].text
    assert [c.url.host for c in fake.calls] == ["eu.hevodata.com", "eu2.hevodata.com"]  # us never left the box
    assert health["regions"] == ["eu", "eu2"]


def test_allowed_regions_are_checked_when_settings_load(env, monkeypatch):
    monkeypatch.setenv("HEVO_MCP_ALLOWED_REGIONS", "EU, eu2")
    assert Settings.load().allowed_regions == ("eu", "eu2")
    monkeypatch.setenv("HEVO_MCP_ALLOWED_REGIONS", "eu,mars")
    with pytest.raises(SystemExit, match="mars"):
        Settings.load()


# --- Outbound User-Agent names the calling client --------------------------------------------------------------

def test_hevo_sees_which_client_called_and_the_log_records_it(env, caplog):
    from mcp_types import Implementation

    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo()

    async def go():
        async with Client(server(fake), client_info=Implementation(name="Claude Code", version="2.1")) as c:
            await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1})

    run(go())
    assert fake.calls[0].headers["User-Agent"].endswith("(stdio; claude-code)")
    assert fake.calls[0].headers["User-Agent"].startswith("hevo-mcp/")
    logs = [r.fields for r in caplog.records if getattr(r, "fields", {}).get("event") == "tool_call"]
    assert logs[0]["client"] == "claude-code"


def test_client_names_are_reduced_to_a_safe_slug():
    from hevo_mcp_server.observability import _slug

    assert _slug("Mozilla/5.0 (X11)\r\nX-Evil: 1") == "mozilla-5.0-x11-x-evil-1"
    assert _slug("") is None and _slug("!!!") is None and len(_slug("a" * 200)) == 48


def test_over_http_the_client_is_named_too(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=UNLIMITED)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "User-Agent": "Cursor/1.7"})
            async with Client(t) as c:
                await call(c, "pipelines_read", "list_pipelines")
        finally:
            task.cancel()

    run(go())
    agent = fake.calls[0].headers["User-Agent"]
    assert agent.startswith("hevo-mcp/") and "(http; " in agent and "unknown" not in agent, agent


def test_local_refusals_say_how_to_allow_changes(env):
    """A local (stdio) user has no header to send: the message names the setting they change instead."""
    from fastmcp import Client as LocalClient

    fake = FakeHevo()
    srv = create_server(remote=False, client=HevoClient(httpx.MockTransport(fake)),
                        settings=Settings(default_scope="write", rate_reads_per_minute=0, rate_writes_per_minute=0))
    ro = create_server(remote=False, client=HevoClient(httpx.MockTransport(fake)),
                       settings=Settings(default_scope="read", rate_reads_per_minute=0, rate_writes_per_minute=0))

    async def go():
        async with LocalClient(ro) as c:
            read_only = await call(c, "pipelines_write", "enable_pipeline", path_params={"pipeline_id": 1})
        async with LocalClient(srv) as c:
            too_narrow = await call(c, "pipelines_delete", "delete_pipeline", path_params={"pipeline_id": 1})
        return read_only, too_narrow

    read_only, too_narrow = run(go())
    assert read_only["error"] == "READ_ONLY" and "HEVO_MCP_DEFAULT_SCOPE to write" in read_only["message"]
    assert too_narrow["error"] == "SCOPE_EXCEEDED" and "HEVO_MCP_DEFAULT_SCOPE to delete" in too_narrow["message"]
    assert "X-Hevo-Scope" not in read_only["message"] + too_narrow["message"] and fake.writes() == []


def test_a_batch_spends_one_rate_limit_token_per_target():
    limiter = RateLimiter(reads_per_minute=0, writes_per_minute=10, clock=lambda: 0.0)
    assert limiter.check("k", True, tokens=8) is None
    assert limiter.check("k", True, tokens=5) is not None  # only 2 tokens left
    assert limiter.check("k", True, tokens=2) is None
    assert limiter.capacity(True) == 10 and limiter.capacity(False) is None


def test_a_batch_larger_than_the_write_limit_is_refused_with_how_to_split_it(env):
    """With 20 writes a minute, a batch of 21 could never pass the limiter: it must say how to split it, not
    return RATE_LIMITED with a retry time that never comes. Nothing is asked and nothing reaches Hevo."""
    fake = FakeHevo()
    port = _free_port()
    limited = Settings(rate_reads_per_minute=0, rate_writes_per_minute=20)
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=limited)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "write"})
            async with Client(t) as c:
                return await call(c, "pipelines_write", "disable_pipeline", region=None,
                                  batch=[{"pipeline_id": i} for i in range(1, 22)])
        finally:
            task.cancel()

    out = run(go())
    assert out["error"] == "INVALID_ARGUMENTS" and out["max"] == 20
    assert "batches of at most 20" in out["message"]
    assert fake.writes() == []


def test_config_numbers_must_be_whole(tmp_path):
    cfg = tmp_path / "hevo_mcp.toml"
    cfg.write_text("[timeouts]\nread_seconds = 0.5\n")
    with pytest.raises(SystemExit, match="whole number"):
        Settings.load(cfg)
    cfg.write_text("[timeouts]\nread_seconds = 45\n")
    assert Settings.load(cfg).read_timeout_seconds == 45


def test_a_key_file_behind_a_symlink_is_refused(env, tmp_path):
    from hevo_mcp_server.auth import credentials_from_env, key_file
    real = tmp_path / "elsewhere.json"
    real.write_text(key_file().read_text()); real.chmod(0o600)
    key_file().unlink(); key_file().symlink_to(real)
    with pytest.raises(Exception, match="symbolic link"):
        credentials_from_env("read", "us")


def test_a_write_answered_with_a_non_json_success_reports_an_unknown_outcome(env):
    fake = FakeHevo({("POST", "/api/v2/pipelines/3/actions/disable"): lambda r: httpx.Response(
        200, content=b"<html>ok</html>", headers={"content-type": "text/html"})})

    async def go():
        async with Client(server(fake)) as c:
            return await call(c, "pipelines_write", "disable_pipeline", path_params={"pipeline_id": 3})

    out = run(go())
    assert out["error"] == "UPSTREAM_INVALID_RESPONSE" and out["write_outcome"] == "unknown"
    assert "Read the resource back" in out["message"]


def test_a_batch_stops_after_a_429_and_lists_the_rest_as_skipped(env):
    fake = FakeHevo({("POST", "/api/v2/pipelines/2/actions/disable"): lambda r: httpx.Response(
        429, json={"message": "slow down"}, headers={"Retry-After": "1"})})

    async def go():
        async with Client(server(fake)) as c:
            return await call(c, "pipelines_write", "disable_pipeline",
                              batch=[{"pipeline_id": i} for i in (1, 2, 3, 4)])

    out = run(go())
    assert (out["succeeded"], out["failed"], out["skipped"]) == (1, 1, 2)
    assert out["batch"][1]["error"]["error"] == "UPSTREAM_RATE_LIMITED"
    assert all("HTTP 429" in r["skipped"] for r in out["batch"][2:])
    assert [c.url.path for c in fake.writes()] == ["/api/v2/pipelines/1/actions/disable",
                                                   "/api/v2/pipelines/2/actions/disable"]


def test_a_batch_spends_its_targets_against_the_write_limit(env):
    """Two batches of two with three writes a minute: the second is refused whole, and no target of it
    reaches Hevo."""
    fake = FakeHevo()
    port = _free_port()
    limited = Settings(rate_reads_per_minute=0, rate_writes_per_minute=3)
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=limited)

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        await _wait(url)
        try:
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "write"})
            async with Client(t) as c:
                first = await call(c, "pipelines_write", "disable_pipeline", region=None,
                                   batch=[{"pipeline_id": 1}, {"pipeline_id": 2}])
                second = await call(c, "pipelines_write", "disable_pipeline", region=None,
                                    batch=[{"pipeline_id": 3}, {"pipeline_id": 4}])
                return first, second
        finally:
            task.cancel()

    first, second = run(go())
    assert first.get("succeeded") == 2, first
    assert second["error"] == "RATE_LIMITED" and second["retry_after"] >= 1
    assert [c.url.path for c in fake.writes()] == ["/api/v2/pipelines/1/actions/disable",
                                                   "/api/v2/pipelines/2/actions/disable"]


def test_the_key_cache_expires_entries_and_keeps_rejections_when_full(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(tools, "KEY_CACHE_MAX", 3)
    srv = HevoServer(remote=False, settings=UNLIMITED, client=HevoClient(httpx.MockTransport(FakeHevo())))
    creds = lambda i: SimpleNamespace(caller_id=f"k{i}", base_url="https://us.hevodata.com", region="us")
    srv._note_key(creds(0), accepted=False)  # rejected: remembered for a minute
    srv._note_key(creds(1), accepted=True)
    srv._note_key(creds(2), accepted=True)
    srv._note_key(creds(3), accepted=True)  # full: the entry closest to expiry goes, not everything
    assert len(srv._key_checks) == 3
    assert srv._key_gate(creds(0))["error"] == "UPSTREAM_UNAUTHORIZED"  # the rejection survived
    now[0] += 61  # the rejection expires; accepted keys are still remembered
    assert srv._key_gate(creds(0)) is None
    srv._note_key(creds(4), accepted=True)
    assert ("k0", "https://us.hevodata.com") not in srv._key_checks
    assert len(srv._key_checks) == 3
