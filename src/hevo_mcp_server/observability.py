"""Structured logs for every tool call, an audit stream for writes, and request ids sent to Hevo.

Logs never contain API keys, request bodies or Hevo responses: bodies can hold Source passwords and
responses hold customer data. Callers are identified by a hash of their key.

* `hevo_mcp` logger: one `tool_call` record per tool call (and `hevo_request` records at DEBUG).
* `hevo_mcp.audit` logger: one `write` record per call to a write or delete endpoint, refused ones too,
  with the user's approval decision for endpoints that need one (approval.py).
* `auth_rejected` records for HTTP requests turned away before any tool runs (missing or bad key).
* `tool_exception` records (ERROR, with the traceback) for unexpected exceptions inside a tool; the
  client only sees INTERNAL_ERROR and the request id.

Records go to stderr, and to Coralogix when CORALOGIX_PRIVATE_KEY is set (see coralogix.py).
"""

import asyncio
import atexit
import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from typing import Any

from fastmcp.exceptions import FastMCPError, ToolError
from fastmcp.exceptions import ValidationError as ArgumentsInvalid
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from pydantic import ValidationError as PydanticValidationError

from hevo_mcp_server import coralogix
from hevo_mcp_server.catalog import BY_NAME, BY_TOOL

try:
    VERSION = metadata.version("hevo-mcp")
except metadata.PackageNotFoundError:
    VERSION = "dev"
USER_AGENT = f"hevo-mcp/{VERSION}"
MODE = "stdio"  # "http" when hosted; set by the server at build time, shown in the outbound User-Agent
REQUEST_ID_HEADER = "X-Request-Id"
# Sent to Hevo on a call the user approved in their MCP client, so Hevo's API logs carry the decision too.
APPROVAL_HEADER = "X-Hevo-Mcp-Approval"
# Error codes that mean the server declined to call Hevo, as opposed to a failed call.
REFUSALS = {"GUARD_REFUSED", "READ_ONLY", "SCOPE_EXCEEDED", "RATE_LIMITED", "ENDPOINT_DISABLED",
            "ENDPOINT_TOOL_MISMATCH", "UNKNOWN_ENDPOINT", "MISSING_PATH_PARAM", "INVALID_BODY", "RESULT_TOO_LARGE",
            "INVALID_ARGUMENTS", "APPROVAL_DECLINED", "APPROVAL_UNAVAILABLE", "APPROVAL_TIMEOUT"}

log = logging.getLogger("hevo_mcp")
audit = logging.getLogger("hevo_mcp.audit")


@dataclass
class CallContext:
    """What one tool call did, filled in as it runs."""

    request_id: str
    endpoint: str | None = None  # endpoint name a group tool dispatched to
    region: str | None = None
    caller: str | None = None
    scope: str | None = None
    result_bytes: int | None = None  # size of the JSON result handed to the client
    hevo_calls: int = 0
    retries: int = 0
    status: int | None = None  # last HTTP status from Hevo
    error_reference_id: str | None = None
    error_code: str | None = None  # the `error` code of a returned error result
    client: str | None = None  # the agent's MCP client (clientInfo name, else its User-Agent), as a short slug
    # The user's approval, for endpoints that need one: requested (prompt sent, MCP 2026-07-28), approved,
    # declined, cancelled, unavailable (the client can't ask) or timeout. None: no approval needed.
    approval: str | None = None
    approval_ms: float | None = None  # time spent waiting for the user's answer


_current: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("hevo_call", default=None)


def current() -> CallContext | None:
    return _current.get()


def _slug(raw: str | None) -> str | None:
    """Lowercase letters, digits, dots and dashes only, at most 48 characters: safe in a header and a log."""
    if not raw:
        return None
    slug = re.sub(r"[^a-z0-9.]+", "-", raw.lower()).strip("-")[:48].strip("-")
    return slug or None


def client_name(context: MiddlewareContext) -> str | None:
    """Which MCP client is calling: the name it gave at initialize, else its HTTP User-Agent.

    Stateless HTTP requests often carry no initialize data, so the User-Agent header is the fallback.
    """
    try:
        params = context.fastmcp_context.session.client_params  # type: ignore[union-attr]
        name = params.client_info.name if params and params.client_info else None
    except (AttributeError, RuntimeError):  # no session (outside a request) or no initialize data
        name = None
    return _slug(name or get_http_headers().get("user-agent"))


def user_agent() -> str:
    """Outbound User-Agent for Hevo: server version, transport and calling client, so Hevo's API logs
    show which agents use the MCP server, e.g. `hevo-mcp/1.0.0 (http; claude-code)`."""
    ctx = current()
    return f"{USER_AGENT} ({MODE}; {(ctx.client if ctx else None) or 'unknown'})"


