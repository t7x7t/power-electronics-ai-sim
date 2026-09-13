import type { OutputDataset, OutputEntry } from "../contracts";

const escapeHtml = (value: unknown) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

export class OutputPanel extends HTMLElement {
  private dataSet?: OutputDataset;
  set data(value: OutputDataset) { this.dataSet = value; this.render(); }
  connectedCallback() { this.render(); }
  private render() {
    const data = this.dataSet; if (!data) return;
    this.innerHTML = `<section class="panel output-panel"><div class="panel-heading"><div><span class="eyebrow">OUTPUTS</span><h2>Results</h2></div><span class="badge">${data.outputs.length} published</span></div><div class="output-grid">${data.outputs.map((entry) => this.renderEntry(entry)).join("")}</div></section>`;
  }
  private renderEntry(entry: OutputEntry): string {
    if (entry.kind === "scalar") return `<article class="output-card"><div class="output-title">${escapeHtml(entry.name)}<small>${escapeHtml(entry.output_id)}</small></div><strong class="scalar-value">${escapeHtml(entry.payload)} <small>${escapeHtml(entry.unit ?? "")}</small></strong><span class="source">${escapeHtml(entry.source)} / v${escapeHtml(entry.definition_version)}</span></article>`;
    if (entry.kind === "series") {
      const payload = entry.payload as { time_s: number[]; values: number[] };
      const preview = payload.values.slice(-1)[0];
      const finite = payload.values.filter(Number.isFinite);
      const min = finite.length ? Math.min(...finite) : 0;
      const max = finite.length ? Math.max(...finite) : 1;
      const span = max - min || 1;
      const points = payload.values.map((value, index) => {
        const x = payload.values.length <= 1 ? 50 : (index / (payload.values.length - 1)) * 100;
        const y = 92 - ((Number(value) - min) / span) * 84;
        return Number.isFinite(Number(value)) ? `${x.toFixed(2)},${y.toFixed(2)}` : "";
      }).filter(Boolean).join(" ");
      return `<article class="output-card"><div class="output-title">${escapeHtml(entry.name)}<small>${escapeHtml(entry.output_id)}</small></div><svg class="output-sparkline" viewBox="0 0 100 100" role="img" aria-label="${escapeHtml(entry.name)} series preview"><polyline points="${points}" /></svg><strong class="series-value">${escapeHtml(preview ?? "--")} <small>${escapeHtml(entry.unit ?? "")}</small></strong><span class="source">Series / ${payload.values.length} points / last sample</span></article>`;
    }
    const payload = entry.payload as { rows: Record<string, unknown>[] }; const fields = entry.fields ?? []; return `<article class="output-card table-card"><div class="output-title">${escapeHtml(entry.name)}<small>${escapeHtml(entry.output_id)}</small></div><div class="table-scroll"><table><thead><tr>${fields.map((field) => `<th>${escapeHtml(field.name)}<small>${escapeHtml(field.unit ?? "")}</small></th>`).join("")}</tr></thead><tbody>${payload.rows.slice(0, 8).map((row) => `<tr>${fields.map((field) => `<td>${escapeHtml(row[field.field_id] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div><span class="source">Table / ${payload.rows.length} rows</span></article>`;
  }
}
customElements.define("output-panel", OutputPanel);
