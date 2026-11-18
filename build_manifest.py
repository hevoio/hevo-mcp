"""Build step: turns Hevo's OpenAPI specs into the manifest the server serves.

    openapi/*.json + endpoint_overrides.json
                            │
                            ▼
                     build_manifest.py
                            │
                            ▼
    src/hevo_mcp_server/manifest/
      ├── endpoints.json                     {tools, endpoints}: what the server loads at start
      ├── <product>/<resource>/<name>.json   one resolved schema per endpoint, read by get_schema
      └── AVAILABLE_ACTIONS.md               human reference

Nothing about an endpoint is written by hand: the name, resource, action and schema come from the
spec. endpoint_overrides.json only adds what a spec can't say (consequences, a better name, a safety
flag). Run it after any spec change:

    python build_manifest.py            # rebuild from openapi/
    python build_manifest.py --fetch    # first pull the latest specs from Hevo's docs sites

The script prints what changed against the previous manifest, so spec drift is visible in review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
OPENAPI_DIR = ROOT / "openapi"
OVERRIDES_FILE = ROOT / "endpoint_overrides.json"
MANIFEST_DIR = ROOT / "src" / "hevo_mcp_server" / "manifest"

PRODUCTS: dict[str, dict[str, str]] = {
    # Edge Pipelines own the plain names (list_pipelines, pipelines_read). Standard pipelines share most of
    # those names in their own API, so their endpoints and tools carry `name_prefix` (standard_list_pipelines,
    # standard_pipelines_read). The team's users and OAuth accounts are shared by both products and live in
    # the Public API: they are split out as their own product (`include`), and left out of Standard
    # (`exclude`), so each endpoint ships exactly once.
    "edge": {
        "spec": "edge.json",
        "prefix": "/api/v2",
        "docs": "https://edge-api-docs.hevodata.com",
        "label": "Edge Pipelines (Edge API v2)",
    },
    "standard": {
        "spec": "standard.json",
        "prefix": "/api/public/v2.0",
        "exclude": "/api/public/v2.0/accounts/",
        "name_prefix": "standard_",
        "docs": "https://api-docs.hevodata.com",
        "label": "Standard Pipelines (Public API v2.0)",
    },
    "account": {
        "spec": "account.json",
        "prefix": "/api/public/v2.0",
        "include": "/api/public/v2.0/accounts/",
        "docs": "https://api-docs.hevodata.com",
        "label": "Team users and OAuth accounts (Public API v2.0)",
    },
}
METHODS = ("get", "post", "put", "patch", "delete")
# Specs come only from Hevo's public API docs sites, never from an internal or test environment.
SPEC_HOST_SUFFIX = ".hevodata.com"
_DROP_KEYS = {"example", "examples", "xml", "externalDocs", "x-readme"}
OVERRIDE_KEYS = {"name", "title", "action", "destructive", "consequence", "description", "exclude", "approval", "records",
                 "body_required"}
# Whether the server asks the user to approve each call, through the MCP client (approval.py):
# always; never; or unless_activating (no approval when the body sets state ACTIVE, approval otherwise).
APPROVALS = ("always", "never", "unless_activating")


# --- OpenAPI helpers -------------------------------------------------------------------------------

def resolve(node: Any, spec: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """Inline local $refs; a cyclic ref becomes a plain object. Drops examples and vendor keys."""
    if isinstance(node, list):
        return [resolve(x, spec, seen) for x in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {"type": "object"}
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target[part]
        merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        return resolve(merged, spec, (*seen, ref))
    return {k: resolve(v, spec, seen) for k, v in node.items() if k not in _DROP_KEYS}


def restrict(spec: dict[str, Any], include: str | None, exclude: str | None = None) -> dict[str, Any]:
    """The spec with only the paths under `include` (all of them when None) and not under `exclude`, and only
    the components those paths reference, so a product that takes part of an API ships nothing it doesn't
    serve."""
    if include is None and exclude is None:
        return spec
    paths = {p: item for p, item in spec["paths"].items()
             if (include is None or p.startswith(include)) and not (exclude and p.startswith(exclude))}
    needed: set[str] = set()

    def refs(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/") and ref not in needed:
                needed.add(ref)
                target: Any = spec
                for part in ref[2:].split("/"):
                    target = target[part]
                refs(target)
            for v in node.values():
                refs(v)
        elif isinstance(node, list):
            for v in node:
                refs(v)

    refs(paths)
    components: dict[str, dict[str, Any]] = {}
    for ref in sorted(needed):
        _, _, section, key = ref.split("/", 3)
        components.setdefault(section, {})[key] = spec["components"][section][key]
    for section in ("securitySchemes",):
        if section in spec.get("components", {}):
            components[section] = spec["components"][section]
    out = {k: v for k, v in spec.items() if k not in ("paths", "components", "tags", "x-readme")}
    out["paths"] = dict(sorted(paths.items()))
    out["components"] = {s: dict(sorted(e.items())) for s, e in sorted(components.items())}
    used = {tag for item in paths.values() for op in item.values() if isinstance(op, dict) for tag in op.get("tags") or []}
    if tags := [tag for tag in spec.get("tags") or [] if tag.get("name") in used]:
        out["tags"] = tags
    return out


def snake(name: str) -> str:
    """getAllOAuthAccounts -> get_all_oauth_accounts; updateUserRolesForRBAC -> update_user_roles_for_rbac."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().replace("-", "_")


