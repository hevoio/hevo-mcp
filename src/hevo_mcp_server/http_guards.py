"""ASGI middleware that runs before the MCP layer sees a request."""

import contextlib
import json
from typing import Any


class MaxBodySize:
    """Rejects HTTP request bodies above `limit` bytes with 413.

    A declared Content-Length above the limit is refused before the body is read; a body that grows
    past the limit while streaming is cut off. MCP requests are a few KB; the cap stops a client from
    pushing megabytes through the JSON parser.
    """

    def __init__(self, app: Any, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self.limit <= 0:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.limit:
            await self._reject(send)
            return
        seen = 0
        tripped = False

        async def counted() -> dict[str, Any]:
            nonlocal seen, tripped
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    tripped = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counted, send)
        if tripped:
            # The app saw a disconnect and sent nothing; tell the client why. The connection may already be gone.
            with contextlib.suppress(Exception):
                await self._reject(send)

    async def _reject(self, send: Any) -> None:
        body = json.dumps({"error": "PAYLOAD_TOO_LARGE",
                           "message": f"Request body exceeds {self.limit} bytes."}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
