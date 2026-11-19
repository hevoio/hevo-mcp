import asyncio
import base64
import json
import logging
import socket

import httpx2 as httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.middleware import Middleware

from hevo_mcp_server import coralogix, observability
from hevo_mcp_server.auth import REGIONS, HevoCredentials, HevoKeyVerifier, encode_basic
from hevo_mcp_server.catalog import (
    BY_NAME,
    ENDPOINTS,
    PRODUCT_PREFIX,
    TOOLS,
    load_schema,
)
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.observability import RejectedRequestLogging
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server
from tests.helpers import FakeHevo, call, minimal_body, run

TOKEN = encode_basic("key-alice-1234", "secret-alice")
HELPERS = {"list_endpoints", "get_schema", "hevo_whoami"}


@pytest.fixture(autouse=True)
def _full_scope(request, monkeypatch):
    """These tests exercise writes and deletes, so they opt in to the full scope (the default is read)."""
    if "env" in request.fixturenames:
        request.getfixturevalue("env")
        monkeypatch.setenv("HEVO_MCP_DEFAULT_SCOPE", "delete")


def local_server(fake, **kwargs):
    return create_server(remote=False, client=HevoClient(httpx.MockTransport(fake)), **kwargs)


def test_tools_are_grouped_by_resource_and_action(env):
    async def go():
        async with Client(local_server(FakeHevo())) as c:
            return {t.name: t for t in await c.list_tools()}

    tools = run(go())
    assert set(tools) == HELPERS | {t.name for t in TOOLS}
    assert len(tools) < 30  # the point of grouping: the list stays small as Hevo adds endpoints
    for group in TOOLS:
        assert group.name == f"{PRODUCT_PREFIX.get(group.product, '')}{group.resource}_{group.mode}"
        tool = tools[group.name]
        assert tool.input_schema["properties"]["name"]["enum"] == list(group.endpoints)
        assert tool.annotations.read_only_hint is (group.action == "read")
        assert ("fetch_all" in tool.input_schema["properties"]) is (group.action == "read")
        # The hint is honest per tool: only the destructive and delete tools carry it.
        assert tool.annotations.destructive_hint is (group.mode in ("destructive", "delete"))
    # Writes that lose or re-ingest data live in their own tool, so "always allow pipelines_write" in a
    # client never covers a resync, and the write tool's destructiveHint is false.
    assert set(tools["pipelines_destructive"].input_schema["properties"]["name"]["enum"]) == {
        "cancel_pipeline_job", "resync_pipeline", "resync_pipeline_objects"}
    assert "resync_pipeline" not in tools["pipelines_write"].input_schema["properties"]["name"]["enum"]
    assert "DESTRUCTIVE" in tools["pipelines_destructive"].description
    assert "DESTRUCTIVE" in tools["pipelines_delete"].description


def test_every_endpoint_belongs_to_exactly_one_tool():
    served = [n for t in TOOLS for n in t.endpoints]
    assert len(served) == len(set(served))
    assert set(served) == {e.name for e in ENDPOINTS if not e.deprecated}
    for ep in ENDPOINTS:
        assert ep.tool in {t.name for t in TOOLS}
        # Edge owns the plain names; Standard's carry the product prefix, since both have pipelines.
        assert not ep.name.startswith("edge_")
        assert ep.name.startswith("standard_") == (ep.product == "standard")
        assert ep.tool.startswith("standard_") == (ep.product == "standard")
    # Writes that lose or re-ingest data are flagged, so the agent sees it before calling.
    assert BY_NAME["resync_pipeline"].destructive and BY_NAME["delete_pipeline"].destructive
    assert not BY_NAME["enable_pipeline"].destructive
    assert BY_NAME["standard_restart_pipeline_object"].destructive and BY_NAME["standard_delete_pipeline"].destructive
    # Three products: Edge pipelines, Standard pipelines, and the team's users and OAuth accounts (shared by
    # both, served once). No endpoint returns pipeline records (those must stay in the customer's region),
    # so nothing samples, previews or reads data.
    assert {e.product for e in ENDPOINTS} == {"edge", "standard", "account"}
    assert all(e.path.startswith("/api/public/v2.0/accounts/") for e in ENDPOINTS if e.product == "account")
    assert all(e.path.startswith("/api/public/v2.0/") and "/accounts/" not in e.path
               for e in ENDPOINTS if e.product == "standard")
    assert {e.tool for e in ENDPOINTS if e.product == "account"} == {"accounts_read", "accounts_write", "accounts_delete"}
    assert BY_NAME["delete_user"].approval == "always" and BY_NAME["invite_user"].approval == "always"
    assert not any(word in e.name for e in ENDPOINTS for word in ("sample", "preview", "records"))


def test_list_endpoints_and_get_schema(env):
    async def go():
        async with Client(local_server(FakeHevo())) as c:
            return (await call(c, "list_endpoints"), await call(c, "list_endpoints", search="resync"),
                    await call(c, "list_endpoints", category="metadata"),
                    await call(c, "get_schema", "create_pipeline"), await call(c, "get_schema", "nope"))

    summary, search, category, schema, unknown = run(go())
    assert summary["total"] == len(ENDPOINTS)
    assert summary["categories"]["pipelines"]["tools"] == [
        "pipelines_delete", "pipelines_destructive", "pipelines_read", "pipelines_write"]
    assert {r["name"] for r in search["endpoints"]} == {"resync_pipeline", "resync_pipeline_objects"}
    assert all(r["tool"] == "pipelines_destructive" and r["destructive"] and r["consequence"] for r in search["endpoints"])
    assert len(category["endpoints"]) == 4 and all(r["tool"] == "metadata_read" for r in category["endpoints"])
    assert schema["tool"] == "pipelines_write" and schema["request_body"]["required"] is True
    assert "source" in schema["request_body"]["schema"]["properties"]
    assert schema["docs"] == "https://edge-api-docs.hevodata.com/reference/post_api-v2-pipelines"
    assert unknown["error"] == "UNKNOWN_ENDPOINT"


