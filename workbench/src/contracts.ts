export interface Provenance {
  run_id?: string;
  manifest_sha256?: string;
  package_sha256?: string;
  source_commit?: string;
  producer?: string;
  metadata?: Record<string, unknown>;
}

export interface TopologyPort {
  port_id: string;
  net_id: string;
  direction: string;
  label?: string;
}

export interface TopologyComponent {
  component_id: string;
  kind: string;
  ports: TopologyPort[];
  parameters: Record<string, unknown>;
  symbol_id?: string;
  model_id?: string;
}

export interface TopologyNet {
  net_id: string;
  label?: string;
  kind: string;
}

export interface TopologyDescriptor {
  schema: string;
  descriptor_version: string;
  topology_id: string;
  components: TopologyComponent[];
  nets: TopologyNet[];
  metadata: Record<string, unknown>;
  fingerprint: string;
}

export interface SignalDefinition {
  signal_id: string;
  name: string;
  unit: string;
  dtype: string;
  source: string;
  metadata?: Record<string, unknown>;
}

export interface TraceSample {
  time_s: number;
  values: Record<string, number | string | boolean>;
}

export interface TraceDataset {
  schema_version: string;
  signals: SignalDefinition[];
  samples: TraceSample[];
  sampling_policy: { mode: string; include_initial?: boolean; [key: string]: unknown };
  provenance: Provenance;
  metadata: Record<string, unknown>;
}

export interface OutputFieldDefinition {
  field_id: string;
  name: string;
  unit?: string;
  dtype: string;
}

export interface OutputEntry {
  output_id: string;
  name: string;
  kind: "scalar" | "series" | "table";
  source: string;
  definition_version: string;
  unit?: string;
  dtype?: string;
  fields?: OutputFieldDefinition[];
  payload: unknown;
}

export interface OutputDataset {
  schema_version: string;
  outputs: OutputEntry[];
  provenance: Provenance;
  metadata: Record<string, unknown>;
  fingerprint: string;
}