def resource_of(path: str, prefix: str) -> str:
    """First path segment after the API prefix: /api/v2/pipelines/{id}/jobs -> pipelines."""
    rel = path.removeprefix(prefix)
    parts = [p for p in rel.split("/") if p and not p.startswith("{")]
    return parts[0] if parts else "other"


def classify_action(method: str) -> str:
    """read (GET) / delete (DELETE) / write (everything else). Overrides can change it per endpoint."""
    if method == "GET":
        return "read"
    if method == "DELETE":
        return "delete"
    return "write"


def _returns_list(operation: dict[str, Any]) -> bool:
    for code, resp in operation.get("responses", {}).items():
        if not str(code).startswith("2"):
            continue
        for media in (resp.get("content") or {}).values():
            schema = media.get("schema") or {}
            data = (schema.get("properties") or {}).get("data") or {}
            if schema.get("type") == "array" or data.get("type") == "array":
                return True
    return False


def derive_name(product: str, method: str, path: str, prefix: str, operation: dict[str, Any]) -> str:
    """Endpoint name from the operationId, or from the method and path when the spec has none.

    With an operationId: `getAllPipelines` -> list_pipelines, `createPipeline` -> create_pipeline.
    Without one (the Edge spec): GET /pipelines/{id}/jobs -> list_pipeline_jobs,
    POST /pipelines/{id}/actions/enable -> enable_pipeline, PATCH /pipelines/{id} -> update_pipeline.
    """
    op_id = operation.get("operationId")
    if op_id:
        base = snake(op_id)
        if base.startswith("get_all_"):
            base = "list_" + base[len("get_all_"):]
        return base

    rel = path.removeprefix(prefix)
    segs = [s for s in rel.split("/") if s]
    words: list[str] = []
    action_word = None
    for i, seg in enumerate(segs):
        if seg.startswith("{"):
            continue
        if seg == "actions" and i + 1 < len(segs):
            action_word = segs[i + 1]
            break
        followed_by_param = i + 1 < len(segs) and segs[i + 1].startswith("{")
        words.append(seg[:-1] if followed_by_param and seg.endswith("s") else seg)
    item = bool(segs) and segs[-1].startswith("{")
    if method == "GET":
        verb = "get" if item or not _returns_list(operation) else "list"
    elif method == "POST":
        verb = action_word or "create"
        if not action_word and words and words[-1].endswith("s"):
            words[-1] = words[-1][:-1]  # POST /pipelines creates one pipeline
    elif method == "DELETE":
        verb = "delete"
    else:
        verb = "update"
    return f"{verb}_{'_'.join(words)}".replace("-", "_")


