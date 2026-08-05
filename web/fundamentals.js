// Cross-sectional fundamentals table and a per-symbol history view.
//
// The API serves a file the metrics job prepared, so there is no computation
// here beyond formatting and sorting: any number shown came from the XBRL
// facts unchanged.

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

let rows = [];
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

function visibleRows() {
  const subsector = document.getElementById("subsector-filter").value;
  const completeOnly = document.getElementById("hide-incomplete").checked;
  let selected = rows;
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
  for (const row of selected) {
    const tr = document.createElement("tr");

    const symbol = document.createElement("td");
    const link = document.createElement("button");
    link.type = "button";
    link.className = "linklike";
    link.textContent = row.symbol;
    link.addEventListener("click", () => toggleDetail(tr, row.symbol));
    symbol.append(link);
    tr.append(symbol);

    const subsector = document.createElement("td");
    subsector.textContent = row.subsector ?? "";
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
    tr.append(
      cell(row.improving_measured ? `${row.improving}/${row.improving_measured}` : ""),
    );
    body.append(tr);
  }
  document.getElementById("row-count").textContent = `${selected.length} 銘柄`;
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
  const heading = document.createElement("p");
  heading.className = "page-note";
  heading.textContent =
    `${row.symbol} ・ ${row.subsector ?? ""} ・ 通貨 ${row.currency ?? "不明"}` +
    ` ・ 直近${(row.history ?? []).length}期の年次実績。単位は各軸の端に示す`;
  cell.append(heading);

  const history = row.history ?? [];
  const series = (key) =>
    history.map((entry) => ({
      label: String(entry.period_end).slice(0, 4),
      value: entry[key],
    }));
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

  const charts = document.createElement("div");
  charts.className = "fundamentals-charts";
  const asPercent = { scale: 100, unit: "%" };
  charts.append(
    chart("売上", series("revenue"), { scale: 1e-9, unit: `10億 ${row.currency ?? ""}` }),
    chart("増収率", series("revenue_yoy"), asPercent),
    chart("粗利率", series("gross_margin"), asPercent),
    chart("営業利益率", series("operating_margin"), asPercent),
    chart("在庫日数", series("inventory_days"), { unit: "日" }),
    chart("設備投資／売上", series("capex_intensity"), asPercent),
    chart("R&D／売上", series("rd_intensity"), asPercent),
    chart("FCFマージン", series("free_cash_flow_margin"), asPercent),
  );
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
  detail.append(detailCell(row, tr.children.length));
  tr.after(detail);
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

  const filter = document.getElementById("subsector-filter");
  for (const subsector of [...new Set(rows.map((row) => row.subsector).filter(Boolean))].sort()) {
    const option = document.createElement("option");
    option.value = subsector;
    option.textContent = subsector;
    filter.append(option);
  }

  filter.addEventListener("change", render);
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
