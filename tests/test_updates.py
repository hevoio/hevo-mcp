"""hevo_whoami's "a newer version is available" notice."""

import asyncio

import httpx2 as httpx
import pytest

from hevo_mcp_server import observability, updates

URL = "https://example.com/tags"
REAL = updates.latest_version  # imported before conftest's per-test stub replaces it


def tags(*names, status=200):
    def handler(request):
        return httpx.Response(status, json=[{"name": n} for n in names])
    return httpx.MockTransport(handler)


@pytest.fixture
def real_check(monkeypatch):
    """Undo conftest's stub, and start each test with an empty cache."""
    monkeypatch.setattr(updates, "latest_version", REAL)
    updates._cache.clear()


def notice(transport):
    return asyncio.run(REAL(URL, transport=transport))


def test_the_highest_version_tag_wins(real_check):
    assert notice(tags("v1.2.0", "v1.10.1", "v1.9.9", "nightly", "v2")) == "1.10.1"


def test_a_newer_release_gives_a_notice_and_an_older_or_equal_one_does_not(real_check, monkeypatch):
    monkeypatch.setattr(observability, "VERSION", "1.0.0")
    monkeypatch.setattr(updates, "VERSION", "1.0.0")
    newer = asyncio.run(updates.update_notice(URL, transport=tags("v1.0.0", "v1.1.0")))
    assert newer.startswith("A newer version of the Hevo MCP server is available: 1.1.0 (this is 1.0.0)")
    updates._cache.clear()
    assert asyncio.run(updates.update_notice(URL, transport=tags("v1.0.0", "v0.9.0"))) is None


def test_pypi_releases_are_read_and_pre_releases_ignored(real_check):
    def handler(request):
        return httpx.Response(200, json={"info": {"version": "1.2.0"},
                                         "releases": {"1.0.0": [{}], "1.2.0": [{}], "1.3.0rc1": [{}], "1.4.0": []}})
    assert notice(httpx.MockTransport(handler)) == "1.2.0"  # rc ignored; 1.4.0 has no files (yanked/empty)


def test_any_failure_means_no_notice(real_check):
    assert notice(tags("v9.9.9", status=404)) is None  # private or missing repository
    updates._cache.clear()

    def offline(request):
        raise httpx.ConnectError("offline")
    assert notice(httpx.MockTransport(offline)) is None


def test_the_answer_is_cached(real_check):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[{"name": "v1.0.0"}])
    transport = httpx.MockTransport(handler)
    notice(transport)
    notice(transport)
    assert len(calls) == 1


def test_hevo_whoami_shows_the_version_and_the_notice(env, monkeypatch):
    from fastmcp import Client

    from tests.helpers import FakeHevo, run
    from tests.test_server import local_server

    async def newer(url, *, transport=None):
        return "99.0.0"

    async def whoami(srv):
        async with Client(srv) as c:
            return (await c.call_tool("hevo_whoami", {"region": "us"})).structured_content

    monkeypatch.setattr(updates, "latest_version", newer)
    who = run(whoami(local_server(FakeHevo())))
    assert who["version"] == observability.VERSION and "99.0.0 (this is" in who["update"]
    from hevo_mcp_server.settings import Settings

    assert "update" not in run(whoami(local_server(FakeHevo(), settings=Settings(update_check=False))))


def test_hevo_whoami_says_whether_the_dependencies_are_the_tested_ones(env, monkeypatch):
    """The tested set ships inside the package; an install that resolved other versions is reported, with the
    fix, so support can tell a tested install from a drifted one."""
    from fastmcp import Client

    from hevo_mcp_server import pins
    from tests.helpers import FakeHevo, run
    from tests.test_server import local_server

    async def whoami(srv):
        async with Client(srv) as c:
            return (await c.call_tool("hevo_whoami", {"region": "us"})).structured_content

    tested = pins.tested_versions()
    assert tested and tested["fastmcp"] and all("==" not in v for v in tested.values())
    assert run(whoami(local_server(FakeHevo())))["dependencies"] == {"status": "as_tested"}  # uv sync --frozen

    real = pins.metadata.version

    def older(name):
        return "0.0.1" if name == "fastmcp" else real(name)

    monkeypatch.setattr(pins.metadata, "version", older)
    status = run(whoami(local_server(FakeHevo())))["dependencies"]
    assert status["status"] == "drifted" and status["drift"] == {"fastmcp": {"tested": tested["fastmcp"], "installed": "0.0.1"}}
    assert "constraints.txt" in status["hint"]