def docs_url(product: str, method: str, path: str, operation: dict[str, Any]) -> str:
    """The endpoint's page on Hevo's docs site (ReadMe slugs: operationId, or method_path for Edge)."""
    base = PRODUCTS[product]["docs"] + "/reference/"
    op_id = operation.get("operationId")
    if op_id:
        return base + op_id.lower()
    slug = path.strip("/").replace("{", "").replace("}", "").replace("_", "-").replace("/", "-")
    return f"{base}{method.lower()}_{slug}"


def _alias_posts(paths: dict[str, Any]) -> set[tuple[str, str]]:
    """Edge documents POST twins of its PATCH endpoints ("Modify ... (POST)"); only PATCH is exposed."""
    out = set()
    for path, item in paths.items():
        post, patch = item.get("post"), item.get("patch")
        if post and patch and str(post.get("summary", "")).rstrip().endswith("(POST)"):
            out.add(("POST", path))
    return out


# --- Overrides ---------------------------------------------------------------------------------------

def load_overrides(path: Path = OVERRIDES_FILE) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if key.startswith("_"):
            continue
        if not isinstance(value, dict) or (unknown := set(value) - OVERRIDE_KEYS):
            raise SystemExit(f"endpoint_overrides.json: bad entry {key!r}"
                             + (f": unknown keys {sorted(unknown)}" if isinstance(value, dict) else ""))
        if "action" in value and value["action"] not in ("read", "write", "delete"):
            raise SystemExit(f"endpoint_overrides.json: {key!r}: action must be read, write or delete")
        if "approval" in value and value["approval"] not in APPROVALS:
            raise SystemExit(f"endpoint_overrides.json: {key!r}: approval must be one of {', '.join(APPROVALS)}")
        if "records" in value and not isinstance(value["records"], bool):
            raise SystemExit(f"endpoint_overrides.json: {key!r}: records must be true or false")
        if "body_required" in value and not (isinstance(value["body_required"], list) and value["body_required"]
                                             and all(isinstance(k, str) for k in value["body_required"])):
            raise SystemExit(f"endpoint_overrides.json: {key!r}: body_required must be a list of body key names")
        out[key] = value
    return out


# --- Build ------------------------------------------------------------------------------------------

