import type { TopologyDescriptor } from "../contracts";
import { topologySvg } from "../renderers/topology-svg";

const escapeHtml = (value: unknown) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

export class TopologyPanel extends HTMLElement {
  private descriptor?: TopologyDescriptor;
  set data(value: TopologyDescriptor) { this.descriptor = value; this.render(); }
  connectedCallback() { this.render(); }
  private render() {
    if (!this.descriptor) return;
    const { topology_id, descriptor_version, fingerprint, components, nets } = this.descriptor;
    this.innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">TOPOLOGY</span><h2>${escapeHtml(topology_id)}</h2></div><span class="badge">${components.length} components / ${nets.length} nets</span></div><div class="topology-meta">Descriptor ${escapeHtml(descriptor_version)} / fingerprint <code>${escapeHtml(fingerprint.slice(0, 12))}...</code></div><div class="topology-canvas">${topologySvg(this.descriptor)}</div></section>`;
  }
}
customElements.define("topology-panel", TopologyPanel);
