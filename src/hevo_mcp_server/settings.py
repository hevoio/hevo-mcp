"""Operational limits and controls, all in one place.

Every tunable (timeouts, limits, rate limits, access, HTTP guards, progress) is a field of `Settings`
and is read, in increasing precedence, from:

1. the defaults below,
2. a TOML config file (`HEVO_MCP_CONFIG` or `--config`; `config/hevo_mcp.toml` lists every setting),
3. for the few settings a user or a hosted deployment sets without a file, an environment variable
   (`FIELDS` names it; the others have none and come only from the file).

`FIELDS` maps each field to its config-file section and key; the README tables and `config/hevo_mcp.toml`
follow it, and a test checks the shipped file matches the defaults. Nothing here is a secret, so the whole
thing is logged at startup. Public URL, host and port stay environment or command-line options: they say
which deployment this is, not how it behaves.
"""

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, NamedTuple

from hevo_mcp_server.auth import REGIONS, SCOPES


@dataclass(frozen=True)
class Settings:
    # [rate_limits]
    rate_reads_per_minute: int = 60  # HTTP only: per caller (hashed key), per replica; 0 disables. A stdio server has no limit
    rate_writes_per_minute: int = 20
    # Calls per minute, per replica, shared by every API key Hevo hasn't accepted yet (a key's first call,
    # or a made-up one). Stops a flood of fake keys from using this server to hammer Hevo; 0 disables.
    rate_unverified_per_minute: int = 60
    # [access]
    # Scope for connections that send no X-Hevo-Scope (and for stdio). Read-only unless the caller opts in.
    default_scope: str = "read"
    disabled_endpoints: frozenset[str] = field(default_factory=frozenset)  # endpoint or group names
    # Hevo regions this deployment serves (http). A request for any other region is refused rather than
    # routed across borders; empty = any region, for a single shared deployment or local testing.
    allowed_regions: tuple[str, ...] = ()
    # [timeouts]
    connect_timeout_seconds: int = 15
    read_timeout_seconds: int = 60  # one Hevo read request
    write_timeout_seconds: int = 420  # one Hevo write request; Edge create tests both connections
    # Total time for one read tool call, retries and fetch_all pages included. Most clients give up on a
    # call after 60-120 s, so a read past this returns TIMEOUT (or fetch_all's rows so far) instead.
    read_deadline_seconds: int = 90
    # [progress]
    progress_interval_seconds: int = 10  # heartbeat while waiting on Hevo, for clients that asked; 0 = off
    # [limits]
    max_rows: int = 5_000  # fetch_all stops here and reports truncated + next_cursor
    max_pages: int = 200
    max_result_bytes: int = 1_000_000  # a tool result above this comes back as RESULT_TOO_LARGE
    # [http]
    max_body_bytes: int = 1_000_000  # HTTP request body cap (413 above it)
    allowed_hosts: tuple[str, ...] = ()  # Host header allowlist (421 otherwise); empty = only on localhost
    allowed_origins: tuple[str, ...] = ()  # browser origins allowed to call (403 otherwise)
    # On SIGTERM: stop accepting, then wait this long for in-flight calls before cutting them off.
    # Longer than the write timeout so a deploy never cuts off an Edge pipeline create mid-call;
    # the orchestrator's own grace period (Kubernetes, ECS, systemd) must be longer still.
    shutdown_grace_seconds: int = 450
    # [hevo_pool]
    max_connections: int = 100  # outbound pool to Hevo per replica
    max_keepalive_connections: int = 20
    # [updates] hevo_whoami says when a newer release exists on PyPI: one anonymous request, at most once a day,
    # never with a key or user data. Off with [updates] enabled = false.
    update_check: bool = True
    update_check_url: str = "https://pypi.org/pypi/hevo-mcp/json"

    @classmethod
    def load(cls, config_path: str | os.PathLike[str] | None = None) -> "Settings":
        """Defaults, then the config file (argument or HEVO_MCP_CONFIG), then the environment variables that
        exist (the settings a user or a hosted deployment sets without a file)."""
        values: dict[str, Any] = {}
        path = config_path or os.environ.get("HEVO_MCP_CONFIG", "").strip() or None
        if path:
            values.update(_from_file(Path(path)))
        for spec in FIELDS:
            raw = os.environ.get(spec.env, "").strip() if spec.env else ""
            if raw:
                values[spec.name] = _convert(spec, raw.split(",") if spec.kind in ("names", "regions") else raw,
                                             spec.env)
        settings = replace(cls(), **values)
        if unknown := [r for r in settings.allowed_regions if r not in REGIONS]:
            raise SystemExit(f"allowed_regions: unknown region(s) {', '.join(unknown)} "
                             f"(known: {', '.join(REGIONS)})")
        return settings

    @classmethod
    def from_env(cls) -> "Settings":
        return cls.load()

    def as_log_fields(self) -> dict[str, object]:
        out = asdict(self)
        out["disabled_endpoints"] = sorted(self.disabled_endpoints)
        out["allowed_regions"] = list(self.allowed_regions)
        out["allowed_hosts"] = list(self.allowed_hosts)
        out["allowed_origins"] = list(self.allowed_origins)
        return out


