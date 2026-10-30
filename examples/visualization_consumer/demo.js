"use strict";
/*
 * 本文件只是消费者：不持有 Runner/Plant，不写 run 包，不保存或生成证据。
 * 它只读取同源 catalog.json 与只读服务的五个平面，把契约字段渲染成可审计的 DOM。
 * 所有渲染都是纯函数：没有随机数、没有时钟、没有动画、没有基于时间的 ID。
 */

const SERVICE = (new URLSearchParams(window.location.search).get("service") || "").replace(/\/+$/, "");
/* 面板清单是唯一事实来源；下面的内置描述只在清单缺失/损坏时保证宿主仍可启动。 */
const PANELS_FILE = "panels.json";
const PANELS_MARKER = "demo-panels-v1";
const PANEL_NEEDS = ["trace", "topology", "outputs", "parameters", "review"];
const PANEL_ID_RE = /^[a-z][a-z0-9-]{1,39}$/;
const PANEL_ENTRY_RE = /^[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*\.js$/;
const DEFAULT_PANELS = [
  { id: "provenance", title: "来源与身份", needs: ["trace"] },
  { id: "topology", title: "拓扑", needs: ["topology"] },
  { id: "trace", title: "波形", needs: ["trace"] },
  { id: "outputs", title: "结构化输出", needs: ["outputs"] },
  { id: "parameters", title: "参数视图", needs: ["parameters"] },
  { id: "review", title: "审阅", needs: ["review"] }
];
const GROUPS = [
  { name: "可引用", tone: "chip-ok", icon: "i-circle-check", rule: "published === true 且在 discovery 中，且 evidence_level ∈ {comparable, limited}" },
  { name: "仅探索", tone: "chip-warn", icon: "i-triangle-alert", rule: "published === true 且在 discovery 中，但 evidence_level 不属于 {comparable, limited}（diagnostic_only / functional）" },
  { name: "不可用", tone: "chip-bad", icon: "i-circle-x", rule: "published === false 或该 run 不在 discovery 列表中" }
];
const state = { runs: [], selected: null, token: 0, panels: [], panelSource: "" };

/* ---------------- 转义、格式化与小型纯函数 ---------------- */
const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (char) => ESCAPES[char]);
const dash = (value) => (value === null || value === undefined || value === "" ? "—" : value);
const firstDefined = (value, fallback) => (value === null || value === undefined ? fallback : value);
const failText = (reason) => (reason && reason.message ? reason.message : String(reason));
const shorten = (value, limit) => {
  const text = String(value === null || value === undefined ? "" : value);
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
};

function formatValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return String(value);
    if (value === 0) return "0";
    const magnitude = Math.abs(value);
    if (magnitude >= 1e4 || magnitude < 1e-3) return value.toExponential(3);
    return String(Number(value.toPrecision(4)));
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return value.map(formatValue).join(", ");
  if (typeof value === "object") { try { return JSON.stringify(value); } catch (error) { return String(value); } }
  return String(value);
}
/* 刻度：先取"整"步长，再按步长量级统一小数位。 */
const NICE_MANTISSAS = [1, 2, 2.5, 5, 10];
const MAX_TICKS = 8;
/* 判定"某值是否为步长整数倍"的相对容差：1e-9 × max(1, |min|, |max|)。
   让 1.9999999999999998 被视为 2.0，避免丢掉末端刻度。 */
const TICK_TOLERANCE = 1e-9;

function niceStep(rawStep) {
  if (!Number.isFinite(rawStep) || rawStep <= 0) return 1;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const limit = rawStep * (1 - 1e-12);
  for (const mantissa of NICE_MANTISSAS) {
    const candidate = mantissa * magnitude;
    if (candidate >= limit) return candidate;
  }
  return 10 * magnitude;
}

/* 下一档更粗的步长（1 → 2 → 2.5 → 5 → 10 → 20 …）。 */
function coarserStep(step) {
  const magnitude = Math.pow(10, Math.floor(Math.log10(step)));
  const scaled = step / magnitude;
  const index = NICE_MANTISSAS.findIndex((mantissa) => mantissa >= scaled * (1 - 1e-12));
  if (index < 0) return 10 * magnitude;
  return (index + 1 < NICE_MANTISSAS.length ? NICE_MANTISSAS[index + 1] : 10) * magnitude;
}

/* 在 [lo, hi] 上按给定步长生成刻度，边界取 ceil(lo/step)*step 到 floor(hi/step)*step。
   数量上限为 MAX_TICKS：超出时**加大步长**（不做线性抽稀），这样首尾刻度与网格对齐都保留。 */
function ticksInRange(lo, hi, step) {
  if (!(hi > lo)) return [lo, hi];
  const tolerance = TICK_TOLERANCE * Math.max(1, Math.abs(lo), Math.abs(hi));
  let current = step;
  let first = Math.ceil((lo - tolerance) / current) * current;
  let last = Math.floor((hi + tolerance) / current) * current;
  let total = Math.round((last - first) / current);
  for (let guard = 0; total > MAX_TICKS - 1 && guard < 32; guard += 1) {
    current = coarserStep(current);
    first = Math.ceil((lo - tolerance) / current) * current;
    last = Math.floor((hi + tolerance) / current) * current;
    total = Math.round((last - first) / current);
  }
  const values = [];
  for (let index = 0; index <= total; index += 1) {
    const value = Number((first + index * current).toPrecision(12));
    values.push(value === 0 ? 0 : value);
  }
  return values.length ? values : [lo, hi];
}

