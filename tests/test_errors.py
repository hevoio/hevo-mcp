"""Error handling: protocol errors, unknown methods, invalid arguments, internal failures and timeouts.

Every failure a tool reports is a result shaped {"error", "message", "request_id"} with isError true;
protocol-level problems are JSON-RPC errors from the MCP layer.
"""

import asyncio
import json
import socket

import httpx2 as httpx
from fastmcp import Client

from hevo_mcp_server import auth
from hevo_mcp_server.auth import HevoKeyVerifier, encode_basic
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.main import http_run_options
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server
from tests.helpers import FakeHevo, call, run

TOKEN = encode_basic("key-err-1234", "secret-err")
FAST = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete")


def local(fake=None, settings=FAST, transport=None):
    transport = transport if transport is not None else httpx.MockTransport(fake or FakeHevo())
    return create_server(remote=False, settings=settings, client=HevoClient(transport, settings=settings))


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _rpc(url, body, **headers):
    base = {"authorization": f"Bearer {TOKEN}", "accept": "application/json, text/event-stream",
            "content-type": "application/json", "x-hevo-region": "us"}
    async with httpx.AsyncClient(timeout=10) as h:
        r = await h.post(url, content=json.dumps(body), headers={**base, **headers})
    data = [line[5:] for line in r.text.splitlines() if line.startswith("data:") and line[5:].strip()]
    return r.status_code, json.loads(data[0] if data else r.text)


async def _with_http_server(fn):
    srv = create_server(remote=True, auth=HevoKeyVerifier(), settings=FAST,
                        client=HevoClient(httpx.MockTransport(FakeHevo()), settings=FAST))
    port = _port()
    task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                  **http_run_options(FAST)))
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as h:
                await h.get(url)
            break
        except httpx.TransportError:
            await asyncio.sleep(0.1)
    try:
        return await fn(url)
    finally:
        task.cancel()


# --- 1. Protocol version and 2. capabilities that weren't negotiated ----------------------------------

def test_protocol_version_and_unknown_methods_get_protocol_errors(env):
    async def go(url):
        init = await _rpc(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        header = await _rpc(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            **{"mcp-protocol-version": "1999-01-01"})
        unknown = await _rpc(url, {"jsonrpc": "2.0", "id": 3, "method": "foo/bar", "params": {}})
        sampling = await _rpc(url, {"jsonrpc": "2.0", "id": 4, "method": "sampling/createMessage", "params": {}})
        bad_jsonrpc = await _rpc(url, {"jsonrpc": "1.0", "id": 5, "method": "tools/list"})
        return init, header, unknown, sampling, bad_jsonrpc

    init, header, unknown, sampling, bad_jsonrpc = run(_with_http_server(go))
    # An unknown version is answered with one the server supports; the client decides whether to go on.
    assert init[0] == 200 and init[1]["result"]["protocolVersion"] != "1999-01-01"
    assert header[0] == 400 and "error" in header[1]
    assert unknown[1]["error"]["code"] == -32601 and sampling[1]["error"]["code"] == -32601
    assert bad_jsonrpc[0] == 400 and "error" in bad_jsonrpc[1]


# --- 3. Invalid tool arguments ---------------------------------------------------------------------------

def test_invalid_group_tool_arguments_are_refused_before_hevo(env):
    fake = FakeHevo()

    async def go():
        async with Client(local(fake)) as c:
            typo = await call(c, "pipelines_read", "list_pipelines", querry={"status": "ACTIVE"})
            missing = await call(c, "pipelines_read")
            wrong = await c.call_tool("pipelines_read", {"name": 5, "query": "x", "fetch_all": "yes"},
                                      raise_on_error=False)
            return typo, missing, wrong

    typo, missing, wrong = run(go())
    assert typo["error"] == "INVALID_ARGUMENTS" and "unknown argument 'querry'" in typo["problems"][0]
    assert missing["error"] == "INVALID_ARGUMENTS" and "'name' is required" in missing["problems"]
    assert wrong.is_error and wrong.structured_content["error"] == "INVALID_ARGUMENTS"
    assert len(wrong.structured_content["problems"]) == 3
    assert fake.calls == []


def test_invalid_helper_tool_arguments_are_shaped_too(env):
    async def go():
        async with Client(local()) as c:
            return await call(c, "get_schema", None, bogus=1)

    out = run(go())
    assert out["error"] == "INVALID_ARGUMENTS" and len(out["request_id"]) == 32
    assert any("bogus" in p for p in out["problems"]) and any("name" in p for p in out["problems"])


def test_every_shaped_error_is_flagged_is_error(env):
    async def go():
        async with Client(local()) as c:
            r = await c.call_tool("pipelines_read", {"name": "no_such_endpoint", "region": "us"}, raise_on_error=False)
            ok = await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "us"}, raise_on_error=False)
            return r, ok

    r, ok = run(go())
    assert r.is_error and r.structured_content["error"] == "UNKNOWN_ENDPOINT"
    assert not ok.is_error


