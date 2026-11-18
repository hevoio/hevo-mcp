"""The endpoints and tools the server serves, read from the manifest that build_manifest.py generates.

Edge pipelines use the Edge API v2 (`/api/v2/`); Standard pipelines and the team's users and OAuth accounts
use the Public API v2.0 (`/api/public/v2.0/`). Nothing here is written by hand: to add or
change an endpoint, update the spec in openapi/ and rerun the build.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any

PRODUCT_LABELS = {
    "edge": "Edge Pipelines (Edge API v2)",
    "standard": "Standard Pipelines (Public API v2.0)",
    "account": "Team users and OAuth accounts (Public API v2.0)",
}
# Edge owns the plain names; Standard shares most of them in its own API, so its tools, endpoints and
# categories carry a prefix (standard_pipelines_read, standard_list_pipelines). Must match build_manifest.py.
PRODUCT_PREFIX = {"standard": "standard_"}


@dataclass(frozen=True)
class Endpoint:
    name: str
    product: str
    resource: str
    action: str  # read, write or delete: which tool group serves it
    method: str
    path: str
    summary: str
    destructive: bool  # loses or re-ingests data
    consequence: str
    approval: str  # always, never or unless_activating: whether the user must approve each call (approval.py)
    paginated: bool
    deprecated: bool
    docs: str
    schema_file: str
    title: str = ""  # the change as the approval prompt names it ("Delete Edge Destination"); "" for reads

    @property
    def mode(self) -> str:
        """The tool that serves this endpoint: read, write, destructive (a write that loses or re-ingests
        data) or delete. The scope needed is still `action`."""
        return "destructive" if self.destructive and self.action == "write" else self.action

    @property
    def tool(self) -> str:
        return f"{PRODUCT_PREFIX.get(self.product, '')}{self.resource}_{self.mode}".replace("-", "_")

    @property
    def category(self) -> str:
        return f"{PRODUCT_PREFIX.get(self.product, '')}{self.resource}".replace("-", "_")

    @property
    def is_write(self) -> bool:
        return self.action != "read"

    def needs_approval(self, body: Any) -> bool:
        """Whether this call must be approved by the user before it reaches Hevo."""
        if self.approval == "unless_activating":
            return not (isinstance(body, dict) and body.get("state") == "ACTIVE")
        return self.approval != "never"


@dataclass(frozen=True)
class ToolGroup:
    name: str
    product: str
    resource: str
    action: str  # the scope its endpoints need: read, write or delete
    mode: str  # read, write, destructive or delete: the kind of endpoint it holds (the name's last part)
    destructive: bool  # at least one endpoint is destructive
    endpoints: tuple[str, ...]

    @property
    def category(self) -> str:
        return f"{PRODUCT_PREFIX.get(self.product, '')}{self.resource}".replace("-", "_")

    @property
    def is_write(self) -> bool:
        return self.action != "read"


def _files():
    return resources.files("hevo_mcp_server.manifest")


_MANIFEST: dict[str, Any] = json.loads(_files().joinpath("endpoints.json").read_text())
# Which specs this catalog was built from: sha256 of each openapi/*.json (reported by /health-check).
MANIFEST_META: dict[str, Any] = _MANIFEST.get("meta", {})

ENDPOINTS: list[Endpoint] = [Endpoint(**row) for row in _MANIFEST["endpoints"]]
BY_NAME: dict[str, Endpoint] = {e.name: e for e in ENDPOINTS}
TOOLS: list[ToolGroup] = [ToolGroup(**{**t, "endpoints": tuple(t["endpoints"])}) for t in _MANIFEST["tools"]]
BY_TOOL: dict[str, ToolGroup] = {t.name: t for t in TOOLS}
CATEGORIES: dict[str, list[Endpoint]] = {}
for _e in ENDPOINTS:
    CATEGORIES.setdefault(_e.category, []).append(_e)


@cache
def load_schema(name: str) -> dict[str, Any]:
    """The endpoint's resolved OpenAPI definition: parameters, request body and response."""
    return json.loads(_files().joinpath(BY_NAME[name].schema_file).read_text())