def test_read_sends_basic_auth_to_region(env):
    fake = FakeHevo({("GET", "/api/v2/pipelines/7"): {"data": {"id": 7, "state": "ACTIVE"}}})

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 7})

    assert run(go())["data"]["id"] == 7
    req = fake.calls[0]
    assert str(req.url) == "https://us.hevodata.com/api/v2/pipelines/7"
    assert req.headers["authorization"] == f"Basic {TOKEN}"


def test_path_values_are_encoded_as_one_segment(env):
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            await call(c, "pipelines_read", "get_pipeline_object",
                       path_params={"pipeline_id": 3, "object_name": "../a b/c@d"})

    run(go())
    assert fake.calls[0].url.raw_path == b"/api/v2/pipelines/3/objects/..%2Fa%20b%2Fc%40d"


def test_write_runs_immediately_and_body_may_be_a_json_string(env):
    fake = FakeHevo({("POST", "/api/v2/destinations"): lambda r: httpx.Response(204)})
    dest = {"name": "wh", "connector_id": "snowflake", "config": {}}

    async def go():
        async with Client(local_server(fake)) as c:
            first = await call(c, "destinations_write", "create_destination", body=dest)
            second = await call(c, "destinations_write", "create_destination", body=json.dumps(dest))
            bad = await call(c, "destinations_write", "create_destination", body="{not json")
            return first, second, bad

    first, second, bad = run(go())
    assert first == {"status": 204} and second == {"status": 204}
    assert bad["error"] == "INVALID_BODY"
    assert [json.loads(w.content) for w in fake.writes()] == [dest, dest]
    assert fake.writes()[0].url.path == "/api/v2/destinations"


def test_dispatch_errors_are_returned_so_the_agent_can_fix_the_call(env):
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            return (await call(c, "pipelines_read", "get_nothing"),
                    await call(c, "destinations_write", "enable_pipeline", path_params={"pipeline_id": 1}),
                    await call(c, "pipelines_read", "get_pipeline"),
                    await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": ".."}))

    unknown, mismatch, missing, dots = run(go())
    assert unknown["error"] == "UNKNOWN_ENDPOINT" and len(unknown["request_id"]) == 32
    assert mismatch["error"] == "ENDPOINT_TOOL_MISMATCH" and mismatch["expected_tool"] == "pipelines_write"
    assert missing["error"] == "MISSING_PATH_PARAM" and missing["missing"] == ["pipeline_id"]
    assert dots["error"] == "MISSING_PATH_PARAM"
    assert fake.calls == []


def test_a_body_that_doesnt_fit_the_schema_is_refused_before_hevo_and_before_approval(env):
    """The agent gets the exact problems; the user is never asked to approve a request Hevo would reject."""
    fake = FakeHevo()
    prompts = []

    async def user(message, response_type, params, context):
        prompts.append(message)
        return {"decision": "approve"}

    async def go():
        async with Client(local_server(fake), elicitation_handler=user) as c:
            missing = await call(c, "pipelines_destructive", "resync_pipeline_objects",
                                 path_params={"pipeline_id": 1}, body={"objects": ["orders"]})  # the key is `object_names`
            wrong_type = await call(c, "pipelines_destructive", "resync_pipeline_objects",
                                    path_params={"pipeline_id": 1}, body={"object_names": "orders"})
            absent = await call(c, "pipelines_destructive", "resync_pipeline_objects",
                                path_params={"pipeline_id": 1})
            return missing, wrong_type, absent

    missing, wrong_type, absent = run(go())
    assert missing["error"] == "INVALID_BODY" and "'object_names' is a required property" in missing["problems"][0]
    assert wrong_type["error"] == "INVALID_BODY" and wrong_type["problems"][0].startswith("object_names:")
    assert absent["error"] == "INVALID_BODY" and "body is required" in absent["problems"][0]
    assert fake.writes() == [] and prompts == []


# A body that passes the schema check for create_pipeline; the guard tests then vary the Source.
EDGE_PIPELINE = minimal_body(load_schema("create_pipeline"), {"destination_id": 1})


def test_edge_create_guard_refuses_unknown_connector(env):
    fake = FakeHevo({("GET", "/api/v2/metadata/sources/connectors"): {"data": [{"connector_id": "stripe", "type": "STRIPE"}]}})

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_write", "create_pipeline",
                              body=EDGE_PIPELINE | {"source": {"name": "s", "connector_id": "db2", "type": "DB2", "config": {}}})

    result = run(go())
    assert result["error"] == "GUARD_REFUSED" and "db2" in result["message"]
    assert fake.writes() == []


