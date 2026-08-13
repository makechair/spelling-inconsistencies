// Cross-sectional fundamentals table and a per-symbol history view.
//
// The API serves a file the metrics job prepared, so there is no computation
// here beyond formatting and sorting: any number shown came from the XBRL
// facts unchanged.

import { subsectorLabel } from "/static/subsectors.js";

const PERCENT = new Intl.NumberFormat("ja-JP", {
  style: "percent",
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});
const POINTS = new Intl.NumberFormat("ja-JP", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
  signDisplay: "exceptZero",
});
const DAYS = new Intl.NumberFormat("ja-JP", { maximumFractionDigits: 0 });

// Columns whose delta is an improvement when it falls rather than rises.
const LOWER_IS_BETTER = new Set(["inventory_days"]);

// The roster is organised by the user's lists; market is one filter within a
// list rather than the top-level split. Revenue is never currency-converted,
// so a view that mixes markets cannot be read by size -- the page says so
// instead of preventing it.
const MARKET_LABELS = { US: "米国", JP: "日本" };
const marketLabel = (row) => MARKET_LABELS[marketOf(row)] ?? "";
const ALL_LISTS = "";
// Why a row has no price ratios. Saying which of the three it is turns an
// empty cell into a fact about the filer.
const WITHHELD_REASONS = {
  adr_share_ratio_unknown: "ADRの原株比率が不明",
  reporting_currency_not_usd: "計上通貨が米ドルではない",
  no_price_or_share_count: "株価または株式数が無い",
};

let rows = [];
let lists = [];
let registered = [];
let maxLists = 20;
let activeList = ALL_LISTS;
// Editing shows every symbol with its membership, not just the members: you
// cannot add to a list from a view that hides everything not already in it.
let editing = false;
let sortKey = "revenue";
let sortDescending = true;

function money(value, currency) {
  if (value == null) return "";
  const scaled = value / 1e9;
  const formatted = new Intl.NumberFormat("ja-JP", {
    maximumFractionDigits: scaled >= 100 ? 0 : 2,
  }).format(scaled);
  // The unit is part of the number: figures are never converted, so a bare
  // amount could silently be compared against a different currency.
  return `${formatted}B ${currency ?? ""}`.trim();
}

function delta(value, key) {
  if (value == null) return { text: "", tone: "" };
  const better = LOWER_IS_BETTER.has(key) ? value < 0 : value > 0;
  const text = key === "inventory_days"
    ? `${POINTS.format(value)}日`
    : `${POINTS.format(value * 100)}pt`;
  if (value === 0) return { text, tone: "" };
  return { text, tone: better ? "up" : "down" };
}

function cell(main, change, key) {
  const td = document.createElement("td");
  td.className = "numeric";
  const primary = document.createElement("div");
  primary.textContent = main;
  td.append(primary);
  if (change !== undefined) {
    const { text, tone } = delta(change, key);
    if (text) {
      const secondary = document.createElement("div");
      secondary.className = `fundamentals-delta ${tone}`;
      secondary.textContent = text;
      td.append(secondary);
    }
  }
  return td;
}

function percent(value) {
  return value == null ? "" : PERCENT.format(value);
}

