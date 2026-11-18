"""Checks that run before specific writes."""

from typing import Any
from urllib.parse import quote

from fastmcp.exceptions import ToolError

from hevo_mcp_server.auth import HevoCredentials
from hevo_mcp_server.client import HevoClient, HevoHTTPError


class GuardRefused(ToolError):
    pass


def _required_missing(schema: dict[str, Any], obj: dict[str, Any], where: str) -> list[str]:
    """Keys required by a connector's JSON schema: `required`, if/then rules and dependentRequired."""
    out = [f"{where}{k}" for k in schema.get("required", []) if k not in obj]
    props = schema.get("properties") or {}
    for rule in schema.get("allOf", []):
        cond = (rule.get("if") or {}).get("properties") or {}

        def hit(key: str, c: dict[str, Any]) -> bool:
            value = obj.get(key, (props.get(key) or {}).get("default"))
            if "const" in c:
                return value == c["const"]
            return value in c["enum"] if "enum" in c else False

        if cond and all(hit(k, c) for k, c in cond.items()):
            out += _required_missing(rule.get("then") or {}, obj, where)
    for key, deps in (schema.get("dependentRequired") or {}).items():
        if key in obj:
            out += [f"{where}{d}" for d in deps if d not in obj]
    return out


async def pipeline_create(client: HevoClient, creds: HevoCredentials, body: Any) -> str:
    """Refuses an Edge create whose Source or Destination Edge doesn't list, or that misses required keys."""
    if not isinstance(body, dict):
        raise GuardRefused("guard: body must be a JSON object")
    src = body.get("source") or {}
    cid, stype = src.get("connector_id"), src.get("type")
    if not isinstance(cid, str) or not cid.strip():
        raise GuardRefused("guard: body needs source.connector_id (see list_source_connectors)")
    if "source_type" in src and not stype:
        raise GuardRefused("guard: Edge v2 names the Source variant source.type, not source.source_type")

    sources = (await client.get_all(creds, "/api/v2/metadata/sources/connectors", {}))["data"]
    types: dict[str, list[Any]] = {}
    for row in sources:
        types.setdefault(row.get("connector_id"), []).append(row.get("type"))
    if cid not in types:
        raise GuardRefused(f"guard: source connector_id {cid!r} is not listed by list_source_connectors")
    if stype and stype not in types[cid]:
        raise GuardRefused(f"guard: source type {stype!r} is not offered by {cid} (listed: {types[cid]})")

    meta = await client.request(creds, "GET", f"/api/v2/metadata/sources/connectors/{quote(cid, safe='')}")
    schema = ((meta.get("data") or {}).get("schema") or {}) if isinstance(meta, dict) else {}
    if not schema:
        raise GuardRefused(f"guard: no config schema for {cid}, so its required keys can't be checked")
    gaps = _required_missing(schema, src, "source.")
    gaps += _required_missing((schema.get("properties") or {}).get("config") or {}, src.get("config") or {}, "source.config.")
    if src.get("auth"):
        # With the user's own OAuth app, the authorization field is left out of config.
        gaps = [g for g in gaps if not g.startswith("source.config.") or "auth" not in g]
    if gaps:
        raise GuardRefused(f"guard: {cid} needs {', '.join(sorted(set(gaps)))} (see get_source_connector)")

    if "destination_id" in body and "destination" in body:
        raise GuardRefused("guard: send destination_id or destination, not both")
    dest_id = body.get("destination_id")
    if isinstance(dest_id, int) and not isinstance(dest_id, bool) and dest_id > 0:
        try:
            dest = await client.request(creds, "GET", f"/api/v2/destinations/{dest_id}")
        except HevoHTTPError as exc:
            if exc.status != 404:
                raise
            raise GuardRefused(f"guard: destination_id {dest_id} is not an Edge destination in this region. "
                               "Standard (v2.0) Destination ids are not Edge Destinations; list Edge destinations "
                               "with list_destinations and use an id from there") from None
        dest = dest.get("data", dest) if isinstance(dest, dict) else {}
        dcid, label = dest.get("connector_id"), f"destination {dest_id} ({dest.get('name')})"
    elif isinstance(body.get("destination"), dict):
        dcid, label = body["destination"].get("connector_id"), f"new destination {body['destination'].get('name')!r}"
    else:
        raise GuardRefused("guard: body needs a positive integer destination_id or a destination object")
    dest_connectors = (await client.get_all(creds, "/api/v2/metadata/destinations/connectors", {}))["data"]
    if dcid not in {row.get("connector_id") for row in dest_connectors}:
        raise GuardRefused(f"guard: {label} uses connector {dcid!r}, which Edge does not list")
    return f"guard ok: source {cid}{'/' + stype if stype else ''}, {label} ({dcid})"

