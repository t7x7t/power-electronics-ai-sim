"""Small, read-only HTTP service for verified Stage 11 visualization data."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from .outputs import OUTPUT_SCHEMA_VERSION
from .topology import TOPOLOGY_SCHEMA
from .traces import TRACE_SCHEMA_VERSION
from .workbench_export import WorkbenchExportError, load_run_workbench_contracts


SERVICE_SCHEMA = "pe-sim.visualization-service.v1"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PLANES = {"topology", "trace", "outputs"}
_CONTRACT_SCHEMAS = {
    "topology": TOPOLOGY_SCHEMA,
    "trace": TRACE_SCHEMA_VERSION,
    "outputs": OUTPUT_SCHEMA_VERSION,
}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class VisualizationServiceError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _error(status: HTTPStatus, code: str, message: str) -> VisualizationServiceError:
    return VisualizationServiceError(status, code, message)


def _safe_run_dir(root: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id) or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_run_id", "run_id must be a simple relative identifier")
    raw_candidate = root / run_id
    # Reject the directory entry itself before resolving it.  This prevents a
    # symlink inside the configured root from redirecting reads elsewhere.
    if raw_candidate.is_symlink():
        raise _error(HTTPStatus.NOT_FOUND, "run_not_found", "published run was not found")
    candidate = raw_candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_run_id", "run_id escapes the configured runs root")
    if not candidate.is_dir() or candidate.is_symlink():
        raise _error(HTTPStatus.NOT_FOUND, "run_not_found", "published run was not found")
    return candidate


def _published_runs(root: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not root.is_dir():
        return result
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.is_symlink() or not _RUN_ID.fullmatch(child.name):
            continue
        try:
            contracts = load_run_workbench_contracts(child)
        except (WorkbenchExportError, OSError, ValueError):
            continue
        # Discovery must only advertise IDs that address their own directory.
        # This prevents a malformed package from returning an unrouteable
        # manifest identity.
        if contracts.run_id != child.name:
            continue
        result.append({
            "run_id": contracts.run_id,
            "manifest_sha256": contracts.manifest_sha256,
            "package_sha256": contracts.package_sha256,
            "planes": sorted(_PLANES),
        })
    return result


def create_server(runs_root: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Create a server; request handling is read-only and scoped to runs_root."""
    if host.strip().lower() not in _LOOPBACK_HOSTS:
        raise ValueError("visualization service may bind only to a loopback host")
    root = Path(runs_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"runs root does not exist or is not a directory: {root}")

    class Handler(BaseHTTPRequestHandler):
        server_version = "pe-sim-visualization/1"

        def _cors_origin(self) -> str | None:
            origin = self.headers.get("Origin")
            if not origin:
                return None
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                return None
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                return None
            return origin

        def _send(self, status: HTTPStatus, payload: Any, *, body: bytes | None = None) -> None:
            body = _json_bytes(payload) if body is None else body
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _handle_error(self, exc: VisualizationServiceError) -> None:
            self._send(exc.status, {"schema_version": SERVICE_SCHEMA, "error": {"code": exc.code, "message": exc.message}})

        def do_POST(self) -> None:  # noqa: N802
            self._handle_error(_error(HTTPStatus.METHOD_NOT_ALLOWED, "read_only", "the visualization service is read-only"))

        def do_PUT(self) -> None:  # noqa: N802
            self._handle_error(_error(HTTPStatus.METHOD_NOT_ALLOWED, "read_only", "the visualization service is read-only"))

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle_error(_error(HTTPStatus.METHOD_NOT_ALLOWED, "read_only", "the visualization service is read-only"))

        def do_OPTIONS(self) -> None:  # noqa: N802
            requested_method = self.headers.get("Access-Control-Request-Method", "GET").upper()
            if requested_method != "GET":
                self._handle_error(_error(HTTPStatus.METHOD_NOT_ALLOWED, "read_only", "only GET is permitted"))
                return
            # A preflight response is harmless for non-loopback origins but
            # deliberately omits ACAO, so browsers cannot use it cross-site.
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Origin")
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._get()
            except VisualizationServiceError as exc:
                self._handle_error(exc)
            except (OSError, ValueError, WorkbenchExportError) as exc:
                self._handle_error(_error(HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_run", str(exc)))

        def _get(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                self._send(HTTPStatus.OK, {"schema_version": SERVICE_SCHEMA, "status": "ok", "read_only": True})
                return
            if path == "/v1/discovery":
                self._send(HTTPStatus.OK, {
                    "schema_version": SERVICE_SCHEMA,
                    "read_only": True,
                    "contract_schemas": _CONTRACT_SCHEMAS,
                    "runs": _published_runs(root),
                })
                return
            parts = path.split("/")
            if len(parts) == 5 and parts[:3] == ["", "v1", "runs"] and parts[4] in _PLANES:
                run_id = unquote(parts[3])
                contracts = load_run_workbench_contracts(_safe_run_dir(root, run_id))
                if contracts.run_id != run_id:
                    raise _error(HTTPStatus.NOT_FOUND, "run_not_found", "published run was not found")
                payload = contracts.to_dict()[parts[4]]
                self._send(HTTPStatus.OK, payload)
                return
            raise _error(HTTPStatus.NOT_FOUND, "unknown_route", "route is not available")

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(runs_root: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(runs_root, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = ["SERVICE_SCHEMA", "VisualizationServiceError", "create_server", "serve"]