const MULTIPLE = new Intl.NumberFormat("ja-JP", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

function multiple(value) {
  return value == null ? "" : `${MULTIPLE.format(value)}倍`;
}

// An EDINET security code: three digits and a fourth character that has been
// allowed to be a letter since 2024 (130A and the like). No US ticker takes
// that shape, so the symbol alone settles the market.
const JP_CODE = /^\d{3}[0-9A-Z]$/;

function marketOf(row) {
  // The metrics job labels each row from the universe file it came from, and
  // that label wins. It is not required, though: a summary.json written
  // before the label existed still separates correctly, so the page works the
  // moment it is deployed rather than waiting for the next metrics run.
  if (row.market) return row.market;
  return JP_CODE.test(String(row.symbol ?? "")) ? "JP" : "US";
}

function inMarket(row) {
  const market = document.getElementById("market-filter").value;
  return !market || marketOf(row) === market;
}

function currentList() {
  return lists.find((entry) => entry.id === activeList) ?? null;
}

function members() {
  return new Set(currentList()?.symbols ?? []);
}

function inList(row) {
  const list = currentList();
  // Editing deliberately ignores membership: the checkbox is how a symbol
  // gets in, so the rows have to be reachable before they are members.
  if (!list || editing) return true;
  return list.symbols.includes(row.symbol);
}

// The screen. Each row is a field, a direction and the unit the value is
// entered in -- percentages are typed as percentages and stored as fractions,
// because nobody screens for "0.15".
const SCREEN_FIELDS = [
  ["revenue_yoy", "増収率", "percent", ["min", "max"]],
  ["gross_margin", "粗利率", "percent", ["min", "max"]],
  ["operating_margin", "営業利益率", "percent", ["min", "max"]],
  ["inventory_days", "在庫日数", "raw", ["min", "max"]],
  ["pe_ratio", "PER", "raw", ["min", "max"]],
  ["pb_ratio", "PBR", "raw", ["min", "max"]],
  ["ps_ratio", "PSR", "raw", ["min", "max"]],
  ["fcf_yield", "FCF利回り", "percent", ["min", "max"]],
  ["volume_ratio_60d", "出来高比", "raw", ["min", "max"]],
  ["rsi_14", "RSI", "raw", ["min", "max"]],
  ["sma_50_gap", "50日乖離", "percent", ["min", "max"]],
  ["sma_200_gap", "200日乖離", "percent", ["min", "max"]],
  ["drawdown_from_52w_high", "52週高値から", "percent", ["min", "max"]],
  ["return_3m", "3ヶ月リターン", "percent", ["min", "max"]],
];

const screen = new Map();

function activeScreen() {
  return [...screen.entries()].filter(([, value]) => Number.isFinite(value));
}

function passesScreen(row) {
  for (const [key, threshold] of activeScreen()) {
    const [field, bound] = key.split(":");
    const value = row[field];
    // A missing value is "cannot tell", not "fails". Dropping it is the safe
    // side for a screen: a symbol shown as matching should actually match.
    if (value == null) return false;
    if (bound === "min" && value < threshold) return false;
    if (bound === "max" && value > threshold) return false;
  }
  return true;
}

function visibleRows() {
  const subsector = document.getElementById("subsector-filter").value;
  const completeOnly = document.getElementById("hide-incomplete").checked;
  let selected = rows.filter(
    (row) => inList(row) && inMarket(row) && passesScreen(row),
  );
  if (subsector) selected = selected.filter((row) => row.subsector === subsector);
  if (completeOnly) {
    selected = selected.filter(
      (row) => row.gross_margin != null && row.inventory_days != null,
    );
  }
  return [...selected].sort((a, b) => {
    const left = a[sortKey];
    const right = b[sortKey];
    // Missing values sort last in both directions: a filer that did not
    // report something should not top the table for it.
    if (left == null && right == null) return 0;
    if (left == null) return 1;
    if (right == null) return -1;
    if (typeof left === "string") {
      return sortDescending ? right.localeCompare(left) : left.localeCompare(right);
    }
    return sortDescending ? right - left : left - right;
  });
}

function render() {
  const body = document.querySelector("#cross-section tbody");
  body.replaceChildren();
  const selected = visibleRows();
  const held = members();
  for (const row of selected) {
    const tr = document.createElement("tr");

    // Present in every row so the column's width does not jump when editing
    // starts; CSS hides it while the table is not in edit mode.
    const membership = document.createElement("td");
    membership.className = "member-cell";
    if (editing) {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = held.has(row.symbol);
      box.title = `${currentList()?.name ?? ""} に入れる`;
      box.addEventListener("change", () => setMembership(row.symbol, box.checked));
      membership.append(box);
    }
    tr.append(membership);

    const symbol = document.createElement("td");
    const link = document.createElement("button");
    link.type = "button";
    link.className = "linklike";
    link.textContent = row.symbol;
    link.addEventListener("click", () => toggleDetail(tr, row.symbol));
    symbol.append(link);
    // A 4-digit Japanese code says nothing on its own, and neither do half the
    // US tickers. Absent until the metrics job has run with names in it.
    if (row.name) {
      const name = document.createElement("span");
      name.className = "entity-name";
      name.textContent = row.name;
      symbol.append(name);
    }
    tr.append(symbol);

    const subsector = document.createElement("td");
    subsector.textContent = subsectorLabel(row.subsector);
    // The slug stays reachable: it is what the CSVs and the corpus use, so a
    // reader tracing a row back to its source needs it.
    if (row.subsector) subsector.title = row.subsector;
    tr.append(subsector);

    const period = document.createElement("td");
    period.textContent = row.period_end ?? "";
    if (row.basis) {
      const basis = document.createElement("div");
      basis.className = "fundamentals-delta";
      basis.textContent = row.basis === "ttm" ? "TTM" : "通期";
      basis.title = row.basis === "ttm"
        ? `直近4四半期の合計。通期の最新は ${row.annual_period_end ?? "不明"}`
        : "通期決算。四半期報告が無いか、通期の方が新しい";
      period.append(basis);
    }
    tr.append(period);

    tr.append(cell(money(row.revenue, row.currency)));
    tr.append(cell(percent(row.revenue_yoy), row.revenue_yoy_change, "revenue_yoy"));
    tr.append(cell(percent(row.gross_margin), row.gross_margin_yoy_change, "gross_margin"));
    tr.append(
      cell(percent(row.operating_margin), row.operating_margin_yoy_change, "operating_margin"),
    );
    tr.append(
      cell(
        row.inventory_days == null ? "" : `${DAYS.format(row.inventory_days)}日`,
        row.inventory_days_yoy_change,
        "inventory_days",
      ),
    );
    tr.append(cell(percent(row.capex_intensity)));
    tr.append(cell(percent(row.rd_intensity)));
    tr.append(cell(percent(row.free_cash_flow_margin)));
    tr.append(cell(row.market_cap == null ? "" : money(row.market_cap, "USD")));
    tr.append(cell(multiple(row.pe_ratio)));
    tr.append(cell(multiple(row.pb_ratio)));
    tr.append(cell(multiple(row.ps_ratio)));
    tr.append(cell(percent(row.fcf_yield)));
    tr.append(cell(multiple(row.volume_ratio_60d)));
    tr.append(cell(row.rsi_14 == null ? "" : MULTIPLE.format(row.rsi_14)));
    tr.append(cell(percent(row.sma_50_gap)));
    tr.append(cell(percent(row.sma_200_gap)));
    tr.append(cell(percent(row.drawdown_from_52w_high)));
    tr.append(cell(percent(row.return_3m)));
    tr.append(
      cell(row.improving_measured ? `${row.improving}/${row.improving_measured}` : ""),
    );
    body.append(tr);
  }
  document.getElementById("cross-section").classList.toggle("editing", editing);
  document.getElementById("row-count").textContent = `${selected.length} 銘柄`;
  // The warning belongs to the mixed view only, and only when there is in
  // fact more than one market to mix.
  const shown = new Set(selected.map(marketOf));
  document.getElementById("market-note").hidden =
    Boolean(document.getElementById("market-filter").value) || shown.size < 2;
  renderPending(selected);
  const conditions = activeScreen().length;
  const note = document.getElementById("screen-note");
  note.hidden = conditions === 0;
  note.textContent = conditions
    ? `${conditions}件の条件で絞り込み中。値が無い銘柄はその条件で外れています。`
    : "";
}

function buildScreen() {
  const grid = document.getElementById("screen-grid");
  grid.replaceChildren();
  for (const [field, label, unit, bounds] of SCREEN_FIELDS) {
    const wrap = document.createElement("div");
    wrap.className = "screen-field";
    const name = document.createElement("span");
    name.textContent = unit === "percent" ? `${label}（%）` : label;
    wrap.append(name);
    for (const bound of bounds) {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "any";
      input.placeholder = bound === "min" ? "以上" : "以下";
      input.dataset.key = `${field}:${bound}`;
      input.dataset.unit = unit;
      input.addEventListener("input", () => {
        const raw = Number.parseFloat(input.value);
        const value = unit === "percent" ? raw / 100 : raw;
        if (Number.isFinite(value)) screen.set(input.dataset.key, value);
        else screen.delete(input.dataset.key);
        render();
      });
      wrap.append(input);
    }
    grid.append(wrap);
  }
  document.getElementById("screen-reset").addEventListener("click", () => {
    screen.clear();
    for (const input of grid.querySelectorAll("input")) input.value = "";
    render();
  });
}

// A symbol can be in a list before anything has been fetched for it. Saying
// so is the difference between "added and waiting" and "added and broken".
function renderPending(selected) {
  const note = document.getElementById("pending-note");
  const known = new Set(rows.map((row) => row.symbol));
  const list = currentList();
  const wanted = list
    ? list.symbols.filter((symbol) => !known.has(symbol))
    : registered.map((entry) => entry.code).filter((code) => !known.has(code));
  if (!wanted.length || editing) {
    note.hidden = true;
    return;
  }
  const named = wanted.map((symbol) => {
    const entry = registered.find((company) => company.code === symbol);
    return entry ? `${symbol} ${entry.name}` : symbol;
  });
  note.hidden = false;
  note.textContent =
    `取り込み待ち: ${named.join("、")}。` +
    "EDINETの定期実行が提出書類を取得し、指標を組み直すまで表には出ません。";
}

// Rebuilt whenever the view changes: a subsector option that matches nothing
// currently shown would silently produce an empty table.
function fillSubsectors() {
  const filter = document.getElementById("subsector-filter");
  const previous = filter.value;
  const available = [
    ...new Set(
      rows
        .filter((row) => inList(row) && inMarket(row))
        .map((row) => row.subsector)
        .filter(Boolean),
    ),
  ].sort((left, right) => subsectorLabel(left).localeCompare(subsectorLabel(right), "ja"));
  filter.replaceChildren(new Option("すべて", ""));
  for (const subsector of available) {
    filter.append(new Option(subsectorLabel(subsector), subsector));
  }
  filter.value = available.includes(previous) ? previous : "";
}

function refresh() {
  // An open detail row belongs to a symbol that may no longer be listed.
  for (const open of document.querySelectorAll("tr.detail-row")) open.remove();
  fillSubsectors();
  render();
}

function selectList(id) {
  activeList = id;
  editing = false;
  renderListTabs();
  refresh();
}

function renderListTabs() {
  const tabs = document.getElementById("list-tabs");
  tabs.replaceChildren();
  const known = new Set(rows.map((row) => row.symbol));
  const choices = [
    [ALL_LISTS, "すべて", rows.length],
    ...lists.map((entry) => [
      entry.id,
      entry.name,
      entry.symbols.filter((symbol) => known.has(symbol)).length,
    ]),
  ];
  for (const [value, label, count] of choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "cross-section");
    button.textContent = `${label} (${count})`;
    button.className = value === activeList ? "active" : "";
    button.setAttribute("aria-selected", String(value === activeList));
    button.addEventListener("click", () => selectList(value));
    tabs.append(button);
  }
  if (lists.length < maxLists) {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "tab-add";
    add.textContent = "＋ リスト";
    add.title = `リストを作る（最大${maxLists}件）`;
    add.addEventListener("click", createList);
    tabs.append(add);
  }
  renderListActions();
}