function ticksFor(min, max, targetCount) {
  const lo = Number.isFinite(min) ? min : 0;
  const hi = Number.isFinite(max) ? max : lo;
  const count = Number.isFinite(targetCount) && targetCount >= 1 ? Math.min(Math.floor(targetCount), MAX_TICKS) : 4;
  if (!(hi > lo)) return [lo, hi];
  return ticksInRange(lo, hi, niceStep((hi - lo) / count));
}

function tickDecimals(step) {
  const magnitude = Math.abs(step);
  if (!Number.isFinite(magnitude) || magnitude <= 0) return 2;
  if (magnitude >= 1 && Number.isInteger(magnitude)) return 0;
  for (let decimals = 0; decimals < 6; decimals += 1) {
    const scaled = magnitude * Math.pow(10, decimals);
    if (Math.abs(scaled - Math.round(scaled)) <= 1e-9 * Math.max(1, scaled)) return decimals;
  }
  return 6;
}

function formatTicks(values, step) {
  const decimals = tickDecimals(step);
  return values.map((value) => {
    const text = Number(value).toFixed(decimals);
    return /^-0(\.0+)?$/.test(text) ? text.slice(1) : text;
  });
}

function timeScale(maxSeconds) {
  if (maxSeconds >= 1) return { factor: 1, unit: "s" };
  if (maxSeconds >= 1e-3) return { factor: 1e3, unit: "ms" };
  if (maxSeconds >= 1e-6) return { factor: 1e6, unit: "µs" };
  return { factor: 1e9, unit: "ns" };
}

/* ---------------- 只读 GET 与错误信封 ---------------- */
function failureText(response, body, parsed) {
  const envelope = parsed && parsed.error && typeof parsed.error === "object" ? parsed.error : null;
  const code = envelope && envelope.code ? String(envelope.code) : "unknown_error";
  const message = envelope && envelope.message ? String(envelope.message) : (String(body || "").trim().slice(0, 160) || response.statusText || "空响应");
  return `HTTP ${response.status} — ${code}: ${message}`;
}
async function request(url, asJson) {
  let response;
  try {
    response = await fetch(url, { credentials: "omit" });
  } catch (error) {
    throw new Error(`请求失败 — ${error.message}`);
  }
  const body = await response.text();
  let parsed = null;
  if (body) { try { parsed = JSON.parse(body); } catch (error) { parsed = null; } }
  if (!response.ok) throw new Error(failureText(response, body, parsed));
  if (!asJson) return body;
  if (parsed === null) throw new Error(`HTTP ${response.status} — invalid_json: 响应不是 JSON`);
  return parsed;
}
/* ---------------- 分组：catalog 字段的确定性函数 ---------------- */
function classify(entry, member) {
  const published = entry.published === true;
  const memberText = member === null ? "未知（discovery 不可用）" : String(member);
  const basis = `published=${formatValue(entry.published)}；在 discovery 中=${memberText}`;
  if (!published || member !== true) return { group: GROUPS[2], basis };
  const level = entry.evidence_level;
  if (level === "comparable" || level === "limited") return { group: GROUPS[0], basis: `${basis}；evidence_level=${formatValue(level)}` };
  return { group: GROUPS[1], basis: `${basis}；evidence_level=${formatValue(level)}` };
}

/* ---------------- 左栏渲染 ---------------- */
function runLink(item) {
  const entry = item.entry;
  const meta = `${dash(entry.plant_id)} · ${dash(entry.status)} · qual ${dash(entry.qualification_level)} · evidence ${dash(entry.evidence_level)}`;
  return `<button type="button" class="run-link" data-run-id="${esc(entry.run_id)}" aria-current="${state.selected === entry.run_id ? "true" : "false"}" title="${esc(item.basis)}">` +
    `<span class="mono">${esc(entry.run_id)}</span> <span class="chip ${item.group.tone}">${esc(item.group.name)}</span><br>` +
    `<span class="ctx meta">${esc(meta)}</span></button>`;
}

function renderFleet() {
  const sections = GROUPS.map((group) => {
    const items = state.runs.filter((item) => item.group === group);
    return `<section class="rail-group"><h3 class="rail-title" title="${esc(group.rule)}"><svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#${group.icon}"></use></svg>${esc(group.name)} <span class="rail-count">${items.length}</span></h3>` +
      (items.length ? items.map(runLink).join("") : '<p class="empty-state">无</p>') + `</section>`;
  }).join("");
  document.getElementById("fleet-groups").innerHTML = sections;
}

function markSelected(runId) {
  state.selected = runId;
  document.querySelectorAll("#fleet-groups .run-link").forEach((button) => {
    button.setAttribute("aria-current", button.dataset.runId === runId ? "true" : "false");
  });
}

