"""MCP progress notifications for calls that wait on Hevo.

An Edge pipeline create can take about 5 minutes and a big `fetch_all` walks many pages. Clients that
sent a `progressToken` get a notification every `progress_interval_seconds` while Hevo is working, and
one per page during `fetch_all`. Clients that honour progress (for example by resetting their request
timeout) then keep waiting instead of giving up; clients that sent no token get nothing, at no cost.

Progress values only ever increase within a call (the spec requires it), so every notification simply
counts up; `message` carries the human-readable state.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger("hevo_mcp")


class Progress:
    """Reports progress for one tool call; a no-op when the client asked for none."""

    def __init__(self, ctx: Any):
        self._ctx = ctx
        self._step = 0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._ctx is not None

    async def report(self, message: str) -> None:
        if self._ctx is None:
            return
        async with self._lock:
            self._step += 1
            try:
                await self._ctx.report_progress(self._step, None, message)
            except Exception:
                log.debug("progress notification failed", exc_info=True)
                self._ctx = None


def current() -> Progress:
    """The progress reporter for the running tool call (no-op outside one, or without a progressToken)."""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        rc = ctx.request_context
        meta = getattr(rc, "meta", None) if rc is not None else None
        token = meta.get("progress_token") if meta is not None else None
        if token is None and rc is not None:
            srctx = getattr(rc, "_srctx", None)  # where FastMCP 4 keeps the request's _meta
            inner = getattr(srctx, "meta", None)
            token = inner.get("progress_token") if inner is not None else None
    except Exception:  # noqa: BLE001 - no MCP request in scope (direct calls, tests)
        return Progress(None)
    return Progress(ctx if token is not None else None)


@contextlib.asynccontextmanager
async def heartbeat(progress: Progress, what: str, interval: float) -> AsyncIterator[None]:
    """While the body runs, report "waiting for Hevo" every `interval` seconds."""
    if not progress.enabled or interval <= 0:
        yield
        return
    start = time.monotonic()

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            await progress.report(f"{what}: waiting for Hevo, {round(time.monotonic() - start)}s so far")

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
