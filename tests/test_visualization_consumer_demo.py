"""Executable specification for the browser-free visualization consumer demo.

The demo is a read-only viewer; these tests never start a server or a browser.
They guard three regressions: assets lose verifiable provenance, the page starts
issuing writes or reaching the network, and the launcher becomes a process that
writes to disk or needs third parties.  A missing artifact fails with its path
and expectation, never a ``KeyError`` or an unhandled ``FileNotFoundError``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "examples" / "visualization_consumer"
ASSETS = ROOT / "assets"

REQUIRED_ARTIFACTS = (
    "demo.html", "demo.js", "launch.py", "demo.cmd", "README.md",
    "panels.json", "verify_panels.py",
    "assets/pico.min.css", "assets/theme.css", "assets/icons.svg", "assets/LICENSES.md",
)
#: The static skeleton.  Panel bodies are created by ``mountPanels()`` from the
#: manifest, so listing them here would re-introduce the hardcoding this
#: manifest exists to remove.
REQUIRED_IDS = (
    "app-bar-status", "fleet-groups", "detail", "run-title", "error-region",
    "panel-rack", "live-region",
)
PANEL_NAMES = frozenset({"provenance", "topology", "trace", "outputs", "parameters", "review"})
PANEL_NEEDS = frozenset({"trace", "topology", "outputs", "parameters", "review"})
PANELS_MARKER = "demo-panels-v1"
PROVENANCE_EXEMPT = frozenset({"LICENSES.md", "README.md"})
#: ``__future__`` is a compiler directive, not a runtime dependency.
LAUNCHER_IMPORT_ROOTS = frozenset({
    "argparse", "asyncio", "contextlib", "functools", "http", "json", "os",
    "pathlib", "socket", "subprocess", "sys", "threading", "typing", "webbrowser",
    "__future__",
})
#: Writes only.  ``subprocess`` is deliberately **not** a marker: R1 supervises a
#: child process, and the supervision itself is asserted separately below.
LAUNCHER_WRITE_MARKERS = ("os.remove", "os.rename", "os.replace", "shutil")
FORBIDDEN_JS_MARKERS = (
    "Math.random", "Date.now", "new Date(", "http://", "https://",
    "localStorage", "sessionStorage", "document.cookie",
)

WRITE_METHOD_RE = re.compile(r"""(["'])(POST|PUT|DELETE)\1""")
ATTRIBUTE_TEMPLATE = r"""(?<![\w-]){}\s*=\s*["']([^"']+)["']"""
ID_ATTRIBUTE_RE = re.compile(ATTRIBUTE_TEMPLATE.format("id"), re.IGNORECASE)


def _read_text(path: Path) -> str:
    assert path.is_file(), f"expected artifact at {path}; finish or create it first"
    return path.read_text(encoding="utf-8")


def _attribute(tag: str, name: str) -> str | None:
    pattern = ATTRIBUTE_TEMPLATE.format(re.escape(name))
    match = re.search(pattern, tag, re.IGNORECASE)
    return match.group(1) if match else None


def _tags(text: str, name: str) -> list[str]:
    return re.findall(rf"<{name}\b[^>]*>", text, re.IGNORECASE)


# --- A. the file family exists -------------------------------------------------

@pytest.mark.parametrize("relative", REQUIRED_ARTIFACTS)
def test_required_artifact_exists_and_is_not_empty(relative: str) -> None:
    path = ROOT / relative
    assert path.is_file(), f"missing demo artifact {relative!r}; expected a file at {path}"
    assert path.stat().st_size > 0, f"demo artifact {relative!r} is empty; expected bytes at {path}"


# --- B. the HTML skeleton is static and self-contained -------------------------


def test_head_loads_local_stylesheets_in_contract_order() -> None:
    html = _read_text(ROOT / "demo.html")
    head = re.search(r"<head\b[^>]*>(.*?)</head>", html, re.IGNORECASE | re.DOTALL)
    assert head, f"demo.html must contain <head>...</head>; prefix={html[:120]!r}"
    hrefs = [href for tag in _tags(head.group(1), "link") if (href := _attribute(tag, "href"))]
    expected = ("assets/pico.min.css", "assets/theme.css")
    for href in expected:
        assert href in hrefs, f"<head> must link {href!r}; found {hrefs}"
    assert hrefs.index(expected[0]) < hrefs.index(expected[1]), f"expected order {expected}; got {hrefs}"


@pytest.mark.parametrize("element_id", REQUIRED_IDS)
def test_required_html_id_is_present(element_id: str) -> None:
    ids = ID_ATTRIBUTE_RE.findall(_read_text(ROOT / "demo.html"))
    assert element_id in ids, f"expected id={element_id!r}; demo.html defines {sorted(set(ids))}"


def test_html_has_one_mount_point_and_no_hardcoded_panels() -> None:
    """Replaces ``test_exactly_six_sections_declare_the_six_panels``.

    The old assertion counted six ``<section data-panel=…>`` in the HTML, which
    a copy-pasted skeleton satisfied without the page being an extension point
    at all.  The replacement forbids hardcoded panels outright and requires the
    single mount point the manifest-driven host mounts into.
    """

    html = _read_text(ROOT / "demo.html")
    ids = ID_ATTRIBUTE_RE.findall(html)
    assert ids.count("panel-rack") == 1, f"expected exactly one id=panel-rack; got {sorted(ids)}"
    assert not [i for i in ids if i.startswith("panel-") and i != "panel-rack"], (
        "panel bodies exist only after mountPanels(); demo.html must not declare them"
    )
    static = [tag for tag in _tags(html, "section") if _attribute(tag, "data-panel") is not None]
    assert not static, f"panels must come from panels.json, not demo.html; found {static}"


def test_panels_manifest_is_the_single_source_of_the_builtins() -> None:
    data = json.loads(_read_text(ROOT / "panels.json"))
    assert data.get("marker") == PANELS_MARKER, (
        f"marker must be {PANELS_MARKER!r} (no pe-sim. prefix, it is not a kernel envelope); "
        f"got {data.get('marker')!r}"
    )
    panels = data.get("panels")
    assert isinstance(panels, list) and panels, f"panels must be a non-empty list; got {panels!r}"
    ids = [panel.get("id") for panel in panels]
    assert len(ids) == len(set(ids)), f"panel ids must be unique; got {ids}"
    assert set(ids) == PANEL_NAMES, f"expected the six builtins {sorted(PANEL_NAMES)}; got {sorted(map(str, ids))}"
    for panel in panels:
        needs = panel.get("needs")
        assert isinstance(needs, list) and needs, f"{panel.get('id')}: needs must be non-empty; got {needs!r}"
        assert set(needs) <= PANEL_NEEDS, f"{panel.get('id')}: unknown needs {sorted(set(needs) - PANEL_NEEDS)}"
        assert isinstance(panel.get("title"), str) and panel["title"].strip(), f"{panel.get('id')}: title required"
        assert "entry" not in panel, f"{panel.get('id')}: builtins render in demo.js; 'entry' is for custom panels"


def test_demo_js_mounts_from_manifest_with_fallback_and_import() -> None:
    js = _read_text(ROOT / "demo.js")
    for needle in ('"panels.json"', '"demo-panels-v1"', "function mountPanels(", "import(`./", "DEFAULT_PANELS"):
        assert needle in js, f"demo.js must contain {needle!r} so panels.json stays the extension point"
    assert "PANEL_NAMES" not in js, "demo.js must not keep a hardcoded PANEL_NAMES list"
    body = re.search(r"async function renderPanel\(.*?\n}\n", js, re.DOTALL)
    assert body, "renderPanel must be a top-level async function"
    assert "try {" in body.group(0) and "catch (error)" in body.group(0), (
        "each panel render needs its own try/catch so one broken panel cannot blank the page"
    )


def test_verify_panels_accepts_the_shipped_manifest() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "verify_panels.py")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, (
        f"verify_panels.py rejected panels.json: rc={completed.returncode}; "
        f"out={completed.stdout!r}; err={completed.stderr!r}"
    )