def note_caller(creds: Any) -> None:
    ctx = current()
    if ctx is not None:
        ctx.region, ctx.caller, ctx.scope = creds.region, creds.caller_id, creds.scope


def note_result_bytes(n: int) -> None:
    ctx = current()
    if ctx is not None:
        ctx.result_bytes = n


def note_endpoint(ep: Any) -> None:
    ctx = current()
    if ctx is not None:
        ctx.endpoint = ep.name


def note_approval(outcome: str, waited_ms: float) -> None:
    ctx = current()
    if ctx is not None:
        ctx.approval, ctx.approval_ms = outcome, round(waited_ms, 1)


def note_error(code: str | None) -> None:
    ctx = current()
    if ctx is not None:
        ctx.error_code = code


def shaped_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """The one error shape every tool returns; records the code for the tool_call log."""
    note_error(code)
    ctx = current()
    return {"error": code, "message": message, **extra, "request_id": ctx.request_id if ctx else None}


def error_result(error: dict[str, Any]) -> ToolResult:
    return ToolResult(content=json.dumps(error, indent=2, default=str), structured_content=error, is_error=True)


def _argument_problems(exc: ArgumentsInvalid) -> list[str]:
    cause = exc.__cause__
    if isinstance(cause, PydanticValidationError):
        return [f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in cause.errors()]
    return [str(exc)]


def record_hevo_response(status: int, payload: Any, attempt: int, duration_ms: float, method: str) -> None:
    ctx = current()
    if ctx is not None:
        ctx.hevo_calls += 1
        ctx.retries += attempt > 1
        ctx.status = status
        if status >= 400 and isinstance(payload, dict):
            ref = payload.get("error_reference_id")
            if not ref and isinstance(payload.get("error"), dict):
                ref = payload["error"].get("error_reference_id")
            ctx.error_reference_id = ref or ctx.error_reference_id
    log.debug("hevo_request", extra={"fields": {
        "event": "hevo_request", "request_id": ctx.request_id if ctx else None, "method": method,
        "status": status, "attempt": attempt, "duration_ms": round(duration_ms, 1),
    }})


def _audit_params(arguments: dict[str, Any]) -> dict[str, Any]:
    # Ids and flags identify what was changed; names, emails and bodies stay out of the log.
    out: dict[str, Any] = {}
    path_params = arguments.get("path_params")
    for k, v in (path_params.items() if isinstance(path_params, dict) else ()):
        if (k == "id" or k.endswith("_id")) and isinstance(v, int | str):
            out[k] = v
    batch = arguments.get("batch")
    if isinstance(batch, list):
        out["batch"] = len(batch)  # how many targets; their ids are in each target's result
    query = arguments.get("query")
    for k, v in (query.items() if isinstance(query, dict) else ()):
        if isinstance(v, bool):
            out[k] = v
    return out