def test_edge_create_guard_requires_schema_keys(env):
    fake = FakeHevo({
        ("GET", "/api/v2/metadata/sources/connectors"): {"data": [{"connector_id": "stripe", "type": "STRIPE"}]},
        ("GET", "/api/v2/metadata/sources/connectors/stripe"): {"data": {"schema": {
            "properties": {"config": {"required": ["api_key"]}}}}},
    })

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_write", "create_pipeline",
                              body=EDGE_PIPELINE | {"source": {"name": "s", "connector_id": "stripe", "type": "STRIPE", "config": {}}})

    result = run(go())
    assert result["error"] == "GUARD_REFUSED" and "source.config.api_key" in result["message"]


def test_fetch_all_follows_the_cursor(env):
    def edge(req):
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"data": [{"id": 10}, {"id": 11}], "has_more": True, "next_cursor": "n1"})
        assert req.url.params["cursor"] == "n1"
        return httpx.Response(200, json={"data": [{"id": 12}], "has_more": False})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): edge})

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    e = run(go())
    assert [r["id"] for r in e["data"]] == [10, 11, 12]
    assert e["count"] == 3 and e["pages"] == 2


def test_hevo_4xx_comes_back_as_a_structured_error(env):
    fake = FakeHevo({
        ("GET", "/api/v2/pipelines/1"): lambda r: httpx.Response(401, json={"message": "bad key"}),
        ("GET", "/api/v2/pipelines/2"): lambda r: httpx.Response(404, json={"error": {
            "message": "no such pipeline", "error_reference_id": "ref-7"}}),
        ("POST", "/api/v2/pipelines/3/actions/sync-now"): lambda r: httpx.Response(
            429, json={"message": "slow down"}, headers={"Retry-After": "7"}),
    })

    async def go():
        async with Client(local_server(fake)) as c:
            # The 401 comes last: a rejected key is then refused locally for a minute (see the key-gate test).
            not_found = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 2})
            limited = await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 3})
            unauthorized = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1})
            return unauthorized, not_found, limited

    unauthorized, not_found, limited = run(go())
    assert unauthorized["error"] == "UPSTREAM_UNAUTHORIZED" and unauthorized["status"] == 401
    assert "another region" in unauthorized["message"] and "bad key" in unauthorized["message"]
    assert not_found["error"] == "UPSTREAM_NOT_FOUND" and not_found["error_reference_id"] == "ref-7"
    assert limited["error"] == "UPSTREAM_RATE_LIMITED" and limited["retry_after"] == "7"
    assert limited["upstream"] == {"message": "slow down"}


def test_hevo_5xx_is_a_shaped_error_and_writes_are_not_retried(env):
    fake = FakeHevo({("POST", "/api/v2/pipelines/1/actions/sync-now"): lambda r: httpx.Response(503)})

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})

    out = run(go())
    assert out["error"] == "UPSTREAM_ERROR" and out["status"] == 503 and len(out["request_id"]) == 32
    assert out["write_outcome"] == "unknown" and "Read the resource back" in out["message"]
    assert len(fake.writes()) == 1


def test_one_http_client_is_shared_across_calls(env):
    fake = FakeHevo()
    client = HevoClient(httpx.MockTransport(fake))

    async def go():
        async with Client(create_server(remote=False, client=client)) as c:
            await call(c, "pipelines_read", "list_pipelines")
            first = client._http
            await call(c, "destinations_read", "list_destinations")
            assert client._http is first and first is not None
        return client._http

    assert run(go()) is None  # closed by the server's lifespan
    assert len(fake.calls) == 2


def test_verifier_accepts_only_key_secret_tokens():
    v = HevoKeyVerifier()
    assert run(v.verify_token(TOKEN)) is not None
    assert run(v.verify_token("not-base64!")) is None
    assert run(v.verify_token(base64.b64encode(b"no-colon").decode())) is None


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_remote_mode_uses_each_callers_own_key(env, caplog):
    # The operator's own key file (the env fixture) must never be used for remote callers.
    fake = FakeHevo()
    port = _free_port()
    server = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)))
    bob = encode_basic("key-bob-5678", "secret-bob")

    async def go():
        task = asyncio.create_task(server.run_http_async(
            host="127.0.0.1", port=port, show_banner=False, middleware=[Middleware(RejectedRequestLogging)],
            host_origin_protection="auto", stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as h:
                    await h.get(url)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        try:
            # No token: rejected before any tool runs.
            async with httpx.AsyncClient() as h:
                r = await h.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"accept": "application/json, text/event-stream"})
                assert r.status_code == 401
                r = await h.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                 headers={"accept": "application/json, text/event-stream",
                                          "authorization": "Bearer not-a-key", "user-agent": "probe/1",
                                          "x-forwarded-for": "1.2.3.4"})
                assert r.status_code == 401
                # Health check answers without a key, for load balancers.
                health = await h.get(f"http://127.0.0.1:{port}/health-check")
                assert health.status_code == 200 and health.json()["status"] == "ok"
                # DNS-rebinding protection: a localhost-bound server rejects foreign Host headers.
                rebind = await h.get(f"http://127.0.0.1:{port}/health-check", headers={"host": "evil.example"})
                assert rebind.status_code == 421

            for token, region in ((TOKEN, "eu"), (bob, "asia")):
                transport = StreamableHttpTransport(url, auth=token, headers={"X-Hevo-Region": region})
                async with Client(transport) as c:
                    await call(c, "pipelines_read", "list_pipelines", region=None)  # the header's region
                    # Stateless: the server hands out no session id, so any replica can serve the next call.
                    assert transport.get_session_id() is None

            transport = StreamableHttpTransport(url, auth=bob, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "read"})
            async with Client(transport) as c:
                refused = await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1},
                                     region=None)
                assert refused["error"] == "READ_ONLY"
        finally:
            task.cancel()

    run(go())
    assert [(str(c.url.host), c.headers["authorization"]) for c in fake.calls] == [
        ("eu.hevodata.com", f"Basic {TOKEN}"),
        ("asia.hevodata.com", f"Basic {bob}"),
    ]
    assert fake.writes() == []
    # The startup probe (GET) is rejected too; check the two POSTs.
    rejected = [f for f in _records(caplog, "hevo_mcp") if f["event"] == "auth_rejected" and f["method"] == "POST"]
    assert [(f["reason"], f["status"], f["path"]) for f in rejected] == [
        ("missing_token", 401, "/mcp"), ("invalid_token", 401, "/mcp")]
    assert rejected[1]["user_agent"] == "probe/1" and "not-a-key" not in _all_log_text(caplog)
    # The raw header is kept in its own field; the address is uvicorn's, which honours the header only from a
    # trusted proxy (localhost here, so this one counts).
    assert rejected[1]["forwarded_for"] == "1.2.3.4" and rejected[0]["forwarded_for"] is None


