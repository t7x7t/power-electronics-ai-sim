import type { OutputDataset, TopologyDescriptor, TraceDataset } from "../contracts";

export interface WorkbenchFixture {
  topology: TopologyDescriptor;
  trace: TraceDataset;
  outputs: OutputDataset;
}

export type FixtureName = "run-demo" | "reference";
export type DataSource = "fixture" | "service";

const RUN_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export interface ServiceSource {
  baseUrl: string;
  runId: string;
}

/** The run demo is derived offline; this browser only reads its public contracts. */
export function selectedFixture(search = window.location.search): FixtureName {
  return new URLSearchParams(search).get("fixture") === "reference" ? "reference" : "run-demo";
}

/** A service source is opt-in; every other URL continues to use public fixtures. */
export function selectedDataSource(search = window.location.search): DataSource {
  return new URLSearchParams(search).get("source") === "service" ? "service" : "fixture";
}

/**
 * The workbench is deliberately a local, read-only consumer.  Restricting the
 * service to loopback HTTP(S) prevents a shared link from making the browser
 * send run metadata to an arbitrary host.  The base URL has no path, query,
 * fragment, or credentials because the endpoint path is fixed below.
 */
export function parseServiceSource(search = window.location.search): ServiceSource {
  const params = new URLSearchParams(search);
  const rawService = params.get("service");
  const runId = params.get("run");
  if (!rawService) throw new Error("Service source requires a service URL parameter.");
  if (!runId) throw new Error("Service source requires a run parameter.");
  if (!RUN_ID_PATTERN.test(runId)) {
    throw new Error("Invalid run identifier. Use 1-128 letters, digits, '.', '_' or '-'.");
  }

  let url: URL;
  try {
    url = new URL(rawService);
  } catch {
    throw new Error("Invalid service URL. Use an absolute loopback HTTP(S) URL.");
  }
  const loopbackHosts = new Set(["localhost", "127.0.0.1", "[::1]"]);
  if (!(["http:", "https:"].includes(url.protocol)) || !loopbackHosts.has(url.hostname) ||
      url.username || url.password || (url.pathname !== "/" && url.pathname !== "") ||
      url.search || url.hash) {
    throw new Error("Service URL must be a credential-free loopback HTTP(S) origin, for example http://127.0.0.1:8765.");
  }
  return { baseUrl: url.origin, runId };
}

export function selectedDataLabel(search = window.location.search): string {
  if (selectedDataSource(search) === "fixture") return selectedFixture(search);
  const source = parseServiceSource(search);
  return `service ${source.runId}`;
}

/** Loads only renderer-neutral JSON contracts. No Runner or Plant object is inspected. */
export async function loadFixture(fixture = selectedFixture()): Promise<WorkbenchFixture> {
  const base = fixture === "reference" ? "/fixtures" : `/fixtures/${fixture}`;
  const load = async <T>(name: string): Promise<T> => {
    const response = await fetch(`${base}/${name}.json`);
    if (!response.ok) throw new Error(`Unable to load ${name}.json (${response.status})`);
    return response.json() as Promise<T>;
  };
  const [topology, trace, outputs] = await Promise.all([
    load<TopologyDescriptor>("topology"),
    load<TraceDataset>("trace"),
    load<OutputDataset>("outputs"),
  ]);
  return { topology, trace, outputs };
}

/**
 * Fetches exactly the three public visualization contracts.  No endpoint for
 * Runner, Plant, Controller, or mutable run-package operations is exposed.
 */
export async function loadWorkbenchData(search = window.location.search): Promise<WorkbenchFixture> {
  if (selectedDataSource(search) === "fixture") return loadFixture(selectedFixture(search));

  const { baseUrl, runId } = parseServiceSource(search);
  const load = async <T>(resource: "topology" | "trace" | "outputs"): Promise<T> => {
    const response = await fetch(`${baseUrl}/v1/runs/${encodeURIComponent(runId)}/${resource}`, {
      method: "GET",
      credentials: "omit",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Service could not load ${resource} (${response.status}).`);
    return response.json() as Promise<T>;
  };
  const [topology, trace, outputs] = await Promise.all([
    load<TopologyDescriptor>("topology"),
    load<TraceDataset>("trace"),
    load<OutputDataset>("outputs"),
  ]);
  return { topology, trace, outputs };
}
