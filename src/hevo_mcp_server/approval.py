"""Asks the user, through their MCP client, to approve a call before it reaches Hevo.

Which endpoints need approval is fixed in the manifest (`approval` in endpoint_overrides.json): every
delete; changes to configuration (Destinations, Sources, pipeline settings, object selection);
destructive actions (cancel, disable, resync); runs that ingest data (sync now); and enabling or
resuming. Creating doesn't: a create makes something new and breaks nothing. On top of that, any request
whose body carries a secret reference (secrets.py) asks, so the user sees which secret goes to which host.
No setting, scope or tool argument turns approval off: the scope still decides what a connection may call
at all, and approval is asked on top of it.

The question goes to the user through MCP elicitation, which the client shows in its own UI, so the model
can't answer it. The answer does come from the client, so a client the user doesn't trust could answer by
itself; the server can't tell a click from a client's auto-reply (see "What this protects against" in the README):

* MCP 2026-07-28 clients: the tool answers with an input request (SEP-2322); the client shows the prompt
  and calls the tool again with the answer. The request carries state, sealed by the MCP framework, that
  is bound to the exact call (endpoint, region, path, query and body) and used once, so an approval can't
  be reused for another call or replayed.
* Older clients: a server-initiated elicitation request (stdio, or HTTP with a session).

A client that can't show the prompt gets APPROVAL_UNAVAILABLE, and nothing is sent to Hevo.
"""

import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import unquote

import mcp_types
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from hevo_mcp_server import observability, sanitize
from hevo_mcp_server.catalog import Endpoint

REQUEST_KEY = "hevo_approval"
STATE_TTL_SECONDS = 600  # how long an approval prompt stays answerable
WAIT_SECONDS = 600  # how long an older client's prompt may stay open
MAX_BODY_CHARS = 1500
# Body keys that say where a request connects. They are shown on their own line, in full, before the body,
# so a long body can't push the host out of the part the user sees. HOST_KEY picks the one the secrets line names.
CONNECTION_KEY = re.compile(r"host|url|endpoint|server|account|bucket|project|region|warehouse|database|db_name|"
                            r"schema|port|subdomain|site|domain|address|addr|ip\b|uri|dsn|connection|cluster|identifier",
                            re.IGNORECASE)
MAX_FIELD_CHARS = 200
# The client's own Accept and Decline buttons are the answer: accepting approves, declining or dismissing
# refuses. Both are user actions in the client's UI that the model can't take. The one field is optional and
# starts at Approve (a form needs a field to render in some clients); picking Reject and accepting also refuses.
# Earlier designs failed in Claude Code: a checkbox came back unticked whatever its default, so every call was
# refused, and a required choice blocked the Accept button until the user opened the field and picked one.
APPROVE, REJECT = "approve", "reject"
SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "title": "Make this change in Hevo?",
            "description": "Select Approve to make the change, or Reject to stop it.",
            "oneOf": [{"const": APPROVE, "title": "Approve"}, {"const": REJECT, "title": "Reject"}],
            "default": APPROVE,
        },
    },
}