def _records(caplog, logger):
    return [r.fields for r in caplog.records if r.name == logger and hasattr(r, "fields")]


def _all_log_text(caplog):
    formatter = observability.JsonFormatter()
    return "\n".join(formatter.format(r) for r in caplog.records)


def test_tool_call_is_logged_and_request_id_sent_to_hevo(env, caplog):
    caplog.set_level("DEBUG", logger="hevo_mcp")
    fake = FakeHevo({("GET", "/api/v2/pipelines/7"): {"data": {"id": 7}}})

    async def go():
        async with Client(local_server(fake)) as c:
            await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 7})

    run(go())
    [rec] = [f for f in _records(caplog, "hevo_mcp") if f["event"] == "tool_call"]
    assert rec["tool"] == "pipelines_read" and rec["endpoint"] == "get_pipeline" and rec["outcome"] == "ok"
    assert rec["region"] == "us" and rec["status"] == 200 and rec["hevo_calls"] == 1
    assert rec["path"] == "/api/v2/pipelines/{pipeline_id}"  # the template, not the values
    assert rec["caller"] == HevoCredentials(TOKEN, "", "us").caller_id
    req = fake.calls[0]
    assert req.headers["x-request-id"] == rec["request_id"]
    assert req.headers["user-agent"].startswith("hevo-mcp/")
    assert "secret-alice" not in _all_log_text(caplog) and TOKEN not in _all_log_text(caplog)


def test_a_cancelled_write_is_audited_as_unknown_not_ok(env, caplog):
    """The client giving up (disconnect, Ctrl-C) while Hevo may still be working is never recorded as ok."""
    import asyncio

    from hevo_mcp_server import observability

    caplog.set_level("INFO", logger="hevo_mcp")
    middleware = observability.ToolCallLogging()
    context = type("Ctx", (), {"message": type("Msg", (), {
        "name": "pipelines_write", "arguments": {"name": "enable_pipeline", "path_params": {"pipeline_id": 1}}})(),
        "fastmcp_context": None})()

    async def hevo_still_working(_):
        raise asyncio.CancelledError

    async def go():
        try:
            await middleware.on_call_tool(context, hevo_still_working)
        except asyncio.CancelledError:
            return "cancelled"

    assert run(go()) == "cancelled"
    [entry] = _records(caplog, "hevo_mcp.audit")
    assert entry["outcome"] == "cancelled" and entry["write_outcome"] == "unknown"
    [call_record] = [r for r in _records(caplog, "hevo_mcp") if r.get("event") == "tool_call"]
    assert call_record["outcome"] == "cancelled" and call_record["error"] == "CancelledError"


def test_the_approval_decision_travels_to_hevo_with_the_call(env):
    """Hevo's API logs can show that the user approved a change: the header goes only on approved calls."""
    fake = FakeHevo({("PATCH", "/api/v2/pipelines/9/sources"): {"code": "Success", "data": {}}})

    async def go():
        async with Client(local_server(fake)) as c:
            await call(c, "pipelines_write", "update_pipeline_sources",
                       path_params={"pipeline_id": 9}, body={"config": {"user": "u"}})
            await call(c, "pipelines_read", "list_pipelines")

    run(go())
    approved, listed = fake.writes()[0], fake.calls[-1]
    assert approved.headers["X-Hevo-Mcp-Approval"] == "approved" and "X-Hevo-Mcp-Approval" not in listed.headers


def test_writes_are_audited_without_bodies(env, caplog):
    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo({("PATCH", "/api/v2/pipelines/9/sources"): {"code": "Success", "data": {}}})

    async def go():
        async with Client(local_server(fake)) as c:
            await call(c, "pipelines_write", "update_pipeline_sources",
                       path_params={"pipeline_id": 9}, body={"config": {"password": "fake-password"}})

    run(go())
    # Two records: the approval prompt sent to the client, then the call made after the user approved it.
    asked, entry = _records(caplog, "hevo_mcp.audit")
    assert asked["outcome"] == "awaiting_approval" and asked["approval"] == "requested"
    assert entry["tool"] == "pipelines_write" and entry["endpoint"] == "update_pipeline_sources"
    assert entry["outcome"] == "ok" and entry["params"] == {"pipeline_id": 9} and entry["approval"] == "approved"
    assert "fake-password" not in _all_log_text(caplog)


