"""HTTP calls to Hevo, with the caller's own credentials on every request.

One httpx client is shared by every call for the life of the process (connection pool), opened on
first use and closed by the server's lifespan. Reads are retried on connection failures, 429 and 5xx,
but not on a read that timed out: that already used its whole timeout, and retrying would keep the
agent waiting past most clients' patience. Writes are never retried, since a write that timed out may
still have happened.

Every failure leaves as one of three exceptions the tool layer turns into a shaped error:
HevoHTTPError (Hevo answered 4xx/5xx), HevoUnreachable (no answer: network failure or timeout) and
HevoInvalidResponse (a 2xx that isn't JSON, e.g. an HTML page from a proxy).
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2 as httpx

from hevo_mcp_server import observability
from hevo_mcp_server.auth import HevoCredentials
from hevo_mcp_server.settings import Settings

READ_RETRIES = 3
MAX_RETRY_AFTER = 30.0  # honour Hevo's Retry-After on a retried read, up to this many seconds


class _NoCookies(httpx.AsyncBaseTransport):
    """Drops Set-Cookie from every response, so the shared client keeps no cookie jar: on a shared HTTP
    server a cookie set for one caller must never travel with another caller's request."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if "set-cookie" in response.headers:
            response.headers = httpx.Headers([(k, v) for k, v in response.headers.raw if k.lower() != b"set-cookie"])
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class HevoHTTPError(Exception):
    """Hevo answered with a 4xx or 5xx. The tool layer turns it into a result the agent can act on."""

    def __init__(self, status: int, method: str, path: str, payload: Any, headers: httpx.Headers):
        super().__init__(f"Hevo returned HTTP {status} for {method} {path}")
        self.status, self.method, self.path, self.payload, self.headers = status, method, path, payload, headers


class HevoUnreachable(Exception):
    """No answer from Hevo: the connection failed or a timeout ran out."""

    def __init__(self, method: str, path: str, reason: str, timed_out: bool, sent: bool):
        super().__init__(f"{reason} calling Hevo for {method} {path}")
        self.method, self.path, self.reason = method, path, reason
        self.timed_out = timed_out
        self.sent = sent  # the request may have reached Hevo, so a write's outcome is unknown


class HevoInvalidResponse(Exception):
    """Hevo (or something in front of it) answered 2xx with a body that isn't JSON."""

    def __init__(self, status: int, method: str, path: str, content_type: str):
        super().__init__(f"Hevo returned a non-JSON {content_type or 'body'} for {method} {path}")
        self.status, self.method, self.path, self.content_type = status, method, path, content_type


# Failures where the request never reached Hevo, so even a write could be retried safely.
NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
# Timeouts that already spent the whole budget waiting for Hevo: never retried.
SPENT = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)


class HevoClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, settings: Settings | None = None):
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.settings = settings or Settings()
        s = self.settings
        self.read_timeout = httpx.Timeout(float(s.read_timeout_seconds), connect=float(s.connect_timeout_seconds))
        # Edge pipeline create tests both connections and can take minutes.
        self.write_timeout = httpx.Timeout(float(s.write_timeout_seconds), connect=float(s.connect_timeout_seconds))
        self.limits = httpx.Limits(max_connections=s.max_connections,
                                   max_keepalive_connections=s.max_keepalive_connections)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            inner = self._transport or httpx.AsyncHTTPTransport(limits=self.limits)
            self._http = httpx.AsyncClient(transport=_NoCookies(inner), timeout=self.read_timeout,
                                           follow_redirects=False)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(
        self,
        creds: HevoCredentials,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        headers = {"Authorization": f"Basic {creds.basic_token}", "Accept": "application/json",
                   "User-Agent": observability.user_agent()}
        ctx = observability.current()
        if ctx is not None:
            # Lets Hevo's API logs be matched to this tool call.
            headers[observability.REQUEST_ID_HEADER] = ctx.request_id
            if ctx.approval == "approved":
                headers[observability.APPROVAL_HEADER] = "approved"
        http = self._client()
        url = creds.base_url + path
        timeout = self.read_timeout if method == "GET" else self.write_timeout
        attempt, delay = 1, 1.0
        while True:
            start = time.perf_counter()
            try:
                resp = await http.request(method, url, params=_query(query), headers=headers, json=body,
                                          timeout=timeout)
            except httpx.TransportError as exc:
                if method == "GET" and attempt < READ_RETRIES and not isinstance(exc, SPENT):
                    await asyncio.sleep(delay)
                    attempt, delay = attempt + 1, delay * 2
                    continue
                raise HevoUnreachable(method, path, type(exc).__name__,
                                      timed_out=isinstance(exc, httpx.TimeoutException),
                                      sent=not isinstance(exc, NOT_SENT)) from None
            payload, is_json = _json(resp)
            observability.record_hevo_response(
                resp.status_code, payload, attempt, (time.perf_counter() - start) * 1000, method)
            if method == "GET" and attempt < READ_RETRIES and (resp.status_code == 429 or resp.status_code >= 500):
                await asyncio.sleep(_retry_after(resp.headers, delay))
                attempt, delay = attempt + 1, delay * 2
                continue
            break
        if not 200 <= resp.status_code < 300:
            # A redirect is not a success either: Hevo's API never redirects, and following one could send the
            # key elsewhere, so the agent sees it as an error rather than as a change that happened.
            raise HevoHTTPError(resp.status_code, method, path, payload, resp.headers)
        if not is_json:
            raise HevoInvalidResponse(resp.status_code, method, path, resp.headers.get("content-type", ""))
        if payload is None:
            return {"status": resp.status_code}
        return payload

    async def get_all(self, creds: HevoCredentials, path: str, query: dict[str, Any],
                      deadline_seconds: float | None = None,
                      on_page: Callable[[int, int], Awaitable[None]] | None = None) -> dict[str, Any]:
        """Walks Standard (`pagination.starting_after`) and Edge (`has_more`/`next_cursor`) cursors.

        Stops at the settings' row and page caps and then reports `truncated: true` with the
        `next_cursor` to continue from, so one call can't pull an unbounded list into memory. The same
        happens when `deadline_seconds` runs out after at least one page: the agent gets the rows so far
        and the cursor of the page that didn't arrive, instead of a timeout that throws them away.
        """
        standard = path.startswith("/api/public/")
        cursor_param = "starting_after" if standard else "cursor"
        query = {"limit": 100 if standard else 500, **query}
        page_limit = query["limit"]
        rows: list[Any] = []
        seen: set[str] = set()
        pages = 0
        size = 2  # bytes of the rows so far, as the result will carry them
        byte_cap = self.settings.max_result_bytes or 0
        end = time.monotonic() + deadline_seconds if deadline_seconds else None

        def partial(cursor: str, reason: str) -> dict[str, Any]:
            return {"data": rows, "count": len(rows), "pages": pages, "truncated": True,
                    "truncated_reason": reason, "next_cursor": cursor, "cursor_param": cursor_param}

        while True:
            try:
                if isinstance(page_limit, int) and page_limit > 0:
                    # Never ask for more rows than the cap leaves, so a page can't overshoot it.
                    query["limit"] = max(min(page_limit, self.settings.max_rows - len(rows)), 1)
                if end is None:
                    page = await self.request(creds, "GET", path, query)
                else:
                    async with asyncio.timeout(max(end - time.monotonic(), 0.001)):
                        page = await self.request(creds, "GET", path, query)
            except (TimeoutError, HevoUnreachable) as exc:
                if not pages or (isinstance(exc, HevoUnreachable) and not exc.timed_out):
                    raise
                return partial(query[cursor_param], "deadline")
            pages += 1
            data = page.get("data") if isinstance(page, dict) else page
            data = data if isinstance(data, list) else []
            cursor = _next_cursor(page)
            if byte_cap and rows:
                # Under the result cap, the rows so far plus this page would be refused whole and the agent
                # would get nothing: hand back the rows that fit and the cursor of this page instead.
                size += len(json.dumps(data, separators=(",", ":"), default=str)) + 1
                if size > byte_cap - byte_cap // 8:
                    return partial(query[cursor_param], "size")
            rows.extend(data)
            if on_page is not None:
                await on_page(pages, len(rows))
            if not cursor:
                return {"data": rows, "count": len(rows), "pages": pages}
            if cursor in seen:
                return partial(cursor, "cursor_repeated")
            if pages >= self.settings.max_pages or len(rows) >= self.settings.max_rows:
                return partial(cursor, "cap")
            seen.add(cursor)
            query = {**query, cursor_param: cursor}


def _retry_after(headers: httpx.Headers, default: float) -> float:
    """Hevo's Retry-After in seconds when it sends one (capped), else the backoff delay."""
    raw = headers.get("retry-after", "")
    try:
        return min(max(float(raw), default), MAX_RETRY_AFTER) if raw else default
    except ValueError:
        return default


def _query(query: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (query or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, dict) or (isinstance(v, list) and any(isinstance(i, dict | list) for i in v)):
            v = json.dumps(v, separators=(",", ":"))  # never Python's repr of a nested value
        out[k] = v
    return out


def _next_cursor(page: Any) -> str | None:
    if not isinstance(page, dict) or not page.get("data"):
        return None
    if isinstance(page.get("pagination"), dict):
        return page["pagination"].get("starting_after") or None
    return (page.get("next_cursor") or None) if page.get("has_more") else None


def _json(resp: httpx.Response) -> tuple[Any, bool]:
    """The parsed body and whether it was JSON (an empty body counts as JSON: nothing to parse)."""
    if not resp.content:
        return None, True
    try:
        return resp.json(), True
    except ValueError:
        return resp.text[:2000], False