/* ---------------- 标题与状态 chip ---------------- */
function chip(tone, icon, text, title) {
  const glyph = icon ? `<svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#${icon}"></use></svg>` : "";
  return `<span class="chip ${tone}" title="${esc(title)}">${glyph}${esc(text)}</span>`;
}

function titleChips(item, trace, parameters) {
  const metadata = (trace && trace.provenance && trace.provenance.metadata) || {};
  const pack = (parameters && parameters.package) || {};
  const entry = item.entry;
  const traceNote = trace ? "" : "；trace 未读取";
  const integrity = metadata.integrity;
  const tree = firstDefined(metadata.working_tree_status, pack.working_tree_status);
  return [
    chip("chip-neutral", "i-database", `evidence ${formatValue(firstDefined(metadata.evidence_level, entry.evidence_level))}`, `来源：trace.provenance.metadata.evidence_level，回退 catalog.evidence_level${traceNote}`),
    chip("chip-neutral", "i-play", `run ${formatValue(firstDefined(metadata.run_status, entry.status))}`, `来源：trace.provenance.metadata.run_status，回退 catalog.status${traceNote}`),
    chip(integrity === "verified" ? "chip-ok" : integrity ? "chip-bad" : "chip-neutral", "i-shield-check", `integrity ${formatValue(integrity)}`, `来源：trace.provenance.metadata.integrity${traceNote}`),
    chip("chip-neutral", "i-layers", `qualification ${formatValue(entry.qualification_level)}`, "来源：catalog.qualification_level"),
    chip(tree === "clean" ? "chip-ok" : tree ? "chip-warn" : "chip-neutral", "i-file-text", `工作树 ${formatValue(tree)}`, `来源：trace.provenance.metadata.working_tree_status，回退 parameters.package.working_tree_status${traceNote}`)
  ].join(" ");
}

function renderTitle(item, trace, parameters) {
  document.getElementById("run-title").innerHTML = `<span class="mono">${esc(item.entry.run_id)}</span> ${titleChips(item, trace, parameters)}`;
}

/* ---------------- 面板工具与负例 ---------------- */
const panelIds = () => state.panels.map((panel) => panel.id);
const panelNode = (name) => document.getElementById(`panel-${name}`);
const setPanel = (name, html) => { const node = panelNode(name); if (node) node.innerHTML = html; };
const clearPanels = () => panelIds().forEach((name) => setPanel(name, ""));
const loadingPanels = () => panelIds().forEach((name) => setPanel(name, '<p class="empty-state">正在读取契约…</p>'));
const okValue = (result) => (result && result.status === "fulfilled" ? result.value : null);

/* ---------------- 受控面板扩展点：清单 → 挂载 → 逐格隔离渲染 ---------------- */
const RENDERERS = { provenance: provenancePanel, topology: topologyPanel, trace: tracePanel, outputs: outputsPanel, parameters: parametersPanel, review: reviewPanel };

/* 纯校验：不合规条目被丢弃并计数，宿主绝不因为一条坏数据而启动失败。 */
function normalizePanels(raw) {
  const list = raw && raw.marker === PANELS_MARKER && Array.isArray(raw.panels) ? raw.panels : [];
  const seen = new Set(), kept = [];
  let dropped = 0;
  list.forEach((panel) => {
    const id = panel && typeof panel.id === "string" ? panel.id : "";
    const needs = panel && Array.isArray(panel.needs) ? panel.needs : null;
    const entry = panel && panel.entry !== undefined ? panel.entry : null;
    const entryOk = entry === null || (typeof entry === "string" && PANEL_ENTRY_RE.test(entry) && !entry.includes(".."));
    if (!PANEL_ID_RE.test(id) || seen.has(id) || !needs || !needs.length || needs.some((need) => !PANEL_NEEDS.includes(need)) || !entryOk) { dropped += 1; return; }
    seen.add(id);
    kept.push({ id, title: typeof panel.title === "string" && panel.title ? panel.title : id, needs: needs.slice(), entry });
  });
  return { panels: kept, dropped };
}

async function loadPanels() {
  try {
    const normalized = normalizePanels(await request(PANELS_FILE, true));
    if (normalized.panels.length) {
      const note = normalized.dropped ? `（忽略 ${normalized.dropped} 条不合规）` : "";
      return { panels: normalized.panels, source: `${PANELS_FILE}：${normalized.panels.length} 条${note}` };
    }
    return { panels: DEFAULT_PANELS.slice(), source: `内置默认（${PANELS_FILE} 无有效面板）` };
  } catch (error) {
    return { panels: DEFAULT_PANELS.slice(), source: `内置默认（${PANELS_FILE} 不可用：${failText(error)}）` };
  }
}

