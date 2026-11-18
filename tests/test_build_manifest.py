import json
from pathlib import Path

import pytest

import build_manifest as bm

LIST_RESPONSE = {"responses": {"200": {"content": {"application/json": {"schema": {
    "properties": {"data": {"type": "array"}}}}}}}}


def test_names_come_from_operation_ids():
    assert bm.snake("getAllOAuthAccounts") == "get_all_oauth_accounts"
    assert bm.snake("updateUserRolesForRBAC") == "update_user_roles_for_rbac"
    assert bm.derive_name("edge", "GET", "/api/v2/pipelines", "/api/v2",
                          {"operationId": "getAllPipelines"}) == "list_pipelines"
    assert bm.derive_name("edge", "POST", "/api/v2/pipelines", "/api/v2",
                          {"operationId": "createPipeline"}) == "create_pipeline"


def test_names_come_from_method_and_path_without_operation_ids():
    p = "/api/v2"
    assert bm.derive_name("edge", "GET", "/api/v2/pipelines/{pipeline_id}/jobs", p, LIST_RESPONSE) == "list_pipeline_jobs"
    assert bm.derive_name("edge", "GET", "/api/v2/pipelines/{pipeline_id}", p, {}) == "get_pipeline"
    assert bm.derive_name("edge", "POST", "/api/v2/pipelines", p, {}) == "create_pipeline"
    assert bm.derive_name("edge", "POST", "/api/v2/pipelines/{pipeline_id}/actions/sync-now", p, {}) == "sync_now_pipeline"
    assert bm.derive_name("edge", "PATCH", "/api/v2/pipelines/{pipeline_id}/objects", p, {}) == "update_pipeline_objects"
    assert bm.derive_name("edge", "DELETE", "/api/v2/destinations/{destination_id}", p, {}) == "delete_destination"


def test_resources_actions_and_post_aliases():
    assert bm.resource_of("/api/v2/pipelines/{id}/jobs", "/api/v2") == "pipelines"
    assert bm.resource_of("/api/public/v2.0/accounts/users", "/api/public/v2.0") == "accounts"
    assert [bm.classify_action(m) for m in ("GET", "POST", "PUT", "PATCH", "DELETE")] == \
        ["read", "write", "write", "write", "delete"]
    paths = {"/api/v2/pipelines/{id}": {"patch": {"summary": "Modify (PATCH)"}, "post": {"summary": "Modify (POST)"}},
             "/api/v2/pipelines": {"post": {"summary": "Create a Pipeline"}}}
    assert bm._alias_posts(paths) == {("POST", "/api/v2/pipelines/{id}")}


def _spec(paths):
    return {"openapi": "3.0.1", "info": {"title": "t", "version": "1"}, "paths": paths,
            "components": {"parameters": {"pid": {"name": "pipeline_id", "in": "path", "required": True,
                                                   "schema": {"type": "integer"}}}}}