function renderListActions() {
  const actions = document.getElementById("list-actions");
  actions.replaceChildren();
  const list = currentList();
  if (!list) {
    if (lists.length >= maxLists) {
      const note = document.createElement("span");
      note.className = "page-note";
      note.textContent = `リストは最大${maxLists}件`;
      actions.append(note);
    }
    return;
  }
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = editing ? "編集を終える" : "銘柄を編集";
  edit.className = editing ? "active" : "";
  edit.addEventListener("click", () => {
    editing = !editing;
    renderListActions();
    refresh();
  });

  const rename = document.createElement("button");
  rename.type = "button";
  rename.textContent = "名前を変える";
  rename.addEventListener("click", async () => {
    const name = window.prompt("リスト名", list.name);
    if (name == null) return;
    await send("PATCH", `/api/watchlists/${list.id}`, { name });
  });

  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "リストを削除";
  remove.addEventListener("click", async () => {
    // The list is a view over symbols that exist elsewhere, so deleting one
    // loses only the grouping -- worth a confirmation, not a warning.
    if (!window.confirm(`「${list.name}」を削除します。銘柄そのものは残ります。`)) return;
    if (await send("DELETE", `/api/watchlists/${list.id}`)) activeList = ALL_LISTS;
  });

  actions.append(edit, rename, remove);
}

