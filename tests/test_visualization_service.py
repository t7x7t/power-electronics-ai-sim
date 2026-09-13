from __future__ import annotations

import json
from http.client import HTTPConnection
from threading import Thread
from unittest.mock import patch

import pytest

from pe_sim.artifacts import artifact_index, manifest_digest, package_digest
from pe_sim.visualization_service import create_server


def _run(root, run_id="buck-demo"):
    run = root / run_id
    run.mkdir()
    (run / "samples.json").write_text(json.dumps([
        {"step_index": 0, "time_s": 0.0, "vout": 0.0, "inductor_current": 0.0, "action": 0.2},
        {"step_index": 1, "time_s": 1e-5, "vout": 0.0025, "inductor_current": 0.24, "action": 0.1995},
    ]), encoding="utf-8")
    (run / ".run-state.json").write_text(json.dumps({"run_id": run_id, "status": "PUBLISHED"}), encoding="utf-8")
    manifest = {
        "run_id": run_id, "status": "QUALIFIED", "run_mode": "exploratory",
        "source_commit": "a" * 40,
        "plant": {"id": "buck", "identity": {"topology": "buck", "kind": "ideal_averaged_reference", "parameters": {
            "input_voltage_v": 12.0, "inductance_h": 100e-6, "capacitance_f": 470e-6,
            "load_resistance_ohm": 10.0, "integration_step_s": 1e-6,
        }}},
    }
    manifest["artifacts"] = artifact_index(run)
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run, manifest)
    manifest["hashes"] = {"manifest_sha256": manifest["manifest_sha256"], "package_sha256": manifest["package_sha256"]}
    (run / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return run


def _set_manifest_identity(run, run_id):
    """Create a internally valid package whose directory and manifest differ."""
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    manifest["manifest_sha256"] = manifest_digest(manifest)
    manifest["package_sha256"] = package_digest(run, manifest)
    manifest["hashes"] = {
        "manifest_sha256": manifest["manifest_sha256"],
        "package_sha256": manifest["package_sha256"],
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (run / ".run-state.json").write_text(json.dumps({"run_id": run_id, "status": "PUBLISHED"}), encoding="utf-8")


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    run = _run(root)
    server = create_server(root, "127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, root, run
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _get(server, path):
    return _request(server, "GET", path)


def _request(server, method, path, headers=None):
    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    conn.request(method, path, headers=headers or {})
    response = conn.getresponse()
    raw = response.read()
    status, response_headers = response.status, dict(response.getheaders())
    conn.close()
    return status, response_headers, json.loads(raw) if raw else None


def test_health_discovery_and_contract_routes(service):
    server, _, run = service
    before = {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()}
    status, _, health = _get(server, "/health")
    assert status == 200 and health["read_only"] is True
    status, _, discovery = _get(server, "/v1/discovery")
    assert status == 200 and discovery["runs"][0]["run_id"] == "buck-demo"
    assert discovery["contract_schemas"] == {
        "topology": "topology-descriptor-v1",
        "trace": "1.0",
        "outputs": "1.0",
    }
    for plane in ("topology", "trace", "outputs"):
        status, _, payload = _get(server, f"/v1/runs/buck-demo/{plane}")
        assert status == 200
        if plane != "topology":
            assert payload["provenance"]["run_id"] == "buck-demo"
    assert {path.name: path.read_bytes() for path in run.iterdir() if path.is_file()} == before


def test_tampered_run_is_not_served(service):
    server, _, run = service
    (run / "samples.json").write_text("[]", encoding="utf-8")
    status, _, body = _get(server, "/v1/runs/buck-demo/trace")
    assert status == 422 and body["error"]["code"] == "invalid_run"


def test_manifest_identity_must_match_requested_directory_and_discovery(service):
    server, _, run = service
    _set_manifest_identity(run, "manifest-only-id")
    status, _, discovery = _get(server, "/v1/discovery")
    assert status == 200 and discovery["runs"] == []
    status, _, body = _get(server, "/v1/runs/buck-demo/trace")
    assert status == 404 and body["error"]["code"] == "run_not_found"


def test_path_traversal_unknown_route_and_write_are_rejected(service):
    server, _, _ = service
    status, _, body = _get(server, "/v1/runs/%2e%2e/trace")
    assert status in {400, 404} and body["error"]["code"] in {"invalid_run_id", "run_not_found"}
    status, _, body = _get(server, "/v1/nope")
    assert status == 404 and body["error"]["code"] == "unknown_route"
    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    conn.request("POST", "/health", body=b"x")
    response = conn.getresponse()
    assert response.status == 405
    conn.close()


def test_symlinked_run_is_rejected_before_resolution(service):
    server, _, _ = service
    # Windows CI may not grant symlink creation privileges. Mocking the raw
    # directory-entry predicate verifies the security branch deterministically.
    with patch("pe_sim.visualization_service.Path.is_symlink", autospec=True, side_effect=lambda path: path.name == "buck-alias"):
        status, _, body = _get(server, "/v1/runs/buck-alias/trace")
    assert status == 404
    assert body["error"]["code"] == "run_not_found"


def test_cors_allows_only_loopback_origins_and_get_preflight(service):
    server, _, _ = service
    status, headers, _ = _request(server, "GET", "/health", {"Origin": "http://localhost:5173"})
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert headers["Vary"] == "Origin"
    assert "Access-Control-Allow-Credentials" not in headers

    status, headers, _ = _request(server, "GET", "/health", {"Origin": "https://example.test"})
    assert status == 200
    assert "Access-Control-Allow-Origin" not in headers
    assert headers["Vary"] == "Origin"

    status, headers, body = _request(server, "OPTIONS", "/v1/discovery", {
        "Origin": "http://[::1]:4173",
        "Access-Control-Request-Method": "GET",
    })
    assert status == 204 and body is None
    assert headers["Access-Control-Allow-Origin"] == "http://[::1]:4173"
    assert headers["Access-Control-Allow-Methods"] == "GET, OPTIONS"

    status, headers, body = _request(server, "OPTIONS", "/v1/discovery", {
        "Origin": "https://example.test",
        "Access-Control-Request-Method": "GET",
    })
    assert status == 204 and body is None
    assert "Access-Control-Allow-Origin" not in headers

    status, _, body = _request(server, "OPTIONS", "/v1/discovery", {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
    })
    assert status == 405
    assert body["error"]["code"] == "read_only"


@pytest.mark.parametrize("host", ("0.0.0.0", "192.0.2.10", "example.test"))
def test_server_rejects_non_loopback_bind_hosts(tmp_path, host):
    root = tmp_path / "runs"
    root.mkdir()

    with pytest.raises(ValueError, match="loopback"):
        create_server(root, host, 0)