# --- 4. Internal failures ------------------------------------------------------------------------------

def test_unexpected_exceptions_become_internal_error_without_details(env, caplog):
    caplog.set_level("INFO", logger="hevo_mcp")

    def boom(request):
        raise RuntimeError("secret internal detail /etc/passwd")

    async def go():
        async with Client(local(transport=httpx.MockTransport(boom))) as c:
            return await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "us"}, raise_on_error=False)

    r = run(go())
    assert r.is_error and r.structured_content["error"] == "INTERNAL_ERROR"
    assert "secret" not in json.dumps(r.structured_content) and "secret" not in r.content[0].text
    [exc] = [rec for rec in caplog.records if getattr(rec, "fields", {}).get("event") == "tool_exception"]
    assert exc.fields["exception"] == "RuntimeError" and exc.fields["request_id"] == r.structured_content["request_id"]
    [log] = [rec.fields for rec in caplog.records if getattr(rec, "fields", {}).get("event") == "tool_call"]
    assert log["outcome"] == "error" and log["error"] == "INTERNAL_ERROR"


def test_non_json_success_from_hevo_is_an_error_not_data(env):
    fake = FakeHevo({("GET", "/api/v2/pipelines"): lambda r: httpx.Response(
        200, content=b"<html>down for upgrade</html>", headers={"content-type": "text/html"})})

    async def go():
        async with Client(local(fake)) as c:
            return await call(c, "pipelines_read", "list_pipelines")

    out = run(go())
    assert out["error"] == "UPSTREAM_INVALID_RESPONSE" and out["content_type"] == "text/html"
    assert "upgrade" not in json.dumps(out)


# --- 5. Timeouts ---------------------------------------------------------------------------------------

class SlowHevo:
    """A real socket that accepts and then says nothing, so httpx's own timeouts fire."""

    def __init__(self):
        self.connections = 0

    async def handle(self, reader, writer):
        self.connections += 1
        await reader.read(65536)
        await asyncio.sleep(30)
        writer.close()


async def _against_slow_hevo(monkeypatch, settings, tool, name, **args):
    slow = SlowHevo()
    server = await asyncio.start_server(slow.handle, "127.0.0.1", 0)
    monkeypatch.setitem(auth.REGIONS, "us", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    try:
        mcp = create_server(remote=False, settings=settings, client=HevoClient(settings=settings))
        async with Client(mcp) as c:
            return await call(c, tool, name, **args), slow.connections
    finally:
        server.close()


def test_a_read_that_times_out_is_not_retried(env, monkeypatch):
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete", read_timeout_seconds=1)
    out, connections = run(_against_slow_hevo(monkeypatch, settings, "pipelines_read", "list_pipelines"))
    assert out["error"] == "TIMEOUT" and out["timeout_seconds"] == 1 and "write_outcome" not in out
    assert connections == 1


def test_a_write_that_times_out_has_an_unknown_outcome(env, monkeypatch):
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete", write_timeout_seconds=1)
    out, connections = run(_against_slow_hevo(monkeypatch, settings, "pipelines_write",
                                              "sync_now_pipeline", path_params={"pipeline_id": 1}))
    assert out["error"] == "TIMEOUT" and out["write_outcome"] == "unknown"
    assert "Read the resource back" in out["message"] and connections == 1


def test_the_read_deadline_caps_the_whole_call(env, monkeypatch):
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete", read_timeout_seconds=30,
                        read_deadline_seconds=1)
    out, _ = run(_against_slow_hevo(monkeypatch, settings, "pipelines_read", "list_pipelines"))
    assert out["error"] == "TIMEOUT" and out["timeout_seconds"] == 1


def test_unreachable_hevo_is_retried_for_reads_then_shaped(env, monkeypatch, no_wait):
    port = _port()  # nothing listens here
    monkeypatch.setitem(auth.REGIONS, "us", f"http://127.0.0.1:{port}")

    async def go():
        async with Client(create_server(remote=False, settings=FAST, client=HevoClient(settings=FAST))) as c:
            read = await call(c, "pipelines_read", "list_pipelines")
            write = await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})
            return read, write

    read, write = run(go())
    assert read["error"] == "UPSTREAM_UNREACHABLE" and read["reason"] == "ConnectError"
    # The write never reached Hevo, so its outcome is known: it didn't happen.
    assert write["error"] == "UPSTREAM_UNREACHABLE" and "write_outcome" not in write


def test_fetch_all_returns_rows_so_far_when_the_deadline_runs_out(env):
    async def pages(request):
        if "cursor" in request.url.params:
            await asyncio.sleep(5)
        return httpx.Response(200, json={"data": [{"id": 1}, {"id": 2}], "has_more": True, "next_cursor": "c2"})

    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete", read_deadline_seconds=1)

    async def go():
        async with Client(local(settings=settings, transport=httpx.MockTransport(pages))) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert out["truncated"] is True and out["truncated_reason"] == "deadline"
    assert out["count"] == 2 and out["next_cursor"] == "c2" and out["cursor_param"] == "cursor"


