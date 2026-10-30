"""Demo launcher for the read-only visualization consumer example.

This is a demonstration launcher, not a product entry point.  It starts two
HTTP servers, both bound to ``127.0.0.1`` only:

* the read-only visualization service from ``pe_sim.visualization_service``,
  which serves recorded run contracts and has no write route;
* a static server for this directory, so ``demo.html`` and ``assets/`` load
  (a ``file://`` page cannot fetch), plus a live ``/catalog.json`` snapshot
  recomputed from ``pe_sim.catalog.scan_runs`` on every request.

The launcher itself writes nothing and offers no write method.  When
``--run-config`` is passed it additionally *supervises* a separately started
``pe-sim run --progress`` child process: stdin is closed, stdout is drained,
stderr is folded into an in-memory ``GET /live.json`` document, and the child
owns the simulation and its run package.  The launcher never imports the
runner and holds no ``Runner``/``Plant``/``Controller`` (``docs/70`` section
4.1).  Standard library only.

Usage::

    python examples/visualization_consumer/launch.py --runs-root runs
    python examples/visualization_consumer/launch.py --no-browser --port 8765
    python examples/visualization_consumer/launch.py --page demo.html
    python examples/visualization_consumer/launch.py --run-config experiment.json
    python examples/visualization_consumer/launch.py --run-config experiment.json --cancel-file .cancel

Resolution order for the runs root is the documented one
(``--runs-root`` -> ``PE_SIM_RUNS_ROOT`` -> ``./runs``).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Sequence

HERE = Path(__file__).resolve().parent

#: Schema tag of the demo's own ``/live.json`` document.
#:
#: Deliberately free of the kernel's schema prefix: the document is folded from
#: a child process's stderr by an example, is registered nowhere, and must never
#: be mistaken for a published envelope.  ``is_evidence`` is a constant
#: ``false`` for the same reason.
LIVE_SCHEMA = "demo-live-v1"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return None if number != number else number


def _step_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _empty_live_state() -> dict[str, Any]:
    return {
        "schema": LIVE_SCHEMA,
        "active": False,
        "run_id": None,
        "step_index": None,
        "fraction": None,
        "sequence": None,
        "status": None,
        "events": 0,
        "last_event": None,
        "skipped_lines": 0,
        "terminal": None,
        "is_evidence": False,
    }


def parse_live_lines(lines: Iterable[str]) -> dict[str, Any]:
    """Fold JSON Lines from the child's stderr into one live view (pure).

    ``run.started`` makes the view active, ``run.progress`` moves
    ``step_index``/``fraction`` forward only (a duplicated or replayed line can
    never drag the bar backwards), and ``run.finished`` copies its ``status``.
    A status is never inferred from an exit code, an elapsed time, or a file;
    an event without one yields ``"finished_without_status"``.

    ``sequence`` is the ordering key: a record whose sequence is not greater
    than the highest accepted one is stale and changes nothing.  Blank lines are
    ignored; a non-blank line that is not a JSON object with a string ``event``
    is counted in ``skipped_lines`` and changes nothing else.
    """

    state = _empty_live_state()
    highest = -1
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except ValueError:
            state["skipped_lines"] += 1
            continue
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            state["skipped_lines"] += 1
            continue
        sequence = _step_number(record.get("sequence"))
        if sequence is None:
            if state["sequence"] is not None:
                state["skipped_lines"] += 1
                continue
        else:
            if sequence <= highest:
                continue
            highest = sequence
            state["sequence"] = sequence
        event = record["event"]
        payload = record.get("data")
        data = payload if isinstance(payload, dict) else {}
        run_id = record.get("run_id")
        if isinstance(run_id, str) and run_id:
            state["run_id"] = run_id
        state["events"] += 1
        state["last_event"] = event
        if event == "run.started":
            state.update(active=True, status=None, terminal=None, step_index=None, fraction=None)
        elif event == "run.progress":
            step = _step_number(data.get("step_index"))
            if step is not None and (state["step_index"] is None or step > state["step_index"]):
                state["step_index"] = step
            fraction = _finite_number(data.get("fraction"))
            if fraction is not None:
                bounded = min(1.0, max(0.0, fraction))
                current = state["fraction"]
                state["fraction"] = bounded if current is None else max(current, bounded)
        elif event == "run.finished":
            status = data.get("status")
            state["active"] = False
            state["status"] = status if isinstance(status, str) and status else "finished_without_status"
            state["terminal"] = "event"
    return state


#: ``None`` means "this launcher supervises nothing": served as 404 rather than
#: as a fabricated ``active: false`` document, because an inactive document is
#: an observed terminal state and inventing one is indistinguishable from real
#: knowledge.
_live_state: dict[str, Any] | None = None
_live_lines: list[str] = []
_live_lock = threading.Lock()


def _live_snapshot() -> dict[str, Any] | None:
    with _live_lock:
        return None if _live_state is None else dict(_live_state)


def _append_live_line(line: str) -> None:
    global _live_state
    with _live_lock:
        if _live_state is None:
            return
        _live_lines.append(line)
        _live_state = parse_live_lines(_live_lines)


def _mark_live_exit(exit_code: int) -> None:
    """Record that the child ended without inventing a run status."""

    global _live_state
    with _live_lock:
        if _live_state is None:
            return
        document = parse_live_lines(_live_lines)
        if document["terminal"] != "event":
            document["terminal"] = "process_exit"
        document["active"] = False
        document["exit_code"] = exit_code
        _live_state = document


def _child_environment() -> dict[str, str]:
    """Environment for the child, with this checkout's ``src`` importable."""

    env = dict(os.environ)
    src = HERE.parents[1] / "src"
    if src.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
    return env