async function createList() {
  const name = window.prompt("新しいリストの名前");
  if (name == null) return;
  const created = await send("POST", "/api/watchlists", { name });
  if (created) activeList = created.id;
}

async function setMembership(symbol, wanted) {
  const list = currentList();
  if (!list) return;
  await send(
    wanted ? "POST" : "DELETE",
    wanted
      ? `/api/watchlists/${list.id}/symbols`
      : `/api/watchlists/${list.id}/symbols/${encodeURIComponent(symbol)}`,
    wanted ? { symbol } : null,
  );
}

// Every write goes through here so that the store on the server stays the
// single source of truth: the reply is discarded and the lists are re-read,
// rather than patched locally into something that might not match.
async function send(method, url, body) {
  const status = document.getElementById("footer-status");
  try {
    const response = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      status.textContent = `操作できない: ${detail.detail ?? response.status}`;
      return null;
    }
    status.textContent = "";
    const payload = response.status === 204 ? {} : await response.json();
    await loadLists();
    renderListTabs();
    refresh();
    return payload;
  } catch (error) {
    status.textContent = `操作できない: ${error.message}`;
    return null;
  }
}

async function loadLists() {
  try {
    const response = await fetch("/api/watchlists");
    if (!response.ok) return;
    const payload = await response.json();
    lists = payload.lists ?? [];
    registered = payload.companies ?? [];
    maxLists = payload.max_lists ?? maxLists;
    if (activeList && !lists.some((entry) => entry.id === activeList)) {
      activeList = ALL_LISTS;
    }
  } catch {
    // The table is readable without lists; losing them is not worth an error
    // banner over the whole page.
    lists = [];
  }
}