/* 挂载：标题用 textContent，正文只有 id，不注入清单里的任何 HTML。 */
function mountPanels(panels) {
  state.panels = panels.slice();
  const rack = document.getElementById("panel-rack");
  if (!rack) return;
  rack.innerHTML = "";
  state.panels.forEach((panel) => {
    const section = document.createElement("section");
    section.className = "panel";
    section.dataset.panel = panel.id;
    const head = document.createElement("header");
    head.className = "panel-head";
    const title = document.createElement("h3");
    title.className = "panel-title";
    title.textContent = panel.title;
    head.appendChild(title);
    const body = document.createElement("div");
    body.className = "panel-body";
    body.id = `panel-${panel.id}`;
    section.append(head, body);
    rack.appendChild(section);
  });
}

const neededPlanes = () => PANEL_NEEDS.filter((plane) => state.panels.some((panel) => panel.needs.includes(plane)));

async function panelHtml(panel, results) {
  if (panel.entry) {
    const module = await import(`./${panel.entry}`);
    const render = module && (module.default || module.render);
    if (typeof render !== "function") throw new Error(`entry ${panel.entry} 没有可调用的默认导出`);
    return render({ id: panel.id, title: panel.title, needs: panel.needs.slice(), results: panel.needs.map((need) => results[need]) });
  }
  const render = RENDERERS[panel.id];
  if (typeof render !== "function") throw new Error(`面板 ${panel.id} 既无内置渲染器也无 entry`);
  return render(results[panel.needs[0]]);
}

/* 一格 try/catch：单面板异常只写自己的格子，晚到的旧结果被 token 丢弃。 */
async function renderPanel(panel, results, token) {
  const node = panelNode(panel.id);
  try {
    const rendered = await panelHtml(panel, results);
    if (token !== state.token || !node) return;
    if (rendered && typeof rendered === "object" && rendered.nodeType === 1) node.replaceChildren(rendered);
    else node.innerHTML = String(rendered);
  } catch (error) {
    if (token !== state.token || !node) return;
    node.innerHTML = errorBox(`面板 ${panel.id} 渲染失败（其它面板不受影响）：${failText(error)}`);
  }
}

const renderPanels = (results, token) => Promise.all(state.panels.map((panel) => renderPanel(panel, results, token)));
const errorBox = (message) => `<div class="error-state">${esc(message)}</div>`;

function showError(html) {
  const region = document.getElementById("error-region");
  region.innerHTML = html;
  region.hidden = false;
}

function hideError() {
  const region = document.getElementById("error-region");
  region.innerHTML = "";
  region.hidden = true;
}

function unavailableNotice(item) {
  const entry = item.entry;
  return `<svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#i-triangle-alert"></use></svg><strong>未通过发布/完整性门，服务不提供详情。</strong>` +
    `<p>判定依据：<span class="mono">published=${esc(formatValue(entry.published))}</span>、<span class="mono">在 discovery 中=${esc(item.membership)}</span>。` +
    `catalog status=<span class="mono">${esc(dash(entry.status))}</span>，run_state=<span class="mono">${esc(dash(entry.run_state))}</span>；这不是页面故障。</p>`;
}

/* ---------------- 六个面板的渲染 ---------------- */
function provenancePanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const provenance = result.value && result.value.provenance;
  if (!provenance || typeof provenance !== "object") return '<p class="empty-state">trace 契约没有 provenance 字段。</p>';
  const metadata = provenance.metadata || {};
  const rows = [
    ["run_id", provenance.run_id], ["producer", provenance.producer], ["source_commit", provenance.source_commit], ["manifest_sha256", provenance.manifest_sha256], ["package_sha256", provenance.package_sha256],
    ["metadata.integrity", metadata.integrity], ["metadata.run_status", metadata.run_status], ["metadata.evidence_level", metadata.evidence_level], ["metadata.run_mode", metadata.run_mode], ["metadata.working_tree_status", metadata.working_tree_status], ["metadata.derived_from", metadata.derived_from]
  ];
  /* R2（降级）：run 文档没有分析类型字段，所以只声明"未声明"。C1–C10 是
     docs/60 的工况编号，不是能力声明；覆盖状态未随包导出，因此不显示猜测值。 */
  const analysis = `<p class="empty-state">分析类型：未声明（run 文档无该字段）。工况编号 C1–C10 见 <span class="mono">docs/60</span>，其覆盖状态未随包导出。</p>`;
  return `<dl class="kv">` + rows.map(([label, value]) => `<dt>${esc(label)}</dt><dd class="mono">${esc(formatValue(value))}</dd>`).join("") + `</dl>` + analysis;
}