class _LiveChild:
    """Read one ``pe-sim run --progress`` child's streams and fold its events."""

    def __init__(self, process: "subprocess.Popen[str]") -> None:
        self.process = process
        self._stderr_pump = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_drain = threading.Thread(target=self._drain_stdout, daemon=True)
        self._watcher = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        for thread in (self._stderr_pump, self._stdout_drain, self._watcher):
            thread.start()

    def _pump_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        for line in stream:
            _append_live_line(line)

    def _drain_stdout(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for _line in stream:
            pass

    def _watch(self) -> None:
        code = self.process.wait()
        self._stderr_pump.join(timeout=5.0)
        self._stdout_drain.join(timeout=5.0)
        _mark_live_exit(code)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        self._watcher.join(timeout=10.0)
        if self.process.poll() is None:
            self.process.kill()
            self._watcher.join(timeout=5.0)


def _start_live_child(config: Path, cancel_file: Path | None) -> _LiveChild:
    """Start ``python -m pe_sim.cli run <config> --progress`` without a shell."""

    global _live_state
    command = [sys.executable, "-m", "pe_sim.cli", "run", str(config), "--progress"]
    if cancel_file is not None:
        command.extend(["--cancel-file", str(cancel_file)])
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
        env=_child_environment(),
    )
    with _live_lock:
        _live_lines.clear()
        _live_state = _empty_live_state()
    child = _LiveChild(process)
    child.start()
    return child


def _ensure_package_importable() -> None:
    """Allow running from a checkout that has not been pip-installed."""

    try:
        import pe_sim  # noqa: F401
    except ImportError:
        src = HERE.parents[1] / "src"
        if src.is_dir():
            sys.path.insert(0, str(src))
        # Retry once with the checkout on sys.path; a real failure propagates.
        import pe_sim  # noqa: F401


