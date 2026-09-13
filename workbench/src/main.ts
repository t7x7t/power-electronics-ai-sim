import "./style.css";
import "./components/topology-panel";
import "./components/waveform-panel";
import "./components/output-panel";
import { loadWorkbenchData, selectedDataLabel } from "./adapters/fixture-loader";
import type { WorkbenchFixture } from "./adapters/fixture-loader";

const app = document.querySelector<HTMLDivElement>("#app")!;
app.innerHTML = `<header class="app-header"><div><span class="eyebrow">POWER ELECTRONICS / DATA VIEW</span><h1>PE Sim Workbench</h1><p>Minimal read-only consumer for topology, trace, and output contracts.</p></div><div class="status" id="status">Loading fixture...</div></header><div class="error-banner" id="error" hidden></div><div class="dashboard" id="dashboard"></div>`;

function render(fixture: WorkbenchFixture) {
  const dashboard = document.querySelector<HTMLDivElement>("#dashboard")!;
  const topology = document.createElement("topology-panel") as HTMLElement & { data: WorkbenchFixture["topology"] };
  const waveform = document.createElement("waveform-panel") as HTMLElement & { data: WorkbenchFixture["trace"] };
  const output = document.createElement("output-panel") as HTMLElement & { data: WorkbenchFixture["outputs"] };
  topology.data = fixture.topology; waveform.data = fixture.trace; output.data = fixture.outputs;
  dashboard.append(topology, waveform, output);
  const status = document.querySelector<HTMLDivElement>("#status")!;
  const metadata = fixture.trace.provenance.metadata ?? {};
  const runStatus = typeof metadata.run_status === "string" ? ` / ${metadata.run_status}` : "";
  const runMode = typeof metadata.run_mode === "string" ? ` / ${metadata.run_mode}` : "";
  const worktree = typeof metadata.working_tree_status === "string" ? ` / worktree ${metadata.working_tree_status}` : "";
  const integrity = metadata.integrity === "verified" ? " / integrity verified" : "";
  status.textContent = `${selectedDataLabel()} / ${fixture.trace.provenance.run_id ?? "exploratory"}${runStatus}${runMode}${worktree}${integrity}`;
  status.classList.add("ready");
}

loadWorkbenchData().then(render).catch((error: unknown) => {
  const banner = document.querySelector<HTMLDivElement>("#error")!;
  banner.hidden = false;
  banner.textContent = `Visualization data load failed: ${error instanceof Error ? error.message : String(error)}`;
  document.querySelector<HTMLDivElement>("#status")!.textContent = "Unavailable";
});