function topologySvg(components, nets) {
  const width = 860, rowHeight = 32, boxHeight = 24, pad = 18, leftWidth = 300, rightWidth = 230, leftX = 10, rightX = width - rightWidth - 10;
  const rows = Math.max(components.length, nets.length, 1);
  const height = rows * rowHeight + pad * 2;
  const startY = (count) => pad + ((rows - count) * rowHeight) / 2;
  const componentY = startY(components.length), netY = startY(nets.length);
  const netIndex = new Map(nets.map((net, index) => [net.net_id, index]));
  const edges = [];
  components.forEach((component, index) => {
    const seen = new Set();
    (Array.isArray(component.ports) ? component.ports : []).forEach((port) => {
      if (seen.has(port.net_id) || !netIndex.has(port.net_id)) return;
      seen.add(port.net_id);
      const y1 = (componentY + index * rowHeight + boxHeight / 2).toFixed(1);
      const y2 = (netY + netIndex.get(port.net_id) * rowHeight + boxHeight / 2).toFixed(1);
      edges.push(`<path d="M${leftX + leftWidth},${y1} C${leftX + leftWidth + 80},${y1} ${rightX - 80},${y2} ${rightX},${y2}" fill="none" stroke="var(--chart-axis)" stroke-width="1.1"><title>${esc(`${component.component_id}.${port.port_id} → ${port.net_id}`)}</title></path>`);
    });
  });
  const boxes = components.map((component, index) => {
    const y = componentY + index * rowHeight;
    const params = component.parameters && typeof component.parameters === "object" ? Object.keys(component.parameters).map((key) => `${key}=${formatValue(component.parameters[key])}`).join(", ") : "";
    const detail = [component.symbol_id ? `symbol_id=${component.symbol_id}` : "", params].filter(Boolean).join("；");
    return `<g><rect x="${leftX}" y="${y}" width="${leftWidth}" height="${boxHeight}" rx="4" fill="var(--pico-card-background-color)" stroke="var(--chart-axis)"/>` +
      `<text x="${leftX + 8}" y="${y + 16}" font-size="12" fill="var(--pico-color)">${esc(shorten(component.component_id, 36))}</text>` +
      `<text x="${leftX + leftWidth - 8}" y="${y + 16}" text-anchor="end" font-size="11" fill="var(--pico-muted-color)">${esc(shorten(dash(component.kind), 18))}</text>` +
      (detail ? `<title>${esc(detail)}</title>` : "") + `</g>`;
  }).join("");
  const netBoxes = nets.map((net, index) => {
    const y = netY + index * rowHeight;
    return `<g><rect x="${rightX}" y="${y}" width="${rightWidth}" height="${boxHeight}" rx="4" fill="var(--pico-card-background-color)" stroke="var(--chart-axis)"/>` +
      `<text x="${rightX + 8}" y="${y + 16}" font-size="12" fill="var(--pico-color)">${esc(shorten(net.net_id, 26))}</text>` +
      `<text x="${rightX + rightWidth - 8}" y="${y + 16}" text-anchor="end" font-size="11" fill="var(--pico-muted-color)">${esc(shorten(dash(net.kind), 16))}</text></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="组件与净的二分连接图">${edges.join("")}${boxes}${netBoxes}</svg>`;
}

function topologyPanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const topology = result.value || {};
  const components = Array.isArray(topology.components) ? topology.components : [];
  if (!components.length) return '<p class="empty-state">topology.components 为空，无法绘制组件–净二分图。</p>';
  const nets = Array.isArray(topology.nets) ? topology.nets : [];
  const declared = new Set(nets.map((net) => net.net_id));
  let dangling = 0;
  components.forEach((component) => (Array.isArray(component.ports) ? component.ports : []).forEach((port) => { if (!declared.has(port.net_id)) dangling += 1; }));
  return topologySvg(components, nets) +
    `<p><small>组件 ${components.length} 个 · 净 ${nets.length} 个 · fingerprint <span class="mono">${esc(dash(topology.fingerprint))}</span>。位置由本页固定规则排布，契约没有坐标。${dangling ? `另有 ${dangling} 个端口引用了未声明的净，未绘制连线。` : ""}</small></p>`;
}

function lineChart(samples, signalId, unitLabel, label) {
  const points = [];
  samples.forEach((sample) => {
    const time = sample && sample.time_s;
    const value = sample && sample.values ? sample.values[signalId] : undefined;
    if (typeof time === "number" && Number.isFinite(time) && typeof value === "number" && Number.isFinite(value)) points.push([time, value]);
  });
  if (points.length < 2) return '<p class="empty-state">有效采样点少于 2 个，不绘制曲线</p>';
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  points.forEach(([time, value]) => {
    minX = Math.min(minX, time); maxX = Math.max(maxX, time);
    minY = Math.min(minY, value); maxY = Math.max(maxY, value);
  });
  if (!(maxX > minX)) maxX = minX + 1e-9;
  const dataMinY = minY, dataMaxY = maxY;
  const span = maxY - minY;
  if (span < 1e-12) { minY -= 0.5; maxY += 0.5; } else { const margin = span * 0.08; minY -= margin; maxY += margin; }
  const width = 860, height = 210, left = 78, right = 18, top = 44, bottom = 36;
  const scale = timeScale(maxX);
  const x = (time) => left + ((time - minX) / (maxX - minX)) * (width - left - right);
  const y = (value) => height - bottom - ((value - minY) / (maxY - minY)) * (height - top - bottom);
  const path = points.map(([time, value], index) => `${index ? "L" : "M"}${x(time).toFixed(2)},${y(value).toFixed(2)}`).join(" ");
  const grid = [], ticks = [];
  /* 纵轴：步长按"数据范围 / 5"选（留白只决定刻度生成区间，不参与选步长），
     刻度在留白后的 [minY, maxY] 上生成，保证顶部刻度能覆盖曲线峰值。 */
  const yStep = niceStep((span < 1e-12 ? maxY - minY : dataMaxY - dataMinY) / 5);
  const yTicks = ticksInRange(minY, maxY, yStep);
  const yLabels = formatTicks(yTicks, yTicks.length > 1 ? yTicks[1] - yTicks[0] : 0);
  yTicks.forEach((value, index) => {
    const yPos = y(value);
    grid.push(`<line x1="${left}" y1="${yPos.toFixed(2)}" x2="${width - right}" y2="${yPos.toFixed(2)}" stroke="var(--chart-grid)" stroke-width="1"/>`);
    ticks.push(`<text x="${left - 8}" y="${(yPos + 4).toFixed(2)}" text-anchor="end" font-size="11" fill="var(--pico-muted-color)">${esc(yLabels[index])}</text>`);
  });
  const xTicks = ticksFor(minX * scale.factor, maxX * scale.factor, 5);
  const xLabels = formatTicks(xTicks, xTicks.length > 1 ? xTicks[1] - xTicks[0] : 0);
  xTicks.forEach((value, index) => {
    const time = value / scale.factor;
    ticks.push(`<text x="${x(time).toFixed(2)}" y="${height - 12}" text-anchor="middle" font-size="11" fill="var(--pico-muted-color)">${esc(xLabels[index])}</text>`);
  });
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="${esc(label)} 时间曲线">` +
    grid.join("") + ticks.join("") +
    `<line x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}" stroke="var(--chart-axis)" stroke-width="1"/>` +
    `<line x1="${left}" y1="${top}" x2="${left}" y2="${height - bottom}" stroke="var(--chart-axis)" stroke-width="1"/>` +
    `<text x="${left}" y="18" font-size="11" fill="var(--pico-muted-color)">纵轴单位：${esc(unitLabel)}</text>` +
    `<text x="${width - right}" y="18" text-anchor="end" font-size="11" fill="var(--pico-muted-color)">横轴：时间 / ${esc(scale.unit)}</text>` +
    `<path d="${path}" fill="none" stroke="var(--chart-line)" stroke-width="1.6"/></svg>`;
}

function tracePanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const trace = result.value || {};
  const signals = Array.isArray(trace.signals) ? trace.signals : [];
  const samples = Array.isArray(trace.samples) ? trace.samples : [];
  if (!signals.length) return '<p class="empty-state">trace 没有声明任何信号，无法绘图。</p>';
  const figures = signals.map((signal) => {
    let minimum = null, maximum = null, last = null;
    samples.forEach((sample) => {
      const value = sample && sample.values ? sample.values[signal.signal_id] : undefined;
      if (typeof value !== "number" || !Number.isFinite(value)) return;
      minimum = minimum === null ? value : Math.min(minimum, value);
      maximum = maximum === null ? value : Math.max(maximum, value);
      last = value;
    });
    const unit = signal.unit ? String(signal.unit) : null;
    const unitChip = unit
      ? `<span class="chip chip-neutral" title="来源：trace.signals[].unit">${esc(unit)}</span>`
      : '<span class="chip chip-warn" title="trace.signals[].unit 缺失，不猜测单位">单位未知</span>';
    const name = String(signal.name || signal.signal_id || "");
    return `<figure class="wave"><div class="wave-head"><span><svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#i-activity"></use></svg><strong>${esc(name)}</strong> <span class="mono">${esc(dash(signal.signal_id))}</span> ${unitChip}</span>` +
      `<span>min ${esc(formatValue(minimum))} · max ${esc(formatValue(maximum))} · last ${esc(formatValue(last))}</span></div>` +
      lineChart(samples, signal.signal_id, unit || "单位未知", name) + `</figure>`;
  }).join("");
  const policy = trace.sampling_policy ? JSON.stringify(trace.sampling_policy) : "—";
  return figures + `<p><small>采样点 ${samples.length} 个 · 信号 ${signals.length} 个 · 采样策略 <span class="mono">${esc(policy)}</span>。单位只取自 trace.signals[].unit；缺失时显示「单位未知」，不做猜测。</small></p>`;
}

function payloadSize(payload) {
  if (Array.isArray(payload)) return payload.length;
  if (payload && Array.isArray(payload.time_s)) return payload.time_s.length;
  if (payload && Array.isArray(payload.rows)) return payload.rows.length;
  return null;
}

function outputsPanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const outputs = result.value || {};
  const items = Array.isArray(outputs.outputs) ? outputs.outputs : [];
  if (!items.length) return '<p class="empty-state">outputs 契约没有输出项。</p>';
  const scalars = items.filter((item) => item.kind === "scalar");
  const others = items.filter((item) => item.kind !== "scalar");
  const table = scalars.length ? `<table><thead><tr><th>输出 ID</th><th>名称</th><th>值</th><th>单位</th><th>来源</th></tr></thead><tbody>` +
    scalars.map((item) => `<tr><td class="mono">${esc(item.output_id)}</td><td>${esc(dash(item.name))}</td><td class="num">${esc(formatValue(item.payload))}</td>` +
      `<td>${item.unit ? esc(item.unit) : '<span class="chip chip-warn" title="outputs[].unit 缺失">单位未知</span>'}</td><td>${esc(dash(item.source))}</td></tr>`).join("") +
    `</tbody></table>` : '<p class="empty-state">没有 scalar 输出。</p>';
  const listed = others.map((item) => `${esc(item.output_id)}（${esc(item.kind)}${payloadSize(item.payload) === null ? "" : `，${payloadSize(item.payload)} 行/点`}）`).join("、");
  const extra = others.length ? `<p><small>另有 ${others.length} 项非 scalar 输出：${listed}。本页只列摘要，不绘制。</small></p>` : "";
  return table + extra + `<p><small>outputs fingerprint：<span class="mono">${esc(dash(outputs.fingerprint))}</span></small></p>`;
}

function parameterUnit(unit) {
  return unit ? esc(unit) : '<span class="chip chip-warn" title="parameters[].unit 缺失">单位未知</span>';
}

function parametersPanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const view = result.value || {};
  const viewSet = view.parameter_set || {};
  const groups = Array.isArray(viewSet.groups) ? viewSet.groups : [];
  if (!groups.length) return '<p class="empty-state">parameter_set 没有参数组。</p>';
  const pack = view.package || {};
  const head = `<p><small>digest <span class="mono">${esc(dash(viewSet.digest))}</span> · manifest <span class="mono">${esc(dash(pack.manifest_sha256))}</span> · package <span class="mono">${esc(dash(pack.package_sha256))}</span> · source_commit <span class="mono">${esc(dash(pack.source_commit))}</span></small></p>`;
  const details = groups.map((group) => {
    const component = group.component || {};
    const items = Array.isArray(group.parameters) ? group.parameters : [];
    const rows = items.map((item) => `<tr><td class="mono">${esc(item.name)}</td><td class="num">${esc(formatValue(item.value))}</td><td>${parameterUnit(item.unit)}</td>` +
      `<td>${esc(dash(item.review_status))}</td><td>${esc(dash(item.provenance_source))}</td><td class="mono">${esc(formatValue(item.range))}</td></tr>`).join("");
    return `<details><summary>组 ${esc(dash(group.id))} · 组件 ${esc(dash(component.id))}（${esc(dash(component.kind))}，${esc(dash(component.level))}）· ${items.length} 个参数</summary>` +
      `<table><thead><tr><th>参数</th><th>值</th><th>单位</th><th>复核</th><th>参数来源</th><th>区间</th></tr></thead><tbody>${rows}</tbody></table></details>`;
  }).join("");
  const claim = view.claim && typeof view.claim === "object" ? view.claim : null;
  const claimKeys = claim ? Object.keys(claim) : [];
  const claimBlock = claimKeys.length ? `<details><summary>申报摘要（claim）</summary><dl class="kv">` +
    claimKeys.map((key) => `<dt>${esc(key)}</dt><dd class="mono">${esc(formatValue(claim[key]))}</dd>`).join("") + `</dl></details>` : "";
  return head + details + claimBlock + '<p><small>range 为 null 表示契约当前实现不提供区间，不是页面缺数据。</small></p>';
}

function reviewPanel(result) {
  if (result.status !== "fulfilled") return errorBox(failText(result.reason));
  const text = String(result.value || "");
  if (!text.trim()) return '<p class="empty-state">review 返回空文本。</p>';
  return `<pre>${esc(text)}</pre>`;
}

/* ---------------- 选择运行：五个平面各自失败、各自呈现 ---------------- */
async function selectRun(runId) {
  const item = state.runs.find((candidate) => candidate.entry.run_id === runId);
  if (!item) return;
  state.token += 1;
  const token = state.token;
  markSelected(runId);
  hideError();
  renderTitle(item, null, null);
  if (item.group === GROUPS[2]) {
    clearPanels();
    showError(unavailableNotice(item));
    return;
  }
  loadingPanels();
  const base = `${SERVICE}/v1/runs/${encodeURIComponent(runId)}`;
  const PLANS = {
    topology: [`${base}/topology`, true], trace: [`${base}/trace`, true],
    outputs: [`${base}/outputs`, true], parameters: [`${base}/parameters`, true],
    review: [`${base}/review`, false]
  };
  const planes = neededPlanes();
  const settled = await Promise.allSettled(planes.map((plane) => request(PLANS[plane][0], PLANS[plane][1])));
  if (token !== state.token) return;
  const results = {};
  planes.forEach((plane, index) => { results[plane] = settled[index]; });
  renderTitle(item, okValue(results.trace), okValue(results.parameters));
  await renderPanels(results, token);
}

/* ---------------- 启动：负例在入口处显式呈现 ---------------- */
function setStatus(text) {
  document.getElementById("app-bar-status").textContent = text;
}

