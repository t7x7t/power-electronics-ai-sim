import type { TraceDataset } from "../contracts";

const palette = ["#f97316", "#22c55e", "#38bdf8", "#e879f9", "#facc15"];
const fmt = (value: number) => Number.isFinite(value) ? value.toPrecision(5) : "--";
const escapeHtml = (value: unknown) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

export class WaveformPanel extends HTMLElement {
  private dataSet?: TraceDataset;
  private selected = new Set<string>();
  private zoom = 1;
  private cursorIndex = -1;
  set data(value: TraceDataset) { this.dataSet = value; this.selected = new Set(value.signals.slice(0, 2).map((s) => s.signal_id)); this.render(); }
  connectedCallback() { this.render(); }
  private render() {
    const data = this.dataSet;
    if (!data) return;
    const controls = data.signals.map((signal, index) => `<label class="signal-toggle"><input type="checkbox" data-signal="${escapeHtml(signal.signal_id)}" ${this.selected.has(signal.signal_id) ? "checked" : ""}/><span class="swatch" style="background:${palette[index % palette.length]}"></span>${escapeHtml(signal.name)} <small>${escapeHtml(signal.unit)}</small></label>`).join("");
    this.innerHTML = `<section class="panel waveform-panel"><div class="panel-heading"><div><span class="eyebrow">WAVEFORMS</span><h2>Signals</h2></div><span class="badge">${data.samples.length} samples / ${escapeHtml(data.sampling_policy.mode)}</span></div><div class="wave-controls"><div class="signal-list">${controls}</div><div class="zoom-tools"><button type="button" data-zoom="out" aria-label="Zoom out" title="Zoom out">-</button><button type="button" data-zoom="reset" title="Reset zoom">${Math.round(this.zoom * 100)}%</button><button type="button" data-zoom="in" aria-label="Zoom in" title="Zoom in">+</button></div></div><div class="chart-wrap"><canvas class="wave-canvas" height="270"></canvas><div class="cursor-readout">Click the plot to inspect a sample</div></div><div class="wave-legend">${data.provenance.run_id ? `Run <code>${escapeHtml(data.provenance.run_id)}</code>` : "Exploratory dataset"} / units are defined by the signal registry</div></section>`;
    this.querySelectorAll<HTMLInputElement>("input[data-signal]").forEach((input) => input.addEventListener("change", () => { if (input.checked) this.selected.add(input.dataset.signal!); else this.selected.delete(input.dataset.signal!); this.draw(); }));
    this.querySelectorAll<HTMLButtonElement>("button[data-zoom]").forEach((button) => button.addEventListener("click", () => { const mode = button.dataset.zoom; this.zoom = mode === "in" ? Math.min(8, this.zoom * 1.5) : mode === "out" ? Math.max(0.5, this.zoom / 1.5) : 1; this.render(); }));
    this.querySelector("canvas")?.addEventListener("click", (event) => { const canvas = event.currentTarget as HTMLCanvasElement; const rect = canvas.getBoundingClientRect(); const visible = Math.max(1, Math.round(data.samples.length / this.zoom)); const start = Math.max(0, data.samples.length - visible); this.cursorIndex = Math.min(data.samples.length - 1, start + Math.round(((event.clientX - rect.left) / rect.width) * (visible - 1))); this.draw(); });
    this.draw();
  }
  private draw() {
    const data = this.dataSet; const canvas = this.querySelector<HTMLCanvasElement>("canvas"); if (!data || !canvas) return;
    const ratio = window.devicePixelRatio || 1; const width = Math.max(300, canvas.clientWidth || 800); const height = 270; canvas.width = width * ratio; canvas.height = height * ratio; const ctx = canvas.getContext("2d"); if (!ctx) return; ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#111827"; ctx.fillRect(0, 0, width, height); ctx.strokeStyle = "#25324a"; ctx.lineWidth = 1; for (let y = 24; y < height; y += 42) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
    const visible = Math.max(1, Math.round(data.samples.length / this.zoom)); const start = Math.max(0, data.samples.length - visible); const values = [...this.selected].map((id) => data.samples.slice(start).map((sample) => Number(sample.values[id])).filter(Number.isFinite)); const all = values.flat(); const min = all.length ? Math.min(...all) : 0; const max = all.length ? Math.max(...all) : 1; const span = max - min || 1;
    [...this.selected].forEach((id, signalIndex) => { const points = data.samples.slice(start); ctx.strokeStyle = palette[data.signals.findIndex((s) => s.signal_id === id) % palette.length] || palette[signalIndex % palette.length]; ctx.lineWidth = 2; ctx.beginPath(); let began = false; points.forEach((sample, index) => { const value = Number(sample.values[id]); if (!Number.isFinite(value)) { began = false; return; } const x = points.length === 1 ? width / 2 : (index / (points.length - 1)) * width; const y = height - 20 - ((value - min) / span) * (height - 42); if (!began) { ctx.moveTo(x, y); began = true; } else ctx.lineTo(x, y); }); ctx.stroke(); });
    if (this.cursorIndex >= start && this.cursorIndex < data.samples.length) { const x = (this.cursorIndex - start) / Math.max(1, visible - 1) * width; ctx.strokeStyle = "#f8fafc"; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); ctx.setLineDash([]); const sample = data.samples[this.cursorIndex]; const details = [...this.selected].map((id) => { const signal = data.signals.find((s) => s.signal_id === id)!; return `${signal.name}: ${fmt(Number(sample.values[id]))} ${signal.unit}`; }).join(" / "); this.querySelector(".cursor-readout")!.textContent = `t = ${sample.time_s.toPrecision(5)} s | ${details}`; }
  }
}
customElements.define("waveform-panel", WaveformPanel);
