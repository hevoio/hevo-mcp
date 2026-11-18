"""The settings file (one place for every tunable) and progress notifications during long calls."""

import asyncio
import tomllib
from dataclasses import fields
from pathlib import Path

import httpx2 as httpx
import pytest
from fastmcp import Client

from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.settings import FIELDS, Settings
from hevo_mcp_server.tools import create_server
from tests.helpers import call, run

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "hevo_mcp.toml"


# --- Config file ---------------------------------------------------------------------------------------

def test_shipped_config_lists_every_setting_with_its_default():
    data = tomllib.loads(SHIPPED.read_text())
    defaults = Settings()
    for spec in FIELDS:
        assert spec.key in data[spec.section], f"[{spec.section}] {spec.key} missing from config/hevo_mcp.toml"
        value = data[spec.section][spec.key]
        default = getattr(defaults, spec.name)
        assert (sorted(value) if isinstance(value, list) else value) == (
            sorted(default) if isinstance(default, frozenset | tuple) else default), spec.name
    assert Settings.load(SHIPPED) == Settings()
    assert len(FIELDS) == len(fields(Settings))


def test_file_overrides_defaults_and_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HEVO_MCP_CONFIG", raising=False)
    cfg = tmp_path / "hevo.toml"
    cfg.write_text('[timeouts]\nread_seconds = 30\nread_deadline_seconds = 45\n'
                   '[access]\ndisabled_endpoints = ["resync_pipeline"]\n'
                   '[http]\nallowed_origins = ["https://inspector.example"]\n')
    s = Settings.load(cfg)
    assert s.read_timeout_seconds == 30 and s.read_deadline_seconds == 45 and s.write_timeout_seconds == 420
    assert s.disabled_endpoints == frozenset({"resync_pipeline"})
    assert s.allowed_origins == ("https://inspector.example",)
    monkeypatch.setenv("HEVO_MCP_CONFIG", str(cfg))
    monkeypatch.setenv("HEVO_MCP_READ_TIMEOUT", "20")  # tunables have no variable: the file is the only place
    monkeypatch.setenv("HEVO_MCP_ALLOWED_ORIGINS", "https://a.example, https://b.example")  # a hosted setting has one
    s = Settings.load()
    assert s.read_timeout_seconds == 30 and s.read_deadline_seconds == 45
    assert s.allowed_origins == ("https://a.example", "https://b.example")


@pytest.mark.parametrize("text, message", [
    ("[timeouts]\nread_secs = 30\n", "unknown key 'read_secs'"),
    ("[timeout]\nread_seconds = 30\n", "unknown section [timeout]"),
    ("[timeouts]\nread_seconds = \"soon\"\n", "must be an integer"),
    ("[access]\ndefault_scope = \"admin\"\n", "must be one of read, write, delete"),
    ("[http]\nallowed_hosts = \"mcp.example.com\"\n", "must be a list of strings"),
    ("[timeouts\n", "not valid TOML"),
])
def test_bad_config_stops_startup_with_a_clear_message(tmp_path, text, message):
    cfg = tmp_path / "bad.toml"
    cfg.write_text(text)
    with pytest.raises(SystemExit, match=message.replace("[", r"\[")):
        Settings.load(cfg)


def test_missing_config_file_stops_startup(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        Settings.load(tmp_path / "nope.toml")


# --- Progress ------------------------------------------------------------------------------------------

def _server(handler, **overrides):
    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, default_scope="delete", **overrides)
    return create_server(remote=False, settings=settings,
                         client=HevoClient(httpx.MockTransport(handler), settings=settings))


def test_long_writes_send_progress_while_waiting(env):
    async def slow(request):
        await asyncio.sleep(2.5)
        return httpx.Response(202, json={"code": 202, "message": "accepted"})

    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total, message))

    async def go():
        async with Client(_server(slow, progress_interval_seconds=1), progress_handler=on_progress) as c:
            return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})

    out = run(go())
    assert out["code"] == 202
    assert len(seen) >= 2 and [p for p, _, _ in seen] == sorted({p for p, _, _ in seen})
    assert "waiting for Hevo" in seen[0][2] and "POST /api/v2/pipelines/1/actions/sync-now" in seen[0][2]


def test_fetch_all_reports_each_page(env):
    def pages(request):
        n = int(request.url.params.get("cursor", "0"))
        return httpx.Response(200, json={"data": [{"id": n}], "has_more": n < 2, "next_cursor": str(n + 1)})

    seen = []

    async def on_progress(progress, total, message):
        seen.append(message)

    async def go():
        async with Client(_server(pages), progress_handler=on_progress) as c:
            return await call(c, "pipelines_read", "list_pipelines", fetch_all=True)

    out = run(go())
    assert out["count"] == 3
    assert seen == [f"fetch_all list_pipelines: {n} page(s), {n} rows so far" for n in (1, 2, 3)]


def test_no_progress_token_means_no_notifications_and_no_errors(env):
    async def slow(request):
        await asyncio.sleep(1.2)
        return httpx.Response(200, json={"data": []})

    async def go():
        async with Client(_server(slow, progress_interval_seconds=1)) as c:
            return await call(c, "pipelines_read", "list_pipelines")

    assert run(go()) == {"data": []}


def test_progress_reaches_clients_over_stateless_http(env):
    """Production runs stateless HTTP: progress must still arrive on the call's own response stream."""
    import socket

    from fastmcp.client.transports import StreamableHttpTransport

    from hevo_mcp_server.auth import HevoKeyVerifier, encode_basic
    from hevo_mcp_server.main import http_run_options

    async def slow(request):
        await asyncio.sleep(2.5)
        return httpx.Response(202, json={"code": 202})

    settings = Settings(rate_reads_per_minute=0, rate_writes_per_minute=0, progress_interval_seconds=1,
                        default_scope="delete")
    srv = create_server(remote=True, auth=HevoKeyVerifier(), settings=settings,
                        client=HevoClient(httpx.MockTransport(slow), settings=settings))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    seen = []

    async def on_progress(progress, total, message):
        seen.append(message)

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
            async with Client(transport, progress_handler=on_progress) as c:
                return await call(c, "pipelines_write", "sync_now_pipeline", path_params={"pipeline_id": 1})
        finally:
            task.cancel()

    assert run(go())["code"] == 202
    assert len(seen) >= 2 and all("waiting for Hevo" in m for m in seen)