function showUsage() {
  showError(`<svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#i-triangle-alert"></use></svg>` +
    `<strong>缺少 ?service 参数。</strong>` +
    `<p>本页只读取只读服务：先运行 <span class="mono">python examples/visualization_consumer/launch.py</span>，再用它输出的 <span class="mono">?service=…</span> 链接打开。未发起任何请求。</p>`);
  setStatus("未指定服务地址");
  document.getElementById("fleet-groups").innerHTML = '<p class="empty-state">未连接服务，运行清单不可用。</p>';
}

async function bootstrap() {
  const manifest = await loadPanels();
  mountPanels(manifest.panels);
  state.panelSource = manifest.source;
  if (!SERVICE) { showUsage(); return; }
  setStatus(`服务 ${SERVICE} · 面板 ${state.panels.length}（${state.panelSource}）· 正在读取 catalog 与 discovery…`);
  const [catalogResult, discoveryResult] = await Promise.allSettled([
    request("catalog.json", true),
    request(`${SERVICE}/v1/discovery`, true)
  ]);
  if (catalogResult.status !== "fulfilled") {
    document.getElementById("fleet-groups").innerHTML = `<p class="empty-state">catalog.json 载入失败：${esc(failText(catalogResult.reason))}。catalog.root 未知（catalog 未载入）。</p>`;
    setStatus(`catalog.json 载入失败 · 服务 ${SERVICE}`);
    return;
  }
  const catalog = catalogResult.value || {};
  const runs = Array.isArray(catalog.runs) ? catalog.runs : [];
  if (!runs.length) {
    document.getElementById("fleet-groups").innerHTML = `<p class="empty-state">该 runs root 下没有 run；catalog.root：<span class="mono">${esc(dash(catalog.root))}</span>。</p>`;
    setStatus(`runs root ${catalog.root || "—"} · catalog 0 条 · 服务 ${SERVICE}`);
    return;
  }
  const discovery = discoveryResult.status === "fulfilled" ? discoveryResult.value : null;
  const members = discovery && Array.isArray(discovery.runs) ? new Set(discovery.runs.map((run) => run.run_id)) : null;
  state.runs = runs.map((entry) => {
    const member = members ? members.has(entry.run_id) : null;
    const info = classify(entry, member);
    return {
      entry,
      group: info.group,
      basis: info.basis,
      membership: member === null ? "未知（discovery 不可用）" : String(member)
    };
  });
  renderFleet();
  setStatus(`runs root ${catalog.root || "—"} · catalog ${dash(catalog.count)} 条 · discovery ${members ? members.size : "未知"} 条 · 服务 ${SERVICE}`);
  if (members === null) {
    showError(`<svg class="chip-icon" aria-hidden="true"><use href="assets/icons.svg#i-triangle-alert"></use></svg>` +
      `<strong>discovery 请求失败，无法确认 run 是否在 discovery 中。</strong>` +
      `<p>${esc(failText(discoveryResult.reason))}。运行清单已全部按「不可用」展示，详情不提供；这是诚实降级，不是页面故障。</p>`);
    return;
  }
  const first = state.runs.find((item) => item.group !== GROUPS[2]);
  if (first) selectRun(first.entry.run_id);
}

document.getElementById("fleet-groups").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-run-id]");
  if (button) selectRun(button.dataset.runId);
});

/* ---------------- 实时块：只在监管中显示；终态来自事件，不推测 ---------------- */
const LIVE_ENABLED = new URLSearchParams(window.location.search).get("live") === "1";

function liveHtml(doc) {
  const pct = typeof doc.fraction === "number" ? Math.round(doc.fraction * 1000) / 10 : null;
  const settled = doc.active === false && (doc.terminal !== null || doc.status !== null);
  const head = doc.active
    ? `<strong>正在运行</strong> · 步骤 ${esc(dash(doc.step_index))}${pct === null ? "" : ` · ${pct}%`}`
    : settled
      ? `<strong>已结束</strong> · 状态 ${esc(dash(doc.status || doc.terminal))}${doc.terminal === "process_exit" ? "（进程退出，未收到 run.finished）" : ""}`
      : `<strong>等待第一个事件</strong>${doc.step_index === null ? "（CLI 默认每 1 s 才发一次进度，很短的运行可能只有开始与结束两条）" : ""}`;
  const bar = doc.active && pct !== null
    ? `<div class="live-bar"><span style="width:${Math.min(100, Math.max(0, pct))}%"></span></div>`
    : "";
  return `<p class="live-note">实时视图来自子进程 stderr 事件；<strong>未发布，不是证据</strong>。</p><p>${head}</p>${bar}`;
}

async function pollLive() {
  if (!LIVE_ENABLED) return;
  const region = document.getElementById("live-region");
  if (!region) return;
  try {
    const doc = await request("live.json", true);
    region.innerHTML = liveHtml(doc || {});
    region.hidden = false;
    const settled = doc && doc.active === false && (doc.terminal !== null || doc.status !== null);
    if (!settled) setTimeout(pollLive, doc && doc.active ? 1000 : 2000);
  } catch (error) {
    /* 未监管（404）或不可用：整块隐藏，绝不伪造一个状态。 */
    region.hidden = true;
  }
}

bootstrap();
pollLive();