def test_refused_and_failed_calls_are_logged_with_request_id(env, caplog, no_wait):
    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo({("GET", "/api/v2/pipelines/1"): lambda r: httpx.Response(
        500, json={"message": "boom", "error_reference_id": "ref-42"})})

    async def go():
        async with Client(local_server(fake)) as c:
            failed = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1})
            assert failed["error"] == "UPSTREAM_ERROR" and failed["error_reference_id"] == "ref-42"
            refused = await call(c, "pipelines_destructive", "resync_pipeline_objects",
                                 path_params={"pipeline_id": 4}, body={"object_names": "orders"})
            assert refused["error"] == "INVALID_BODY"

    run(go())
    failed, refused = [f for f in _records(caplog, "hevo_mcp") if f["event"] == "tool_call"]
    assert failed["outcome"] == "error" and failed["status"] == 500
    assert failed["error_reference_id"] == "ref-42" and failed["retries"] == 2
    assert refused["outcome"] == "refused" and refused["error"] == "INVALID_BODY" and refused["hevo_calls"] == 0
    [entry] = _records(caplog, "hevo_mcp.audit")
    assert entry["endpoint"] == "resync_pipeline_objects" and entry["outcome"] == "refused"
    assert entry["params"] == {"pipeline_id": 4}
    assert fake.writes() == []


def _shipper(fake, **kwargs):
    handler = coralogix.CoralogixHandler(
        "cx-key", "https://ingress.coralogix.in/logs/v1/singles", "hevo-mcp", "http",
        transport=httpx.MockTransport(fake), flush_seconds=60, **kwargs)
    handler.setFormatter(observability.JsonFormatter())
    return handler


def _record(level, **fields):
    record = logging.LogRecord("hevo_mcp", level, __file__, 1, fields["event"], None, None)
    record.fields = fields
    return record


def test_coralogix_receives_log_records():
    fake = FakeHevo({("POST", "/logs/v1/singles"): {}})
    handler = _shipper(fake)
    handler.emit(_record(logging.INFO, event="tool_call", tool="pipelines_read"))
    handler.emit(_record(logging.WARNING, event="auth_rejected", reason="missing_token"))
    handler.close()
    [req] = fake.calls
    assert str(req.url) == "https://ingress.coralogix.in/logs/v1/singles"
    assert req.headers["authorization"] == "Bearer cx-key"
    first, second = json.loads(req.content)
    assert (first["applicationName"], first["subsystemName"], first["severity"]) == ("hevo-mcp", "http", 3)
    assert json.loads(first["text"])["tool"] == "pipelines_read"
    assert second["severity"] == 4


def test_coralogix_stops_when_key_is_rejected(capsys):
    fake = FakeHevo({("POST", "/logs/v1/singles"): lambda r: httpx.Response(403)})
    handler = _shipper(fake)
    handler.emit(_record(logging.INFO, event="tool_call"))
    handler.flush()
    handler.emit(_record(logging.INFO, event="tool_call"))
    handler.close()
    assert len(fake.calls) == 1 and not handler.active
    assert "rejected the key" in capsys.readouterr().err


def test_coralogix_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("CORALOGIX_PRIVATE_KEY", raising=False)
    assert coralogix.handler_from_env(observability.JsonFormatter(), "stdio") is None
    assert coralogix.logs_url("eu2.coralogix.com") == "https://ingress.eu2.coralogix.com/logs/v1/singles"


# --- One server, several regions ------------------------------------------------------------------------

def _key(folder, name, key, secret):
    (folder / name).write_text(json.dumps({"api_key": key, "secret_key": secret}))
    (folder / name).chmod(0o600)


def test_over_http_a_call_can_name_a_region_within_the_servers_allowed_ones(env):
    fake = FakeHevo()
    port = _free_port()
    srv = create_server(remote=True, auth=HevoKeyVerifier(), client=HevoClient(httpx.MockTransport(fake)),
                        settings=Settings(rate_reads_per_minute=0, allowed_regions=("eu", "eu2")))

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      stateless_http=True))
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as h:
                    await h.get(url)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        try:
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "eu"})
            async with Client(t) as c:
                eu2 = await call(c, "pipelines_read", "list_pipelines", region="eu2")
                us = await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "us"},
                                       raise_on_error=False)
                return eu2, us
        finally:
            task.cancel()

    eu2, us = run(go())
    assert "error" not in eu2 and us.is_error and "serves region eu, eu2" in us.content[0].text
    assert [r.url.host for r in fake.calls] == ["eu2.hevodata.com"]


# --- Fake keys can't turn the server into a relay -------------------------------------------------------

def test_a_rejected_key_is_refused_locally_and_unknown_keys_share_one_limit(env):
    fake = FakeHevo({("GET", "/api/v2/pipelines"): lambda r: httpx.Response(401, json={"message": "bad key"})})
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, rate_unverified_per_minute=2)
    srv = create_server(remote=False, client=HevoClient(httpx.MockTransport(fake)), settings=settings)

    async def go():
        async with Client(srv) as c:
            return [await call(c, "pipelines_read", "list_pipelines") for _ in range(3)]

    first, second, _ = run(go())
    assert first["error"] == "UPSTREAM_UNAUTHORIZED" and "bad key" in first["message"]
    assert second["error"] == "UPSTREAM_UNAUTHORIZED" and "a moment ago" in second["message"]  # not sent again
    assert len(fake.calls) == 1  # both later calls answered locally


