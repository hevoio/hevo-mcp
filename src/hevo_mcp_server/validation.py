"""Checks a request body against the endpoint's schema before the user is asked to approve it.

The group tools take `body` untyped, so without this the first thing to see a wrong body is Hevo, after the
user has already approved the request. The manifest ships each endpoint's request-body schema from the
OpenAPI spec (plain JSON Schema: no $ref, nullable or format left in it), so a body is checked here first:
missing required keys, wrong types, values outside an enum, keys a closed object doesn't allow. The agent
gets INVALID_BODY with the problems, and the approval prompt only ever shows a request Hevo would accept
in shape. What a value means (an id that exists, a valid password) is still Hevo's to check.
"""

from functools import cache
from typing import Any

from jsonschema import Draft202012Validator

from hevo_mcp_server.catalog import load_schema

MAX_PROBLEMS = 8


def _relax_ambiguous_oneof(node: Any) -> Any:
    """Replace ``oneOf`` with ``anyOf`` where every branch is an open object.

    Hevo's Standard-API OpenAPI spec uses ``oneOf`` for config fields whose branches are all
    ``type: object`` without ``additionalProperties: false`` and without ``required`` that would
    discriminate.  Any config object therefore matches every branch, and a strict ``oneOf`` validator
    rejects the body before it reaches Hevo.  ``anyOf`` ("at least one must match") is what the spec
    author meant: Hevo picks the right branch from a sibling ``type`` field, not from JSON Schema.

    The Edge API's ``oneOf`` (branches that discriminate by ``required``) is unaffected: those
    branches have no ``properties``, so the heuristic does not match them.
    """
    if isinstance(node, list):
        return [_relax_ambiguous_oneof(x) for x in node]
    if not isinstance(node, dict):
        return node
    branches = node.get("oneOf")
    if isinstance(branches, list) and len(branches) >= 2 and all(
        isinstance(b, dict) and b.get("type") == "object" and "properties" in b
        and "additionalProperties" not in b
        for b in branches
    ):
        node = {("anyOf" if k == "oneOf" else k): _relax_ambiguous_oneof(v) for k, v in node.items()}
    else:
        node = {k: _relax_ambiguous_oneof(v) for k, v in node.items()}
    return node


@cache
def _validator(name: str) -> tuple[bool, Draft202012Validator | None]:
    """(body required, validator) for an endpoint; (False, None) when it takes no body."""
    rb = load_schema(name).get("request_body")
    if not isinstance(rb, dict):
        return False, None
    schema = rb.get("schema") if "schema" in rb else {k: v for k, v in rb.items() if k != "required"}
    if not isinstance(schema, dict) or not schema:
        return bool(rb.get("required")), None
    return bool(rb.get("required")), Draft202012Validator(_relax_ambiguous_oneof(schema))


def body_problems(name: str, body: Any) -> list[str]:
    """What is wrong with `body` for endpoint `name`; empty when it fits the schema (or there is no schema)."""
    required, validator = _validator(name)
    if body is None:
        return ["body is required for this endpoint (see get_schema)"] if required else []
    if validator is None:
        return []
    problems = []
    for err in sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "body"
        problems.append(f"{where}: {_short_message(err)}")
        if len(problems) >= MAX_PROBLEMS:
            problems.append("...")
            break
    return problems + _unknown_top_level_keys(name, body)


MAX_PROBLEM_CHARS = 240


def _short_message(err: Any) -> str:
    """One sentence per problem. jsonschema's text for anyOf/oneOf repeats every branch schema, which is
    kilobytes the agent can't use; the branch titles say what shapes exist, and get_schema has the rest."""
    if err.validator in ("anyOf", "oneOf"):
        titles = [b.get("title") for b in err.validator_value if isinstance(b, dict) and b.get("title")]
        shapes = ", ".join(titles) if titles else f"{len(err.validator_value)} shapes"
        verb = "fits none of" if err.validator == "anyOf" or "not valid under any" in err.message else "fits more than one of"
        return f"{verb} the shapes {shapes} (get_schema shows each)"
    text = err.message
    if len(text) > MAX_PROBLEM_CHARS:
        text = text[:MAX_PROBLEM_CHARS] + "... (get_schema shows the full schema)"
    return text


def _unknown_top_level_keys(name: str, body: Any) -> list[str]:
    """Top-level body keys the schema doesn't name. Hevo answers those with "payload is malformed" and no
    detail, so naming the allowed keys here saves a round trip. Nested objects (per-type configs) vary, so
    only the top level is checked, and only when the schema names keys and doesn't open the object."""
    _, validator = _validator(name)
    schema = validator.schema if validator is not None else {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(body, dict) or not isinstance(props, dict) or not props or schema.get("additionalProperties"):
        return []
    unknown = [k for k in body if k not in props]
    return [f"body: unknown key {k!r} (allowed: {', '.join(props)})" for k in unknown]


def takes_body(name: str) -> bool:
    """Whether the endpoint's schema has a request body at all (none of the GET or DELETE endpoints do)."""
    return isinstance(load_schema(name).get("request_body"), dict)


@cache
def _query_keys(name: str) -> frozenset[str] | None:
    """The query parameters the endpoint's schema names; None when it names none (nothing to check)."""
    params = load_schema(name).get("parameters")
    if not isinstance(params, list):
        return None
    keys = frozenset(p["name"] for p in params if isinstance(p, dict) and p.get("in") == "query" and "name" in p)
    return keys or None


def query_problems(name: str, query: dict[str, Any]) -> list[str]:
    """Query keys the endpoint doesn't know. Hevo ignores them silently, so a misspelt filter would return
    everything as if it had filtered."""
    known = _query_keys(name)
    unknown = [k for k in query if known is not None and k not in known]
    if not unknown and known is None and query:
        return [f"{name} takes no query parameters (given: {', '.join(query)})"]
    return [f"unknown query parameter {k!r} (allowed: {', '.join(sorted(known))})" for k in unknown]