class ToolCallLogging(Middleware):
    """Gives each tool call a request id, logs its outcome, and audits writes."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        name = context.message.name
        arguments = dict(context.message.arguments or {})
        ctx = CallContext(request_id=uuid.uuid4().hex, client=client_name(context))
        token = _current.set(ctx)
        start = time.perf_counter()
        outcome, error = "ok", None
        try:
            result = await call_next(context)
            if ctx.error_code and isinstance(result, ToolResult) and not result.is_error:
                # Shaped errors are results, so the agent can read the code; isError tells the client too.
                result = ToolResult(content=result.content, structured_content=result.structured_content,
                                    meta=result.meta, is_error=True)
            return result
        except ArgumentsInvalid as exc:
            # A helper tool's arguments failed validation (group tools check their own in run()).
            problems = _argument_problems(exc)
            return error_result(shaped_error(
                "INVALID_ARGUMENTS", f"The arguments don't match {name}'s schema: " + "; ".join(problems) + ".",
                problems=problems))
        except ToolError as exc:
            cause = exc.__cause__
            if cause is not None and not isinstance(cause, FastMCPError):
                # FastMCP wraps an unexpected exception in a ToolError. Its message can hold internals,
                # so the client gets a generic error and the details stay in this server's logs.
                log.error("tool_exception", exc_info=cause, extra={"fields": {
                    "event": "tool_exception", "request_id": ctx.request_id, "tool": name,
                    "exception": type(cause).__name__}})
                return error_result(shaped_error(
                    "INTERNAL_ERROR", "The MCP server hit an unexpected error. Retry once. If the error occurs again, "
                    "contact Hevo support with the request ID.", exception=type(cause).__name__))
            outcome, error = "error", type(exc).__name__
            # The id lets a user quote this call to Hevo support.
            raise type(exc)(f"{exc} (request ID {ctx.request_id})") from None
        except asyncio.CancelledError:
            # The client gave up (disconnect, timeout, Ctrl-C) while Hevo may still be processing the request:
            # the outcome is unknown, never "ok". The audit record says so.
            outcome, error = "cancelled", "CancelledError"
            raise
        except BaseException as exc:
            outcome, error = "error", type(exc).__name__
            raise
        finally:
            _current.reset(token)
            if outcome == "ok" and ctx.error_code:
                outcome, error = ("refused" if ctx.error_code in REFUSALS else "error"), ctx.error_code
            elif outcome == "ok" and ctx.approval == "requested":
                outcome = "awaiting_approval"  # the client asks the user, then calls again with the answer
            group = BY_TOOL.get(name)
            ep = BY_NAME.get(ctx.endpoint or "") if group else None
            fields = {
                "event": "tool_call", "request_id": ctx.request_id, "tool": name, "endpoint": ctx.endpoint,
                "product": group.product if group else "helper", "action": group.action if group else "read",
                "destructive": ep.destructive if ep else False,
                "method": ep.method if ep else None, "path": ep.path if ep else None,
                "region": ctx.region, "caller": ctx.caller, "scope": ctx.scope, "client": ctx.client,
                "outcome": outcome, "error": error, "approval": ctx.approval, "approval_ms": ctx.approval_ms,
                "status": ctx.status, "error_reference_id": ctx.error_reference_id,
                "hevo_calls": ctx.hevo_calls, "retries": ctx.retries, "fetch_all": bool(arguments.get("fetch_all")),
                "result_bytes": ctx.result_bytes,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            }
            log.log(logging.WARNING if outcome == "error" else logging.INFO, "tool_call", extra={"fields": fields})
            if group is not None and group.is_write:
                audit.info("write", extra={"fields": {
                    **{k: fields[k] for k in ("request_id", "tool", "endpoint", "product", "action", "destructive",
                                               "method", "path", "region", "caller", "client", "outcome", "status",
                                               "error_reference_id", "approval", "approval_ms")},
                    "event": "write", "params": _audit_params(arguments),
                    **({"write_outcome": "unknown"} if outcome == "cancelled" else {}),
                }})


class RejectedRequestLogging:
    """ASGI middleware that logs HTTP requests the auth layer refuses (401/403).

    These never reach a tool, so ToolCallLogging can't see them. Logs why (no token, not a Bearer
    token, or a token that isn't base64 key:secret), never the token itself.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()

        async def watch(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and message["status"] in (401, 403):
                _log_rejected(scope, message["status"], (time.perf_counter() - start) * 1000)
            await send(message)

        await self.app(scope, receive, watch)


def _log_rejected(scope: dict[str, Any], status: int, duration_ms: float) -> None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if not auth:
        reason = "missing_token"
    elif not auth.lower().startswith("bearer "):
        reason = "not_bearer"
    else:
        reason = "invalid_token" if status == 401 else "forbidden"
    # client_ip is the address uvicorn resolved: the connection's peer, or X-Forwarded-For only when that peer
    # is a trusted proxy (uvicorn's forwarded_allow_ips, localhost by default). The raw header can be set by
    # anyone, so it is logged as given in its own field and never taken as the address by this code.
    forwarded = headers.get("x-forwarded-for", "")[:200] or None
    log.warning("auth_rejected", extra={"fields": {
        "event": "auth_rejected", "status": status, "reason": reason, "method": scope.get("method"),
        "path": scope.get("path"), "client_ip": (scope.get("client") or [None])[0], "forwarded_for": forwarded,
        "user_agent": (headers.get("user-agent") or "")[:200] or None,
        "region": headers.get("x-hevo-region"), "duration_ms": round(duration_ms, 1),
    }})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        out.update(fields if isinstance(fields, dict) else {"message": record.getMessage()})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", None)
        if not isinstance(fields, dict):
            return f"{record.levelname} {record.name} {record.getMessage()}"
        pairs = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, False) and k != "event")
        return f"{record.levelname} {record.name} {fields.get('event', record.getMessage())} {pairs}"


def configure_logging(subsystem: str = "stdio") -> None:
    """Logs go to stderr (stdout carries the MCP protocol in stdio mode).

    JSON lines at INFO. HEVO_MCP_LOG_LEVEL (DEBUG or WARNING) is the one knob, for support debugging a
    customer install; the hosted server configures the rest through CORALOGIX_* (see
    coralogix.handler_from_env; `subsystem` is the default Coralogix subsystem). The audit records go to the
    same stream (and to Coralogix where it is on).
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    log.handlers[:] = [handler]
    log.setLevel(os.environ.get("HEVO_MCP_LOG_LEVEL", "INFO").upper())
    log.propagate = False
    audit.handlers.clear()
    shipper = coralogix.handler_from_env(JsonFormatter(), subsystem)
    if shipper is not None:
        log.addHandler(shipper)  # audit records reach it through propagation
        atexit.register(shipper.close)