def build_endpoints(product: str, spec: dict[str, Any], overrides: dict[str, dict[str, Any]],
                    used_overrides: set[str]) -> list[dict[str, Any]]:
    cfg = PRODUCTS[product]
    spec = restrict(spec, cfg.get("include"), cfg.get("exclude"))
    aliases = _alias_posts(spec["paths"])
    rows = []
    unclassified: list[str] = []
    for path, item in spec["paths"].items():
        shared_params = item.get("parameters", [])
        for method_l in METHODS:
            if method_l not in item:
                continue
            method = method_l.upper()
            key = f"{product} {method} {path}"
            ov = overrides.get(key, {})
            if key in overrides:
                used_overrides.add(key)
            if (method, path) in aliases:
                continue
            # Every endpoint is classified by hand before it ships: a spec sync can add endpoints, and one that
            # returns pipeline records must not reach the server (data residency; see the README).
            if "records" not in ov:
                unclassified.append(key)
            elif ov["records"] and not ov.get("exclude"):
                raise SystemExit(f"endpoint_overrides.json: {key!r} returns pipeline records (records: true), so it "
                                 "needs exclude: true; re-adding such an endpoint needs a data-residency review first")
            if ov.get("exclude"):
                continue
            raw = item[method_l]
            op = resolve(deepcopy(raw), spec)
            op["parameters"] = resolve(deepcopy(shared_params), spec) + op.get("parameters", [])
            action = ov.get("action") or classify_action(method)
            destructive = bool(ov.get("destructive")) or (action == "delete")
            approval = approval_of(key, action, ov.get("approval"))
            description = "\n\n".join(x for x in (ov.get("description"), raw.get("description")) if x)
            body = (op.get("requestBody") or {}).get("content", {}).get("application/json", {}).get("schema")
            if ov.get("body_required"):
                # Hevo requires some top-level body keys that its spec leaves optional (a missing one returns an
                # empty 500). Marking them here keeps that through every spec sync; the spec itself stays as fetched.
                if not isinstance(body, dict) or not all(k in (body.get("properties") or {}) for k in ov["body_required"]):
                    raise SystemExit(f"endpoint_overrides.json: {key!r}: body_required names a key the request body "
                                     "doesn't have")
                body["required"] = sorted(set(body.get("required") or []) | set(ov["body_required"]))
            response = None
            for code in sorted(c for c in op.get("responses", {}) if str(c).startswith("2")):
                content = op["responses"][code].get("content") or {}
                response = (content.get("application/json") or next(iter(content.values()), {})).get("schema")
                if response:
                    break
            query_names = {p["name"] for p in op["parameters"] if p.get("in") == "query"}
            rows.append({
                "name": ov.get("name") or cfg.get("name_prefix", "") + derive_name(product, method, path, cfg["prefix"], op),
                "product": product,
                "resource": resource_of(path, cfg["prefix"]),
                "action": action,
                "method": method,
                "path": path,
                "summary": raw.get("summary") or "",
                "description": description,
                "destructive": destructive,
                "consequence": ov.get("consequence") or "",
                "title": ov.get("title") or "",
                "approval": approval,
                "paginated": method == "GET" and bool(query_names & {"starting_after", "cursor"}),
                "deprecated": bool(raw.get("deprecated")),
                "docs": docs_url(product, method, path, raw),
                "parameters": op["parameters"],
                "request_body": {"schema": body, "required": bool((op.get("requestBody") or {}).get("required"))}
                if op.get("requestBody") else None,
                "response": response,
            })
    if unclassified:
        raise SystemExit("New endpoint(s) in the specs need a review before they ship. Add each to "
                         "endpoint_overrides.json with records: false (it returns configuration or metadata only) or "
                         "records: true plus exclude: true (it returns pipeline records, which must stay in the "
                         "customer's region):\n  " + "\n  ".join(unclassified))
    return rows


def approval_of(key: str, action: str, override: str | None) -> str:
    """Writes need the user's approval unless an override says otherwise, so a new write endpoint from a spec
    is asked about until someone decides it needn't be. Deletes are always approved; reads never are."""
    if action == "delete":
        if override not in (None, "always"):
            raise SystemExit(f"endpoint_overrides.json: {key!r}: a delete always needs approval")
        return "always"
    return override or ("never" if action == "read" else "always")


def _summary_with_prefix(ep: dict[str, Any]) -> str:
    if ep["destructive"]:
        return f"DESTRUCTIVE - {ep['summary']}"
    if ep["action"] == "write":
        return f"WRITE - {ep['summary']}"
    if ep["paginated"]:
        return f"PAGINATED - {ep['summary']}"
    return ep["summary"]


def tool_mode(ep: dict[str, Any]) -> str:
    """Which tool serves an endpoint: its action, except that destructive writes (resync, restart, cancel,
    moving a position) get their own `destructive` tool. A plain write tool then carries no destructive
    endpoint, its destructiveHint is honest, and a client rule that always allows `*_write` doesn't cover
    them. The scope is still the action's (`write`)."""
    return "destructive" if ep["destructive"] and ep["action"] == "write" else ep["action"]


