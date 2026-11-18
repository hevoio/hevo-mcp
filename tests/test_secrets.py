"""`$secret_file:` references: the password reaches Hevo, and only Hevo, without passing through
the chat; a reference that can't be read sends nothing."""

import asyncio
import json
import os

import httpx2 as httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hevo_mcp_server import auth, secrets
from hevo_mcp_server.auth import HevoKeyVerifier
from hevo_mcp_server.client import HevoClient
from hevo_mcp_server.tools import create_server
from tests.helpers import FakeHevo, call, run
from tests.test_controls import TOKEN, UNLIMITED, _free_port, _wait

PASSWORD = "s3cr3t-db-pass"
DEST = {"name": "wh", "connector_id": "snowflake"}  # what create_destination requires besides config


@pytest.fixture
def secrets_dir(env):
    """`secrets/` next to the key file (the env fixture's keys folder), with one password saved."""
    folder = env / "secrets"
    folder.mkdir()
    (folder / "mysql-prod").write_text(PASSWORD + "\n")
    (folder / "mysql-prod").chmod(0o600)
    return folder


def _server(fake, settings=UNLIMITED):
    return create_server(remote=False, client=HevoClient(httpx.MockTransport(fake), settings=settings),
                         settings=settings)


# --- resolution ------------------------------------------------------------------------------------------

def test_references_are_found_anywhere_and_misplaced_ones_are_named():
    body = {"source": {"config": {"password": "$secret_file:a", "hosts": ["$secret_file:h", "x"]}}, "n": 1,
            "again": "$secret_file:a"}
    assert secrets.references(body) == ["$secret_file:a", "$secret_file:h"]
    assert secrets.references({"password": "plain"}) == [] and secrets.references(None) == []
    # Only a field that names a secret may hold a reference: a host, name or query must not.
    assert secrets.misplaced(body) == ["source.config.hosts[0]", "again"]
    assert secrets.misplaced({"auth": {"client_secret": "$secret_file:x", "api_key": "$secret_file:k"}}) == []


def test_a_reference_is_never_redacted_so_the_user_can_see_it():
    from hevo_mcp_server import sanitize

    cleaned = sanitize.clean({"password": "$secret_file:mysql-prod", "token": "real-value"})
    assert cleaned == {"password": "$secret_file:mysql-prod", "token": "real-value"}
    assert sanitize.clean({"password": "hunter2"}) == {"password": "[redacted]"}


def test_file_references_resolve_and_the_rest_is_untouched(secrets_dir):
    (secrets_dir / "stripe").write_text("sk_live_123")
    (secrets_dir / "stripe").chmod(0o600)
    body = {"name": "p", "source": {"config": {"password": "$secret_file:mysql-prod", "port": 3306},
                                    "auth": [{"token": "$secret_file:stripe"}]}}
    out = secrets.resolve(body)
    assert out["source"]["config"] == {"password": PASSWORD, "port": 3306}  # trailing newline dropped
    assert out["source"]["auth"] == [{"token": "sk_live_123"}]
    assert body["source"]["config"]["password"] == "$secret_file:mysql-prod"  # the input is not changed


@pytest.mark.parametrize("ref, reason", [
    ("$secret_file:missing", "no file named missing"),
    ("$secret_file:../mysql-prod", "bare file name"),
    ("$secret_file:sub/mysql-prod", "bare file name"),
    ("$secret_file:/etc/passwd", "bare file name"),
    ("$secret_file:", "bare file name"),
])
def test_bad_references_are_refused_with_the_reason(secrets_dir, ref, reason):
    with pytest.raises(secrets.SecretUnavailable, match=reason):
        secrets.resolve({"password": ref})


def test_files_other_users_can_read_and_empty_files_are_refused(secrets_dir):
    (secrets_dir / "mysql-prod").chmod(0o644)
    with pytest.raises(secrets.SecretUnavailable, match="readable by other users"):
        secrets.resolve("$secret_file:mysql-prod")
    (secrets_dir / "empty").write_text("\n")
    (secrets_dir / "empty").chmod(0o600)
    with pytest.raises(secrets.SecretUnavailable, match="is empty"):
        secrets.resolve("$secret_file:empty")


