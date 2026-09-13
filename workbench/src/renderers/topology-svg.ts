import type { TopologyDescriptor } from "../contracts";

const esc = (value: string) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");

/** Deterministic, read-only SVG view. Layout is presentation only; nets remain descriptor truth. */
export function topologySvg(descriptor: TopologyDescriptor): string {
  const width = 960;
  const rowHeight = 92;
  const height = Math.max(220, descriptor.components.length * rowHeight + 48);
  const netIndex = new Map(descriptor.nets.map((net, index) => [net.net_id, index]));
  const netX = (netId: string) => 250 + ((netIndex.get(netId) ?? 0) % 5) * 145;
  const componentMarkup = descriptor.components.map((component, index) => {
    const y = 28 + index * rowHeight;
    const ports = component.ports.map((port, portIndex) => {
      const x = netX(port.net_id);
      const py = y + 26 + portIndex * 18;
      return `<line x1="${x}" y1="${py}" x2="${x - 34}" y2="${py}" class="wire"/><circle cx="${x}" cy="${py}" r="4" class="port"/><text x="${x + 8}" y="${py + 4}" class="port-label">${esc(port.port_id)} / ${esc(port.net_id)}</text>`;
    }).join("");
    const parameterText = Object.entries(component.parameters ?? {}).map(([key, value]) => `${key}=${String(value)}`).join("  ");
    return `<g class="component"><rect x="24" y="${y}" width="210" height="68" rx="6"/><text x="38" y="${y + 23}" class="component-title">${esc(component.component_id)}</text><text x="38" y="${y + 42}" class="component-kind">${esc(component.kind)}${component.symbol_id ? ` / ${esc(component.symbol_id)}` : ""}</text><text x="38" y="${y + 58}" class="component-params">${esc(parameterText || "no parameters")}</text>${ports}</g>`;
  }).join("");
  const netMarkup = descriptor.nets.map((net) => `<g><line x1="${netX(net.net_id)}" y1="16" x2="${netX(net.net_id)}" y2="${height - 20}" class="net-line"/><text x="${netX(net.net_id) + 7}" y="${height - 26}" class="net-label">${esc(net.label ?? net.net_id)}</text></g>`).join("");
  return `<svg class="topology-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(descriptor.topology_id)} topology"><rect width="100%" height="100%" class="canvas"/>${netMarkup}${componentMarkup}</svg>`;
}