def call_fingerprint(ep: Endpoint, region: str, path: str | list[str], query: dict[str, Any], body: Any) -> str:
    """sha256 of exactly what would be sent: an approval is valid for this call only. For a batch, `path`
    is the list of targets, so the approval covers exactly that set and no other."""
    canonical = json.dumps({"endpoint": ep.name, "region": region, "method": ep.method, "path": path,
                            "query": query, "body": body}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


MAX_TARGETS_SHOWN = 60
HOST_KEY = re.compile(r"host|url|endpoint|server|account|address|addr|ip\b|uri|dsn|cluster|identifier", re.IGNORECASE)


def prompt(ep: Endpoint, region: str, path: str | list[str], query: dict[str, Any], body: Any) -> str:
    """What the user sees. The first line names the change, the resource and the region (Claude Code shows
    only that line until the prompt is opened), then its effect, who may approve, and the exact request last
    under "Technical details" for anyone who wants to verify it. Secrets are masked. A batch (one action on
    many targets) is one prompt that lists every target."""
    title, effect = _title_and_effect(ep, body)
    what = f"({len(path)} targets)" if isinstance(path, list) else _resource(ep, path)
    # A title holds "{}" where the resource goes ("Run Standard Pipeline {} now"); otherwise it goes last.
    first = title.replace("{}", what, 1) if "{}" in title else f"{title} {what}".rstrip()
    lines = [f"{first} in the {region} region?"]
    if effect:
        lines += ["", effect]
    lines += ["", "Approve only if you asked your agent for this change. Your agent cannot approve it for you."]
    if isinstance(path, list):
        lines += ["", "Targets:", *[f"  {p}" for p in path[:MAX_TARGETS_SHOWN]]]
        if len(path) > MAX_TARGETS_SHOWN:
            lines.append(f"  ... and {len(path) - MAX_TARGETS_SHOWN} more")
        lines += ["", f"Technical details: {ep.method} {ep.path} ({ep.name}), one request for each target"]
    else:
        lines += ["", f"Technical details: {ep.method} {path} ({ep.name})"]
    if query:
        lines += ["Query: " + json.dumps(sanitize.clean(query), default=str)]
    where = connection_fields(body) if body is not None else []
    if body is not None:
        if where:
            lines += ["Connects to: " + ", ".join(f"{k}={v}" for k, v in where)]
        text = json.dumps(sanitize.clean(body), indent=1, default=str)
        # A body that sends a secret is shown whole, whatever its size: nothing may hide past the cut.
        cut = len(text) > MAX_BODY_CHARS and not secret_fields(body)
        lines += ["Body: " + (text[:MAX_BODY_CHARS] + f" ... ({len(text) - MAX_BODY_CHARS:,} more characters)" if cut else text)]
    if used := secret_fields(body):
        # The host named in the secrets line is a string under a host-like key; a boolean or number under a
        # key that merely looks like one (use_private_ip: false) is never "the host".
        host = next((v for k, v in where if HOST_KEY.search(k.rsplit(".", 1)[-1]) and _is_host_value(v)), None)
        for ref, field in used:
            name = ref.split(":", 1)[-1]
            lines += ["", f"Hevo uses the value of your secret file {name} as {field} to connect to "
                      + (f"{host}. Check that {host} is the host of your Source or Destination." if host else
                         "the Source or Destination in this request.")]
    return "\n".join(lines)


def _is_host_value(text: str) -> bool:
    """A host, account or URL is a non-empty string that isn't a bare boolean or number."""
    return bool(text) and text.lower() not in ("true", "false", "none") and not re.fullmatch(r"-?\d+(\.\d+)?", text)


def _title_and_effect(ep: Endpoint, body: Any) -> tuple[str, str]:
    """The first line's title and the effect line. An endpoint that does several things by its body
    (update_pipeline_objects includes, excludes or updates objects) is named by what this request does."""
    title = ep.title or ep.summary.split(" - ", 1)[-1]
    if ep.name == "update_pipeline_objects" and isinstance(body, dict):
        mode = str(body.get("selection_mode") or "RETAIN_EXISTING").upper()
        objects = body.get("objects") if isinstance(body.get("objects"), list) else []
        flags = [o.get("selected") for o in objects if isinstance(o, dict)]
        n = f"{len(objects)} object" + ("s" if len(objects) != 1 else "")
        if mode == "INCLUDE_ALL":
            return ("Include all objects in Edge Pipeline {}" + (f" except {n}" if objects else ""),
                    "Hevo starts ingesting the data of every object of this Pipeline"
                    + (" except the listed ones." if objects else "."))
        if mode == "EXCLUDE_ALL":
            return ("Exclude all objects from Edge Pipeline {}" + (f" except {n}" if objects else ""),
                    "Hevo stops ingesting the data of every object of this Pipeline"
                    + (" except the listed ones." if objects else "."))
        if objects and all(f is True for f in flags):
            return f"Include {n} in Edge Pipeline {{}}", "Hevo starts ingesting the data of these objects."
        if objects and all(f is False for f in flags):
            return f"Exclude {n} from Edge Pipeline {{}}", "Hevo stops ingesting the data of these objects."
        if objects and all(f is None for f in flags):
            return f"Update {n} of Edge Pipeline {{}}", "Changes how Hevo loads the data of these objects."
    return title, ep.consequence


def _resource(ep: Endpoint, path: str) -> str:
    """The ids in the path, for the first line: "352" for one, "12 (object_name users)" for more. Ids are
    shown as the user knows them (a@example.com, not a%40example.com)."""
    pattern = re.sub(r"\\{([^{}]+)\\}", r"(?P<\1>[^/]+)", re.escape(ep.path))
    match = re.fullmatch(pattern, path)
    if not match or not match.groupdict():
        return ""
    values = [(k, unquote(v)) for k, v in match.groupdict().items()]
    rest = ", ".join(f"{k} {v}" for k, v in values[1:])
    return values[0][1] + (f" ({rest})" if rest else "")


def secret_fields(body: Any) -> list[tuple[str, str]]:
    """Each secret reference in the body with the field it fills, as a dotted path, in order."""
    out: list[tuple[str, str]] = []

    def walk(v: Any, where: str) -> None:
        if isinstance(v, dict):
            for k, item in v.items():
                walk(item, f"{where}.{k}" if where else str(k))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{where}[{i}]")
        elif isinstance(v, str) and sanitize.is_reference(v):
            out.append((v, where))

    walk(body, "")
    return out


def connection_fields(body: Any) -> list[tuple[str, str]]:
    """Every scalar under a key that names where the request connects (host, url, account, ...), as dotted
    paths, secrets left out. Shown whole, whatever the body's length."""
    out: list[tuple[str, str]] = []

    def walk(v: Any, where: str) -> None:
        if isinstance(v, dict):
            for k, item in v.items():
                path = f"{where}.{k}" if where else str(k)
                if isinstance(item, dict | list):
                    walk(item, path)
                elif (isinstance(k, str) and CONNECTION_KEY.search(k) and not sanitize.is_secret_key(k)
                      and item not in (None, "")):
                    text = str(sanitize.clean(item))
                    out.append((path, text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "..."))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                walk(item, f"{where}[{i}]")

    walk(body, "")
    return out


def _context() -> Any:
    try:
        from fastmcp.server.dependencies import get_context

        return get_context()
    except RuntimeError:
        return None


def _is_modern(ctx: Any) -> bool:
    try:
        rc = ctx.request_context
    except (AttributeError, RuntimeError, LookupError):
        return False
    return rc is not None and getattr(rc, "protocol_version", None) in MODERN_PROTOCOL_VERSIONS


def _can_elicit(ctx: Any) -> bool:
    """Whether the client said at initialize that it can show prompts. Unknown (a stateless HTTP request
    without initialize data) counts as yes: the protocol version already says it has the channel."""
    try:
        params = ctx.session.client_params
    except (AttributeError, RuntimeError):
        return True
    if params is None or params.capabilities is None:
        return True
    return params.capabilities.elicitation is not None


class Approvals:
    """Decides whether a call that needs approval may go ahead."""

    def __init__(self) -> None:
        self._used: dict[str, float] = {}  # approval prompts already answered: nonce -> expiry

    async def check(self, ep: Endpoint, region: str, path: str | list[str], query: dict[str, Any], body: Any) -> Any:
        """None when the user approved this exact call; otherwise a shaped error, or (MCP 2026-07-28) the
        input request to return to the client so it asks the user."""
        start = time.perf_counter()
        ctx = _context()
        if ctx is None:
            return self._unavailable(ep, start, "no MCP request is in progress")
        message = prompt(ep, region, path, query, body)
        fingerprint = call_fingerprint(ep, region, path, query, body)
        if _is_modern(ctx):
            return self._modern(ctx, ep, message, fingerprint, start)
        return await self._legacy(ctx, ep, message, start)

    def _modern(self, ctx: Any, ep: Endpoint, message: str, fingerprint: str, start: float) -> Any:
        if not _can_elicit(ctx):
            return self._unavailable(ep, start, "this MCP client cannot show approval prompts")
        responses, state = ctx.input_responses, ctx.request_state
        if responses is not None and state:
            nonce = self._redeem(state, fingerprint)
            if nonce is not None:
                return self._verdict(ep, responses.get(REQUEST_KEY), start)
        # First round, or an answer for a different, expired or already-used prompt: ask (again).
        now = time.time()
        sealed = json.dumps({"call": fingerprint, "nonce": uuid.uuid4().hex, "exp": now + STATE_TTL_SECONDS})
        observability.note_approval("requested", (time.perf_counter() - start) * 1000)
        from fastmcp.tools.base import InputRequiredToolResult

        request = mcp_types.ElicitRequest(params=mcp_types.ElicitRequestFormParams(
            message=message, requested_schema=SCHEMA))
        return InputRequiredToolResult(mcp_types.InputRequiredResult(
            input_requests={REQUEST_KEY: request}, request_state=sealed))

    def _redeem(self, state: str, fingerprint: str) -> str | None:
        """The prompt's nonce, if `state` is a live, unused prompt for this exact call; marks it used."""
        try:
            data = json.loads(state)
        except (TypeError, ValueError):
            return None
        now = time.time()
        if not isinstance(data, dict) or data.get("call") != fingerprint or not isinstance(data.get("nonce"), str):
            return None
        if not isinstance(data.get("exp"), int | float) or data["exp"] < now or data["nonce"] in self._used:
            return None
        self._used = {n: exp for n, exp in self._used.items() if exp >= now}
        self._used[data["nonce"]] = data["exp"]
        return data["nonce"]

    async def _legacy(self, ctx: Any, ep: Endpoint, message: str, start: float) -> Any:
        try:
            session = ctx.session
            params = session.client_params
        except (AttributeError, RuntimeError):
            session, params = None, None
        capabilities = params.capabilities if params is not None else None
        if session is None or capabilities is None or capabilities.elicitation is None:
            reason = ("this MCP client cannot show approval prompts" if params is not None else
                      "this connection cannot show approval prompts, because an HTTP client on an MCP version "
                      "before 2026-07-28 has no channel for them")
            return self._unavailable(ep, start, reason)
        try:
            answer = await asyncio.wait_for(
                session.elicit(message=message, requested_schema=SCHEMA, related_request_id=ctx.request_id),
                WAIT_SECONDS)
        except TimeoutError:
            observability.note_approval("timeout", (time.perf_counter() - start) * 1000)
            return observability.shaped_error(
                "APPROVAL_TIMEOUT", f"Nobody answered the approval prompt for {ep.name} within {WAIT_SECONDS // 60} "
                "minutes. Nothing was sent to Hevo. Ask the user whether they still want it.", endpoint=ep.name)
        except Exception:  # noqa: BLE001 - the client refused or couldn't show it: never approval
            return self._unavailable(ep, start, "the MCP client couldn't show the approval prompt")
        return self._verdict(ep, answer, start)

    def _verdict(self, ep: Endpoint, answer: Any, start: float) -> dict[str, Any] | None:
        action = getattr(answer, "action", None)
        content = getattr(answer, "content", None)
        ms = (time.perf_counter() - start) * 1000
        if action == "accept" and (content or {}).get("decision", APPROVE) == APPROVE:
            observability.note_approval("approved", ms)
            return None
        outcome = "cancelled" if action == "cancel" else "declined"
        observability.note_approval(outcome, ms)
        # The client may have answered by itself (Claude Code in print mode, including the VS Code extension,
        # declines every prompt), so this can't claim the user said no.
        return observability.shaped_error(
            "APPROVAL_DECLINED", f"The approval prompt for {ep.name} was rejected or dismissed. "
            "Nothing was sent to Hevo. Some clients reject prompts without showing them, such as Claude Code in "
            "print mode or in its VS Code extension. If no prompt appeared, the change can be made from Claude Code "
            "in an interactive terminal session (not print mode) or in the Hevo UI. Do not retry or look for another "
            "way to make this change unless asked again.", endpoint=ep.name, approval=outcome)

    @staticmethod
    def _unavailable(ep: Endpoint, start: float, reason: str) -> dict[str, Any]:
        observability.note_approval("unavailable", (time.perf_counter() - start) * 1000)
        return observability.shaped_error(
            "APPROVAL_UNAVAILABLE", f"{ep.name} needs the user's approval, and {reason}. Nothing was sent to Hevo. "
            "Use an MCP client that supports approval prompts, such as Claude Code in an interactive terminal "
            "session (not print mode), or make this change in the Hevo UI.", endpoint=ep.name)
