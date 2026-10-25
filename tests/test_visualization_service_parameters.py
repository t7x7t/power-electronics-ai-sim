"""Read-only parameter-view route on the Stage 11 visualization service."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from threading import Thread
import urllib.error
import urllib.request

import pytest

from pe_sim.visualization_service import create_server


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "visualization_service"
FIXTURE = FIXTURE_ROOT / "buck-contract-fixture"


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    shutil.copytree(FIXTURE, root / FIXTURE.name)
    server = create_server(root, "127.0.0.1", 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _request(server, method, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return exc.code, json.loads(body) if body else None


def test_parameter_route_serves_view_for_published_run(service):
    status, view = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/parameters")
    assert status == 200
    assert view["schema_version"] == "pe-sim.parameter-view.v1"
    assert view["run"]["run_id"] == FIXTURE.name


def test_parameter_view_contains_plant_group_and_digest(service):
    status, view = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/parameters")
    assert status == 200
    groups = view["parameter_set"]["groups"]
    assert any(group["id"] == "plant" for group in groups)
    assert isinstance(view["parameter_set"]["digest"], str)
    assert view["parameter_set"]["digest"]


def test_unknown_run_returns_run_not_found(service):
    status, body = _request(service, "GET", "/v1/runs/no-such-run/parameters")
    assert status == 404
    assert body["error"]["code"] == "run_not_found"


@pytest.mark.parametrize("method", ("POST", "PUT", "DELETE"))
def test_parameter_route_rejects_write_methods(service, method):
    status, body = _request(service, method, f"/v1/runs/{FIXTURE.name}/parameters")
    assert status == 405
    assert body["error"]["code"] == "read_only"


def test_discovery_reports_inspection_schemas_and_three_contracts(service):
    status, discovery = _request(service, "GET", "/v1/discovery")
    assert status == 200
    assert discovery["inspection_schemas"]["parameters"] == "pe-sim.parameter-view.v1"
    assert discovery["inspection_schemas"]["claim"] == "pe-sim.claim.v1"
    assert discovery["contract_schemas"] == {
        "topology": "topology-descriptor-v1",
        "trace": "1.0",
        "outputs": "1.0",
    }


def test_existing_contract_routes_are_unchanged(service):
    status, topology = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/topology")
    assert status == 200
    assert topology["schema"] == "topology-descriptor-v1"
    assert topology["descriptor_version"] == "1.0"
    for plane in ("trace", "outputs"):
        status, payload = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/{plane}")
        assert status == 200
        assert payload["schema_version"] == "1.0"
        assert payload["provenance"]["run_id"] == FIXTURE.name