@pytest.mark.parametrize("mutation", ["duplicate-id", "unknown-need", "prefixed-marker", "escaping-entry"])
def test_verify_panels_rejects_broken_manifest(tmp_path: Path, mutation: str) -> None:
    data = json.loads(_read_text(ROOT / "panels.json"))
    if mutation == "duplicate-id":
        data["panels"].append(dict(data["panels"][0]))
    elif mutation == "unknown-need":
        data["panels"][0]["needs"] = ["bogus"]
    elif mutation == "prefixed-marker":
        data["marker"] = "pe-sim.panels-v1"
    else:
        data["panels"].append({"id": "extra", "title": "x", "needs": ["trace"], "entry": "../demo.js"})
    target = tmp_path / "panels.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "verify_panels.py"), str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 1, f"expected exit 1 for {mutation}; got {completed.returncode}: {completed.stdout!r}"


def test_verify_panels_accepts_a_custom_entry_and_scans_it_for_network_tokens(tmp_path: Path) -> None:
    data = json.loads(_read_text(ROOT / "panels.json"))
    data["panels"].append({"id": "extra", "title": "自定义", "needs": ["trace"], "entry": "extra.js"})
    target = tmp_path / "panels.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    entry = tmp_path / "extra.js"
    entry.write_text("export default () => '<p>ok</p>';\n", encoding="utf-8")
    ok = subprocess.run(
        [sys.executable, str(ROOT / "verify_panels.py"), str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert ok.returncode == 0 and "7 panels" in ok.stdout, (
        f"a valid custom entry must be accepted; rc={ok.returncode}; out={ok.stdout!r}"
    )
    entry.write_text("export default () => fetch('/x');\n", encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, str(ROOT / "verify_panels.py"), str(target)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert bad.returncode == 1 and "fetch(" in bad.stdout, f"network token must be rejected; out={bad.stdout!r}"


def test_the_only_external_script_is_demo_js() -> None:
    html = _read_text(ROOT / "demo.html")
    sources = [src for tag in _tags(html, "script") if (src := _attribute(tag, "src")) is not None]
    assert sources == ["demo.js"], f"expected exactly ['demo.js']; got {sources}"


def test_html_is_offline_and_has_no_inline_style_block() -> None:
    html = _read_text(ROOT / "demo.html")
    urls = re.findall(r"https?://", html, re.IGNORECASE)
    assert not urls, f"demo.html must not contain http(s):// literals; got {urls}"
    assert "<style" not in html.lower(), "demo.html must not embed an inline <style> block"


# --- C. demo.js only reads and is deterministic --------------------------------


def test_js_never_selects_an_http_method() -> None:
    assert "method:" not in _read_text(ROOT / "demo.js"), "demo.js must not pass 'method:' to fetch"


def test_js_credentials_match_every_fetch() -> None:
    js = _read_text(ROOT / "demo.js")
    fetches, credentials = js.count("fetch("), js.count("credentials")
    assert credentials >= fetches and credentials >= 1, (
        f"expected credentials >= fetch( and >= 1; got credentials={credentials}, fetch(={fetches}"
    )


def test_js_has_no_write_method_literals() -> None:
    found = [match.group(0) for match in WRITE_METHOD_RE.finditer(_read_text(ROOT / "demo.js"))]
    assert not found, f"demo.js must not contain 'POST'/'PUT'/'DELETE' literals; got {found}"


@pytest.mark.parametrize("marker", sorted(FORBIDDEN_JS_MARKERS))
def test_js_avoids_forbidden_marker(marker: str) -> None:
    js = _read_text(ROOT / "demo.js")
    assert marker not in js, f"demo.js must not contain {marker!r}"


# --- D. assets have verifiable provenance --------------------------------------


def test_every_asset_has_a_line_with_its_real_sha256() -> None:
    assert ASSETS.is_dir(), f"expected assets directory at {ASSETS}"
    names = sorted(p.name for p in ASSETS.iterdir() if p.is_file() and p.name not in PROVENANCE_EXEMPT)
    assert names, f"expected localized assets under {ASSETS}; found none"
    lines = _read_text(ASSETS / "LICENSES.md").splitlines()
    problems: list[str] = []
    for name in names:
        digest = hashlib.sha256((ASSETS / name).read_bytes()).hexdigest()
        mentioning = [line for line in lines if name in line]
        if not mentioning:
            problems.append(f"{name}: no line mentions it; expected SHA-256 {digest}")
        elif not any(digest in line.lower() for line in mentioning):
            problems.append(f"{name}: expected SHA-256 {digest}; lines={mentioning}")
    assert not problems, "asset provenance is incomplete:\n" + "\n".join(problems)


@pytest.mark.parametrize("license_id", ("MIT", "ISC"))
def test_licenses_md_names_both_licenses(license_id: str) -> None:
    assert license_id in _read_text(ASSETS / "LICENSES.md"), f"LICENSES.md must name {license_id}"


def test_icons_svg_declares_at_least_ten_prefixed_symbols() -> None:
    symbols = _tags(_read_text(ASSETS / "icons.svg"), "symbol")
    assert len(symbols) >= 10, f"expected >= 10 <symbol> elements; got {len(symbols)}"
    identifiers: list[str] = []
    for tag in symbols:
        identifier = _attribute(tag, "id")
        assert identifier is not None, f"every <symbol> needs an id; got {tag!r}"
        identifiers.append(identifier)
    offenders = [identifier for identifier in identifiers if not identifier.startswith("i-")]
    assert not offenders, f"every symbol id must start with 'i-'; offenders={offenders}"


# --- E. the launcher is a read-only standard-library process -------------------


def _launcher() -> tuple[str, ast.Module]:
    path = ROOT / "launch.py"
    source = _read_text(path)
    try:
        return source, ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        pytest.fail(f"launch.py at {path} must parse as Python; SyntaxError: {exc}")


def _imported_roots(tree: ast.Module) -> list[tuple[str, int]]:
    roots: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots += [(alias.name.split(".")[0], node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            roots.append((module.split(".")[0], node.lineno))
    return roots


def _literal_mode(call: ast.Call) -> str | None:
    mode = call.args[1] if len(call.args) > 1 else next(
        (keyword.value for keyword in call.keywords if keyword.arg == "mode"), None
    )
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return mode.value
    return None


def test_launcher_parses_and_imports_only_whitelisted_modules() -> None:
    _source, tree = _launcher()
    assert isinstance(tree, ast.Module), f"expected ast.Module; got {type(tree).__name__}"
    offenders = [
        f"{name!r} at line {line}" for name, line in _imported_roots(tree)
        if name not in LAUNCHER_IMPORT_ROOTS and not name.startswith("pe_sim")
    ]
    assert not offenders, f"launch.py may import only {sorted(LAUNCHER_IMPORT_ROOTS)} or pe_sim*; got {offenders}"


def test_launcher_never_opens_a_file_for_writing() -> None:
    _source, tree = _launcher()
    # Builtin open only; webbrowser.open is a browser action, not file I/O.
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
    ]
    offenders = [
        f"line {call.lineno}: open(..., mode={_literal_mode(call)!r})"
        for call in calls if _literal_mode(call) not in {"r", "rb"}
    ]
    assert not offenders, f"launch.py may open() only with mode 'r'/'rb'; got {offenders}"


def test_launcher_has_no_write_or_process_markers() -> None:
    source, _tree = _launcher()
    found = [marker for marker in LAUNCHER_WRITE_MARKERS if marker in source]
    assert not found, f"launch.py must not contain write markers; got {found}"


def test_launcher_supervises_a_child_without_a_shell() -> None:
    """R1 supervises ``pe-sim run --progress`` as a separate process only.

    ``shell=False`` keeps a config path from being re-parsed as shell syntax,
    and the command must be a literal argv list rather than an interpolated
    string.  ``docs/70`` section 4.1 forbids holding a Runner in-process; a
    supervised child is the documented alternative.
    """

    _source, tree = _launcher()
    popens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert len(popens) == 1, f"expected exactly one subprocess.Popen; got {len(popens)}"
    keywords = {keyword.arg: keyword.value for keyword in popens[0].keywords}
    shell = keywords.get("shell")
    assert isinstance(shell, ast.Constant) and shell.value is False, (
        "the supervised child must be started with shell=False"
    )
    stdin = keywords.get("stdin")
    assert isinstance(stdin, ast.Attribute) and stdin.attr == "DEVNULL", (
        "the child must not inherit a terminal on stdin"
    )
    first = popens[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "command", (
        "Popen must receive the literal argv list built earlier, not a string"
    )


def test_live_document_is_demo_local_and_never_called_evidence() -> None:
    source, _tree = _launcher()
    assert 'LIVE_SCHEMA = "demo-live-v1"' in source, "the live document must carry the demo-local marker"
    assert '"is_evidence": False' in source, "the live document must declare is_evidence: false"
    assert "pe-sim." not in source, "the demo live document must not borrow the kernel schema prefix"


def test_launcher_help_exits_cleanly_with_output() -> None:
    launch_py = ROOT / "launch.py"
    assert launch_py.is_file(), f"missing launcher at {launch_py}"
    command = [sys.executable, str(launch_py), "--help"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{' '.join(command)} did not finish within 30 seconds")
    assert completed.returncode == 0, f"expected exit code 0; got {completed.returncode}; stderr={completed.stderr!r}"
    assert completed.stdout.strip(), f"expected non-empty --help output; stdout={completed.stdout!r}"


def _live_module():
    path = ROOT / "launch.py"
    spec = importlib.util.spec_from_file_location("demo_launcher_under_test", path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(sequence: int, name: str, data: dict | None = None) -> str:
    return json.dumps(
        {"schema": "pe-sim.run-event.v1", "sequence": sequence, "event": name, "run_id": "r1", "data": data or {}},
        ensure_ascii=True,
    )


def test_live_fold_takes_its_terminal_state_from_the_event() -> None:
    live = _live_module()
    document = live.parse_live_lines(
        [
            _event(1, "run.started", {"steps_total": 4}),
            _event(2, "run.progress", {"step_index": 2, "fraction": 0.5}),
            _event(3, "run.finished", {"status": "INCOMPLETE"}),
        ]
    )
    assert document["schema"] == "demo-live-v1", "the live document is the demo's own, not a kernel envelope"
    assert document["is_evidence"] is False
    assert document["active"] is False
    assert document["status"] == "INCOMPLETE", "the terminal status must be copied from run.finished"
    assert document["terminal"] == "event"
    assert document["step_index"] == 2


def test_live_fold_ignores_stale_and_malformed_lines() -> None:
    live = _live_module()
    document = live.parse_live_lines(
        [
            _event(2, "run.progress", {"step_index": 2, "fraction": 0.5}),
            _event(1, "run.progress", {"step_index": 1, "fraction": 0.1}),  # stale: cannot drag back
            "not json at all",
            "{}",
            _event(3, "run.finished", {"status": "RUN_OK"}),
        ]
    )
    assert document["step_index"] == 2, "a late stale record must not move progress backwards"
    assert document["fraction"] == 0.5
    assert document["skipped_lines"] == 2
    assert document["status"] == "RUN_OK"


def test_live_fold_never_invents_a_status() -> None:
    live = _live_module()
    document = live.parse_live_lines([_event(1, "run.started"), _event(2, "run.finished")])
    assert document["status"] == "finished_without_status", (
        "an event without a status must not be replaced by an inferred one"
    )
    assert document["active"] is False


# --- F. the double-click entry point stays trivial -----------------------------

@pytest.mark.parametrize("needle", (".venv\\Scripts\\python.exe", "if errorlevel 1 pause", "%~dp0"))
def test_demo_cmd_contains_the_double_click_contract(needle: str) -> None:
    text = _read_text(ROOT / "demo.cmd")
    assert needle in text, f"demo.cmd must contain {needle!r}; got:\n{text}"