@pytest.mark.parametrize("windows, expected, unexpected", [
    (False, ["mkdir -p", "chmod 700", "chmod 600"], ["icacls"]),
    (True, ["PowerShell", "New-Item", "icacls", "${env:USERNAME}"], ["chmod", "mkdir -p"]),
])
def test_setup_help_gives_the_commands_for_this_machines_shell(secrets_dir, monkeypatch, windows, expected,
                                                                unexpected):
    """A missing key file or secret file tells the user how to restrict it in the shell they have: chmod on
    macOS and Linux, PowerShell's icacls on Windows (README: Key file)."""
    from hevo_mcp_server import auth
    monkeypatch.setattr(auth, "_windows", lambda: windows)
    with pytest.raises(secrets.SecretUnavailable) as missing:
        secrets.resolve("$secret_file:not-there")
    key_help = auth.no_key_help()
    for text in (str(missing.value), key_help):
        assert not any(u in text for u in unexpected)
    assert all(e in str(missing.value) for e in expected)
    assert ("icacls" if windows else "chmod 600") in key_help


def test_whitespace_around_a_value_is_trimmed_and_inner_lines_are_kept(secrets_dir):
    """An editor's final newline or a pasted space must not become part of the password; a multi-line
    secret (service-account JSON, PEM key) keeps its lines."""
    for raw in (PASSWORD, PASSWORD + "\n", PASSWORD + "\r\n", f"  {PASSWORD}\t\n", f"\n{PASSWORD} \n\n"):
        (secrets_dir / "pw").write_text(raw)
        (secrets_dir / "pw").chmod(0o600)
        assert secrets.resolve("$secret_file:pw") == PASSWORD, repr(raw)
    pem = "-----BEGIN PRIVATE KEY-----\nabc\ndef\n-----END PRIVATE KEY-----"
    (secrets_dir / "key.pem").write_text(pem + "\n")
    (secrets_dir / "key.pem").chmod(0o600)
    assert secrets.resolve("$secret_file:key.pem") == pem
    (secrets_dir / "blank").write_text(" \t\n")
    (secrets_dir / "blank").chmod(0o600)
    with pytest.raises(secrets.SecretUnavailable, match="is empty"):
        secrets.resolve("$secret_file:blank")


def test_error_messages_never_contain_the_secret(secrets_dir):
    (secrets_dir / "mysql-prod").chmod(0o644)
    with pytest.raises(secrets.SecretUnavailable) as exc:
        secrets.resolve("$secret_file:mysql-prod")
    assert PASSWORD not in str(exc.value)


def test_secrets_dir_defaults_to_the_keys_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("HEVO_KEYS_DIR", str(tmp_path))
    assert secrets.secrets_dir() == tmp_path / "secrets"


# --- through the server ---------------------------------------------------------------------------------

def test_the_value_reaches_hevo_and_nothing_else(env, secrets_dir, caplog):
    caplog.set_level("DEBUG", logger="hevo_mcp")
    fake = FakeHevo({("POST", "/api/v2/destinations"): {"code": "Success", "data": {"id": 7}}})

    async def go():
        async with Client(_server(fake)) as c:
            return await call(c, "destinations_write", "create_destination",
                              body={**DEST, "config": {"password": "$secret_file:mysql-prod"}})

    result = run(go())
    assert result["data"] == {"id": 7}
    [sent] = fake.writes()
    assert json.loads(sent.content)["config"]["password"] == PASSWORD
    assert PASSWORD not in json.dumps(result) and PASSWORD not in "\n".join(r.getMessage() for r in caplog.records)


def test_an_unreadable_reference_sends_nothing(env, secrets_dir):
    fake = FakeHevo()

    async def go():
        async with Client(_server(fake)) as c:
            return await call(c, "destinations_write", "create_destination",
                              body={**DEST, "config": {"password": "$secret_file:nope"}})

    result = run(go())
    assert result["error"] == "SECRET_UNAVAILABLE" and result["reference"] == "$secret_file:nope"
    assert "no file named nope" in result["message"] and "Nothing was sent" in result["message"]
    assert fake.writes() == []


def test_references_are_resolved_after_approval_and_the_prompt_shows_only_the_reference(env, secrets_dir):
    fake = FakeHevo({("PATCH", "/api/v2/pipelines/9/sources"): {"code": "Success", "data": {}}})
    prompts = []

    async def user(message, response_type, params, context):
        prompts.append(message)
        return {"decision": "approve"}

    async def go():
        async with Client(_server(fake), elicitation_handler=user) as c:
            return await call(c, "pipelines_write", "update_pipeline_sources",
                              path_params={"pipeline_id": 9}, body={"config": {"password": "$secret_file:mysql-prod"}})

    run(go())
    [sent] = fake.writes()
    assert json.loads(sent.content)["config"]["password"] == PASSWORD
    assert len(prompts) == 1 and PASSWORD not in prompts[0]


