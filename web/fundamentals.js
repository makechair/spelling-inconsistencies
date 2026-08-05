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
    link.addEventListener("click", () => showDetail(row.symbol));
    symbol.append(link);
    tr.append(symbol);

    const subsector = document.createElement("td");
    subsector.textContent = row.subsector ?? "";
    tr.append(subsector);

    const period = document.createElement("td");
    period.textContent = row.period_end ?? "";
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

function sparkline(title, series, format) {
  const points = series.filter((entry) => entry.value != null);
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
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low || Math.abs(high) || 1;
  const width = 260;
  const height = 90;
  const step = width / (points.length - 1);
  const path = points
    .map((entry, index) => {
      const x = index * step;
      const y = height - ((entry.value - low) / span) * (height - 12) - 6;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${title} の推移`);
  const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
  line.setAttribute("d", path);
  line.setAttribute("class", "spark");
  svg.append(line);
  figure.append(svg);

  const range = document.createElement("p");
  range.className = "page-note";
  const first = points[0];
  const last = points[points.length - 1];
  range.textContent =
    `${first.label}: ${format(first.value)} → ${last.label}: ${format(last.value)}`;
  figure.append(range);
  return figure;
}

async function showDetail(symbol) {
  const response = await fetch(`/api/fundamentals/${encodeURIComponent(symbol)}`);
  if (!response.ok) return;
  const row = await response.json();
  const history = row.history ?? [];
  document.getElementById("detail-title").textContent = `${row.symbol} の推移`;
  document.getElementById("detail-meta").textContent =
    `${row.subsector ?? ""} ・ 通貨 ${row.currency ?? "不明"} ・ 直近決算期 ${row.period_end}`;

  const series = (key) =>
    history.map((entry) => ({
      label: String(entry.period_end).slice(0, 4),
      value: entry[key],
    }));
  const charts = document.getElementById("detail-charts");
  charts.replaceChildren(
    sparkline("売上", series("revenue"), (v) => money(v, row.currency)),
    sparkline("増収率", series("revenue_yoy"), percent),
    sparkline("粗利率", series("gross_margin"), percent),
    sparkline("営業利益率", series("operating_margin"), percent),
    sparkline("在庫日数", series("inventory_days"), (v) => `${DAYS.format(v)}日`),
    sparkline("設備投資／売上", series("capex_intensity"), percent),
    sparkline("R&D／売上", series("rd_intensity"), percent),
    sparkline("FCFマージン", series("free_cash_flow_margin"), percent),
  );
  const detail = document.getElementById("detail");
  detail.hidden = false;
  detail.scrollIntoView({ behavior: "smooth", block: "start" });
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
  document.getElementById("history-years").textContent = String(payload.history_years ?? "");
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
  document.getElementById("close-detail").addEventListener("click", () => {
    document.getElementById("detail").hidden = true;
  });
  render();
}

load();