// A step of 1, 2, 2.5 or 5 times a power of ten -- the values people read
// without doing arithmetic. Anything else gives gridlines like 45.2 and 18.0.
function niceStep(rough) {
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1
    : normalized <= 2 ? 2
    : normalized <= 2.5 ? 2.5
    : normalized <= 5 ? 5
    : 10;
  return step * magnitude;
}

function niceScale(min, max, ticks = 4) {
  // Zero is always in range. A level drawn from 95 to 180 exaggerates the
  // swing; from 0 to 200 it reads as the ~40% move it actually is.
  let low = Math.min(0, min);
  let high = Math.max(0, max);
  if (low === high) high = low + 1;
  const step = niceStep((high - low) / ticks);
  low = Math.floor(low / step) * step;
  high = Math.ceil(high / step) * step;
  const values = [];
  // Accumulate off the index: repeated addition drifts on steps like 2.5.
  for (let index = 0; low + step * index <= high + step / 1e6; index += 1) {
    values.push(low + step * index);
  }
  return { low, high, values, step };
}

function tickText(value, step) {
  // Decimals only where the step needs them, so 0/50/100 does not print as
  // 0.0/50.0/100.0 next to a chart that has no use for the precision.
  const decimals = step >= 1 ? 0 : Math.min(2, Math.ceil(-Math.log10(step)));
  return value.toFixed(decimals);
}