def tools_index(endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One tool per (product, resource, mode) with at least one live endpoint. Edge names carry no product
    prefix; Standard's carry `standard_` (PRODUCTS[...]["name_prefix"]), because both have pipelines."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for ep in endpoints:
        if ep["deprecated"]:
            continue
        groups.setdefault((ep["product"], ep["resource"], tool_mode(ep)), []).append(ep["name"])
    return [
        {"name": f"{PRODUCTS[product].get('name_prefix', '')}{resource}_{mode}".replace("-", "_"), "product": product, "resource": resource,
         "action": "write" if mode == "destructive" else mode, "mode": mode,
         "destructive": any(e["destructive"] for e in endpoints if e["name"] in names),
         "endpoints": sorted(names)}
        for (product, resource, mode), names in sorted(groups.items())
    ]


def write_available_actions(tools: list[dict[str, Any]], by_name: dict[str, dict[str, Any]], out: Path) -> None:
    intro = ("Generated by `build_manifest.py` from the OpenAPI specs; do not edit. "
             "Call `list_endpoints` to browse at runtime and `get_schema` for an endpoint's full schema.")
    lines = ["# Available actions", "", intro, ""]
    for product, cfg in PRODUCTS.items():
        lines += [f"## {cfg['label']}", ""]
        for tool in (t for t in tools if t["product"] == product):
            lines += [f"### `{tool['name']}`", "", "| Endpoint | Method and path | Summary |", "| --- | --- | --- |"]
            for name in tool["endpoints"]:
                ep = by_name[name]
                lines.append(f"| `{name}` | `{ep['method']} {ep['path']}` | {_summary_with_prefix(ep)} |")
            lines.append("")
    (out / "AVAILABLE_ACTIONS.md").write_text("\n".join(lines))


# --- Screening what reaches the agent ----------------------------------------------------------------------
# Spec text becomes tool descriptions and schemas the agent reads, so a changed spec could carry hidden text or
# instructions aimed at the model. The build fails on any of these; a reviewer then decides (and, for a link,
# can add its host below).
HIDDEN_TEXT = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                         "\u2066-\u2069\ufeff\U000e0000-\U000e007f]|\x1b")
INSTRUCTION_LIKE = re.compile(
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)|disregard\s+(?:all|any|the|previous)"
    r"|system\s+prompt|you\s+are\s+now|new\s+instructions|do\s+not\s+(?:tell|reveal|inform|mention)"
    r"|<\|im_|^\s*(?:assistant|system)\s*:|</?(?:script|iframe)\b", re.IGNORECASE | re.MULTILINE)
LINK = re.compile(r"\b(?:https?|ftp|file|data|javascript):[^\s)\"'<>]*", re.IGNORECASE)
LINK_HOSTS = ("hevodata.com", "wikipedia.org", "json-schema.org", "snowflakecomputing.com")


def screen_text(endpoints: list[dict[str, Any]]) -> list[str]:
    """Problems in any string of the built endpoints: hidden characters, instruction-like phrases, or links
    to hosts outside LINK_HOSTS (and any non-https link)."""
    problems: list[str] = []

    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(k, f"{where} key")
                walk(v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]")
        elif isinstance(value, str):
            if HIDDEN_TEXT.search(value):
                problems.append(f"{where}: hidden or control characters")
            if match := INSTRUCTION_LIKE.search(value):
                problems.append(f"{where}: instruction-like text {match.group(0)!r}")
            for link in LINK.findall(value):
                host = link.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
                if not link.lower().startswith("https://") or not any(
                        host == h or host.endswith("." + h) for h in LINK_HOSTS):
                    problems.append(f"{where}: link {link[:80]!r} (allowed hosts: LINK_HOSTS in build_manifest.py)")

    for ep in endpoints:
        walk(ep, ep["name"])
    return problems


