import sys
from pathlib import Path

import pytest

# build_manifest.py lives at the repo root, next to the specs it reads.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line("markers", "no_auto_approve: the test's MCP clients can't show approval prompts")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Every test starts without the developer's own Hevo settings: variables exported in the shell that runs
    pytest (HEVO_*, CORALOGIX_*) would otherwise change defaults, regions and keys."""
    import os

    for var in list(os.environ):
        if var.startswith(("HEVO_", "CORALOGIX_")):
            monkeypatch.delenv(var)
    # Never pick up the developer's own key files from ~/.hevo during tests.
    monkeypatch.setenv("HEVO_KEYS_DIR", str(Path(__file__).parent / "no-such-keys-dir"))
    # No real request to the release list: hevo_whoami's update check sees "no newer release" unless a test
    # gives it an answer (tests/test_updates.py).
    from hevo_mcp_server import updates

    async def no_release(url, *, transport=None):
        return None

    monkeypatch.setattr(updates, "latest_version", no_release)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A user's (stdio) setup: the key file in the keys folder, nothing else. Calls name their region (the
    `call` helper passes us unless a test says otherwise)."""
    import json

    keys = tmp_path / "hevo"
    keys.mkdir()
    (keys / "hevo-key.json").write_text(json.dumps({"api_key": "key-alice-1234", "secret_key": "secret-alice"}))
    (keys / "hevo-key.json").chmod(0o600)
    monkeypatch.setenv("HEVO_KEYS_DIR", str(keys))
    return keys


@pytest.fixture
def no_wait(monkeypatch):
    async def skip(_):
        pass

    monkeypatch.setattr("hevo_mcp_server.client.asyncio.sleep", skip)


async def approve_everything(message, response_type, params, context):
    """The user picks Approve on every prompt: the default for tests that aren't about approval."""
    return {"decision": "approve"}


@pytest.fixture(autouse=True)
def approving_user(request, monkeypatch):
    """MCP clients in tests answer approval prompts with yes unless the test passes its own
    `elicitation_handler` or is marked `no_auto_approve` (a client that can't show prompts)."""
    if request.node.get_closest_marker("no_auto_approve"):
        return
    import fastmcp

    original = fastmcp.Client.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("elicitation_handler", approve_everything)
        original(self, *args, **kwargs)

    monkeypatch.setattr(fastmcp.Client, "__init__", init)