def test_whoami_shows_where_secret_files_are_read_from(env, secrets_dir):
    async def go():
        async with Client(_server(FakeHevo())) as c:
            return await call(c, "hevo_whoami")

    assert run(go())["secrets_dir"] == str(secrets_dir)


def test_a_reference_in_the_wrong_field_sends_nothing(env, secrets_dir):
    fake = FakeHevo()

    async def go():
        async with Client(_server(fake)) as c:
            return await call(c, "destinations_write", "create_destination",
                              body={**DEST, "config": {"host": "$secret_file:mysql-prod", "password": "x"}})

    result = run(go())
    assert result["error"] == "SECRET_UNAVAILABLE" and result["fields"] == ["config.host"]
    assert fake.writes() == []


@pytest.mark.parametrize("mode", ["legacy", "auto"])
def test_a_create_with_a_reference_asks_for_approval_and_names_the_secret(env, secrets_dir, mode):
    """Creates don't ask, but a request that carries a secret always does: the user sees which secret goes to
    which host before the connection test sends it, and a prompt-injected create can't send it unseen."""
    fake = FakeHevo({("POST", "/api/v2/destinations"): {"code": "Success", "data": {"id": 7}}})
    prompts = []

    async def user(message, response_type, params, context):
        prompts.append(message)
        return {"decision": "reject" if "evil.example" in message else "approve"}

    async def go():
        async with Client(_server(fake), mode=mode, elicitation_handler=user) as c:
            ok = await call(c, "destinations_write", "create_destination",
                            body={**DEST, "config": {"host": "db.internal", "password": "$secret_file:mysql-prod"}})
            bad = await call(c, "destinations_write", "create_destination",
                             body={**DEST, "config": {"host": "evil.example.net", "password": "$secret_file:mysql-prod"}})
            return ok, bad

    ok, bad = run(go())
    assert ok["data"] == {"id": 7} and bad["error"] == "APPROVAL_DECLINED"
    assert len(prompts) == 2 and all("$secret_file:mysql-prod" in p and "db.internal" in p or "evil" in p for p in prompts)
    # The secrets line names the file, the field it fills and the host it reaches, in the user's words.
    assert ("Hevo uses the value of your secret file mysql-prod as config.password to connect to db.internal. "
            "Check that db.internal is the host of your Source or Destination.") in prompts[0]
    assert prompts[0].startswith("Create Edge Destination in the us region?") and "[redacted]" not in prompts[0]
    assert PASSWORD not in "".join(prompts)
    [sent] = fake.writes()  # only the approved one reached Hevo
    assert json.loads(sent.content)["config"]["host"] == "db.internal"
    assert sent.headers["X-Hevo-Mcp-Approval"] == "approved"


def test_a_shared_server_refuses_references(env, secrets_dir):
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
            t = StreamableHttpTransport(url, auth=TOKEN, headers={"X-Hevo-Region": "us", "X-Hevo-Scope": "write"})
            async with Client(t) as c:
                return (await call(c, "destinations_write", "create_destination",
                                   body={**DEST, "config": {"password": "$secret_file:mysql-prod"}}),
                        await call(c, "hevo_whoami"))
        finally:
            task.cancel()

    result, who = run(go())
    assert result["error"] == "SECRET_UNAVAILABLE" and "shared server" in result["message"]
    assert fake.writes() == [] and "secrets_dir" not in who
    assert secrets.secrets_dir().is_dir()  # the folder exists; it's the server kind that refuses


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_the_private_file_reader_refuses_links_folders_and_open_files(tmp_path):
    real = tmp_path / "real"
    real.write_text("value\n")
    real.chmod(0o600)
    assert auth.read_private_file(real) == "value\n"
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(auth.PrivateFileError, match="symbolic link"):
        auth.read_private_file(link)
    with pytest.raises(auth.PrivateFileError, match="not a regular file"):
        auth.read_private_file(tmp_path)
    real.chmod(0o644)
    with pytest.raises(auth.PrivateFileError, match="other users") as exc:
        auth.read_private_file(real)
    assert exc.value.code == "mode"
    with pytest.raises(auth.PrivateFileError, match="does not exist"):
        auth.read_private_file(tmp_path / "missing")
