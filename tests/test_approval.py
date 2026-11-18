"""Calls that need approval reach Hevo only after the user approves them in their MCP client."""

import asyncio
import json
import socket

import httpx2 as httpx
import mcp_types
import pytest
from fastmcp import Client
from helpers import FakeHevo, call, minimal_body, run

from hevo_mcp_server import approval
from hevo_mcp_server.catalog import BY_NAME, ENDPOINTS, load_schema
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server

# The agreed policy: everything else that writes needs approval; reads never do.
NO_APPROVAL = {"create_destination", "create_pipeline", "standard_create_destination", "standard_create_model",
               "standard_create_pipeline"}  # a create breaks nothing; enabling and resuming ask
UNLESS_ACTIVATING = {"standard_update_pipeline_status", "standard_update_model_status"}  # ask only when deactivating
MODES = ["legacy", "auto"]  # an older client (server-initiated prompt) and an MCP 2026-07-28 one (input request)


def server(fake, **settings):
    settings.setdefault("default_scope", "delete")  # the scope allows everything: approval still applies
    s = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, **settings)
    return create_server(remote=False, settings=s, client=HevoClient(httpx.MockTransport(fake), settings=s))


class User:
    """Answers approval prompts and remembers what it was shown."""

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def __call__(self, message, response_type, params, context):
        self.prompts.append(message)
        return self.answer


def test_the_manifest_carries_the_agreed_approval_policy():
    for ep in ENDPOINTS:
        if ep.action == "read":
            assert ep.approval == "never", ep.name
        elif ep.action == "delete":
            assert ep.approval == "always", ep.name
        elif ep.name in NO_APPROVAL:
            assert ep.approval == "never", ep.name
        elif ep.name in UNLESS_ACTIVATING:
            assert ep.approval == "unless_activating", ep.name
        else:
            assert ep.approval == "always", ep.name
    # Configuration changes, destructive actions and data loads all ask.
    for name in ("update_destination", "update_pipeline", "update_pipeline_sources",
                 "disable_pipeline", "cancel_pipeline_job", "sync_now_pipeline",
                 "resync_pipeline", "resync_pipeline_objects"):
        assert BY_NAME[name].needs_approval({}), name


def test_a_delete_can_never_be_overridden_to_skip_approval():
    import build_manifest

    with pytest.raises(SystemExit, match="always needs approval"):
        build_manifest.approval_of("edge DELETE /x", "delete", "never")
    assert build_manifest.approval_of("edge POST /x", "write", None) == "always"  # new writes ask by default


@pytest.mark.parametrize("mode", MODES)
def test_an_approved_call_reaches_hevo_and_the_prompt_shows_it(env, mode):
    fake = FakeHevo({("PATCH", "/api/v2/pipelines/9/sources"): {"code": "Success", "data": {}}})
    user = User({"decision": "approve"})

    async def go():
        async with Client(server(fake), mode=mode, elicitation_handler=user) as c:
            return await call(c, "pipelines_write", "update_pipeline_sources", region="us",
                              path_params={"pipeline_id": 9}, body={"config": {"host": "db1", "password": "pw-123"}})

    assert run(go())["code"] == "Success"
    [sent] = fake.writes()
    assert json.loads(sent.content) == {"config": {"host": "db1", "password": "pw-123"}}  # Hevo gets the real body
    [shown] = user.prompts
    assert "update_pipeline_sources" in shown and "PATCH /api/v2/pipelines/9/sources" in shown
    assert "If the Source credentials are incorrect, the Pipeline cannot read data from the Source." in shown
    assert "db1" in shown and "pw-123" not in shown  # the prompt masks secrets
    # The first line names the change, the resource and the region: Claude Code shows only that line until
    # the prompt is opened. The request itself comes last, under "Technical details".
    assert shown.splitlines()[0] == "Update Source of Edge Pipeline 9 in the us region?"
    assert shown.index("Your agent cannot approve it for you.") < shown.index("Technical details: PATCH")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("answer", [mcp_types.ElicitResult(action="decline"), mcp_types.ElicitResult(action="cancel"),
                                    {"decision": "reject"}])
def test_a_declined_call_never_reaches_hevo(env, mode, answer, caplog):
    caplog.set_level("INFO", logger="hevo_mcp")
    fake = FakeHevo()

    async def go():
        async with Client(server(fake), mode=mode, elicitation_handler=User(answer)) as c:
            return await call(c, "destinations_delete", "delete_destination",
                              path_params={"destination_id": 5})

    result = run(go())
    assert result["error"] == "APPROVAL_DECLINED" and fake.calls == []
    audit = [r.fields for r in caplog.records if r.name == "hevo_mcp.audit"][-1]
    assert audit["outcome"] == "refused" and audit["approval"] in ("declined", "cancelled")