class Field(NamedTuple):
    name: str  # Settings attribute
    section: str  # config file [section]
    key: str  # key in that section
    env: str | None  # environment variable, for the settings set without a config file; None = file only
    kind: str  # int | bool | scope | names | regions | str | url


FIELDS = (
    Field("rate_reads_per_minute", "rate_limits", "reads_per_minute", None, "int"),
    Field("rate_writes_per_minute", "rate_limits", "writes_per_minute", None, "int"),
    Field("rate_unverified_per_minute", "rate_limits", "unverified_per_minute", None, "int"),
    Field("default_scope", "access", "default_scope", "HEVO_MCP_DEFAULT_SCOPE", "scope"),  # set by users
    Field("disabled_endpoints", "access", "disabled_endpoints", None, "names"),
    Field("allowed_regions", "access", "allowed_regions", "HEVO_MCP_ALLOWED_REGIONS", "regions"),  # hosted
    Field("connect_timeout_seconds", "timeouts", "connect_seconds", None, "int"),
    Field("read_timeout_seconds", "timeouts", "read_seconds", None, "int"),
    Field("write_timeout_seconds", "timeouts", "write_seconds", None, "int"),
    Field("read_deadline_seconds", "timeouts", "read_deadline_seconds", None, "int"),
    Field("progress_interval_seconds", "progress", "interval_seconds", None, "int"),
    Field("max_rows", "limits", "max_rows", None, "int"),
    Field("max_pages", "limits", "max_pages", None, "int"),
    Field("max_result_bytes", "limits", "max_result_bytes", None, "int"),
    Field("max_body_bytes", "http", "max_body_bytes", "HEVO_MCP_MAX_BODY_BYTES", "int"),  # hosted
    Field("allowed_hosts", "http", "allowed_hosts", "HEVO_MCP_ALLOWED_HOSTS", "names"),  # hosted
    Field("allowed_origins", "http", "allowed_origins", "HEVO_MCP_ALLOWED_ORIGINS", "names"),  # hosted
    Field("shutdown_grace_seconds", "http", "shutdown_grace_seconds", "HEVO_MCP_SHUTDOWN_GRACE", "int"),  # hosted
    Field("max_connections", "hevo_pool", "max_connections", None, "int"),
    Field("max_keepalive_connections", "hevo_pool", "max_keepalive_connections", None, "int"),
    Field("update_check", "updates", "enabled", None, "bool"),
    Field("update_check_url", "updates", "url", None, "url"),
)
assert {f.name for f in FIELDS} == {f.name for f in fields(Settings)}, "every setting needs a FIELDS entry"


def _from_file(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"Config file {path} not found (HEVO_MCP_CONFIG / --config)") from None
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Config file {path} is not valid TOML: {exc}") from None
    by_place = {(f.section, f.key): f for f in FIELDS}
    sections = {f.section for f in FIELDS}
    values: dict[str, Any] = {}
    for section, table in data.items():
        if section not in sections or not isinstance(table, dict):
            raise SystemExit(f"{path}: unknown section [{section}] (known: {', '.join(sorted(sections))})")
        for key, raw in table.items():
            spec = by_place.get((section, key))
            if spec is None:
                known = ", ".join(f.key for f in FIELDS if f.section == section)
                raise SystemExit(f"{path}: unknown key {key!r} in [{section}] (known: {known})")
            values[spec.name] = _convert(spec, raw, f"{path} [{section}] {key}")
    return values


def _convert(spec: Field, raw: Any, where: str) -> Any:
    if spec.kind == "int":
        if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
            raise SystemExit(f"{where} must be a whole number of units, got {raw!r}")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SystemExit(f"{where} must be an integer, got {raw!r}") from None
        if value < 0:
            raise SystemExit(f"{where} must be 0 or more, got {value}")
        return value
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        raise SystemExit(f"{where} must be true or false (or on/off), got {raw!r}")
    if spec.kind in ("str", "url"):
        if not isinstance(raw, str):
            raise SystemExit(f"{where} must be a string, got {raw!r}")
        value = raw.strip()
        if spec.kind == "url" and value and not value.startswith("https://"):
            raise SystemExit(f"{where} must be an https:// URL, got {value!r}")
        return value
    if spec.kind == "scope":
        value = str(raw).strip().lower()
        if value not in SCOPES:
            raise SystemExit(f"{where} must be one of {', '.join(SCOPES)}, got {value!r}")
        return value
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise SystemExit(f"{where} must be a list of strings, got {raw!r}")
    names = [v.strip() for v in raw if v.strip()]
    if spec.kind == "regions":
        names = [n.lower() for n in names]  # checked against the known regions once everything is loaded
    return frozenset(names) if spec.name == "disabled_endpoints" else tuple(names)


def scope_allows(scope: str, action: str) -> bool:
    """Whether a connection scope permits an endpoint action (read < write < delete)."""
    return SCOPES.index(action) <= SCOPES.index(scope)