def test_unknown_keys_share_a_small_limit_until_hevo_accepts_one(env, monkeypatch):
    fake = FakeHevo()
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, rate_unverified_per_minute=2)
    srv = create_server(remote=False, client=HevoClient(httpx.MockTransport(fake)), settings=settings)

    async def go():
        async with Client(srv) as c:
            out = []
            for i in range(3):  # three different keys Hevo has never seen
                _key(env, "hevo-key.json", f"key-new-{i:04d}", "secret")
                out.append(await call(c, "pipelines_read", "list_pipelines"))
            _key(env, "hevo-key.json", "key-new-0000", "secret")  # accepted on its first call: no longer limited
            out.append(await call(c, "pipelines_read", "list_pipelines"))
            return out

    first, second, third, again = run(go())
    assert "error" not in first and "error" not in second
    assert third["error"] == "RATE_LIMITED"  # the third unknown key in a minute waits
    assert "error" not in again


def test_a_key_file_other_users_can_read_is_refused(env, monkeypatch, tmp_path):
    import os

    key = tmp_path / "hevo-key.json"
    key.write_text(json.dumps({"api_key": "key-file-0001", "secret_key": "secret"}))
    os.chmod(key, 0o644)
    monkeypatch.setenv("HEVO_KEYS_DIR", str(tmp_path))
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            return await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "us"}, raise_on_error=False)

    out = run(go())
    assert out.is_error and "chmod 600" in out.content[0].text and fake.calls == []
    os.chmod(key, 0o600)
    assert "error" not in (run(go()).structured_content or {})


def test_one_key_file_serves_every_production_region_and_the_region_is_required(env):
    """The key is ~/.hevo/hevo-key.json (or $HEVO_KEYS_DIR/hevo-key.json), one for all production regions. No
    region is assumed: a call without one tells the agent to ask the user, and hevo_whoami without one reports
    the setup instead of guessing."""
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            for region in ("us", "EU", "us2"):
                assert "error" not in await call(c, "pipelines_read", "list_pipelines", region=region)
            unknown = await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "mars"}, raise_on_error=False)
            missing = await c.call_tool("pipelines_read", {"name": "list_pipelines"}, raise_on_error=False)
            setup = (await c.call_tool("hevo_whoami", {})).structured_content
            who = (await c.call_tool("hevo_whoami", {"region": "eu"})).structured_content
            tools = {t.name: t for t in await c.list_tools()}
            return unknown, missing, setup, who, tools

    unknown, missing, setup, who, tools = run(go())
    basic = "Basic " + encode_basic("key-alice-1234", "secret-alice")
    assert [(r.url.host, r.headers["authorization"]) for r in fake.calls[:3]] == [
        ("us.hevodata.com", basic), ("eu.hevodata.com", basic), ("us2.hevodata.com", basic)]
    assert unknown.is_error and "Unknown Hevo region 'mars'" in unknown.content[0].text
    assert missing.is_error and "Ask the user which Hevo region" in missing.content[0].text
    assert setup["region"] is None and setup["key_file_found"] and setup["key_file"].endswith("hevo-key.json")
    assert setup["secrets_dir"] == str(env / "secrets") and "Ask the user" in setup["next"]
    assert who["region"] == "eu" and who["key_from"] == "hevo-key.json" and who["regions"] == list(REGIONS)
    assert "Required; ask the user" in tools["pipelines_read"].input_schema["properties"]["region"]["description"]


def test_a_missing_key_file_says_where_to_put_it(env, monkeypatch, tmp_path):
    monkeypatch.setenv("HEVO_KEYS_DIR", str(tmp_path / "empty"))
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            return await c.call_tool("pipelines_read", {"name": "list_pipelines", "region": "us"}, raise_on_error=False)

    out = run(go())
    assert out.is_error and str(tmp_path / "empty" / "hevo-key.json") in out.content[0].text
    assert "account/api-keys" in out.content[0].text and fake.calls == []


def test_fetch_all_follows_both_cursor_styles(env):
    """Standard pages with `pagination.starting_after`, Edge with `has_more`/`next_cursor`."""
    def standard(req):
        if "starting_after" not in req.url.params:
            return httpx.Response(200, json={"data": [{"id": 1}, {"id": 2}], "pagination": {"starting_after": "abc"}})
        return httpx.Response(200, json={"data": [{"id": 3}], "pagination": {}})

    def edge(req):
        if "cursor" not in req.url.params:
            return httpx.Response(200, json={"data": [{"id": 10}], "has_more": True, "next_cursor": "n1"})
        return httpx.Response(200, json={"data": [{"id": 11}], "has_more": False})

    fake = FakeHevo({("GET", "/api/public/v2.0/pipelines"): standard, ("GET", "/api/v2/pipelines"): edge})

    async def go():
        async with Client(local_server(fake)) as c:
            s = await call(c, "standard_pipelines_read", "standard_list_pipelines", fetch_all=True)
            e = await call(c, "pipelines_read", "list_pipelines", fetch_all=True)
            return s, e

    s, e = run(go())
    assert [r["id"] for r in s["data"]] == [1, 2, 3] and [r["id"] for r in e["data"]] == [10, 11]
    assert s["count"] == 3 and s["pages"] == 2


