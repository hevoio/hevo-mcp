import argparse
import logging
import os
from dataclasses import replace

from sse_starlette.sse import AppStatus
from starlette.middleware import Middleware

from hevo_mcp_server import pins
from hevo_mcp_server.auth import HevoKeyVerifier
from hevo_mcp_server.http_guards import MaxBodySize
from hevo_mcp_server.observability import (
    VERSION,
    RejectedRequestLogging,
    configure_logging,
)
from hevo_mcp_server.settings import Settings
from hevo_mcp_server.tools import create_server, risky_endpoints


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def http_run_options(settings: Settings) -> dict:
    """Keyword arguments for `run_http_async` in a hosted deployment; tests start the server with these too."""
    # Tool results stream back as server-sent events, and sse-starlette closes every open event stream
    # the moment uvicorn receives SIGTERM, which cut off every in-flight call on each deploy. Stateless
    # mode has no long-lived streams (GET returns 405), so each one ends when its call does, and
    # uvicorn's graceful timeout below still caps the wait.
    AppStatus.disable_automatic_graceful_drain()
    return {
        # Stateless: every request carries its own key and region and nothing is kept between requests,
        # so any replica can answer any request and no sessions accumulate in memory.
        "stateless_http": True,
        "middleware": [Middleware(MaxBodySize, limit=settings.max_body_bytes), Middleware(RejectedRequestLogging)],
        "host_origin_protection": "auto",
        "allowed_hosts": list(settings.allowed_hosts) or None,
        # Always an explicit list, even when empty: a request that carries an Origin header comes from a
        # browser page, and only the origins listed here (or the server's own) may call the server.
        # Agent backends and desktop clients send no Origin header and are unaffected.
        "allowed_origins": list(settings.allowed_origins),
        # FastMCP's default is 2 s, which would cut off an in-flight Edge create on every deploy.
        "uvicorn_config": {"timeout_graceful_shutdown": settings.shutdown_grace_seconds},
    }


def main():
    parser = argparse.ArgumentParser(description="Hevo MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("HEVO_MCP_TRANSPORT", "stdio"),
                        help="stdio: local, key from ~/.hevo/hevo-key.json. http: shared, every caller sends their own key.")
    parser.add_argument("--host", default=os.environ.get("HEVO_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HEVO_MCP_PORT", "8000")))
    parser.add_argument("--path", default="/mcp")
    parser.add_argument("--config", default=os.environ.get("HEVO_MCP_CONFIG", ""),
                        help="TOML settings file (timeouts, limits, rate limits, access, http guards); see "
                             "config/hevo_mcp.toml.")
    parser.add_argument("--allowed-hosts", default="",
                        help="comma-separated Host header values to accept (http); overrides [http] allowed_hosts "
                             "and HEVO_MCP_ALLOWED_HOSTS. Unset everywhere: only a server bound to localhost checks it.")
    parser.add_argument("--allowed-origins", default="",
                        help="comma-separated browser origins allowed to call the server (http); overrides "
                             "[http] allowed_origins and HEVO_MCP_ALLOWED_ORIGINS. Other Origin headers get 403.")
    args = parser.parse_args()
    configure_logging(subsystem=args.transport)
    log = logging.getLogger("hevo_mcp")
    settings = Settings.load(args.config or None)
    if args.allowed_hosts:
        settings = replace(settings, allowed_hosts=tuple(_csv(args.allowed_hosts)))
    if args.allowed_origins:
        settings = replace(settings, allowed_origins=tuple(_csv(args.allowed_origins)))
    log.info("server_start", extra={"fields": {
        "event": "server_start", "version": VERSION, "transport": args.transport, "config": args.config or None,
        "dependencies": pins.dependency_status(), "settings": settings.as_log_fields()}})
    if args.transport == "http" and (risky := risky_endpoints(settings)):
        # A shared server's operator chose the default scope for everyone; a local user who set
        # HEVO_MCP_DEFAULT_SCOPE=write opted in on purpose, so they aren't warned at every start.
        log.warning("risky_endpoints_reachable", extra={"fields": {
            "event": "risky_endpoints_reachable", **risky,
            "hint": "the default scope lets a caller reach data-losing endpoints without asking for a wider scope. "
                    "Add them to [access] disabled_endpoints (see config/hevo_mcp.toml) to take them out of reach."}})
    if args.transport == "http" and not settings.allowed_regions:
        log.warning("regions_not_pinned", extra={"fields": {
            "event": "regions_not_pinned",
            "hint": "this server routes calls to any Hevo region. To limit it to the regions it should serve, set "
                    "HEVO_MCP_ALLOWED_REGIONS."}})
    if args.transport == "http" and settings.shutdown_grace_seconds < settings.write_timeout_seconds:
        log.warning("shutdown_grace_seconds (%ss) is shorter than write timeout (%ss): a deploy can cut "
                    "off a long write mid-call", settings.shutdown_grace_seconds, settings.write_timeout_seconds)

    try:
        if args.transport == "stdio":
            create_server(settings=settings).run(transport="stdio")
            return
        base_url = os.environ.get("HEVO_MCP_PUBLIC_URL", f"http://{args.host}:{args.port}")
        server = create_server(remote=True, auth=HevoKeyVerifier(base_url=base_url), settings=settings)
        server.run(transport="http", host=args.host, port=args.port, path=args.path, **http_run_options(settings))
    finally:
        log.info("server_stop", extra={"fields": {"event": "server_stop", "version": VERSION,
                                                  "transport": args.transport}})


if __name__ == "__main__":
    main()