@pytest.mark.no_auto_approve
def test_a_client_that_cant_show_prompts_gets_nothing_done(env):
    fake = FakeHevo()

    async def legacy():
        async with Client(server(fake), mode="legacy") as c:
            return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 3})

    assert run(legacy())["error"] == "APPROVAL_UNAVAILABLE"

    async def modern():
        async with Client(server(fake)) as c:
            return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 3})

    # A client on the newer protocol that declared no elicitation capability gets the same shaped error,
    # not a raw protocol failure.
    assert run(modern())["error"] == "APPROVAL_UNAVAILABLE"
    assert fake.calls == []


@pytest.mark.parametrize("mode", MODES)
def test_enabling_asks_and_creating_doesnt(env, mode):
    """Re-enabling a disabled pipeline asks (a prompt-injected re-enable never slips through); a create breaks
    nothing and runs without a prompt."""
    fake = FakeHevo()
    user = User({"decision": "reject"})

    async def go():
        async with Client(server(fake), mode=mode, elicitation_handler=user) as c:
            enable = await call(c, "pipelines_write", "enable_pipeline", path_params={"pipeline_id": 1})
            create = await call(c, "destinations_write", "create_destination",
                                body=minimal_body(load_schema("create_destination"), {"name": "d", "connector_id": "snowflake"}))
            return enable, create

    enable, create = run(go())
    assert enable["error"] == "APPROVAL_DECLINED" and "error" not in create
    assert len(user.prompts) == 1 and len(fake.writes()) == 1


def test_the_widest_scope_still_asks(env):
    fake = FakeHevo()
    user = User({"decision": "reject"})

    async def go():
        async with Client(server(fake), elicitation_handler=user) as c:
            return await call(c, "pipelines_delete", "delete_pipeline", path_params={"pipeline_id": 4})

    assert run(go())["error"] == "APPROVAL_DECLINED" and user.prompts and fake.calls == []


def test_an_approval_is_for_one_exact_call_and_only_once():
    ep = BY_NAME["sync_now_pipeline"]
    approvals = approval.Approvals()
    fingerprint = approval.call_fingerprint(ep, "us", "/api/v2/pipelines/3/actions/sync-now", {}, None)
    state = json.dumps({"call": fingerprint, "nonce": "n1", "exp": 4_000_000_000})
    other = approval.call_fingerprint(ep, "us", "/api/v2/pipelines/4/actions/sync-now", {}, None)
    assert approvals._redeem(state, other) is None  # approved pipeline 3, not 4
    assert approvals._redeem(state, fingerprint) == "n1"
    assert approvals._redeem(state, fingerprint) is None  # already used
    expired = json.dumps({"call": fingerprint, "nonce": "n2", "exp": 1})
    assert approvals._redeem(expired, fingerprint) is None


@pytest.mark.parametrize("mode,expected", [("legacy", "APPROVAL_UNAVAILABLE"), ("auto", None)])
def test_over_stateless_http(env, mode, expected):
    """Hosted servers run stateless HTTP: MCP 2026-07-28 clients get the prompt; older ones can't, so nothing
    happens rather than the call going ahead unapproved."""
    from fastmcp.client.transports import StreamableHttpTransport

    from hevo_mcp_server.auth import HevoKeyVerifier, encode_basic
    from hevo_mcp_server.main import http_run_options

    fake = FakeHevo({("POST", "/api/v2/pipelines/1/actions/sync-now"): {"code": "Accepted"}})
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="write")
    srv = create_server(remote=True, auth=HevoKeyVerifier(), settings=settings,
                        client=HevoClient(httpx.MockTransport(fake), settings=settings))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    user = User({"decision": "approve"})

    async def go():
        task = asyncio.create_task(srv.run_http_async(host="127.0.0.1", port=port, show_banner=False,
                                                      **http_run_options(settings)))
        url = f"http://127.0.0.1:{port}/mcp"
        for _ in range(50):
            try:
                async with httpx.AsyncClient() as h:
                    await h.get(url)
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)
        try:
            transport = StreamableHttpTransport(url, auth=encode_basic("k-1234", "s"), headers={"X-Hevo-Region": "us"})
            async with Client(transport, mode=mode, elicitation_handler=user) as c:
                return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})
        finally:
            task.cancel()

    result = run(go())
    if expected:
        assert result["error"] == expected and fake.writes() == []
    else:
        assert result["code"] == "Accepted" and len(user.prompts) == 1 and len(fake.writes()) == 1


