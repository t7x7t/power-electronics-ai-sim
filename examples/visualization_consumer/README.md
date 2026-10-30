# External Visualization Consumer Example

This example is a safe starting point for a user or AI Agent writing a custom
visualization frontend.  It contains two consumers with different dependency
rules:

- `verify_contracts.py` and `index.html` are standard library only and have no
  dependency on `pe_sim`; they read `topology.json`, `trace.json`, and
  `outputs.json` and display their common provenance and simple values.
- `launch.py` / `demo.html` / `demo.js` are a local read-only demo host.  They
  do import `pe_sim` (to scan the run catalog and start the read-only service),
  and they never become a second implementation of the runtime.

The browser workbench is temporarily withdrawn. First export the three
contracts from a published run, then validate them with the standard-library
script:

```powershell
pe-sim export-workbench ../../runs/ngspice-buck-reference ../../.tmp/visualization-demo
python verify_contracts.py --directory ../../.tmp/visualization-demo
```

Or read an explicitly started local visualization service:

```powershell
python verify_contracts.py --service http://127.0.0.1:8765 --run ngspice-buck-reference
```

For the browser example, serve this directory rather than opening the file
directly:

```powershell
python -m http.server 8000
```

Open `http://127.0.0.1:8000/?directory=../../.tmp/visualization-demo`.
Service mode is
`?service=http://127.0.0.1:8765&run=ngspice-buck-reference` and requires the
service to permit that browser origin through CORS.

Neither consumer writes run packages, loads third-party code, or treats
drawings as circuit truth.  The launcher writes nothing itself; when it is
given `--run-config` it *supervises* a separate `pe-sim run --progress` child
which owns the run, and folds that child's stderr events into an in-memory
`GET /live.json` marked `demo-live-v1` with `is_evidence: false` (deliberately
not a kernel envelope).  See `docs/26_stage11_consumer_and_service_v1.md`
(§2 and §5) and `docs/70_visualization_handover_v1.md` for the boundaries.

## Demo（本地只读）

- 怎么跑：双击 `demo.cmd`；或 `python examples/visualization_consumer/launch.py --runs-root runs`。
- 它做什么：起两个只绑 `127.0.0.1` 的回环服务器（只读服务 + 静态页），打开 `demo.html`；
  `demo.html` 用到的 `/catalog.json` 由启动器每次请求现算。
- 面板：`panels.json` 决定面板的数量、顺序、标题与依赖平面（标记 `demo-panels-v1`）；
  加自定义面板＝追加一条带 `entry` 的条目，宿主用动态 `import()` 加载并**逐格隔离**异常。
  校验：`python examples/visualization_consumer/verify_panels.py`。
- 实时：`launch.py --run-config <config.json>` 会监管一次 `pe-sim run --progress`，页面出现
  “实时”区块；未给该参数时 `/live.json` 返回 404，页面不显示该区块。CLI 的进度事件默认
  每 1 s 才发一次，很短的运行可能只有开始与结束两条事件。
- 它不做什么：不写 run 包、不提供任何写方法、不是证据、不是产品 UI。
- 回退方式：删除 `demo.html` `demo.js` `launch.py` `demo.cmd` `panels.json`
  `verify_panels.py` `assets/` 即完全移除。