def test_standard_delete_requires_explicit_drop_resource(env):
    """A Standard pipeline delete must say whether the Destination tables go too; the user is asked first."""
    fake = FakeHevo({("DELETE", "/api/public/v2.0/pipelines/4"): lambda r: httpx.Response(204)})

    async def user(message, response_type, params, context):
        return {"decision": "approve"}

    async def go():
        async with Client(local_server(fake, settings=Settings(default_scope="delete")), elicitation_handler=user) as c:
            refused = await call(c, "standard_pipelines_delete", "standard_delete_pipeline", path_params={"id": 4})
            done = await call(c, "standard_pipelines_delete", "standard_delete_pipeline", path_params={"id": 4},
                              query={"dropResource": False})
            return refused, done

    refused, done = run(go())
    assert refused["error"] == "GUARD_REFUSED" and "dropResource" in refused["message"]
    assert done == {"status": 204}
    [delete] = fake.writes()
    assert delete.url.params["dropResource"] == "false"


def test_cookies_never_travel_between_callers(env):
    """A shared client keeps no cookie jar: a Set-Cookie from one call is not sent with the next."""
    seen: list[str | None] = []

    def handler(req):
        seen.append(req.headers.get("cookie"))
        return httpx.Response(200, json={"data": []}, headers={"set-cookie": "session=caller-a"})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): handler})

    async def go():
        async with Client(local_server(fake)) as c:
            await call(c, "pipelines_read", "list_pipelines")
            await call(c, "pipelines_read", "list_pipelines")

    run(go())
    assert seen == [None, None]


def test_a_redirect_is_an_error_not_a_success(env):
    fake = FakeHevo({("POST", "/api/v2/pipelines/1/actions/enable"): lambda r: httpx.Response(302, headers={"location": "https://elsewhere.example"})})

    async def user(message, response_type, params, context):
        return {"decision": "approve"}

    async def go():
        async with Client(local_server(fake, settings=Settings(default_scope="write")), elicitation_handler=user) as c:
            return await call(c, "pipelines_write", "enable_pipeline", path_params={"pipeline_id": 1})

    out = run(go())
    assert out["error"] == "UPSTREAM_ERROR" and out["status"] == 302


def test_fetch_all_and_a_body_are_refused_where_they_make_no_sense(env):
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            single = await call(c, "pipelines_read", "get_pipeline", path_params={"pipeline_id": 1}, fetch_all=True)
            with_body = await call(c, "pipelines_read", "list_pipelines", body={"state": "Initialized"})
            return single, with_body

    single, with_body = run(go())
    assert single["error"] == "INVALID_ARGUMENTS" and "fetch_all" in single["message"]
    assert with_body["error"] == "INVALID_ARGUMENTS" and "query" in with_body["message"]
    assert fake.calls == []


def test_the_create_guard_runs_before_approval_and_names_a_missing_destination(env):
    fake = FakeHevo({
        ("GET", "/api/v2/metadata/sources/connectors"): {"data": [{"connector_id": "stripe", "type": "STRIPE"}]},
        ("GET", "/api/v2/metadata/sources/connectors/stripe"): {"data": {"schema": {"type": "object", "properties": {"config": {"type": "object"}}}}},
        ("GET", "/api/v2/destinations/9"): lambda r: httpx.Response(404, json={"message": "no such destination"}),
    })
    prompts = []

    async def user(message, response_type, params, context):
        prompts.append(message)
        return {"decision": "approve"}

    async def go():
        async with Client(local_server(fake, settings=Settings(default_scope="write")), elicitation_handler=user) as c:
            return await call(c, "pipelines_write", "create_pipeline", body=minimal_body(load_schema("create_pipeline"), {
                "source": {"name": "s", "connector_id": "stripe", "type": "STRIPE", "config": {}}, "destination_id": 9}))

    out = run(go())
    assert out["error"] == "GUARD_REFUSED" and "destination_id 9" in out["message"]
    assert prompts == [] and fake.writes() == []


def test_the_approval_prompt_shows_where_a_request_connects_in_full(env):
    """A long body can't push the host out of the part of the prompt the user sees."""
    from hevo_mcp_server import approval

    body = {"name": "wh", "connector_id": "snowflake",
            "config": {"notes": "x" * 5000, "account_url": "acme-xy12345.snowflakecomputing.com", "database_name": "ANALYTICS",
                       "database_password": "$secret_file:snowflake-prod"}}
    text = approval.prompt(BY_NAME["create_destination"], "us", "/api/v2/destinations", {}, body)
    assert "Connects to: config.account_url=acme-xy12345.snowflakecomputing.com, config.database_name=ANALYTICS" in text
    assert "database_password" not in text.split("Connects to:")[1].split("Body:")[0]
    assert text.index("Connects to:") < text.index("Body:")