def test_replicas_can_share_the_approval_key(env, monkeypatch):
    from hevo_mcp_server.tools import _approval_state_security

    assert _approval_state_security() is None  # one process: its own key
    monkeypatch.setenv("HEVO_MCP_APPROVAL_KEY", "k" * 32 + ", " + "o" * 32)
    security = _approval_state_security()
    assert security is not None and security.audience == "Hevo"
    create_server(remote=True, settings=Settings())  # builds with the shared key


def test_accepting_the_prompt_approves(env):
    """The client's Accept button is the approval: accepting the form as shown, even with no field touched,
    goes ahead; only Decline, a dismissed form or picking Reject stops it."""
    field = approval.SCHEMA["properties"]["decision"]
    assert "required" not in approval.SCHEMA and field["default"] == "approve"
    assert [o["const"] for o in field["oneOf"]] == ["approve", "reject"]
    for answer in ({}, {"decision": "approve"}):
        fake = FakeHevo({("POST", "/api/v2/pipelines/178/actions/disable"): {"code": "Accepted"}})

        async def go(fake=fake, answer=answer):
            async with Client(server(fake), mode="legacy", elicitation_handler=User(answer)) as c:
                return await call(c, "pipelines_write", "disable_pipeline",
                                  path_params={"pipeline_id": 178})

        assert run(go())["code"] == "Accepted" and len(fake.writes()) == 1, answer


@pytest.mark.parametrize("mode", MODES)
def test_a_batch_gets_one_prompt_listing_every_target_and_one_request_each(env, mode):
    """"Pause these objects": one call with `batch`, one approval prompt that lists every target, then one
    request per target with each result reported. Declining sends nothing."""
    fake = FakeHevo({("POST", f"/api/v2/pipelines/{i}/actions/disable"): {"code": "Success"} for i in (7, 9)})
    fake.routes[("POST", "/api/v2/pipelines/8/actions/disable")] = lambda req: __import__(
        "httpx2").Response(404, json={"message": "no such pipeline"})
    user = User({"decision": "approve"})
    targets = [{"pipeline_id": i} for i in (7, 8, 9)]

    async def go():
        async with Client(server(fake), mode=mode, elicitation_handler=user) as c:
            out = await call(c, "pipelines_write", "disable_pipeline", batch=targets)
            user.answer = {"decision": "reject"}
            declined = await call(c, "pipelines_write", "disable_pipeline", batch=targets[:2])
            return out, declined

    out, declined = run(go())
    assert len(user.prompts) == 2
    first = user.prompts[0]
    assert first.splitlines()[0] == "Disable Edge Pipeline (3 targets) in the us region?"
    assert "Targets:" in first and "one request for each target" in first
    assert all(f"/api/v2/pipelines/{i}/actions/disable" in first for i in (7, 8, 9))
    assert out["targets"] == 3 and out["succeeded"] == 2 and out["failed"] == 1 and out["skipped"] == 0
    assert out["batch"][1]["error"]["error"] == "UPSTREAM_NOT_FOUND" and "result" in out["batch"][2]
    assert "error" not in out  # a partly successful batch is a result, with the counts
    assert declined["error"] == "APPROVAL_DECLINED"
    assert [c.url.path for c in fake.writes()] == [f"/api/v2/pipelines/{i}/actions/disable" for i in (7, 8, 9)]


def test_a_batch_stops_after_a_key_problem_and_lists_the_rest_as_skipped(env):
    fake = FakeHevo({("POST", "/api/v2/pipelines/7/actions/disable"): lambda req: __import__(
        "httpx2").Response(403, json={"message": "no"})})

    async def go():
        async with Client(server(fake)) as c:
            return await call(c, "pipelines_write", "disable_pipeline",
                              batch=[{"pipeline_id": i} for i in (7, 8, 9)])

    out = run(go())
    assert out["failed"] == 1 and out["skipped"] == 2 and out["batch"][2]["skipped"].startswith("not sent")
    assert len(fake.writes()) == 1


