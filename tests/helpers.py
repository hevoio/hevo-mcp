"""Shared test doubles: a fake Hevo API and MCP client helpers."""

import asyncio

import httpx2 as httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError


class FakeHevo:
    """Records requests and answers from a route table."""

    def __init__(self, routes=None):
        self.calls: list[httpx.Request] = []
        self.routes = routes or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        handler = self.routes.get(key)
        if handler is None:
            return httpx.Response(200, json={"data": None})
        return handler(request) if callable(handler) else httpx.Response(200, json=handler)

    def writes(self):
        return [c for c in self.calls if c.method != "GET"]


def run(coro):
    return asyncio.run(coro)


def minimal_body(schema, overrides=None):
    """The smallest body that fits an endpoint's request schema (every required key, typed), merged with
    `overrides`. Tests that aren't about the body use it so validation lets the call through."""
    def example(s):
        if "enum" in s:
            return s["enum"][0]
        if "const" in s:
            return s["const"]
        for key in ("oneOf", "anyOf", "allOf"):
            if key in s:
                merged = {k: v for k, v in s.items() if k != key}  # the outer required keys stay required
                for branch in (s[key] if key == "allOf" else s[key][:1]):
                    merged = {**merged, **branch, "required": merged.get("required", []) + branch.get("required", []),
                              "properties": {**merged.get("properties", {}), **branch.get("properties", {})}}
                return example(merged)
        t = s.get("type")
        if isinstance(t, list):
            t = t[0]
        if t == "object" or "properties" in s or "required" in s:
            return {k: example(s.get("properties", {}).get(k, {})) for k in s.get("required", [])}
        if t == "array":
            return [example(s.get("items", {}))] if s.get("minItems") else []
        return {"string": "x", "integer": 1, "number": 1, "boolean": True}.get(t, "x")

    rb = (schema or {}).get("request_body") or {}
    body = example(rb.get("schema", rb)) if rb else {}
    return {**(body if isinstance(body, dict) else {}), **(overrides or {})}


async def call(c: Client, tool: str, name: str | None = None, **args):
    """Calls a Hevo tool the way an agent does. There is no default region, so every call names one: `us`
    unless the test passes `region`; `region=None` leaves it out on purpose."""
    if name is not None:
        args["name"] = name
    if tool not in ("list_endpoints", "get_schema"):
        args.setdefault("region", "us")
    if args.get("region", "us") is None:
        del args["region"]
    result = await c.call_tool(tool, args, raise_on_error=False)
    if result.is_error and result.structured_content is None:
        # A plain tool error (no shaped body): raise it, as a client would show it.
        raise ToolError(result.content[0].text)
    if result.structured_content and "error" in result.structured_content:
        assert result.is_error, "shaped errors must be flagged isError"
    return result.structured_content
