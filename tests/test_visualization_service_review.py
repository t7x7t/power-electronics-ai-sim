"""Read-only review route on the Stage 11 visualization service."""

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
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _json_body(body):
    return json.loads(body.decode("utf-8"))


def test_review_route_defaults_to_text(service):
    status, headers, body = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/review")
    assert status == 200
    assert headers.get_content_type() == "text/plain"
    assert headers.get_content_charset() == "utf-8"
    text = body.decode("utf-8")
    assert text.startswith("pe-sim run review:")
    assert body.isascii()


def test_review_route_serves_markdown(service):
    status, headers, body = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/review?format=markdown")
    assert status == 200
    assert headers.get_content_type() == "text/markdown"
    assert headers.get_content_charset() == "utf-8"
    text = body.decode("utf-8")
    assert "## 1." in text
    assert body.isascii()


def test_review_route_serves_json_envelope(service):
    status, headers, body = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/review?format=json")
    assert status == 200
    assert headers.get_content_type() == "application/json"
    review = _json_body(body)
    assert review["schema_version"] == "pe-sim.run-review.v1"
    assert review["run_id"] == FIXTURE.name


def test_review_route_rejects_unknown_format(service):
    status, headers, body = _request(service, "GET", f"/v1/runs/{FIXTURE.name}/review?format=yaml")
    assert status == 400
    assert headers.get_content_type() == "application/json"
    assert _json_body(body)["error"]["code"] == "invalid_request"


def test_review_route_unknown_run_returns_run_not_found(service):
    status, _, body = _request(service, "GET", "/v1/runs/no-such-run/review")
    assert status == 404
    assert _json_body(body)["error"]["code"] == "run_not_found"


def test_review_route_rejects_post(service):
    status, _, body = _request(service, "POST", f"/v1/runs/{FIXTURE.name}/review")
    assert status == 405
    assert _json_body(body)["error"]["code"] == "read_only"


def test_discovery_reports_review_schema_and_three_contracts(service):
    status, _, body = _request(service, "GET", "/v1/discovery")
    assert status == 200
    discovery = _json_body(body)
    assert discovery["inspection_schemas"]["review"] == "pe-sim.run-review.v1"
    assert set(discovery["contract_schemas"]) == {"topology", "trace", "outputs"}
    assert len(discovery["contract_schemas"]) == 3