EDGE_PATHS = {
    "/api/v2/pipelines": {
        "get": {"summary": "List", "parameters": [{"name": "cursor", "in": "query", "schema": {"type": "string"}}],
                **LIST_RESPONSE},
        "post": {"summary": "Create", "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "properties": {"name": {"type": "string", "example": "x"}}}}}}},
    },
    "/api/v2/pipelines/{pipeline_id}": {
        "parameters": [{"$ref": "#/components/parameters/pid"}],
        "patch": {"summary": "Modify Pipeline (PATCH)"},
        "post": {"summary": "Modify Pipeline (POST)"},
        "delete": {"summary": "Delete", "deprecated": True},
    },
    "/api/v2/pipelines/{pipeline_id}/actions/resync": {
        "parameters": [{"$ref": "#/components/parameters/pid"}],
        "post": {"summary": "Resync", "description": "Re-ingests."},
    },
}


# Every endpoint ships classified: these are the review entries for EDGE_PATHS (the POST twin of PATCH is an alias).
EDGE_REVIEWED = {
    "edge GET /api/v2/pipelines": {"records": False},
    "edge POST /api/v2/pipelines": {"records": False},
    "edge PATCH /api/v2/pipelines/{pipeline_id}": {"records": False},
    "edge DELETE /api/v2/pipelines/{pipeline_id}": {"records": False},
    "edge POST /api/v2/pipelines/{pipeline_id}/actions/resync": {"records": False},
}


def _specs(tmp_path: Path, edge=EDGE_PATHS, overrides=None):
    openapi = tmp_path / "openapi"
    openapi.mkdir(exist_ok=True)
    (openapi / "edge.json").write_text(json.dumps(_spec(edge)))
    # The account product keeps only /api/public/v2.0/accounts/ paths; a Standard pipeline path in its spec
    # must be dropped by the build, so a spec sync can't bring Standard pipeline management back.
    (openapi / "account.json").write_text(json.dumps(_spec({"/api/public/v2.0/pipelines": {"get": {"operationId": "getAllPipelines", "summary": "x", "responses": {"200": {"description": "ok"}}}}})))
    # The Standard product drops the account paths, which the account product serves.
    (openapi / "standard.json").write_text(json.dumps(_spec({"/api/public/v2.0/accounts/users": {"get": {"operationId": "getAllUsersFromTeam", "summary": "x", "responses": {"200": {"description": "ok"}}}}})))
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(overrides if overrides is not None else EDGE_REVIEWED))
    return openapi, path


def test_build_groups_endpoints_into_tools(tmp_path: Path):
    openapi, overrides = _specs(tmp_path, overrides={
        "_comment": "ignored", **EDGE_REVIEWED,
        "edge POST /api/v2/pipelines/{pipeline_id}/actions/resync": {
            "records": False, "destructive": True, "consequence": "Drops tables.", "description": "Careful."},
    })
    out = tmp_path / "manifest"
    manifest = bm.build(openapi, overrides, out, quiet=True)

    names = {e["name"]: e for e in manifest["endpoints"]}
    # The POST twin of PATCH is dropped; the deprecated delete is kept but gets no tool.
    assert set(names) == {"list_pipelines", "create_pipeline", "update_pipeline",
                          "delete_pipeline", "resync_pipeline"}
    assert [t["name"] for t in manifest["tools"]] == [
        "pipelines_destructive", "pipelines_read", "pipelines_write"]
    destructive, write = manifest["tools"][0], manifest["tools"][2]
    assert write["destructive"] is False and set(write["endpoints"]) == {"create_pipeline", "update_pipeline"}
    assert destructive["destructive"] is True and destructive["endpoints"] == ["resync_pipeline"]
    assert destructive["action"] == "write" and destructive["mode"] == "destructive"  # scope stays write
    assert names["list_pipelines"]["paginated"] and names["list_pipelines"]["summary"] == "PAGINATED - List"
    assert names["resync_pipeline"]["summary"] == "DESTRUCTIVE - Resync"
    assert names["resync_pipeline"]["consequence"] == "Drops tables."

    schema = json.loads((out / names["resync_pipeline"]["schema_file"]).read_text())
    assert schema["description"] == "Careful.\n\nRe-ingests."
    assert schema["parameters"][0]["name"] == "pipeline_id"  # shared $ref parameter resolved
    create = json.loads((out / names["create_pipeline"]["schema_file"]).read_text())
    assert create["request_body"]["required"] is True
    assert "example" not in json.dumps(create["request_body"])
    assert (out / "AVAILABLE_ACTIONS.md").exists() and (out / "__init__.py").exists()


def test_overrides_must_match_the_specs(tmp_path: Path):
    openapi, overrides = _specs(tmp_path, overrides={**EDGE_REVIEWED, "edge GET /api/v2/nope": {"name": "x"}})
    with pytest.raises(SystemExit, match="not in the specs"):
        bm.build(openapi, overrides, tmp_path / "m", quiet=True)
    overrides.write_text(json.dumps({**EDGE_REVIEWED, "edge GET /api/v2/pipelines": {"colour": "red"}}))
    with pytest.raises(SystemExit, match="unknown keys"):
        bm.build(openapi, overrides, tmp_path / "m", quiet=True)