def test_batch_arguments_are_checked(env):
    fake = FakeHevo()

    async def go():
        async with Client(server(fake)) as c:
            both = await call(c, "pipelines_write", "disable_pipeline",
                              path_params={"pipeline_id": 1}, batch=[{"pipeline_id": 2}])
            no_target = await call(c, "destinations_write", "create_destination", batch=[{}],
                                   body=minimal_body(load_schema("create_destination"),
                                                     {"name": "d", "connector_id": "snowflake"}))
            too_many = await call(c, "pipelines_write", "disable_pipeline",
                                  batch=[{"pipeline_id": i} for i in range(101)])
            missing = await call(c, "pipelines_destructive", "cancel_pipeline_job",
                                 batch=[{"pipeline_id": 1, "job_id": "a"}, {"pipeline_id": 1}])
            on_a_read = await c.call_tool("pipelines_read", {"name": "get_pipeline",
                                                                   "batch": [{"pipeline_id": 1}]}, raise_on_error=False)
            return both, no_target, too_many, missing, on_a_read

    both, no_target, too_many, missing, on_a_read = run(go())
    assert both["error"] == "INVALID_ARGUMENTS" and "not in path_params as well" in both["message"]
    assert no_target["error"] == "INVALID_ARGUMENTS" and "addresses a target" in no_target["message"]
    assert too_many["error"] == "INVALID_ARGUMENTS" and too_many["max"] == 100
    assert missing["error"] == "MISSING_PATH_PARAM" and missing["missing"] == ["job_id"]
    assert on_a_read.is_error and "unknown argument 'batch'" in on_a_read.content[0].text
    assert fake.writes() == []


def test_the_first_line_places_the_id_where_the_title_says_and_decodes_it():
    from hevo_mcp_server.approval import prompt
    from hevo_mcp_server.catalog import BY_NAME
    first = lambda name, path: prompt(BY_NAME[name], "us", path, {}, None).splitlines()[0]
    assert first("standard_run_pipeline", "/api/public/v2.0/pipelines/3/run-now") == "Run Standard Pipeline 3 now in the us region?"
    assert first("delete_user", "/api/public/v2.0/accounts/users/a%40example.com") == "Remove user a@example.com from team in the us region?"
    assert first("standard_pause_pipeline_object", "/api/public/v2.0/pipelines/12/objects/users/pause") == \
        "Pause object of Standard Pipeline 12 (object_name users) in the us region?"
    assert first("standard_run_pipeline", ["/a", "/b"]) == "Run Standard Pipeline (2 targets) now in the us region?"


def test_an_object_change_is_named_by_what_the_request_does():
    from hevo_mcp_server.approval import prompt
    from hevo_mcp_server.catalog import BY_NAME
    ep, path = BY_NAME["update_pipeline_objects"], "/api/v2/pipelines/7/objects"
    first = lambda body: prompt(ep, "us", path, {}, body).splitlines()[:3]
    assert first({"objects": [{"name": "a", "selected": False}, {"name": "b", "selected": False}]}) == [
        "Exclude 2 objects from Edge Pipeline 7 in the us region?", "", "Hevo stops ingesting the data of these objects."]
    assert first({"objects": [{"name": "a", "selected": True}]})[0] == "Include 1 object in Edge Pipeline 7 in the us region?"
    assert first({"objects": [{"name": "a", "load_mode": "APPEND"}]})[0] == "Update 1 object of Edge Pipeline 7 in the us region?"
    assert first({"selection_mode": "INCLUDE_ALL"})[0] == "Include all objects in Edge Pipeline 7 in the us region?"
    mixed = first({"objects": [{"name": "a", "selected": True}, {"name": "b", "selected": False}]})
    assert mixed[0] == "Include, exclude, or update objects of Edge Pipeline 7 in the us region?"


def test_nobody_answering_the_prompt_is_a_timeout_and_nothing_is_sent(env, monkeypatch):
    monkeypatch.setattr(approval, "WAIT_SECONDS", 0.2)
    fake = FakeHevo()

    async def silent(message, response_type, params, context):
        await asyncio.sleep(5)

    async def go():
        async with Client(server(fake), mode="legacy", elicitation_handler=silent) as c:
            return await call(c, "pipelines_write", "disable_pipeline", path_params={"pipeline_id": 3})

    out = run(go())
    assert out["error"] == "APPROVAL_TIMEOUT" and "Nothing was sent to Hevo" in out["message"]
    assert fake.calls == []


def test_the_secrets_line_names_a_string_host_never_a_flag():
    from hevo_mcp_server.approval import prompt
    from hevo_mcp_server.catalog import BY_NAME
    ep, path = BY_NAME["create_destination"], "/api/v2/destinations"
    flagged = {"name": "wh", "connector_id": "redshift", "config": {
        "use_private_ip": False, "cluster_identifier": "c1.abc.us-east-1.redshift.amazonaws.com",
        "database_password": "$secret_file:rs"}}
    last = prompt(ep, "us", path, {}, flagged).splitlines()[-1]
    assert last.startswith("Hevo uses the value of your secret file rs as config.database_password to connect to "
                           "c1.abc.us-east-1.redshift.amazonaws.com.")
    no_host = {"name": "wh", "connector_id": "redshift", "config": {"use_private_ip": False,
                                                                     "database_password": "$secret_file:rs"}}
    assert prompt(ep, "us", path, {}, no_host).splitlines()[-1].endswith(
        "to connect to the Source or Destination in this request.")