def build(openapi_dir: Path = OPENAPI_DIR, overrides_file: Path = OVERRIDES_FILE, out: Path = MANIFEST_DIR,
          *, quiet: bool = False) -> dict[str, Any]:
    overrides = load_overrides(overrides_file)
    used: set[str] = set()
    endpoints: list[dict[str, Any]] = []
    spec_sha: dict[str, str] = {}
    for product, cfg in PRODUCTS.items():
        raw = (openapi_dir / cfg["spec"]).read_bytes()
        spec_sha[product] = hashlib.sha256(raw).hexdigest()[:16]
        endpoints += build_endpoints(product, json.loads(raw), overrides, used)
    unused = sorted(set(overrides) - used)
    if unused:
        raise SystemExit("endpoint_overrides.json has entries for endpoints not in the specs:\n  " + "\n  ".join(unused))
    names = [e["name"] for e in endpoints]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise SystemExit(f"Endpoint names collide; add a `name` override: {dupes}")
    endpoints.sort(key=lambda e: (e["product"], e["resource"], e["name"]))
    if problems := screen_text(endpoints):
        raise SystemExit("Spec text the agent would read needs a review:\n  " + "\n  ".join(problems[:50]))

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "__init__.py").write_text("")
    rows = []
    for ep in endpoints:
        rel = Path(ep["product"]) / ep["resource"].replace("-", "_") / f"{ep['name']}.json"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(json.dumps(ep, indent=2, ensure_ascii=False) + "\n")
        rows.append({k: ep[k] for k in ("name", "product", "resource", "action", "method", "path", "destructive",
                                        "consequence", "title", "approval", "paginated", "deprecated", "docs")}
                    | {"summary": _summary_with_prefix(ep), "schema_file": rel.as_posix()})
    tools = tools_index(endpoints)
    # No timestamp: the build is deterministic, so an unchanged spec gives an unchanged file.
    manifest = {"meta": {"spec_sha256": spec_sha, "endpoints": len(rows), "tools": len(tools)},
                "tools": tools, "endpoints": rows}
    (out / "endpoints.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    write_available_actions(tools, {e["name"]: e for e in endpoints}, out)
    if not quiet:
        print(f"Wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}: "
              f"{len(rows)} endpoints, {len(tools)} tools")
    return manifest


# --- Diff against the previous build --------------------------------------------------------------------

def snapshot(out: Path) -> tuple[set[str], dict[str, str]]:
    manifest = out / "endpoints.json"
    if not manifest.exists():
        return set(), {}
    doc = json.loads(manifest.read_text())
    hashes = {}
    for ep in doc.get("endpoints", []):
        f = out / ep["schema_file"]
        if f.exists():
            hashes[ep["name"]] = hashlib.sha1(f.read_bytes()).hexdigest()
    return {t["name"] for t in doc.get("tools", [])}, hashes


def print_diff(old: tuple[set[str], dict[str, str]], new: tuple[set[str], dict[str, str]]) -> None:
    old_tools, old_eps = old
    new_tools, new_eps = new
    if not old_tools and not old_eps:
        print("No previous manifest to diff against.")
        return
    changes = {
        "+ tool": sorted(new_tools - old_tools), "- tool": sorted(old_tools - new_tools),
        "+ endpoint": sorted(new_eps.keys() - old_eps.keys()), "- endpoint": sorted(old_eps.keys() - new_eps.keys()),
        "~ endpoint": sorted(n for n in old_eps.keys() & new_eps.keys() if old_eps[n] != new_eps[n]),
    }
    if not any(changes.values()):
        print("Changes vs. previous build: none")
        return
    print("Changes vs. previous build:")
    for label, names in changes.items():
        for n in names:
            print(f"  {label} {n}")


# --- Fetching specs from Hevo's docs sites ------------------------------------------------------------------

def fetch_specs(openapi_dir: Path = OPENAPI_DIR) -> None:
    """Rebuilds each spec from the per-endpoint OpenAPI snippets embedded in Hevo's docs pages.

    Each ReadMe reference page (listed in /llms.txt) carries a ```json block with that endpoint's
    OpenAPI definition; merging them gives the full, current spec.
    """
    import httpx2 as httpx

    def download(docs: str) -> tuple[list[str], list[str]]:
        """Every reference page of one docs site: its URLs and their text."""
        with httpx.Client(timeout=30.0, follow_redirects=True) as http:
            index = http.get(f"{docs}/llms.txt").raise_for_status().text
            pages = sorted(set(re.findall(re.escape(docs) + r"/reference/[A-Za-z0-9_-]+\.md", index)))

            def page(url: str) -> str:
                # The docs host sometimes resets a connection under parallel load: retry a few times.
                for attempt in range(4):
                    try:
                        return http.get(url).raise_for_status().text
                    except httpx.TransportError:
                        if attempt == 3:
                            raise
                        time.sleep(1 + attempt * 2)
                raise AssertionError("unreachable")

            with ThreadPoolExecutor(4) as pool:
                return pages, list(pool.map(page, pages))

    # Two products can share one docs site (Standard and the account endpoints both come from the Public
    # API's docs): each site is downloaded once and split by product afterwards.
    sites: dict[str, tuple[list[str], list[str]]] = {}
    for product, cfg in PRODUCTS.items():
        if not (cfg["docs"].startswith("https://") and urlsplit(cfg["docs"]).hostname.endswith(SPEC_HOST_SUFFIX)):
            raise SystemExit(f"{product}: specs come only from Hevo's public API docs, not {cfg['docs']}")
        if cfg["docs"] not in sites:
            sites[cfg["docs"]] = download(cfg["docs"])
        pages, texts = sites[cfg["docs"]]
        merged: dict[str, Any] | None = None
        found = 0
        for text in texts:
            m = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
            if not m:
                continue
            try:
                doc = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(doc, dict) or "paths" not in doc:
                continue
            found += 1
            if merged is None:
                merged = {k: v for k, v in doc.items() if k not in ("paths", "components", "tags")}
                merged["paths"], merged["components"], merged["tags"] = {}, {}, []
            for path, item in doc["paths"].items():
                merged["paths"].setdefault(path, {}).update(item)
            # Each page lists only its own tag; keep every tag the API uses, once, in a stable order.
            for tag in doc.get("tags") or []:
                if tag not in merged["tags"]:
                    merged["tags"].append(tag)
            for section, entries in (doc.get("components") or {}).items():
                target = merged["components"].setdefault(section, {})
                for key, value in entries.items():
                    if key in target and target[key] != value:
                        print(f"  warning: components.{section}.{key} differs between pages; keeping the first",
                              file=sys.stderr)
                        continue
                    target[key] = value
        if merged is None:
            raise SystemExit(f"{product}: no OpenAPI definitions found in {len(pages)} docs pages")
        merged["paths"] = dict(sorted(merged["paths"].items()))
        merged["tags"] = sorted(merged["tags"], key=lambda tag: str(tag.get("name", "")))
        merged["components"] = {s: dict(sorted(e.items())) for s, e in sorted(merged["components"].items())}
        merged = restrict(merged, cfg.get("include"), cfg.get("exclude"))
        (openapi_dir / cfg["spec"]).write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        ops = sum(1 for item in merged["paths"].values() for m in item if m in METHODS)
        print(f"{product}: {found} endpoint pages -> openapi/{cfg['spec']} ({len(merged['paths'])} paths, {ops} operations)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fetch", action="store_true", help="pull the latest specs from Hevo's docs into openapi/ first")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed manifest differs from what the specs produce (for CI)")
    args = parser.parse_args(argv)
    if args.fetch:
        fetch_specs()
    if args.check:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build(out=Path(tmp), quiet=True)
            fresh = snapshot(Path(tmp))
        current = snapshot(MANIFEST_DIR)
        if fresh != current:
            print("Manifest is stale: run `python build_manifest.py` and commit the result.")
            print_diff(current, fresh)
            return 1
        print("Manifest is up to date.")
        return 0
    before = snapshot(MANIFEST_DIR)
    build()
    print_diff(before, snapshot(MANIFEST_DIR))
    return 0


if __name__ == "__main__":
    sys.exit(main())
