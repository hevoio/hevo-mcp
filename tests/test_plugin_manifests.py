"""The Claude Code and Codex plugins ship the same server, skill and version."""

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
# What each client must pass to the server: the two things a user can set. The key is a file under
# HEVO_KEYS_DIR (default ~/.hevo), and the region comes from the agent on every call.
ENV = {"HEVO_KEYS_DIR", "HEVO_MCP_DEFAULT_SCOPE"}


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text())


def test_every_manifest_has_the_package_version():
    claude, codex = load(".claude-plugin/plugin.json"), load(".codex-plugin/plugin.json")
    assert claude["version"] == codex["version"] == VERSION
    assert claude["name"] == codex["name"] == "hevo-data"
    assert codex["interface"]["displayName"] == "Hevo"
    assert "displayName" not in claude  # Claude Code's installer rejects unknown keys in plugin.json
    assert claude["repository"] == codex["repository"]


def test_every_client_starts_the_same_server_with_the_same_settings():
    claude = load(".claude-plugin/plugin.json")["mcpServers"]["hevo"]
    codex_manifest = load(".codex-plugin/plugin.json")
    codex = load(codex_manifest["mcpServers"].removeprefix("./"))["mcpServers"]["hevo"]
    for server in (claude, codex):
        assert server["command"] == "uvx" and server["args"][-1] == "hevo-mcp"
        assert server["args"][2] == "-c" and server["args"][3].endswith("constraints.txt")  # pinned dependencies
    assert claude["args"][3] == "${CLAUDE_PLUGIN_ROOT}/constraints.txt"
    # The tagged release's own pins, from the same tag.
    assert codex["args"][3] == f"https://raw.githubusercontent.com/hevoio/hevo-mcp/v{VERSION}/constraints.txt"
    assert set(claude["env"]) == ENV
    assert set(codex["env_vars"]) == ENV | {"HOME"}  # HOME: so ~/.hevo resolves
    # Codex runs this release from PyPI, so the pinned version must match.
    assert codex["args"][1] == f"hevo-mcp=={VERSION}"


def test_every_client_gets_the_skills_folder():
    assert load(".codex-plugin/plugin.json")["skills"] == "./skills/"
    skill = (ROOT / "skills" / "hevo-pipelines" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: hevo-pipelines\n")  # Claude Code and Codex read this frontmatter
    assert (ROOT / "skills" / "hevo-pipelines" / "agents" / "openai.yaml").is_file()


def test_the_marketplaces_list_the_plugin():
    claude = load(".claude-plugin/marketplace.json")["plugins"][0]
    codex = load(".agents/plugins/marketplace.json")["plugins"][0]
    assert claude["name"] == codex["name"] == "hevo-data"
    for plugin, manifest in ((claude, ".claude-plugin/plugin.json"), (codex, ".codex-plugin/plugin.json")):
        # A release tag, not a moving branch: a merge without a version bump must not reach users. An https
        # url, not a github source, so Claude Code installs and updates without a GitHub SSH key.
        assert plugin["source"]["source"] == "url" and plugin["source"]["ref"] == f"v{VERSION}"
        assert plugin["source"]["url"].removesuffix(".git") == load(manifest)["repository"]


def test_every_manifest_declares_the_same_license():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT" and set(project["license-files"]) == {"LICENSE", "NOTICE"}
    assert load(".claude-plugin/plugin.json")["license"] == load(".codex-plugin/plugin.json")["license"] == "MIT"
    assert (ROOT / "LICENSE").read_text().lstrip().startswith("MIT License")


def test_constraints_txt_pins_exactly_what_uv_lock_does():
    """Plugins and the documented commands install with constraints.txt; it must follow uv.lock."""
    import subprocess

    exported = subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                               "--no-header", "--no-annotate"], cwd=ROOT, capture_output=True, text=True, check=True)
    assert (ROOT / "constraints.txt").read_text() == exported.stdout, \
        "constraints.txt is stale: run uv run python scripts/set_version.py --constraints"
    # The copy inside the package (what hevo_whoami compares against) must be the same file.
    assert (ROOT / "src" / "hevo_mcp_server" / "constraints.txt").read_text() == exported.stdout, \
        "the packaged constraints.txt is stale: run uv run python scripts/set_version.py --constraints"
    assert all("==" in line for line in exported.stdout.splitlines() if line and not line.startswith("#"))


def test_the_skill_names_only_endpoints_and_tools_that_exist():
    """A renamed or removed endpoint must not leave the skill sending the agent to a call that fails."""
    import re

    from hevo_mcp_server.catalog import BY_NAME, BY_TOOL

    folder = ROOT / "skills" / "hevo-pipelines"
    files = [folder / "SKILL.md", *sorted((folder / "references").glob("*.md"))]
    assert len(files) > 1, "the references folder is missing"
    for file in files:
        skill = file.read_text()
        # Endpoint names (verb_resource), tool names (resource_kind), Standard's prefixed names, and any old
        # `edge_` name, which must not survive: Edge names carry no prefix.
        pattern = (r"`((?:list|get|create|update|delete|enable|disable|resync|sync_now|cancel|invite)_[a-z_]+"
                   r"|(?:pipelines|destinations|metadata|accounts)_(?:read|write|destructive|delete)"
                   r"|(?:standard|edge)_[a-z_]+)`")
        named = set(re.findall(pattern, skill)) - {"list_endpoints", "get_schema"}
        unknown = {n for n in named if n not in BY_NAME and n not in BY_TOOL}
        assert not unknown, f"{file.name} names calls that don't exist: {sorted(unknown)}"
        # Every reference link points at a file that exists.
        for link in re.findall(r"\]\((references/[^)]+|[a-z-]+\.md)\)", skill):
            assert (folder / link).is_file() or (file.parent / link).is_file(), f"{file.name} links to missing {link}"