def test_unknown_query_keys_and_bodies_on_deletes_are_refused_before_hevo(env):
    fake = FakeHevo()

    async def go():
        async with Client(local(fake)) as c:
            misspelt = await call(c, "pipelines_read", "list_pipeline_jobs", path_params={"pipeline_id": 1},
                                  query={"stauts": ["FAILED"]})
            none_taken = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1},
                                    query={"limit": 5})
            tools_ = {t.name: t for t in await c.list_tools()}
            return misspelt, none_taken, tools_

    misspelt, none_taken, tools_ = run(go())
    assert misspelt["error"] == "INVALID_ARGUMENTS" and "'stauts'" in misspelt["message"] and "status" in misspelt["message"]
    assert none_taken["error"] == "INVALID_ARGUMENTS" and "takes no query parameters" in none_taken["message"]
    assert fake.calls == []
    # Read and delete tools offer no body (none of their endpoints takes one); write tools do.
    assert "body" not in tools_["pipelines_read"].input_schema["properties"]
    assert "body" not in tools_["pipelines_delete"].input_schema["properties"]
    assert "body" in tools_["pipelines_write"].input_schema["properties"]
    assert "region" in tools_["pipelines_read"].input_schema["required"]


def test_unknown_category_is_an_error_and_get_schema_leaves_the_response_out_by_default(env):
    async def go():
        async with Client(local(FakeHevo())) as c:
            cat = (await c.call_tool("list_endpoints", {"category": "nope"}, raise_on_error=False)).structured_content
            lean = (await c.call_tool("get_schema", {"name": "create_pipeline"})).structured_content
            full = (await c.call_tool("get_schema", {"name": "create_pipeline", "include_response": True})).structured_content
            return cat, lean, full

    cat, lean, full = run(go())
    assert cat["error"] == "UNKNOWN_CATEGORY" and "pipelines" in cat["message"]
    assert "request_body" in lean and "response" not in lean
    assert "response" in full


def test_hevos_own_message_leads_and_generic_hints_fill_only_a_silence(env):
    """A 404 with "Event Type 'x' not found" must not add "the ID may belong to another region", and a 500
    whose body says "workflow is still in draft" must not open with "an error occurred in Hevo"."""
    fake = FakeHevo({
        ("GET", "/api/public/v2.0/pipelines/838/mappings/contact_lists"): lambda r: httpx.Response(
            404, json={"success": False, "error_message": "Event Type 'contact_lists' not found for Pipeline #838"}),
        ("GET", "/api/public/v2.0/pipelines/9"): lambda r: httpx.Response(404, content=b""),
        ("POST", "/api/public/v2.0/workflows/1/run-now"): lambda r: httpx.Response(
            500, json={"success": False, "error_message": "Can not run a workflow that is still in draft."}),
        ("GET", "/api/public/v2.0/accounts/oauth/102"): lambda r: httpx.Response(
            500, json={"success": False, "error_message": "java.lang.String cannot be cast to java.lang.Long",
                       "error_ticket": "ZoBCfEP5"}),
    })

    async def go():
        async with Client(local(fake)) as c:
            told = await call(c, "standard_pipelines_read", "standard_get_schema_mapping",
                              path_params={"id": 838, "event_type": "contact_lists"})
            silent = await call(c, "standard_pipelines_read", "standard_get_pipeline", path_params={"id": 9})
            draft = await call(c, "standard_workflows_write", "standard_run_workflow", path_params={"id": 1})
            ticket = await call(c, "accounts_read", "get_oauth_account", path_params={"id": 102})
            return told, silent, draft, ticket

    told, silent, draft, ticket = run(go())
    # The Standard API's error_ticket is the reference support asks for, so it is named, not buried in upstream.
    assert ticket["error_reference_id"] == "ZoBCfEP5"
    assert ticket["message"].endswith("contact Hevo support with the request ID and Hevo's error ticket ZoBCfEP5 "
                                      "if the error persists.")
    assert told["message"] == ("Hevo returned HTTP 404 for GET /api/public/v2.0/pipelines/838/mappings/contact_lists: "
                               "Event Type 'contact_lists' not found for Pipeline #838.")
    assert "other product" in silent["message"]  # no detail from Hevo: the generic hint is all there is
    assert draft["message"].startswith("Hevo returned HTTP 500 for POST /api/public/v2.0/workflows/1/run-now: Can not run "
                                       "a workflow that is still in draft. If the request itself was fine, retry later")
    assert draft["message"].endswith("The write may have happened. Read the resource back before you retry.")
    assert draft["write_outcome"] == "unknown"