function chart(title, series, { scale = 1, unit = "" } = {}) {
  const points = series
    .filter((entry) => entry.value != null)
    .map((entry) => ({ label: entry.label, value: entry.value * scale }));
  const figure = document.createElement("figure");
  figure.className = "fundamentals-chart";
  const caption = document.createElement("figcaption");
  caption.textContent = title;
  figure.append(caption);

  if (points.length < 2) {
    const empty = document.createElement("p");
    empty.className = "page-note";
    empty.textContent = "データが足りない";
    figure.append(empty);
    return figure;
  }

  const values = points.map((entry) => entry.value);
  const axis = niceScale(Math.min(...values), Math.max(...values));

  const width = 300;
  const height = 168;
  // Top padding holds the unit on its own line, clear of the first gridline
  // label -- the two used to sit on top of each other.
  const pad = { left: 42, right: 12, top: 26, bottom: 30 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (index) => pad.left + (index * plotWidth) / (points.length - 1);
  const y = (value) =>
    pad.top + plotHeight - ((value - axis.low) / (axis.high - axis.low)) * plotHeight;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${title} の推移`);

  const add = (name, attributes, text) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) {
      node.setAttribute(key, String(value));
    }
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  };

  // Units live at the ends of the axes, once. Repeating them on every tick
  // makes the numbers long enough to collide with the plot.
  add("text", { x: 2, y: 11, class: "axis-unit", "text-anchor": "start" }, unit);
  add(
    "text",
    { x: width - pad.right, y: height - 4, class: "axis-unit", "text-anchor": "end" },
    "決算年",
  );

  for (const value of axis.values) {
    add("line", {
      x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value),
      class: value === 0 && axis.low < 0 ? "axis-zero" : "axis-grid",
    });
    add("text", {
      x: pad.left - 5, y: y(value) + 3, class: "axis-label", "text-anchor": "end",
    }, tickText(value, axis.step));
  }
  add("line", {
    x1: pad.left, x2: pad.left, y1: pad.top, y2: pad.top + plotHeight, class: "axis-line",
  });
  add("line", {
    x1: pad.left, x2: width - pad.right,
    y1: pad.top + plotHeight, y2: pad.top + plotHeight, class: "axis-line",
  });

  add("text", {
    x: pad.left, y: height - 16, class: "axis-label", "text-anchor": "start",
  }, points[0].label);
  add("text", {
    x: width - pad.right, y: height - 16, class: "axis-label", "text-anchor": "end",
  }, points[points.length - 1].label);

  add("path", {
    d: points
      .map((entry, index) =>
        `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(entry.value).toFixed(1)}`)
      .join(" "),
    class: "spark",
  });
  for (const [index, entry] of points.entries()) {
    add("circle", { cx: x(index), cy: y(entry.value), r: 2.5, class: "spark-point" })
      .append(
        Object.assign(
          document.createElementNS("http://www.w3.org/2000/svg", "title"),
          { textContent: `${entry.label}: ${tickText(entry.value, axis.step)}${unit}` },
        ),
      );
  }
  figure.append(svg);
  return figure;
}

function detailCell(row, columns) {
  const cell = document.createElement("td");
  cell.colSpan = columns;

  const annual = row.history ?? [];
  const quarterly = row.quarterly_history ?? [];

  const heading = document.createElement("p");
  heading.className = "page-note";
  cell.append(heading);

  // Quarterly is the default where it exists: a year averages away the turn
  // that the quarters make visible.
  const controls = document.createElement("div");
  controls.className = "fundamentals-controls";
  const modes = [
    ["quarter", `四半期（${quarterly.length}期）`, quarterly],
    ["annual", `年次（${annual.length}期）`, annual],
  ].filter(([, , series]) => series.length >= 2);
  let mode = modes.length ? modes[0][0] : null;

  const charts = document.createElement("div");
  charts.className = "fundamentals-charts";

  const draw = () => {
    const source = mode === "annual" ? annual : quarterly;
    // Quarterly points need the month too; two 2025 labels tell you nothing.
    const label = (value) =>
      mode === "annual"
        ? String(value).slice(0, 4)
        : String(value).slice(2, 7).replace("-", "/");
    const series = (key) =>
      source.map((entry) => ({ label: label(entry.period_end), value: entry[key] }));
    const asPercent = { scale: 100, unit: "%" };
    charts.replaceChildren(
      chart("売上", series("revenue"), { scale: 1e-9, unit: `10億 ${row.currency ?? ""}` }),
      chart("増収率", series("revenue_yoy"), asPercent),
      chart("粗利率", series("gross_margin"), asPercent),
      chart("営業利益率", series("operating_margin"), asPercent),
      chart("在庫日数", series("inventory_days"), { unit: "日" }),
      chart("設備投資／売上", series("capex_intensity"), asPercent),
      chart("R&D／売上", series("rd_intensity"), asPercent),
      chart("FCFマージン", series("free_cash_flow_margin"), asPercent),
    );
    const span = mode === "annual" ? "年次" : "四半期";
    heading.textContent =
      `${row.symbol}${row.name ? ` ${row.name}` : ""}` +
      ` ・ ${marketLabel(row)} ・ ${subsectorLabel(row.subsector)}` +
      ` ・ 通貨 ${row.currency ?? "不明"}` +
      (row.valuation_withheld ? ` ・ 株価指標なし（${WITHHELD_REASONS[row.valuation_withheld] ?? row.valuation_withheld}）` : "") +
      ` ・ 直近${source.length}期の${span}実績。単位は各軸の端に示す` +
      (mode === "quarter"
        ? "。四半期の売上・利益はその3ヶ月分で、年次と直接は比べられない"
        : "");
  };

  for (const [value, text] of modes) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.className = value === mode ? "active" : "";
    button.addEventListener("click", () => {
      mode = value;
      for (const other of controls.querySelectorAll("button")) {
        other.className = other === button ? "active" : "";
      }
      draw();
    });
    controls.append(button);
  }
  if (modes.length > 1) cell.append(controls);

  if (!modes.length) {
    heading.textContent =
      `${row.symbol}${row.name ? ` ${row.name}` : ""}` +
      ` ・ ${marketLabel(row)} ・ ${subsectorLabel(row.subsector)}` +
      " ・ 推移を描けるだけの期間がまだない";
  } else {
    draw();
  }

  const guide = row.narrative;
  if (guide) {
    const box = document.createElement("div");
    box.className = "narrative";
    const overview = document.createElement("p");
    overview.className = "narrative-overview";
    overview.textContent = guide.overview ?? "";
    box.append(overview);
    for (const point of guide.points ?? []) {
      const item = document.createElement("div");
      item.className = "narrative-point";
      const title = document.createElement("strong");
      title.textContent = point.title ?? "";
      const body = document.createElement("span");
      body.textContent = ` — ${point.interpretation ?? ""}`;
      item.append(title, body);
      // The figures come from the metrics, never from the model's prose.
      const evidence = document.createElement("p");
      evidence.className = "narrative-evidence";
      evidence.textContent = (point.evidence ?? []).map((e) => e.text).join(" ／ ");
      item.append(evidence);
      box.append(item);
    }
    if (guide.caution) {
      const caution = document.createElement("p");
      caution.className = "narrative-caution";
      caution.textContent = `注意: ${guide.caution}`;
      box.append(caution);
    }
    const provenance = document.createElement("p");
    provenance.className = "page-note";
    provenance.textContent =
      `解説はローカル${guide.model ?? "LLM"}が指標を読んだもの。` +
      "数値は指標からの引用で、モデルが書いたものではない";
    box.append(provenance);
    cell.append(box);
  }

  cell.append(charts);
  return cell;
}

async function toggleDetail(tr, symbol) {
  const existing = tr.nextElementSibling;
  if (existing?.classList.contains("detail-row")) {
    existing.remove();
    return;
  }
  // Only one open at a time: several expanded blocks push the table apart
  // and defeat the point of opening it in place.
  for (const open of document.querySelectorAll("tr.detail-row")) open.remove();

  const response = await fetch(`/api/fundamentals/${encodeURIComponent(symbol)}`);
  if (!response.ok) return;
  const row = await response.json();
  const detail = document.createElement("tr");
  detail.className = "detail-row";
  // Only the columns actually on screen: the membership cell is present in
  // every row but hidden outside edit mode, and counting it would stretch the
  // detail cell past the table.
  const columns = [...tr.children].filter((cell) => cell.offsetParent !== null).length;
  detail.append(detailCell(row, columns || tr.children.length));
  tr.after(detail);
}

// ---------------------------------------------------------- company search

let searchTimer = null;

function wireCompanySearch() {
  const query = document.getElementById("company-query");
  const chooser = document.getElementById("company-subsector");
  // The same taxonomy the table groups by, so an added company lands in a
  // heading that already means something rather than only in 未分類.
  chooser.replaceChildren();
  const known = [...new Set([...rows.map((row) => row.subsector), "unclassified"])]
    .filter(Boolean)
    .sort((left, right) => subsectorLabel(left).localeCompare(subsectorLabel(right), "ja"));
  for (const subsector of known) {
    chooser.append(new Option(subsectorLabel(subsector), subsector));
  }
  chooser.value = "unclassified";

  query.addEventListener("input", () => {
    // Debounced: the list is thousands of companies and the user is typing a
    // name a character at a time.
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(runCompanySearch, 250);
  });
}

async function runCompanySearch() {
  const query = document.getElementById("company-query").value.trim();
  const note = document.getElementById("company-search-note");
  const results = document.getElementById("company-results");
  if (!query) {
    results.replaceChildren();
    note.textContent = "";
    return;
  }
  let payload;
  try {
    const response = await fetch(`/api/companies/search?q=${encodeURIComponent(query)}`);
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      note.textContent = `検索できない: ${detail.detail ?? response.status}`;
      results.replaceChildren();
      return;
    }
    payload = await response.json();
  } catch (error) {
    note.textContent = `検索できない: ${error.message}`;
    return;
  }
  const found = payload.results ?? [];
  note.textContent = found.length ? "" : "該当なし";
  results.replaceChildren();
  for (const company of found) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${company.code} ${company.name}`;
    const industry = document.createElement("span");
    industry.className = "page-note";
    // EDINET's own industry, shown as context for the subsector choice; it is
    // a different taxonomy and is never mapped onto ours automatically.
    industry.textContent = company.industry ?? "";
    const action = document.createElement("button");
    action.type = "button";
    if (company.added) {
      action.textContent = "削除";
      action.addEventListener("click", async () => {
        if (!window.confirm(`${company.name} を取得対象から外します。`)) return;
        if (await send("DELETE", `/api/companies/${encodeURIComponent(company.code)}`)) {
          runCompanySearch();
        }
      });
    } else {
      action.textContent = "追加";
      action.addEventListener("click", async () => {
        const created = await send("POST", "/api/companies", {
          code: company.code,
          subsector: document.getElementById("company-subsector").value,
          list_id: activeList || null,
        });
        if (created) runCompanySearch();
      });
    }
    item.append(label, industry, action);
    results.append(item);
  }
}