def _make_handler(runs_root: Path, live_snapshot=None):
    from pe_sim.catalog import CatalogError, scan_runs

    class Handler(SimpleHTTPRequestHandler):
        """Static demo files plus live ``/catalog.json`` and ``/live.json``."""

        def _write_json(self, status: int, document: dict[str, Any]) -> None:
            payload = json.dumps(document, ensure_ascii=True, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server protocol name
            path = self.path.split("?", 1)[0]
            if path == "/catalog.json":
                try:
                    self._write_json(200, scan_runs(runs_root).to_dict())
                except (CatalogError, OSError) as exc:
                    self._write_json(500, {"schema_version": "demo-catalog-error", "error": str(exc)})
                return
            if path == "/live.json":
                snapshot = live_snapshot() if live_snapshot is not None else None
                if snapshot is None:
                    # No supervisor: 404 rather than a fabricated inactive document.
                    self._write_json(404, {"schema": LIVE_SCHEMA, "supervised": False})
                else:
                    self._write_json(200, snapshot)
                return
            super().do_GET()

        def do_HEAD(self) -> None:  # noqa: N802 - keep probes cheap and honest
            if self.path.split("?", 1)[0] in {"/catalog.json", "/live.json"}:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            super().do_HEAD()

        def log_message(self, fmt: str, *args) -> None:  # noqa: A002
            return  # silence per-request noise; the launcher prints its own summary

    return partial(Handler, directory=str(HERE))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="Start the loopback visualization service and open the demo page.",
    )
    parser.add_argument(
        "--runs-root",
        default=None,
        help="directory to scan; defaults to PE_SIM_RUNS_ROOT or ./runs",
    )
    parser.add_argument("--port", type=int, default=0, help="service port; 0 picks a free one")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--page", default="demo.html", help="static page opened by the launcher"
    )
    parser.add_argument(
        "--run-config",
        default=None,
        help="also supervise `pe-sim run --progress` for this configuration",
    )
    parser.add_argument(
        "--cancel-file",
        default=None,
        help="forward a cooperative cancel file to the supervised child",
    )
    args = parser.parse_args(argv)

    _ensure_package_importable()
    from pe_sim.catalog import CatalogError, scan_runs
    from pe_sim.run_store import discovery_root
    from pe_sim.visualization_service import create_server

    runs_root = Path(discovery_root(args.runs_root)).expanduser().resolve()
    if not runs_root.is_dir():
        print(f"runs root 不是一个目录: {runs_root}", file=sys.stderr)
        return 2

    try:
        service = create_server(runs_root, port=args.port)
    except ValueError as exc:
        print(f"服务启动失败: {exc}", file=sys.stderr)
        return 2
    service_port = service.server_address[1]

    static = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(runs_root, _live_snapshot))
    static_port = static.server_address[1]

    for server in (service, static):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    child: _LiveChild | None = None
    if args.run_config is not None:
        config = Path(args.run_config).expanduser().resolve()
        if not config.is_file():
            print(f"--run-config 不是一个文件: {config}", file=sys.stderr)
            service.shutdown()
            static.shutdown()
            return 2
        cancel_file = Path(args.cancel_file).expanduser().resolve() if args.cancel_file else None
        child = _start_live_child(config, cancel_file)

    page_path = args.page.lstrip("/")
    page = (
        f"http://127.0.0.1:{static_port}/{page_path}"
        f"?service=http://127.0.0.1:{service_port}"
        f"{'&live=1' if child is not None else ''}"
    )

    try:
        catalog = scan_runs(runs_root)
        count: int | None = len(catalog.runs)
        unreadable = len(catalog.skipped)
    except (CatalogError, OSError) as exc:  # summary only; the page reports details
        count, unreadable = None, 0
        print(f"清单扫描失败: {exc}", file=sys.stderr)

    print("PE Sim 证据阅读器（demo）")
    print(f"  runs root : {runs_root}")
    print(f"  服务       : http://127.0.0.1:{service_port}")
    print(f"  页面       : {page}")
    print(f"  实时       : {'监管中（demo-live-v1，未发布，不是证据）' if child is not None else '未启用（--run-config 未给出）'}")
    if count is None:
        print("  清单       : 无法统计")
    else:
        skipped = f"（{unreadable} 个无法读取）" if unreadable > 0 else ""
        print(f"  清单       : {count} 个 run{skipped}")
    print("  停止       : Ctrl+C")
    print("只读：仅 GET；不写 run 包；显示的是契约字段，不是电气真值。")

    if not args.no_browser:
        webbrowser.open(page)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("正在停止…")
    finally:
        if child is not None:
            child.stop()
        service.shutdown()
        static.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