def test_fetch_all_stops_when_pages_stay_empty_but_claim_more(env):
    """An API that keeps answering "has_more" with no rows and a fresh cursor can't spin forever."""
    def edge(req):
        n = int(req.url.params.get("cursor", "0"))
        return httpx.Response(200, json={"data": [], "has_more": True, "next_cursor": str(n + 1)})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): edge})
    settings = Settings(max_pages=4, rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(create_server(remote=False, settings=settings, client=HevoClient(httpx.MockTransport(fake), settings=settings))) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert out["count"] == 0 and out["pages"] == 1 and "truncated" not in out  # an empty page ends the walk
    assert len(fake.calls) == 1


def test_fetch_all_stops_on_a_repeated_cursor(env):
    fake = FakeHevo({("GET", "/api/v2/pipelines"): lambda r: httpx.Response(
        200, json={"data": [{"id": 1}], "has_more": True, "next_cursor": "same"})})

    async def go():
        async with Client(local_server(fake)) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert out["truncated_reason"] == "cursor_repeated" and out["next_cursor"] == "same" and out["count"] == 2


def test_fetch_all_hands_back_the_rows_that_fit_and_a_cursor_under_the_size_cap(env):
    """A full list above the result cap used to come back as RESULT_TOO_LARGE with no rows and no cursor."""
    def edge(req):
        n = int(req.url.params.get("cursor", "0"))
        rows = [{"id": n * 100 + i, "pad": "x" * 100} for i in range(10)]
        return httpx.Response(200, json={"data": rows, "has_more": True, "next_cursor": str(n + 1)})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): edge})
    settings = Settings(max_result_bytes=4000, rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(create_server(remote=False, settings=settings, client=HevoClient(httpx.MockTransport(fake), settings=settings))) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert "error" not in out and out["truncated"] and out["truncated_reason"] == "size"
    assert out["count"] >= 10 and out["next_cursor"] and out["cursor_param"] == "cursor"


def test_fetch_all_never_asks_for_more_rows_than_the_cap_leaves(env):
    def edge(req):
        limit = int(req.url.params["limit"])
        return httpx.Response(200, json={"data": [{"id": i} for i in range(limit)], "has_more": True,
                                         "next_cursor": req.url.params.get("cursor", "0") + "x"})

    fake = FakeHevo({("GET", "/api/v2/pipelines"): edge})
    settings = Settings(max_rows=7, rate_reads_per_minute=0, rate_writes_per_minute=0)

    async def go():
        async with Client(create_server(remote=False, settings=settings, client=HevoClient(httpx.MockTransport(fake), settings=settings))) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True, query={"limit": 5})

    out = run(go())
    assert out["count"] == 7 and out["truncated_reason"] == "cap"
    assert [int(r.url.params["limit"]) for r in fake.calls] == [5, 2]


def test_a_region_that_carries_one_product_refuses_the_other_before_hevo(env):
    """eu2 has Edge Pipelines only and us-gcp Standard Pipelines only (Hevo's regions page; checked live: the
    Edge API is absent on us-gcp and the Standard API on eu2 answers with eu's data). Account endpoints
    answer everywhere."""
    fake = FakeHevo()

    async def go():
        async with Client(local_server(fake)) as c:
            std_on_eu2 = await call(c, "standard_pipelines_read", "standard_list_pipelines", region="eu2")
            edge_on_gcp = await call(c, "pipelines_read", "list_pipelines", region="us-gcp")
            ok = [await call(c, "pipelines_read", "list_pipelines", region="eu2"),
                  await call(c, "standard_pipelines_read", "standard_list_pipelines", region="us-gcp"),
                  await call(c, "accounts_read", "list_users", region="eu2"),
                  await call(c, "accounts_read", "list_users", region="us-gcp")]
            who = (await c.call_tool("hevo_whoami", {"region": "us-gcp"})).structured_content
            return std_on_eu2, edge_on_gcp, ok, who

    std_on_eu2, edge_on_gcp, ok, who = run(go())
    assert std_on_eu2["error"] == "REGION_UNSUPPORTED" and "Edge Pipelines only" in std_on_eu2["message"]
    assert edge_on_gcp["error"] == "REGION_UNSUPPORTED" and "Standard Pipelines only" in edge_on_gcp["message"]
    assert all("error" not in r for r in ok)
    assert [r.url.host for r in fake.calls][:4] == ["eu2.hevodata.com", "us-gcp.hevodata.com",
                                                    "eu2.hevodata.com", "us-gcp.hevodata.com"]
    assert who["api_checks"]["edge"] == "not offered in this region" and who["api_checks"]["standard"] == "ok"


def test_body_problems_are_short_and_name_unknown_keys(env):
    """A config that fits several per-type shapes passes; one that fits none is reported in a sentence that
    names the shapes, never the schemas; a key the schema doesn't know is named with the allowed keys."""
    from hevo_mcp_server import validation
    redshift = {"name": "t", "type": "REDSHIFT", "config": {"db_host": "h", "db_port": 5439, "db_user": "u",
                                                             "db_password": "$secret_file:x", "schema_name": "public"}}
    assert validation.body_problems("standard_create_destination", redshift) == []
    [problem] = validation.body_problems("standard_update_object_query_mode", {"config": "bulk"})
    assert problem.startswith("config: fits none of the shapes ") and "unique_incrementing" in problem
    assert len(problem) < 400 and "{'type'" not in problem
    assert validation.body_problems("standard_update_auto_mapping", {"auto_mapping": "DISABLED"}) == [
        "body: unknown key 'auto_mapping' (allowed: state)"]
    assert validation.body_problems("standard_update_model_status", {"status": "ACTIVE"}) == [
        "body: unknown key 'status' (allowed: state)"]
    assert validation.body_problems("standard_update_pipeline_position", {"position": "2026-11-01"}) == [
        "body: unknown key 'position' (allowed: file_name, offset)"]