def test_body_required_marks_keys_the_spec_leaves_optional(tmp_path: Path):
    """Hevo requires some body keys its spec leaves optional; the override adds them on every build, so a spec
    sync keeps them and the spec file stays exactly as fetched."""
    create = "edge POST /api/v2/pipelines"
    openapi, overrides = _specs(tmp_path, overrides={**EDGE_REVIEWED, create: {"records": False, "body_required": ["name"]}})
    out = tmp_path / "m"
    manifest = bm.build(openapi, overrides, out, quiet=True)
    ep = next(e for e in manifest["endpoints"] if e["name"] == "create_pipeline")
    assert json.loads((out / ep["schema_file"]).read_text())["request_body"]["schema"]["required"] == ["name"]
    assert "required" not in json.loads((openapi / "edge.json").read_text())["paths"]["/api/v2/pipelines"]["post"][
        "requestBody"]["content"]["application/json"]["schema"]
    for bad in (["nope"], [], "name"):
        overrides.write_text(json.dumps({**EDGE_REVIEWED, create: {"records": False, "body_required": bad}}))
        with pytest.raises(SystemExit, match="body_required"):
            bm.build(openapi, overrides, tmp_path / "m2", quiet=True)


def test_no_endpoint_ships_unclassified(tmp_path: Path):
    """A spec sync can add endpoints; none reaches the server until someone has said whether it returns pipeline
    records (data residency), and one that does must be excluded."""
    reviewed = dict(EDGE_REVIEWED)
    del reviewed["edge POST /api/v2/pipelines/{pipeline_id}/actions/resync"]
    openapi, overrides = _specs(tmp_path, overrides=reviewed)
    with pytest.raises(SystemExit, match=r"(?s)need a review.*actions/resync"):
        bm.build(openapi, overrides, tmp_path / "m", quiet=True)
    overrides.write_text(json.dumps({**EDGE_REVIEWED, "edge GET /api/v2/pipelines": {"records": True}}))
    with pytest.raises(SystemExit, match="returns pipeline records.*exclude: true"):
        bm.build(openapi, overrides, tmp_path / "m", quiet=True)
    overrides.write_text(json.dumps({**EDGE_REVIEWED, "edge GET /api/v2/pipelines": {"records": "yes"}}))
    with pytest.raises(SystemExit, match="records must be true or false"):
        bm.build(openapi, overrides, tmp_path / "m", quiet=True)
    overrides.write_text(json.dumps({**EDGE_REVIEWED, "edge GET /api/v2/pipelines": {"records": True, "exclude": True}}))
    manifest = bm.build(openapi, overrides, tmp_path / "m", quiet=True)
    assert "list_pipelines" not in {e["name"] for e in manifest["endpoints"]}


def test_every_shipped_endpoint_is_classified_as_not_returning_records():
    overrides = bm.load_overrides()
    shipped = {e.name for e in __import__("hevo_mcp_server.catalog", fromlist=["ENDPOINTS"]).ENDPOINTS}
    for key, ov in overrides.items():
        assert ov["records"] is (ov.get("exclude", False) is True), key
    assert len(shipped) == sum(1 for ov in overrides.values() if not ov.get("exclude"))


def test_committed_manifest_matches_the_specs(tmp_path: Path):
    """Fails when someone edits openapi/ or endpoint_overrides.json without rerunning the build."""
    fresh = tmp_path / "manifest"
    bm.build(out=fresh, quiet=True)
    committed = bm.MANIFEST_DIR
    assert (fresh / "endpoints.json").read_text() == (committed / "endpoints.json").read_text(), \
        "run `python build_manifest.py`"
    for f in sorted(fresh.rglob("*.json")):
        assert f.read_text() == (committed / f.relative_to(fresh)).read_text(), f"stale {f.name}: run the build"
