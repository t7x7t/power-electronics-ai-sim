"""Opt-in browser acceptance for the source-distributed reference workbench.

This suite deliberately uses Python Playwright rather than an npm dependency.
It is skipped in ordinary development unless ``PE_SIM_RUN_BROWSER_E2E=1``.
The release gate enables that variable and supplies an isolated workbench copy
plus a started loopback visualization service.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest


if os.environ.get("PE_SIM_RUN_BROWSER_E2E") != "1":
    pytestmark = pytest.mark.skip(reason="set PE_SIM_RUN_BROWSER_E2E=1 to run local browser acceptance")


def _unused_loopback_port() -> int:
    """Reserve a likely free port long enough to pass it to Vite."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_http(url: str, process: subprocess.Popen[str], log_path: Path) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"Vite exited before readiness ({process.returncode}):\n{output}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    output = log_path.read_text(encoding="utf-8", errors="replace")
    pytest.fail(f"Vite did not become ready at {url}:\n{output}")


def _source_url(workbench_url: str) -> tuple[str, str]:
    """Choose service mode when configured; retain fixture-only local use."""
    service_url = os.environ.get("PE_SIM_BROWSER_SERVICE_URL")
    run_id = os.environ.get("PE_SIM_BROWSER_RUN_ID")
    if bool(service_url) != bool(run_id):
        pytest.fail("set both PE_SIM_BROWSER_SERVICE_URL and PE_SIM_BROWSER_RUN_ID, or neither")
    if service_url and run_id:
        return (
            f"{workbench_url}/?source=service&service={service_url}&run={run_id}",
            f"service {run_id}",
        )
    return f"{workbench_url}/?fixture=reference", "reference"


def _stop_vite_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop only the npm/Vite tree created by this test.

    On Windows, terminating ``npm.cmd`` alone can leave its Node/Vite child
    listening. ``taskkill /T`` targets the recorded parent PID and descendants
    only; it does not discover or stop any unrelated developer process.
    """
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def test_reference_workbench_interactions_in_browser(tmp_path: Path) -> None:
    """Render all three contracts and exercise the public waveform controls."""
    if importlib.util.find_spec("playwright.sync_api") is None:
        pytest.fail("Python Playwright is required when PE_SIM_RUN_BROWSER_E2E=1")

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    repository_root = Path(__file__).parents[1]
    workbench = Path(os.environ.get("PE_SIM_WORKBENCH_DIR", repository_root / "workbench")).resolve()
    if not (workbench / "package.json").is_file():
        pytest.fail(f"PE_SIM_WORKBENCH_DIR is not a workbench source directory: {workbench}")
    if not (workbench / "node_modules").is_dir():
        pytest.fail(f"workbench dependencies are not installed in {workbench}; run npm ci first")

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    port = _unused_loopback_port()
    workbench_url = f"http://127.0.0.1:{port}"
    vite_log = tmp_path / "vite-browser-e2e.log"
    with vite_log.open("w", encoding="utf-8") as log_file:
        server = subprocess.Popen(
            [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=workbench,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_http(workbench_url, server, vite_log)
            page_url, source_label = _source_url(workbench_url)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=True)
                except PlaywrightError as exc:
                    pytest.fail(f"Playwright Chromium is unavailable: {exc}")
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                    page.locator("#status.ready").wait_for(state="visible", timeout=30_000)
                    assert source_label in page.locator("#status").inner_text()
                    assert page.locator("#error[hidden]").count() == 1

                    assert page.locator("topology-panel svg").count() == 1
                    assert page.locator("topology-panel .topology-canvas").is_visible()
                    canvas = page.locator("waveform-panel canvas.wave-canvas")
                    assert canvas.count() == 1
                    assert canvas.is_visible()
                    assert page.locator("output-panel .output-card").count() >= 1

                    checks = page.locator("waveform-panel input[data-signal]")
                    assert checks.count() >= 2
                    assert checks.nth(0).is_checked()
                    checks.nth(0).uncheck()
                    assert not checks.nth(0).is_checked()
                    checks.nth(1).check()
                    assert checks.nth(1).is_checked()

                    page.get_by_role("button", name="Zoom in").click()
                    assert page.locator("button[data-zoom=reset]").inner_text() == "150%"
                    canvas.click(position={"x": 120, "y": 80})
                    cursor = page.locator("waveform-panel .cursor-readout")
                    assert "t =" in cursor.inner_text()
                    assert "Click the plot" not in cursor.inner_text()
                finally:
                    browser.close()
        finally:
            _stop_vite_process_tree(server)