async function load() {
  const status = document.getElementById("footer-status");
  let payload;
  try {
    const response = await fetch("/api/fundamentals");
    if (response.status === 404) {
      status.textContent = "決算指標がまだ生成されていない";
      return;
    }
    if (!response.ok) throw new Error(String(response.status));
    payload = await response.json();
  } catch (error) {
    status.textContent = `決算指標を取得できない: ${error.message}`;
    return;
  }

  rows = payload.symbols ?? [];
  document.getElementById("meta").textContent =
    `${rows.length} 銘柄 ・ 生成 ${String(payload.generated_at).slice(0, 19).replace("T", " ")}`;
  document.getElementById("improving-header").title =
    `改善した指標の数 / 測定できた指標の数（${(payload.direction_metrics ?? []).join(", ")}）`;

  await loadLists();
  renderListTabs();
  fillSubsectors();
  wireCompanySearch();
  buildScreen();

  document.getElementById("market-filter").addEventListener("change", () => {
    fillSubsectors();
    render();
  });
  document.getElementById("subsector-filter").addEventListener("change", render);
  document.getElementById("hide-incomplete").addEventListener("change", render);
  for (const header of document.querySelectorAll("th.sortable")) {
    header.addEventListener("click", () => {
      const key = header.dataset.sort;
      if (key === sortKey) {
        sortDescending = !sortDescending;
      } else {
        sortKey = key;
        sortDescending = key !== "symbol" && key !== "subsector" && key !== "period_end";
      }
      for (const other of document.querySelectorAll("th.sortable")) {
        other.removeAttribute("aria-sort");
      }
      header.setAttribute("aria-sort", sortDescending ? "descending" : "ascending");
      render();
    });
  }
  render();
}

load();
