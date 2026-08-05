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

function chart(title, series, format, axisLabel) {
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
  // The vertical axis includes zero whenever the data straddles it, so a
  // margin near break-even is not drawn as though it were mid-range.
  let low = Math.min(...values, values.some((v) => v < 0) ? 0 : Math.min(...values));
  let high = Math.max(...values, 0 > Math.max(...values) ? 0 : Math.max(...values));
  if (low === high) {
    low -= Math.abs(low) || 1;
    high += Math.abs(high) || 1;
  }
  const span = high - low;

  const width = 300;
  const height = 150;
  const pad = { left: 56, right: 10, top: 10, bottom: 28 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (index) => pad.left + (index * plotWidth) / (points.length - 1);
  const y = (value) => pad.top + plotHeight - ((value - low) / span) * plotHeight;

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

  // Three horizontal gridlines with their values: without them the line shows
  // a shape but no magnitude, which is not enough to judge a level by.
  for (const fraction of [0, 0.5, 1]) {
    const value = low + span * fraction;
    add("line", {
      x1: pad.left, x2: width - pad.right,
      y1: y(value), y2: y(value), class: "axis-grid",
    });
    add("text", {
      x: pad.left - 6, y: y(value) + 3, class: "axis-label", "text-anchor": "end",
    }, format(value));
  }
  add("line", {
    x1: pad.left, x2: pad.left, y1: pad.top, y2: pad.top + plotHeight, class: "axis-line",
  });
  add("line", {
    x1: pad.left, x2: width - pad.right,
    y1: pad.top + plotHeight, y2: pad.top + plotHeight, class: "axis-line",
  });

  // Only the ends are labelled on the time axis; five ticks in 300px collide.
  add("text", {
    x: pad.left, y: height - 8, class: "axis-label", "text-anchor": "start",
  }, points[0].label);
  add("text", {
    x: width - pad.right, y: height - 8, class: "axis-label", "text-anchor": "end",
  }, points[points.length - 1].label);
  add("text", {
    x: 4, y: pad.top + 4, class: "axis-label", "text-anchor": "start",
  }, axisLabel);

  add("path", {
    d: points.map((entry, index) =>
      `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(entry.value).toFixed(1)}`
    ).join(" "),
    class: "spark",
  });
  for (const [index, entry] of points.entries()) {
    add("circle", { cx: x(index), cy: y(entry.value), r: 2.5, class: "spark-point" })
      .append(
        Object.assign(
          document.createElementNS("http://www.w3.org/2000/svg", "title"),
          { textContent: `${entry.label}: ${format(entry.value)}` },
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
    ` ・ 直近${(row.history ?? []).length}期の年次実績（横軸=決算年、縦軸=各指標）`;
  cell.append(heading);

  const history = row.history ?? [];
  const series = (key) =>
    history.map((entry) => ({
      label: String(entry.period_end).slice(0, 4),
      value: entry[key],
    }));
  const charts = document.createElement("div");
  charts.className = "fundamentals-charts";
  charts.append(
    chart("売上", series("revenue"), (v) => money(v, row.currency), `10億 ${row.currency ?? ""}`),
    chart("増収率", series("revenue_yoy"), percent, "%"),
    chart("粗利率", series("gross_margin"), percent, "%"),
    chart("営業利益率", series("operating_margin"), percent, "%"),
    chart("在庫日数", series("inventory_days"), (v) => `${DAYS.format(v)}日`, "日"),
    chart("設備投資／売上", series("capex_intensity"), percent, "%"),
    chart("R&D／売上", series("rd_intensity"), percent, "%"),
    chart("FCFマージン", series("free_cash_flow_margin"), percent, "%"),
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
